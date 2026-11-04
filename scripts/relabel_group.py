#!/usr/bin/env python3
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROXY = Path(__file__).with_name("cua_direct_proxy.py")


async def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "Claude"
    from_title = sys.argv[2] if len(sys.argv) > 2 else "ChatGPT"
    params = StdioServerParameters(command=sys.executable, args=[str(PROXY)])
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            code = f"""
const b = await cua.getBrowser({{id:'chrome'}});
const m = await b.capabilities.get('management');
const groups = await m.tabGroups.query({{}});
const targets = groups.filter(g => g.title === {json.dumps(from_title)});
for (const g of targets) await m.tabGroups.update(g.id, {{title: {json.dumps(label)}}});
const after = (await m.tabGroups.query({{}})).map(g => ({{id:g.id, title:g.title, color:g.color}}));
nodeRepl.write(JSON.stringify({{renamed: targets.length, groups: after}}));
"""
            result = await session.call_tool("js", {"code": code, "title": f"relabel tab groups to {label}"})
            for c in result.content:
                if c.type == "text" and c.text.strip().startswith("{"):
                    print(c.text.strip().splitlines()[-1])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
