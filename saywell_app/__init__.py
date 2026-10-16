"""saywell_app: the entry points `uv tool install` creates (see pyproject.toml).

  saywell             the backend CLI (the `saywell` script, run as is)
  saywell-tray        the Windows tray (a GUI exe: no console window); started again while it
                      runs, it opens the popup of the running one
  saywell-autostart   enable|disable|status of starting saywell-tray at logon (console)

Installed, the backend and windows/ sit next to this file; in a checkout (an editable install,
or `python -c "import saywell_app; ..."`) they are one level up. The Linux symlink setup does
not go through here at all.
"""

import os
import runpy
import shutil
import sys
import sysconfig
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRAY_EXE = "saywell-tray.exe"


def root():
    """The directory holding the `saywell` backend and windows/: the package or the checkout."""
    return HERE if (HERE / "saywell").is_file() else HERE.parent


def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "saywell" / "config.toml"


def ensure_config():
    """An installed tool has no checkout to copy config.example.toml from, so the first run does it.
    An existing config is never touched."""
    path, example = config_path(), root() / "config.example.toml"
    if path.exists() or not example.is_file():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, path)
    if sys.stderr:  # a GUI exe has none
        print(f"saywell: created {path} from config.example.toml", file=sys.stderr)
    return path


def cli():
    ensure_config()
    backend = root() / "saywell"
    sys.argv[0] = str(backend)
    runpy.run_path(str(backend), run_name="__main__")


def installed_launcher(scripts_dir=None):
    """saywell-tray.exe in this environment's Scripts dir, the command to run at logon; None if
    there is none (not Windows, or not installed with its entry points)."""
    exe = Path(scripts_dir or sysconfig.get_path("scripts")) / TRAY_EXE
    return exe if sys.platform == "win32" and exe.is_file() else None


def windows_modules():
    """Make windows/*.py importable, and point the logon entry at the installed launcher."""
    path = str(root() / "windows")
    if path not in sys.path:
        sys.path.insert(0, path)
    import saywell_win
    saywell_win.LAUNCHER = installed_launcher()
    return saywell_win


def tray():
    ensure_config()
    windows_modules()
    import saywell_tray
    sys.exit(saywell_tray.main())


def autostart():
    windows_modules()
    import saywell_tray
    sys.exit(saywell_tray.main(["autostart"] + sys.argv[1:]))
