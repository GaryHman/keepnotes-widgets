"""Sync layer for KeepNotes Widgets.

This module has no Qt dependency so it can be unit-tested on its own.
All access to the gkeepapi ``Keep`` object goes through :class:`SyncEngine`,
which is driven by a single background :class:`SyncWorker` thread.

Uses the UNOFFICIAL Google Keep API via gkeepapi. Google can change or block
it at any time; when that happens sync pauses and the cached notes stay usable.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

APP_NAME = "KeepNotesWidgets"
KEYRING_SERVICE = "KeepNotesWidgets"
KEYRING_EMAIL_KEY = "account-email"

log = logging.getLogger("keepnotes")


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------
def data_dir() -> Path:
    """%APPDATA%\\KeepNotesWidgets on Windows (override with KNW_DATA_DIR)."""
    override = os.environ.get("KNW_DATA_DIR")
    if override:
        p = Path(override)
    elif os.name == "nt":
        p = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    else:
        p = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s (%s); using defaults", path.name, exc)
        return default


def atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, default=str)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Credentials (Windows Credential Manager via keyring). Never written to files.
# --------------------------------------------------------------------------
class CredentialStore:
    def get(self) -> tuple[str, str] | None:
        import keyring

        email = keyring.get_password(KEYRING_SERVICE, KEYRING_EMAIL_KEY)
        if not email:
            return None
        token = keyring.get_password(KEYRING_SERVICE, email)
        return (email, token) if token else None

    def set(self, email: str, master_token: str) -> None:
        import keyring

        keyring.set_password(KEYRING_SERVICE, KEYRING_EMAIL_KEY, email)
        keyring.set_password(KEYRING_SERVICE, email, master_token)

    def clear(self) -> None:
        import keyring

        email = keyring.get_password(KEYRING_SERVICE, KEYRING_EMAIL_KEY)
        for key in filter(None, [email, KEYRING_EMAIL_KEY]):
            try:
                keyring.delete_password(KEYRING_SERVICE, key)
            except Exception:  # noqa: BLE001 - already gone
                pass


class MemoryCredentialStore(CredentialStore):
    """In-memory store for tests and demo mode."""

    def __init__(self, email: str | None = None, token: str | None = None):
        self._v = (email, token) if email and token else None

    def get(self):
        return self._v

    def set(self, email, master_token):
        self._v = (email, master_token)

    def clear(self):
        self._v = None


class AuthError(Exception):
    pass


def clean_oauth_cookie(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("oauth_token="):
        value = value.split("=", 1)[1]
    return value.strip().strip(";").strip().strip('"').strip("'").strip()


def exchange_oauth_token(email: str, oauth_token: str, android_id: str) -> str:
    """Exchange the EmbeddedSetup ``oauth_token`` cookie for a master token."""
    import gpsoauth

    token = clean_oauth_cookie(oauth_token)
    if not token:
        raise AuthError("The oauth_token value is empty.")
    try:
        res = gpsoauth.exchange_token(email.strip(), token, android_id)
    except Exception as exc:  # network errors etc.
        raise AuthError(f"Could not reach Google: {type(exc).__name__}") from exc
    master = res.get("Token") if isinstance(res, dict) else None
    if not master:
        err = res.get("Error", "unknown error") if isinstance(res, dict) else "unknown error"
        raise AuthError(
            f"Google refused the token exchange ({err}). The oauth_token cookie is "
            "single-use and expires after a few minutes: sign in on the "
            "EmbeddedSetup page again and paste the fresh value right away."
        )
    return master


def looks_like_master_token(value: str) -> bool:
    return value.strip().startswith("aas_et/") and len(value.strip()) > 40


# --------------------------------------------------------------------------
# Plain data handed to the UI (never gkeepapi objects)
# --------------------------------------------------------------------------
@dataclass
class ItemData:
    id: str
    text: str
    checked: bool


@dataclass
class NoteData:
    id: str
    kind: str  # "note" | "list"
    title: str
    text: str
    color: str  # gkeepapi ColorValue value, e.g. "DEFAULT", "YELLOW"
    pinned: bool
    labels: list[str] = field(default_factory=list)
    items: list[ItemData] = field(default_factory=list)

    def digest(self) -> str:
        return hashlib.sha1(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def node_to_data(node) -> NoteData:
    from gkeepapi import node as gn

    is_list = isinstance(node, gn.List)
    try:
        labels = sorted(lbl.name for lbl in node.labels.all())
    except Exception:  # noqa: BLE001
        labels = []
    color = getattr(node.color, "value", str(node.color))
    return NoteData(
        id=node.id,
        kind="list" if is_list else "note",
        title=node.title or "",
        text="" if is_list else (node.text or ""),
        color=color,
        pinned=bool(node.pinned),
        labels=labels,
        items=[ItemData(i.id, i.text or "", bool(i.checked)) for i in node.items] if is_list else [],
    )


def is_visible(node) -> bool:
    return not (node.trashed or node.archived or node.deleted)


@dataclass
class SyncResult:
    ok: bool = True
    error: str | None = None
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def _friendly_error(exc: BaseException) -> str:
    name = type(exc).__name__
    if name in ("ConnectionError", "Timeout", "ConnectTimeout", "ReadTimeout", "gaierror"):
        return "offline (no connection to Google)"
    if name in ("LoginException", "BrowserLoginRequiredException"):
        return "Google rejected the login - sign in again from Settings"
    msg = str(exc).splitlines()[0][:160] if str(exc) else ""
    return f"{name}: {msg}" if msg else name


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
class SyncEngine:
    """Owns the gkeepapi Keep object, the offline cache and pending local edits."""

    def __init__(
        self,
        keep=None,
        creds: CredentialStore | None = None,
        directory: Path | str | None = None,
        keep_factory: Callable[[], Any] | None = None,
        offline_only: bool = False,
    ):
        if keep_factory is None:
            import gkeepapi

            keep_factory = gkeepapi.Keep
        self._factory = keep_factory
        self.keep = keep if keep is not None else keep_factory()
        self.creds = creds if creds is not None else CredentialStore()
        self.dir = Path(directory) if directory else data_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.dir / "state.json"
        self.config_path = self.dir / "config.json"
        self.config = read_json(self.config_path, {})
        if "android_id" not in self.config:
            self.config["android_id"] = secrets.token_hex(8)  # random device id, not secret
            atomic_write_json(self.config_path, self.config)
        self.offline_only = offline_only
        self.status = "signed_out"  # signed_out | ok | paused
        self.last_error: str | None = None
        self.last_sync: _dt.datetime | None = None
        self.email: str | None = None
        self.authenticated = False
        self._ops: list[dict] = []
        self._ops_lock = threading.Lock()
        self._lock = threading.RLock()

    # -- identity ---------------------------------------------------------
    @property
    def android_id(self) -> str:
        return self.config["android_id"]

    # -- cache ------------------------------------------------------------
    def load_cache(self) -> bool:
        data = read_json(self.cache_path, None)
        if not data or "keep" not in data:
            return False
        try:
            with self._lock:
                self.keep.restore(data["keep"])
            log.info("Loaded %d cached notes", len(self.snapshot()))
            return True
        except Exception:  # noqa: BLE001
            log.exception("Offline cache is unreadable; ignoring it")
            return False

    def save_cache(self) -> None:
        try:
            with self._lock:
                state = {"keep": self.keep.dump(), "email": self.email,
                         "saved_at": _dt.datetime.now().isoformat(timespec="seconds")}
            atomic_write_json(self.cache_path, state)
        except Exception:  # noqa: BLE001
            log.exception("Could not write offline cache")

    # -- auth -------------------------------------------------------------
    def sign_in(self, email: str, master_token: str, save: bool = True) -> None:
        """Authenticate without syncing (the worker syncs). Raises on failure."""
        email = email.strip()
        master_token = master_token.strip()
        with self._lock:
            cached_email = self.config.get("email")
            if cached_email and cached_email.lower() != email.lower():
                log.info("Switching Google account; dropping cached notes of the old account")
                self.keep = self._factory()
                try:
                    self.cache_path.unlink()
                except FileNotFoundError:
                    pass
            self.keep.authenticate(email, master_token, state=None, sync=False,
                                   device_id=self.android_id)
        self.email = email
        self.authenticated = True
        self.config["email"] = email
        atomic_write_json(self.config_path, self.config)
        if save:
            self.creds.set(email, master_token)
        log.info("Signed in as %s", email)

    def sign_out(self) -> None:
        self.creds.clear()
        self.authenticated = False
        self.status = "signed_out"
        log.info("Signed out (credentials removed from Credential Manager)")

    def start(self) -> None:
        """Load the offline cache, then try to authenticate with stored credentials."""
        self.load_cache()
        if self.offline_only:
            self.status = "ok"
            return
        creds = self.creds.get()
        if not creds:
            self.status = "signed_out"
            return
        self.email = creds[0]
        try:
            self.sign_in(creds[0], creds[1], save=False)
        except Exception as exc:  # noqa: BLE001
            self._pause(exc)

    def _pause(self, exc: BaseException) -> None:
        self.status = "paused"
        self.last_error = _friendly_error(exc)
        log.warning("Sync paused: %s", self.last_error)

    # -- reading ----------------------------------------------------------
    def snapshot(self) -> dict[str, NoteData]:
        with self._lock:
            return {n.id: node_to_data(n) for n in self.keep.all() if is_visible(n)}

    def label_names(self) -> list[str]:
        with self._lock:
            try:
                return sorted(lbl.name for lbl in self.keep.labels())
            except Exception:  # noqa: BLE001
                return []

    def has_local_changes(self) -> bool:
        with self._lock:
            return any(n.dirty for n in self.keep.all())

    # -- local edits ------------------------------------------------------
    def submit(self, op: dict) -> None:
        with self._ops_lock:
            # Coalesce repeated text edits of the same field.
            key = (op.get("op"), op.get("id"), op.get("item_id"))
            if op.get("op") in ("set_title", "set_text", "set_item_text") and self._ops:
                last = self._ops[-1]
                if (last.get("op"), last.get("id"), last.get("item_id")) == key:
                    self._ops[-1] = op
                    return
            self._ops.append(op)

    def pending_ops(self) -> int:
        with self._ops_lock:
            return len(self._ops)

    def apply_pending(self) -> list[dict]:
        with self._ops_lock:
            ops, self._ops = self._ops, []
        events: list[dict] = []
        if not ops:
            return events
        with self._lock:
            for op in ops:
                try:
                    ev = self._apply(op)
                    if ev:
                        events.append(ev)
                except Exception:  # noqa: BLE001
                    log.exception("Could not apply local edit %s", op.get("op"))
        self.save_cache()
        return events

    def _find_item(self, node, item_id):
        for child in node.children:
            if child.id == item_id:
                return child
        return None

    def _apply(self, op: dict) -> dict | None:
        from gkeepapi import node as gn

        kind = op["op"]
        if kind == "create":
            if op.get("kind") == "list":
                node = self.keep.createList(op.get("title", ""), [(t, False) for t in op.get("items", [])])
            else:
                node = self.keep.createNote(op.get("title", ""), op.get("text", ""))
            log.info("Created %s %s", op.get("kind", "note"), node.id)
            return {"event": "created", "id": node.id, "tag": op.get("tag")}

        node = self.keep.get(op["id"])
        if node is None:
            log.warning("Edit for unknown note %s ignored", op["id"])
            return None
        if kind == "set_title":
            if node.title != op["value"]:
                node.title = op["value"]
        elif kind == "set_text":
            if isinstance(node, gn.List):
                return None
            if node.text != op["value"]:
                node.text = op["value"]
        elif kind == "set_pinned":
            node.pinned = bool(op["value"])
        elif kind == "set_color":
            try:
                node.color = gn.ColorValue(op["value"])
            except Exception as exc:  # noqa: BLE001 - known gkeepapi colour bug
                log.warning("Colour push to Keep failed (%s); colour kept locally only", type(exc).__name__)
                return {"event": "color_local", "id": node.id}
        elif kind == "trash":
            node.trash()
            log.info("Moved note %s to trash", node.id)
            return {"event": "removed", "id": node.id}
        elif kind == "add_item":
            if not isinstance(node, gn.List):
                return None
            node.add(op.get("text", ""), False, gn.NewListItemPlacementValue.Bottom)
            return {"event": "structure", "id": node.id}
        elif kind in ("set_item_checked", "set_item_text", "delete_item"):
            item = self._find_item(node, op["item_id"])
            if item is None:
                log.warning("Edit for unknown list item ignored")
                return None
            if kind == "set_item_checked":
                item.checked = bool(op["value"])
            elif kind == "set_item_text":
                if item.text != op["value"]:
                    item.text = op["value"]
            else:
                item.delete()
                node.touch(True)
                return {"event": "structure", "id": node.id}
        else:
            log.warning("Unknown op %s", kind)
            return None
        return {"event": "edited", "id": node.id}

    # -- sync -------------------------------------------------------------
    def sync(self) -> SyncResult:
        res = SyncResult()
        if self.offline_only:
            self.status, self.last_sync = "ok", _dt.datetime.now()
            return res
        if not self.authenticated:
            creds = self.creds.get()
            if not creds:
                self.status = "signed_out"
                return SyncResult(ok=False, error="not signed in")
            try:
                self.sign_in(creds[0], creds[1], save=False)
            except Exception as exc:  # noqa: BLE001
                self._pause(exc)
                return SyncResult(ok=False, error=self.last_error)
        with self._lock:
            before = {nid: n.digest() for nid, n in self.snapshot().items()}
            pushed = {n.id: before.get(n.id) for n in self.keep.all() if n.dirty}
            try:
                self.keep.sync()
            except Exception as exc:  # noqa: BLE001
                if type(exc).__name__ == "ResyncRequiredException":
                    log.warning("Google asked for a full resync; unsynced local edits may be lost")
                    try:
                        self.keep.sync(resync=True)
                    except Exception as exc2:  # noqa: BLE001
                        self._pause(exc2)
                        return SyncResult(ok=False, error=self.last_error)
                else:
                    self._pause(exc)
                    return SyncResult(ok=False, error=self.last_error)
            after = {nid: n.digest() for nid, n in self.snapshot().items()}
        res.changed = [i for i, d in after.items() if before.get(i) != d]
        res.removed = [i for i in before if i not in after]
        for nid, dig in pushed.items():
            if dig is not None and nid in after and after[nid] != dig:
                res.conflicts.append(nid)
                log.warning("Conflict on note %s: edited here and in Keep at the same time; "
                            "last write wins, the version from Google is kept", nid)
        self.status, self.last_error = "ok", None
        self.last_sync = _dt.datetime.now()
        self.save_cache()
        if res.changed or res.removed:
            log.info("Sync: %d changed, %d removed", len(res.changed), len(res.removed))
        return res


# --------------------------------------------------------------------------
# Background worker
# --------------------------------------------------------------------------
class SyncWorker(threading.Thread):
    """Applies local edits immediately and syncs every ``interval`` seconds
    and ``debounce`` seconds after the last edit.

    ``callback(kind, payload)`` is called from this thread with kind in
    {"snapshot", "local", "synced"}.
    """

    def __init__(self, engine: SyncEngine, callback, interval: float = 60, debounce: float = 2.0):
        super().__init__(name="KeepSync", daemon=True)
        self.engine = engine
        self.callback = callback
        self.interval = max(15.0, float(interval))
        self.debounce = debounce
        self._wake = threading.Event()
        self._stop_evt = threading.Event()
        self._next_periodic = 0.0
        self._debounce_at: float | None = None
        self._now = False

    def payload(self, **extra) -> dict:
        e = self.engine
        d = {"snapshot": e.snapshot(), "labels": e.label_names(), "status": e.status,
             "error": e.last_error, "last_sync": e.last_sync, "email": e.email}
        d.update(extra)
        return d

    def submit(self, op: dict) -> None:
        self.engine.submit(op)
        self._debounce_at = time.monotonic() + self.debounce
        self._wake.set()

    def sync_now(self) -> None:
        self._now = True
        self._wake.set()

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()

    def run(self) -> None:
        try:
            self.engine.start()
        except Exception:  # noqa: BLE001
            log.exception("Startup failed")
        self.callback("snapshot", self.payload())
        while not self._stop_evt.is_set():
            events = self.engine.apply_pending()
            if events:
                self.callback("local", self.payload(events=events))
            now = time.monotonic()
            due = [self._next_periodic]
            if self._debounce_at is not None:
                due.append(self._debounce_at)
            if self._now:
                due.append(now)
            if now >= min(due):
                self._now = False
                self._debounce_at = None
                try:
                    result = self.engine.sync()
                except Exception as exc:  # noqa: BLE001
                    log.exception("Unexpected sync failure")
                    self.engine._pause(exc)
                    result = SyncResult(ok=False, error=self.engine.last_error)
                self._next_periodic = time.monotonic() + self.interval
                self.callback("synced", self.payload(result=result))
                continue
            self._wake.wait(timeout=max(0.05, min(due) - now))
            self._wake.clear()
        # final flush
        self.engine.apply_pending()
        self.engine.save_cache()
