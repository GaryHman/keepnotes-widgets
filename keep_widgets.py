#!/usr/bin/env python3
"""KeepNotes Widgets - your Google Keep notes as Sticky-Notes-style desktop widgets.

Run with pythonw (no console):  pythonw keep_widgets.py
Options:  --demo   show 4 sample notes without signing in (nothing is synced)

Uses the UNOFFICIAL Google Keep API (gkeepapi). It can break if Google changes things.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # the app folder is shared/synced between PCs: keep no __pycache__ there

if sys.version_info < (3, 11):
    sys.stderr.write("KeepNotes Widgets needs Python 3.11 or newer.\n")
    sys.exit(1)

import argparse
import copy
import datetime as _dt
import logging
import os
import subprocess
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

_APP_NAME = "KeepNotes Widgets"
APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

def _startup_failure(exc: BaseException) -> None:
    """A required package is missing/broken (e.g. an incomplete .venv). pythonw has no console,
    so write the reason to the log file and show a Windows message box instead of exiting silently."""
    import ctypes
    import datetime
    from pathlib import Path as _P
    msg = (f"{_APP_NAME} could not start because a required Python package is missing or broken:\n\n"
           f"{exc}\n\nPython used: {sys.executable}\n\nRun install.bat in\n{_P(__file__).resolve().parent}\n"
           "or install the packages with:  py -m pip install -r requirements.txt")
    try:
        d = _P(os.environ.get("KNW_DATA_DIR") or _P(os.environ.get("APPDATA", str(_P.home()))) / "KeepNotesWidgets")
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "keepnotes.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} ERROR Startup failed: {exc!r} "
                    f"(python: {sys.executable})\n")
    except OSError:
        pass
    if sys.stderr is not None:
        sys.stderr.write(msg + "\n")
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, msg, _APP_NAME, 0x10)
        except Exception:  # noqa: BLE001
            pass
    sys.exit(1)


try:
    from PySide6.QtCore import QLockFile, QObject, QPoint, QSize, Qt, QTimer, QUrl, Signal
    from PySide6.QtGui import (QAction, QActionGroup, QColor, QDesktopServices, QFont, QGuiApplication,
                               QIcon, QPainter, QPixmap)
    from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
                                   QDialogButtonBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
                                   QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                                   QRadioButton, QScrollArea, QSizeGrip, QSpinBox, QSystemTrayIcon,
                                   QToolButton, QVBoxLayout, QWidget)

    import keep_sync as ks  # noqa: E402
    import win_hotkeys as hk  # noqa: E402
except ImportError as _exc:  # pragma: no cover - exercised on broken installs
    _startup_failure(_exc)


APP_TITLE = "KeepNotes Widgets"
log = logging.getLogger("keepnotes")

# Sticky Notes palette: name -> (header, body, text, Keep colour to push)
PALETTE = {
    "Yellow": ("#FFF2AB", "#FFF7D1", "#1F1F1F", "YELLOW"),
    "Green": ("#CBF1C4", "#E4F9E0", "#1F1F1F", "GREEN"),
    "Pink": ("#FFCCE5", "#FFE4F1", "#1F1F1F", "PINK"),
    "Purple": ("#E7CFFF", "#F2E6FF", "#1F1F1F", "PURPLE"),
    "Blue": ("#CDE9FF", "#E2F1FF", "#1F1F1F", "BLUE"),
    "Gray": ("#E0E0E0", "#F3F2F1", "#1F1F1F", "GRAY"),
    "Charcoal": ("#454545", "#616161", "#F5F5F5", "BROWN"),
}
KEEP_TO_STICKY = {
    "DEFAULT": "Yellow", "YELLOW": "Yellow", "ORANGE": "Yellow", "RED": "Pink", "PINK": "Pink",
    "GREEN": "Green", "TEAL": "Green", "BLUE": "Blue", "CERULEAN": "Blue", "PURPLE": "Purple",
    "GRAY": "Gray", "BROWN": "Charcoal",
}
DEFAULT_W, DEFAULT_H = 260, 240

# Visual templates (Settings > Template). Each palette maps the same 7 colour names, so a note's
# own colour (from Keep, or the per-note override) keeps working in every template.
# palette value: (header or accent colour, body colour, text colour)
_CLASSIC = {k: v[:3] for k, v in PALETTE.items()}
TEMPLATES = {
    "Classic Sticky": {
        "palette": _CLASSIC, "radius": 0, "border": "1px solid rgba(0,0,0,0.18)", "inset": 0,
        "header": "bar", "header_h": 30, "margins": (12, 6, 10, 0), "title_font": "",
        "title_pt": 11, "title_weight": 600, "body_pt": 10.5, "btn_px": 15, "spacing": 4,
        "min": (180, 130), "default": (260, 240),
    },
    "Minimal Paper": {
        "palette": {"Yellow": ("#F2B705", "#FFFFFF", "#202124"), "Green": ("#34A853", "#FFFFFF", "#202124"),
                    "Pink": ("#E8457C", "#FFFFFF", "#202124"), "Purple": ("#9B59B6", "#FFFFFF", "#202124"),
                    "Blue": ("#1A73E8", "#FFFFFF", "#202124"), "Gray": ("#9AA0A6", "#FFFFFF", "#202124"),
                    "Charcoal": ("#9AA0A6", "#2B2B2B", "#F1F1F1")},
        "radius": 3, "border": "1px solid #D5D7DB", "inset": 1, "header": "accent", "header_h": 28,
        "margins": (14, 4, 12, 0), "title_font": "Georgia", "title_pt": 13, "title_weight": 600,
        "body_pt": 10.5, "btn_px": 14, "spacing": 4, "min": (180, 130), "default": (260, 240),
    },
    "Dark Glass": {
        "palette": {"Yellow": ("rgba(251,188,4,0.50)", "rgba(38,35,24,0.74)", "#F3F3F3"),
                    "Green": ("rgba(52,168,83,0.50)", "rgba(22,38,28,0.74)", "#F3F3F3"),
                    "Pink": ("rgba(233,69,124,0.50)", "rgba(42,24,32,0.74)", "#F3F3F3"),
                    "Purple": ("rgba(155,89,182,0.50)", "rgba(34,26,44,0.74)", "#F3F3F3"),
                    "Blue": ("rgba(26,115,232,0.50)", "rgba(20,30,46,0.74)", "#F3F3F3"),
                    "Gray": ("rgba(154,160,166,0.40)", "rgba(32,33,36,0.74)", "#F3F3F3"),
                    "Charcoal": ("rgba(90,90,90,0.55)", "rgba(16,16,18,0.82)", "#F3F3F3")},
        "radius": 12, "border": "1px solid rgba(255,255,255,0.16)", "inset": 1, "header": "bar",
        "header_h": 30, "margins": (12, 6, 10, 0), "title_font": "", "title_pt": 11,
        "title_weight": 600, "body_pt": 10.5, "btn_px": 15, "spacing": 4, "min": (180, 130),
        "default": (260, 240),
    },
    "Pastel Rounded": {
        "palette": {"Yellow": ("#FFEDB0", "#FFF8DC", "#2D2A32"), "Green": ("#CDEFD6", "#EAF8EE", "#2D2A32"),
                    "Pink": ("#FFD3E1", "#FFEEF4", "#2D2A32"), "Purple": ("#E3D5FF", "#F4EEFF", "#2D2A32"),
                    "Blue": ("#CFE6FF", "#ECF5FF", "#2D2A32"), "Gray": ("#E5E5EA", "#F6F6F8", "#2D2A32"),
                    "Charcoal": ("#4A4A57", "#5C5C6B", "#F7F7FA")},
        "radius": 18, "border": "1px solid rgba(0,0,0,0.07)", "inset": 1, "header": "bar",
        "header_h": 32, "margins": (16, 8, 14, 2), "title_font": "", "title_pt": 11.5,
        "title_weight": 700, "body_pt": 10.5, "btn_px": 15, "spacing": 5, "min": (190, 140),
        "default": (260, 240),
    },
    "Compact": {
        "palette": _CLASSIC, "radius": 4, "border": "1px solid rgba(0,0,0,0.18)", "inset": 0,
        "header": "bar", "header_h": 22, "margins": (8, 2, 6, 0), "title_font": "", "title_pt": 9.5,
        "title_weight": 600, "body_pt": 9, "btn_px": 12, "spacing": 1, "min": (150, 100),
        "default": (220, 180),
    },
}
DEFAULT_TEMPLATE = "Classic Sticky"


def swatch_colour(css: str) -> QColor:
    """QColor from '#hex' or 'rgba(r,g,b,a)' (alpha ignored, for menu swatches)."""
    if css.startswith("rgba("):
        r, g, b = (int(x) for x in css[5:-1].split(",")[:3])
        return QColor(r, g, b)
    return QColor(css)


def is_light_text(colour: str) -> bool:
    c = QColor(colour)
    return c.lightness() > 160

EMBEDDED_SETUP_URL = "https://accounts.google.com/EmbeddedSetup"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def setup_logging(directory: Path) -> None:
    handler = RotatingFileHandler(directory / "keepnotes.log", maxBytes=512_000, backupCount=2,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    if sys.stderr is not None and not getattr(sys, "frozen", False):
        log.addHandler(logging.StreamHandler())


def app_icon(paused: bool = False) -> QIcon:
    ico = APP_DIR / "icon.ico"
    base = QPixmap(str(ico)) if ico.exists() else QPixmap()
    if base.isNull():
        base = draw_icon_pixmap(64)
    if not paused:
        return QIcon(base)
    pm = base.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QColor("#FFFFFF"))
    p.setBrush(QColor("#E8710A"))
    p.drawEllipse(34, 34, 28, 28)
    p.setPen(QColor("#FFFFFF"))
    p.setBrush(QColor("#FFFFFF"))
    p.drawRect(42, 41, 4, 14)
    p.drawRect(50, 41, 4, 14)
    p.end()
    return QIcon(pm)


def draw_icon_pixmap(size: int) -> QPixmap:
    """Yellow sticky note with a folded corner and three text lines."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 64.0
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, 50))
    p.drawRoundedRect(int(7 * s), int(8 * s), int(52 * s), int(52 * s), 4 * s, 4 * s)
    p.setBrush(QColor("#FFF7D1"))
    p.drawRoundedRect(int(5 * s), int(5 * s), int(52 * s), int(52 * s), 4 * s, 4 * s)
    p.setBrush(QColor("#FBBC04"))
    p.drawRoundedRect(int(5 * s), int(5 * s), int(52 * s), int(12 * s), 4 * s, 4 * s)
    p.drawRect(int(5 * s), int(11 * s), int(52 * s), int(6 * s))
    p.setBrush(QColor("#5F6368"))
    for i, w in enumerate((38, 32, 24)):
        p.drawRoundedRect(int(12 * s), int((25 + i * 9) * s), int(w * s), int(4 * s), 2 * s, 2 * s)
    p.end()
    return pm


