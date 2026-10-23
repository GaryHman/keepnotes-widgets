"""Unit tests for the sync layer against a mocked Google Keep backend.

A real gkeepapi.Keep object is used, but its network layer (KeepAPI.changes
and the auth step) is replaced by an in-memory FakeServer, so dirty tracking,
dump/restore and node parsing are the real library code.
"""
import copy
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gkeepapi  # noqa: E402
import keep_sync as ks  # noqa: E402

EMAIL = "test@example.com"
TOKEN = "aas_et/" + "x" * 60


def strip_dirty(obj):
    """Server payloads never carry gkeepapi's local '_dirty' markers."""
    if isinstance(obj, dict):
        return {k: strip_dirty(v) for k, v in obj.items() if k != "_dirty"}
    if isinstance(obj, list):
        return [strip_dirty(v) for v in obj]
    return obj


class FakeServer:
    def __init__(self):
        self.nodes = {}  # id -> raw dict
        self.versions = {}  # id -> version when last changed
        self.version = 0
        self.online = True
        self.calls = 0
        self.pushed = []

    def changes(self, target_version=None, nodes=None, labels=None):
        self.calls += 1
        if not self.online:
            raise requests.ConnectionError("network down")
        since = int(target_version or 0)
        for raw in nodes or []:
            raw = copy.deepcopy(raw)
            raw.setdefault("serverId", "srv-" + raw["id"])
            self.version += 1
            self.nodes[raw["id"]] = raw
            self.versions[raw["id"]] = self.version
            self.pushed.append(raw["id"])
        out = [copy.deepcopy(self.nodes[i]) for i, v in self.versions.items() if v > since]
        return {"toVersion": str(self.version), "truncated": False, "nodes": out}

    def remote_edit(self, node_id, **fields):
        self.version += 1
        self.nodes[node_id].update(fields)
        self.versions[node_id] = self.version

    def remote_create_note(self, node_id, title, text):
        self.version += 1
        k = gkeepapi.Keep()
        n = k.createNote(title, text)
        raw = strip_dirty(n.save(False))
        raw["id"] = node_id
        raw["serverId"] = "srv-" + node_id
        self.nodes[node_id] = raw
        self.versions[node_id] = self.version
        for child in n.children:  # a Keep note's body text lives in a child node
            craw = strip_dirty(child.save(False))
            craw["id"] = node_id + "-text"
            craw["parentId"] = node_id
            craw["parentServerId"] = "srv-" + node_id
            craw["serverId"] = "srv-" + craw["id"]
            self.nodes[craw["id"]] = craw
            self.versions[craw["id"]] = self.version

    def text_node(self, note_id):
        return next(r for r in self.nodes.values() if r.get("parentId") == note_id)

    def remote_edit_text(self, note_id, text):
        self.remote_edit(self.text_node(note_id)["id"], text=text)


def make_keep(server, auth_ok=True):
    keep = gkeepapi.Keep()
    keep._keep_api.changes = server.changes

    def fake_authenticate(email, master_token, state=None, sync=True, device_id=None):
        if not server.online:
            raise requests.ConnectionError("network down")
        if not auth_ok:
            raise gkeepapi.exception.LoginException("BadAuthentication")
        assert master_token == TOKEN
        if state is not None:
            keep.restore(state)
        if sync:
            keep.sync()

    keep.authenticate = fake_authenticate
    return keep


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def engine(tmp_path, server):
    eng = ks.SyncEngine(keep=make_keep(server), creds=ks.MemoryCredentialStore(EMAIL, TOKEN),
                        directory=tmp_path, keep_factory=lambda: make_keep(server))
    eng.start()
    return eng


def test_start_signs_in_and_initial_sync(engine, server):
    assert engine.authenticated and engine.email == EMAIL
    server.remote_create_note("r1", "Groceries", "milk")
    res = engine.sync()
    assert res.ok and "r1" in res.changed
    snap = engine.snapshot()
    assert snap["r1"].title == "Groceries" and snap["r1"].text == "milk"
    assert engine.status == "ok"


