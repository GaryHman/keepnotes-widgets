"""Headless UI smoke test + screenshots (QT_QPA_PLATFORM=offscreen).

Builds the real app with a fake (demo) store of 4 notes, exercises a few
interactions, then renders the widgets onto a desktop-like background.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["KNW_DATA_DIR"] = tempfile.mkdtemp(prefix="knw-")

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap, QRadialGradient  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import keep_sync as ks  # noqa: E402
import keep_widgets as kw  # noqa: E402


def pump(app, secs=0.3):
    end = time.time() + secs
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def main():
    qapp = QApplication(sys.argv[:1])
    qapp.setFont(QFont("Segoe UI", 10))
    directory = ks.data_dir()
    engine = ks.SyncEngine(keep=kw.build_demo_keep(), creds=ks.MemoryCredentialStore(),
                           directory=directory, offline_only=True)
    app = kw.KeepWidgetsApp(qapp, engine, demo=True)
    app.worker.debounce = 0.2
    app.start()
    pump(qapp, 1.0)
    assert len(app.widgets) == 4, f"expected 4 widgets, got {len(app.widgets)}"
    print("widgets:", sorted((w.note.title, w.note.kind) for w in app.widgets.values()))

    by_title = {w.note.title: w for w in app.widgets.values()}
    gl = by_title["Groceries"]
    assert len(gl.rows) == 4 and gl.rows[-1].edit.text() == "Eggs", "checked item should be last"
    assert gl.colour_name() == "Green" and by_title["Gift ideas"].colour_name() == "Pink"
    assert by_title["Call the plumber"].colour_name() == "Yellow"
    assert all(w.windowFlags() & Qt.WindowStaysOnTopHint for w in app.widgets.values()), "on top default"

    # interactions -> engine
    gl.rows[0].check.setChecked(True)  # Milk
    gl.add_edit.setText("Butter")
    gl._add_item()
    plumber = by_title["Call the plumber"]
    plumber.text_edit.setPlainText(plumber.text_edit.toPlainText() + "\nNumber: 020-1234567")
    pump(qapp, 1.5)
    snap = engine.snapshot()
    g = snap[gl.note_id]
    assert {i.text: i.checked for i in g.items} == {"Milk": True, "Eggs": True, "Coffee beans": False,
                                                       "Bread": False, "Butter": False}, g.items
    assert "020-1234567" in snap[plumber.note_id].text
    assert [r.edit.text() for r in gl.rows] == ["Coffee beans", "Bread", "Butter", "Milk", "Eggs"] or \
        [r.edit.text() for r in gl.rows][:3] == ["Coffee beans", "Bread", "Butter"], [r.edit.text() for r in gl.rows]
    print("interactions OK: checkbox, add item, edit text synced into the store")

    # restore demo-looking state for the screenshot (uncheck Milk)
    milk = next(r for r in gl.rows if r.edit.text() == "Milk")
    milk.check.setChecked(False)
    butter = next(r for r in gl.rows if r.edit.text() == "Butter")
    butter.delete_btn.click()
    pump(qapp, 1.0)

    # colour change keeps a local override
    by_title["Gift ideas"].set_colour("Purple")
    pump(qapp, 0.5)
    assert app.layout_store.note(by_title["Gift ideas"].note_id)["color"] == "Purple"
    by_title["Gift ideas"].set_colour("Pink")

    # new note via '+'
    plumber.add_btn.click()
    pump(qapp, 1.0)
    assert len(app.widgets) == 5, len(app.widgets)
    newest = [w for w in app.widgets.values() if not w.note.title][0]
    newest.hide_note()
    print("new note via '+' OK, hide OK")

    # layout persisted
    app.layout_store.save()
    data = ks.read_json(directory / "layout.json", {})
    assert len(data["notes"]) >= 5 and all("geom" in v for k, v in data["notes"].items() if k != newest.note_id)
    print("layout.json saved with", len(data["notes"]), "entries")

    # paused indicator
    engine.status, engine.last_error = "paused", "offline (no connection to Google)"
    app.demo = False
    app.update_status()
    print("tray status (paused):", app.status_action.text())
    assert "paused" in app.status_action.text().lower()
    app.demo = True
    engine.status = "ok"
    app.update_status()

    # ---- screenshot of widgets on a desktop-like background ----
    W, H = 1280, 760
    canvas = QPixmap(W, H)
    p = QPainter(canvas)
    grad = QLinearGradient(0, 0, W, H)
    grad.setColorAt(0, QColor("#0B3A6E"))
    grad.setColorAt(0.55, QColor("#1D6FB8"))
    grad.setColorAt(1, QColor("#7FB5E6"))
    p.fillRect(canvas.rect(), grad)
    bloom = QRadialGradient(W * 0.62, H * 0.55, 420)
    bloom.setColorAt(0, QColor(255, 255, 255, 110))
    bloom.setColorAt(1, QColor(255, 255, 255, 0))
    p.fillRect(canvas.rect(), bloom)
    p.fillRect(QRect(0, H - 48, W, 48), QColor(20, 24, 32, 230))  # taskbar
    p.setPen(QColor("white"))
    p.drawText(QRect(W - 140, H - 48, 130, 48), Qt.AlignCenter, "16:55\n20-10-2026")
    tray = kw.app_icon().pixmap(22, 22)
    p.drawPixmap(W - 175, H - 35, tray)

    order = ["Call the plumber", "Groceries", "Gift ideas", "Meeting notes – Q4 planning"]
    sizes = {"Call the plumber": (250, 200), "Groceries": (240, 250), "Gift ideas": (220, 190),
             "Meeting notes – Q4 planning": (300, 330)}
    pos = {"Call the plumber": (60, 50), "Groceries": (340, 70), "Gift ideas": (610, 40),
           "Meeting notes – Q4 planning": (880, 90)}
    for t in order:
        w = by_title[t]
        w.resize(*sizes[t])
        w.show()
        pump(qapp, 0.2)
        shot = w.grab()
        x, y = pos[t]
        p.fillRect(QRect(x + 4, y + 6, shot.width(), shot.height()), QColor(0, 0, 0, 55))
        p.drawPixmap(x, y, shot)
    p.end()
    out = ROOT / "screenshot-widgets.png"
    canvas.save(str(out))
    print("saved", out, canvas.width(), "x", canvas.height())

    # ---- setup dialog ----
    dlg = kw.SetupDialog(app)
    dlg.email.setText("you@gmail.com")
    dlg.show()
    pump(qapp, 0.4)
    shot = dlg.grab()
    out2 = ROOT / "screenshot-setup.png"
    shot.save(str(out2))
    print("saved", out2, shot.width(), "x", shot.height())

    # setup validation (no network): wrong master token format
    dlg.rb_master.setChecked(True)
    dlg.token.setText("not-a-token")
    dlg.sign_in()
    assert "does not look like a master token" in dlg.status.text()
    print("setup validation OK")

    # ---- templates: default unchanged, live switch, persisted, per-note colour kept ----
    assert app.template_name() == "Classic Sticky", "default template must be the classic look"
    gift = by_title["Gift ideas"]
    for name in kw.TEMPLATES:
        app.layout_store.settings["template"] = name
        app.apply_template()
        pump(qapp, 0.1)
        hdr, body, _fg = gift.palette_entry()
        assert gift.colour_name() == "Pink" and hdr == kw.TEMPLATES[name]["palette"]["Pink"][0], name
        assert body.split("(")[0] in gift.styleSheet() or body in gift.styleSheet(), name
    app.apply_template("Dark Glass")  # preview only
    assert app.template_name() == "Dark Glass" and app.layout_store.settings["template"] == "Compact"
    app.apply_template(None)
    assert app.template_name() == "Compact"
    app.layout_store.save()
    assert ks.read_json(directory / "layout.json", {})["settings"]["template"] == "Compact"
    print("templates OK:", ", ".join(kw.TEMPLATES), "(live switch, preview/undo, persisted, note colour kept)")
    render_templates(qapp, app, by_title)
    app.layout_store.settings["template"] = "Classic Sticky"
    app.apply_template()

    # ---- global hotkeys ----
    assert app.layout_store.settings["hotkeys"] == {"toggle": "Ctrl+Alt+K", "show": "", "hide": ""}
    errs = app.apply_hotkeys(notify=False)
    assert errs == [] and app.hotkeys.active == {"toggle": "Ctrl+Alt+K"}, (errs, app.hotkeys.active)
    for w in app.widgets.values():
        if w.lay.get("visible", True):
            w.show()
    pump(qapp, 0.2)
    app.on_hotkey("toggle")
    assert not any(w.isVisible() for w in app.widgets.values()), "toggle should hide all"
    app.on_hotkey("toggle")
    assert all(w.isVisible() for w in app.widgets.values()), "toggle should show all"
    app.on_hotkey("hide")
    assert not any(w.isVisible() for w in app.widgets.values())
    app.on_hotkey("show")
    assert all(w.isVisible() for w in app.widgets.values())
    app.layout_store.settings["hotkeys"] = {"toggle": "Ctrl+Alt+K", "show": "Ctrl+Alt+K", "hide": "Ctrl+H"}
    errs = app.apply_hotkeys(notify=False)
    print("hotkey conflicts detected:", errs)
    assert [e[0] for e in errs] == ["show", "hide"]
    app.layout_store.settings["hotkeys"] = {"toggle": "Ctrl+Alt+K", "show": "", "hide": ""}
    app.apply_hotkeys(notify=False)
    print("hotkeys OK (toggle/show/hide, duplicate + single-modifier rejected)")

    # ---- settings dialog with template + hotkey fields ----
    sd = kw.SettingsDialog(app)
    sd.show()
    pump(qapp, 0.4)
    sd.template_combo.setCurrentText("Pastel Rounded")  # live preview
    pump(qapp, 0.1)
    assert app.template_name() == "Pastel Rounded"
    sd.template_combo.setCurrentText("Classic Sticky")
    shot = sd.grab()
    out3 = ROOT / "screenshot-settings.png"
    shot.save(str(out3))
    print("saved", out3, shot.width(), "x", shot.height())
    sd.hotkey_group.edits["hide"].setKeySequence("Ctrl+Alt+Shift+K")
    warned = []
    QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a))
    sd._save()
    assert not warned and app.layout_store.settings["hotkeys"]["hide"] == "Ctrl+Alt+Shift+K"
    sd2 = kw.SettingsDialog(app)
    sd2.hotkey_group.edits["show"].setKeySequence("Ctrl+Alt+K")
    sd2._save()
    assert warned and sd2.result() == 0, "conflict must show a message and keep the dialog open"
    print("settings dialog OK (template preview, hotkey save, conflict message):", warned[0][2].splitlines()[2])
    sd2.hotkey_group.edits["show"].clear()
    sd2.hotkey_group.edits["hide"].clear()
    sd2._save()
    for d in (sd, sd2, dlg):
        d.close()
        d.deleteLater()
    pump(qapp, 0.2)

    app.quit()
    print("UI SMOKE TEST PASSED")


def render_templates(qapp, app, by_title):
    order = ["Call the plumber", "Groceries", "Gift ideas", "Meeting notes – Q4 planning"]
    sizes = {"Call the plumber": (240, 190), "Groceries": (230, 215), "Gift ideas": (220, 180),
             "Meeting notes – Q4 planning": (290, 215)}
    row_h, W = 270, 1280
    names = list(kw.TEMPLATES)
    canvas = QPixmap(W, row_h * len(names))
    p = QPainter(canvas)
    grad = QLinearGradient(0, 0, W, canvas.height())
    grad.setColorAt(0, QColor("#0B3A6E"))
    grad.setColorAt(0.55, QColor("#1D6FB8"))
    grad.setColorAt(1, QColor("#7FB5E6"))
    p.fillRect(canvas.rect(), grad)
    f = QFont("Segoe UI", 13)
    f.setBold(True)
    for r, name in enumerate(names):
        app.layout_store.settings["template"] = name
        app.apply_template()
        y0 = r * row_h
        if r:
            p.fillRect(QRect(0, y0, W, 2), QColor(255, 255, 255, 90))
        p.setFont(f)
        p.setPen(QColor("white"))
        label = name + ("  (default)" if name == kw.DEFAULT_TEMPLATE else "")
        p.drawText(QRect(24, y0 + 6, 600, 30), Qt.AlignLeft | Qt.AlignVCenter, label)
        x = 24
        for t in order:
            w = by_title[t]
            w.resize(*sizes[t])
            w.show()
            pump(qapp, 0.15)
            shot = w.grab()
            if kw.TEMPLATES[name]["radius"] == 0:
                p.fillRect(QRect(x + 4, y0 + 46, shot.width(), shot.height()), QColor(0, 0, 0, 55))
            p.drawPixmap(x, y0 + 40, shot)
            x += sizes[t][0] + 36
    p.end()
    out = ROOT / "templates.png"
    canvas.save(str(out))
    print("saved", out, canvas.width(), "x", canvas.height())


if __name__ == "__main__":
    main()