def startup_shortcut_path() -> Path | None:
    if os.name != "nt":
        return None
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" /
            "Programs" / "Startup" / "KeepNotes Widgets.lnk")


def pythonw_executable() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def create_shortcut(lnk: Path, target: str, args: str, workdir: str, icon: str) -> None:
    env = dict(os.environ, KNW_LNK=str(lnk), KNW_TARGET=target, KNW_ARGS=args, KNW_WD=workdir,
               KNW_ICON=icon)
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:KNW_LNK);"
          "$s.TargetPath=$env:KNW_TARGET;$s.Arguments=$env:KNW_ARGS;"
          "$s.WorkingDirectory=$env:KNW_WD;$s.IconLocation=$env:KNW_ICON;"
          "$s.Description='KeepNotes Widgets';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env,
                   check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30)


# --------------------------------------------------------------------------
# Layout / settings persistence (%APPDATA%\KeepNotesWidgets\layout.json)
# --------------------------------------------------------------------------
class LayoutStore:
    DEFAULT_SETTINGS = {"filter": "all", "label": "", "interval": 60, "template": DEFAULT_TEMPLATE,
                        "hotkeys": {"toggle": "Ctrl+Alt+K", "show": "", "hide": ""}}

    def __init__(self, path: Path):
        self.path = path
        self.data = ks.read_json(path, {})
        self.data.setdefault("notes", {})
        self.data.setdefault("settings", {})
        for k, v in self.DEFAULT_SETTINGS.items():
            self.data["settings"].setdefault(k, copy.deepcopy(v))
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(600)
        self._timer.timeout.connect(self.save)

    @property
    def settings(self) -> dict:
        return self.data["settings"]

    def note(self, note_id: str) -> dict:
        return self.data["notes"].setdefault(note_id, {})

    def save_soon(self) -> None:
        self._timer.start()

    def save(self) -> None:
        try:
            ks.atomic_write_json(self.path, self.data)
        except OSError:
            log.exception("Could not save layout.json")


