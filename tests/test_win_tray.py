"""The Windows tray (windows/saywell_tray.py) headless, against a fake Win32 layer: hotkey
registration and its failure, popup lifecycle, focus restore and paste keys, the learn timer,
single instance, the autostart menu. Registers no real hotkey, never touches the registry,
sends no real keys.

Run: uv run --no-project --with pyside6-essentials python -m unittest discover -s tests -p "test_win_*.py"
"""

import ctypes
import os
import sys
import tempfile
import time
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).resolve().parent / "fake_saywell.py"
sys.path.insert(0, str(ROOT / "windows"))
_TMP = tempfile.TemporaryDirectory()
os.environ.update({"XDG_CONFIG_HOME": str(Path(_TMP.name) / "config"),
                   "XDG_DATA_HOME": str(Path(_TMP.name) / "data"),
                   "XDG_CACHE_HOME": str(Path(_TMP.name) / "cache")})

try:
    from PySide6.QtCore import QByteArray, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    import saywell_tray as tray_mod
    import saywell_win as sw
    import win32
except ImportError as e:
    tray_mod = None
    SKIP = f"PySide6 not available: {e}"
else:
    SKIP = ""
    APP = QApplication.instance() or QApplication([])
    APP.setQuitOnLastWindowClosed(False)

TERMINAL, EDITOR, DESKTOP = 0x100, 0x200, 0x300


def tearDownModule():
    _TMP.cleanup()


