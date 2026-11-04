#!/usr/bin/env python3
"""Register (or remove) the cua_direct MCP server in Claude Code's config.

Usage:
  python register_claude.py            # add/update cua_direct (dedicated tools + raw js)
  python register_claude.py --raw      # register only the bare proxy (single `js` tool)
  python register_claude.py --remove   # delete the cua_direct entry

Edits ~/.claude.json in place and keeps a timestamped backup next to it.
"""
import argparse
import json
import shutil
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROXY = HERE / "cua_direct_proxy.py"
TOOLS = HERE / "cua_direct_tools.py"
VENV_PY = HERE.parent / ".venv" / "Scripts" / "python.exe"
CONFIG = Path.home() / ".claude.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--raw", action="store_true", help="register the bare proxy instead of the tools server")
    ap.add_argument("--config", default=str(CONFIG))
    args = ap.parse_args()

    cfg_path = Path(args.config)
    data = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    servers = data.setdefault("mcpServers", {})

    backup = cfg_path.with_suffix(cfg_path.suffix + f".bak-{int(time.time())}")
    if cfg_path.exists():
        shutil.copy2(cfg_path, backup)

    if args.remove:
        servers.pop("cua_direct", None)
        action = "removed"
    else:
        servers["cua_direct"] = {
            "command": str(VENV_PY),
            "args": [str(PROXY if args.raw else TOOLS)],
        }
        action = "registered"

    cfg_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(json.dumps({"action": action, "config": str(cfg_path), "backup": str(backup)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