# --------------------------------------------------------------------------
# Note widget
# --------------------------------------------------------------------------
class HeaderBar(QFrame):
    """Drag handle strip at the top of a note."""

    def __init__(self, owner: "NoteWidget"):
        super().__init__(owner)
        self.owner = owner
        self._drag: QPoint | None = None
        self.setFixedHeight(30)
        self.setCursor(Qt.SizeAllCursor)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemMove():
                return
            self._drag = ev.globalPosition().toPoint() - self.window().frameGeometry().topLeft()

    def mouseMoveEvent(self, ev):
        if self._drag is not None and ev.buttons() & Qt.LeftButton:
            self.window().move(ev.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, ev):
        self._drag = None


class ItemRow(QWidget):
    def __init__(self, widget: "NoteWidget", item: ks.ItemData):
        super().__init__()
        self.w = widget
        self.item_id = item.id
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.check = QCheckBox()
        self.check.setCursor(Qt.PointingHandCursor)
        self.edit = QLineEdit()
        self.edit.setFrame(False)
        self.edit.setObjectName("itemEdit")
        self.delete_btn = QToolButton()
        self.delete_btn.setText("✕")
        self.delete_btn.setObjectName("rowDelete")
        self.delete_btn.setToolTip("Delete item")
        self.delete_btn.setCursor(Qt.PointingHandCursor)
        lay.addWidget(self.check)
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.delete_btn)
        self.set_item(item)
        self.check.toggled.connect(self._toggled)
        self.edit.textEdited.connect(self._edited)
        self.edit.returnPressed.connect(lambda: self.w.add_edit.setFocus())
        self.delete_btn.clicked.connect(self._delete)

    def set_item(self, item: ks.ItemData) -> None:
        self.check.blockSignals(True)
        self.check.setChecked(item.checked)
        self.check.blockSignals(False)
        if not self.edit.hasFocus() and self.edit.text() != item.text:
            self.edit.setText(item.text)
        self._style(item.checked)

    def _style(self, checked: bool) -> None:
        f = self.edit.font()
        f.setStrikeOut(checked)
        self.edit.setFont(f)
        self.edit.setProperty("done", checked)
        self.edit.style().unpolish(self.edit)
        self.edit.style().polish(self.edit)

    def _toggled(self, value: bool) -> None:
        self._style(value)
        self.w.queue({"op": "set_item_checked", "id": self.w.note_id, "item_id": self.item_id,
                      "value": value})

    def _edited(self, text: str) -> None:
        self.w.queue({"op": "set_item_text", "id": self.w.note_id, "item_id": self.item_id,
                      "value": text})

    def _delete(self) -> None:
        self.w.queue({"op": "delete_item", "id": self.w.note_id, "item_id": self.item_id})
        self.hide()


