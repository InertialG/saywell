"""Drive the PySide6 popup with a fake backend, headless.

Run: uv run --no-project --with pyside6-essentials python -m unittest discover -s tests -p "test_popup_qt*.py"
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).resolve().parent / "fake_saywell.py"
sys.path.insert(0, str(ROOT / "windows"))

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLabel
    import saywell_popup_qt as popup_qt
except ImportError as e:  # PySide6 is not installed in the plain interpreter
    popup_qt = None
    SKIP = f"PySide6 not available: {e}"
else:
    SKIP = ""
    APP = QApplication.instance() or QApplication([])

DRAFT = "is there a app can fix my input? i dont want to 打断 the agent"


def wait_for(cond, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the popup")


def texts(widget):
    return [w.text() for w in widget.findChildren(QLabel)]


@unittest.skipIf(SKIP, SKIP)
class PopupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.log = self.tmp / "calls.jsonl"
        env = {"XDG_CONFIG_HOME": str(self.tmp / "config"), "XDG_DATA_HOME": str(self.tmp / "data"),
               "XDG_CACHE_HOME": str(self.tmp / "cache"), "FAKE_SAYWELL_LOG": str(self.log),
               "FAKE_SAYWELL_SCENARIO": "ok"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.popup = popup_qt.Popup([sys.executable, str(FAKE)])
        self.addCleanup(self.popup.close)
        self.results = []
        self.popup.checked.connect(self.results.append)
        self.answers = []
        self.popup.answered.connect(lambda: self.answers.append(1))
        self.inserted = []
        self.popup.inserted.connect(self.inserted.append)
        self.popup.show()
        QApplication.processEvents()

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def check(self, scenario="ok", draft=DRAFT):
        os.environ["FAKE_SAYWELL_SCENARIO"] = scenario
        self.popup.input.setPlainText(draft)
        QTest.keyClick(self.popup.input, Qt.Key_Return, Qt.ControlModifier)
        wait_for(lambda: self.results)
        return self.results[-1]

    def wait_meaning(self):
        wait_for(lambda: self.popup.meaning_w.isVisible())

    # ---- full flow --------------------------------------------------------

    def test_full_flow(self):
        renders = []
        render = self.popup.render
        self.popup.render = lambda: (renders.append(dict(self.popup.analysis)), render())
        self.assertTrue(self.check())
        # The draft went to the backend as UTF-8, Chinese included, with the stream protocol.
        call = self.calls()[0]
        self.assertEqual(call["argv"], ["--json", "--stream"])
        self.assertEqual(call["stdin"], DRAFT)
        # Partial versions were shown before the final one.
        self.assertFalse(renders[0]["done"])
        self.assertTrue(renders[-1]["done"])
        self.assertEqual(list(self.popup.cards), ["fixed", "improved", "mine"])
        self.assertEqual(self.popup.status.text(), "Checked in 1.2 s")
        self.assertEqual(texts(self.popup.progress)[1:], ["✓  Fixing grammar", "✓  Improving wording",
                                                          "✓  Translating back · 含义", "✓  Checking meaning"])
        self.assertEqual(self.popup.bar.value(), 1000)
        self.wait_meaning()
        self.assertFalse(self.popup.progress.isVisible())
        self.assertTrue(self.popup.chat_intro.isVisible())
        self.assertFalse(self.popup.left_empty.isVisible())
        self.assertIn("✓  Meaning unchanged", texts(self.popup.meaning_w)[0])
        self.assertEqual(self.popup.insert_hint.word.text(), "Insert Fixed")

    def test_highlight_and_tooltip(self):
        self.check()
        fixed = texts(self.popup.cards["fixed"])
        body = next(t for t in fixed if "background-color" in t)
        self.assertIn('<span style="background-color:#233c5d; color:#ffffff;">an</span>', body)
        self.assertIn('<span style="background-color:#233c5d; color:#ffffff;">interrupt</span>', body)
        tip = self.popup.cards["fixed"].toolTip()
        self.assertTrue(tip.startswith("Changes:\n"))
        self.assertIn("a → an", tip)
        self.assertIn("打断 → interrupt", tip)
        # My draft is shown as typed, without highlights or a tooltip.
        mine = texts(self.popup.cards["mine"])
        self.assertFalse(any("background-color" in t for t in mine))
        self.assertEqual(self.popup.cards["mine"].toolTip(), "")

    def test_back_translation_and_why(self):
        self.check()
        self.assertIn("含义  有没有一个应用可以修正我的输入？我不想打断智能体。", texts(self.popup.cards["fixed"]))
        improved = texts(self.popup.cards["improved"])
        self.assertIn("含义  有没有一个应用可以在不打断智能体的情况下修正我的输入？", improved)
        self.assertIn("为什么  improved 用 without 合并两句，更简洁。", improved)
        self.assertFalse(any(t.startswith("含义") for t in texts(self.popup.cards["mine"])))

    def test_progress_steps(self):
        self.popup.start_progress()
        self.popup.set_stage("why")
        self.assertEqual(texts(self.popup.progress)[1:], ["✓  Fixing grammar", "●  Improving wording",
                                                          "○  Translating back · 含义", "○  Checking meaning"])
        self.popup.set_stage("fixed")  # never steps back
        self.assertEqual(self.popup.step, 1)
        for _ in range(50):
            self.popup.tick()
        self.assertGreater(self.popup.bar.value(), 0)
        self.assertLess(self.popup.bar.value(), 1000 * 1.9 / 4)
        self.popup.ticker.stop()

    # ---- meaning check ----------------------------------------------------

    def test_meaning_changed(self):
        self.check("flags")
        self.wait_meaning()
        shown = texts(self.popup.meaning_w)
        self.assertEqual(shown[0], "⚠  MEANING MAY HAVE CHANGED")
        self.assertEqual(shown[1], "• “打断” 被译成 “interrupt”，读者会理解为中途停止")

    def test_tone_changed(self):
        self.check("tone")
        self.wait_meaning()
        self.assertEqual(texts(self.popup.meaning_w)[:2], ["◐  TONE CHANGED", "• （语气）“pls” 变成 “please”，更正式"])

    # ---- failures ---------------------------------------------------------

    def test_check_failed(self):
        self.assertFalse(self.check("fail"))
        self.assertEqual(self.popup.status.text(), "Check failed")
        self.assertEqual(self.popup.toast.text(), "claude timed out")
        self.assertFalse(self.popup.progress.isVisible())
        self.assertTrue(self.popup.chat_intro.isVisible())

    def test_backend_missing(self):
        self.popup.backend = [str(self.tmp / "no-such-saywell.exe")]
        self.assertFalse(self.check())
        self.assertIn("cannot start the backend", self.popup.toast.text())

    def test_empty_draft(self):
        QTest.keyClick(self.popup.input, Qt.Key_Return, Qt.ControlModifier)
        self.assertEqual(self.popup.toast.text(), "Type a message first")
        self.assertFalse(self.log.exists())

    # ---- follow-up chat ---------------------------------------------------

    def test_follow_up(self):
        self.check()
        self.popup.ask.setText("why interrupt?")
        QTest.keyClick(self.popup.ask, Qt.Key_Return)
        self.assertIn("Thinking…", texts(self.popup.right_scroll))
        wait_for(lambda: self.answers)
        answer = self.popup.history[0]["label"].text()
        self.assertIn("<b>why interrupt?</b>", answer)
        self.assertIn("<code>interrupt</code>", answer)
        self.assertIn("(history 0)", answer)
        self.assertNotIn("\r", self.popup.history[0]["a"])  # Windows text-mode stdout writes \r\n
        ask = self.calls()[-1]
        self.assertEqual(ask["argv"], ["--ask", "why interrupt?"])
        context = json.loads(ask["stdin"])
        self.assertEqual(context["fixed"], self.popup.analysis["fixed"])
        self.assertEqual(context["history"], [])
        # The second question carries the first answer as history.
        self.popup.ask.setText("太正式吗？")
        QTest.keyClick(self.popup.ask, Qt.Key_Return)
        wait_for(lambda: len(self.answers) == 2)
        self.assertIn("(history 1)", self.popup.history[1]["label"].text())
        self.assertEqual(json.loads(self.calls()[-1]["stdin"])["history"][0]["q"], "why interrupt?")
        self.assertIn("why interrupt?", texts(self.popup.right_scroll))

    def test_ask_before_check(self):
        self.popup.ask.setText("why?")
        QTest.keyClick(self.popup.ask, Qt.Key_Return)
        self.assertEqual(self.popup.toast.text(), "Check the draft first")
        self.assertEqual(self.popup.ask.text(), "why?")

    def test_new_check_clears_chat(self):
        self.check()
        self.popup.ask.setText("why?")
        QTest.keyClick(self.popup.ask, Qt.Key_Return)
        wait_for(lambda: self.answers)
        self.check()
        self.assertEqual(self.popup.history, [])
        self.assertNotIn("why?", texts(self.popup.right_scroll))

    # ---- keys -------------------------------------------------------------

    def test_pick_keys(self):
        self.check()
        for key, choice, word in ((Qt.Key_2, "improved", "Insert Improved"), (Qt.Key_3, "mine", "Insert My draft"),
                                  (Qt.Key_1, "fixed", "Insert Fixed")):
            QTest.keyClick(self.popup.ask, key, Qt.AltModifier)  # works from any focused widget
            self.assertEqual(self.popup.choice, choice)
            self.assertEqual(self.popup.insert_hint.word.text(), word)
            self.assertEqual(self.popup.cards[choice].property("selected"), "true")

    def test_click_and_double_click(self):
        self.check()
        card = self.popup.cards["improved"]
        QTest.mouseClick(card, Qt.LeftButton)
        self.assertEqual(self.popup.choice, "improved")
        QTest.mouseDClick(self.popup.cards["fixed"], Qt.LeftButton)
        self.assertEqual(self.popup.input.toPlainText(), self.popup.analysis["fixed"])

    def test_edit_in_draft(self):
        self.check()
        QTest.keyClick(self.popup.input, Qt.Key_2, Qt.AltModifier)
        QTest.keyClick(self.popup.input, Qt.Key_Return, Qt.AltModifier)
        self.assertEqual(self.popup.input.toPlainText(), self.popup.analysis["improved"])
        self.assertTrue(self.popup.toast.text().startswith("Improved version moved to the draft"))
        self.assertTrue(self.popup.isVisible())

    def test_insert(self):
        self.check()
        QTest.keyClick(self.popup.input, Qt.Key_2, Qt.AltModifier)
        QTest.keyClick(self.popup.input, Qt.Key_Enter, Qt.ControlModifier | Qt.ShiftModifier | Qt.KeypadModifier)
        self.assertEqual(self.inserted, [self.popup.analysis["improved"]])
        self.assertEqual(QGuiApplication.clipboard().text(), self.popup.analysis["improved"])
        self.assertFalse(self.popup.isVisible())

    def test_insert_my_draft_without_check(self):
        self.popup.input.setPlainText("  my own words  ")
        QTest.keyClick(self.popup.input, Qt.Key_Return, Qt.ControlModifier | Qt.ShiftModifier)
        self.assertEqual(self.inserted, ["my own words"])

    def test_nothing_to_insert(self):
        QTest.keyClick(self.popup.input, Qt.Key_Return, Qt.ControlModifier | Qt.ShiftModifier)
        self.assertEqual(self.inserted, [])
        self.assertEqual(self.popup.toast.text(), "Nothing to insert")
        self.assertTrue(self.popup.isVisible())

    def test_escape_closes_and_stops_backend(self):
        os.environ["FAKE_SAYWELL_SCENARIO"] = "ok"
        self.popup.input.setPlainText(DRAFT)
        self.popup.analyze()
        proc = self.popup.proc
        QTest.keyClick(self.popup.ask, Qt.Key_Escape)
        self.assertFalse(self.popup.isVisible())
        self.assertIsNone(self.popup.proc)
        self.assertEqual(proc.state(), proc.ProcessState.NotRunning)
        self.assertEqual(self.inserted, [])

    def test_plain_enter_in_draft_is_a_newline(self):
        self.popup.input.setPlainText("line one")
        self.popup.input.moveCursor(popup_qt.QTextCursor.End)
        QTest.keyClick(self.popup.input, Qt.Key_Return)
        self.assertEqual(self.popup.input.toPlainText(), "line one\n")
        self.assertFalse(self.log.exists())


@unittest.skipIf(SKIP, SKIP)
class HelpersTest(unittest.TestCase):
    def test_highlight_html(self):
        markup, changes = popup_qt.highlight_html("i dont want <b>", "I don't want <b>")
        self.assertEqual(changes, ["i → I", "dont → don't"])
        self.assertIn("&lt;b&gt;", markup)
        self.assertTrue(markup.startswith('<div style="white-space:pre-wrap;">'))

    def test_highlight_deletion(self):
        _, changes = popup_qt.highlight_html("fix the the bug", "fix the bug")
        self.assertEqual(changes, ["the → ∅"])

    def test_md_html(self):
        self.assertIn("<b>a</b> <code>x &lt; y</code>", popup_qt.md_html("**a** `x < y`"))

    def test_backend_command(self):
        self.assertEqual(popup_qt.backend_command("C:/bin/saywell.exe"), ["C:/bin/saywell.exe"])
        self.assertEqual(popup_qt.backend_command("/repo/saywell"), [sys.executable, "/repo/saywell"])
        with mock.patch.dict(os.environ, {"SAYWELL_BACKEND": "/x/saywell"}):
            self.assertEqual(popup_qt.backend_command(), [sys.executable, "/x/saywell"])
        with mock.patch.dict(os.environ, {"SAYWELL_BACKEND": ""}):
            self.assertEqual(popup_qt.backend_command(), [sys.executable, str(ROOT / "saywell")])


if __name__ == "__main__":
    unittest.main()
