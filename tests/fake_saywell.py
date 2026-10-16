#!/usr/bin/env python3
"""A stand-in for `saywell --json --stream` and `saywell --ask` with canned replies, for the popup tests.

$FAKE_SAYWELL_SCENARIO picks the reply: ok (no meaning flags), flags, tone, fail.
$FAKE_SAYWELL_ASK_DELAY delays the --ask answer by that many seconds.
$FAKE_SAYWELL_LOG, if set, gets one JSON line per call with argv and stdin.
"""

import json
import os
import sys
import time

STAGES = ("fixed", "improved", "why", "fixed_zh", "improved_zh", "flag")
FIXED = "Is there an app that can fix my input? I don't want to interrupt the agent."
IMPROVED = "Is there an app that can fix my input without interrupting the agent?"
FLAGS = {"ok": [], "flags": ["“打断” 被译成 “interrupt”，读者会理解为中途停止"],
         "tone": ["（语气）“pls” 变成 “please”，更正式"]}


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    time.sleep(0.05)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    stdin = sys.stdin.buffer.read().decode("utf-8")
    scenario = os.environ.get("FAKE_SAYWELL_SCENARIO", "ok")
    if os.environ.get("FAKE_SAYWELL_LOG"):
        with open(os.environ["FAKE_SAYWELL_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin}, ensure_ascii=False) + "\n")
    if "--ask" in sys.argv:
        question = sys.argv[sys.argv.index("--ask") + 1]
        context = json.loads(stdin)
        time.sleep(float(os.environ.get("FAKE_SAYWELL_ASK_DELAY", "0")))
        print(f"**{question}**: 用 `interrupt` 更自然。\n\n(history {len(context.get('history', []))})")
        return
    text = stdin.strip()
    if scenario == "fail":
        emit({"stage": "fixed", "stages": STAGES})
        sys.exit("saywell [fake]: claude timed out")
    data = {"original": text, "provider": "fake"}
    emit({"stage": "fixed", "stages": STAGES})
    data["fixed"] = FIXED
    emit({**data, "done": False})
    emit({"stage": "improved", "stages": STAGES})
    data["improved"] = IMPROVED
    emit({"stage": "why", "stages": STAGES})
    data["why"] = "improved 用 without 合并两句，更简洁。"
    emit({**data, "done": False})
    emit({"stage": "fixed_zh", "stages": STAGES})
    data["fixed_zh"] = "有没有一个应用可以修正我的输入？我不想打断智能体。"
    emit({"stage": "improved_zh", "stages": STAGES})
    data["improved_zh"] = "有没有一个应用可以在不打断智能体的情况下修正我的输入？"
    emit({**data, "done": False})
    emit({"stage": "flag", "stages": STAGES})
    emit({**data, "meaning_flags": FLAGS[scenario], "ms": 1234, "done": True})


if __name__ == "__main__":
    main()
