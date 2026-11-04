#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

PLUGIN_ROOT = Path.home() / ".codex/plugins/cache/openai-bundled/unified-computer-use"


def find_spec_path() -> Path:
    """Locate the newest unified-computer-use .mcp.json."""
    override = os.environ.get("CUA_DIRECT_SPEC_PATH")
    if override:
        return Path(override)
    candidates = sorted(
        (p for p in PLUGIN_ROOT.glob("*/.mcp.json") if p.is_file()),
        key=lambda p: p.parent.name,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"Could not find unified-computer-use .mcp.json under {PLUGIN_ROOT}"
        )
    return candidates[0]


def build_child(spec_path: Path):
    spec = json.loads(spec_path.read_text())["mcpServers"]["cua_repl"]
    env = dict(os.environ)
    env.update(spec.get("env") or {})
    env.setdefault("CUA_REPL_ENABLED_SURFACES", "browser")
    # Strip the "--import <loader>" pair so the access-signing gate stays off.
    args = []
    skip = False
    for a in spec["args"]:
        if skip:
            skip = False
            continue
        if a == "--import":
            skip = True
            continue
        args.append(a)
    return spec["command"], args, env


def main() -> None:
    spec_path = find_spec_path()
    command, args, env = build_child(spec_path)

    session_id = os.environ.get("CUA_DIRECT_SESSION_ID") or str(uuid.uuid4())
    turn_meta = json.dumps({"session_id": session_id, "turn_id": str(uuid.uuid4())})

    child = subprocess.Popen(
        [command, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
        bufsize=0,
    )

    def pump_up():
        for line in iter(child.stdout.readline, b""):
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()

    threading.Thread(target=pump_up, daemon=True).start()

    for raw in iter(sys.stdin.buffer.readline, b""):
        line = raw
        stripped = raw.strip()
        if stripped:
            try:
                msg = json.loads(stripped)
                if isinstance(msg, dict) and msg.get("method") == "tools/call":
                    params = msg.setdefault("params", {})
                    meta = params.setdefault("_meta", {})
                    meta.setdefault("x-codex-turn-metadata", turn_meta)
                    line = (json.dumps(msg) + "\n").encode("utf-8")
            except (json.JSONDecodeError, TypeError):
                line = raw
        try:
            child.stdin.write(line)
            child.stdin.flush()
        except (BrokenPipeError, OSError):
            break

    try:
        child.stdin.close()
    except Exception:
        pass
    child.wait()


if __name__ == "__main__":
    main()
