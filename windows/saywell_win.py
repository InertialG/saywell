"""saywell_win: the Qt-free half of the Windows tray: settings, key specs, terminal detection,
paste keys and the logon (Run key) command. Every Win32 call goes through a `win` object
(win32.Win32, or a fake in the tests).

Settings live in the backend's config.toml, in a [windows] table the backend ignores:

  [windows]
  hotkey = "Win+Alt+E"                 # global hotkey that opens the popup
  paste = "Ctrl+V"                     # paste keys for ordinary windows
  terminal_paste = "Ctrl+Shift+V"      # paste keys for terminals
  terminals = [...]                    # window classes / exe names that are terminals (replaces the list)
  extra_terminals = ["myterm.exe"]     # ... or add to the default list
  learn_interval_minutes = 60          # run `saywell --learn` this often; 0 turns it off

  [windows.paste_keys]                 # per window class / exe name, wins over the two above
  "mintty.exe" = "Shift+Insert"
"""

import os
import shutil
import sys
import tomllib
from pathlib import Path

import win32

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "saywell" / "config.toml"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "saywell"
TRAY_SCRIPT = HERE / "saywell_tray.py"
# saywell-tray.exe of a `uv tool install`, set by saywell_app's entry points; None from a checkout.
LAUNCHER = None
RUN_NAME = "Saywell"
INSTANCE_NAME = "saywell-tray-" + os.environ.get("USERNAME", "user")

# Matched case-insensitively against the foreground window's class name and its exe name.
TERMINALS = (
    "CASCADIA_HOSTING_WINDOW_CLASS", "WindowsTerminal.exe",  # Windows Terminal
    "ConsoleWindowClass",                                    # conhost
    "mintty", "mintty.exe",                                  # Git Bash, MSYS2, Cygwin
    "VirtualConsoleClass", "ConEmu64.exe", "ConEmu.exe",     # ConEmu / Cmder
    "PuTTY", "putty.exe", "wezterm-gui.exe", "alacritty.exe", "Tabby.exe", "Hyper.exe",
)
# Terminals where Ctrl+Shift+V does not paste by default; Shift+Insert does in both.
# conhost types ^V for it (checked on Windows 11); mintty pastes with it only when its
# Ctrl+Shift shortcuts option is on.
PASTE_KEYS = {"ConsoleWindowClass": "Shift+Insert", "mintty": "Shift+Insert", "mintty.exe": "Shift+Insert"}
DEFAULTS = {"hotkey": "Win+Alt+E", "paste": "Ctrl+V", "terminal_paste": "Ctrl+Shift+V",
            "terminals": list(TERMINALS), "extra_terminals": [], "paste_keys": {},
            "learn_interval_minutes": 60}

MODIFIERS = {"win": (win32.MOD_WIN, win32.VK_LWIN), "alt": (win32.MOD_ALT, win32.VK_MENU),
             "ctrl": (win32.MOD_CONTROL, win32.VK_CONTROL), "control": (win32.MOD_CONTROL, win32.VK_CONTROL),
             "shift": (win32.MOD_SHIFT, win32.VK_SHIFT)}
