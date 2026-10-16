#!/usr/bin/env python3
"""saywell_popup_qt: the saywell popup in PySide6, a port of the GTK `saywell-popup` for Windows.

Type a draft (mixing Chinese is fine), press Ctrl+Enter to analyze it with `saywell --json --stream`,
pick a version, and press Ctrl+Shift+Enter: the picked text goes to the clipboard, the `inserted`
signal fires with it, and the window closes. Pasting it into the previous window is left to the
system integration (global hotkey, focus restore, SendInput), which connects to that signal.

Keys:
  Ctrl+Enter        analyze the draft (again)
  Alt+1 / 2 / 3     pick fixed / improved / my draft
  Alt+Enter         copy the picked version into the draft to keep writing (also: double-click it)
  Ctrl+Shift+Enter  insert the picked version (clipboard + `inserted` signal)
  Enter (Ask box)   ask a follow-up question about the English
  Esc               close without inserting

Run:  uv run --no-project --with pyside6-essentials python windows/saywell_popup_qt.py [--backend PATH]
The backend defaults to the `saywell` script in this repo, started with the current Python;
override it with --backend or $SAYWELL_BACKEND.

Parity with saywell-popup (GTK), item by item:
  [x] draft as a top bar, placeholder, grows to ~130 px, "Ctrl Enter · Check" button
  [x] two panes in a splitter: versions on the left, progress / meaning / chat on the right
  [x] versions as selectable cards (FIXED / IMPROVED / MY DRAFT) with their Alt+N key cap
  [x] changed words highlighted against the original, "Changes: old → new" tooltip on the card
  [x] 含义 back-translation under fixed and improved, 为什么 under improved
  [x] streamed partial versions while the model writes
  [x] progress: bar easing toward the current step, four steps marked ✓ / ● / ○ from stage events
  [x] meaning check after the progress: ⚠ meaning may have changed / ◐ tone changed / ✓ unchanged
  [x] 追问 chat: question bubbles, "Thinking…" then the answer with **bold** and `code`,
      earlier answers sent as history, auto-scroll to the bottom
  [x] footer hints (clickable), status line ("Checking… N s", "Checked in N s", "Check failed"),
      "Insert <version>" follows the selection
  [x] toasts: empty draft, check failed (backend stderr), moved to draft, check first, nothing to insert
  [x] every key above, including double-click on a version and keypad Enter; key caps read
      Ctrl / Alt / Shift / Enter instead of ⌃ ⌥ ⇧ ↵, which Windows fonts render poorly
  [x] a new check or closing the window kills the running backend
  [-] paste into the previous window: deferred to the system integration stage; here insert puts the
      text on the clipboard and emits `inserted` (GTK uses wl-copy + hyprctl + wtype, Hyprland only)
  [-] single instance / raise on a second hotkey press (GTK: Gtk.Application): same stage, it owns the hotkey
  [-] screen dimming: Hyprland compositor rule in the GTK version, no equivalent here
"""

import argparse
import difflib
import html
import json
import os
import re
import sys
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QProcess, QProcessEnvironment, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QTextCursor
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSplitter,
                               QVBoxLayout, QWidget)

HERE = Path(__file__).resolve().parent
DEFAULT_BACKEND = HERE.parent / "saywell"

