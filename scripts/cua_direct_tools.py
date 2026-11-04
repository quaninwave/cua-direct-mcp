import asyncio
import base64
import json
import os
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, ImageContent, TextContent

PROXY = Path(__file__).with_name("cua_direct_proxy.py")
SENTINEL = "@@CUA@@"
_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE.is_file() else "0.2.0"
MAX_OUT = int(os.environ.get("CUA_DIRECT_MAX_OUTPUT_CHARS", "60000"))

Target = dict[str, Any] | None
TARGET_DOC = (
    "target = locator spec, one of: selector (CSS), role (+name), text, label, "
    "placeholder, test_id. Optional: frame (CSS selector of an iframe), nth (0-based), "
    "has_text, exact (bool), regex (bool: treat text/name/label/placeholder as RegExp)."
)


def targeted(fn):
    """Append the locator-spec help to a tool's description."""
    fn.__doc__ = (fn.__doc__ or "").rstrip() + " " + TARGET_DOC
    return fn


class Bridge:
    """Holds the client session to the proxy plus a little per-session state."""

    def __init__(self) -> None:
        self.session: ClientSession | None = None
        self.lock = asyncio.Lock()  # the cua_repl runtime is stateful: one call at a time
        self.browser = os.environ.get("CUA_DIRECT_BROWSER", "chrome")
        self.current_tab: str | None = None


BRIDGE = Bridge()


