"""Packaging: pyproject.toml, the saywell_app entry points and the logon command they set up.

Fast tests need nothing but the standard library. The full `uv tool install` round trip
(build, install, run the CLI on a fake claude, uninstall) needs uv and maybe the network,
so it runs only with SAYWELL_TEST_UV=1; it uses temporary UV_TOOL_DIR / UV_TOOL_BIN_DIR and
XDG dirs and never touches the real uv tools or saywell data.
"""

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = ROOT / "tests" / "fake_claude.py"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "windows"))
_TMP = tempfile.TemporaryDirectory()
# Before the import: saywell_win reads the config and data paths from these.
os.environ.update({"XDG_CONFIG_HOME": str(Path(_TMP.name) / "config"),
                   "XDG_DATA_HOME": str(Path(_TMP.name) / "data")})

import saywell_app  # noqa: E402
import saywell_win as sw  # noqa: E402

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def tearDownModule():
    _TMP.cleanup()


def toml_str(s):
    return '"' + str(s).replace("\\", "\\\\") + '"'


def fake_config(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'default = "fake"\n[providers.fake]\ntype = "claude-cli"\nmodel = "haiku"\n'
                    f"command = [{toml_str(sys.executable)}, {toml_str(FAKE_CLAUDE)}]\n", encoding="utf-8")


def isolated_env(tmp, **extra):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "SAYWELL_PROVIDER", "PYTHONUTF8")}
    env.update(XDG_CONFIG_HOME=str(tmp / "config"), XDG_DATA_HOME=str(tmp / "data"),
               XDG_CACHE_HOME=str(tmp / "cache"), **extra)
    return env


class PyprojectTest(unittest.TestCase):
    def test_entry_points_exist(self):
        project = PYPROJECT["project"]
        entries = {**project["scripts"], **project["gui-scripts"]}
        self.assertEqual(set(entries), {"saywell", "saywell-autostart", "saywell-tray"})
        self.assertIn("saywell-tray", project["gui-scripts"])  # no console window at logon
        for target in entries.values():
            module, func = target.split(":")
            self.assertTrue(callable(getattr(importlib.import_module(module), func)), target)

    def test_pyside_only_on_windows(self):
        deps = PYPROJECT["project"]["dependencies"]
        self.assertTrue(deps and all("sys_platform == 'win32'" in d for d in deps), deps)

    def test_wheel_ships_backend_and_every_windows_module(self):
        include = PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        for src in include:
            self.assertTrue((ROOT / src).is_file(), src)
        modules = {p.relative_to(ROOT).as_posix() for p in (ROOT / "windows").glob("*.py")}
        self.assertLessEqual(modules, set(include))
        self.assertEqual(include["saywell"], "saywell_app/saywell")
        for src, dst in include.items():
            self.assertEqual(dst, "saywell_app/" + src)  # the repo layout, so ../saywell still resolves


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_root_in_a_checkout(self):
        self.assertEqual(saywell_app.root(), ROOT)

    def test_ensure_config_copies_the_example_once(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp)}), \
                mock.patch.object(sys, "stderr", None):
            path = saywell_app.ensure_config()
            self.assertEqual(path, self.tmp / "saywell" / "config.toml")
            self.assertEqual(path.read_bytes(), (ROOT / "config.example.toml").read_bytes())
            path.write_text("# mine\n", encoding="utf-8")
            self.assertIsNone(saywell_app.ensure_config())
            self.assertEqual(path.read_text(encoding="utf-8"), "# mine\n")

    def test_installed_launcher(self):
        self.assertIsNone(saywell_app.installed_launcher(self.tmp))
        (self.tmp / "saywell-tray.exe").write_bytes(b"")
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(saywell_app.installed_launcher(self.tmp), self.tmp / "saywell-tray.exe")
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIsNone(saywell_app.installed_launcher(self.tmp))

    def test_windows_modules_sets_the_logon_launcher(self):
        exe = self.tmp / "saywell-tray.exe"
        self.addCleanup(setattr, sw, "LAUNCHER", None)
        with mock.patch.object(saywell_app, "installed_launcher", return_value=exe):
            self.assertIs(saywell_app.windows_modules(), sw)
        self.assertEqual(sw.autostart_command(), f'"{exe}"')

    def test_cli_runs_the_backend(self):
        fake_config(self.tmp / "config" / "saywell" / "config.toml")
        proc = subprocess.run([sys.executable, "-c", "import saywell_app; saywell_app.cli()"],
                              input="why the test is fail".encode("utf-8"), capture_output=True,
                              cwd=ROOT, env=isolated_env(self.tmp), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(proc.stdout.decode("utf-8").strip(),
                         "Is there an app that can fix my input? I don't want to interrupt the agent.")
        self.assertTrue((self.tmp / "data" / "saywell" / "log.jsonl").exists())

    def test_cli_passes_arguments_and_exit_code(self):
        proc = subprocess.run([sys.executable, "-c", "import saywell_app; saywell_app.cli()", "--no-such-flag"],
                              capture_output=True, cwd=ROOT, env=isolated_env(self.tmp), timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"--no-such-flag", proc.stderr)


class AutostartCommandTest(unittest.TestCase):
    def test_launcher_wins(self):
        exe = r"C:\Users\me\AppData\Roaming\uv\tools\saywell\Scripts\saywell-tray.exe"
        self.assertEqual(sw.autostart_command(r"C:\Python\python.exe", "s.py", launcher=exe), f'"{exe}"')

    def test_no_launcher_keeps_the_script(self):
        exe = Path(tempfile.gettempdir()) / "py" / "python.exe"
        self.assertIsNone(sw.LAUNCHER)
        self.assertEqual(sw.autostart_command(str(exe), "s.py"), f'"{exe}" "s.py"')


@unittest.skipUnless(os.environ.get("SAYWELL_TEST_UV") == "1", "set SAYWELL_TEST_UV=1 to build and install with uv")
class UvToolInstallTest(unittest.TestCase):
    def test_install_run_uninstall(self):
        uv = shutil.which("uv")
        self.assertTrue(uv, "uv not in PATH")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        bindir = tmp / "bin"
        env = isolated_env(tmp, UV_TOOL_DIR=str(tmp / "tools"), UV_TOOL_BIN_DIR=str(bindir))
        run = lambda *argv, **kw: subprocess.run(argv, capture_output=True, env=env, cwd=tmp, timeout=600, **kw)

        proc = run(uv, "tool", "install", str(ROOT))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        exe = ".exe" if sys.platform == "win32" else ""
        names = {p.name for p in bindir.iterdir()}
        self.assertLessEqual({f"saywell{exe}", f"saywell-tray{exe}", f"saywell-autostart{exe}"}, names)

        proc = run(str(bindir / f"saywell{exe}"), "--list")  # first run: config from the example
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertIn(b"claude", proc.stdout)
        fake_config(tmp / "config" / "saywell" / "config.toml")
        proc = run(str(bindir / f"saywell{exe}"), input="why the test is fail 这个".encode("utf-8"))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertIn(b"Is there an app", proc.stdout)

        proc = run(uv, "tool", "uninstall", "saywell")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertFalse((bindir / f"saywell{exe}").exists())


if __name__ == "__main__":
    unittest.main()
