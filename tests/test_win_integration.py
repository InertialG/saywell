"""The Qt-free half of the Windows tray (windows/saywell_win.py) against a fake Win32 layer:
key specs, terminal detection, paste keys, settings, the logon command. No registry, no hotkeys.

Run: python -m unittest discover -s tests -p "test_win_*.py"
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "windows"))
_TMP = tempfile.TemporaryDirectory()
# Before the import: the module reads the config and data paths from these.
os.environ.update({"XDG_CONFIG_HOME": str(Path(_TMP.name) / "config"),
                   "XDG_DATA_HOME": str(Path(_TMP.name) / "data")})

import saywell_win as sw  # noqa: E402
import win32  # noqa: E402

CTRL, SHIFT, ALT, LWIN = win32.VK_CONTROL, win32.VK_SHIFT, win32.VK_MENU, win32.VK_LWIN


def tearDownModule():
    _TMP.cleanup()


class FakeWin:
    """Windows by handle: {hwnd: (class name, exe name)}; the Run key as a dict."""

    def __init__(self, windows=None):
        self.windows = windows or {}
        self.run = {}

    def window_class(self, hwnd):
        return self.windows.get(hwnd, ("", ""))[0]

    def window_process(self, hwnd):
        return self.windows.get(hwnd, ("", ""))[1]

    def run_value(self, name):
        return self.run.get(name)

    def set_run_value(self, name, command):
        self.run[name] = command

    def delete_run_value(self, name):
        self.run.pop(name, None)


WINDOWS = {1: ("CASCADIA_HOSTING_WINDOW_CLASS", "windowsterminal.exe"),
           2: ("ConsoleWindowClass", ""),
           3: ("mintty", "mintty.exe"),
           4: ("Notepad", "notepad.exe"),
           5: ("Chrome_WidgetWin_1", "code.exe"),
           6: ("org.wezfurlong.wezterm", "wezterm-gui.exe"),
           7: ("SomeClass", "myterm.exe")}


class KeysTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(sw.parse_keys("Win+Alt+E"), (win32.MOD_WIN | win32.MOD_ALT, [LWIN, ALT], ord("E")))
        self.assertEqual(sw.parse_keys(" ctrl + shift + v "), (win32.MOD_CONTROL | win32.MOD_SHIFT,
                                                               [CTRL, SHIFT], ord("V")))
        self.assertEqual(sw.parse_keys("Control+F12")[2], 0x7B)
        self.assertEqual(sw.parse_keys("Shift+Insert")[2], 0x2D)
        self.assertEqual(sw.parse_keys("Alt+Space")[2], 0x20)
        self.assertEqual(sw.parse_keys("Ctrl+Ctrl+1"), (win32.MOD_CONTROL, [CTRL], ord("1")))

    def test_parse_errors(self):
        for bad in ("Hyper+E", "Ctrl+", "Ctrl+Nope", "", 42, "Ctrl+Shift"):
            with self.subTest(bad=bad), self.assertRaises(sw.SettingsError):
                sw.parse_keys(bad)

    def test_hotkey_args(self):
        self.assertEqual(sw.hotkey_args("Win+Alt+E"),
                         (win32.MOD_WIN | win32.MOD_ALT | win32.MOD_NOREPEAT, ord("E")))
        with self.assertRaises(sw.SettingsError):
            sw.hotkey_args("E")  # a bare key would swallow every E typed anywhere

    def test_key_events(self):
        self.assertEqual(sw.key_events("Ctrl+V"),
                         [(CTRL, False, False), (0x56, False, False), (0x56, True, False), (CTRL, True, False)])
        self.assertEqual(sw.key_events("Ctrl+Shift+V"),
                         [(CTRL, False, False), (SHIFT, False, False), (0x56, False, False),
                          (0x56, True, False), (SHIFT, True, False), (CTRL, True, False)])
        # Insert is an extended key; without the flag it would be keypad 0.
        self.assertEqual(sw.key_events("Shift+Insert")[1:3], [(0x2D, False, True), (0x2D, True, True)])


class TerminalTest(unittest.TestCase):
    def setUp(self):
        self.win = FakeWin(WINDOWS)
        self.settings = dict(sw.DEFAULTS)

    def test_is_terminal(self):
        terminals = [h for h in WINDOWS if sw.is_terminal(self.win, h, self.settings)]
        self.assertEqual(terminals, [1, 2, 3, 6])  # by class (conhost has no exe here) or by exe
        self.assertFalse(sw.is_terminal(self.win, 99, self.settings))  # unknown / gone window

    def test_configured_terminals(self):
        self.settings["extra_terminals"] = ["MyTerm.exe"]
        self.assertTrue(sw.is_terminal(self.win, 7, self.settings))
        self.assertTrue(sw.is_terminal(self.win, 1, self.settings))
        self.settings["terminals"] = ["somEclass"]  # replaces the default list
        self.settings["extra_terminals"] = []
        self.assertEqual([h for h in WINDOWS if sw.is_terminal(self.win, h, self.settings)], [7])

    def test_paste_keys(self):
        keys = {h: sw.paste_keys(self.win, h, self.settings) for h in WINDOWS}
        self.assertEqual(keys, {1: "Ctrl+Shift+V", 2: "Shift+Insert", 3: "Shift+Insert", 4: "Ctrl+V",
                                5: "Ctrl+V", 6: "Ctrl+Shift+V", 7: "Ctrl+V"})

    def test_paste_keys_configured(self):
        self.settings.update(paste="Shift+Insert", terminal_paste="Ctrl+Alt+V",
                             paste_keys={"Code.exe": "Ctrl+Shift+Alt+V", "mintty": "Ctrl+Shift+V"})
        self.assertEqual(sw.paste_keys(self.win, 4, self.settings), "Shift+Insert")
        self.assertEqual(sw.paste_keys(self.win, 1, self.settings), "Ctrl+Alt+V")
        self.assertEqual(sw.paste_keys(self.win, 5, self.settings), "Ctrl+Shift+Alt+V")
        self.assertEqual(sw.paste_keys(self.win, 3, self.settings), "Ctrl+Shift+V")  # config beats default


class SettingsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.toml"

    def test_paths_follow_xdg(self):
        # Under a temporary XDG dir (this module's, or another test module's that imported first).
        tmp = Path(tempfile.gettempdir()).resolve()
        self.assertTrue(sw.CONFIG_PATH.resolve().is_relative_to(tmp), sw.CONFIG_PATH)
        self.assertEqual(sw.CONFIG_PATH.parts[-2:], ("saywell", "config.toml"))
        self.assertTrue(sw.DATA_DIR.resolve().is_relative_to(tmp), sw.DATA_DIR)

    def test_defaults_without_config(self):
        self.assertEqual(sw.load_settings(self.path), sw.DEFAULTS)

    def test_windows_table(self):
        self.path.write_text('default = "claude"\n[providers.claude]\ntype = "claude-cli"\n\n'
                             '[windows]\nhotkey = "Ctrl+Alt+Space"\nlearn_interval_minutes = 0\n'
                             'extra_terminals = ["myterm.exe"]\n[windows.paste_keys]\n"code.exe" = "Ctrl+Shift+V"\n',
                             encoding="utf-8")
        s = sw.load_settings(self.path)
        self.assertEqual(s["hotkey"], "Ctrl+Alt+Space")
        self.assertEqual(s["learn_interval_minutes"], 0)
        self.assertEqual(s["paste_keys"], {"code.exe": "Ctrl+Shift+V"})
        self.assertEqual(s["paste"], "Ctrl+V")
        self.assertEqual(s["terminals"], list(sw.TERMINALS))

    def test_bad_settings(self):
        for body in ('[windows]\nhotkey = "Ctrl+Nope"\n', '[windows]\nlearn_interval_minutes = -1\n',
                     '[windows]\nlearn_interval_minutes = "hourly"\n', '[windows.paste_keys]\nx = "Meta+V"\n',
                     '[windows\n'):
            with self.subTest(body=body):
                self.path.write_text(body, encoding="utf-8")
                with self.assertRaises(sw.SettingsError):
                    sw.load_settings(self.path)


class AutostartTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def python_dir(self, *parts):
        d = self.tmp.joinpath(*parts)
        d.mkdir(parents=True)
        for exe in ("python.exe", "pythonw.exe"):
            (d / exe).touch()
        return d

    def test_command_uses_pythonw(self):
        d = self.python_dir("Python313")
        script = self.tmp / "my dir" / "saywell_tray.py"
        self.assertEqual(sw.autostart_command(str(d / "python.exe"), script), f'"{d / "pythonw.exe"}" "{script}"')
        self.assertEqual(sw.autostart_command(str(d / "pythonw.exe"), script), f'"{d / "pythonw.exe"}" "{script}"')

    def test_command_without_pythonw(self):
        exe = self.tmp / "python.exe"
        exe.touch()
        self.assertEqual(sw.autostart_command(str(exe), "s.py"), f'"{exe}" "s.py"')

    def test_command_from_uv_run(self):
        exe = self.tmp / "AppData" / "Local" / "uv" / "cache" / "builds-v0" / ".tmpX" / "Scripts" / "python.exe"
        self.assertTrue(sw.in_uv_cache(exe))
        which = {"uvw": r"C:\bin\uvw.exe"}.get
        self.assertEqual(sw.autostart_command(str(exe), "C:/s.py", which),
                         r'"C:\bin\uvw.exe" run --no-project --with pyside6-essentials pythonw "C:/s.py"')
        with self.assertRaises(RuntimeError):
            sw.autostart_command(str(exe), "C:/s.py", lambda name: None)
        self.assertFalse(sw.in_uv_cache(self.tmp / "uv" / "python.exe"))

    def test_console_python(self):
        d = self.python_dir("venv", "Scripts")
        self.assertEqual(sw.console_python(d / "pythonw.exe"), d / "python.exe")
        self.assertEqual(sw.console_python(d / "python.exe"), d / "python.exe")

    def test_enable_disable_status(self):
        win = FakeWin()
        self.assertEqual(sw.autostart(win, "status"), "autostart disabled")
        self.assertEqual(sw.autostart(win, "enable", '"py" "tray"'), 'autostart enabled: "py" "tray"')
        self.assertEqual(win.run, {"Saywell": '"py" "tray"'})
        self.assertEqual(sw.autostart(win, "status"), 'autostart enabled: "py" "tray"')
        self.assertEqual(sw.autostart(win, "disable"), "autostart disabled")
        self.assertEqual(win.run, {})
        self.assertEqual(sw.autostart(win, "disable"), "autostart disabled")  # twice is fine

    def test_enable_default_command(self):
        win = FakeWin()
        with mock.patch.object(sw, "autostart_command", return_value='"pyw" "t.py"'):
            sw.autostart(win, "enable")
        self.assertEqual(win.run["Saywell"], '"pyw" "t.py"')


class Win32StructTest(unittest.TestCase):
    def test_input_size(self):
        # SendInput rejects every event when cbSize is not sizeof(INPUT) as Windows defines it.
        import ctypes
        self.assertEqual(ctypes.sizeof(win32.INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)

    def test_message_at(self):
        import ctypes
        msg = ctypes.wintypes.MSG(None, win32.WM_HOTKEY, 1, 0)
        self.assertEqual(win32.message_at(ctypes.addressof(msg)).message, win32.WM_HOTKEY)


if __name__ == "__main__":
    unittest.main()
