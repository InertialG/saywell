"""A stand-in for `claude -p` used by the tests: answers by system prompt, never calls a model.

Environment knobs:
  FAKE_CLAUDE_LOG    append {"argv", "env", "cwd", "prompt"} of each call as a JSON line
  FAKE_CLAUDE_SLEEP  seconds to sleep before answering (timeout tests)
  FAKE_CLAUDE_CHILD  write the PID of a sleeping grandchild here (process-tree kill test)
  FAKE_CLAUDE_ERROR  answer with an is_error result carrying this message
  FAKE_CLAUDE_CHUNK  stream-json delta size: "section" (one delta per tagged section) or a number
"""

import json
import os
import re
import subprocess
import sys
import time

FIX_REPLY = "Is there an app that can fix my input? I don't want to interrupt the agent."

ANALYSIS_REPLY = (
    "<fixed>Is there an app that can fix my input? I don't want to interrupt the agent.</fixed>\n"
    "<improved>Is there an app that can fix my input? I'd rather not interrupt the agent.</improved>\n"
    "<why>用 I'd rather not 语气更自然。</why>\n"
    "<fixed_zh>有没有一个应用可以修正我的输入？我不想打断 agent。</fixed_zh>\n"
    "<improved_zh>有没有一个应用可以修正我的输入？我宁愿不打断 agent。</improved_zh>\n"
    "<flag><changed>tone</changed>“don't want” → “I'd rather not”：语气更委婉。</flag>\n"
    "<flag><changed>no</changed></flag>"
)

ASK_REPLY = "因为 ensure 表示“确保”，这里用 make sure 更口语。\nMake sure the tests pass."

LEARN_REPLY = (
    "<note><msg>0</msg><type>vocab</type><from>打断</from><to>interrupt</to>"
    "<explain>“打断”对应 interrupt。</explain><example>Don't interrupt the agent.</example></note>"
)


def reply_for(system):
    if "proofreader" in system:
        return FIX_REPLY
    if "tagged sections" in system:
        return ANALYSIS_REPLY
    if "follow-up question" in system:
        return ASK_REPLY
    if "learning points" in system:
        return LEARN_REPLY
    return "?"


def chunks(text, size):
    if size == "section":
        return re.findall(r".*?</\w+>\n?", text, flags=re.S)
    size = int(size)
    return [text[i:i + size] for i in range(0, len(text), size)]


def out(obj):
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def main():
    argv = sys.argv[1:]
    opt = lambda name: argv[argv.index(name) + 1] if name in argv else None
    system, fmt = opt("--system-prompt") or "", opt("--output-format")
    prompt = sys.stdin.buffer.read().decode("utf-8")
    if os.environ.get("FAKE_CLAUDE_LOG"):
        with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": argv, "env": sorted(os.environ), "cwd": os.getcwd(),
                                "prompt": prompt}, ensure_ascii=False) + "\n")
    if os.environ.get("FAKE_CLAUDE_CHILD"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with open(os.environ["FAKE_CLAUDE_CHILD"], "w") as f:
            f.write(str(child.pid))
    time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))

    error = os.environ.get("FAKE_CLAUDE_ERROR")
    reply = error or reply_for(system)
    result = {"type": "result", "subtype": "success", "is_error": bool(error), "result": reply,
              "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0},
              "total_cost_usd": 0.0012, "modelUsage": {"claude-haiku-fake": {}}}
    if fmt == "json":
        out(result)
    elif fmt == "stream-json":
        out({"type": "system", "subtype": "init"})
        sys.stdout.buffer.write(b"not json, must be skipped\n")
        if not error:
            for piece in chunks(reply, os.environ.get("FAKE_CLAUDE_CHUNK", "section")):
                out({"type": "stream_event",
                     "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": piece}}})
        out(result)
    else:
        sys.stdout.buffer.write(reply.encode("utf-8"))
    sys.exit(1 if error else 0)


if __name__ == "__main__":
    main()