class NoteWidget(QWidget):
    def __init__(self, app: "KeepWidgetsApp", note: ks.NoteData, index: int = 0):
        super().__init__(None)
        self.app = app
        self.note_id = note.id
        self.note = note
        self.lay = app.layout_store.note(note.id)
        self.lay.setdefault("on_top", True)
        self.lay.setdefault("visible", True)
        self._last_edit = 0.0
        self._loading = False
        self.rows: list[ItemRow] = []

        flags = Qt.FramelessWindowHint | Qt.Tool
        if self.lay["on_top"]:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setWindowTitle(note.title or "Note")
        self.setMinimumSize(180, 130)
        self.setObjectName("noteWindow")
        # The window itself is transparent; the visible note is the inner "card", so templates can
        # have rounded corners. With the Classic template the card fills the window exactly.
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.card = QWidget()
        self.card.setObjectName("card")
        self.card.setAttribute(Qt.WA_StyledBackground, True)
        outer.addWidget(self.card)

        root = QVBoxLayout(self.card)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.root_layout = root

        self.header = HeaderBar(self)
        self.header.setObjectName("header")
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(4, 0, 4, 0)
        hl.setSpacing(2)
        self.add_btn = self._hbtn("+", "New note")
        self.add_btn.clicked.connect(lambda: self.app.new_note("note", near=self))
        self.pin_mark = QLabel("📌")
        self.pin_mark.setObjectName("pinMark")
        self.pin_mark.setToolTip("Pinned in Google Keep")
        self.menu_btn = self._hbtn("⋯", "Menu")
        self.menu_btn.clicked.connect(self.show_menu)
        self.close_btn = self._hbtn("✕", "Hide note (it stays in Google Keep)")
        self.close_btn.clicked.connect(self.hide_note)
        hl.addWidget(self.add_btn)
        hl.addStretch(1)
        hl.addWidget(self.pin_mark)
        hl.addWidget(self.menu_btn)
        hl.addWidget(self.close_btn)
        root.addWidget(self.header)

        body = QVBoxLayout()
        body.setContentsMargins(12, 6, 10, 0)
        body.setSpacing(4)
        self.body_layout = body
        self.title_edit = QLineEdit()
        self.title_edit.setObjectName("title")
        self.title_edit.setFrame(False)
        self.title_edit.setPlaceholderText("Title")
        self.title_edit.textEdited.connect(
            lambda t: self.queue({"op": "set_title", "id": self.note_id, "value": t}))
        body.addWidget(self.title_edit)

        if note.kind == "list":
            self.text_edit = None
            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setFrameShape(QFrame.NoFrame)
            self.scroll.setObjectName("scroll")
            inner = QWidget()
            inner.setObjectName("inner")
            self.rows_layout = QVBoxLayout(inner)
            self.rows_layout.setContentsMargins(0, 0, 0, 0)
            self.rows_layout.setSpacing(1)
            self.add_edit = QLineEdit()
            self.add_edit.setFrame(False)
            self.add_edit.setObjectName("addItem")
            self.add_edit.setPlaceholderText("+ List item")
            self.add_edit.returnPressed.connect(self._add_item)
            self.rows_layout.addWidget(self.add_edit)
            self.rows_layout.addStretch(1)
            self.scroll.setWidget(inner)
            body.addWidget(self.scroll, 1)
        else:
            self.add_edit = None
            self.text_edit = QPlainTextEdit()
            self.text_edit.setObjectName("body")
            self.text_edit.setFrameShape(QFrame.NoFrame)
            self.text_edit.setPlaceholderText("Take a note…")
            self.text_edit.textChanged.connect(self._text_changed)
            body.addWidget(self.text_edit, 1)
        root.addLayout(body, 1)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("foot")
        foot.addSpacing(12)
        foot.addWidget(self.status_lbl)
        foot.addStretch(1)
        self.grip = QSizeGrip(self.card)
        foot.addWidget(self.grip, 0, Qt.AlignBottom | Qt.AlignRight)
        root.addLayout(foot)

        self._flush = QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(400)
        self._flush.timeout.connect(self._flush_ops)
        self._queued: list[dict] = []

        self.apply_colour()
        self.update_from(note, force=True)
        self._place(index)

    # -- construction helpers ---------------------------------------------
    def _hbtn(self, text: str, tip: str) -> QToolButton:
        b = QToolButton()
        b.setText(text)
        b.setToolTip(tip)
        b.setObjectName("hbtn")
        b.setCursor(Qt.PointingHandCursor)
        b.setAutoRaise(True)
        return b

    def _place(self, index: int) -> None:
        g = self.lay.get("geom")
        if g and len(g) == 4:
            x, y, w, h = g
            self.resize(max(180, w), max(130, h))
            self.move(x, y)
            if not any(s.availableGeometry().intersects(self.frameGeometry())
                       for s in QGuiApplication.screens()):
                g = None
        if not g:
            scr = QGuiApplication.primaryScreen().availableGeometry()
            per_col = max(1, (scr.height() - 20) // (DEFAULT_H + 16))
            col, row = divmod(index, per_col)
            x = scr.right() - (col + 1) * (DEFAULT_W + 16)
            y = scr.top() + 16 + row * (DEFAULT_H + 16)
            self.resize(*TEMPLATES[self.app.template_name()]["default"])
            self.move(max(scr.left(), x), y)
        self._remember_geometry()

    def colour_name(self) -> str:
        return self.lay.get("color") or KEEP_TO_STICKY.get(self.note.color, "Yellow")

    def template(self) -> dict:
        return TEMPLATES[self.app.template_name()]

    def palette_entry(self) -> tuple[str, str, str]:
        pal = self.template()["palette"]
        return pal.get(self.colour_name(), pal["Yellow"])

    def apply_colour(self) -> None:
        """Apply the current template + this note's colour (name kept for compatibility)."""
        t = self.template()
        header, bg, fg = self.palette_entry()
        light = is_light_text(fg)
        dim = "rgba(255,255,255,0.65)" if light else "rgba(0,0,0,0.45)"
        hover = "rgba(255,255,255,0.14)" if light else "rgba(0,0,0,0.10)"
        r, inset = t["radius"], t["inset"]
        hr = max(0, r - inset)
        self.root_layout.setContentsMargins(inset, inset, inset, inset)
        self.header.setFixedHeight(t["header_h"])
        self.body_layout.setContentsMargins(*t["margins"])
        self.body_layout.setSpacing(t["spacing"])
        self.setMinimumSize(*t["min"])
        if t["header"] == "accent":
            header_css = (f"background:transparent; border-top:4px solid {header}; "
                          f"border-top-left-radius:{hr}px; border-top-right-radius:{hr}px;")
        else:
            header_css = (f"background:{header}; border-top-left-radius:{hr}px; "
                          f"border-top-right-radius:{hr}px;")
        title_font = f"font-family:'{t['title_font']}';" if t["title_font"] else ""
        card_radius = f" border-radius:{r}px;" if r else ""
        self.setStyleSheet(f"""
            #card {{ background:{bg}; border:{t['border']};{card_radius} }}
            #header {{ {header_css} }}
            QToolButton#hbtn {{ color:{fg}; font-size:{t['btn_px']}px; border:none; padding:2px 6px; }}
            QToolButton#hbtn:hover {{ background:{hover}; }}
            #pinMark {{ font-size:11px; }}
            QLineEdit, QPlainTextEdit {{ background:transparent; color:{fg};
                selection-background-color:rgba(0,0,0,0.25); border:none; }}
            QLineEdit#title {{ {title_font} font-weight:{t['title_weight']}; font-size:{t['title_pt']}pt; }}
            QLineEdit[done="true"] {{ color:{dim}; }}
            QLineEdit#addItem {{ color:{dim}; }}
            QPlainTextEdit#body {{ font-size:{t['body_pt']}pt; }}
            QLineEdit#itemEdit {{ font-size:{t['body_pt']}pt; }}
            #scroll, #inner {{ background:transparent; }}
            QToolButton#rowDelete {{ color:{dim}; border:none; font-size:10px; }}
            QToolButton#rowDelete:hover {{ color:{fg}; }}
            QLabel#foot {{ color:{dim}; font-size:8pt; }}
            QCheckBox::indicator {{ width:14px; height:14px; }}
            QScrollBar:vertical {{ width:6px; background:transparent; }}
            QScrollBar::handle:vertical {{ background:{"rgba(255,255,255,0.25)" if light else "rgba(0,0,0,0.2)"};
                border-radius:3px; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ height:0; }}
        """)

    # -- incoming data ----------------------------------------------------
    def recently_edited(self) -> bool:
        return bool(self._queued) or (time.monotonic() - self._last_edit) < 4.0

    def update_from(self, note: ks.NoteData, force: bool = False, structure: bool = False) -> None:
        colour_changed = note.color != self.note.color
        self.note = note
        self.setWindowTitle(note.title or "Note")
        self.pin_mark.setVisible(note.pinned)
        if colour_changed and not self.lay.get("color"):
            self.apply_colour()
        busy = self.recently_edited() and not force
        self._loading = True
        try:
            if not (busy or self.title_edit.hasFocus()) and self.title_edit.text() != note.title:
                self.title_edit.setText(note.title)
            if self.text_edit is not None:
                if not (busy or self.text_edit.hasFocus()) and self.text_edit.toPlainText() != note.text:
                    self.text_edit.setPlainText(note.text)
            elif force or structure or not busy:
                self._sync_rows(note.items)
        finally:
            self._loading = False

    def _sync_rows(self, items: list[ks.ItemData]) -> None:
        ordered = [i for i in items if not i.checked] + [i for i in items if i.checked]
        if [r.item_id for r in self.rows] == [i.id for i in ordered]:
            for r, i in zip(self.rows, ordered):
                r.set_item(i)
            return
        for r in self.rows:
            r.setParent(None)
            r.deleteLater()
        self.rows = []
        for pos, item in enumerate(ordered):
            row = ItemRow(self, item)
            self.rows.append(row)
            # unchecked items above the "+ List item" field, checked ones below it
            idx = pos if not item.checked else pos + 1
            self.rows_layout.insertWidget(idx, row)

    # -- outgoing edits ---------------------------------------------------
    def queue(self, op: dict) -> None:
        if self._loading:
            return
        self._last_edit = time.monotonic()
        if op["op"] in ("set_item_checked", "delete_item", "add_item"):
            self._flush_ops()
            self.app.submit(op)
            return
        for i, q in enumerate(self._queued):
            if (q["op"], q.get("item_id")) == (op["op"], op.get("item_id")):
                self._queued[i] = op
                break
        else:
            self._queued.append(op)
        self._flush.start()

    def _flush_ops(self) -> None:
        ops, self._queued = self._queued, []
        for op in ops:
            self.app.submit(op)

    def _text_changed(self) -> None:
        if not self._loading:
            self.queue({"op": "set_text", "id": self.note_id, "value": self.text_edit.toPlainText()})

    def _add_item(self) -> None:
        text = self.add_edit.text().strip()
        if text:
            self.queue({"op": "add_item", "id": self.note_id, "text": text})
            self.add_edit.clear()

    # -- menu -------------------------------------------------------------
    def show_menu(self) -> None:
        m = QMenu(self)
        colours = m.addMenu("Colour")
        pal = self.template()["palette"]
        for name in PALETTE:
            header, bg, _fg = pal[name]
            pm = QPixmap(14, 14)
            pm.fill(swatch_colour(header))
            a = colours.addAction(QIcon(pm), name)
            a.setCheckable(True)
            a.setChecked(name == self.colour_name())
            a.triggered.connect(lambda _=False, n=name: self.set_colour(n))
        m.addSeparator()
        pin = m.addAction("Pin in Google Keep")
        pin.setCheckable(True)
        pin.setChecked(self.note.pinned)
        pin.triggered.connect(lambda v: self.queue({"op": "set_pinned", "id": self.note_id,
                                                   "value": v}))
        top = m.addAction("Always on top")
        top.setCheckable(True)
        top.setChecked(self.lay["on_top"])
        top.triggered.connect(self.set_on_top)
        m.addAction("Hide this note", self.hide_note)
        m.addSeparator()
        m.addAction("Open Google Keep in browser",
                    lambda: QDesktopServices.openUrl(QUrl("https://keep.google.com/")))
        m.addAction("Move to trash…", self.trash)
        m.exec(self.menu_btn.mapToGlobal(QPoint(0, self.menu_btn.height())))

    def set_colour(self, name: str) -> None:
        self.lay["color"] = name  # always remembered locally (Keep colour push may be ignored)
        self.app.layout_store.save_soon()
        self.apply_colour()
        self.queue({"op": "set_color", "id": self.note_id, "value": PALETTE[name][3]})

    def set_on_top(self, on: bool) -> None:
        self.lay["on_top"] = bool(on)
        self.app.layout_store.save_soon()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(on))
        self.show()

    def hide_note(self) -> None:
        self.lay["visible"] = False
        self.app.layout_store.save_soon()
        self.hide()

    def trash(self) -> None:
        r = QMessageBox.question(self, APP_TITLE,
                                 "Move this note to the Trash in Google Keep?\n\n"
                                 "You can restore it from Keep's Trash for 7 days.")
        if r == QMessageBox.Yes:
            self._flush_ops()
            self.app.submit({"op": "trash", "id": self.note_id})
            self.hide()

    # -- geometry ---------------------------------------------------------
    def _remember_geometry(self) -> None:
        g = self.geometry()
        self.lay["geom"] = [g.x(), g.y(), g.width(), g.height()]
        self.app.layout_store.save_soon()

    def moveEvent(self, ev):
        super().moveEvent(ev)
        if self.isVisible():
            self._remember_geometry()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.isVisible():
            self._remember_geometry()


