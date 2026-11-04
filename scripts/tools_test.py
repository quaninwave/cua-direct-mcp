#!/usr/bin/env python3
"""End-to-end test for cua_direct_tools.py against a real Chrome.

Serves a small local test page (form, checkbox, select, iframe, alert button,
download link, file input, console logs), opens it in ONE throwaway tab, runs
every tool group against it and closes the tab. It never touches the user's
other tabs/bookmarks. Pass --only a,b to run a subset of step names.
"""
import argparse
import asyncio
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOOLS = Path(__file__).with_name("cua_direct_tools.py")

PAGE = """<!doctype html><meta charset=utf-8><title>cua-direct tools test</title>
<body style="font-family:sans-serif">
<h1 id=h>Tools test</h1>
<input id=name placeholder="Your name" aria-label="Name">
<label><input type=checkbox id=agree> I agree</label>
<select id=color><option value=r>Red</option><option value=g>Green</option><option value=b>Blue</option></select>
<button id=btn onclick="document.getElementById('out').textContent='clicked:'+(window.__n=(window.__n||0)+1)">Click me</button>
<button id=alertbtn onclick="setTimeout(()=>alert('hello alert'),300)">Alert</button>
<button id=confirmbtn onclick="setTimeout(()=>{document.getElementById('out').textContent='confirm:'+confirm('sure?')},300)">Confirm</button>
<button id=syncprompt onclick="document.getElementById('out').textContent='prompt:'+prompt('name?')">Ask name</button>
<button id=syncalert onclick="alert('sync alert');document.getElementById('out').textContent='sync-alert-closed'">Show alert</button>
<div id=out>-</div>
<a id=dl href="/file.txt" download>Download</a>
<input type=file id=up onchange="document.getElementById('out').textContent='file:'+this.files[0].name">
<img id=img src="/pic.svg" width=40 height=40>
<iframe id=fr src="/frame.html" width=300 height=60></iframe>
<table id=tb><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>
<ul id=list><li class=it><b>one</b> <a href="/a1">first</a></li><li class=it><b>two</b> <a href="/a2">second</a></li></ul>
<div style="height:2500px">tall</div>
<script>console.log('hello-log'); console.warn('warn-log'); console.error('error-log');</script>
</body>"""
FRAME = "<!doctype html><title>f</title><button id=fb onclick=\"document.body.append('in-frame-clicked')\">Frame btn</button>"
SVG = "<svg xmlns='http://www.w3.org/2000/svg' width='40' height='40'><rect width='40' height='40' fill='tomato'/></svg>"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        routes = {
            "/": ("text/html; charset=utf-8", PAGE, {}),
            "/frame.html": ("text/html; charset=utf-8", FRAME, {}),
            "/pic.svg": ("image/svg+xml", SVG, {}),
            "/file.txt": ("application/octet-stream", "cua-direct download test\n",
                          {"Content-Disposition": 'attachment; filename="cua-test.txt"'}),
        }
        ctype, body, extra = routes.get(self.path.split("?")[0], ("text/plain", "404", {}))
        data = body.encode()
        self.send_response(200 if self.path.split("?")[0] in routes else 404)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)


def text_of(res):
    return "".join(c.text for c in res.content if c.type == "text")


def jload(res):
    try:
        return json.loads(text_of(res))
    except Exception:
        return None


