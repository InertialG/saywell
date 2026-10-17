"""What e2e.ps1 asks Python, because PowerShell has no TOML parser and the claude.exe lookup
lives in the backend:

  provider CONFIG [NAME]   the provider NAME (default: the config's default) as JSON, with
                           key_state ok / placeholder / empty / env-missing / none-needed;
                           never the key itself
  secret CONFIG [NAME]     the provider's API key, so e2e.ps1 can mask it in what it prints and logs
  claude BACKEND           the argv the backend would run for claude-cli, as JSON
"""

import importlib.machinery
import importlib.util
import json
import os
import sys
import tomllib

PLACEHOLDER = "PASTE_YOUR_API_KEY_HERE"


def out(obj):
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")


def load_provider(config_path, name):
    with open(config_path, "rb") as f:
        config = tomllib.load(f)
    providers = config.get("providers", {})
    name = name or config.get("default") or ""
    if name not in providers:
        raise LookupError(f"unknown provider {name!r}; known: {', '.join(providers) or 'none'}")
    return config, name, providers[name]


def key_of(p):
    env = p.get("api_key_env")
    return os.environ.get(env, "") if env else str(p.get("api_key", ""))


def key_state(p):
    if p.get("type") == "claude-cli":
        return "none-needed"
    key = key_of(p).strip()
    if key == PLACEHOLDER:
        return "placeholder"
    if not key:
        return "env-missing" if p.get("api_key_env") else "empty"
    return "ok"


def cmd_provider(config_path, name=None):
    try:
        config, name, p = load_provider(config_path, name)
    except (OSError, tomllib.TOMLDecodeError, LookupError) as e:
        out({"error": str(e)})
        return 1
    out({"name": name, "default": config.get("default"), "type": p.get("type", "openai"),
         "model": p.get("model", ""), "base_url": p.get("base_url", ""),
         "api_key_env": p.get("api_key_env", ""), "key_state": key_state(p)})
    return 0


def cmd_secret(config_path, name=None):
    try:
        _, _, p = load_provider(config_path, name)
    except (OSError, tomllib.TOMLDecodeError, LookupError):
        return 1
    sys.stdout.buffer.write(key_of(p).strip().encode("utf-8"))
    return 0


def cmd_claude(backend):
    loader = importlib.machinery.SourceFileLoader("saywell_backend", backend)
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    try:
        out({"argv": module.claude_command({})})
    except RuntimeError as e:
        out({"error": str(e)})
        return 1
    return 0


def main(argv):
    commands = {"provider": cmd_provider, "secret": cmd_secret, "claude": cmd_claude}
    if not argv or argv[0] not in commands:
        sys.exit(__doc__)
    return commands[argv[0]](*argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