# --------------------------------------------------------------------------
# Setup (sign-in) dialog
# --------------------------------------------------------------------------
SETUP_STEPS = f"""
<p><b>KeepNotes Widgets uses an unofficial Google Keep API.</b> It can stop working if Google
changes things. Your master token gives access to your Google account; it is stored only in
<b>Windows Credential Manager</b>, never in a file.</p>
<p><b>Option A: oauth_token cookie (recommended)</b></p>
<ol>
<li>Click <b>Open Google sign-in page</b>, or open <a href="{EMBEDDED_SETUP_URL}">{EMBEDDED_SETUP_URL}</a> in Chrome or Edge.</li>
<li>Sign in with your Google account and click <b>I agree</b>. The page may keep loading forever; that is normal.</li>
<li>Press <b>F12</b>, open <b>Application</b> &rsaquo; <b>Cookies</b> &rsaquo; <i>https://accounts.google.com</i>
(Firefox: <b>Storage</b> &rsaquo; Cookies).</li>
<li>Find the cookie <code>oauth_token</code> and copy its value (it starts with <code>oauth2_4/</code>).</li>
<li>Paste it below with your email and click <b>Sign in</b> within a few minutes (the value is single-use).</li>
</ol>
<p><b>Option B:</b> paste a master token you already have (starts with <code>aas_et/</code>).</p>
"""