# The GTK palette, flattened onto the window background where Qt needs opaque colors.
CSS = """
#popup { background-color: #1a1a1e; }
QWidget { color: #ececf1; font-size: 14px; }
QToolTip { color: #ececf1; background-color: #2a2a30; border: 1px solid #3a3a42; padding: 4px; }
#draft { background: transparent; border: none; color: #f4f4f6; font-size: 17px; }
#topbar { padding: 6px 10px 4px 10px; }
#divider { background-color: #2c2c31; }
QSplitter::handle { background-color: #2c2c31; }
QScrollArea, #pane { background: transparent; border: none; }
QLabel[css="eyebrow"] { font-size: 11px; font-weight: 700; letter-spacing: 1px; color: #8e8e98; }
QLabel[css="dim"] { color: #8e8e98; }
QLabel[css="muted"] { color: #b4b4bd; }
#card { border-radius: 10px; background-color: #212125; }
#card:hover { background-color: #27272b; }
#card[selected="true"] { background-color: #1f2a3b; border-left: 3px solid #3584e4; }
QLabel[css="version-text"] { font-size: 16px; color: #f4f4f6; }
#warncard { border-radius: 10px; background-color: #2a281e; }
#warncard QLabel { color: #f6d32d; }
QLabel[css="ok-line"] { color: #8ff0a4; }
QProgressBar { min-height: 6px; max-height: 6px; border-radius: 3px; background-color: #2c2c31; border: none; }
QProgressBar::chunk { border-radius: 3px; background-color: #3584e4; }
QLabel[css="step-done"] { color: #8ff0a4; }
QLabel[css="step-active"] { color: #f4f4f6; font-weight: 600; }
QLabel[css="step-todo"] { color: #6e6e78; }
QLabel[css="bubble-me"] { border-radius: 12px; padding: 7px 12px; background-color: #3584e4; color: white; }
#ask { border-radius: 10px; background-color: #29292d; min-height: 36px; padding: 0 10px; border: none; }
#footer { background-color: #1e1e22; }
QLabel[css="kbd"] { font-size: 11px; font-weight: 600; padding: 1px 6px; border-radius: 5px;
       background-color: #333338; color: #d6d6dc; }
#hint { padding: 3px 8px; border-radius: 7px; font-size: 13px; color: #b4b4bd; background: transparent;
        border: none; }
#hint:hover { background-color: #2a2a2f; }
#hint[primary="true"] { color: #f4f4f6; background-color: #2f2f34; }
#toast { background-color: #3a3a40; color: #f4f4f6; border-radius: 16px; padding: 8px 16px; }
"""

# #3584e4 at 32% over the window background: Qt rich text has no alpha backgrounds.
HIGHLIGHT = '<span style="background-color:#233c5d; color:#ffffff;">{}</span>'


def highlight_html(old, new):
    """HTML for new with the changed words highlighted, plus a list of 'old → new' changes."""
    tokenize = lambda s: re.findall(r"\s+|\w+|[^\w\s]", s)
    a, b = tokenize(old), tokenize(new)
    out, changes = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        added = "".join(b[j1:j2])
        if op == "equal":
            out.append(html.escape(added))
            continue
        removed = "".join(a[i1:i2]).strip()
        if added.strip():
            lead = added[:len(added) - len(added.lstrip())]
            trail = added[len(added.rstrip()):]
            out.append(html.escape(lead) + HIGHLIGHT.format(html.escape(added.strip())) + html.escape(trail))
        else:
            out.append(html.escape(added))
        if removed or added.strip():
            changes.append(f"{removed or '∅'} → {added.strip() or '∅'}")
    return pre("".join(out)), changes


def md_html(text):
    """Escape text and turn **bold** and `code` into HTML."""
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return pre(re.sub(r"`([^`]+)`", r"<code>\1</code>", text))


def pre(markup):
    """Keep the line breaks and runs of spaces of escaped text in a rich-text label."""
    return f'<div style="white-space:pre-wrap;">{markup}</div>'


def backend_command(path=None):
    """argv prefix for the backend: a Python script runs with this interpreter, anything else as is."""
    path = str(path or os.environ.get("SAYWELL_BACKEND") or DEFAULT_BACKEND)
    if Path(path).suffix.lower() in (".exe", ".cmd", ".bat", ".com"):
        return [path]
    return [sys.executable, path]


def label(text="", css="", rich=False, wrap=True, selectable=False):
    w = QLabel()
    w.setTextFormat(Qt.RichText if rich else Qt.PlainText)
    w.setText(text)
    w.setWordWrap(wrap)
    if selectable:
        w.setTextInteractionFlags(Qt.TextSelectableByMouse)
    if css:
        w.setProperty("css", css)
    return w


def vbox(spacing=0, margins=0):
    box = QVBoxLayout()
    box.setSpacing(spacing)
    box.setContentsMargins(margins, margins, margins, margins)
    return box


def clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        if widget := item.widget():
            widget.setParent(None)  # out of the tree now, not at the next event loop turn
            widget.deleteLater()
        elif item.layout():
            clear(item.layout())
            item.layout().deleteLater()