def test_create_note_and_list_push(engine, server):
    engine.submit({"op": "create", "kind": "note", "title": "Hello", "text": "World", "tag": "t1"})
    engine.submit({"op": "create", "kind": "list", "title": "Todo", "items": ["a", "b"]})
    events = engine.apply_pending()
    assert [e["event"] for e in events] == ["created", "created"] and events[0]["tag"] == "t1"
    res = engine.sync()
    assert res.ok
    note_id = events[0]["id"]
    assert server.nodes[note_id]["title"] == "Hello"
    list_id = events[1]["id"]
    item_texts = sorted(r["text"] for r in server.nodes.values() if r.get("parentId") == list_id)
    assert item_texts == ["a", "b"]
    assert not engine.has_local_changes()


def test_edit_title_text_coalesced(engine, server):
    engine.submit({"op": "create", "kind": "note", "title": "T", "text": ""})
    nid = engine.apply_pending()[0]["id"]
    engine.sync()
    for t in ["a", "ab", "abc"]:
        engine.submit({"op": "set_text", "id": nid, "value": t})
    assert engine.pending_ops() == 1
    engine.submit({"op": "set_title", "id": nid, "value": "New title"})
    engine.apply_pending()
    engine.sync()
    assert server.text_node(nid)["text"] == "abc" and server.nodes[nid]["title"] == "New title"


def test_check_item_add_delete(engine, server):
    engine.submit({"op": "create", "kind": "list", "title": "L", "items": ["x", "y"]})
    lid = engine.apply_pending()[0]["id"]
    engine.sync()
    items = engine.snapshot()[lid].items
    x = next(i for i in items if i.text == "x")
    engine.submit({"op": "set_item_checked", "id": lid, "item_id": x.id, "value": True})
    engine.submit({"op": "add_item", "id": lid, "text": "z"})
    y = next(i for i in items if i.text == "y")
    engine.submit({"op": "delete_item", "id": lid, "item_id": y.id})
    engine.apply_pending()
    assert engine.sync().ok
    assert server.nodes[x.id]["checked"] is True
    assert any(r["text"] == "z" for r in server.nodes.values() if r.get("parentId") == lid)
    snap_items = {i.text: i.checked for i in engine.snapshot()[lid].items}
    assert snap_items == {"x": True, "z": False}


def test_remote_change_appears(engine, server):
    server.remote_create_note("r2", "Remote", "v1")
    engine.sync()
    server.remote_edit("r2", title="Remote edited")
    server.remote_edit_text("r2", "v2 from phone")
    res = engine.sync()
    assert "r2" in res.changed
    assert engine.snapshot()["r2"].text == "v2 from phone"


def test_remote_trash_removes_widget(engine, server):
    server.remote_create_note("r3", "Bye", "x")
    engine.sync()
    raw = server.nodes["r3"]
    raw["timestamps"]["trashed"] = "2026-10-20T12:00:00.000000Z"
    server.remote_edit("r3")
    res = engine.sync()
    assert "r3" in res.removed and "r3" not in engine.snapshot()


def test_conflict_last_write_wins_logged(engine, server, caplog):
    server.remote_create_note("r4", "Shared", "base")
    engine.sync()
    engine.submit({"op": "set_text", "id": "r4", "value": "local edit"})
    engine.apply_pending()
    # Server overrides whatever we push with a newer remote write.
    orig = server.changes

    def changes_with_remote_win(target_version=None, nodes=None, labels=None):
        out = orig(target_version, nodes, labels)
        server.remote_edit_text("r4", "phone edit (newer)")
        return orig(out["toVersion"], [], None)

    server.changes = changes_with_remote_win
    engine.keep._keep_api.changes = server.changes
    with caplog.at_level("WARNING", logger="keepnotes"):
        res = engine.sync()
    assert "r4" in res.conflicts
    assert engine.snapshot()["r4"].text == "phone edit (newer)"
    assert "last write wins" in caplog.text