class SetupDialog(QDialog):
    finished_signin = Signal(bool, str)

    def __init__(self, app: "KeepWidgetsApp", parent=None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle(f"{APP_TITLE} – Connect Google Keep")
        self.setWindowIcon(app_icon())
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        info = QLabel(SETUP_STEPS)
        info.setWordWrap(True)
        info.setOpenExternalLinks(True)
        info.setTextFormat(Qt.RichText)
        v.addWidget(info)
        open_btn = QPushButton("Open Google sign-in page")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(EMBEDDED_SETUP_URL)))
        v.addWidget(open_btn, 0, Qt.AlignLeft)

        form = QFormLayout()
        self.email = QLineEdit(app.engine.email or "")
        self.email.setPlaceholderText("you@gmail.com")
        form.addRow("Google email:", self.email)
        kind = QHBoxLayout()
        self.rb_cookie = QRadioButton("oauth_token cookie")
        self.rb_master = QRadioButton("Master token")
        self.rb_cookie.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_cookie)
        grp.addButton(self.rb_master)
        kind.addWidget(self.rb_cookie)
        kind.addWidget(self.rb_master)
        kind.addStretch(1)
        form.addRow("I am pasting:", kind)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("oauth2_4/…")
        self.rb_master.toggled.connect(
            lambda m: self.token.setPlaceholderText("aas_et/…" if m else "oauth2_4/…"))
        form.addRow("Value:", self.token)
        v.addLayout(form)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        self.buttons = QDialogButtonBox()
        self.ok_btn = self.buttons.addButton("Sign in", QDialogButtonBox.AcceptRole)
        self.buttons.addButton(QDialogButtonBox.Cancel)
        self.ok_btn.clicked.connect(self.sign_in)
        self.buttons.rejected.connect(self.reject)
        v.addWidget(self.buttons)
        self.finished_signin.connect(self._done)

    def sign_in(self) -> None:
        email = self.email.text().strip()
        value = self.token.text().strip()
        if "@" not in email or not value:
            self.status.setText("Enter your Google email and paste the value first.")
            return
        if self.rb_cookie.isChecked() and value.startswith("aas_et/"):
            self.rb_master.setChecked(True)
        master_mode = self.rb_master.isChecked()
        if master_mode and not ks.looks_like_master_token(value):
            self.status.setText("That does not look like a master token (it should start with aas_et/).")
            return
        self.ok_btn.setEnabled(False)
        self.status.setText("Signing in…")

        def work():
            try:
                master = value if master_mode else ks.exchange_oauth_token(
                    email, value, self.app.engine.android_id)
                self.app.engine.sign_in(email, master, save=True)
                self.finished_signin.emit(True, "")
            except Exception as exc:  # noqa: BLE001
                msg = str(exc) if isinstance(exc, ks.AuthError) else ks._friendly_error(exc)
                log.warning("Sign-in failed: %s", type(exc).__name__)
                self.finished_signin.emit(False, msg)

        threading.Thread(target=work, daemon=True).start()

    def _done(self, ok: bool, msg: str) -> None:
        self.ok_btn.setEnabled(True)
        if ok:
            self.token.clear()
            self.app.worker.sync_now()
            self.accept()
        else:
            self.status.setText(f"<span style='color:#B3261E'>Sign-in failed: {msg}</span>")


# --------------------------------------------------------------------------
# Settings dialog
# --------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, app: "KeepWidgetsApp"):
        super().__init__()
        self.app = app
        s = app.layout_store.settings
        self.setWindowTitle(f"{APP_TITLE} – Settings")
        self.setWindowIcon(app_icon())
        v = QVBoxLayout(self)

        show = QGroupBox("Which notes get a widget")
        sv = QVBoxLayout(show)
        self.rb_all = QRadioButton("All notes (not archived or trashed)")
        self.rb_pinned = QRadioButton("Only pinned notes")
        self.rb_label = QRadioButton("Only notes with label:")
        self.label_combo = QComboBox()
        self.label_combo.addItems(app.labels or [])
        if s.get("label"):
            if self.label_combo.findText(s["label"]) < 0:
                self.label_combo.addItem(s["label"])
            self.label_combo.setCurrentText(s["label"])
        {"pinned": self.rb_pinned, "label": self.rb_label}.get(s.get("filter"), self.rb_all).setChecked(True)
        sv.addWidget(self.rb_all)
        sv.addWidget(self.rb_pinned)
        row = QHBoxLayout()
        row.addWidget(self.rb_label)
        row.addWidget(self.label_combo, 1)
        sv.addLayout(row)
        v.addWidget(show)

        form = QFormLayout()
        self.interval = QSpinBox()
        self.interval.setRange(15, 3600)
        self.interval.setSuffix(" s")
        self.interval.setValue(int(s.get("interval", 60)))
        form.addRow("Background sync every:", self.interval)
        self.account = QLabel(app.engine.email or "not signed in")
        form.addRow("Google account:", self.account)
        self.template_combo = QComboBox()
        self.template_combo.addItems(list(TEMPLATES))
        self._orig_template = app.template_name()
        self.template_combo.setCurrentText(self._orig_template)
        self.template_combo.setToolTip("Look of all note widgets. The note colours still apply.")
        self.template_combo.currentTextChanged.connect(app.apply_template)  # live preview
        form.insertRow(0, "Template:", self.template_combo)
        v.addLayout(form)
        self.hotkey_group = hk.HotkeyGroup(s.get("hotkeys") or {},
                                           "Click a field and press a combination (default toggle: "
                                           "Ctrl+Alt+K). Leave empty for no shortcut.")
        v.addWidget(self.hotkey_group)

        acc = QHBoxLayout()
        b1 = QPushButton("Sign in / switch account…")
        b1.clicked.connect(lambda: (self.accept(), app.show_setup()))
        b2 = QPushButton("Sign out")
        b2.clicked.connect(self._sign_out)
        b3 = QPushButton("Open data folder")
        b3.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(app.engine.dir))))
        acc.addWidget(b1)
        acc.addWidget(b2)
        acc.addWidget(b3)
        v.addLayout(acc)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def reject(self) -> None:
        self.app.apply_template(self._orig_template)  # undo the live preview
        super().reject()

    def _sign_out(self) -> None:
        if QMessageBox.question(self, APP_TITLE, "Remove the saved Google login from this PC?\n"
                                "Your cached notes stay visible until you sign in again.") == QMessageBox.Yes:
            self.app.engine.sign_out()
            self.account.setText("not signed in")
            self.app.update_status()

    def _save(self) -> None:
        s = self.app.layout_store.settings
        s["filter"] = "pinned" if self.rb_pinned.isChecked() else "label" if self.rb_label.isChecked() else "all"
        s["label"] = self.label_combo.currentText()
        s["interval"] = self.interval.value()
        s["template"] = self.template_combo.currentText()
        self._orig_template = s["template"]
        self.app.apply_template(s["template"])
        self.app.worker.interval = float(self.interval.value())
        s["hotkeys"] = self.hotkey_group.values()
        self.app.layout_store.save()
        self.app.refresh_widgets()
        errors = self.app.apply_hotkeys(notify=False)
        if errors:
            QMessageBox.warning(self, APP_TITLE, "These global hotkeys could not be registered:\n\n"
                                + hk.describe_errors(errors) +
                                "\n\nThe other settings are saved. Choose a different combination "
                                "(or clear the field) and press OK again.")
            return
        self.accept()