async def main(only: set[str]) -> int:
    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/"
    tmp = Path(tempfile.mkdtemp(prefix="cua-tools-test-"))
    upfile = tmp / "upload-me.txt"
    upfile.write_text("hello upload")

    results: list[tuple[str, bool, str]] = []

    params = StdioServerParameters(command=sys.executable, args=[str(TOOLS)])
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as c:
            await c.initialize()

            async def call(_tool, **kw):
                return await c.call_tool(_tool, kw)

            async def step(name, fn):
                if only and name not in only:
                    return
                try:
                    ok, info = await fn()
                except Exception as e:  # noqa: BLE001
                    ok, info = False, f"EXC {type(e).__name__}: {e}"
                results.append((name, bool(ok), str(info)[:220]))
                print(("PASS " if ok else "FAIL ") + name + " :: " + str(info)[:220], flush=True)

            tab = {}

            async def s_browsers():
                r = jload(await call("browser_list"))
                return any(b.get("name") == "Chrome" for b in r), [b.get("name") for b in r]

            async def s_open():
                r = await call("tab_open", url=base, session_name="🧪 cua-direct tools test")
                d = jload(r)
                tab["id"] = d["id"]
                return d["url"].startswith(base), d

            async def s_list():
                r = jload(await call("tab_list", scope="all", query="cua-direct tools test"))
                return r["count"] >= 1, r["count"]

            async def s_select():
                d = jload(await call("tab_select", tab_id=tab["id"]))
                return d["id"] == tab["id"], d

            async def s_ax():
                t = text_of(await call("page_ax_tree", full=True))
                return "Tools test" in t and "Click me" in t, t[:100].replace("\n", " ")

            async def s_dom():
                t = text_of(await call("page_dom_snapshot"))
                return "cua-direct tools test" in t or "Click me" in t, len(t)

            async def s_read():
                a = jload(await call("page_read", target={"selector": "#h"}, what="inner_text"))
                t = text_of(await call("page_read", target={"selector": "#h"}))
                at = text_of(await call("page_read", target={"selector": "#dl"}, what="attribute", attribute="href"))
                h = text_of(await call("page_read", target={"selector": "#btn"}, what="html"))
                al = jload(await call("page_read", target={"selector": "button"}, all_matches=True))
                return (t == "Tools test" and at == "/file.txt" and "<button" in h and al["count"] >= 3), (t, at, h[:40], al["count"])

            async def s_find():
                r = jload(await call("page_find", target={"role": "button", "name": "Click me"}))
                f = jload(await call("page_find", target={"selector": "iframe"}))
                return r["count"] == 1 and f["items"][0]["src"] == "/frame.html", (r["count"], f["count"])

            async def s_evaluate():
                a = jload(await call("page_evaluate", expression="() => document.title"))
                b = jload(await call("page_evaluate", expression="(el, n) => el.id + n", arg="!", target={"selector": "#h"}))
                return a["result"] == "cua-direct tools test" and b["result"] == "h!", (a, b)

            async def s_type():
                await call("page_type", text="Claude", target={"selector": "#name"})
                v = jload(await call("page_read", target={"selector": "#name"}, what="value"))
                return text_of(await call("page_read", target={"selector": "#name"}, what="value")) == "Claude", v

            async def s_type_native():
                await call("page_type", text="X", target={"selector": "#name"}, mode="fill")
                await call("page_click", target={"selector": "#name"})
                await call("page_type", text="YZ", mode="native_type")
                v = text_of(await call("page_read", target={"selector": "#name"}, what="value"))
                return v == "XYZ", v

            async def s_click():
                await call("page_click", target={"selector": "#btn"})
                await call("page_click", target={"role": "button", "name": "Click me"}, double=True)
                o = text_of(await call("page_read", target={"selector": "#out"}))
                return o.startswith("clicked:") and o != "clicked:1", o

            async def s_click_index():
                t = text_of(await call("page_ax_tree", full=True))
                idx = next(int(l.strip().split()[0]) for l in t.splitlines() if "Click me" in l and l.strip()[0].isdigit())
                await call("page_click", element_index=idx)
                o = text_of(await call("page_read", target={"selector": "#out"}))
                return o.startswith("clicked:"), (idx, o)

            async def s_check():
                await call("page_check", target={"selector": "#agree"}, checked=True)
                on = jload(await call("page_evaluate", expression="() => document.getElementById('agree').checked"))
                await call("page_check", target={"selector": "#agree"}, checked=False)
                off = jload(await call("page_evaluate", expression="() => document.getElementById('agree').checked"))
                return on["result"] is True and off["result"] is False, (on, off)

            async def s_select_opt():
                await call("page_select_option", target={"selector": "#color"}, option={"label": "Blue"})
                v = jload(await call("page_evaluate", expression="() => document.getElementById('color').value"))
                return v["result"] == "b", v

            async def s_key():
                await call("page_click", target={"selector": "#name"})
                await call("page_press_key", key="Control+A", target={"selector": "#name"})
                await call("page_press_key", key="BackSpace", target={"selector": "#name"})
                v = text_of(await call("page_read", target={"selector": "#name"}, what="value"))
                return v == "", repr(v)

            async def s_frame():
                await call("page_click", target={"selector": "#fb", "frame": "#fr"})
                t = text_of(await call("page_read", target={"selector": "body", "frame": "#fr"}))
                return "in-frame-clicked" in t, t[:60]

            async def s_scroll():
                before = jload(await call("page_evaluate", expression="() => window.scrollY"))
                await call("page_scroll", direction="down", pages=1)
                after = jload(await call("page_evaluate", expression="() => window.scrollY"))
                await call("page_scroll", direction="up", pages=5)
                return after["result"] > before["result"], (before, after)

            async def s_wait():
                a = await call("page_wait", ms=100)
                b = await call("page_wait", target={"selector": "#h"}, state="visible")
                c2 = await call("page_wait", load_state="load")
                return not (a.is_error or b.is_error or c2.is_error), [text_of(x)[:50] for x in (a, b, c2) if x.is_error]

            async def s_shot():
                p = tmp / "shot.jpg"
                r = await call("page_screenshot", save_path=str(p))
                r2 = await call("page_screenshot", full_page=True)
                r3 = await call("page_screenshot", target={"selector": "#h"})
                r4 = await call("page_screenshot", clip={"x": 0, "y": 0, "width": 200, "height": 100})
                imgs = [sum(1 for c in x.content if c.type == "image") for x in (r, r2, r3, r4)]
                return all(i == 1 for i in imgs) and p.stat().st_size > 500, (imgs, p.stat().st_size if p.exists() else None, [text_of(x)[:80] for x in (r, r2, r3, r4) if x.is_error])

            async def s_clip():
                await call("clipboard_write", text="from-claude 123")
                r = jload(await call("clipboard_read"))
                return r["text"] == "from-claude 123", r

            async def s_dialog():
                await call("page_click", target={"selector": "#alertbtn"})
                await asyncio.sleep(0.8)
                g = jload(await call("dialog_get"))
                h = await call("dialog_handle", action="accept")
                g2 = jload(await call("dialog_get"))
                return g.get("open") and g.get("type") == "alert" and not h.is_error and g2.get("open") is False, (g, text_of(h)[:80], g2)

            async def s_confirm():
                # the button text matches the risky-control guard, so the test confirms it explicitly
                await call("page_click", target={"selector": "#confirmbtn"}, confirmed=True)
                await asyncio.sleep(0.8)
                g = jload(await call("dialog_get"))
                await call("dialog_handle", action="accept")
                await asyncio.sleep(0.3)
                v = text_of(await call("page_read", target={"selector": "#out"}))
                return g.get("type") == "confirm" and v == "confirm:true", (g, v)

            async def s_sync_dialogs():
                # onclick calls prompt()/alert() synchronously: a locator click must not wedge the tab
                c1 = await call("page_click", target={"selector": "#syncprompt"}, timeout_ms=5000)
                g = jload(await call("dialog_get"))
                h = await call("dialog_handle", action="accept", prompt_text="probe-answer")
                v = text_of(await call("page_read", target={"selector": "#out"}))
                await call("page_click", target={"selector": "#syncalert"}, timeout_ms=5000)
                h2 = await call("dialog_handle", action="dismiss")
                v2 = text_of(await call("page_read", target={"selector": "#out"}))
                ok = (not c1.is_error and jload(c1).get("dialog") == "prompt" and g.get("type") == "prompt" and not h.is_error
                      and v == "prompt:probe-answer" and not h2.is_error and v2 == "sync-alert-closed")
                return ok, (text_of(c1)[:90], g.get("type"), v, v2)

            async def s_download():
                # scratch=True: cleanup removes it from Downloads, so reruns do not get "cua-test (1).txt"
                dest = tmp / "dl"
                dest.mkdir()
                r = await call("file_download", target={"selector": "#dl"}, method="click", copy_to=str(dest), scratch=True)
                got = [p.name for p in dest.iterdir()]
                ok = (not r.is_error) and any(n.startswith("cua-test") and n.endswith(".txt") for n in got)
                return ok, (text_of(r)[:160], got)

            async def s_upload():
                r = await call("file_upload", files=[str(upfile)], target={"selector": "#up"}, confirmed=True)
                v = text_of(await call("page_read", target={"selector": "#out"}))
                return (not r.is_error) and v == "file:upload-me.txt", (text_of(r)[:150], v)

            async def s_logs():
                r = jload(await call("page_console_logs"))
                msgs = [l["message"] for l in r["logs"]]
                e = jload(await call("page_console_logs", levels=["error"]))
                return any("hello-log" in m for m in msgs) and all(l["level"] == "error" for l in e["logs"]) and e["count"] >= 1, (len(msgs), e["count"])

            async def s_assets():
                r = jload(await call("page_assets_list", kinds=["image"]))
                b = await call("page_assets_bundle", kinds=["image"], inventory_id=r["inventoryId"])
                if b.is_error and "EPERM" in text_of(b) and "browser-use" in text_of(b):
                    return r["summary"]["totalCount"] >= 1, "list OK; bundle SKIP (runtime fs sandbox EPERM, needs Codex grant)"
                return r["summary"]["totalCount"] >= 1 and not b.is_error, (r["summary"], text_of(b)[:400])

            async def s_viewport():
                a = await call("viewport_set", preset="mobile")
                w = jload(await call("page_evaluate", expression="() => window.innerWidth"))
                b = await call("viewport_reset")
                return not a.is_error and not b.is_error and w["result"] < 500, (text_of(a)[:80], w, text_of(b)[:80])

            async def s_cdp():
                raw = await call("cdp_send", method="Runtime.evaluate", params={"expression": "1+41", "returnByValue": True})
                if raw.is_error and "approval gate" in text_of(raw):
                    return True, "SKIP (raw CDP needs Browser Use approval; gate reached correctly)"
                r = json.loads(text_of(raw))
                ev = jload(await call("cdp_events", limit=5))
                return r["result"]["result"]["value"] == 42 and "cursor" in ev, (r, list(ev))

            async def s_nav():
                await call("tab_navigate", action="goto", url=base + "frame.html")
                await call("tab_navigate", action="back")
                u1 = jload(await call("tab_navigate", action="forward"))
                u2 = jload(await call("tab_navigate", action="reload", observe=True))
                return u2["url"].endswith("frame.html") and "state" in u2, (u1["url"], u2["url"])

            async def s_session():
                a = await call("session_name", name="🧪 tools test")
                b = await call("tab_mark", kind="handoff")
                return not a.is_error and not b.is_error, (text_of(a)[:80], text_of(b)[:80])

            async def s_mgmt_read():
                q = jload(await call("tabs_query", filter={}))
                g = jload(await call("tab_group_list"))
                w = jload(await call("window_list"))
                bm = jload(await call("bookmark_list"))
                bs = jload(await call("bookmark_search", query="inwave", limit=3))
                at = jload(await call("browser_audit_trail", limit=1))
                return q["count"] >= 1 and isinstance(g, list) and len(w) >= 1 and "folders" in bm and "bookmarks" in bs and "changes" in at, (q["count"], len(g), len(w), len(bm["folders"]), bs["count"], at["total"])

            async def s_group():
                mid = int(tab["id"])
                g = jload(await call("tab_group_create", tab_ids=[mid], title="cua-test", color="cyan"))
                gid = g["groupId"]
                u = jload(await call("tab_group_update", group_id=gid, title="cua-test2", collapsed=False))
                lst = jload(await call("tab_group_list"))
                mine = [x for x in lst if x["id"] == gid]
                ok = bool(mine) and mine[0]["title"] == "cua-test2" and mid in mine[0]["tabIds"]
                await call("tab_ungroup", tab_ids=[mid])
                p = jload(await call("tab_pin", tab_ids=[mid], pinned=True))
                await call("tab_pin", tab_ids=[mid], pinned=False)
                return ok and p["ok"], (gid, mine[0] if mine else None)

            async def s_sort_move():
                mid = int(tab["id"])
                r = jload(await call("tab_sort", tab_ids=[mid], by="title"))
                m = await call("tab_move", tab_ids=[mid], index=-1)
                return "order" in r and not m.is_error, (len(r["order"]), text_of(m)[:80])

            async def s_history():
                # narrow no-match query: exercises the path without reading real history
                r = await call("browser_history", queries=["zzzz-cua-no-such-term-zzzz"], limit=1, confirmed=True)
                d = jload(r)
                return (not r.is_error) and d["count"] == 0, text_of(r)[:200]

            async def s_close():
                # tab_mark(handoff) took the tab off the cleanup list, so closing it needs confirmed=True
                r = jload(await call("tab_close", tab_id=tab["id"], confirmed=True))
                return r.get("closed") == tab["id"], r

            async def s_js():
                r = await call("js", code="nodeRepl.write('raw-ok')", title="raw js")
                return "raw-ok" in text_of(r), text_of(r)[-60:]

            async def s_gate():
                r = await call("page_click", target={"selector": "#confirmbtn"})
                # no confirmed: refused before any bookmark lookup, so the id never has to exist
                b = await call("bookmark_remove", id="0")
                return (r.is_error and "needs confirmation" in text_of(r) and b.is_error
                        and "needs confirmation" in text_of(b)), (text_of(r)[:100], text_of(b)[:100])

            async def s_hint():
                # confirmed=True skips the guard's element lookup so the click fails fast on timeout_ms
                r = await call("page_click", target={"role": "button", "name": "Clik me"}, timeout_ms=1500, confirmed=True)
                return r.is_error and "closest elements" in text_of(r) and "target=" in text_of(r), text_of(r)[-200:]

            async def s_look():
                d = jload(await call("look"))
                targets = [b.get("target") for b in d["buttons"]]
                return d["title"] == "cua-direct tools test" and {"selector": "#btn"} in targets, (len(d["inputs"]), targets[:3])

            async def s_links():
                d = jload(await call("links", filter="file"))
                return d["count"] >= 1 and d["links"][0]["href"].endswith("/file.txt"), d

            async def s_table():
                d = jload(await call("table", selector="#tb"))
                return d["rows"] == [["A", "B"], ["1", "2"]], d

            async def s_items():
                d = jload(await call("items", container="#list .it", fields={"name": "b", "href": "a@href"}))
                w = jload(await call("items", container="#list .it", fields={"name": "b", "nope": ".no-such"}))
                return (d["count"] == 2 and d["rows"][0] == {"name": "one", "href": "/a1"} and "warning" not in d
                        and "nope" in w.get("warning", "") and "name" not in w["warning"]), (d, w.get("warning"))

            async def s_fill():
                d = jload(await call("fill_form", data={"Name": "Ada", "I agree": True}))
                v = text_of(await call("page_read", target={"selector": "#name"}, what="value"))
                return all(x["ok"] for x in d["results"]) and v == "Ada", (d, v)

            async def s_snapshot():
                a = jload(await call("snapshot", action="save", name="cua-tools-test", selector="#h"))
                b = jload(await call("snapshot", action="diff", name="cua-tools-test", selector="#h"))
                return "saved" in a and b["changed"] is False, (a, b)

            async def s_scroll_load():
                d = jload(await call("scroll_load", item="#list .it", rounds=3, wait_ms=300))
                return d["final"] == 2, d

            async def s_cleanup():
                d = jload(await call("cleanup"))
                return d is not None and not d.get("failed"), d

            for name, fn in [
                ("browsers", s_browsers), ("open", s_open), ("list", s_list), ("select", s_select),
                ("ax", s_ax), ("dom", s_dom), ("read", s_read), ("find", s_find), ("evaluate", s_evaluate),
                ("type", s_type), ("type_native", s_type_native), ("click", s_click), ("click_index", s_click_index),
                ("check", s_check), ("select_opt", s_select_opt), ("key", s_key), ("frame", s_frame),
                ("gate", s_gate), ("hint", s_hint), ("look", s_look), ("links", s_links), ("table", s_table),
                ("items", s_items), ("fill_form", s_fill), ("snapshot", s_snapshot), ("scroll_load", s_scroll_load),
                ("scroll", s_scroll), ("wait", s_wait), ("shot", s_shot), ("clipboard", s_clip),
                ("dialog", s_dialog), ("confirm", s_confirm), ("sync_dialogs", s_sync_dialogs),
                ("download", s_download), ("upload", s_upload),
                ("logs", s_logs), ("assets", s_assets), ("viewport", s_viewport), ("cdp", s_cdp),
                ("nav", s_nav), ("session", s_session), ("mgmt_read", s_mgmt_read), ("group", s_group),
                ("sort_move", s_sort_move), ("history", s_history), ("js", s_js), ("close", s_close),
                ("cleanup", s_cleanup),
            ]:
                await step(name, fn)

    srv.shutdown()
    failed = [n for n, ok, _ in results if not ok]
    print(json.dumps({"passed": len(results) - len(failed), "failed": failed}))
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main({s for s in a.only.split(",") if s})))
