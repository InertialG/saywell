"""Smoke tests for the saywell CLI: run the real script against a fake `claude` and a fake
OpenAI-compatible server, with throwaway XDG dirs and without UTF-8 mode.

    python -m unittest discover tests
"""

import ctypes
import http.server
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SAYWELL = ROOT / "saywell"
FAKE_CLAUDE = Path(__file__).resolve().parent / "fake_claude.py"
sys.path.insert(0, str(FAKE_CLAUDE.parent))
import fake_claude  # noqa: E402

DRAFT = "is there a app can fix my input? i dont want to 打断 the agent\n"
STAGES = ["fixed", "improved", "why", "fixed_zh", "improved_zh", "flag"]


def load_saywell():
    loader = importlib.machinery.SourceFileLoader("saywell_cli", str(SAYWELL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def toml_str(value):
    return json.dumps(str(value))  # a JSON string is a valid TOML basic string


class FakeOpenAI(http.server.BaseHTTPRequestHandler):
    """/v1/chat/completions answering like fake_claude; /v1/models; a slow model for timeouts."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body["model"] == "slow":
            time.sleep(3)
        system = body["messages"][0]["content"]
        reply = fake_claude.reply_for(system)
        self.send_json({"model": body["model"], "choices": [{"message": {"content": reply}}],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 7}})

    def do_GET(self):
        self.send_json({"data": [{"id": "fake-b"}, {"id": "fake-a"}]})

    def send_json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass  # the client gave up (timeout test)

    def log_message(self, *args):
        pass


class SaywellCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="saywell-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config_dir = self.tmp / "config" / "saywell"
        self.data_dir = self.tmp / "data" / "saywell"
        self.config_dir.mkdir(parents=True)
        self.calls = self.tmp / "calls.jsonl"
        command = f"[{toml_str(sys.executable)}, {toml_str(FAKE_CLAUDE)}]"
        base_url = toml_str(f"http://127.0.0.1:{self.server.server_address[1]}/v1")
        (self.config_dir / "config.toml").write_text(f"""
default = "claude"

[providers.claude]
type = "claude-cli"
model = "haiku"
timeout = 20
command = {command}

[providers.slow]
type = "claude-cli"
model = "haiku"
timeout = 2
command = {command}

[providers.api]
base_url = {base_url}
model = "fake-model"
api_key_env = "SAYWELL_TEST_KEY"

[providers.api-slow]
base_url = {base_url}
model = "slow"
timeout = 1
""", encoding="utf-8")

    def run_saywell(self, *args, input=DRAFT, env=None):
        """Run the script like a user would, but with the locale's code page (no UTF-8 mode)."""
        full_env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("PYTHON", "SAYWELL", "XDG_", "FAKE_CLAUDE"))}
        full_env.update({
            "PYTHONUTF8": "0",
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_DATA_HOME": str(self.tmp / "data"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "CLAUDECODE": "1",                  # as inside a Claude Code session
            "ANTHROPIC_API_KEY": "must-not-leak",
            "FAKE_CLAUDE_LOG": str(self.calls),
            **(env or {}),
        })
        start = time.monotonic()
        proc = subprocess.run([sys.executable, str(SAYWELL), *args], input=input.encode("utf-8"),
                              capture_output=True, env=full_env, timeout=60)
        proc.elapsed = time.monotonic() - start
        proc.out = proc.stdout.decode("utf-8")  # strict: anything else is a bug
        proc.err = proc.stderr.decode("utf-8")
        return proc

    def claude_calls(self):
        return [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]

    def jsonl(self, name):
        path = self.data_dir / name
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestFix(SaywellCase):
    def test_fix_claude_cli(self):
        proc = self.run_saywell()
        self.assertEqual(proc.returncode, 0, proc.err)
        self.assertEqual(proc.stdout, (fake_claude.FIX_REPLY + "\n").encode("utf-8"))

        call, = self.claude_calls()
        self.assertIn("打断", call["prompt"])        # prompt reached claude as UTF-8, LF only
        self.assertNotIn("\r", call["prompt"])
        self.assertNotIn("CLAUDECODE", call["env"])
        self.assertNotIn("ANTHROPIC_API_KEY", call["env"])
        self.assertEqual(Path(call["cwd"]), self.tmp / "cache" / "saywell")
        argv = call["argv"]
        self.assertEqual(argv[:3], ["-p", "--model", "haiku"])
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--tools") + 1], "")  # empty argument survives quoting

        log, = self.jsonl("log.jsonl")
        self.assertEqual(log["original"], DRAFT.strip())
        self.assertEqual(log["fixed"], fake_claude.FIX_REPLY)
        usage, = self.jsonl("usage.jsonl")
        self.assertEqual((usage["kind"], usage["in"], usage["out"]), ("fix", 120, 30))
        self.assertNotIn(b"\r\n", (self.data_dir / "log.jsonl").read_bytes())

    def test_fix_openai_api(self):
        proc = self.run_saywell("-p", "api", env={"SAYWELL_TEST_KEY": "k"})
        self.assertEqual(proc.returncode, 0, proc.err)
        self.assertEqual(proc.out, fake_claude.FIX_REPLY + "\n")
        usage, = self.jsonl("usage.jsonl")
        self.assertEqual((usage["provider"], usage["models"], usage["in"]), ("api", ["fake-model"], 11))

    def test_empty_input_passes_through(self):
        proc = self.run_saywell(input="  \n")
        self.assertEqual((proc.returncode, proc.out), (0, "  \n"))
        self.assertFalse(self.calls.exists())

    def test_claude_error(self):
        proc = self.run_saywell(env={"FAKE_CLAUDE_ERROR": "登录已过期"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("saywell [claude]: 登录已过期", proc.err)

    def test_bom_on_stdin_is_dropped(self):
        proc = self.run_saywell(input="\ufeff" + DRAFT)
        self.assertEqual(proc.returncode, 0, proc.err)
        self.assertTrue(self.claude_calls()[0]["prompt"].startswith("Proofread this draft."))
        self.assertNotIn("\ufeff", self.claude_calls()[0]["prompt"])


class TestJson(SaywellCase):
    """The --json --stream lines are what the popup reads; this pins their format."""

    FINAL = {
        "original": DRAFT.strip(),
        "fixed": "Is there an app that can fix my input? I don't want to interrupt the agent.",
        "improved": "Is there an app that can fix my input? I'd rather not interrupt the agent.",
        "why": "用 I'd rather not 语气更自然。",
        "fixed_zh": "有没有一个应用可以修正我的输入？我不想打断 agent。",
        "improved_zh": "有没有一个应用可以修正我的输入？我宁愿不打断 agent。",
        "meaning_flags": ["（语气）“don't want” → “I'd rather not”：语气更委婉。"],
    }

    def stage_line(self, stage):
        return {"stage": stage, "stages": STAGES}

    def partial(self, upto):
        fields = ["fixed", "improved", "why", "fixed_zh", "improved_zh"]
        data = {"original": self.FINAL["original"]}
        for f in fields:
            data[f] = self.FINAL[f] if fields.index(f) < upto else ""
        data["meaning_flags"] = self.FINAL["meaning_flags"] if upto > 5 else []
        return {**data, "provider": "claude", "done": False}

    def test_stream_protocol(self):
        proc = self.run_saywell("--json", "--stream")
        self.assertEqual(proc.returncode, 0, proc.err)
        raw = proc.stdout.split(b"\n")
        self.assertEqual(raw[-1], b"")                       # newline-terminated
        self.assertFalse(any(line.endswith(b"\r") for line in raw))  # "\n", also on Windows
        lines = [json.loads(line) for line in raw[:-1]]

        expected = []
        for i, stage in enumerate(STAGES):
            expected += [self.stage_line(stage), self.partial(i + 1)]
        self.assertEqual(lines[:-1], expected)
        # Key order is part of the protocol too.
        self.assertEqual([list(line) for line in lines[:-1]], [list(line) for line in expected])
        # Stage lines are ASCII JSON with ", " / ": " separators; analysis lines keep Chinese as is.
        self.assertEqual(raw[0], b'{"stage": "fixed", "stages": ["fixed", "improved", "why", '
                                 b'"fixed_zh", "improved_zh", "flag"]}')
        self.assertIn("有没有".encode("utf-8"), raw[7])

        final = lines[-1]
        self.assertEqual(list(final), ["original", "fixed", "improved", "why", "fixed_zh",
                                       "improved_zh", "meaning_flags", "ms", "provider", "done"])
        self.assertIsInstance(final.pop("ms"), int)
        self.assertEqual(final, {**self.FINAL, "provider": "claude", "done": True})

        argv = self.claude_calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        log, = self.jsonl("log.jsonl")
        self.assertEqual((log["kind"], log["improved"]), ("analyze", self.FINAL["improved"]))

    def test_stream_small_deltas(self):
        """Deltas that split tags: stages still arrive once each, in order, before the final line."""
        proc = self.run_saywell("--json", "--stream", env={"FAKE_CLAUDE_CHUNK": "3"})
        self.assertEqual(proc.returncode, 0, proc.err)
        lines = [json.loads(line) for line in proc.out.splitlines()]
        self.assertEqual([line["stage"] for line in lines if "stage" in line], STAGES)
        self.assertTrue(all(line["done"] is False for line in lines[:-1] if "done" in line))
        self.assertEqual({k: v for k, v in lines[-1].items() if k != "ms"},
                         {**self.FINAL, "provider": "claude", "done": True})

    def test_json_without_stream(self):
        proc = self.run_saywell("--json")
        self.assertEqual(proc.returncode, 0, proc.err)
        line, = proc.out.splitlines()
        final = json.loads(line)
        final.pop("ms")
        self.assertEqual(final, {**self.FINAL, "provider": "claude", "done": True})

    def test_json_openai_api(self):
        proc = self.run_saywell("--json", "--stream", "-p", "api")
        self.assertEqual(proc.returncode, 0, proc.err)
        line, = proc.out.splitlines()  # no streaming over the API: just the final line
        self.assertEqual(json.loads(line)["fixed_zh"], self.FINAL["fixed_zh"])

    def test_empty_input(self):
        proc = self.run_saywell("--json", input="\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("empty input", proc.err)


class TestAsk(SaywellCase):
    def test_ask(self):
        context = json.dumps({"original": DRAFT.strip(), "fixed": "f", "improved": "i",
                              "history": [{"q": "上一个问题", "a": "上一个回答"}]}, ensure_ascii=False)
        proc = self.run_saywell("--ask", "为什么不用 ensure？", input=context)
        self.assertEqual(proc.returncode, 0, proc.err)
        self.assertEqual(proc.out, fake_claude.ASK_REPLY + "\n")
        prompt = self.claude_calls()[0]["prompt"]
        self.assertIn("Question: 为什么不用 ensure？", prompt)
        self.assertIn("Earlier question: 上一个问题", prompt)
        log, = self.jsonl("log.jsonl")
        self.assertEqual((log["kind"], log["question"]), ("ask", "为什么不用 ensure？"))

    def test_ask_bad_stdin(self):
        proc = self.run_saywell("--ask", "q", input="not json")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("stdin must be the --json output", proc.err)


class TestOffline(SaywellCase):
    def test_list(self):
        proc = self.run_saywell("--list", input="")
        self.assertEqual(proc.returncode, 0, proc.err)
        rows = proc.out.splitlines()
        self.assertEqual(rows[0].split(), ["*", "claude", "haiku", "no", "key", "needed"])
        self.assertEqual(rows[2].split()[:3], ["api", "fake-model", "$SAYWELL_TEST_KEY"])

    def test_notes(self):
        self.data_dir.mkdir(parents=True)
        notes = [{"key": "a", "type": "vocab", "from": "打断", "to": "interrupt", "explain": "“打断”即 interrupt",
                  "example": "Don't interrupt.", "count": 1, "last_seen": "2026-01-01T00:00:00"},
                 {"key": "b", "type": "grammar", "from": "a app", "to": "an app", "explain": "元音前用 an",
                  "example": "", "count": 3, "last_seen": "2026-01-02T00:00:00"}]
        (self.data_dir / "notes.jsonl").write_text(
            "".join(json.dumps(n, ensure_ascii=False) + "\n" for n in notes), encoding="utf-8")
        proc = self.run_saywell("--notes", "5", input="")
        self.assertEqual(proc.returncode, 0, proc.err)
        self.assertEqual(proc.out.splitlines(), [
            " 3×  grammar  a app → an app", "     元音前用 an",
            " 1×  vocab    打断 → interrupt", "     “打断”即 interrupt", "     e.g. Don't interrupt."])

    def test_notes_empty(self):
        proc = self.run_saywell("--notes", input="")
        self.assertEqual(proc.out, "no notes yet; run: saywell learn\n")

    def test_usage(self):
        self.assertEqual(self.run_saywell().returncode, 0)
        proc = self.run_saywell("--usage", input="")
        self.assertEqual(proc.returncode, 0, proc.err)
        self.assertIn(time.strftime("%Y-%m-%d"), proc.out)
        kind = next(line for line in proc.out.splitlines() if line.startswith("fix "))
        self.assertEqual(kind.split(), ["fix", "1", "120", "30", kind.split()[4], "0.0012"])
        self.assertIn("claude/haiku", proc.out)

    def test_learn(self):
        self.assertEqual(self.run_saywell().returncode, 0)
        proc = self.run_saywell("--learn", input="")
        self.assertEqual(proc.returncode, 0, proc.err)
        self.assertIn("1 messages → 1 new notes", proc.out)
        note, = self.jsonl("notes.jsonl")
        self.assertEqual((note["from"], note["to"], note["sources"]), ("打断", "interrupt", [DRAFT.strip()]))
        proc = self.run_saywell("--learn", input="")
        self.assertIn("0 messages", proc.out)  # learn-state remembers the offset

    def test_models(self):
        proc = self.run_saywell("--models", "-p", "api", input="")
        self.assertEqual(proc.out.splitlines(), ["fake-a", "fake-b"])


def pid_alive(pid):
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return code.value == 259  # STILL_ACTIVE


class TestTimeout(SaywellCase):
    def test_timeout(self):
        proc = self.run_saywell("-p", "slow", env={"FAKE_CLAUDE_SLEEP": "30"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("saywell [slow]: claude timed out", proc.err)
        self.assertLess(proc.elapsed, 15)

    def test_stream_timeout(self):
        proc = self.run_saywell("--json", "--stream", "-p", "slow", env={"FAKE_CLAUDE_SLEEP": "30"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("saywell [slow]: claude timed out", proc.err)
        self.assertLess(proc.elapsed, 15)

    def test_api_timeout(self):
        proc = self.run_saywell("-p", "api-slow")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("saywell [api-slow]: ", proc.err)
        self.assertIn("timed out", proc.err)
        self.assertNotIn("Traceback", proc.err)

    @unittest.skipUnless(sys.platform == "win32", "process-tree kill is Windows-only")
    def test_timeout_kills_process_tree(self):
        for args in ([], ["--json", "--stream"]):
            with self.subTest(args=args):
                pid_file = self.tmp / f"child{len(args)}.pid"
                proc = self.run_saywell(*args, "-p", "slow",
                                        env={"FAKE_CLAUDE_SLEEP": "30", "FAKE_CLAUDE_CHILD": str(pid_file)})
                self.assertIn("claude timed out", proc.err)
                pid = int(pid_file.read_text())
                deadline = time.monotonic() + 5
                while pid_alive(pid) and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertFalse(pid_alive(pid), "grandchild of claude survived the timeout")


class TestFindClaude(unittest.TestCase):
    """Locating claude.exe behind the npm / Volta shims (pure path logic, runs on any OS)."""

    NPM_CMD = '@ECHO off\r\nSETLOCAL\r\nCALL :find_dp0\r\n' \
              '"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"   %*\r\n'
    NPM_PS1 = '$basedir=Split-Path $MyInvocation.MyCommand.Definition -Parent\n' \
              '& "$basedir/node_modules/@anthropic-ai/claude-code/bin/claude.exe"   $args\n'

    def setUp(self):
        self.sw = load_saywell()
        self.tmp = Path(tempfile.mkdtemp(prefix="saywell-find-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        env = {"PATH": "", "USERPROFILE": str(self.home), "HOME": str(self.home),
               "APPDATA": str(self.tmp / "roaming"), "LOCALAPPDATA": str(self.tmp / "local")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("VOLTA_HOME", None)
        os.environ.pop("SAYWELL_CLAUDE", None)

    def touch(self, path, text=""):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        return path

    def package_exe(self, root):
        return self.touch(root / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")

    def set_path(self, *dirs):
        os.environ["PATH"] = os.pathsep.join(str(d) for d in dirs)

    def test_exe_in_path(self):
        exe = self.touch(self.tmp / "bin" / "claude.exe")
        self.set_path(self.tmp / "empty", exe.parent)
        self.assertEqual(Path(self.sw.find_claude_windows()[0]).resolve(), exe.resolve())

    def test_npm_cmd_shim(self):
        node = self.tmp / "node"
        self.touch(node / "claude.cmd", self.NPM_CMD)
        exe = self.package_exe(node)
        self.set_path(node)
        self.assertEqual(Path(self.sw.find_claude_windows()[0]), exe.resolve())

    def test_npm_ps1_shim(self):
        node = self.tmp / "node"
        self.touch(node / "claude.ps1", self.NPM_PS1)
        exe = self.package_exe(node)
        self.set_path(node)
        self.assertEqual(Path(self.sw.find_claude_windows()[0]), exe.resolve())

    def test_volta_shim_falls_back_to_volta_image(self):
        volta_bin = self.tmp / "local" / "Volta" / "bin"
        self.touch(volta_bin / "claude.cmd", "@echo off\r\nvolta run %~n0 %*\r\n")
        exe = self.package_exe(self.tmp / "local" / "Volta" / "tools" / "image" / "node" / "24.12.0")
        self.set_path(volta_bin)
        self.assertEqual(Path(self.sw.find_claude_windows()[0]), exe)

    def test_appdata_npm(self):
        exe = self.package_exe(self.tmp / "roaming" / "npm")
        self.assertEqual(Path(self.sw.find_claude_windows()[0]), exe)

    def test_native_installer(self):
        exe = self.touch(self.home / ".local" / "bin" / "claude.exe")
        self.assertEqual(Path(self.sw.find_claude_windows()[0]), exe)

    def test_not_found(self):
        self.assertIsNone(self.sw.find_claude_windows())

    def test_overrides(self):
        provider = {"command": ["node", "cli.js"]}
        self.assertEqual(self.sw.claude_command(provider), ["node", "cli.js"])
        self.assertEqual(self.sw.claude_command({"command": "C:/x/claude.exe"}), ["C:/x/claude.exe"])
        os.environ["SAYWELL_CLAUDE"] = str(self.tmp / "my claude.exe")
        self.assertEqual(self.sw.claude_command(provider), [str(self.tmp / "my claude.exe")])

    @unittest.skipUnless(sys.platform == "win32", "Windows-only error")
    def test_not_found_error(self):
        with self.assertRaisesRegex(RuntimeError, "claude.exe not found.*SAYWELL_CLAUDE"):
            self.sw.claude_command({})

    @unittest.skipIf(sys.platform == "win32", "Linux keeps running `claude` from PATH")
    def test_linux_unchanged(self):
        self.assertEqual(self.sw.claude_command({}), ["claude"])


if __name__ == "__main__":
    unittest.main()