# --------------------------------------------------------------------------
# Application / tray
# --------------------------------------------------------------------------
class Bridge(QObject):
    event = Signal(str, object)


class KeepWidgetsApp(QObject):
    def __init__(self, qapp: QApplication, engine: ks.SyncEngine, demo: bool = False):
        super().__init__()
        self.qapp = qapp
        self.engine = engine
        self.demo = demo
        self.layout_store = LayoutStore(engine.dir / "layout.json")
        self.widgets: dict[str, NoteWidget] = {}
        self.snapshot: dict[str, ks.NoteData] = {}
        self.labels: list[str] = []
        self.status_info: dict = {}
        self.bridge = Bridge()
        self.bridge.event.connect(self.on_event)
        self.worker = ks.SyncWorker(engine, self.bridge.event.emit,
                                    interval=self.layout_store.settings.get("interval", 60))
        self._pending_focus: set[str] = set()
        self._created_here: set[str] = set()  # always shown, even with a pinned/label filter
        self._setup_dlg: SetupDialog | None = None
        self._preview_template: str | None = None
        self._build_tray()
        self.hotkeys = hk.HotkeyManager(self)
        self.hotkeys.triggered.connect(self.on_hotkey)

    # -- templates ------------------------------------------------------------
    def template_name(self) -> str:
        name = self._preview_template or self.layout_store.settings.get("template", DEFAULT_TEMPLATE)
        return name if name in TEMPLATES else DEFAULT_TEMPLATE

    def apply_template(self, name: str | None = None) -> None:
        """Re-style all notes. `name` previews a template without saving it (Settings dialog)."""
        saved = self.layout_store.settings.get("template", DEFAULT_TEMPLATE)
        self._preview_template = name if name and name != saved else None
        for w in self.widgets.values():
            w.apply_colour()

    # -- global hotkeys -------------------------------------------------------
    def apply_hotkeys(self, notify: bool = True) -> list:
        errors = self.hotkeys.apply(self.layout_store.settings.get("hotkeys") or {})
        for action, text in self.hotkeys.active.items():
            log.info("Global hotkey %s = %s", action, text)
        for action, text, reason in errors:
            log.warning("Global hotkey %s = %s not registered: %s", action, text, reason)
        if errors and notify:
            self.tray.showMessage(APP_TITLE, "Some global hotkeys could not be registered (see Settings):\n"
                                  + hk.describe_errors(errors), QSystemTrayIcon.Warning, 8000)
        return errors

    def on_hotkey(self, action: str) -> None:
        {"toggle": self.toggle_all, "show": self.show_all, "hide": self.hide_all}.get(action, lambda: None)()

    def toggle_all(self) -> None:
        if any(w.isVisible() for w in self.widgets.values()):
            self.hide_all()
        else:
            self.show_all()

    # -- tray ---------------------------------------------------------------
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(APP_TITLE)
        m = QMenu()
        self.status_action = m.addAction("Starting…")
        self.status_action.setEnabled(False)
        m.addSeparator()
        m.addAction("Sync now", self.worker.sync_now)
        m.addAction("Show all notes", self.show_all)
        m.addAction("Hide all notes", self.hide_all)
        m.addAction("Toggle all notes", self.toggle_all)
        m.addSeparator()
        m.addAction("New note", lambda: self.new_note("note"))
        m.addAction("New list", lambda: self.new_note("list"))
        m.addSeparator()
        m.addAction("Settings…", self.show_settings)
        self.autostart_action = m.addAction("Start with Windows")
        self.autostart_action.setCheckable(True)
        lnk = startup_shortcut_path()
        self.autostart_action.setEnabled(lnk is not None)
        self.autostart_action.setChecked(bool(lnk and lnk.exists()))
        self.autostart_action.toggled.connect(self.set_autostart)
        m.addSeparator()
        m.addAction("Quit", self.quit)
        self.tray_menu = m
        self.tray.setContextMenu(m)
        self.tray.activated.connect(
            lambda r: self.show_all() if r in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()

    def update_status(self) -> None:
        st = self.status_info
        status = self.engine.status
        if self.demo:
            text, paused = "Demo mode – not synced", False
        elif status == "ok":
            ls = st.get("last_sync")
            text = f"Synced {ls:%H:%M}" if isinstance(ls, _dt.datetime) else "Connected"
            text += f" · {st.get('email') or ''}"
            paused = False
        elif status == "paused":
            text = f"Sync paused: {self.engine.last_error or 'unknown error'} – showing cached notes"
            paused = True
        else:
            text, paused = "Not signed in – open Settings to connect", True
        self.status_action.setText(text)
        self.tray.setIcon(app_icon(paused))
        self.tray.setToolTip(f"{APP_TITLE}\n{text}")
        foot = "" if not paused else ("sync paused" if status == "paused" else "not signed in")
        for w in self.widgets.values():
            w.status_lbl.setText(foot)

    # -- events from sync thread ------------------------------------------
    def on_event(self, kind: str, payload: dict) -> None:
        self.snapshot = payload["snapshot"]
        self.labels = payload.get("labels", [])
        self.status_info = payload
        structure_ids = set()
        for ev in payload.get("events", []):
            if ev["event"] == "created":
                self._pending_focus.add(ev["id"])
                self._created_here.add(ev["id"])
                self.layout_store.note(ev["id"])["visible"] = True
                if ev.get("tag") and ev["tag"].startswith("near:"):
                    self.layout_store.note(ev["id"])["near"] = ev["tag"][5:]
            elif ev["event"] == "structure":
                structure_ids.add(ev["id"])
        self.refresh_widgets(structure_ids)
        if kind == "synced":
            res = payload.get("result")
            if res is not None and res.conflicts:
                self.tray.showMessage(APP_TITLE, f"{len(res.conflicts)} note(s) were changed in Keep "
                                      "and here at the same time; Keep's version was kept.",
                                      QSystemTrayIcon.Warning, 6000)
        self.update_status()

    def filtered(self) -> list[ks.NoteData]:
        s = self.layout_store.settings
        notes = list(self.snapshot.values())
        if s.get("filter") == "pinned":
            notes = [n for n in notes if n.pinned or n.id in self._created_here]
        elif s.get("filter") == "label" and s.get("label"):
            notes = [n for n in notes if s["label"] in n.labels or n.id in self._created_here]
        return notes

    def refresh_widgets(self, structure_ids: set | None = None) -> None:
        structure_ids = structure_ids or set()
        notes = self.filtered()
        wanted = {n.id for n in notes}
        for nid in list(self.widgets):
            if nid not in wanted:
                w = self.widgets.pop(nid)
                w.close()
                w.deleteLater()
        for n in notes:
            w = self.widgets.get(n.id)
            if w is None:
                w = NoteWidget(self, n, index=len(self.widgets))
                self.widgets[n.id] = w
                near = self.layout_store.note(n.id).pop("near", None)
                if near and near in self.widgets and near != n.id:
                    src = self.widgets[near]
                    w.move(src.x() + 30, src.y() + 30)
                if w.lay.get("visible", True):
                    w.show()
            else:
                w.update_from(n, structure=n.id in structure_ids)
            if n.id in self._pending_focus:
                self._pending_focus.discard(n.id)
                w.show()
                w.raise_()
                w.activateWindow()
                w.title_edit.setFocus()
        self.layout_store.save_soon()

    # -- actions ------------------------------------------------------------
    def submit(self, op: dict) -> None:
        self.worker.submit(op)

    def new_note(self, kind: str = "note", near: NoteWidget | None = None) -> None:
        self.submit({"op": "create", "kind": kind, "title": "", "text": "",
                     "tag": f"near:{near.note_id}" if near else None})

    def show_all(self) -> None:
        for w in self.widgets.values():
            w.lay["visible"] = True
            w.show()
            w.raise_()
        self.layout_store.save_soon()

    def hide_all(self) -> None:
        for w in self.widgets.values():
            w.lay["visible"] = False
            w.hide()
        self.layout_store.save_soon()

    def show_settings(self) -> None:
        SettingsDialog(self).exec()

    def show_setup(self) -> None:
        if self._setup_dlg is None:
            self._setup_dlg = SetupDialog(self)
        self._setup_dlg.show()
        self._setup_dlg.raise_()
        self._setup_dlg.activateWindow()

    def set_autostart(self, on: bool) -> None:
        lnk = startup_shortcut_path()
        if lnk is None:
            return
        try:
            if on:
                create_shortcut(lnk, pythonw_executable(), f'"{APP_DIR / "keep_widgets.py"}"',
                                str(APP_DIR), str(APP_DIR / "icon.ico"))
                log.info("Start with Windows enabled")
            elif lnk.exists():
                lnk.unlink()
                log.info("Start with Windows disabled")
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not change startup shortcut")
            QMessageBox.warning(None, APP_TITLE, f"Could not change the startup shortcut:\n{exc}")
            self.autostart_action.blockSignals(True)
            self.autostart_action.setChecked(lnk.exists())
            self.autostart_action.blockSignals(False)

    def start(self) -> None:
        self.apply_hotkeys()
        self.worker.start()
        if not self.demo and self.engine.creds.get() is None:
            QTimer.singleShot(400, self.show_setup)

    def quit(self) -> None:
        self.hotkeys.unregister_all()
        for w in self.widgets.values():
            w._flush_ops()
        self.layout_store.save()
        self.worker.stop()
        self.worker.join(timeout=5)
        self.tray.hide()
        self.qapp.quit()


# --------------------------------------------------------------------------
# Demo data (also used for screenshots/tests)
# --------------------------------------------------------------------------
def build_demo_keep():
    import gkeepapi
    from gkeepapi import node as gn

    keep = gkeepapi.Keep()
    n1 = keep.createNote("Call the plumber", "Thursday after 14:00.\nAsk about the kitchen tap too.")
    n1.pinned = True
    l1 = keep.createList("Groceries", [("Milk", False), ("Eggs", True), ("Coffee beans", False),
                                       ("Bread", False)])
    l1.color = gn.ColorValue.Green
    n3 = keep.createNote("Gift ideas", "Headphones\nCookbook\nConcert tickets")
    n3.color = gn.ColorValue.Pink
    keep.createNote("Meeting notes – Q4 planning",
                    "Goals: ship the new onboarding flow, cut support tickets by 20%, and "
                    "migrate the remaining services to the new cluster.\n\n"
                    "Action items:\n- Draft the timeline by Friday\n- Book the review meeting\n"
                    "- Collect feedback from the sales team\n- Update the roadmap document\n"
                    "- Prepare the budget overview for next quarter\n- Send recap to everyone")
    return keep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=APP_TITLE)
    ap.add_argument("--demo", action="store_true", help="show sample notes without signing in")
    ap.add_argument("--data-dir", help="override the data folder (default %%APPDATA%%\\KeepNotesWidgets)")
    args = ap.parse_args(argv)
    if args.data_dir:
        os.environ["KNW_DATA_DIR"] = args.data_dir
    directory = ks.data_dir()
    if args.demo:
        directory = directory / "demo"
        directory.mkdir(exist_ok=True)
    setup_logging(directory)

    QApplication.setQuitOnLastWindowClosed(False)
    qapp = QApplication(sys.argv[:1])
    qapp.setApplicationName(APP_TITLE)
    qapp.setWindowIcon(app_icon())
    qapp.setFont(QFont("Segoe UI", 10))

    lock = QLockFile(str(directory / "app.lock"))
    if not lock.tryLock(200):
        QMessageBox.information(None, APP_TITLE, "KeepNotes Widgets is already running (see the tray icon).")
        return 0
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning("No system tray available")

    if args.demo:
        engine = ks.SyncEngine(keep=build_demo_keep(), creds=ks.MemoryCredentialStore(),
                               directory=directory, offline_only=True)
        engine.cache_path = directory / "demo-state.json"
    else:
        engine = ks.SyncEngine(directory=directory)
    app = KeepWidgetsApp(qapp, engine, demo=args.demo)
    app.start()
    log.info("KeepNotes Widgets started (data folder: %s)", directory)
    rc = qapp.exec()
    lock.unlock()
    return rc


if __name__ == "__main__":
    sys.exit(main())
