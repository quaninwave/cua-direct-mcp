#!/usr/bin/env python3
"""Smoke test for cua-direct: drives Chrome through the proxy end-to-end.

Opens a tab via the ChatGPT extension, navigates it, then lists the tabs that
Claude controls (the ones Chrome places in the "ChatGPT" tab group). Prints a
small JSON summary and exits non-zero on failure. Read-only except for opening
two throwaway tabs (example.com / wikipedia.org).
"""
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROXY = Path(__file__).with_name("cua_direct_proxy.py")


async def main() -> int:
    params = StdioServerParameters(command=sys.executable, args=[str(PROXY)])
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            if "js" not in tools:
                print(json.dumps({"ok": False, "error": "js tool missing", "tools": sorted(tools)}))
                return 1

            code = """
const tab = await cua.createBrowserTab('chrome','https://www.wikipedia.org/');
await tab.goto('https://example.com/?opened-by=claude');
// Chrome assigns the "ChatGPT" tab group a moment after the tab opens; poll briefly.
let all = [], opened = null;
for (let i = 0; i < 10; i++) {
  all = await cua.listTabs({browser:'chrome'});
  opened = all.find(t => t.id === tab.id);
  if (opened && opened.tabGroup === 'ChatGPT') break;
  await new Promise(r => setTimeout(r, 300));
}
const mine = all.filter(t => t.tabGroup === 'ChatGPT');
nodeRepl.write(JSON.stringify({
  ok: true,
  openedTabId: tab.id,
  openedTab: { title: opened.title, host: new URL(opened.url).host, tabGroup: opened.tabGroup || null },
  claudeControlledTabs: mine.length,
  groupName: mine[0] ? mine[0].tabGroup : null
}));
"""
            result = await session.call_tool("js", {"code": code, "title": "cua-direct smoke test"})
            payload = None
            for c in result.content:
                if c.type == "text" and c.text.strip().startswith("{"):
                    payload = c.text.strip().splitlines()[-1]
            print(payload or "<no json payload>")
            return 0 if (payload and '"ok": true' in payload.replace(" ", "").replace('"ok":true', '"ok": true')) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