def wait_for(cond, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return
        time.sleep(0.005)
    raise AssertionError("timed out")


class FakeWin:
    """Records what the tray asks of Win32; windows are {hwnd: (class, exe, pid)}."""

    def __init__(self):
        self.windows = {TERMINAL: ("CASCADIA_HOSTING_WINDOW_CLASS", "windowsterminal.exe", 11),
                        EDITOR: ("Notepad", "notepad.exe", 12),
                        DESKTOP: ("WorkerW", "explorer.exe", 13)}
        self.foreground = EDITOR
        self.hotkey_error = 0
        self.hotkeys = {}
        self.focused = []
        self.focus_ok = True
        self.held = []          # successive keys_down results; the last one repeats
        self.sent = []
        self.mutex_exists = False
        self.mutexes = []
        self.run = {}

    def register_hotkey(self, hotkey_id, mods, vk):
        if not self.hotkey_error:
            self.hotkeys[hotkey_id] = (mods, vk)
        return self.hotkey_error

    def unregister_hotkey(self, hotkey_id):
        self.hotkeys.pop(hotkey_id, None)

    def foreground_window(self):
        return self.foreground

    def is_window(self, hwnd):
        return hwnd in self.windows

    def window_class(self, hwnd):
        return self.windows.get(hwnd, ("", "", 0))[0]

    def window_process(self, hwnd):
        return self.windows.get(hwnd, ("", "", 0))[1]

    def window_pid(self, hwnd):
        return self.windows.get(hwnd, ("", "", os.getpid()))[2]

    def focus_window(self, hwnd):
        self.focused.append(hwnd)
        if self.focus_ok:
            self.foreground = hwnd
        return self.focus_ok

    def keys_down(self, vks):
        held = self.held.pop(0) if len(self.held) > 1 else (self.held[0] if self.held else [])
        return [vk for vk in held if vk in vks]

    def send_keys(self, events):
        self.sent.append(list(events))
        return len(events)

    def create_mutex(self, name):
        self.mutexes.append(name)
        return 42, self.mutex_exists

    def run_value(self, name):
        return self.run.get(name)

    def set_run_value(self, name, command):
        self.run[name] = command

    def delete_run_value(self, name):
        self.run.pop(name, None)


@unittest.skipIf(SKIP, SKIP)
class TrayTest(unittest.TestCase):
    def setUp(self):
        self.win = FakeWin()
        self.settings = dict(sw.DEFAULTS)
        env = mock.patch.dict(os.environ, {"FAKE_SAYWELL_SCENARIO": "ok"})
        env.start()
        self.addCleanup(env.stop)
        self.warnings = []
        warn = mock.patch.object(tray_mod.Tray, "warn", lambda _, m: self.warnings.append(m))
        warn.start()
        self.addCleanup(warn.stop)

    def make_tray(self):
        tray = tray_mod.Tray(self.win, self.settings, [sys.executable, str(FAKE)])
        self.addCleanup(tray.deleteLater)
        self.addCleanup(tray.stop)
        self.pasted = []
        tray.paster.done.connect(lambda ok, msg: self.pasted.append((ok, msg)))
        tray.start()
        return tray

    # ---- hotkey -------------------------------------------------------------

    def test_registers_default_hotkey(self):
        tray = self.make_tray()
        self.assertEqual(self.win.hotkeys, {tray_mod.HOTKEY_ID: (win32.MOD_WIN | win32.MOD_ALT | win32.MOD_NOREPEAT,
                                                                 ord("E"))})
        self.assertEqual(self.warnings, [])
        tray.stop()
        self.assertEqual(self.win.hotkeys, {})

    def test_configured_hotkey(self):
        self.settings["hotkey"] = "Ctrl+Shift+F9"
        self.make_tray()
        self.assertEqual(self.win.hotkeys[tray_mod.HOTKEY_ID],
                         (win32.MOD_CONTROL | win32.MOD_SHIFT | win32.MOD_NOREPEAT, 0x78))

    def test_hotkey_taken_is_reported(self):
        self.win.hotkey_error = win32.ERROR_HOTKEY_ALREADY_REGISTERED
        tray = self.make_tray()
        self.assertFalse(tray.hotkey_ok)
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("Win+Alt+E could not be registered: another program already uses it", self.warnings[0])
        self.assertIn(str(sw.CONFIG_PATH), self.warnings[0])
        self.assertIn("unavailable", tray.icon.toolTip())
        # The popup still opens from the menu.
        tray.open_from_menu()
        self.assertTrue(tray.popup_open())

    def test_hotkey_message_reaches_the_callback(self):
        calls = []
        filt = tray_mod.HotkeyFilter(7, lambda: calls.append(1))
        hit = wintypes.MSG(None, win32.WM_HOTKEY, 7, 0)
        other_id = wintypes.MSG(None, win32.WM_HOTKEY, 8, 0)
        other_msg = wintypes.MSG(None, 0x0100, 7, 0)
        kind = QByteArray(b"windows_dispatcher_MSG")
        self.assertEqual(filt.nativeEventFilter(kind, ctypes.addressof(hit)), (True, 0))
        self.assertEqual(filt.nativeEventFilter(kind, ctypes.addressof(other_id)), (False, 0))
        self.assertEqual(filt.nativeEventFilter(kind, ctypes.addressof(other_msg)), (False, 0))
        wait_for(lambda: calls)
        self.assertEqual(calls, [1])

    # ---- popup --------------------------------------------------------------

    def test_hotkey_opens_popup_for_the_foreground_window(self):
        tray = self.make_tray()
        tray.on_hotkey()
        self.assertEqual(tray.target, EDITOR)
        popup = tray.popup
        self.assertTrue(popup.isVisible())
        flags = popup.windowFlags()
        self.assertTrue(flags & Qt.FramelessWindowHint)
        self.assertTrue(flags & Qt.WindowStaysOnTopHint)
        self.assertEqual(self.win.focused, [int(popup.winId())])
        # The offscreen screen is 800x800: centered vertically; wider than the screen, so at its left edge.
        area, geo = QGuiApplication.primaryScreen().availableGeometry(), popup.frameGeometry()
        self.assertLessEqual(abs(geo.center().y() - area.center().y()), 1)
        if geo.width() > area.width():
            self.assertEqual(geo.left(), area.left())
        else:
            self.assertLessEqual(abs(geo.center().x() - area.center().x()), 1)

    def test_second_press_raises_the_same_popup(self):
        tray = self.make_tray()
        tray.on_hotkey()
        popup = tray.popup
        popup.input.setPlainText("keep me")
        tray.on_hotkey()  # from the popup itself: same popup, same target
        self.assertIs(tray.popup, popup)
        self.assertEqual(tray.target, EDITOR)
        self.win.foreground = DESKTOP
        tray.on_hotkey()
        self.assertEqual(tray.target, EDITOR)
        self.win.foreground = TERMINAL  # the user went to the terminal and pressed it there
        tray.on_hotkey()
        self.assertIs(tray.popup, popup)
        self.assertEqual(popup.input.toPlainText(), "keep me")
        self.assertEqual(tray.target, TERMINAL)
        self.assertEqual(self.win.focused[-1], int(popup.winId()))

    def test_reopen_after_close_is_fresh(self):
        tray = self.make_tray()
        tray.on_hotkey()
        tray.popup.input.setPlainText("old")
        tray.popup.close()
        self.win.foreground = TERMINAL
        tray.on_hotkey()
        self.assertEqual(tray.popup.input.toPlainText(), "")
        self.assertEqual(tray.target, TERMINAL)

    def test_no_target_for_desktop_or_own_window(self):
        tray = self.make_tray()
        for fg in (DESKTOP, 0, 0x999):  # 0x999: unknown to FakeWin, so it has our pid
            self.win.foreground = fg
            tray.on_hotkey()
            self.assertEqual(tray.target, 0, hex(fg))
            tray.popup.close()

    def test_closing_the_popup_keeps_the_tray(self):
        tray = self.make_tray()
        tray.on_hotkey()
        QTest.keyClick(tray.popup.input, Qt.Key_Escape)
        QApplication.processEvents()
        self.assertFalse(tray.popup_open())
        self.assertTrue(tray.icon.isVisible() or not tray.icon.isSystemTrayAvailable())
        self.assertEqual(self.win.sent, [])

    # ---- insert, focus, paste -----------------------------------------------

    def insert(self, tray, text="Hello there."):
        tray.popup.input.setPlainText(text)
        QTest.keyClick(tray.popup.input, Qt.Key_Return, Qt.ControlModifier | Qt.ShiftModifier)  # insert my draft
        self.assertEqual(QGuiApplication.clipboard().text(), text)
        wait_for(lambda: self.pasted)
        return self.pasted[-1]

    def test_paste_into_editor_with_ctrl_v(self):
        tray = self.make_tray()
        self.win.foreground = EDITOR
        tray.on_hotkey()
        self.assertEqual(self.win.foreground, int(tray.popup.winId()))
        self.assertEqual(self.insert(tray), (True, "Ctrl+V"))
        self.assertFalse(tray.popup_open())
        self.assertEqual(self.win.focused[-1], EDITOR)
        self.assertEqual(self.win.sent, [sw.key_events("Ctrl+V")])
        self.assertEqual(tray.target, 0)

    def test_paste_into_terminal_with_ctrl_shift_v(self):
        tray = self.make_tray()
        self.win.foreground = TERMINAL
        tray.on_hotkey()
        self.assertEqual(self.insert(tray), (True, "Ctrl+Shift+V"))
        self.assertEqual(self.win.sent, [sw.key_events("Ctrl+Shift+V")])

    def test_waits_for_modifiers_to_be_released(self):
        tray = self.make_tray()
        tray.on_hotkey()
        C, S = win32.VK_CONTROL, win32.VK_SHIFT
        self.win.held = [[C, S], [C, S], [C], []]  # the user lets go of Ctrl+Shift+Enter
        self.assertEqual(self.insert(tray), (True, "Ctrl+V"))
        self.assertEqual(self.win.held, [[]])
        self.assertEqual(self.win.sent, [sw.key_events("Ctrl+V")])  # no extra key-ups

    def test_lifts_modifiers_still_held(self):
        tray = self.make_tray()
        tray.on_hotkey()
        tray.paster.WAIT_S = 0.05
        self.win.held = [[win32.VK_SHIFT, win32.VK_MENU]]
        self.assertEqual(self.insert(tray), (True, "Ctrl+V"))
        lift = [(win32.VK_MASK, False, False), (win32.VK_MASK, True, False),
                (win32.VK_SHIFT, True, False), (win32.VK_MENU, True, False)]
        self.assertEqual(self.win.sent, [lift, sw.key_events("Ctrl+V")])

    def test_overlapping_pastes_send_once_to_the_newest_target(self):
        # A second paste while the first still waits for the modifiers: the first must not send
        # anything (the clipboard holds the second text), the second sends once.
        tray = self.make_tray()
        released = time.monotonic() + 0.1
        self.win.keys_down = lambda vks: [win32.VK_CONTROL] if time.monotonic() < released else []
        tray.paster.paste(EDITOR)
        QTest.qWait(50)
        tray.paster.paste(TERMINAL)
        wait_for(lambda: self.pasted)
        QTest.qWait(300)  # let any stale step of the first paste run
        self.assertEqual(self.pasted, [(True, "Ctrl+Shift+V")])
        self.assertEqual(self.win.sent, [sw.key_events("Ctrl+Shift+V")])
        self.assertIsNone(tray.paster.job)

    def test_reopening_the_popup_cancels_a_pending_paste(self):
        tray = self.make_tray()
        tray.on_hotkey()
        self.win.held = [[win32.VK_CONTROL]]  # still holding Ctrl+Shift+Enter's Ctrl
        tray.popup.input.setPlainText("first")
        QTest.keyClick(tray.popup.input, Qt.Key_Return, Qt.ControlModifier | Qt.ShiftModifier)
        QTest.qWait(50)
        self.assertIsNotNone(tray.paster.job)
        self.win.foreground = TERMINAL
        tray.on_hotkey()  # back to the popup before the paste happened
        self.win.held = [[]]
        QTest.qWait(1300)  # past the first paste's deadline
        self.assertEqual(self.win.sent, [])
        self.assertEqual(self.pasted, [])
        self.assertEqual(tray.target, TERMINAL)

    def test_window_gone(self):
        tray = self.make_tray()
        tray.on_hotkey()
        del self.win.windows[EDITOR]
        ok, msg = self.insert(tray)
        self.assertFalse(ok)
        self.assertIn("gone", msg)
        self.assertEqual(self.win.sent, [])

    def test_focus_refused(self):
        tray = self.make_tray()
        tray.on_hotkey()
        self.win.focus_ok = False
        ok, msg = self.insert(tray)
        self.assertFalse(ok)
        self.assertIn("Could not switch back", msg)
        self.assertEqual(self.win.sent, [])

    def test_focus_moved_before_paste(self):
        tray = self.make_tray()
        tray.on_hotkey()
        real_keys_down = self.win.keys_down

        def keys_down(vks):
            self.win.foreground = TERMINAL  # the user switched windows while we waited
            return real_keys_down(vks)
        self.win.keys_down = keys_down
        ok, msg = self.insert(tray)
        self.assertFalse(ok)
        self.assertIn("Focus moved", msg)
        self.assertEqual(self.win.sent, [])

    def test_insert_without_target_only_copies(self):
        tray = self.make_tray()
        tray.open_from_menu()
        tray.popup.input.setPlainText("just copy")
        QTest.keyClick(tray.popup.input, Qt.Key_Return, Qt.ControlModifier | Qt.ShiftModifier)
        QTest.qWait(100)
        self.assertEqual(QGuiApplication.clipboard().text(), "just copy")
        self.assertEqual(self.pasted, [])
        self.assertEqual(self.win.sent, [])

    def test_full_check_then_insert(self):
        tray = self.make_tray()
        self.win.foreground = TERMINAL
        tray.on_hotkey()
        popup = tray.popup
        checked = []
        popup.checked.connect(checked.append)
        popup.input.setPlainText("is there a app can fix my input?")
        QTest.keyClick(popup.input, Qt.Key_Return, Qt.ControlModifier)
        wait_for(lambda: checked)
        QTest.keyClick(popup.input, Qt.Key_Return, Qt.ControlModifier | Qt.ShiftModifier)
        wait_for(lambda: self.pasted)
        self.assertEqual(QGuiApplication.clipboard().text(), popup.analysis["fixed"])
        self.assertEqual(self.pasted, [(True, "Ctrl+Shift+V")])

    # ---- autostart menu -----------------------------------------------------

    def test_autostart_toggle(self):
        tray = self.make_tray()
        self.assertFalse(tray.autostart_action.isChecked())
        with mock.patch.object(sw, "autostart_command", return_value='"pyw" "tray"'):
            tray.autostart_action.trigger()
        self.assertEqual(self.win.run, {"Saywell": '"pyw" "tray"'})
        self.assertTrue(tray.autostart_action.isChecked())
        tray.autostart_action.trigger()
        self.assertEqual(self.win.run, {})
        self.assertFalse(tray.autostart_action.isChecked())
        self.win.run["Saywell"] = "x"  # changed from the command line meanwhile
        tray.menu.aboutToShow.emit()
        self.assertTrue(tray.autostart_action.isChecked())

    def test_menu_entries(self):
        tray = self.make_tray()
        names = [a.text().split("\t")[0] for a in tray.menu.actions() if not a.isSeparator()]
        self.assertEqual(names, ["Open Saywell", "Learn now", "Start at logon", "Quit"])


@unittest.skipIf(SKIP, SKIP)
class LearnerTest(unittest.TestCase):
    def learner(self, script, interval=60):
        learner = tray_mod.Learner([sys.executable, "-c", script], interval)
        self.addCleanup(learner.deleteLater)
        self.addCleanup(learner.timer.stop)
        self.results = []
        learner.finished.connect(lambda *r: self.results.append(r))
        return learner

    def test_schedule(self):
        learner = self.learner("pass")
        self.assertTrue(learner.timer.isActive())
        self.assertEqual(learner.timer.interval(), 60 * 60_000)
        self.assertFalse(learner.timer.isSingleShot())
        self.assertFalse(self.learner("pass", 0).timer.isActive())
        self.assertEqual(self.learner("pass", 1.5).timer.interval(), 90_000)

    def test_timer_runs_learn(self):
        learner = self.learner("import sys; print('args', sys.argv[1:]); print('learned 3 notes')")
        with mock.patch.object(learner, "run", wraps=learner.run) as run:
            learner.timer.setInterval(10)
            wait_for(lambda: run.called)
        learner.timer.stop()
        wait_for(lambda: self.results)
        self.assertEqual(run.call_args, mock.call(False))
        self.assertEqual(self.results[0], (True, "learned 3 notes", False))

    def test_passes_learn_flag_and_utf8(self):
        learner = self.learner("import os, sys; print(sys.argv[1:], os.environ['PYTHONUTF8'], '知识点')")
        learner.run(True)
        wait_for(lambda: self.results)
        self.assertEqual(self.results[0], (True, "['--learn'] 1 知识点", True))

    def test_failure_is_logged_not_raised(self):
        learner = self.learner("import sys; sys.exit('saywell learn [claude]: claude timed out')")
        with self.assertLogs("saywell-tray", "WARNING") as logs:
            learner.run(False)
            wait_for(lambda: self.results)
        self.assertEqual(self.results[0], (False, "saywell learn [claude]: claude timed out", False))
        self.assertIn("learn failed: saywell learn [claude]: claude timed out", logs.output[0])

    def test_backend_missing(self):
        learner = tray_mod.Learner([str(Path(_TMP.name) / "nope.exe")], 60)
        self.addCleanup(learner.timer.stop)
        results = []
        learner.finished.connect(lambda *r: results.append(r))
        with self.assertLogs("saywell-tray", "WARNING"):
            learner.run(False)
            wait_for(lambda: results)
        self.assertFalse(results[0][0])
        self.assertIsNone(learner.proc)

    def backend_with_child(self, name):
        """A backend script that starts a child (as the backend starts claude.exe) which writes a
        marker file after 2 s, then waits a minute itself. Returns (script, marker, started)."""
        marker, started = Path(_TMP.name) / f"{name}-marker", Path(_TMP.name) / f"{name}-started"
        child = "import sys, time; time.sleep(2); open(sys.argv[1], 'w').write('child outlived the kill')"
        script = (f"import subprocess, sys, time\n"
                  f"subprocess.Popen([sys.executable, '-c', {child!r}, {str(marker)!r}])\n"
                  f"open({str(started)!r}, 'w').close()\n"
                  f"time.sleep(60)\n")
        return script, marker, started

    @unittest.skipUnless(sys.platform == "win32", "Windows process trees")
    def test_timeout_kills_the_whole_tree(self):
        script, marker, started = self.backend_with_child("timeout")
        learner = self.learner(script)
        learner.run(False)
        wait_for(started.exists)
        learner.on_timeout()
        wait_for(lambda: self.results)
        self.assertFalse(self.results[0][0])
        self.assertIsNone(learner.proc)
        time.sleep(3)
        self.assertFalse(marker.exists(), "the backend's child kept running after the timeout")
        self.assertTrue(learner.run(True))  # the slot is free again
        learner.kill()

    @unittest.skipUnless(sys.platform == "win32", "Windows process trees")
    def test_stop_on_quit_kills_the_whole_tree(self):
        script, marker, started = self.backend_with_child("quit")
        learner = self.learner(script)
        learner.run(False)
        wait_for(started.exists)
        learner.stop()
        self.assertFalse(learner.timer.isActive())
        self.assertIsNone(learner.proc)
        time.sleep(3)
        self.assertFalse(marker.exists(), "the backend's child kept running after quit")

    @unittest.skipUnless(sys.platform == "win32", "Windows process trees")
    def test_popup_close_kills_the_backend_tree(self):
        script, marker, started = self.backend_with_child("popup")
        popup = tray_mod.popup_qt.Popup([sys.executable, "-c", script])
        popup.input.setPlainText("draft")
        popup.analyze()
        wait_for(started.exists)
        popup.close()
        self.assertIsNone(popup.proc)
        time.sleep(3)
        self.assertFalse(marker.exists(), "the backend's child kept running after Esc")

    def test_one_at_a_time(self):
        learner = self.learner("import time; time.sleep(30)")
        self.assertTrue(learner.run(False))
        self.assertFalse(learner.run(False))
        self.assertFalse(learner.run(True))
        self.assertEqual(self.results, [(False, "learn is already running", True)])
        learner.on_timeout()  # the guard timer kills it
        wait_for(lambda: len(self.results) == 2)
        self.assertFalse(self.results[1][0])
        self.assertIsNone(learner.proc)


@unittest.skipIf(SKIP, SKIP)
class InstanceTest(unittest.TestCase):
    def test_first_instance(self):
        win = FakeWin()
        self.assertEqual(tray_mod.acquire_instance(win, "x"), 42)
        self.assertEqual(win.mutexes, ["Local\\x"])
        win.mutex_exists = True
        self.assertIsNone(tray_mod.acquire_instance(win, "x"))

    def test_second_instance_asks_the_first_to_show(self):
        win = FakeWin()
        win.mutex_exists = True
        with mock.patch.object(tray_mod, "notify_running", return_value=True) as notify, \
                mock.patch.object(tray_mod, "Tray") as tray, mock.patch.object(tray_mod, "setup_logging"):
            self.assertEqual(tray_mod.main([], win=win), 0)
        notify.assert_called_once_with()
        tray.assert_not_called()
        self.assertEqual(win.hotkeys, {})

    def test_show_request_over_local_socket(self):
        name = f"saywell-test-{os.getpid()}"
        shows = []
        server = tray_mod.listen(name, lambda: shows.append(1), None)
        self.addCleanup(server.close)
        # The second instance is another process: a pipe write waits for this event loop to read it.
        import subprocess
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from PySide6.QtCore import QCoreApplication; "
                "app = QCoreApplication([]); import saywell_tray; sys.exit(0 if saywell_tray.notify_running(sys.argv[2]) else 3)")
        client = subprocess.Popen([sys.executable, "-c", code, str(ROOT / "windows"), name])
        self.addCleanup(client.wait, 10)
        wait_for(lambda: shows and client.poll() is not None, 20)
        self.assertEqual(client.returncode, 0)
        QTest.qWait(50)
        self.assertEqual(shows, [1])
        self.assertFalse(tray_mod.notify_running(f"saywell-test-nobody-{os.getpid()}"))

    def test_autostart_cli(self):
        win = FakeWin()
        out = []
        with mock.patch("builtins.print", out.append):
            tray_mod.main(["autostart", "enable", "--command", '"a" "b"'], win=win)
            tray_mod.main(["autostart", "status"], win=win)
            tray_mod.main(["autostart", "disable"], win=win)
        self.assertEqual(out, ['autostart enabled: "a" "b"', 'autostart enabled: "a" "b"', "autostart disabled"])
        self.assertEqual(win.run, {})


@unittest.skipIf(SKIP, SKIP)
class BackendTest(unittest.TestCase):
    def test_backend_runs_on_console_python(self):
        with tempfile.TemporaryDirectory() as d:
            pythonw, python = Path(d) / "pythonw.exe", Path(d) / "python.exe"
            pythonw.touch()
            python.touch()
            with mock.patch.object(sys, "executable", str(pythonw)), \
                    mock.patch.dict(os.environ, {"SAYWELL_BACKEND": ""}):
                self.assertEqual(tray_mod.backend_argv(), [str(python), str(ROOT / "saywell")])
                self.assertEqual(tray_mod.backend_argv("C:/x/saywell.exe"), ["C:/x/saywell.exe"])


if __name__ == "__main__":
    unittest.main()