class Hint(QPushButton):
    """A footer hint: key caps followed by a word, clickable when it has an action."""

    def __init__(self, keys, text, action=None, primary=False):
        super().__init__()
        self.setObjectName("hint")
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor if action else Qt.ArrowCursor)
        if primary:
            self.setProperty("primary", "true")
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 3, 8, 3)
        row.setSpacing(6)
        for k in keys.split():
            row.addWidget(label(k, "kbd", wrap=False))
        self.word = label(text, wrap=False)
        row.addWidget(self.word)
        if action:
            self.clicked.connect(action)

    def sizeHint(self):
        return self.layout().sizeHint()


class Card(QFrame):
    """One version: clicking selects it, double-clicking moves it into the draft."""

    def __init__(self, key, on_click, on_double_click):
        super().__init__()
        self.setObjectName("card")
        self.key = key
        self.on_click, self.on_double_click = on_click, on_double_click
        self.setAccessibleName(Popup.NAMES[key])

    def mousePressEvent(self, event):
        self.on_click(self.key)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.on_double_click(self.key)


class Draft(QPlainTextEdit):
    """The draft box: grows with its text between one line and about 130 px."""

    def __init__(self):
        super().__init__()
        self.setObjectName("draft")
        self.setPlaceholderText("Type your message — Chinese words are fine")
        self.setTabChangesFocus(True)
        self.setAccessibleName("Draft")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.document().contentsChanged.connect(self.fit)
        self.fit()

    def fit(self):
        line = self.fontMetrics().lineSpacing()
        lines = max(1, int(self.document().size().height()))
        self.setFixedHeight(min(130, max(28, lines * line + 12)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit()


class Popup(QWidget):
    VERSIONS = (("fixed", "FIXED", "Alt+1"), ("improved", "IMPROVED", "Alt+2"), ("mine", "MY DRAFT", "Alt+3"))
    NAMES = {"fixed": "Fixed", "improved": "Improved", "mine": "My draft"}
    # Steps shown while checking, and the model sections that belong to each.
    STEPS = (("Fixing grammar", ("fixed",)), ("Improving wording", ("improved", "why")),
             ("Translating back · 含义", ("fixed_zh", "improved_zh")), ("Checking meaning", ("flag",)))

    inserted = Signal(str)     # the picked text, already on the clipboard; the window closes next
    checked = Signal(bool)     # an analysis finished (True) or failed (False)
    answered = Signal()        # a follow-up answer arrived

    def __init__(self, backend=None):
        super().__init__()
        self.backend = backend or backend_command()
        self.setObjectName("popup")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowTitle("Saywell")
        self.resize(1040, 660)
        self.setStyleSheet(CSS)
        self.analysis = None        # latest --json result
        self.history = []           # follow-up questions and answers
        self.proc = None            # running analysis
        self.asks = []              # running follow-up questions
        self.choice = "fixed"
        self.started = 0.0
        self.step = 0
        self.fraction = 0.0

        # Top bar: the draft, like a launcher's search field.
        self.input = Draft()
        top_w = QWidget(objectName="topbar")
        top = QHBoxLayout(top_w)
        top.setContentsMargins(14, 12, 14, 10)
        top.setSpacing(12)
        top.addWidget(self.input, 1, Qt.AlignTop)
        top.addWidget(Hint("Ctrl Enter", "Check", self.analyze), 0, Qt.AlignTop)

        # Left: versions as selectable cards.
        self.left_empty = label("Press Ctrl+Enter to check your draft.\nVersions appear here.", "dim")
        self.left_empty.setAlignment(Qt.AlignCenter)
        self.versions = vbox(6)
        left_w = QWidget(objectName="pane")
        left = vbox(10, 12)
        left_w.setLayout(left)
        left.addWidget(self.left_empty, 1)
        left.addLayout(self.versions)
        left.addStretch(1)
        self.left_scroll = scroll(left_w)

        # Right: progress while checking, then the meaning check and the follow-up chat,
        # with its entry pinned at the bottom.
        self.progress = QWidget()
        prog = vbox(10)
        self.progress.setLayout(prog)
        prog.addWidget(label("CHECKING", "eyebrow"))
        self.bar = QProgressBar(textVisible=False, minimum=0, maximum=1000)
        prog.addWidget(self.bar)
        self.steps = vbox(6)
        prog.addLayout(self.steps)
        self.progress.setVisible(False)
        self.meaning_w = QWidget()
        self.meaning = vbox(6)
        self.meaning_w.setLayout(self.meaning)
        self.meaning_w.setVisible(False)
        self.chat_intro = QWidget()
        intro = vbox(4)
        self.chat_intro.setLayout(intro)
        intro.addWidget(label("追问", "eyebrow"))
        intro.addWidget(label("Why this word? Too formal? Ask about any change.", "dim"))
        self.chat = vbox(10)
        right_w = QWidget(objectName="pane")
        right = vbox(14, 12)
        right_w.setLayout(right)
        right.addWidget(self.progress)
        right.addWidget(self.meaning_w)
        right.addWidget(self.chat_intro)
        right.addLayout(self.chat)
        right.addStretch(1)
        self.right_scroll = scroll(right_w)
        self.ask = QLineEdit(objectName="ask", placeholderText="Ask a follow-up…  e.g. why not “ensure”?")
        self.ask.returnPressed.connect(self.on_ask)
        right_col = QWidget()
        rc = vbox()
        right_col.setLayout(rc)
        rc.addWidget(self.right_scroll, 1)
        ask_box = vbox(0, 12)
        ask_box.addWidget(self.ask)
        rc.addLayout(ask_box)

        body = QSplitter(Qt.Horizontal, childrenCollapsible=False, handleWidth=1)
        body.addWidget(self.left_scroll)
        body.addWidget(right_col)
        body.setSizes([560, 480])

        # Footer: key hints, clickable.
        footer_w = QWidget(objectName="footer")
        footer = QHBoxLayout(footer_w)
        footer.setContentsMargins(12, 8, 12, 8)
        footer.setSpacing(4)
        footer.addWidget(Hint("esc", "Cancel", self.close))
        footer.addWidget(Hint("Alt 1-3", "Pick"))
        footer.addWidget(Hint("Alt Enter", "Edit in draft", lambda: self.use_as_draft(self.choice)))
        self.status = label("", "dim", wrap=False)
        self.status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        footer.addWidget(self.status, 1)
        footer.addSpacing(10)
        self.insert_hint = Hint("Ctrl Shift Enter", "Insert Fixed", self.insert, primary=True)
        footer.addWidget(self.insert_hint)

        root = vbox()
        self.setLayout(root)
        root.addWidget(top_w)
        root.addWidget(divider())
        root.addWidget(body, 1)
        root.addWidget(divider())
        root.addWidget(footer_w)

        self.toast = label("", wrap=False)
        self.toast.setObjectName("toast")
        self.toast.setParent(self)
        self.toast.hide()
        self.toast_timer = QTimer(self, singleShot=True, timeout=self.toast.hide)
        self.ticker = QTimer(self, interval=80, timeout=self.tick)

        self.show_results(False)
        # Window-wide keys, seen before the focused widget handles them (GTK: a CAPTURE-phase controller).
        self.keys = KeyFilter(self)
        QApplication.instance().installEventFilter(self.keys)
        self.input.setFocus()

    # ---- keys -------------------------------------------------------------

    def on_key(self, event):
        key, mods = event.key(), event.modifiers()
        ctrl, shift, alt = (bool(mods & m) for m in (Qt.ControlModifier, Qt.ShiftModifier, Qt.AltModifier))
        enter = key in (Qt.Key_Return, Qt.Key_Enter)
        if key == Qt.Key_Escape:
            self.close()
        elif enter and ctrl and shift:
            self.insert()
        elif enter and ctrl:
            self.analyze()
        elif enter and alt:
            self.use_as_draft(self.choice)
        elif alt and key in (Qt.Key_1, Qt.Key_2, Qt.Key_3):
            self.pick(self.VERSIONS[key - Qt.Key_1][0])
        else:
            return False
        return True

    # ---- analysis ---------------------------------------------------------

    def draft(self):
        return self.input.toPlainText().strip()

    def set_status(self, text=""):
        self.status.setText(text)

    def start_progress(self):
        self.step = 0
        self.fraction = 0.0
        self.bar.setValue(0)
        self.progress.setVisible(True)
        self.chat_intro.setVisible(False)
        self.meaning_w.setVisible(False)
        self.show_steps()
        self.ticker.start()

    def set_stage(self, stage):
        for i, (_, stages) in enumerate(self.STEPS):
            if stage in stages and i > self.step:
                self.step = i
                self.show_steps()

    def show_steps(self):
        clear(self.steps)
        for i, (name, _) in enumerate(self.STEPS):
            mark, css = (("✓", "step-done") if i < self.step else ("●", "step-active") if i == self.step
                         else ("○", "step-todo"))
            self.steps.addWidget(label(f"{mark}  {name}", css, wrap=False))

    def tick(self):
        # Ease toward the end of the current step; the model gives no finer progress than that.
        target = (self.step + 0.9) / len(self.STEPS)
        self.fraction += (target - self.fraction) * 0.04
        self.bar.setValue(round(self.fraction * 1000))

    def stop_progress(self, ok):
        self.ticker.stop()
        if ok:
            self.step = len(self.STEPS)
            self.bar.setValue(1000)
            self.show_steps()
            QTimer.singleShot(600, self.show_meaning)
        else:
            self.progress.setVisible(False)
            self.chat_intro.setVisible(True)

    def show_meaning(self):
        if self.proc:  # a new check started meanwhile
            return
        self.progress.setVisible(False)
        self.meaning_w.setVisible(True)
        self.chat_intro.setVisible(True)

    def show_results(self, shown):
        self.left_empty.setVisible(not shown)

    def stop(self):
        if self.proc:
            proc, self.proc = self.proc, None
            proc.kill()
            proc.waitForFinished(2000)

    def stop_asks(self):
        # Take the list first: waitForFinished runs on_answer, which must find nothing to update.
        asks, self.asks = self.asks, []
        for proc in asks:
            proc.kill()
            proc.waitForFinished(2000)

    def process(self, args):
        """A backend process with UTF-8 IO on every platform (Windows defaults to the ANSI code page)."""
        proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        proc.setProcessEnvironment(env)
        proc.setProgram(self.backend[0])
        proc.setArguments(self.backend[1:] + args)
        return proc

    def analyze(self):
        text = self.draft()
        if not text:
            self.show_toast("Type a message first")
            return
        self.stop()
        self.stop_asks()  # their answers belong to the chat cleared below
        self.analysis = None
        self.history = []
        clear(self.chat)
        self.chat_intro.setVisible(True)
        self.started = time.monotonic()
        self.set_status("Checking…")
        self.left_empty.setText("Versions appear here as soon as they are ready.")
        self.start_progress()
        proc = self.process(["--json", "--stream"])
        self.proc = proc
        buf = bytearray()
        proc.readyReadStandardOutput.connect(lambda: self.on_output(proc, buf))
        proc.finished.connect(lambda code, status: self.on_exit(proc, buf, code, status))
        proc.errorOccurred.connect(lambda err: self.on_error(proc, err))
        proc.start()
        proc.write(text.encode("utf-8"))
        proc.closeWriteChannel()

    def on_output(self, proc, buf):
        if proc is not self.proc:
            return  # replaced or closed
        buf += bytes(proc.readAllStandardOutput())
        while (nl := buf.find(b"\n")) >= 0:
            line = bytes(buf[:nl]).decode("utf-8", errors="replace")
            del buf[:nl + 1]
            self.on_line(line)

    def on_line(self, line):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(obj, dict):
            return
        if "stage" in obj:
            self.set_stage(obj["stage"])
        else:
            self.analysis = obj
            self.render()

    def on_exit(self, proc, buf, code, status):
        if proc is not self.proc:
            return
        self.on_output(proc, buf)
        if buf.strip():
            self.on_line(bytes(buf).decode("utf-8", errors="replace"))
        self.proc = None
        if status == QProcess.NormalExit and code == 0 and self.analysis and self.analysis.get("done"):
            self.set_status(f"Checked in {self.analysis.get('ms', 0) / 1000:.1f} s")
            self.stop_progress(True)
            self.render()
            self.checked.emit(True)
            return
        err = bytes(proc.readAllStandardError()).decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
        self.fail(re.sub(r"^saywell \[\w+\]: ", "", err) or "the check failed")

    def on_error(self, proc, error):
        if proc is self.proc and error == QProcess.FailedToStart:
            self.proc = None
            self.fail(f"cannot start the backend: {' '.join(self.backend)}")

    def fail(self, message):
        self.stop_progress(False)
        self.set_status("Check failed")
        self.show_toast(message[:120], 6000)
        self.checked.emit(False)

    # ---- rendering --------------------------------------------------------

    def render(self):
        a = self.analysis
        done = a.get("done")
        if not done:
            self.set_status(f"Checking… {time.monotonic() - self.started:.0f} s")
        self.show_results(True)
        self.render_versions(a)
        if done:
            self.render_meaning(a)

    def render_versions(self, a):
        clear(self.versions)
        self.cards = {}
        texts = {"fixed": (a.get("fixed"), a.get("fixed_zh")),
                 "improved": (a.get("improved"), a.get("improved_zh")),
                 "mine": (self.draft(), None)}
        for key, name, keys in self.VERSIONS:
            text, zh = texts[key]
            if not text:
                continue
            card = Card(key, self.pick, self.use_as_draft)
            col = vbox(6)
            col.setContentsMargins(14, 12, 14, 12)
            card.setLayout(col)
            head = QHBoxLayout()
            head.addWidget(label(name, "eyebrow", wrap=False))
            head.addStretch(1)
            head.addWidget(label(keys, "kbd", wrap=False))
            col.addLayout(head)
            if key == "mine":
                markup, changes = pre(html.escape(text)), []
            else:
                markup, changes = highlight_html(a.get("original", ""), text)
            body = label(markup, "version-text", rich=True)
            body.setObjectName("version-text")
            col.addWidget(body)
            if changes:
                card.setToolTip("Changes:\n" + "\n".join(changes))
            if zh:
                col.addWidget(label(f"含义  {zh}", "muted"))
            if key == "improved" and a.get("why"):
                col.addWidget(label(f"为什么  {a['why']}", "dim"))
            self.versions.addWidget(card)
            self.cards[key] = card
        self.show_choice()

    def render_meaning(self, a):
        clear(self.meaning)
        flags = a.get("meaning_flags") or []
        if flags:
            card = QFrame(objectName="warncard")
            col = vbox(4)
            col.setContentsMargins(14, 10, 14, 10)
            card.setLayout(col)
            tone_only = all(f.startswith("（语气）") for f in flags)
            col.addWidget(label("◐  TONE CHANGED" if tone_only else "⚠  MEANING MAY HAVE CHANGED", "eyebrow"))
            for f in flags:
                col.addWidget(label(f"• {f}"))
            self.meaning.addWidget(card)
        else:
            self.meaning.addWidget(label("✓  Meaning unchanged — still compare the 含义 lines with what you meant",
                                         "ok-line"))

    def show_choice(self):
        """Mark the picked card; like a list selection, it falls back to nothing if that card is absent."""
        for key, card in getattr(self, "cards", {}).items():
            card.setProperty("selected", "true" if key == self.choice else "false")
            card.style().unpolish(card)
            card.style().polish(card)
        self.insert_hint.word.setText(f"Insert {self.NAMES[self.choice]}")

    def pick(self, key):
        if key in getattr(self, "cards", {}):
            self.choice = key
            self.show_choice()
            self.left_scroll.ensureWidgetVisible(self.cards[key])

    def use_as_draft(self, key):
        """Put a version into the draft box so the user can keep writing from it; the window stays open."""
        if not self.analysis or key == "mine":
            self.input.setFocus()
            return
        text = self.analysis.get(key) or ""
        if not text:
            return
        self.input.setPlainText(text)
        self.input.moveCursor(QTextCursor.End)
        self.input.setFocus()
        self.show_toast(f"{self.NAMES[key]} version moved to the draft · Ctrl+Enter to check again", 3000)

    # ---- follow-up questions ---------------------------------------------

    def on_ask(self):
        question = self.ask.text().strip()
        if not question:
            return
        if not self.analysis or not self.analysis.get("done"):
            self.show_toast("Check the draft first")
            return
        self.ask.clear()
        context = {**self.analysis, "history": [{"q": qa["q"], "a": qa["a"]} for qa in self.history if qa["a"]]}
        bubble = label(question, "bubble-me", selectable=True)
        bubble.setMaximumWidth(360)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(bubble)
        self.chat.addLayout(row)
        answer = label("Thinking…", "dim", selectable=True)
        answer.setObjectName("answer")
        self.chat.addWidget(answer)
        qa = {"q": question, "a": "", "label": answer}
        self.history.append(qa)
        self.scroll_chat()
        proc = self.process(["--ask", question])
        self.asks.append(proc)
        proc.finished.connect(lambda code, status: self.on_answer(proc, qa, code == 0 and
                                                                  status == QProcess.NormalExit))
        proc.errorOccurred.connect(lambda err: err == QProcess.FailedToStart and self.on_answer(proc, qa, False))
        proc.start()
        proc.write(json.dumps(context, ensure_ascii=False).encode("utf-8"))
        proc.closeWriteChannel()

    def on_answer(self, proc, qa, ok):
        if proc not in self.asks:
            return
        self.asks.remove(proc)
        out = bytes(proc.readAllStandardOutput() if ok else proc.readAllStandardError())
        qa["a"] = out.decode("utf-8", errors="replace").replace("\r\n", "\n").strip() or ("" if ok else proc.errorString())
        if qa in self.history:
            qa["label"].setProperty("css", "answer")
            qa["label"].style().unpolish(qa["label"])
            qa["label"].style().polish(qa["label"])
            qa["label"].setTextFormat(Qt.RichText)
            qa["label"].setText(md_html(qa["a"]) if ok else pre(html.escape(f"Error: {qa['a']}")))
            self.scroll_chat()
        if not ok:
            qa["a"] = ""  # errors are not sent back as history
        self.answered.emit()

    def scroll_chat(self):
        bar = self.right_scroll.verticalScrollBar()
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    # ---- insert -----------------------------------------------------------

    def insert(self):
        if self.choice == "mine" or not self.analysis:
            text = self.draft()
        else:
            text = self.analysis.get(self.choice) or self.analysis.get("fixed") or ""
        if not text:
            self.show_toast("Nothing to insert")
            return
        QGuiApplication.clipboard().setText(text)
        self.inserted.emit(text)
        self.close()

    # ---- window -----------------------------------------------------------

    def show_toast(self, text, ms=2000):
        self.toast.setText(text)
        self.toast.adjustSize()
        self.toast.move((self.width() - self.toast.width()) // 2, self.height() - self.toast.height() - 56)
        self.toast.raise_()
        self.toast.show()
        self.toast_timer.start(ms)

    def closeEvent(self, event):
        self.stop()
        self.stop_asks()
        QApplication.instance().removeEventFilter(self.keys)
        super().closeEvent(event)


class KeyFilter(QObject):
    """Hands key presses aimed at the popup (or its children) to Popup.on_key first."""

    def __init__(self, popup):
        super().__init__(popup)
        self.popup = popup

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and isinstance(obj, QWidget) and obj.window() is self.popup:
            return self.popup.on_key(event)
        return False


def scroll(widget):
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    area.setFrameShape(QFrame.NoFrame)
    return area


def divider():
    d = QFrame(objectName="divider")
    d.setFixedHeight(1)
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(description="The saywell popup (PySide6).")
    ap.add_argument("--backend", help="path to the saywell backend (default: $SAYWELL_BACKEND or the repo copy)")
    args, qt_args = ap.parse_known_args(argv)
    app = QApplication.instance() or QApplication([sys.argv[0]] + qt_args)
    popup = Popup(backend_command(args.backend))
    # The system integration stage pastes this into the previous window; standalone, the clipboard has it.
    popup.inserted.connect(lambda text: print(f"saywell-popup: inserted {len(text)} chars (on the clipboard)",
                                              file=sys.stderr))
    popup.show()
    popup.activateWindow()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