# Keys sent with KEYEVENTF_EXTENDEDKEY; without it Insert would arrive as keypad 0, and so on.
EXTENDED = {0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28}
KEYS = {"space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
        "backspace": 0x08, "insert": 0x2D, "ins": 0x2D, "delete": 0x2E, "del": 0x2E, "home": 0x24,
        "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27,
        "down": 0x28, ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
        "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE}
KEYS.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
KEYS.update({chr(c): c for c in range(ord("0"), ord("9") + 1)})
KEYS.update({f"f{n}": 0x6F + n for n in range(1, 25)})


class SettingsError(ValueError):
    pass


def load_settings(path=None):
    """The [windows] settings from config.toml over the defaults; a missing file means defaults."""
    path = Path(path or CONFIG_PATH)
    table = {}
    if path.exists():
        try:
            with path.open("rb") as f:
                table = tomllib.load(f).get("windows", {})
        except tomllib.TOMLDecodeError as e:
            raise SettingsError(f"{path}: {e}") from None
    settings = {**DEFAULTS, **table}
    for key in ("hotkey", "paste", "terminal_paste"):
        parse_keys(settings[key])  # fail at startup, not at the first paste
    for spec in settings["paste_keys"].values():
        parse_keys(spec)
    if not isinstance(settings["learn_interval_minutes"], (int, float)) or settings["learn_interval_minutes"] < 0:
        raise SettingsError("windows.learn_interval_minutes must be a number of minutes, 0 to turn it off")
    return settings


def parse_keys(spec):
    """'Ctrl+Shift+V' -> (MOD_* flags, [modifier vks], key vk)."""
    if not isinstance(spec, str):
        raise SettingsError(f"not a key combination: {spec!r}")
    *mods, key = [p.strip().lower() for p in spec.split("+")]
    flags, vks = 0, []
    for m in mods:
        if m not in MODIFIERS:
            raise SettingsError(f"unknown modifier {m!r} in {spec!r} (use Win, Alt, Ctrl, Shift)")
        flag, vk = MODIFIERS[m]
        if not flags & flag:
            flags |= flag
            vks.append(vk)
    if key not in KEYS:
        raise SettingsError(f"unknown key {key!r} in {spec!r}")
    return flags, vks, KEYS[key]


def hotkey_args(spec):
    """RegisterHotKey's (modifiers, vk) for a spec; holding the key down does not repeat it."""
    flags, _, vk = parse_keys(spec)
    if not flags:
        raise SettingsError(f"the hotkey {spec!r} needs a modifier (Win, Alt, Ctrl or Shift)")
    return flags | win32.MOD_NOREPEAT, vk


def key_events(spec):
    """SendInput events for a spec: modifiers down, the key down and up, modifiers up."""
    _, mods, vk = parse_keys(spec)
    ext = lambda k: k in EXTENDED
    return ([(m, False, False) for m in mods] + [(vk, False, ext(vk)), (vk, True, ext(vk))]
            + [(m, True, False) for m in reversed(mods)])


def window_names(win, hwnd):
    """The lowercased class name and exe name of a window, the names terminals are matched by."""
    return {n.lower() for n in (win.window_class(hwnd), win.window_process(hwnd)) if n}


def is_terminal(win, hwnd, settings):
    terminals = {t.lower() for t in list(settings["terminals"]) + list(settings["extra_terminals"])}
    return bool(window_names(win, hwnd) & terminals)


def paste_keys(win, hwnd, settings):
    """The key spec that pastes into hwnd: a paste_keys entry, else terminal_paste or paste."""
    names = window_names(win, hwnd)
    for table in (settings["paste_keys"], PASTE_KEYS):
        for name, spec in table.items():
            if name.lower() in names:
                return spec
    return settings["terminal_paste"] if is_terminal(win, hwnd, settings) else settings["paste"]


# ---- logon ----------------------------------------------------------------

def gui_python(executable):
    """pythonw.exe next to a python.exe (no console window at logon), else the executable itself."""
    exe = Path(executable)
    pythonw = exe.with_name("pythonw.exe")
    return pythonw if exe.name.lower() == "python.exe" and pythonw.exists() else exe


def console_python(executable):
    """python.exe next to a pythonw.exe, for running the backend (see saywell_tray.backend_argv)."""
    exe = Path(executable)
    python = exe.with_name("python.exe")
    return python if exe.name.lower() == "pythonw.exe" and python.exists() else exe


def in_uv_cache(executable):
    """True for the throwaway environments of `uv run --with ...`, which uv may delete or replace."""
    parts = [p.lower() for p in Path(executable).parts]
    return any(a == "uv" and b == "cache" for a, b in zip(parts, parts[1:]))


def autostart_command(executable=None, script=TRAY_SCRIPT, which=shutil.which, launcher=None):
    """The command line the Run key starts at logon: the installed saywell-tray.exe when there is
    one, else this script on pythonw (or through uv, from a `uv run --with` environment)."""
    launcher = launcher or LAUNCHER
    if launcher:
        return f'"{launcher}"'
    executable = executable or sys.executable
    if in_uv_cache(executable):
        uv = which("uvw") or which("uv")
        if not uv:
            raise RuntimeError("running from a uv environment, but uv is not in PATH; "
                               "pass --command to say what to run at logon")
        return f'"{uv}" run --no-project --with pyside6-essentials pythonw "{script}"'
    return f'"{gui_python(executable)}" "{script}"'


def autostart(win, action, command=None):
    """enable / disable / status of the logon entry; returns the line to print."""
    if action == "enable":
        command = command or autostart_command()
        win.set_run_value(RUN_NAME, command)
        return f"autostart enabled: {command}"
    if action == "disable":
        win.delete_run_value(RUN_NAME)
        return "autostart disabled"
    current = win.run_value(RUN_NAME)
    return f"autostart enabled: {current}" if current else "autostart disabled"