@asynccontextmanager
async def lifespan(_server):
    params = StdioServerParameters(
        command=sys.executable, args=[str(PROXY)], env={**os.environ}
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            BRIDGE.session = session
            yield BRIDGE


mcp = MCPServer(
    "cua_direct",
    version=VERSION,
    instructions=(
        "Drive Chrome (via the ChatGPT extension) with dedicated tools. Typical flow: "
        "tab_list -> tab_select / tab_open -> page_ax_tree or page_read -> page_click / "
        "page_type -> page_screenshot. Tools that act on a tab take tab_id; when omitted "
        "they use the last tab opened/selected. Locator-based tools take a `target` dict "
        "(selector/role/text/label/placeholder/test_id, optional frame/nth). Pass "
        "observe=true on action tools to get the fresh accessibility tree back. Only "
        "reorganise tabs/windows/bookmarks when the user asked; confirm before deleting."
    ),
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# JS bridge
# --------------------------------------------------------------------------

PRELUDE = r"""
const A = __ARGS__;
const out = (v) => nodeRepl.write("@@CUA@@" + JSON.stringify(v === undefined ? null : v));
const browser = await cua.getBrowser({ id: A.browser });
const getTab = (id) => cua.getTab(String(id), { browser: A.browser });
const mgmt = () => browser.capabilities.get("management");
const rootOf = (tab, frame) => (frame ? tab.playwright.frameLocator(frame) : tab.playwright);
const loc = (tab, s) => {
  s = s || {};
  const r = rootOf(tab, s.frame);
  const m = (v) => (s.regex ? new RegExp(v, "i") : v);
  const ex = s.exact == null ? {} : { exact: s.exact };
  let l;
  if (s.selector) l = r.locator(s.selector);
  else if (s.role) l = r.getByRole(s.role, { ...ex, ...(s.name != null ? { name: m(s.name) } : {}) });
  else if (s.text != null) l = r.getByText(m(s.text), ex);
  else if (s.label != null) l = r.getByLabel(m(s.label), ex);
  else if (s.placeholder != null) l = r.getByPlaceholder(m(s.placeholder), ex);
  else if (s.test_id != null) l = r.getByTestId(s.test_id);
  else throw new Error("target needs one of: selector, role(+name), text, label, placeholder, test_id");
  if (s.has_text != null) l = l.filter({ hasText: m(s.has_text) });
  if (s.nth != null) l = l.nth(s.nth);
  return l;
};
const brief = async (tab) => ({ id: tab.id, title: await tab.title(), url: await tab.url() });
const after = async (tab, r) => {
  if (A.observe) r.state = await tab.getAXState({ emit: false });
  return r;
};
const TO = A.timeout_ms ? { timeoutMs: A.timeout_ms } : {};
"""


def _wrap(body: str, args: dict) -> str:
    prelude = PRELUDE.replace("__ARGS__", json.dumps(args, ensure_ascii=False))
    return (
        "{\ntry {\n"
        + prelude
        + body
        + '\n} catch (e) { nodeRepl.write("@@CUA@@" + JSON.stringify({ __error: String((e && e.message) || e) })); }\n}'
    )


def _text(s: str) -> TextContent:
    return TextContent(type="text", text=s)


def _error(msg: str) -> CallToolResult:
    return CallToolResult(content=[_text(msg)], is_error=True)


def _format(data: Any) -> str:
    if isinstance(data, dict) and set(data) == {"__text"}:
        s = data["__text"]
    else:
        s = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(s) > MAX_OUT:
        s = s[:MAX_OUT] + f"\n...[truncated {len(s) - MAX_OUT} chars; narrow the request]"
    return s


async def _call_js(code: str, title: str, timeout_ms: int):
    if BRIDGE.session is None:
        raise RuntimeError("proxy session not ready")
    async with BRIDGE.lock:
        return await BRIDGE.session.call_tool(
            "js", {"code": code, "title": title[:80] or "cua-direct", "timeout_ms": timeout_ms}
        )


def _extract(result) -> tuple[Any, list[ImageContent]]:
    """Pull the sentinel-tagged JSON payload and any images out of a js result."""
    data: Any = None
    found = False
    images: list[ImageContent] = []
    decoder = json.JSONDecoder()
    for c in result.content:
        if c.type == "image":
            images.append(c)
        elif c.type == "text":
            i = c.text.find(SENTINEL)
            if i >= 0:
                try:
                    data, _ = decoder.raw_decode(c.text, i + len(SENTINEL))
                    found = True
                except json.JSONDecodeError:
                    pass
    if not found:
        texts = [c.text for c in result.content if c.type == "text"]
        data = {"__error": "no result from runtime: " + " | ".join(t[-400:] for t in texts)[-800:]}
    return data, images


def _need_tab(tab_id: str | int | None) -> str:
    tid = str(tab_id) if tab_id not in (None, "") else BRIDGE.current_tab
    if not tid:
        raise ValueError("No tab selected: pass tab_id, or call tab_open / tab_select first.")
    return tid


async def run(
    body: str,
    args: dict | None = None,
    *,
    title: str,
    tab: bool = False,
    tab_id: str | int | None = None,
    timeout_ms: int = 60000,
    save_to: str | None = None,
    set_current: bool = False,
) -> CallToolResult:
    args = dict(args or {})
    args["browser"] = BRIDGE.browser
    if tab:
        args["tab_id"] = _need_tab(tab_id)
    result = await _call_js(_wrap(body, args), title, timeout_ms)
    if result.is_error:
        return _error("\n".join(c.text for c in result.content if c.type == "text")[-1500:])
    data, images = _extract(result)
    if isinstance(data, dict) and "__error" in data:
        return _error(data["__error"])
    if set_current and isinstance(data, dict) and data.get("id"):
        BRIDGE.current_tab = str(data["id"])
    content: list[Any] = []
    if save_to and images:
        p = Path(save_to).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(base64.b64decode(images[0].data))
        data = {**data, "saved_to": str(p)} if isinstance(data, dict) else data
    content.append(_text(_format(data)))
    content.extend(images)
    return CallToolResult(content=content)


# --------------------------------------------------------------------------
# 1. Browser & tabs
# --------------------------------------------------------------------------


@mcp.tool()
async def js(code: str, timeout_ms: int = 30000, title: str = "cua-direct js") -> CallToolResult:
    """Raw escape hatch: run JavaScript in the cua_repl runtime (same as the proxy's js tool).
    Output is returned unfiltered, including first-use documentation."""
    result = await _call_js(code, title, timeout_ms)
    return CallToolResult(content=list(result.content), is_error=result.is_error)


@mcp.tool()
async def browser_list() -> CallToolResult:
    """List browsers the runtime can drive (Chrome, Edge, in-app) with their open tabs."""
    return await run(
        """
const res = [];
for (const b of await cua.listBrowsers({ emit: false })) {
  let tabs = [];
  try { tabs = await cua.listTabs({ browser: b.id, emit: false }); } catch (e) {}
  res.push({ ...b, tabs });
}
out(res);""",
        title="List browsers",
    )


@mcp.tool()
async def browser_select(browser: str) -> str:
    """Choose which browser the other tools drive for the rest of the session. Accepts 'chrome',
    'edge', or a browser id from browser_list: when several Chrome profiles are connected each is
    its own browser with its own id (e.g. '3'), so pick the id of the profile you want."""
    BRIDGE.browser = browser
    BRIDGE.current_tab = None
    return f"Now driving browser {browser}."


@mcp.tool()
async def tab_list(
    scope: Literal["all", "agent"] = "all", query: str | None = None, limit: int = 100
) -> CallToolResult:
    """List tabs. scope=all: every user tab in all windows (with lastOpened and tabGroup);
    scope=agent: only tabs this session controls. query filters by title/URL substring."""
    return await run(
        """
let tabs = A.scope === "agent" ? await browser.tabs.list() : await browser.user.openTabs();
if (A.query) { const q = A.query.toLowerCase(); tabs = tabs.filter(t => ((t.title||"") + " " + (t.url||"")).toLowerCase().includes(q)); }
out({ count: tabs.length, tabs: tabs.slice(0, A.limit) });""",
        {"scope": scope, "query": query, "limit": limit},
        title="List tabs",
    )


@mcp.tool()
async def tab_select(tab_id: str) -> CallToolResult:
    """Claim an existing tab (from tab_list) and make it the default for later tools."""
    return await run(
        "const t = await getTab(A.tab_id); out(await brief(t));",
        title="Select tab",
        tab=True,
        tab_id=tab_id,
        set_current=True,
    )


@mcp.tool()
async def tab_open(
    url: str | None = None, session_name: str | None = None, make_current: bool = True
) -> CallToolResult:
    """Open a new tab (optionally at url). session_name is a short emoji-prefixed label
    for the browser session, e.g. '🔎 Research'."""
    return await run(
        """
const opts = A.session_name ? { sessionName: A.session_name } : undefined;
const t = await cua.createBrowserTab(A.browser, A.url || undefined, opts);
out(await brief(t));""",
        {"url": url, "session_name": session_name},
        title="Open tab",
        set_current=make_current,
    )


@mcp.tool()
async def tab_close(tab_id: str | None = None) -> CallToolResult:
    """Close a tab. Closing a tab the user owns loses its state: only do it when asked."""
    tid = _need_tab(tab_id)
    res = await run(
        "const t = await getTab(A.tab_id); await t.close(); out({ closed: A.tab_id });",
        title="Close tab",
        tab=True,
        tab_id=tid,
    )
    if BRIDGE.current_tab == tid:
        BRIDGE.current_tab = None
    return res


@mcp.tool()
async def tab_navigate(
    action: Literal["goto", "back", "forward", "reload"],
    url: str | None = None,
    tab_id: str | None = None,
    observe: bool = False,
) -> CallToolResult:
    """Navigate a tab: goto (needs url), back, forward or reload. Do not goto the URL the tab
    is already on (use reload)."""
    if action == "goto" and not url:
        raise ValueError("url is required for goto")
    return await run(
        """
const t = await getTab(A.tab_id);
if (A.action === "goto") await t.goto(A.url);
else await t[A.action]();
out(await after(t, await brief(t)));""",
        {"action": action, "url": url, "observe": observe},
        title=f"Tab {action}",
        tab=True,
        tab_id=tab_id,
    )


# --------------------------------------------------------------------------
# 2. Reading pages
# --------------------------------------------------------------------------


@mcp.tool()
async def page_ax_tree(tab_id: str | None = None, full: bool = False) -> CallToolResult:
    """Accessibility tree with numeric element indices (use them as element_index in click/type).
    Default returns a diff vs the previous tree; full=true forces the whole tree."""
    return await run(
        "const t = await getTab(A.tab_id); out({ __text: await t.getAXState({ emit: false, disableDiffing: !!A.full }) });",
        {"full": full},
        title="Read accessibility tree",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
async def page_dom_snapshot(tab_id: str | None = None) -> CallToolResult:
    """DOM snapshot of the page as a string, including expanded iframe bodies."""
    return await run(
        "const t = await getTab(A.tab_id); out({ __text: await t.playwright.domSnapshot() });",
        title="DOM snapshot",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
@targeted
async def page_read(
    target: Target = None,
    what: Literal["text", "inner_text", "attribute", "html", "value"] = "inner_text",
    attribute: str | None = None,
    all_matches: bool = False,
    limit: int = 50,
    tab_id: str | None = None,
) -> CallToolResult:
    """Read text/attributes. Without target reads the whole body. what: text (textContent),
    inner_text (rendered), attribute (needs `attribute`), html (outerHTML), value (form value).
    all_matches=true returns one entry per matching element (up to limit). """
    return await run(
        """
const t = await getTab(A.tab_id);
const spec = A.target || { selector: "body" };
const base = loc(t, spec);
const read = async (l) => {
  if (A.what === "text") return await l.textContent(TO);
  if (A.what === "inner_text") return await l.innerText(TO);
  if (A.what === "attribute") return await l.getAttribute(A.attribute, TO);
  if (A.what === "html") return await l.evaluate((el) => el.outerHTML);
  return await l.evaluate((el) => el.value ?? null);
};
if (A.all_matches) {
  const items = (await base.all()).slice(0, A.limit);
  const vals = [];
  for (const l of items) vals.push(await read(l));
  out({ count: await base.count(), values: vals });
} else {
  out({ __text: String(await read(base)) });
}""",
        {"target": target, "what": what, "attribute": attribute, "all_matches": all_matches, "limit": limit},
        title="Read page content",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
@targeted
async def page_find(
    target: dict[str, Any], limit: int = 20, tab_id: str | None = None
) -> CallToolResult:
    """Find elements and describe them (tag, text, href, role, value, visibility, bounding box).
    Use target={"selector":"iframe"} to list iframes, then pass frame=<selector> in other tools. """
    return await run(
        """
const t = await getTab(A.tab_id);
const l = loc(t, A.target);
const count = await l.count();
const items = await l.evaluateAll((els, n) => els.slice(0, n).map((e) => {
  const r = e.getBoundingClientRect();
  const cs = getComputedStyle(e);
  return {
    tag: e.tagName.toLowerCase(), id: e.id || undefined, name: e.getAttribute("name") || undefined,
    type: e.getAttribute("type") || undefined, role: e.getAttribute("role") || undefined,
    href: e.getAttribute("href") || undefined, src: e.getAttribute("src") || undefined,
    placeholder: e.getAttribute("placeholder") || undefined,
    aria: e.getAttribute("aria-label") || undefined,
    value: ("value" in e && e.value !== "") ? String(e.value).slice(0, 80) : undefined,
    text: (e.innerText || e.textContent || "").trim().replace(/\\s+/g, " ").slice(0, 120) || undefined,
    visible: r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none",
    box: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
  };
}), A.limit);
out({ count, shown: items.length, items });""",
        {"target": target, "limit": limit},
        title="Find elements",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
@targeted
async def page_evaluate(
    expression: str,
    arg: Any = None,
    target: Target = None,
    tab_id: str | None = None,
) -> CallToolResult:
    """Run a READ-ONLY JavaScript function in the page and return its JSON result, e.g.
    expression="() => document.title". With target, the function receives that element as
    its first argument: "(el, arg) => el.dataset.id". """
    return await run(
        """
const t = await getTab(A.tab_id);
// The read-only page scope has no eval, and a string is evaluated as an *expression*, not
// invoked as a function. So function-shaped sources are turned into an IIFE expression
// (page level), or into a function whose toString() carries the wrapped source (element level).
const isFn = /^\\s*(async\\b|function\\b|\\([^)]*\\)\\s*=>|[\\w$]+\\s*=>)/.test(A.expression);
const argJson = JSON.stringify(A.arg === undefined ? null : A.arg);
let r;
if (A.target) {
  const src = isFn ? "(el) => (" + A.expression + ")(el, " + argJson + ")" : "(el) => (" + A.expression + ")";
  const f = (el) => 0;
  f.toString = () => src;
  r = await loc(t, A.target).evaluate(f);
} else {
  r = await t.playwright.evaluate(isFn ? "(" + A.expression + ")(" + argJson + ")" : A.expression);
}
out({ result: r === undefined ? null : r });""",
        {"expression": expression, "arg": arg, "target": target},
        title="Evaluate in page",
        tab=True,
        tab_id=tab_id,
    )


# --------------------------------------------------------------------------
# 3. Actions
# --------------------------------------------------------------------------


@mcp.tool()
@targeted
async def page_click(
    target: Target = None,
    element_index: int | None = None,
    x: float | None = None,
    y: float | None = None,
    button: Literal["left", "right", "middle"] = "left",
    double: bool = False,
    modifiers: list[Literal["Alt", "Control", "ControlOrMeta", "Meta", "Shift"]] | None = None,
    force: bool = False,
    timeout_ms: int | None = None,
    observe: bool = False,
    tab_id: str | None = None,
) -> CallToolResult:
    """Click or double-click. Give exactly one way to aim: target (locator), element_index
    (from page_ax_tree) or x,y viewport coordinates. modifiers/force only apply to locators. """
    return await run(
        """
const t = await getTab(A.tab_id);
if (A.element_index != null || A.x != null) {
  const aim = A.element_index != null ? A.element_index : [A.x, A.y];
  await t.click(aim, { mouseButton: A.button, clickCount: A.double ? 2 : 1 });
} else {
  const l = loc(t, A.target);
  const o = { ...TO, button: A.button };
  if (A.force) o.force = true;
  if (A.modifiers) o.modifiers = A.modifiers;
  if (A.double) await l.dblclick(o); else await l.click(o);
}
out(await after(t, { ok: true }));""",
        {"target": target, "element_index": element_index, "x": x, "y": y, "button": button,
         "double": double, "modifiers": modifiers, "force": force, "timeout_ms": timeout_ms,
         "observe": observe},
        title="Click",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
@targeted
async def page_type(
    text: str,
    target: Target = None,
    element_index: int | None = None,
    mode: Literal["fill", "type", "sequential", "native_type", "set_value", "paste"] | None = None,
    press_enter: bool = False,
    timeout_ms: int | None = None,
    observe: bool = False,
    tab_id: str | None = None,
) -> CallToolResult:
    """Enter text. mode: fill (replace value, default with target), type (append), sequential
    (key by key), native_type (type into focused / element_index, default without target),
    set_value (needs element_index), paste (for long or multiline text).
    press_enter submits afterwards. """
    return await run(
        """
const t = await getTab(A.tab_id);
const mode = A.mode || (A.target ? "fill" : "native_type");
const L = () => loc(t, A.target);
if (mode === "fill") await L().fill(A.text, TO);
else if (mode === "type") await L().type(A.text, TO);
else if (mode === "sequential") await L().pressSequentially(A.text, TO);
else if (mode === "native_type") await t.typeText(A.element_index ?? null, A.text);
else if (mode === "set_value") await t.setValue(A.element_index, A.text);
else if (mode === "paste") await t.paste(A.element_index ?? null, A.text, { format: "text" });
else throw new Error("unknown mode " + mode);
if (A.press_enter) {
  if (A.target && ["fill", "type", "sequential"].includes(mode)) await L().press("Enter", TO);
  else await t.pressKey(A.element_index ?? null, "Return");
}
out(await after(t, { ok: true, mode }));""",
        {"text": text, "target": target, "element_index": element_index, "mode": mode,
         "press_enter": press_enter, "timeout_ms": timeout_ms, "observe": observe},
        title="Type text",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
@targeted
async def page_press_key(
    key: str,
    target: Target = None,
    element_index: int | None = None,
    observe: bool = False,
    tab_id: str | None = None,
) -> CallToolResult:
    """Press a key or shortcut. With target (locator) use Playwright names: 'Enter', 'Control+A',
    'ArrowDown'. Without target uses native xdotool-style names: 'Return', 'ctrl+a', 'Down',
    'Tab' on the focused element or element_index. """
    return await run(
        """
const t = await getTab(A.tab_id);
if (A.target) await loc(t, A.target).press(A.key, TO);
else await t.pressKey(A.element_index ?? null, A.key);
out(await after(t, { ok: true }));""",
        {"key": key, "target": target, "element_index": element_index, "observe": observe},
        title=f"Press {key}",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
@targeted
async def page_check(
    target: dict[str, Any],
    checked: bool = True,
    force: bool = False,
    observe: bool = False,
    tab_id: str | None = None,
) -> CallToolResult:
    """Set a checkbox / switch / radio to checked (default) or unchecked. """
    return await run(
        """
const t = await getTab(A.tab_id);
await loc(t, A.target).setChecked(A.checked, { ...TO, ...(A.force ? { force: true } : {}) });
out(await after(t, { ok: true, checked: A.checked }));""",
        {"target": target, "checked": checked, "force": force, "observe": observe},
        title="Set checkbox",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
@targeted
async def page_select_option(
    target: dict[str, Any],
    option: str | dict[str, Any] | list[str | dict[str, Any]],
    observe: bool = False,
    tab_id: str | None = None,
) -> CallToolResult:
    """Pick option(s) in a native <select>. option: a value string, {"label":..}, {"value":..},
    {"index":n}, or a list for multi-select. """
    return await run(
        """
const t = await getTab(A.tab_id);
await loc(t, A.target).selectOption(A.option, TO);
out(await after(t, { ok: true }));""",
        {"target": target, "option": option, "observe": observe},
        title="Select option",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
async def page_scroll(
    direction: Literal["up", "down", "left", "right"],
    pages: float = 1,
    element_index: int | None = None,
    x: float | None = None,
    y: float | None = None,
    observe: bool = False,
    tab_id: str | None = None,
) -> CallToolResult:
    """Scroll by pages at element_index or at viewport point x,y (default 400,300).
    Works on tabs that support native input; DOM-only tabs need page_evaluate-free alternatives."""
    return await run(
        """
const t = await getTab(A.tab_id);
const aim = A.element_index != null ? A.element_index : [A.x ?? 400, A.y ?? 300];
await t.scroll(aim, A.direction, A.pages);
out(await after(t, { ok: true }));""",
        {"direction": direction, "pages": pages, "element_index": element_index, "x": x, "y": y,
         "observe": observe},
        title="Scroll",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
async def page_drag(
    from_x: float, from_y: float, to_x: float, to_y: float,
    observe: bool = False, tab_id: str | None = None,
) -> CallToolResult:
    """Drag from one viewport point to another (native input tabs only)."""
    return await run(
        """
const t = await getTab(A.tab_id);
await t.drag([A.from_x, A.from_y], [A.to_x, A.to_y]);
out(await after(t, { ok: true }));""",
        {"from_x": from_x, "from_y": from_y, "to_x": to_x, "to_y": to_y, "observe": observe},
        title="Drag",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
@targeted
async def page_wait(
    ms: int | None = None,
    target: Target = None,
    state: Literal["attached", "detached", "visible", "hidden"] = "visible",
    url: str | None = None,
    load_state: Literal["load", "domcontentloaded", "networkidle"] | None = None,
    timeout_ms: int | None = None,
    tab_id: str | None = None,
) -> CallToolResult:
    """Wait for one condition: ms (fixed delay), target reaching `state`, url, or load_state.
    Normal actions/observations already wait appropriately; use this sparingly. """
    return await run(
        """
const t = await getTab(A.tab_id);
if (A.ms != null) await t.playwright.waitForTimeout(A.ms);
else if (A.target) await loc(t, A.target).waitFor({ state: A.state, ...TO });
else if (A.url) await t.playwright.waitForURL(A.url, TO);
else await t.playwright.waitForLoadState({ state: A.load_state || "load", ...TO });
out(await brief(t));""",
        {"ms": ms, "target": target, "state": state, "url": url, "load_state": load_state,
         "timeout_ms": timeout_ms},
        title="Wait",
        tab=True,
        tab_id=tab_id,
    )


# --------------------------------------------------------------------------
# 4. Screenshots
# --------------------------------------------------------------------------


@mcp.tool()
@targeted
async def page_screenshot(
    full_page: bool = False,
    clip: dict[str, float] | None = None,
    target: Target = None,
    save_path: str | None = None,
    tab_id: str | None = None,
) -> CallToolResult:
    """Screenshot (JPEG) of the viewport, the full page, a clip {x,y,width,height}, or one
    element (target). save_path also writes the image to disk. """
    return await run(
        """
const t = await getTab(A.tab_id);
const o = {};
if (A.full_page) o.fullPage = true;
if (A.clip) o.clip = A.clip;
if (A.target) {
  o.clip = await loc(t, A.target).evaluate((el) => {
    const b = el.getBoundingClientRect();
    return { x: b.x, y: b.y, width: b.width, height: b.height };
  });
}
let bytes;
for (let i = 0; ; i++) {
  try { bytes = await t.screenshot(o); break; }
  catch (e) { if (i >= 2 || !/timed? ?out/i.test(String(e && e.message))) throw e; }
}
await nodeRepl.emitImage({ bytes, mimeType: "image/jpeg" });
out({ ok: true, bytes: bytes.length, ...(await brief(t)) });""",
        {"full_page": full_page, "clip": clip, "target": target},
        title="Screenshot",
        tab=True,
        tab_id=tab_id,
        save_to=save_path,
    )


# --------------------------------------------------------------------------
# 5. Files
# --------------------------------------------------------------------------


@mcp.tool()
@targeted
async def file_upload(
    files: list[str],
    target: dict[str, Any],
    timeout_ms: int = 15000,
    tab_id: str | None = None,
) -> CallToolResult:
    """Upload local files (absolute paths) through the page's file chooser. target is the
    <input type=file> or the button/label that opens the chooser. If it fails on Chrome, enable
    'Allow access to file URLs' for the ChatGPT extension at chrome://extensions. Uploading
    sends file contents to the site: only do it when the user asked. """
    missing = [f for f in files if not Path(f).is_file()]
    if missing:
        raise ValueError(f"Files not found: {missing}")
    return await run(
        """
const t = await getTab(A.tab_id);
const chooserP = t.playwright.waitForEvent("filechooser", { timeoutMs: A.timeout_ms });
chooserP.catch(() => {});
await loc(t, A.target).click(TO);
const chooser = await chooserP;
const multiple = chooser.isMultiple();
if (!multiple && A.files.length > 1) throw new Error("Chooser accepts a single file");
await chooser.setFiles(multiple ? A.files : A.files[0], { timeoutMs: A.timeout_ms });
out({ ok: true, uploaded: A.files, multiple });""",
        {"files": [str(Path(f).resolve()) for f in files], "target": target, "timeout_ms": timeout_ms},
        title="Upload files",
        tab=True,
        tab_id=tab_id,
        timeout_ms=timeout_ms + 30000,
    )


@mcp.tool()
@targeted
async def file_download(
    target: dict[str, Any],
    method: Literal["click", "media"] = "click",
    timeout_ms: int = 120000,
    copy_to: str | None = None,
    tab_id: str | None = None,
) -> CallToolResult:
    """Download a file and return its local path. method=click: click target (a download
    button/link) and wait for the browser download; method=media: download the matched image /
    video / file link directly. copy_to copies the result to that file or folder. """
    res = await run(
        """
const t = await getTab(A.tab_id);
const l = loc(t, A.target);
let path;
if (A.method === "media") {
  path = await l.downloadMedia({ timeoutMs: A.timeout_ms });
} else {
  const dl = t.playwright.waitForEvent("download", { timeoutMs: A.timeout_ms });
  dl.catch(() => {});
  await l.click(TO);
  path = await (await dl).path({ timeoutMs: A.timeout_ms });
}
out({ ok: true, path });""",
        {"target": target, "method": method, "timeout_ms": timeout_ms},
        title="Download file",
        tab=True,
        tab_id=tab_id,
        timeout_ms=timeout_ms + 30000,
    )
    if copy_to and not res.is_error:
        try:
            src = json.loads(res.content[0].text)["path"]
            dest = Path(copy_to).expanduser()
            if dest.is_dir():
                dest = dest / Path(src).name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            res.content.append(_text(f"copied to {dest}"))
        except Exception as e:  # keep the download result even if the copy fails
            res.content.append(_text(f"copy failed: {e}"))
    return res


# --------------------------------------------------------------------------
# 6. Clipboard & dialogs
# --------------------------------------------------------------------------


@mcp.tool()
async def clipboard_read(text_only: bool = True, tab_id: str | None = None) -> CallToolResult:
    """Read the browser clipboard: plain text, or (text_only=false) all items incl. binary."""
    return await run(
        """
const t = await getTab(A.tab_id);
out(A.text_only ? { text: await t.clipboard.readText() } : { items: await t.clipboard.read() });""",
        {"text_only": text_only},
        title="Read clipboard",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
async def clipboard_write(text: str, tab_id: str | None = None) -> CallToolResult:
    """Write plain text to the browser clipboard (overwrites what the user copied)."""
    return await run(
        "const t = await getTab(A.tab_id); await t.clipboard.writeText(A.text); out({ ok: true, chars: A.text.length });",
        {"text": text},
        title="Write clipboard",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
async def dialog_get(tab_id: str | None = None) -> CallToolResult:
    """Check whether a JavaScript dialog (alert/confirm/prompt/beforeunload) is open on the tab."""
    return await run(
        """
const t = await getTab(A.tab_id);
const d = await t.getJsDialog();
out(d ? { open: true, type: d.type, info: Object.fromEntries(Object.entries(d).filter(([, v]) => typeof v !== "function")) } : { open: false });""",
        title="Get dialog",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
async def dialog_handle(
    action: Literal["accept", "dismiss"],
    prompt_text: str | None = None,
    tab_id: str | None = None,
) -> CallToolResult:
    """Accept or dismiss the open dialog. prompt_text is the answer for prompt dialogs. alert and
    beforeunload dialogs can only be dismissed."""
    return await run(
        """
const t = await getTab(A.tab_id);
const d = await t.getJsDialog();
if (!d) throw new Error("No JavaScript dialog is open on this tab");
if (A.action === "dismiss" || d.type === "alert" || d.type === "beforeunload") await d.dismiss();
else if (d.type === "prompt") await d.accept(A.prompt_text ?? "");
else await d.accept();
out({ ok: true, type: d.type, action: A.action });""",
        {"action": action, "prompt_text": prompt_text},
        title="Handle dialog",
        tab=True,
        tab_id=tab_id,
    )


# --------------------------------------------------------------------------
# 7. Tab groups & tab organisation (management capability; Chrome tab ids)
# --------------------------------------------------------------------------


@mcp.tool()
async def tabs_query(filter: dict[str, Any] | None = None) -> CallToolResult:
    """chrome.tabs.query across all windows, e.g. {"url":"https://docs.example.com/*"},
    {"pinned":true}, {"windowId":123}, {"groupId":5}. Returns id, windowId, index, groupId,
    pinned, title, url."""
    return await run(
        """
const mg = await mgmt();
const tabs = await mg.tabs.query(A.filter || {});
out({ count: tabs.length, tabs: tabs.map(({ id, windowId, index, groupId, pinned, active, title, url }) => ({ id, windowId, index, groupId, pinned, active, title, url })) });""",
        {"filter": filter},
        title="Query tabs",
    )


@mcp.tool()
async def tab_group_list(window_id: int | None = None) -> CallToolResult:
    """List tab groups (id, title, color, collapsed, windowId) with the tab ids in each."""
    return await run(
        """
const mg = await mgmt();
const q = A.window_id != null ? { windowId: A.window_id } : {};
const groups = await mg.tabGroups.query(q);
const tabs = await mg.tabs.query(q);
out(groups.map((g) => ({ ...g, tabIds: tabs.filter((t) => t.groupId === g.id).map((t) => t.id) })));""",
        {"window_id": window_id},
        title="List tab groups",
    )


@mcp.tool()
async def tab_group_create(
    tab_ids: list[int],
    title: str | None = None,
    color: Literal["grey", "blue", "red", "yellow", "green", "pink", "purple", "cyan", "orange"] | None = None,
    group_id: int | None = None,
    collapsed: bool | None = None,
) -> CallToolResult:
    """Group tabs (all in the same window) into a new group, or add them to group_id. Optionally
    set the group's title, color and collapsed state."""
    return await run(
        """
const mg = await mgmt();
const gid = await mg.tabs.group({ tabIds: A.tab_ids, ...(A.group_id != null ? { groupId: A.group_id } : {}) });
const props = {};
if (A.title != null) props.title = A.title;
if (A.color) props.color = A.color;
if (A.collapsed != null) props.collapsed = A.collapsed;
if (Object.keys(props).length) await mg.tabGroups.update(gid, props);
out({ groupId: gid, group: await mg.tabGroups.get(gid) });""",
        {"tab_ids": tab_ids, "title": title, "color": color, "group_id": group_id, "collapsed": collapsed},
        title="Group tabs",
    )


@mcp.tool()
async def tab_group_update(
    group_id: int,
    title: str | None = None,
    color: Literal["grey", "blue", "red", "yellow", "green", "pink", "purple", "cyan", "orange"] | None = None,
    collapsed: bool | None = None,
) -> CallToolResult:
    """Rename / recolor / collapse a tab group."""
    return await run(
        """
const mg = await mgmt();
const props = {};
if (A.title != null) props.title = A.title;
if (A.color) props.color = A.color;
if (A.collapsed != null) props.collapsed = A.collapsed;
out(await mg.tabGroups.update(A.group_id, props));""",
        {"group_id": group_id, "title": title, "color": color, "collapsed": collapsed},
        title="Update tab group",
    )


@mcp.tool()
async def tab_group_move(group_id: int, index: int, window_id: int | None = None) -> CallToolResult:
    """Move a tab group to a position (index -1 = end), optionally to another window."""
    return await run(
        """
const mg = await mgmt();
out(await mg.tabGroups.move(A.group_id, { index: A.index, ...(A.window_id != null ? { windowId: A.window_id } : {}) }));""",
        {"group_id": group_id, "index": index, "window_id": window_id},
        title="Move tab group",
    )


@mcp.tool()
async def tab_ungroup(tab_ids: list[int]) -> CallToolResult:
    """Remove tabs from their groups (the tabs stay open)."""
    return await run(
        "const mg = await mgmt(); await mg.tabs.ungroup(A.tab_ids); out({ ok: true, ungrouped: A.tab_ids });",
        {"tab_ids": tab_ids},
        title="Ungroup tabs",
    )


@mcp.tool()
async def tab_pin(tab_ids: list[int], pinned: bool = True) -> CallToolResult:
    """Pin (default) or unpin tabs."""
    return await run(
        """
const mg = await mgmt();
for (const id of A.tab_ids) await mg.tabs.update(id, { pinned: A.pinned });
out({ ok: true, pinned: A.pinned, tab_ids: A.tab_ids });""",
        {"tab_ids": tab_ids, "pinned": pinned},
        title="Pin tabs",
    )


@mcp.tool()
async def tab_move(tab_ids: list[int], index: int, window_id: int | None = None) -> CallToolResult:
    """Move tabs to an index (-1 = end). Omit window_id to reorder in place; set it to transfer
    the tabs to another window."""
    return await run(
        """
const mg = await mgmt();
out(await mg.tabs.move(A.tab_ids, { index: A.index, ...(A.window_id != null ? { windowId: A.window_id } : {}) }));""",
        {"tab_ids": tab_ids, "index": index, "window_id": window_id},
        title="Move tabs",
    )


@mcp.tool()
async def tab_sort(
    by: Literal["title", "url", "host"] = "title",
    window_id: int | None = None,
    tab_ids: list[int] | None = None,
    descending: bool = False,
) -> CallToolResult:
    """Sort tabs by title, url or host. Give window_id (sorts that window's unpinned tabs) or
    tab_ids (sorted into the index slots they already occupy; all in one window). Returns the
    resulting order."""
    if window_id is None and not tab_ids:
        raise ValueError("Give window_id or tab_ids so only the intended tabs are reordered.")
    return await run(
        """
const mg = await mgmt();
const tabs = A.tab_ids && A.tab_ids.length
  ? await Promise.all(A.tab_ids.map((i) => mg.tabs.get(i)))
  : (await mg.tabs.query({ windowId: A.window_id })).filter((x) => !x.pinned);
if (new Set(tabs.map((x) => x.windowId)).size > 1) throw new Error("Tabs must all be in the same window");
const host = (u) => { try { return new URL(u).host; } catch (e) { return u || ""; } };
const key = (x) => A.by === "url" ? (x.url || "") : A.by === "host" ? host(x.url) + "|" + (x.title || "") : (x.title || "");
const slots = tabs.map((x) => x.index).sort((a, b) => a - b);
const sorted = [...tabs].sort((a, b) => key(a).localeCompare(key(b)) * (A.descending ? -1 : 1));
for (let i = 0; i < sorted.length; i++) await mg.tabs.move(sorted[i].id, { index: slots[i] });
const now = await mg.tabs.query({ windowId: tabs[0].windowId });
out({ order: now.sort((a, b) => a.index - b.index).map((x) => ({ id: x.id, index: x.index, title: x.title })) });""",
        {"by": by, "window_id": window_id, "tab_ids": tab_ids, "descending": descending},
        title="Sort tabs",
    )


@mcp.tool()
async def management_call(
    namespace: Literal["windows", "tabs", "tabGroups", "bookmarks"],
    method: str,
    args: list[Any] | None = None,
) -> CallToolResult:
    """Escape hatch for any other Chrome extension API method exposed by the management
    capability, e.g. namespace=tabs method=get args=[123]. Follows the chrome.tabs/windows/
    tabGroups/bookmarks references. Destructive calls need explicit user confirmation first."""
    return await run(
        """
const mg = await mgmt();
const fn = mg[A.namespace] && mg[A.namespace][A.method];
if (typeof fn !== "function") throw new Error("Unavailable: " + A.namespace + "." + A.method);
const r = await fn.apply(mg[A.namespace], A.args || []);
out({ result: r === undefined ? null : r });""",
        {"namespace": namespace, "method": method, "args": args},
        title=f"management {namespace}.{method}",
    )


# --------------------------------------------------------------------------
# 8. Bookmarks & windows
# --------------------------------------------------------------------------


@mcp.tool()
async def bookmark_search(query: str, limit: int = 50) -> CallToolResult:
    """Search bookmarks by title/URL text. Prefer this to reading the whole tree."""
    return await run(
        """
const mg = await mgmt();
const r = await mg.bookmarks.search({ query: A.query });
out({ count: r.length, bookmarks: r.slice(0, A.limit) });""",
        {"query": query, "limit": limit},
        title="Search bookmarks",
    )


@mcp.tool()
async def bookmark_list(parent_id: str | None = None) -> CallToolResult:
    """List the children of a bookmark folder. Without parent_id returns the top-level folders
    (bookmarks bar, other bookmarks...) with their child counts."""
    return await run(
        """
const mg = await mgmt();
const slim = (n) => ({ id: n.id, title: n.title, url: n.url, parentId: n.parentId, index: n.index });
if (A.parent_id) {
  out({ children: (await mg.bookmarks.getChildren(A.parent_id)).map(slim) });
} else {
  const tree = await mg.bookmarks.getTree();
  const roots = (tree[0] && tree[0].children) || [];
  out({ folders: roots.map((r) => ({ ...slim(r), childCount: (r.children || []).length })) });
}""",
        {"parent_id": parent_id},
        title="List bookmarks",
    )


@mcp.tool()
async def bookmark_create(
    title: str, url: str | None = None, parent_id: str | None = None, index: int | None = None
) -> CallToolResult:
    """Create a bookmark (give http/https url) or a folder (omit url)."""
    return await run(
        """
const mg = await mgmt();
const o = { title: A.title };
if (A.url) o.url = A.url;
if (A.parent_id) o.parentId = A.parent_id;
if (A.index != null) o.index = A.index;
out(await mg.bookmarks.create(o));""",
        {"title": title, "url": url, "parent_id": parent_id, "index": index},
        title="Create bookmark",
    )


@mcp.tool()
async def bookmark_move(id: str, parent_id: str | None = None, index: int | None = None) -> CallToolResult:
    """Move a bookmark/folder to another folder and/or position."""
    return await run(
        """
const mg = await mgmt();
const d = {};
if (A.parent_id) d.parentId = A.parent_id;
if (A.index != null) d.index = A.index;
out(await mg.bookmarks.move(A.id, d));""",
        {"id": id, "parent_id": parent_id, "index": index},
        title="Move bookmark",
    )


@mcp.tool()
async def bookmark_update(id: str, title: str | None = None, url: str | None = None) -> CallToolResult:
    """Rename a bookmark/folder or change its URL."""
    return await run(
        """
const mg = await mgmt();
const c = {};
if (A.title != null) c.title = A.title;
if (A.url != null) c.url = A.url;
out(await mg.bookmarks.update(A.id, c));""",
        {"id": id, "title": title, "url": url},
        title="Update bookmark",
    )


@mcp.tool()
async def bookmark_remove(id: str, confirmed: bool = False) -> CallToolResult:
    """Delete a bookmark. DESTRUCTIVE: ask the user for explicit confirmation right before calling
    this, then pass confirmed=true."""
    if not confirmed:
        raise ValueError("Deleting a bookmark needs explicit user confirmation; call again with confirmed=true after asking.")
    return await run(
        "const mg = await mgmt(); await mg.bookmarks.remove(A.id); out({ ok: true, removed: A.id });",
        {"id": id},
        title="Remove bookmark",
    )


@mcp.tool()
async def window_list(populate: bool = False) -> CallToolResult:
    """List browser windows (id, bounds, state, focused). populate=true adds each window's tabs."""
    return await run(
        "const mg = await mgmt(); out(await mg.windows.getAll({ populate: !!A.populate }));",
        {"populate": populate},
        title="List windows",
    )


@mcp.tool()
async def window_create(tab_id: int | None = None) -> CallToolResult:
    """Open a blank window, or move an existing unpinned tab into a new window (moving a
    window's last tab closes the old window)."""
    return await run(
        "const mg = await mgmt(); out(await mg.windows.create(A.tab_id != null ? { tabId: A.tab_id } : {}));",
        {"tab_id": tab_id},
        title="Create window",
    )


@mcp.tool()
async def window_update(
    window_id: int,
    focused: bool | None = None,
    state: Literal["normal", "minimized", "maximized", "fullscreen"] | None = None,
    left: int | None = None,
    top: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> CallToolResult:
    """Focus / resize / move a window. Use state='normal' when setting bounds."""
    return await run(
        """
const mg = await mgmt();
const u = {};
for (const k of ["focused", "state", "left", "top", "width", "height"]) if (A[k] != null) u[k] = A[k];
out(await mg.windows.update(A.window_id, u));""",
        {"window_id": window_id, "focused": focused, "state": state, "left": left, "top": top,
         "width": width, "height": height},
        title="Update window",
    )


# --------------------------------------------------------------------------
# 9. History, logs, audit trail
# --------------------------------------------------------------------------


@mcp.tool()
async def browser_history(
    queries: list[str] | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 50,
) -> CallToolResult:
    """Search browsing history (newest first). Sensitive and may prompt for user approval: call
    it only when needed, once, with date bounds (ISO 8601) and a few query terms."""
    return await run(
        """
const o = { limit: A.limit };
if (A.queries) o.queries = A.queries;
if (A.from_date) o.from = A.from_date;
if (A.to_date) o.to = A.to_date;
const h = await browser.history(o);
out({ count: h.length, entries: h });""",
        {"queries": queries, "from_date": from_date, "to_date": to_date, "limit": limit},
        title="Browser history",
    )


@mcp.tool()
async def page_console_logs(
    filter: str | None = None,
    levels: list[Literal["debug", "info", "log", "warn", "error", "warning"]] | None = None,
    limit: int = 100,
    tab_id: str | None = None,
) -> CallToolResult:
    """Read console messages captured for the tab (level, message, timestamp, source url)."""
    return await run(
        """
const t = await getTab(A.tab_id);
const o = { limit: A.limit };
if (A.filter) o.filter = A.filter;
if (A.levels) o.levels = A.levels;
const logs = await t.dev.logs(o);
out({ count: logs.length, logs });""",
        {"filter": filter, "levels": levels, "limit": limit},
        title="Console logs",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
async def browser_audit_trail(limit: int = 20) -> CallToolResult:
    """Recent model-initiated window/tab/bookmark changes (newest first) with the state before each
    change, useful for undoing organisation changes."""
    return await run(
        """
const mg = await mgmt();
const r = await mg.getAuditTrail();
out({ total: r.changes.length, changes: r.changes.slice(0, A.limit) });""",
        {"limit": limit},
        title="Audit trail",
    )


# --------------------------------------------------------------------------
# 10. Responsive viewport
# --------------------------------------------------------------------------

PRESETS = {
    "mobile": (375, 812),
    "tablet": (768, 1024),
    "laptop": (1366, 768),
    "desktop": (1920, 1080),
}


@mcp.tool()
async def viewport_set(
    width: int | None = None,
    height: int | None = None,
    preset: Literal["mobile", "tablet", "laptop", "desktop"] | None = None,
) -> CallToolResult:
    """Override the viewport size for responsive testing (width+height, or a preset). Call
    viewport_reset when finished."""
    if preset:
        width, height = PRESETS[preset]
    if not width or not height:
        raise ValueError("Give width and height, or a preset.")
    return await run(
        """
const v = await browser.capabilities.get("viewport");
await v.set({ width: A.width, height: A.height });
out({ ok: true, width: A.width, height: A.height });""",
        {"width": width, "height": height},
        title="Set viewport",
    )


@mcp.tool()
async def viewport_reset() -> CallToolResult:
    """Clear the viewport override and return to default sizing."""
    return await run(
        'const v = await browser.capabilities.get("viewport"); await v.reset(); out({ ok: true });',
        title="Reset viewport",
    )


# --------------------------------------------------------------------------
# 11. Page assets
# --------------------------------------------------------------------------


@mcp.tool()
async def page_assets_list(
    kinds: list[Literal["image", "font", "stylesheet", "video", "script", "other"]] | None = None,
    limit: int = 100,
    include_inline_svg: bool = False,
    tab_id: str | None = None,
) -> CallToolResult:
    """Inventory images, fonts, stylesheets, videos (and scripts) the page currently shows. Returns
    an inventory id for page_assets_bundle. Load lazy content first, then list."""
    return await run(
        """
const t = await getTab(A.tab_id);
const cap = await t.capabilities.get("pageAssets");
const inv = await cap.list();
let assets = inv.assets;
if (A.kinds) assets = assets.filter((a) => A.kinds.includes(a.kind));
out({ inventoryId: inv.id, pageUrl: inv.pageUrl, summary: inv.summary, shown: Math.min(assets.length, A.limit), assets: assets.slice(0, A.limit).map(({ id, kind, name, url }) => ({ id, kind, name, url })),
      inlineSvgs: A.include_inline_svg ? inv.inlineSvgs : inv.inlineSvgs.map(({ id, name }) => ({ id, name })) });""",
        {"kinds": kinds, "limit": limit, "include_inline_svg": include_inline_svg},
        title="List page assets",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
async def page_assets_bundle(
    asset_ids: list[str] | None = None,
    kinds: list[Literal["image", "font", "stylesheet", "video"]] | None = None,
    inventory_id: str | None = None,
    tab_id: str | None = None,
) -> CallToolResult:
    """Export discovered assets to a local temp folder; returns file paths + manifest. Use kinds
    for broad grabs or asset_ids (from page_assets_list) for specific ones. Without inventory_id a
    fresh inventory is taken. Needs the runtime's file-system grant: without it the bundle step fails
    with "EPERM ... mkdir ...browser-use" (a runtime sandbox limit, not an OS error)."""
    return await run(
        """
const t = await getTab(A.tab_id);
const cap = await t.capabilities.get("pageAssets");
const inventoryId = A.inventory_id || (await cap.list()).id;
const o = { inventoryId };
if (A.asset_ids) o.assetIds = A.asset_ids;
if (A.kinds) o.kinds = A.kinds;
out(await cap.bundle(o));""",
        {"asset_ids": asset_ids, "kinds": kinds, "inventory_id": inventory_id},
        title="Bundle page assets",
        tab=True,
        tab_id=tab_id,
        timeout_ms=180000,
    )


# --------------------------------------------------------------------------
# 12. Content export
# --------------------------------------------------------------------------


@mcp.tool()
async def content_export_gsuite(
    type: Literal["pdf", "md", "xlsx", "csv", "docx", "pptx"], tab_id: str | None = None
) -> CallToolResult:
    """Export the Google Docs/Sheets/Slides tab in the given supported format; returns the local
    file path."""
    return await run(
        "const t = await getTab(A.tab_id); out({ path: await t.content.exportGsuite(A.type) });",
        {"type": type},
        title=f"Export Google Workspace as {type}",
        tab=True,
        tab_id=tab_id,
        timeout_ms=180000,
    )


@mcp.tool()
async def content_export_youtube_transcript(tab_id: str | None = None) -> CallToolResult:
    """Export the transcript of a youtube.com/watch tab to a UTF-8 .txt file; returns its path."""
    return await run(
        "const t = await getTab(A.tab_id); out({ path: await t.content.exportYouTubeTranscript() });",
        title="Export YouTube transcript",
        tab=True,
        tab_id=tab_id,
        timeout_ms=180000,
    )


# --------------------------------------------------------------------------
# 13. CDP
# --------------------------------------------------------------------------


@mcp.tool()
async def cdp_send(
    method: str,
    params: dict[str, Any] | None = None,
    target: dict[str, str] | None = None,
    timeout_ms: int | None = None,
    tab_id: str | None = None,
) -> CallToolResult:
    """Send a raw Chrome DevTools Protocol command to the tab (scoped to its current http(s)
    origin; navigate there first). target={"sessionId"|"targetId": ...} addresses an attached child
    target. Prefer the higher-level tools; tell the user about any lasting change made via CDP.
    Raw CDP is behind Browser Use's approval gate: when no approver is available it fails with
    "CDP approval gate did not grant access" and must not be worked around."""
    return await run(
        """
const t = await getTab(A.tab_id);
const cdp = await t.capabilities.get("cdp");
await cdp.documentation();
const o = {};
if (A.target) o.target = A.target;
if (A.timeout_ms) o.timeoutMs = A.timeout_ms;
out({ result: await cdp.send(A.method, A.params || {}, o) });""",
        {"method": method, "params": params, "target": target, "timeout_ms": timeout_ms},
        title=f"CDP {method}",
        tab=True,
        tab_id=tab_id,
    )


@mcp.tool()
async def cdp_events(
    after_sequence: int | None = None,
    methods: list[str] | None = None,
    limit: int = 100,
    timeout_ms: int | None = None,
    target: dict[str, str] | None = None,
    tab_id: str | None = None,
) -> CallToolResult:
    """Read buffered CDP events. Call once without after_sequence to get the current cursor, do the
    action, then call again with after_sequence=cursor. Continue while hasMore is true."""
    return await run(
        """
const t = await getTab(A.tab_id);
const cdp = await t.capabilities.get("cdp");
await cdp.documentation();
const o = { limit: A.limit };
if (A.after_sequence != null) o.afterSequence = A.after_sequence;
if (A.methods) o.methods = A.methods;
if (A.timeout_ms) o.timeoutMs = A.timeout_ms;
if (A.target) o.target = A.target;
out(await cdp.readEvents(o));""",
        {"after_sequence": after_sequence, "methods": methods, "limit": limit,
         "timeout_ms": timeout_ms, "target": target},
        title="CDP events",
        tab=True,
        tab_id=tab_id,
        timeout_ms=(timeout_ms or 0) + 60000,
    )


# --------------------------------------------------------------------------
# 14. Session lifecycle
# --------------------------------------------------------------------------


@mcp.tool()
async def session_name(name: str) -> CallToolResult:
    """Name the browser automation session (short, starting with an emoji, e.g. '🔎 Research')."""
    return await run(
        "await browser.nameSession(A.name); out({ ok: true, name: A.name });",
        {"name": name},
        title="Name session",
    )


@mcp.tool()
async def tab_mark(
    kind: Literal["deliverable", "handoff"], tab_id: str | None = None
) -> CallToolResult:
    """Mark a tab so it survives cleanup: deliverable = the live page is the requested output;
    handoff = work continues from it later (login, approval, CAPTCHA, payment...)."""
    return await run(
        """
const t = await getTab(A.tab_id);
if (A.kind === "deliverable") await t.markDeliverable(); else await t.markHandoff();
out({ ok: true, kind: A.kind, ...(await brief(t)) });""",
        {"kind": kind},
        title=f"Mark tab {kind}",
        tab=True,
        tab_id=tab_id,
    )


if __name__ == "__main__":
    mcp.run("stdio")
