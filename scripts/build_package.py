#!/usr/bin/env python3
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = [
    "README.md", "CHANGELOG.md", "VERSION", "requirements.txt", "install.ps1",
    "mcp-config.example.json",
    "scripts/cua_direct_proxy.py", "scripts/cua_direct_tools.py", "scripts/register_claude.py",
    "scripts/relabel_group.py", "scripts/smoke_test.py", "scripts/tools_test.py",
    "scripts/build_package.py",
]


def main() -> None:
    version = (ROOT / "VERSION").read_text().strip()
    name = f"cua-direct-mcp-{version}"
    out = ROOT / "dist" / f"{name}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in FILES:
            z.write(ROOT / rel, f"{name}/{rel}")
    print(out)


if __name__ == "__main__":
    main()
