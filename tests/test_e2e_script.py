"""scripts/windows/e2e.ps1: the config template, the helper it asks for provider and key state,
and that the script parses under every PowerShell on this machine (skipped without one).

The end-to-end run itself installs with uv and calls a real provider, so it is not run here.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
E2E = ROOT / "scripts" / "windows"
TEMPLATE = E2E / "e2e.config.example.toml"
SCRIPT = E2E / "e2e.ps1"
HELPER = E2E / "e2e_helper.py"
PLACEHOLDER = "PASTE_YOUR_API_KEY_HERE"


def helper(*args, env=None):
    proc = subprocess.run([sys.executable, str(HELPER), *map(str, args)], capture_output=True, env=env)
    return proc.returncode, proc.stdout.decode("utf-8")


class TemplateTest(unittest.TestCase):
    def setUp(self):
        self.config = tomllib.loads(TEMPLATE.read_text(encoding="utf-8"))

    def test_default_is_an_api_provider_with_a_placeholder_key(self):
        default = self.config["default"]
        self.assertIn(default, self.config["providers"])
        p = self.config["providers"][default]
        self.assertNotEqual(p.get("type"), "claude-cli")
        self.assertTrue(p["base_url"].startswith("http"))
        self.assertTrue(p["model"])
        self.assertEqual(p["api_key"], PLACEHOLDER)
        self.assertNotIn("api_key_env", p)  # the key goes inline, in the sandbox copy

    def test_claude_is_the_fallback(self):
        self.assertEqual(self.config["providers"]["claude"]["type"], "claude-cli")

    def test_ignored_by_git(self):
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".e2e/", lines)


class HelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "config.toml"

    def write(self, key):
        text = TEMPLATE.read_text(encoding="utf-8").replace(PLACEHOLDER, key)
        self.config.write_text(text, encoding="utf-8")

    def state(self, *name, env=None):
        code, out = helper("provider", self.config, *name, env=env)
        self.assertEqual(code, 0, out)
        return json.loads(out)

    def test_placeholder_and_empty_keys(self):
        self.write(PLACEHOLDER)
        self.assertEqual(self.state()["key_state"], "placeholder")
        self.write("")
        self.assertEqual(self.state()["key_state"], "empty")

    def test_a_real_key_is_ok_and_never_printed_by_provider(self):
        self.write("sk-test-0123456789")
        code, out = helper("provider", self.config)
        self.assertEqual(json.loads(out)["key_state"], "ok")
        self.assertNotIn("sk-test-0123456789", out)
        self.assertEqual(helper("secret", self.config), (0, "sk-test-0123456789"))

    def test_claude_needs_no_key(self):
        self.write(PLACEHOLDER)
        info = self.state("claude")
        self.assertEqual((info["name"], info["type"], info["key_state"]), ("claude", "claude-cli", "none-needed"))

    def test_api_key_env(self):
        self.config.write_text('default = "x"\n[providers.x]\nbase_url = "http://h/v1"\nmodel = "m"\n'
                               'api_key_env = "E2E_TEST_KEY"\n', encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "E2E_TEST_KEY"}
        self.assertEqual(self.state(env=env)["key_state"], "env-missing")
        self.assertEqual(self.state(env={**env, "E2E_TEST_KEY": "k-123"})["key_state"], "ok")

    def test_unknown_provider_and_bad_toml(self):
        self.write(PLACEHOLDER)
        code, out = helper("provider", self.config, "nope")
        self.assertEqual(code, 1)
        self.assertIn("unknown provider", json.loads(out)["error"])
        self.config.write_text("default = \n", encoding="utf-8")
        code, out = helper("provider", self.config)
        self.assertEqual(code, 1)
        self.assertIn("error", json.loads(out))


class ScriptTest(unittest.TestCase):
    def test_ps1_has_a_utf8_bom(self):
        # Windows PowerShell 5.1 reads a BOM-less script as the ANSI code page: the Chinese breaks.
        self.assertTrue(SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_cmd_wrapper(self):
        text = (E2E / "e2e.cmd").read_bytes().decode("ascii")  # cmd.exe reads it in the OEM code page
        self.assertIn("-ExecutionPolicy Bypass", text)
        self.assertIn('-File "%~dp0e2e.ps1" %*', text)
        self.assertIn("exit /b %ERRORLEVEL%", text)

    def test_parses_in_every_powershell(self):
        shells = [s for s in ("powershell", "pwsh") if shutil.which(s)]
        if not shells:
            self.skipTest("no PowerShell on this machine")
        path = str(SCRIPT).replace("'", "''")
        check = (f"$errors = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('{path}', "
                 "[ref]$null, [ref]$errors); $errors | ForEach-Object { \"$($_.Extent.StartLineNumber): $($_.Message)\" }; "
                 "exit $errors.Count")
        for shell in shells:
            with self.subTest(shell=shell):
                proc = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", check],
                                      capture_output=True, timeout=120)
                self.assertEqual(proc.returncode, 0, proc.stdout.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