def test_offline_keeps_local_edits_and_resumes(engine, server, tmp_path):
    server.remote_create_note("r5", "Offline", "start")
    engine.sync()
    server.online = False
    engine.submit({"op": "set_text", "id": "r5", "value": "written offline"})
    engine.apply_pending()
    res = engine.sync()
    assert not res.ok and engine.status == "paused" and "offline" in engine.last_error
    assert engine.snapshot()["r5"].text == "written offline"  # still visible locally

    # App restarts while still offline: cache restores notes, dirty flag survives.
    eng2 = ks.SyncEngine(keep=make_keep(server), creds=ks.MemoryCredentialStore(EMAIL, TOKEN),
                         directory=tmp_path, keep_factory=lambda: make_keep(server))
    eng2.start()
    assert eng2.status == "paused" and not eng2.authenticated
    assert eng2.snapshot()["r5"].text == "written offline"
    assert eng2.has_local_changes()

    server.online = True
    res = eng2.sync()
    assert res.ok and eng2.status == "ok"
    assert server.text_node("r5")["text"] == "written offline"


def test_colour_push_failure_is_local_only(engine, server, monkeypatch):
    server.remote_create_note("r6", "Colour", "")
    engine.sync()
    engine.submit({"op": "set_color", "id": "r6", "value": "NOT_A_COLOUR"})
    ev = engine.apply_pending()
    assert ev == [{"event": "color_local", "id": "r6"}]
    engine.submit({"op": "set_color", "id": "r6", "value": "GREEN"})
    engine.apply_pending()
    assert engine.sync().ok and server.nodes["r6"]["color"] == "GREEN"


def test_trash_and_pin(engine, server):
    server.remote_create_note("r7", "Pin me", "")
    engine.sync()
    engine.submit({"op": "set_pinned", "id": "r7", "value": True})
    engine.apply_pending(); engine.sync()
    assert server.nodes["r7"]["isPinned"] is True and engine.snapshot()["r7"].pinned
    engine.submit({"op": "trash", "id": "r7"})
    engine.apply_pending(); engine.sync()
    assert "r7" not in engine.snapshot()
    assert server.nodes["r7"]["timestamps"]["trashed"] not in (None, "1970-01-01T00:00:00.000000Z")


def test_bad_login_pauses(tmp_path, server):
    eng = ks.SyncEngine(keep=make_keep(server, auth_ok=False),
                        creds=ks.MemoryCredentialStore(EMAIL, TOKEN), directory=tmp_path)
    eng.start()
    assert eng.status == "paused" and "sign in again" in eng.last_error


def test_signed_out_without_creds(tmp_path, server):
    eng = ks.SyncEngine(keep=make_keep(server), creds=ks.MemoryCredentialStore(), directory=tmp_path)
    eng.start()
    assert eng.status == "signed_out"
    assert eng.sync().ok is False


def test_token_never_written_to_disk(engine, server, tmp_path):
    engine.submit({"op": "create", "kind": "note", "title": "x", "text": "y"})
    engine.apply_pending(); engine.sync()
    for f in tmp_path.iterdir():
        assert TOKEN not in f.read_text(encoding="utf-8", errors="ignore"), f


def test_exchange_oauth_token(monkeypatch):
    import gpsoauth
    seen = {}

    def fake_exchange(email, token, android_id):
        seen.update(email=email, token=token, android_id=android_id)
        return {"Token": TOKEN} if token == "oauth2_4/abc" else {"Error": "BadAuthentication"}

    monkeypatch.setattr(gpsoauth, "exchange_token", fake_exchange)
    assert ks.exchange_oauth_token(EMAIL, ' oauth_token="oauth2_4/abc" ', "0123456789abcdef") == TOKEN
    assert seen["token"] == "oauth2_4/abc"
    with pytest.raises(ks.AuthError, match="BadAuthentication"):
        ks.exchange_oauth_token(EMAIL, "oauth2_4/expired", "0123456789abcdef")
    assert ks.looks_like_master_token(TOKEN) and not ks.looks_like_master_token("oauth2_4/abc")


def test_worker_debounce_and_periodic(engine, server):
    import threading
    import time
    got = []
    done = threading.Event()

    def cb(kind, payload):
        got.append(kind)
        if kind == "synced" and got.count("synced") >= 2:
            done.set()

    w = ks.SyncWorker(engine, cb, interval=60, debounce=0.3)
    w.start()
    time.sleep(0.3)
    w.submit({"op": "create", "kind": "note", "title": "from worker", "text": ""})
    assert done.wait(5)
    w.stop(); w.join(5)
    assert got[0] == "snapshot" and "local" in got and got.count("synced") >= 2
    assert any(r.get("title") == "from worker" for r in server.nodes.values())
