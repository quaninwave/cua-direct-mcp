#!/usr/bin/env python3
import argparse
import base64
import csv
import datetime as _dt
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: never flash/keep a console window
PROXY = HERE / "cua_proxy.py"
STATE_DIR = Path(os.environ.get("CUA_STATE_DIR", Path.home() / ".cua-chrome"))
STATE_FILE = STATE_DIR / "state.json"
DAEMON_FILE = STATE_DIR / "daemon.json"
SCRATCH_DIR = STATE_DIR / "scratch"          # default home of analysis-only files (screenshots without --out)
LOG_FILE = STATE_DIR / "log.jsonl"           # one line per command (name only, never arguments)
SCRATCH_HOURS = float(os.environ.get("CUA_SCRATCH_HOURS", "24"))
LOG_DAYS = float(os.environ.get("CUA_LOG_DAYS", "7"))
IDLE_SECONDS = int(float(os.environ.get("CUA_IDLE_MINUTES", "20")) * 60)
SENTINEL = "@@CUA@@"
MAX_OUT = int(os.environ.get("CUA_MAX_OUTPUT_CHARS", "60000"))
GUARD_OFF = os.environ.get("CUA_NO_GUARD") == "1"
RISK_SRC = r"\b(send|submit|post|publish|share|pay|buy|purchase|order|checkout|place order|confirm|delete|remove|unsubscribe|subscribe|sign up|create account|register|transfer|donate|apply|book|reserve|gửi|đăng|xóa|xoá|mua|thanh toán|đặt hàng|xác nhận|hủy|đăng ký)\b"

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


# ------------------------------------------------------------------ state
def state_path(task):
    return STATE_FILE if task in (None, "", "default") else STATE_DIR / f"state-{task}.json"


def load_state(path=None):
    try:
        return json.loads((path or STATE_FILE).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_state(st, path=None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (path or STATE_FILE).write_text(json.dumps(st, indent=1), encoding="utf-8")


# ------------------------------------------------------------------ runtime (MCP client over the proxy)
class Runtime:
    def __init__(self, session_id):
        env = dict(os.environ)
        env["CUA_DIRECT_SESSION_ID"] = session_id
        self.proc = subprocess.Popen([sys.executable, str(PROXY)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env, bufsize=0, creationflags=NO_WINDOW)
        self.q = queue.Queue()
        self.next_id = 1
        threading.Thread(target=self._pump, daemon=True).start()
        self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                 "clientInfo": {"name": "cua-cli", "version": "0.1"}}, timeout=90)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _pump(self):
        for line in iter(self.proc.stdout.readline, b""):
            try:
                self.q.put(json.loads(line))
            except ValueError:
                pass
        self.q.put(None)

    def _send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _rpc(self, method, params, timeout):
        rid = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        end = time.time() + timeout
        while True:
            left = end - time.time()
            if left <= 0:
                raise TimeoutError(f"{method} timed out after {timeout}s")
            try:
                m = self.q.get(timeout=left)
            except queue.Empty:
                raise TimeoutError(f"{method} timed out after {timeout}s") from None
            if m is None:
                raise RuntimeError("proxy exited (is the Codex unified-computer-use plugin installed? run: cua.py doctor)")
            if m.get("id") == rid:
                if "error" in m:
                    raise RuntimeError(json.dumps(m["error"])[:400])
                return m.get("result", {})

    def call_js(self, code, title, timeout_ms):
        r = self._rpc("tools/call", {"name": "js", "arguments": {"code": code, "title": title[:80] or "cua", "timeout_ms": timeout_ms}},
                      timeout=timeout_ms / 1000 + 45)
        return r

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.proc.kill()


# ------------------------------------------------------------------ background daemon
# The Chrome runtime ties tabs to the live runtime process: when it exits, the tabs are cleaned up and cannot be re-claimed.
# So by default one background daemon keeps a single runtime alive and every `cua.py` call talks to it over localhost
# (token protected). It exits after CUA_IDLE_MINUTES (default 20) without requests, or on `cua.py stop`.
def _read_line(sock):
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = sock.recv(1 << 20)
        if not chunk:
            break
        buf += chunk
    return buf


def _daemon_request(info, req, timeout):
    with socket.create_connection(("127.0.0.1", info["port"]), timeout=5) as sk:
        sk.settimeout(timeout)
        sk.sendall((json.dumps({**req, "token": info["token"]}) + "\n").encode("utf-8"))
        line = _read_line(sk)
    if not line:
        raise ConnectionError("daemon closed the connection")
    return json.loads(line)


def daemon_info():
    try:
        return json.loads(DAEMON_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def daemon_alive(info):
    try:
        return bool(info) and _daemon_request(info, {"op": "ping"}, 5).get("ok")
    except Exception:  # noqa: BLE001
        return False


def daemon_main(session_id):
    rt = Runtime(session_id)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    srv.settimeout(1.0)
    token = secrets.token_hex(16)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DAEMON_FILE.write_text(json.dumps({"port": srv.getsockname()[1], "token": token, "pid": os.getpid()}), encoding="utf-8")
    last = time.time()
    try:
        while time.time() - last < IDLE_SECONDS:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            with conn:
                try:
                    req = json.loads(_read_line(conn))
                    if req.get("token") != token:
                        resp = {"ok": False, "error": "bad token"}
                    elif req.get("op") == "ping":
                        resp = {"ok": True}
                    elif req.get("op") == "stop":
                        conn.sendall(b'{"ok":true}\n')
                        break
                    else:
                        try:
                            res = rt.call_js(req["code"], req.get("title", "cua"), req["timeout_ms"])
                        except (RuntimeError, OSError) as e:
                            if not (isinstance(e, OSError) or "proxy exited" in str(e)):
                                raise
                            # the runtime process died: start a fresh one and retry this call once
                            try:
                                rt.close()
                            except Exception:  # noqa: BLE001
                                pass
                            rt = Runtime(session_id)
                            try:
                                res = rt.call_js(req["code"], req.get("title", "cua"), req["timeout_ms"])
                            except Exception as e2:  # noqa: BLE001
                                raise RuntimeError("the browser runtime had crashed and was restarted (tabs opened before are gone; "
                                                   f"re-run `open`, or `use TAB_ID` for a tab that still exists): {e2}") from None
                        resp = {"ok": True, "result": res}
                        if res.get("isError") and "kernel reset" in json.dumps(res):
                            rt.close()
                            rt = Runtime(session_id)
                except Exception as e:  # noqa: BLE001
                    resp = {"ok": False, "error": str(e)[:600]}
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
            last = time.time()
    finally:
        rt.close()
        try:
            DAEMON_FILE.unlink()
        except OSError:
            pass


def start_daemon(session_id):
    flags = (NO_WINDOW | 0x00000200) if os.name == "nt" else 0  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP (no visible console)
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_daemon"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags, env={**os.environ, "CUA_DAEMON_SESSION": session_id})
    end = time.time() + 120
    while time.time() < end:
        info = daemon_info()
        if daemon_alive(info):
            return info
        time.sleep(0.5)
    raise SystemExit("could not start the background runtime (run `cua.py doctor`; or use --no-daemon)")


class RemoteRuntime:
    def __init__(self, info):
        self.info = info

    def call_js(self, code, title, timeout_ms):
        r = _daemon_request(self.info, {"op": "js", "code": code, "title": title, "timeout_ms": timeout_ms}, timeout_ms / 1000 + 60)
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "daemon error"))
        return r["result"]

    def close(self):
        pass


# ------------------------------------------------------------------ JS bridge
PRELUDE = r"""
const A = __ARGS__;
const out = (v) => nodeRepl.write("@@CUA@@" + JSON.stringify(v === undefined ? null : v));
// Browser ids are handed out per runtime start and change; the extension instance id is stable, so `ext:<prefix>` is supported.
const resolveBrowser = async (spec) => {
  if (!spec) return null;
  if (String(spec).startsWith("ext:")) {
    const pre = String(spec).slice(4);
    const hit = (await cua.listBrowsers({ emit: false })).find((b) => b.metadata && (b.metadata.extensionInstanceId || "").startsWith(pre));
    if (!hit) throw new Error("No connected browser with extension instance " + pre + " (is that Chrome profile open with the ChatGPT extension enabled?)");
    return await cua.getBrowser({ id: hit.id });
  }
  return await cua.getBrowser({ id: spec });
};
const browser = await resolveBrowser(A.browser);
const BID = browser ? browser.browserId : null;
const getTab = (id) => cua.getTab(String(id), { browser: BID });
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
  else throw new Error("target needs one of: --selector, --role(+--name), --text, --label, --placeholder, --test-id");
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


def wrap(body, args):
    pre = PRELUDE.replace("__ARGS__", json.dumps(args, ensure_ascii=False))
    return ("{\ntry {\n" + pre + body +
            '\n} catch (e) { nodeRepl.write("@@CUA@@" + JSON.stringify({ __error: String((e && e.message) || e) })); }\n}')


def extract(result):
    """Return (payload, images[(mime, b64)])."""
    dec = json.JSONDecoder()
    data, found, images = None, False, []
    for c in result.get("content", []):
        if c.get("type") == "image":
            images.append((c.get("mimeType", "image/jpeg"), c.get("data", "")))
        elif c.get("type") == "text":
            t = c.get("text", "")
            i = t.find(SENTINEL)
            if i >= 0:
                try:
                    data, _ = dec.raw_decode(t, i + len(SENTINEL))
                    found = True
                except ValueError:
                    pass
    if not found:
        texts = " | ".join(c.get("text", "")[-300:] for c in result.get("content", []) if c.get("type") == "text")
        data = {"__error": "no result from runtime: " + texts[-700:]}
    return data, images


class Ctx:
    """Per-process context shared by all commands (browser, current tab, runtime)."""

    def __init__(self, ns):
        # `session_id` (runtime identity) is global; the current tab/browser/opened-tab list is per task (--task NAME or CUA_TASK)
        gst = load_state()
        if "session_id" not in gst:
            gst["session_id"] = str(uuid.uuid4())
            save_state(gst)
        self.session_id = gst["session_id"]
        self.task = getattr(ns, "task", None) or os.environ.get("CUA_TASK") or "default"
        self.state_file = state_path(self.task)
        st = gst if self.state_file == STATE_FILE else load_state(self.state_file)
        self.state = st
        self.browser = ns.browser or os.environ.get("CUA_BROWSER") or st.get("browser") or gst.get("browser") or "chrome"
        self.tab_override = ns.tab
        self.no_daemon = bool(getattr(ns, "no_daemon", False))
        self.rt = None

    def save(self):
        save_state(self.state, self.state_file)

    def register(self, path, kind, keep=False):
        """Record a file/folder this tool created. keep=False means scratch: `cleanup`/`stop` delete it."""
        lst = [a for a in self.state.get("artifacts", []) if a["path"] != str(path)]
        lst.append({"path": str(path), "kind": kind, "keep": bool(keep), "ts": int(time.time())})
        self.state["artifacts"] = lst
        self.save()

    def for_task(self, task):
        """A sibling context for another task, sharing this process's runtime."""
        ns2 = argparse.Namespace(browser=None, tab=None, task=task, no_daemon=self.no_daemon)
        c = Ctx(ns2)
        c.rt = self.runtime()
        return c

    def runtime(self):
        if self.rt is None:
            if self.no_daemon:
                self.rt = Runtime(self.session_id)
            else:
                info = daemon_info()
                if not daemon_alive(info):
                    info = start_daemon(self.session_id)
                self.rt = RemoteRuntime(info)
        return self.rt

    def tab(self):
        t = self.tab_override or self.state.get("tab")
        if not t:
            raise SystemExit("No tab selected: run `cua.py open URL` or `cua.py use TAB_ID` first (or pass --tab).")
        return str(t)

    def set_tab(self, tid):
        self.state["tab"] = str(tid)
        self.state["browser"] = self.browser
        self.save()

    def track_opened(self, tid):
        """Remember tabs this tool opened so they can be closed when the task is done."""
        lst = [o for o in self.state.get("opened", []) if o["id"] != str(tid)]
        lst.append({"id": str(tid), "browser": self.browser})
        self.state["opened"] = lst
        self.save()

    def untrack(self, tid):
        self.state["opened"] = [o for o in self.state.get("opened", []) if o["id"] != str(tid)]
        self.save()


def _run_js_once(ctx, ns, body, args=None, title="cua", tab=False, timeout_ms=60000, save_shot=None):
    args = dict(args or {})
    args["browser"] = ctx.browser
    args["observe"] = bool(getattr(ns, "observe", False))
    to = getattr(ns, "timeout", None)
    if to:
        args["timeout_ms"] = int(to)
    if tab:
        args["tab_id"] = ctx.tab()
    result = ctx.runtime().call_js(wrap(body, args), title, timeout_ms)
    if result.get("isError"):
        raise SystemExit("runtime error: " + " ".join(c.get("text", "") for c in result.get("content", []))[-800:])
    data, images = extract(result)
    if isinstance(data, dict) and "__error" in data:
        raise SystemExit("error: " + data["__error"])
    if images and save_shot is not None:
        scratch = not save_shot
        p = Path(save_shot) if save_shot else SCRATCH_DIR / ctx.task / f"shot-{int(time.time() * 1000)}.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(base64.b64decode(images[0][1]))
        ctx.register(p, "screenshot", keep=not scratch)  # no --out: scratch (deleted by cleanup); --out: the user's file, kept
        data = {**data, "saved_to": str(p), **({"scratch": True} if scratch else {})} if isinstance(data, dict) else data
    return data


TRANSIENT = ("Debugger unattached", "Debugger detached", "Detached while", "CDP command", "Execution context was destroyed", "Target closed", "not attached to")
LOCATOR_FAIL = re.compile(r"waiting for|selector|resolved to|strict mode|no element|not found|locator", re.I)


def run_js(ctx, ns, body, args=None, title="cua", tab=False, timeout_ms=60000, save_shot=None, retry=0):
    """Run JS; retry transient browser errors (read-only commands only) and add locator suggestions to failures."""
    attempt = 0
    while True:
        try:
            return _run_js_once(ctx, ns, body, args, title, tab, timeout_ms, save_shot)
        except SystemExit as e:
            msg = str(e)
            if attempt < retry and any(k in msg for k in TRANSIENT):
                attempt += 1
                time.sleep(1.5 * attempt)
                continue
            if args and args.get("target") and LOCATOR_FAIL.search(msg) and not msg.startswith(("needs confirmation", "RISKY")):
                hint = suggest_locators(ctx, ns, args["target"])
                if hint:
                    msg += "\n" + hint
            if "RISKY:" in msg:
                msg = "needs confirmation: " + msg[msg.index("RISKY:") + 6:].strip() + " Ask the user for an explicit go-ahead, then re-run with --confirmed."
            raise SystemExit(msg) from None


def suggest_locators(ctx, ns, target):
    """After a locator failed, list the closest interactive elements as ready-to-paste flags."""
    import re as _re
    words = set()
    for k in ("text", "name", "label", "placeholder", "has_text", "test_id", "selector", "role"):
        v = target.get(k)
        if isinstance(v, str):
            words.update(w.lower() for w in _re.split(r"[^\w]+", v) if len(w) >= 3)
    full = " ".join(str(target[k]) for k in ("text", "name", "label", "placeholder", "has_text") if isinstance(target.get(k), str)).lower()
    if not words and not full:
        return ""
    try:
        res = _run_js_once(ctx, ns, r"""
const t = await getTab(A.tab_id);
const items = await rootOf(t, A.frame).locator("a,button,input,select,textarea,summary,label,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[tabindex]").evaluateAll((els, o) => {
  const words = o.words; const full = o.full;
  const dice = (a, b) => { if (a.length < 2 || b.length < 2) return 0; const bg = (x) => { const m = {}; for (let i = 0; i < x.length - 1; i++) { const g = x.slice(i, i + 2); m[g] = (m[g] || 0) + 1; } return m; }; const P = bg(a), Q = bg(b); let n = 0; for (const g in P) if (Q[g]) n += Math.min(P[g], Q[g]); return 2 * n / (a.length - 1 + b.length - 1); };
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const out = [];
  for (const e of els) {
    let r; try { r = e.getBoundingClientRect(); } catch (x) { continue; }
    if (!(r.width > 0 && r.height > 0)) continue;
    const text = norm(e.innerText || e.textContent || e.getAttribute("value") || "");
    const hay = (text + " " + (e.getAttribute("aria-label") || "") + " " + (e.getAttribute("placeholder") || "") + " " + (e.getAttribute("name") || "") + " " + (e.id || "") + " " + (e.getAttribute("title") || "")).toLowerCase();
    let score = 0; for (const w of words) if (hay.includes(w)) score++;
    let best = 0; for (const f of [text, e.getAttribute("aria-label") || "", e.getAttribute("placeholder") || "", e.getAttribute("name") || "", e.id || "", e.getAttribute("title") || ""]) if (f) best = Math.max(best, dice(full, f.toLowerCase()));
    if (best >= 0.4) score += best * 3;
    if (score <= 0) continue;
    const tag = e.tagName.toLowerCase(); const name = norm(e.getAttribute("aria-label") || text).slice(0, 40);
    let flag = null;
    if (e.id) flag = "--selector #" + e.id;
    else if (tag === "a" && name) flag = '--role link --name "' + name + '"';
    else if ((tag === "button" || e.getAttribute("role") === "button") && name) flag = '--role button --name "' + name + '"';
    else if ((tag === "input" || tag === "textarea") && e.getAttribute("placeholder")) flag = '--placeholder "' + e.getAttribute("placeholder") + '"';
    else if (name) flag = '--text "' + name + '"';
    if (flag) out.push({ score, tag, text: text.slice(0, 50), flag });
  }
  out.sort((a, b) => b.score - a.score);
  return out.slice(0, 5);
}, { words: A.words, full: A.full });
out({ items });""", {"words": sorted(words), "full": full, "frame": target.get("frame"), "tab_id": ctx.tab()}, title="suggest", tab=False)
    except SystemExit:
        return ""
    items = (res or {}).get("items") or []
    if not items:
        return "hint: no visible element looks similar; run `cua.py look` or `cua.py ax` to see what is on the page"
    return "hint: closest elements -> " + " | ".join(f"{i['tag']} \"{i['text']}\": {i['flag']}" for i in items)


def _emit_plain(data):
    if isinstance(data, dict) and set(data) == {"__text"}:
        s = data["__text"]
    else:
        s = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(s) > MAX_OUT:
        s = s[:MAX_OUT] + f"\n...[truncated {len(s) - MAX_OUT} chars; narrow the request or set CUA_MAX_OUTPUT_CHARS]"
    print(s)


# ------------------------------------------------------------------ site notes (memory per website)
NOTES_DIR = STATE_DIR / "notes"


def host_of(url):
    m = re.match(r"https?://([^/:?#]+)", url or "")
    h = (m.group(1) if m else (url or "")).lower()
    return h[4:] if h.startswith("www.") else h


def site_note(url):
    f = NOTES_DIR / f"{host_of(url)}.md"
    try:
        return f.read_text(encoding="utf-8").strip()[:800] if f.is_file() else ""
    except OSError:
        return ""


def emit(data):  # noqa: F811 - wraps the plain emitter below so site notes surface automatically
    if isinstance(data, dict) and data.get("url") and data.get("title") is not None and "site_notes" not in data:
        n = site_note(data["url"])
        if n:
            data = {**data, "site_notes": n}
    _emit_plain(data)


# ------------------------------------------------------------------ locator flags
LOC_FIELDS = ("selector", "role", "name", "text", "label", "placeholder", "test_id", "frame", "nth", "has_text")


def _need(ns, what):
    if not getattr(ns, "confirmed", False) and not GUARD_OFF:
        raise SystemExit("needs confirmation: " + what + " Ask the user for an explicit go-ahead, then re-run with --confirmed.")


def add_confirm(sp, text="the user explicitly confirmed this action"):
    sp.add_argument("--confirmed", action="store_true", help=text)


def add_locator(sp, required=False):
    g = sp.add_argument_group("locator (pick one of selector/role/text/label/placeholder/test-id)")
    g.add_argument("--selector", "-s", help="CSS selector")
    g.add_argument("--role", help="ARIA role, e.g. button (combine with --name)")
    g.add_argument("--name", help="accessible name for --role")
    g.add_argument("--text", help="visible text")
    g.add_argument("--label", help="form label text")
    g.add_argument("--placeholder", help="placeholder text")
    g.add_argument("--test-id", dest="test_id", help="data-testid value")
    g.add_argument("--frame", help="CSS selector of an iframe to search inside")
    g.add_argument("--nth", type=int, help="0-based index among matches")
    g.add_argument("--has-text", dest="has_text", help="narrow to elements containing this text")
    g.add_argument("--exact", action="store_true", help="exact text match")
    g.add_argument("--regex", action="store_true", help="treat text/name/label/placeholder as regex (case-insensitive)")


def target_of(ns):
    t = {k: getattr(ns, k) for k in LOC_FIELDS if getattr(ns, k, None) is not None}
    if getattr(ns, "exact", False):
        t["exact"] = True
    if getattr(ns, "regex", False):
        t["regex"] = True
    return t or None


def common(sp, observe=False):
    sp.add_argument("--timeout", type=int, help="locator/action timeout in ms")
    if observe:
        sp.add_argument("--observe", action="store_true", help="also return the fresh accessibility tree")


# ------------------------------------------------------------------ commands
COMMANDS = {}


def command(name, help_, setup=None):
    def deco(fn):
        COMMANDS[name] = (fn, help_, setup)
        return fn
    return deco


@command("doctor", "check that the plugin runtime, proxy and browser are reachable")
def c_doctor(ctx, ns):
    info = {"python": sys.version.split()[0], "proxy": str(PROXY), "state_file": str(STATE_FILE)}
    sys.path.insert(0, str(HERE))
    try:
        import cua_proxy
        spec = cua_proxy.find_spec_path()
        info["plugin_spec"] = str(spec)
        cmd, _, _ = cua_proxy.build_child(spec)
        info["node"] = cmd
        info["node_exists"] = Path(cmd).exists()
    except Exception as e:  # noqa: BLE001
        info["plugin_error"] = str(e)
        emit(info)
        raise SystemExit(1)
    info["browsers"] = run_js(ctx, ns, "out(await cua.listBrowsers({ emit: false }));", title="doctor")
    emit(info)


@command("start", "start the background runtime now (also happens automatically on first use)")
def c_start(ctx, ns):
    info = daemon_info()
    if not daemon_alive(info):
        info = start_daemon(ctx.session_id)
    emit({"running": True, "pid": info["pid"], "idle_minutes": IDLE_SECONDS // 60})


def _rm(path):
    """Delete a file or folder; returns freed bytes (0 if it was already gone)."""
    path = Path(path)
    try:
        if path.is_file():
            n = path.stat().st_size
            path.unlink()
            return n
        if path.is_dir():
            n = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            shutil.rmtree(path)
            return n
    except OSError:
        return -1
    return 0


def _prune_empty(parents):
    for d in sorted(set(parents), key=lambda x: -len(str(x))):
        try:
            if d.is_dir() and not any(d.iterdir()) and (d.name in ("videos", "frames") or SCRATCH_DIR in d.parents or d == SCRATCH_DIR):
                d.rmdir()
        except OSError:
            pass


def clean_artifacts(ctx, include_kept=False):
    """Delete every scratch file this task created (screenshots, videos, frame tiles, scratch downloads)."""
    deleted = freed = 0
    errors, kept, parents = [], 0, []
    remaining = []
    for a in ctx.state.get("artifacts", []):
        if a["keep"] and not include_kept:
            kept += 1
            remaining.append(a)
            continue
        existed = Path(a["path"]).exists()
        n = _rm(a["path"])
        parents.append(Path(a["path"]).parent)
        if n < 0:
            errors.append(a["path"])
            remaining.append(a)
        elif Path(a["path"]).exists():
            remaining.append(a)
        elif existed:
            deleted += 1
            freed += n
    ctx.state["artifacts"] = remaining
    ctx.save()
    _prune_empty(parents + [SCRATCH_DIR / ctx.task])
    return {"files_deleted": deleted, "bytes_freed": freed, "kept": kept, **({"errors": errors} if errors else {})}


def gc_scratch(max_age_hours=None):
    """Delete scratch files older than N hours (any task) and trim the log. Runs at the start of every command."""
    cutoff = time.time() - 3600 * (SCRATCH_HOURS if max_age_hours is None else max_age_hours)
    removed = freed = 0
    if SCRATCH_DIR.is_dir():
        for f in list(SCRATCH_DIR.rglob("*")):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    freed += f.stat().st_size
                    f.unlink()
                    removed += 1
            except OSError:
                pass
        for d in sorted([x for x in SCRATCH_DIR.rglob("*") if x.is_dir()], key=lambda x: -len(str(x))):
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass
    return removed, freed


def prune_log(force_all=False):
    if not LOG_FILE.is_file():
        return 0
    if force_all:
        n = len(LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines())
        LOG_FILE.unlink()
        return n
    if LOG_FILE.stat().st_size < 200_000:
        return 0
    keep, drop = [], 0
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=LOG_DAYS)).isoformat()
    for line in LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            (keep if json.loads(line)["t"] >= cutoff else []).append(line)
        except (ValueError, KeyError):
            continue
    keep = keep[-2000:]
    drop = max(0, len(LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()) - len(keep))
    LOG_FILE.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    return drop


def log_command(task, cmd, ok):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": _dt.datetime.now().isoformat(timespec="seconds"), "task": task, "cmd": cmd, "ok": ok}) + "\n")
    except OSError:
        pass


def s_cleanup(sp):
    sp.add_argument("--all-tasks", action="store_true", help="also clean the tabs and files recorded by every other task")
    sp.add_argument("--keep-files", action="store_true", help="close tabs only; keep scratch files")
    sp.add_argument("--logs", action="store_true", help="also delete the command log now")


def s_stop(sp):
    sp.add_argument("--keep-tabs", action="store_true")
    sp.add_argument("--keep-files", action="store_true", help="do not delete scratch files")


def close_opened_tabs(ctx, ns):
    """Close every tab this tool opened (tracked in state). Returns a summary dict."""
    closed, failed = [], []
    saved_browser, saved_tab = ctx.browser, ctx.tab_override
    for o in list(ctx.state.get("opened", [])):
        ctx.browser, ctx.tab_override = o["browser"], o["id"]
        try:
            run_js(ctx, ns, """
const id = A.tab_id;
try { const t = await getTab(id); await t.close(); }
catch (e) { const mg = await mgmt(); await mg.tabs.remove(Number(id)); }
out({ closed: id });""", title="cleanup", tab=True)
            closed.append(o["id"])
        except SystemExit as e:
            msg = str(e)
            if "not found" in msg.lower() or "no tab with id" in msg.lower():
                closed.append(o["id"])  # the tab no longer exists: already clean
            else:
                failed.append({"id": o["id"], "error": msg[:120]})
    ctx.browser, ctx.tab_override = saved_browser, saved_tab
    still_open = {f["id"] for f in failed}
    ctx.state["opened"] = [o for o in ctx.state.get("opened", []) if o["id"] in still_open]
    if ctx.state.get("tab") in closed:
        ctx.state.pop("tab", None)
    ctx.save()
    return {"closed": closed, "failed": failed}


@command("cleanup", "task finished: close every tab this tool opened AND delete its scratch files (screenshots, videos, frames); --logs also drops the log", s_cleanup)
def c_cleanup(ctx, ns):
    def one(c):
        r = close_opened_tabs(c, ns)
        if not ns.keep_files:
            r["files"] = clean_artifacts(c)
        return r
    res = one(ctx)
    if ns.all_tasks:
        others = {}
        for f in STATE_DIR.glob("state-*.json"):
            name = f.stem[len("state-"):]
            if name != ctx.task:
                others[name] = one(ctx.for_task(name))
        res = {"this_task": res, "other_tasks": others}
    if ns.logs:
        res["log_entries_deleted"] = prune_log(force_all=True)
    emit(res)


def s_artifacts(sp):
    sp.add_argument("action", choices=["list", "keep", "drop", "gc"], nargs="?", default="list")
    sp.add_argument("path", nargs="?", help="file for keep/drop")
    sp.add_argument("--hours", type=float, help="gc: delete scratch files older than this (default CUA_SCRATCH_HOURS=24)")


@command("artifacts", "files this tool created: list | keep PATH (protect from cleanup) | drop PATH (delete now) | gc (delete old scratch and trim the log)", s_artifacts)
def c_artifacts(ctx, ns):
    if ns.action == "gc":
        n, freed = gc_scratch(ns.hours)
        emit({"scratch_files_deleted": n, "bytes_freed": freed, "log_entries_trimmed": prune_log()})
        return
    arts = ctx.state.get("artifacts", [])
    if ns.action in ("keep", "drop"):
        if not ns.path:
            raise SystemExit(f"artifacts {ns.action} needs a PATH")
        want = str(Path(ns.path))
        hit = [a for a in arts if a["path"] == want]
        if not hit:
            raise SystemExit("not a tracked artifact: " + want)
        if ns.action == "keep":
            hit[0]["keep"] = True
            ctx.save()
            emit({"kept": want})
        else:
            freed = _rm(want)
            ctx.state["artifacts"] = [a for a in arts if a["path"] != want]
            ctx.save()
            emit({"deleted": want, "bytes_freed": max(freed, 0)})
        return
    now = time.time()
    rows = []
    for a in arts:
        f = Path(a["path"])
        rows.append({"path": a["path"], "kind": a["kind"], "keep": a["keep"], "exists": f.exists(),
                     "mb": round(f.stat().st_size / 1e6, 2) if f.is_file() else None, "age_min": int((now - a["ts"]) / 60)})
    scratch_bytes = sum(f.stat().st_size for f in SCRATCH_DIR.rglob("*") if f.is_file()) if SCRATCH_DIR.is_dir() else 0
    emit({"task": ctx.task, "tracked": rows, "scratch_dir": str(SCRATCH_DIR), "scratch_mb": round(scratch_bytes / 1e6, 2),
          "log_kb": round(LOG_FILE.stat().st_size / 1e3, 1) if LOG_FILE.is_file() else 0})


@command("stop", "close the tabs this tool opened, delete its scratch files, then stop the background runtime (--keep-tabs / --keep-files to skip)", s_stop)
def c_stop(ctx, ns):
    info = daemon_info()
    summary = None
    if not ns.keep_tabs and ctx.state.get("opened") and daemon_alive(info):
        summary = close_opened_tabs(ctx, ns)
    files = None if ns.keep_files else clean_artifacts(ctx)
    if files and not files["files_deleted"]:
        files = None
    if daemon_alive(info):
        try:
            _daemon_request(info, {"op": "stop"}, 10)
        except Exception:  # noqa: BLE001
            pass
        emit({"stopped": True, **({"cleanup": summary} if summary else {}), **({"files": files} if files else {})})
    else:
        ctx.state["opened"] = []  # the runtime is gone, so its tabs were released with it
        ctx.save()
        emit({"stopped": False, "note": "not running", **({"files": files} if files else {})})


@command("_daemon", "(internal) background runtime server")
def c_daemon(ctx, ns):
    daemon_main(os.environ.get("CUA_DAEMON_SESSION") or ctx.session_id)


@command("state", "show saved defaults (browser, tab, session id)")
def c_state(ctx, ns):
    emit({"task": ctx.task, **ctx.state, "effective_browser": ctx.browser})


@command("use-browser", "set the default browser (id, name, or ext:<instance prefix>); ids are converted to the stable ext: form",
         lambda sp: sp.add_argument("browser"))
def c_use_browser(ctx, ns):
    spec = ns.browser
    if spec.isdigit():
        ctx.browser = ""
        found = run_js(ctx, ns, """
const hit = (await cua.listBrowsers({ emit: false })).find((b) => String(b.id) === String(A.want));
out(hit ? { ext: (hit.metadata || {}).extensionInstanceId || null, name: hit.name } : { ext: null });""", {"want": spec}, title="use-browser")
        if found.get("ext"):
            spec = "ext:" + found["ext"][:8]
    ctx.state["browser"] = spec
    ctx.state.pop("tab", None)
    ctx.save()
    emit({"browser": spec})


@command("browsers", "list browsers (each Chrome profile with the extension is its own id) with open tabs")
def c_browsers(ctx, ns):
    ctx.browser = ""  # listing must work even when the saved default browser id is stale
    emit(run_js(ctx, ns, """
const res = [];
for (const b of await cua.listBrowsers({ emit: false })) {
  let tabs = [];
  try { tabs = await cua.listTabs({ browser: b.id, emit: false }); } catch (e) {}
  const ext = (b.metadata || {}).extensionInstanceId || null;
  res.push({ ...b, use: ext ? "ext:" + ext.slice(0, 8) : String(b.id), tabs });
}
out(res);""", title="List browsers"))


def s_tabs(sp):
    sp.add_argument("--scope", choices=["all", "agent"], default="all")
    sp.add_argument("--query", help="substring of title/url")
    sp.add_argument("--limit", type=int, default=100)


@command("tabs", "list tabs (scope all = every user tab; agent = only this session's)", s_tabs)
def c_tabs(ctx, ns):
    emit(run_js(ctx, ns, """
let tabs = A.scope === "agent" ? await browser.tabs.list() : await browser.user.openTabs();
if (A.query) { const q = A.query.toLowerCase(); tabs = tabs.filter(t => ((t.title||"") + " " + (t.url||"")).toLowerCase().includes(q)); }
out({ count: tabs.length, tabs: tabs.slice(0, A.limit) });""", {"scope": ns.scope, "query": ns.query, "limit": ns.limit}, title="List tabs", retry=2))


def s_open(sp):
    sp.add_argument("url", nargs="?")
    sp.add_argument("--session", help="short emoji-prefixed session label, e.g. '🔎 Research'")
    sp.add_argument("--keep", action="store_true", help="mark the tab as handoff (only matters with --no-daemon; otherwise the daemon keeps it alive)")


@command("open", "open a new tab (becomes the current tab)", s_open)
def c_open(ctx, ns):
    d = run_js(ctx, ns, """
const opts = A.session ? { sessionName: A.session } : undefined;
const t = await cua.createBrowserTab(BID, A.url || undefined, opts);
if (A.keep) await t.markHandoff();
out(await brief(t));""", {"url": ns.url, "session": ns.session, "keep": ns.keep}, title="Open tab")
    ctx.set_tab(d["id"])
    ctx.track_opened(d["id"])
    d["tabs_to_close_when_done"] = len(ctx.state.get("opened", []))
    emit(d)


@command("use", "claim an existing tab and make it current", lambda sp: sp.add_argument("tab_id"))
def c_use(ctx, ns):
    ctx.tab_override = ns.tab_id
    d = run_js(ctx, ns, "const t = await getTab(A.tab_id); out(await brief(t));", title="Use tab", tab=True)
    ctx.set_tab(ns.tab_id)
    emit(d)


@command("close", "close the current tab (or --tab); tabs you did not open need --confirmed",
         lambda sp: add_confirm(sp, "the user asked to close a tab that this tool did not open"))
def c_close(ctx, ns):
    tid = ctx.tab()
    if tid not in [o["id"] for o in ctx.state.get("opened", [])]:
        _need(ns, f"tab {tid} was not opened by this tool (it may be the user's own tab).")
    emit(run_js(ctx, ns, "const t = await getTab(A.tab_id); await t.close(); out({ closed: A.tab_id });", title="Close tab", tab=True))
    ctx.untrack(tid)
    if ctx.state.get("tab") == tid:
        ctx.state.pop("tab", None)
        ctx.save()


def s_nav(sp):
    sp.add_argument("action", choices=["goto", "back", "forward", "reload"])
    sp.add_argument("url", nargs="?")
    common(sp, observe=True)


@command("nav", "navigate: goto URL | back | forward | reload", s_nav)
def c_nav(ctx, ns):
    if ns.action == "goto" and not ns.url:
        raise SystemExit("goto needs a URL")
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
if (A.action === "goto") await t.goto(A.url); else await t[A.action]();
out(await after(t, await brief(t)));""", {"action": ns.action, "url": ns.url}, title=f"nav {ns.action}", tab=True, retry=2 if ns.action in ("goto", "reload") else 0))


@command("ax", "accessibility tree with element indexes (diff vs previous unless --full)",
         lambda sp: sp.add_argument("--full", action="store_true"))
def c_ax(ctx, ns):
    emit(run_js(ctx, ns, "const t = await getTab(A.tab_id); out({ __text: await t.getAXState({ emit: false, disableDiffing: !!A.full }) });",
                {"full": ns.full}, title="AX tree", retry=2, tab=True))


@command("dom", "DOM snapshot (includes iframe bodies)")
def c_dom(ctx, ns):
    emit(run_js(ctx, ns, "const t = await getTab(A.tab_id); out({ __text: await t.playwright.domSnapshot() });", title="DOM snapshot", retry=2, tab=True))


def s_read(sp):
    add_locator(sp)
    sp.add_argument("--what", choices=["inner_text", "text", "attribute", "html", "value"], default="inner_text")
    sp.add_argument("--attr", help="attribute name for --what attribute")
    sp.add_argument("--all", action="store_true", help="one value per matching element")
    sp.add_argument("--limit", type=int, default=50)
    common(sp)


@command("read", "read text/attribute/html/value (whole body if no locator)", s_read)
def c_read(ctx, ns):
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
const base = loc(t, A.target || { selector: "body" });
const read = async (l) => {
  if (A.what === "text") return await l.textContent(TO);
  if (A.what === "inner_text") return await l.innerText(TO);
  if (A.what === "attribute") return await l.getAttribute(A.attr, TO);
  if (A.what === "html") return await l.evaluate((el) => el.outerHTML);
  return await l.evaluate((el) => el.value ?? null);
};
if (A.all) {
  const vals = [];
  for (const l of (await base.all()).slice(0, A.limit)) vals.push(await read(l));
  out({ count: await base.count(), values: vals });
} else out({ __text: String(await read(base)) });""", {"target": target_of(ns), "what": ns.what, "attr": ns.attr, "all": ns.all, "limit": ns.limit}, title="read", retry=2, tab=True))


def s_find(sp):
    add_locator(sp)
    sp.add_argument("--limit", type=int, default=20)


@command("find", "describe matching elements (tag, text, href, visibility, bounding box)", s_find)
def c_find(ctx, ns):
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
const l = loc(t, A.target);
const count = await l.count();
const items = await l.evaluateAll((els, n) => els.slice(0, n).map((e) => {
  const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
  return { tag: e.tagName.toLowerCase(), id: e.id || undefined, name: e.getAttribute("name") || undefined, type: e.getAttribute("type") || undefined,
    role: e.getAttribute("role") || undefined, href: e.getAttribute("href") || undefined, src: e.getAttribute("src") || undefined,
    placeholder: e.getAttribute("placeholder") || undefined, aria: e.getAttribute("aria-label") || undefined,
    value: ("value" in e && e.value !== "") ? String(e.value).slice(0, 80) : undefined,
    text: (e.innerText || e.textContent || "").trim().replace(/\\s+/g, " ").slice(0, 120) || undefined,
    visible: r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none",
    box: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) } };
}), A.limit);
out({ count, shown: items.length, items });""", {"target": target_of(ns), "limit": ns.limit}, title="find", retry=2, tab=True))


def s_eval(sp):
    sp.add_argument("expression", help="read-only JS, e.g. '() => document.title' or '(el, arg) => el.id' with a locator")
    sp.add_argument("--arg", help="JSON argument")
    add_locator(sp)


@command("eval", "evaluate READ-ONLY JS in the page's isolated DOM scope (cannot see window.* of the page)", s_eval)
def c_eval(ctx, ns):
    arg = json.loads(ns.arg) if ns.arg else None
    emit(run_js(ctx, ns, r"""
const t = await getTab(A.tab_id);
// The read-only scope has no eval and strings are evaluated as expressions, so function-shaped
// sources are wrapped into an IIFE (page level) or a function whose toString carries the source (element level).
const isFn = /^\s*(async\b|function\b|\([^)]*\)\s*=>|[\w$]+\s*=>)/.test(A.expression);
const argJson = JSON.stringify(A.arg === undefined ? null : A.arg);
let r;
if (A.target) {
  const src = isFn ? "(el) => (" + A.expression + ")(el, " + argJson + ")" : "(el) => (" + A.expression + ")";
  const f = (el) => 0; f.toString = () => src;
  r = await loc(t, A.target).evaluate(f);
} else {
  r = await t.playwright.evaluate(isFn ? "(" + A.expression + ")(" + argJson + ")" : A.expression);
}
out({ result: r === undefined ? null : r });""", {"expression": ns.expression, "arg": arg, "target": target_of(ns)}, title="eval", retry=2, tab=True))


def s_click(sp):
    add_locator(sp)
    sp.add_argument("--index", type=int, help="element index from `ax`")
    sp.add_argument("--xy", help="viewport point 'x,y'")
    sp.add_argument("--button", choices=["left", "right", "middle"], default="left")
    sp.add_argument("--double", action="store_true")
    sp.add_argument("--mod", action="append", choices=["Alt", "Control", "ControlOrMeta", "Meta", "Shift"], help="modifier (repeatable)")
    sp.add_argument("--force", action="store_true")
    add_confirm(sp, "allow clicking controls that look like send/submit/post/pay/delete/subscribe (after the user said yes)")
    common(sp, observe=True)


@command("click", "click / double-click by locator, ax index, or x,y (risky-looking controls need --confirmed)", s_click)
def c_click(ctx, ns):
    xy = [float(v) for v in ns.xy.split(",")] if ns.xy else None
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
if (A.index != null || A.xy) {
  const aim = A.index != null ? A.index : A.xy;
  await t.click(aim, { mouseButton: A.button, clickCount: A.double ? 2 : 1 });
} else {
  const l = loc(t, A.target);
  if (!A.confirmed) {
    const info = await l.evaluate((el, src) => {
      const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
      const text = norm(el.innerText || el.textContent || el.getAttribute("value") || "");
      const hay = text + " " + (el.getAttribute("aria-label") || "") + " " + (el.getAttribute("title") || "");
      const m = hay.match(new RegExp(src, "i"));
      const submit = (el.getAttribute("type") || "").toLowerCase() === "submit";
      return { text: text.slice(0, 60), word: m ? m[0] : null, submit };
    }, A.risk_src);
    if (info.word || info.submit) throw new Error("RISKY: this control looks like '" + (info.word || "submit") + "' (" + JSON.stringify(info.text) + ").");
  }
  const o = { ...TO, button: A.button };
  if (A.force) o.force = true;
  if (A.mod) o.modifiers = A.mod;
  if (A.double) await l.dblclick(o); else await l.click(o);
}
out(await after(t, { ok: true }));""", {"target": target_of(ns), "index": ns.index, "xy": xy, "button": ns.button, "double": ns.double, "mod": ns.mod, "force": ns.force,
                                        "confirmed": ns.confirmed or GUARD_OFF, "risk_src": RISK_SRC}, title="click", tab=True))


def s_type(sp):
    sp.add_argument("value", metavar="TEXT", help="the text to enter (separate from the --text locator flag)")
    add_locator(sp)
    sp.add_argument("--index", type=int, help="element index from `ax`")
    sp.add_argument("--mode", choices=["fill", "type", "sequential", "native", "set-value", "paste"],
                    help="default: fill with a locator, native (focused element) without")
    sp.add_argument("--enter", action="store_true", help="press Enter afterwards")
    common(sp, observe=True)


@command("type", "enter text (fill/type/sequential/native/set-value/paste)", s_type)
def c_type(ctx, ns):
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
const mode = A.mode || (A.target ? "fill" : "native");
const L = () => loc(t, A.target);
if (mode === "fill") await L().fill(A.text, TO);
else if (mode === "type") await L().type(A.text, TO);
else if (mode === "sequential") await L().pressSequentially(A.text, TO);
else if (mode === "native") await t.typeText(A.index ?? null, A.text);
else if (mode === "set-value") await t.setValue(A.index, A.text);
else if (mode === "paste") await t.paste(A.index ?? null, A.text, { format: "text" });
else throw new Error("unknown mode " + mode);
if (A.enter) {
  if (A.target && ["fill", "type", "sequential"].includes(mode)) await L().press("Enter", TO);
  else await t.pressKey(A.index ?? null, "Return");
}
out(await after(t, { ok: true, mode }));""", {"text": ns.value, "target": target_of(ns), "index": ns.index, "mode": ns.mode, "enter": ns.enter}, title="type", tab=True))


def s_key(sp):
    sp.add_argument("key", help="with a locator: Enter, Control+A, ArrowDown. Without: xdotool style Return, ctrl+a, Down, Tab")
    add_locator(sp)
    sp.add_argument("--index", type=int)
    common(sp, observe=True)


@command("key", "press a key / shortcut", s_key)
def c_key(ctx, ns):
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
if (A.target) await loc(t, A.target).press(A.key, TO); else await t.pressKey(A.index ?? null, A.key);
out(await after(t, { ok: true }));""", {"key": ns.key, "target": target_of(ns), "index": ns.index}, title="key", tab=True))


def s_check(sp):
    add_locator(sp)
    sp.add_argument("--off", action="store_true", help="uncheck instead")
    sp.add_argument("--force", action="store_true")
    common(sp, observe=True)


@command("check", "set a checkbox/switch/radio (checked by default)", s_check)
def c_check(ctx, ns):
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
await loc(t, A.target).setChecked(!A.off, { ...TO, ...(A.force ? { force: true } : {}) });
out(await after(t, { ok: true, checked: !A.off }));""", {"target": target_of(ns), "off": ns.off, "force": ns.force}, title="check", tab=True))


def s_selopt(sp):
    add_locator(sp)
    sp.add_argument("--option", required=True, help="value string, or JSON like '{\"label\":\"Blue\"}' / '[\"a\",\"b\"]'")
    common(sp, observe=True)


@command("select-option", "pick option(s) in a native <select>", s_selopt)
def c_selopt(ctx, ns):
    try:
        opt = json.loads(ns.option)
    except ValueError:
        opt = ns.option
    emit(run_js(ctx, ns, "const t = await getTab(A.tab_id); await loc(t, A.target).selectOption(A.option, TO); out(await after(t, { ok: true }));",
                {"target": target_of(ns), "option": opt}, title="select-option", tab=True))


def s_scroll(sp):
    sp.add_argument("direction", choices=["up", "down", "left", "right"])
    sp.add_argument("--pages", type=float, default=1)
    sp.add_argument("--index", type=int)
    sp.add_argument("--xy", help="viewport point (default 400,300)")
    common(sp, observe=True)


@command("scroll", "scroll by pages", s_scroll)
def c_scroll(ctx, ns):
    xy = [float(v) for v in ns.xy.split(",")] if ns.xy else None
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
const aim = A.index != null ? A.index : (A.xy || [400, 300]);
await t.scroll(aim, A.direction, A.pages);
out(await after(t, { ok: true }));""", {"direction": ns.direction, "pages": ns.pages, "index": ns.index, "xy": xy}, title="scroll", tab=True))


def s_drag(sp):
    for k in ("x1", "y1", "x2", "y2"):
        sp.add_argument(k, type=float)
    common(sp, observe=True)


@command("drag", "drag from (x1,y1) to (x2,y2) in viewport coordinates (straight line)", s_drag)
def c_drag(ctx, ns):
    emit(run_js(ctx, ns, "const t = await getTab(A.tab_id); await t.drag([A.x1, A.y1], [A.x2, A.y2]); out(await after(t, { ok: true }));",
                {"x1": ns.x1, "y1": ns.y1, "x2": ns.x2, "y2": ns.y2}, title="drag", tab=True))


def s_wait(sp):
    add_locator(sp)
    sp.add_argument("--ms", type=int)
    sp.add_argument("--state", choices=["attached", "detached", "visible", "hidden"], default="visible")
    sp.add_argument("--url")
    sp.add_argument("--load", choices=["load", "domcontentloaded", "networkidle"])
    common(sp)


@command("wait", "wait for --ms, a locator state, --url, or --load (use sparingly)", s_wait)
def c_wait(ctx, ns):
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
if (A.ms != null) await t.playwright.waitForTimeout(A.ms);
else if (A.target) await loc(t, A.target).waitFor({ state: A.state, ...TO });
else if (A.url) await t.playwright.waitForURL(A.url, TO);
else await t.playwright.waitForLoadState({ state: A.load || "load", ...TO });
out(await brief(t));""", {"target": target_of(ns), "ms": ns.ms, "state": ns.state, "url": ns.url, "load": ns.load}, title="wait", retry=2, tab=True))


def s_shot(sp):
    add_locator(sp)
    sp.add_argument("--out", help="output .jpg path (default ~/.cua-chrome/shots/)")
    sp.add_argument("--full", action="store_true", help="full page")
    sp.add_argument("--clip", help="x,y,width,height")
    common(sp)


@command("shot", "screenshot (JPEG) of viewport, --full page, --clip, or one element; prints the saved path", s_shot)
def c_shot(ctx, ns):
    clip = None
    if ns.clip:
        x, y, w, h = [float(v) for v in ns.clip.split(",")]
        clip = {"x": x, "y": y, "width": w, "height": h}
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
const o = {};
if (A.full) o.fullPage = true;
if (A.clip) o.clip = A.clip;
if (A.target) o.clip = await loc(t, A.target).evaluate((el) => { const b = el.getBoundingClientRect(); return { x: b.x, y: b.y, width: b.width, height: b.height }; });
let bytes;
for (let i = 0; ; i++) {
  try { bytes = await t.screenshot(o); break; }
  catch (e) { if (i >= 2 || !/timed? ?out/i.test(String(e && e.message))) throw e; }
}
await nodeRepl.emitImage({ bytes, mimeType: "image/jpeg" });
out({ ok: true, bytes: bytes.length, ...(await brief(t)) });""", {"full": ns.full, "clip": clip, "target": target_of(ns)}, title="screenshot", retry=2, tab=True, save_shot=ns.out or ""))


def s_upload(sp):
    sp.add_argument("files", nargs="+", help="absolute paths")
    add_locator(sp)
    sp.add_argument("--wait", type=int, default=15000)
    add_confirm(sp, "the user agreed to send these files to this site")
    common(sp)


@command("upload", "upload local files through the page's file chooser (sends file contents to the site)", s_upload)
def c_upload(ctx, ns):
    _need(ns, "uploading sends file contents to a website.")
    files = [str(Path(f).resolve()) for f in ns.files]
    for f in files:
        if not Path(f).is_file():
            raise SystemExit(f"file not found: {f}")
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
const chooserP = t.playwright.waitForEvent("filechooser", { timeoutMs: A.wait }); chooserP.catch(() => {});
await loc(t, A.target).click(TO);
const chooser = await chooserP; const multiple = chooser.isMultiple();
if (!multiple && A.files.length > 1) throw new Error("Chooser accepts a single file");
await chooser.setFiles(multiple ? A.files : A.files[0], { timeoutMs: A.wait });
out({ ok: true, uploaded: A.files, multiple });""", {"files": files, "target": target_of(ns), "wait": ns.wait}, title="upload", tab=True, timeout_ms=ns.wait + 30000))


def s_download(sp):
    add_locator(sp)
    sp.add_argument("--media", action="store_true", help="download the matched image/video/link directly")
    sp.add_argument("--wait", type=int, default=120000)
    sp.add_argument("--copy-to", help="copy the downloaded file to this file/folder")
    sp.add_argument("--scratch", action="store_true", help="the file is only needed for analysis: `cleanup` deletes it from the browser's download folder")
    common(sp)


@command("download", "download a file (click a download control, or --media) and print its local path", s_download)
def c_download(ctx, ns):
    d = run_js(ctx, ns, """
const t = await getTab(A.tab_id);
const l = loc(t, A.target);
let path;
if (A.media) path = await l.downloadMedia({ timeoutMs: A.wait });
else {
  const dl = t.playwright.waitForEvent("download", { timeoutMs: A.wait }); dl.catch(() => {});
  await l.click(TO);
  path = await (await dl).path({ timeoutMs: A.wait });
}
out({ ok: true, path });""", {"target": target_of(ns), "media": ns.media, "wait": ns.wait}, title="download", tab=True, timeout_ms=ns.wait + 30000)
    if ns.scratch:
        ctx.register(d["path"], "download", keep=False)
        d["scratch"] = True
    if ns.copy_to:
        import shutil as _sh
        dest = Path(ns.copy_to).expanduser()
        if dest.is_dir():
            dest = dest / Path(d["path"]).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        _sh.copy2(d["path"], dest)
        d["copied_to"] = str(dest)
    emit(d)


def s_clip(sp):
    sp.add_argument("action", choices=["read", "write"])
    sp.add_argument("text", nargs="?")


@command("clipboard", "read or write the browser clipboard (write overwrites what the user copied)", s_clip)
def c_clip(ctx, ns):
    if ns.action == "write" and ns.text is None:
        raise SystemExit("clipboard write needs TEXT")
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
if (A.action === "read") out({ text: await t.clipboard.readText() });
else { await t.clipboard.writeText(A.text); out({ ok: true, chars: A.text.length }); }""", {"action": ns.action, "text": ns.text}, title="clipboard", tab=True))


def s_dialog(sp):
    sp.add_argument("action", choices=["get", "accept", "dismiss"])
    sp.add_argument("--text", help="answer for prompt dialogs")


@command("dialog", "inspect or answer a JS alert/confirm/prompt dialog", s_dialog)
def c_dialog(ctx, ns):
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
const d = await t.getJsDialog();
if (A.action === "get") out(d ? { open: true, type: d.type } : { open: false });
else {
  if (!d) throw new Error("No JavaScript dialog is open on this tab");
  if (A.action === "dismiss" || d.type === "alert" || d.type === "beforeunload") await d.dismiss();
  else if (d.type === "prompt") await d.accept(A.text ?? ""); else await d.accept();
  out({ ok: true, type: d.type, action: A.action });
}""", {"action": ns.action, "text": ns.text}, title="dialog", tab=True))


def parse_json_tokens(tokens):
    out = []
    for tk in tokens:
        try:
            out.append(json.loads(tk))
        except ValueError:
            out.append(tk)
    return out


def s_mgmt(sp):
    sp.add_argument("namespace", choices=["windows", "tabs", "tabGroups", "bookmarks"])
    sp.add_argument("method")
    sp.add_argument("args", nargs="*", help="each token parsed as JSON when possible, e.g. 123  '{\"pinned\":true}'")
    add_confirm(sp, "required for remove/delete/clear methods")


@command("mgmt", "any chrome.tabs/windows/tabGroups/bookmarks method exposed by the management capability", s_mgmt)
def c_mgmt(ctx, ns):
    if re.search(r"remove|delete|clear", ns.method, re.I):
        _need(ns, f"{ns.namespace}.{ns.method} is destructive.")
    emit(run_js(ctx, ns, """
const mg = await mgmt();
const fn = mg[A.namespace] && mg[A.namespace][A.method];
if (typeof fn !== "function") throw new Error("Unavailable: " + A.namespace + "." + A.method);
const r = await fn.apply(mg[A.namespace], A.args || []);
out({ result: r === undefined ? null : r });""", {"namespace": ns.namespace, "method": ns.method, "args": parse_json_tokens(ns.args)}, title=f"mgmt {ns.namespace}.{ns.method}"))


def s_tabsq(sp):
    sp.add_argument("filter", nargs="?", default="{}", help="JSON, e.g. '{\"pinned\":true}'")


@command("tabs-query", "chrome.tabs.query across windows (id, windowId, index, groupId, pinned, title, url)", s_tabsq)
def c_tabsq(ctx, ns):
    emit(run_js(ctx, ns, """
const mg = await mgmt();
const tabs = await mg.tabs.query(A.filter || {});
out({ count: tabs.length, tabs: tabs.map(({ id, windowId, index, groupId, pinned, active, title, url }) => ({ id, windowId, index, groupId, pinned, active, title, url })) });""",
                {"filter": json.loads(ns.filter)}, title="tabs-query"))


@command("group-list", "list tab groups with their tab ids")
def c_group_list(ctx, ns):
    emit(run_js(ctx, ns, """
const mg = await mgmt();
const groups = await mg.tabGroups.query({}); const tabs = await mg.tabs.query({});
out(groups.map((g) => ({ ...g, tabIds: tabs.filter((t) => t.groupId === g.id).map((t) => t.id) })));""", title="group-list"))


def s_group_create(sp):
    sp.add_argument("--tabs", required=True, help="comma-separated tab ids (same window)")
    sp.add_argument("--title")
    sp.add_argument("--color", choices=["grey", "blue", "red", "yellow", "green", "pink", "purple", "cyan", "orange"])
    sp.add_argument("--group-id", type=int, help="add to an existing group")
    sp.add_argument("--collapsed", choices=["true", "false"])


@command("group-create", "group tabs (new group, or add to --group-id), optionally title/color", s_group_create)
def c_group_create(ctx, ns):
    emit(run_js(ctx, ns, """
const mg = await mgmt();
const gid = await mg.tabs.group({ tabIds: A.tabs, ...(A.group_id != null ? { groupId: A.group_id } : {}) });
const props = {};
if (A.title != null) props.title = A.title;
if (A.color) props.color = A.color;
if (A.collapsed != null) props.collapsed = A.collapsed === "true";
if (Object.keys(props).length) await mg.tabGroups.update(gid, props);
out({ groupId: gid, group: await mg.tabGroups.get(gid) });""", {"tabs": [int(x) for x in ns.tabs.split(",")], "title": ns.title, "color": ns.color,
                                                              "group_id": ns.group_id, "collapsed": ns.collapsed}, title="group-create"))


def s_group_update(sp):
    sp.add_argument("group_id", type=int)
    sp.add_argument("--title")
    sp.add_argument("--color", choices=["grey", "blue", "red", "yellow", "green", "pink", "purple", "cyan", "orange"])
    sp.add_argument("--collapsed", choices=["true", "false"])


@command("group-update", "rename / recolor / collapse a tab group", s_group_update)
def c_group_update(ctx, ns):
    emit(run_js(ctx, ns, """
const mg = await mgmt();
const props = {};
if (A.title != null) props.title = A.title;
if (A.color) props.color = A.color;
if (A.collapsed != null) props.collapsed = A.collapsed === "true";
out(await mg.tabGroups.update(A.group_id, props));""", {"group_id": ns.group_id, "title": ns.title, "color": ns.color, "collapsed": ns.collapsed}, title="group-update"))


@command("ungroup", "remove tabs from their groups", lambda sp: sp.add_argument("tabs", help="comma-separated tab ids"))
def c_ungroup(ctx, ns):
    emit(run_js(ctx, ns, "const mg = await mgmt(); await mg.tabs.ungroup(A.tabs); out({ ok: true });", {"tabs": [int(x) for x in ns.tabs.split(",")]}, title="ungroup"))


def s_pin(sp):
    sp.add_argument("tabs", help="comma-separated tab ids")
    sp.add_argument("--off", action="store_true")


@command("pin", "pin (or --off unpin) tabs", s_pin)
def c_pin(ctx, ns):
    emit(run_js(ctx, ns, "const mg = await mgmt(); for (const id of A.tabs) await mg.tabs.update(id, { pinned: !A.off }); out({ ok: true, pinned: !A.off });",
                {"tabs": [int(x) for x in ns.tabs.split(",")], "off": ns.off}, title="pin"))


def s_tab_move(sp):
    sp.add_argument("tabs", help="comma-separated tab ids")
    sp.add_argument("--index", type=int, required=True, help="-1 = end")
    sp.add_argument("--window", type=int, help="transfer to this window id")


@command("tab-move", "move tabs to an index (and optionally another window)", s_tab_move)
def c_tab_move(ctx, ns):
    emit(run_js(ctx, ns, "const mg = await mgmt(); out(await mg.tabs.move(A.tabs, { index: A.index, ...(A.window != null ? { windowId: A.window } : {}) }));",
                {"tabs": [int(x) for x in ns.tabs.split(",")], "index": ns.index, "window": ns.window}, title="tab-move"))


def s_sort(sp):
    sp.add_argument("--by", choices=["title", "url", "host"], default="title")
    sp.add_argument("--window", type=int, help="sort this window's unpinned tabs")
    sp.add_argument("--tabs", help="or sort these tab ids within the slots they occupy")
    sp.add_argument("--desc", action="store_true")


@command("sort-tabs", "sort a window's unpinned tabs, or the given tab ids, by title/url/host", s_sort)
def c_sort(ctx, ns):
    if ns.window is None and not ns.tabs:
        raise SystemExit("give --window or --tabs so only the intended tabs are reordered")
    emit(run_js(ctx, ns, """
const mg = await mgmt();
const tabs = A.tabs && A.tabs.length ? await Promise.all(A.tabs.map((i) => mg.tabs.get(i))) : (await mg.tabs.query({ windowId: A.window })).filter((x) => !x.pinned);
if (new Set(tabs.map((x) => x.windowId)).size > 1) throw new Error("Tabs must all be in the same window");
const host = (u) => { try { return new URL(u).host; } catch (e) { return u || ""; } };
const key = (x) => A.by === "url" ? (x.url || "") : A.by === "host" ? host(x.url) + "|" + (x.title || "") : (x.title || "");
const slots = tabs.map((x) => x.index).sort((a, b) => a - b);
const sorted = [...tabs].sort((a, b) => key(a).localeCompare(key(b)) * (A.desc ? -1 : 1));
for (let i = 0; i < sorted.length; i++) await mg.tabs.move(sorted[i].id, { index: slots[i] });
const now = await mg.tabs.query({ windowId: tabs[0].windowId });
out({ order: now.sort((a, b) => a.index - b.index).map((x) => ({ id: x.id, index: x.index, title: x.title })) });""",
                {"by": ns.by, "window": ns.window, "tabs": [int(x) for x in ns.tabs.split(",")] if ns.tabs else None, "desc": ns.desc}, title="sort-tabs"))


def s_bm(sp):
    sp.add_argument("action", choices=["search", "list", "create", "move", "update", "remove"])
    sp.add_argument("--query")
    sp.add_argument("--id")
    sp.add_argument("--parent")
    sp.add_argument("--title")
    sp.add_argument("--url")
    sp.add_argument("--index", type=int)
    sp.add_argument("--confirmed", action="store_true", help="required for remove: only after the user explicitly confirmed")


@command("bookmark", "bookmarks: search | list | create | move | update | remove", s_bm)
def c_bm(ctx, ns):
    if ns.action == "remove" and not ns.confirmed:
        raise SystemExit("Deleting a bookmark needs explicit user confirmation; re-run with --confirmed after asking.")
    emit(run_js(ctx, ns, """
const mg = await mgmt();
const slim = (n) => ({ id: n.id, title: n.title, url: n.url, parentId: n.parentId, index: n.index });
const a = A.action;
if (a === "search") { const r = await mg.bookmarks.search({ query: A.query }); out({ count: r.length, bookmarks: r.slice(0, 50) }); }
else if (a === "list") {
  if (A.parent) out({ children: (await mg.bookmarks.getChildren(A.parent)).map(slim) });
  else { const tree = await mg.bookmarks.getTree(); out({ folders: ((tree[0] && tree[0].children) || []).map((r) => ({ ...slim(r), childCount: (r.children || []).length })) }); }
}
else if (a === "create") { const o = { title: A.title }; if (A.url) o.url = A.url; if (A.parent) o.parentId = A.parent; if (A.index != null) o.index = A.index; out(await mg.bookmarks.create(o)); }
else if (a === "move") { const d = {}; if (A.parent) d.parentId = A.parent; if (A.index != null) d.index = A.index; out(await mg.bookmarks.move(A.id, d)); }
else if (a === "update") { const c = {}; if (A.title != null) c.title = A.title; if (A.url != null) c.url = A.url; out(await mg.bookmarks.update(A.id, c)); }
else { await mg.bookmarks.remove(A.id); out({ ok: true, removed: A.id }); }""",
                {"action": ns.action, "query": ns.query, "id": ns.id, "parent": ns.parent, "title": ns.title, "url": ns.url, "index": ns.index}, title=f"bookmark {ns.action}"))


def s_window(sp):
    sp.add_argument("action", choices=["list", "create", "update"])
    sp.add_argument("--id", type=int, help="window id (update)")
    sp.add_argument("--move-tab", dest="wtab", type=int, help="tab id to move into a new window (create)")
    sp.add_argument("--populate", action="store_true")
    sp.add_argument("--focused", choices=["true", "false"])
    sp.add_argument("--state", choices=["normal", "minimized", "maximized", "fullscreen"])
    for k in ("left", "top", "width", "height"):
        sp.add_argument(f"--{k}", type=int)


@command("window", "windows: list | create | update (focus/resize/move)", s_window)
def c_window(ctx, ns):
    props = {k: getattr(ns, k) for k in ("left", "top", "width", "height", "state") if getattr(ns, k) is not None}
    if ns.focused is not None:
        props["focused"] = ns.focused == "true"
    emit(run_js(ctx, ns, """
const mg = await mgmt();
if (A.action === "list") out(await mg.windows.getAll({ populate: !!A.populate }));
else if (A.action === "create") out(await mg.windows.create(A.wtab != null ? { tabId: A.wtab } : {}));
else out(await mg.windows.update(A.id, A.props));""", {"action": ns.action, "id": ns.id, "wtab": ns.wtab, "populate": ns.populate, "props": props}, title=f"window {ns.action}"))


def s_history(sp):
    sp.add_argument("--query", action="append", help="term (repeatable)")
    sp.add_argument("--from", dest="from_date")
    sp.add_argument("--to", dest="to_date")
    sp.add_argument("--limit", type=int, default=50)
    add_confirm(sp, "the user agreed to a history lookup")


@command("history", "search browsing history (sensitive, may need user approval: use narrowly)", s_history)
def c_history(ctx, ns):
    _need(ns, "browsing history is sensitive.")
    emit(run_js(ctx, ns, """
const o = { limit: A.limit };
if (A.queries) o.queries = A.queries;
if (A.from_date) o.from = A.from_date;
if (A.to_date) o.to = A.to_date;
const h = await browser.history(o);
out({ count: h.length, entries: h });""", {"queries": ns.query, "from_date": ns.from_date, "to_date": ns.to_date, "limit": ns.limit}, title="history"))


def s_logs(sp):
    sp.add_argument("--filter")
    sp.add_argument("--level", action="append", choices=["debug", "info", "log", "warn", "error", "warning"])
    sp.add_argument("--limit", type=int, default=100)


@command("logs", "console messages captured for the current tab", s_logs)
def c_logs(ctx, ns):
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
const o = { limit: A.limit };
if (A.filter) o.filter = A.filter;
if (A.levels) o.levels = A.levels;
const logs = await t.dev.logs(o);
out({ count: logs.length, logs });""", {"filter": ns.filter, "levels": ns.level, "limit": ns.limit}, title="logs", retry=2, tab=True))


@command("audit", "recent model-initiated window/tab/bookmark changes (for undoing organisation changes)",
         lambda sp: sp.add_argument("--limit", type=int, default=20))
def c_audit(ctx, ns):
    emit(run_js(ctx, ns, "const mg = await mgmt(); const r = await mg.getAuditTrail(); out({ total: r.changes.length, changes: r.changes.slice(0, A.limit) });",
                {"limit": ns.limit}, title="audit"))


def s_viewport(sp):
    sp.add_argument("action", choices=["set", "reset"])
    sp.add_argument("--width", type=int)
    sp.add_argument("--height", type=int)
    sp.add_argument("--preset", choices=["mobile", "tablet", "laptop", "desktop"])


PRESETS = {"mobile": (375, 812), "tablet": (768, 1024), "laptop": (1366, 768), "desktop": (1920, 1080)}


@command("viewport", "responsive testing: set (width/height or preset) | reset (Chrome floors the width near 416px)", s_viewport)
def c_viewport(ctx, ns):
    w, h = ns.width, ns.height
    if ns.preset:
        w, h = PRESETS[ns.preset]
    if ns.action == "set" and not (w and h):
        raise SystemExit("viewport set needs --width and --height, or --preset")
    emit(run_js(ctx, ns, """
const v = await browser.capabilities.get("viewport");
if (A.action === "set") { await v.set({ width: A.width, height: A.height }); out({ ok: true, width: A.width, height: A.height }); }
else { await v.reset(); out({ ok: true }); }""", {"action": ns.action, "width": w, "height": h}, title=f"viewport {ns.action}"))


def s_assets(sp):
    sp.add_argument("action", choices=["list", "bundle", "save"])
    sp.add_argument("--out", help="folder for `save` (downloads the listed assets over HTTP from the CLI, so it works even when `bundle` hits the sandbox)")
    sp.add_argument("--kind", action="append", choices=["image", "font", "stylesheet", "video", "script", "other"])
    sp.add_argument("--ids", help="comma-separated asset ids (bundle)")
    sp.add_argument("--inventory", help="inventory id from `assets list` (bundle)")
    sp.add_argument("--limit", type=int, default=100)


@command("assets", "page assets: list | bundle (bundle needs the runtime's file-system grant; may fail with EPERM)", s_assets)
def c_assets(ctx, ns):
    if ns.action == "save":
        import urllib.request
        if not ns.out:
            raise SystemExit("assets save needs --out FOLDER")
        d = run_js(ctx, ns, """
const t = await getTab(A.tab_id);
const cap = await t.capabilities.get("pageAssets");
const inv = await cap.list(); let assets = inv.assets;
if (A.kinds) assets = assets.filter((a) => A.kinds.includes(a.kind));
out({ pageUrl: inv.pageUrl, assets: assets.slice(0, A.limit).map(({ id, kind, name, url }) => ({ id, kind, name, url })), svgs: inv.inlineSvgs.slice(0, A.limit) });""",
                   {"kinds": ns.kind, "limit": ns.limit}, title="assets save", tab=True, timeout_ms=120000)
        out_dir = Path(ns.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        saved, failed = [], []
        for a in d["assets"]:
            if not re.match(r"https?://", a["url"]):
                continue
            name = re.sub(r"[^\w.\-]+", "_", a["name"] or Path(a["url"].split("?")[0]).name or a["id"])[:80] or a["id"]
            dest = out_dir / name
            n = 1
            while dest.exists():
                dest = out_dir / f"{dest.stem}_{n}{dest.suffix}"
                n += 1
            try:
                req = urllib.request.Request(a["url"], headers={"User-Agent": "Mozilla/5.0", "Referer": d["pageUrl"] or ""})
                dest.write_bytes(urllib.request.urlopen(req, timeout=40).read())
                saved.append({"kind": a["kind"], "file": str(dest)})
            except Exception as e:  # noqa: BLE001
                failed.append({"url": a["url"][:90], "error": str(e)[:60]})
        for sv in d.get("svgs", []):
            if sv.get("markup"):
                stem = re.sub(r"[^\w.\-]+", "_", sv.get("name") or sv["id"])
                f = out_dir / (stem + ".svg")
                f.write_text(sv["markup"], encoding="utf-8")
                saved.append({"kind": "inline-svg", "file": str(f)})
        emit({"saved": len(saved), "failed": failed, "files": saved})
        return
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
const cap = await t.capabilities.get("pageAssets");
if (A.action === "list") {
  const inv = await cap.list(); let assets = inv.assets;
  if (A.kinds) assets = assets.filter((a) => A.kinds.includes(a.kind));
  out({ inventoryId: inv.id, pageUrl: inv.pageUrl, summary: inv.summary, shown: Math.min(assets.length, A.limit),
        assets: assets.slice(0, A.limit).map(({ id, kind, name, url }) => ({ id, kind, name, url })), inlineSvgs: inv.inlineSvgs.map(({ id, name }) => ({ id, name })) });
} else {
  const o = { inventoryId: A.inventory || (await cap.list()).id };
  if (A.ids) o.assetIds = A.ids; if (A.kinds) o.kinds = A.kinds;
  out(await cap.bundle(o));
}""", {"action": ns.action, "kinds": ns.kind, "ids": ns.ids.split(",") if ns.ids else None, "inventory": ns.inventory, "limit": ns.limit},
                title=f"assets {ns.action}", tab=True, timeout_ms=180000))


def s_export(sp):
    sp.add_argument("what", choices=["gsuite", "youtube"])
    sp.add_argument("--type", choices=["pdf", "md", "xlsx", "csv", "docx", "pptx"], default="pdf", help="format for gsuite")


@command("export", "export Google Docs/Sheets/Slides (--type) or a YouTube transcript from the current tab", s_export)
def c_export(ctx, ns):
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
out({ path: A.what === "gsuite" ? await t.content.exportGsuite(A.type) : await t.content.exportYouTubeTranscript() });""",
                {"what": ns.what, "type": ns.type}, title=f"export {ns.what}", tab=True, timeout_ms=180000))


def s_cdp(sp):
    sp.add_argument("action", choices=["send", "events"])
    sp.add_argument("method", nargs="?", help="CDP method for send, e.g. Runtime.evaluate")
    sp.add_argument("--params", help="JSON params for send")
    sp.add_argument("--after", type=int, help="cursor for events")
    sp.add_argument("--methods", help="comma-separated event methods filter")
    sp.add_argument("--limit", type=int, default=100)
    sp.add_argument("--wait", type=int, help="ms to wait for the first matching event")


@command("cdp", "raw DevTools Protocol send/events (behind Browser Use's approval gate: may be refused)", s_cdp)
def c_cdp(ctx, ns):
    if ns.action == "send" and not ns.method:
        raise SystemExit("cdp send needs a METHOD")
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
const cdp = await t.capabilities.get("cdp");
await cdp.documentation();
if (A.action === "send") out({ result: await cdp.send(A.method, A.params || {}, A.wait ? { timeoutMs: A.wait } : {}) });
else {
  const o = { limit: A.limit };
  if (A.after != null) o.afterSequence = A.after; if (A.methods) o.methods = A.methods; if (A.wait) o.timeoutMs = A.wait;
  out(await cdp.readEvents(o));
}""", {"action": ns.action, "method": ns.method, "params": json.loads(ns.params) if ns.params else None, "after": ns.after,
       "methods": ns.methods.split(",") if ns.methods else None, "limit": ns.limit, "wait": ns.wait}, title=f"cdp {ns.action}", tab=True,
                timeout_ms=(ns.wait or 0) + 60000))


@command("session-name", "name the browser automation session (short, starts with an emoji)", lambda sp: sp.add_argument("name"))
def c_session_name(ctx, ns):
    emit(run_js(ctx, ns, "await browser.nameSession(A.name); out({ ok: true, name: A.name });", {"name": ns.name}, title="session-name"))


def s_mark(sp):
    sp.add_argument("kind", choices=["deliverable", "handoff"])


@command("mark", "keep the current tab alive: deliverable (live page is the output) | handoff (work continues later)", s_mark)
def c_mark(ctx, ns):
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
if (A.kind === "deliverable") await t.markDeliverable(); else await t.markHandoff();
out({ ok: true, kind: A.kind, ...(await brief(t)) });""", {"kind": ns.kind}, title=f"mark {ns.kind}", tab=True))
    ctx.untrack(ctx.tab())  # the user asked to keep this tab, so `cleanup` must leave it alone


def s_js(sp):
    sp.add_argument("code", nargs="?", help="JS body (or '-' / omit to read stdin). Helpers: A, out(v), browser, getTab(id), loc(tab, target), brief(tab)")
    sp.add_argument("--file", help="read JS from a file")
    sp.add_argument("--args", help="JSON merged into A")


@command("js", "run your own JS in the cua runtime (escape hatch); call out(value) to return a result", s_js)
def c_js(ctx, ns):
    code = ns.code if ns.code not in (None, "-") else None
    if ns.file:
        code = Path(ns.file).read_text(encoding="utf-8")
    elif code is None:
        code = sys.stdin.read()
    extra = json.loads(ns.args) if ns.args else {}
    tid = ctx.tab_override or ctx.state.get("tab")
    if tid and "tab_id" not in extra:
        extra["tab_id"] = str(tid)  # available to the snippet as A.tab_id
    emit(run_js(ctx, ns, code, extra, title="custom js", tab=False, timeout_ms=120000))



def s_look(sp):
    sp.add_argument("--chars", type=int, default=1500, help="max characters of main text")
    sp.add_argument("--links", type=int, default=15)
    sp.add_argument("--shot", help="also save a screenshot to this path")
    common(sp)


@command("look", "one-call page overview: url/title, headings, main text, inputs and buttons with ready-to-paste locator flags, login/captcha hints", s_look)
def c_look(ctx, ns):
    d = run_js(ctx, ns, r"""
const t = await getTab(A.tab_id);
const info = await t.playwright.evaluate((o) => {
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const vis = (e) => { try { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e); return r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none"; } catch (x) { return false; } };
  const q = (s) => [...document.querySelectorAll(s)].filter(vis);
  const txt = (e) => norm(e.innerText || e.textContent);
  const flag = (e) => {
    const tag = e.tagName.toLowerCase();
    if (e.id) return "--selector #" + e.id;
    const name = norm(e.getAttribute("aria-label") || txt(e) || e.getAttribute("value") || e.getAttribute("title"));
    if (tag === "input" || tag === "textarea") {
      const ph = e.getAttribute("placeholder"); if (ph) return '--placeholder "' + ph + '"';
      const nm = e.getAttribute("name"); if (nm) return "--selector " + tag + '[name="' + nm + '"]';
    }
    if (tag === "a" && name) return '--role link --name "' + name.slice(0, 40) + '"';
    if ((tag === "button" || e.getAttribute("role") === "button" || tag === "input") && name) return '--role button --name "' + name.slice(0, 40) + '"';
    return name ? '--text "' + name.slice(0, 40) + '"' : null;
  };
  let main = document.querySelector("main,[role=main],article") || document.body;
  if ((main.innerText || "").trim().length < 200) main = document.body;  // some apps render results outside <main>
  const inputs = q("input,select,textarea").filter((e) => (e.getAttribute("type") || "") !== "hidden" && (e.getAttribute("type") || "") !== "submit").slice(0, 25).map((e) => {
    let label = e.getAttribute("aria-label") || "";
    try { if (!label && e.id) { const l = document.querySelector('label[for="' + e.id + '"]'); if (l) label = txt(l); } } catch (x) {}
    const type = (e.getAttribute("type") || e.tagName.toLowerCase()).toLowerCase();
    let value = ""; try { value = type === "password" ? "" : String(e.value || "").slice(0, 60); } catch (x) {}
    return { kind: e.tagName.toLowerCase(), type, label: label || undefined, placeholder: e.getAttribute("placeholder") || undefined, value: value || undefined, locator: flag(e) };
  });
  const buttons = q("button,[role=button],input[type=submit],input[type=button]").slice(0, 25).map((e) => ({ text: txt(e).slice(0, 50) || e.getAttribute("aria-label") || e.getAttribute("value") || "", locator: flag(e) }));
  const links = q("a[href]");
  const iframes = [...document.querySelectorAll("iframe")];
  return {
    headings: q("h1,h2,h3").slice(0, 12).map((h) => h.tagName.toLowerCase() + ": " + txt(h).slice(0, 80)),
    text: txt(main).slice(0, o.chars),
    inputs, buttons,
    links: { count: links.length, top: links.slice(0, o.links).map((a) => ({ text: txt(a).slice(0, 50), href: (a.getAttribute("href") || "").slice(0, 100) })) },
    iframes: iframes.length,
    dialogs: q("[role=dialog],dialog").length,
    login_form: q("input[type=password]").length > 0,
    captcha: iframes.some((f) => /captcha|recaptcha|hcaptcha|turnstile/i.test(f.getAttribute("src") || "")) || q("[class*=captcha],[id*=captcha]").length > 0,
    scroll: { y: Math.round(window.scrollY || 0), page_height: document.documentElement.scrollHeight, viewport_height: window.innerHeight },
  };
}, { chars: A.chars, links: A.links });
out({ ...(await brief(t)), ...info });""", {"chars": ns.chars, "links": ns.links}, title="look", tab=True, retry=2)
    if ns.shot:
        c_shot(ctx, argparse.Namespace(out=ns.shot, full=False, clip=None, timeout=None, **{k: None for k in LOC_FIELDS}, exact=False, regex=False))
        d["screenshot"] = ns.shot
    emit(d)


def s_note(sp):
    sp.add_argument("action", choices=["get", "add", "list", "clear"])
    sp.add_argument("site", nargs="?", help="domain (or 'current' for the current tab's site)")
    sp.add_argument("text", nargs="?", help="note text for add (one line: selector tips, language of labels, login state, quirks)")


@command("note", "per-site memory: get | add | list | clear notes (shown automatically on open/nav to that site)", s_note)
def c_note(ctx, ns):
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    if ns.action == "list":
        emit({"sites": sorted(f.stem for f in NOTES_DIR.glob("*.md"))})
        return
    site = ns.site
    if not site:
        raise SystemExit("give a site (e.g. facebook.com) or 'current'")
    if site == "current":
        d = run_js(ctx, ns, "const t = await getTab(A.tab_id); out(await brief(t));", title="note", tab=True)
        site = host_of(d["url"])
    site = host_of(site)
    f = NOTES_DIR / f"{site}.md"
    if ns.action == "get":
        emit({"site": site, "notes": f.read_text(encoding="utf-8") if f.is_file() else ""})
    elif ns.action == "add":
        if not ns.text:
            raise SystemExit("note add needs TEXT")
        with f.open("a", encoding="utf-8") as fh:
            fh.write(f"- {_dt.date.today().isoformat()}: {ns.text.strip()}\n")
        emit({"site": site, "saved": True})
    else:
        if f.is_file():
            f.unlink()
        emit({"site": site, "cleared": True})


# ------------------------------------------------------------------ playbook: Meta Ad Library
EN_MONTH = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
CTA_WORDS_ALL = ["Chơi game", "Cài đặt ngay", "Tải xuống", "Tìm hiểu thêm", "Xem thêm", "Mua ngay", "Install Now", "Play Game", "Learn More",
                 "Download", "Shop Now", "Sign Up", "Get Offer", "Book Now", "Watch More", "Apply Now"]


def parse_meta_card(c):
    j = c["text"]
    m = re.search(r"Ngày bắt đầu chạy: (\d+) Tháng (\d+), (\d+)", j)
    start = f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}" if m else None
    if not start:
        m = re.search(r"Started running on (\w{3})\w* (\d+), (\d{4})", j)
        if m and m[1][:3] in EN_MONTH:
            start = f"{m[3]}-{EN_MONTH[m[1][:3]]:02d}-{int(m[2]):02d}"
    mm = re.search(r"(?:Xem chi tiết (?:quảng cáo|bản tóm tắt)|See (?:ad|summary) details) \| (.*?) \| (?:Được tài trợ|Sponsored) \| (.*)$", j)
    page, rest = (mm[1], mm[2]) if mm else ("(unknown)", "")
    dur = re.search(r"\d+:\d+ / (\d+):(\d+)", rest)
    copy = re.split(r" \| (?:\d+:\d+ / |[A-Z0-9.\-]+\.[A-Z]{2,} \|)", rest)[0]
    if re.match(r"^\d+:\d+ / ", copy) or copy.strip() in ("Ít lượt hiển thị", "Đa phiên bản", "Low impression count", "Multiple versions"):
        copy = ""
    dom = re.search(r"\| ([A-Z0-9.\-]+\.[A-Z]{2,}) \|", rest)
    grp = re.search(r"(\d+) (?:quảng cáo sử dụng|ads use this)", j)
    days = (_dt.date.today() - _dt.date.fromisoformat(start)).days if start else None
    return {"id": c["id"], "advertiser": page, "start": start, "days_running": days, "video_seconds": int(dur[1]) * 60 + int(dur[2]) if dur else None,
            "primary_text": copy[:240], "store_domain": dom[1] if dom else None, "cta": next((w for w in CTA_WORDS_ALL if w in j), None),
            "ads_sharing_creative": int(grp[1]) if grp else 1, "low_impressions": ("Ít lượt hiển thị" in j) or ("Low impression" in j),
            "library_url": f"https://www.facebook.com/ads/library/?id={c['id']}"}


def s_meta(sp):
    sp.add_argument("--query", required=True, help="keyword, e.g. 'arrow puzzle'")
    sp.add_argument("--country", default="ALL")
    sp.add_argument("--media", choices=["video", "image", "all"], default="video")
    sp.add_argument("--all-status", action="store_true", help="include inactive ads (default: active only)")
    sp.add_argument("--scrolls", type=int, default=6, help="page-down passes to load more cards (~40 per pass)")
    sp.add_argument("--out", default="meta-ads-out", help="output folder (cards.json, ads.csv, videos/, frames/)")
    sp.add_argument("--download", type=int, default=0, help="download this many videos (newest first)")
    sp.add_argument("--frames", action="store_true", help="with --download: cut 8 frames per video into one tile image (needs ffmpeg)")
    sp.add_argument("--keep-media", action="store_true", help="keep the downloaded videos/frame tiles (default: scratch, deleted by cleanup after you analysed them)")
    sp.add_argument("--keep-tab", action="store_true")


def _find_tool(name):
    exe = shutil.which(name)
    if exe:
        return exe
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    for hit in base.glob(f"*FFmpeg*/**/{name}.exe") if base.exists() else []:
        return str(hit)
    return None


@command("meta-ads", "playbook: scan Meta Ad Library for a keyword -> cards.json + ads.csv (+ videos, frame tiles); opens and closes its own tab", s_meta)
def c_meta_ads(ctx, ns):
    import urllib.parse
    import urllib.request
    out_dir = Path(ns.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    url = ("https://www.facebook.com/ads/library/?active_status=" + ("all" if ns.all_status else "active") + "&ad_type=all&country=" + ns.country
           + "&is_targeted_country=false&media_type=" + ns.media + "&q=" + urllib.parse.quote(ns.query) + "&search_type=keyword_unordered")
    d = run_js(ctx, ns, """
const t = await cua.createBrowserTab(BID, A.url, { sessionName: "📊 Ad Library scan" });
out(await brief(t));""", {"url": url}, title="meta-ads open", timeout_ms=90000)
    tid = d["id"]
    ctx.track_opened(tid)
    ctx.tab_override = tid
    summary = {"query": ns.query, "tab": tid}
    try:
        time.sleep(6)
        for _ in range(max(0, ns.scrolls)):
            try:
                run_js(ctx, ns, 'const t = await getTab(A.tab_id); await t.playwright.locator("body").press("End", { timeoutMs: 3000 }); out({ ok: true });',
                       title="meta-ads scroll", tab=True)
            except SystemExit:
                pass
            time.sleep(1.8)
        code = (HERE / "extract_meta_cards.js").read_text(encoding="utf-8")
        cards = run_js(ctx, ns, code, {}, title="meta-ads extract", tab=True, timeout_ms=120000)
        records = [parse_meta_card(c) for c in cards]
        srcs = {c["id"]: c.get("src") for c in cards}
        (out_dir / "cards.json").write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
        with (out_dir / "ads.csv").open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(records[0].keys()) if records else ["id"])
            w.writeheader()
            w.writerows(records)
        by = {}
        for r in records:
            by.setdefault(r["advertiser"], []).append(r)
        top = sorted(by.items(), key=lambda kv: -len(kv[1]))[:10]
        summary.update({"cards": len(records), "with_video_src": sum(1 for v in srcs.values() if v), "advertisers": len(by),
                        "top_advertisers": [{"advertiser": k, "cards": len(v), "earliest": min((r["start"] for r in v if r["start"]), default=None)} for k, v in top],
                        "files": {"cards": str(out_dir / "cards.json"), "csv": str(out_dir / "ads.csv")}})
        if ns.download:
            vid_dir = out_dir / "videos"
            vid_dir.mkdir(exist_ok=True)
            ffprobe, ffmpeg = _find_tool("ffprobe"), _find_tool("ffmpeg")
            pick = sorted([r for r in records if srcs.get(r["id"])], key=lambda r: r["start"] or "", reverse=True)[: ns.download]
            got = []
            for r in pick:
                mp4 = vid_dir / f"{r['id']}.mp4"
                try:
                    req = urllib.request.Request(srcs[r["id"]], headers={"User-Agent": "Mozilla/5.0"})
                    mp4.write_bytes(urllib.request.urlopen(req, timeout=60).read())
                except Exception as e:  # noqa: BLE001
                    got.append({"id": r["id"], "error": str(e)[:80]})
                    continue
                info = {"id": r["id"], "advertiser": r["advertiser"], "file": str(mp4), "mb": round(mp4.stat().st_size / 1e6, 2)}
                ctx.register(mp4, "video", keep=ns.keep_media)
                if ffprobe:
                    try:
                        pr = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration:stream=width,height", "-of", "json", str(mp4)],
                                            capture_output=True, text=True, timeout=60)
                        j = json.loads(pr.stdout)
                        info["seconds"] = round(float(j["format"]["duration"]), 1)
                        st_ = next((x for x in j["streams"] if "width" in x), {})
                        info["size"] = f"{st_.get('width')}x{st_.get('height')}"
                        if ns.frames and ffmpeg and info["seconds"] > 0:
                            fr_dir = out_dir / "frames"
                            fr_dir.mkdir(exist_ok=True)
                            tile = fr_dir / f"{r['id']}.jpg"
                            subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(mp4), "-vf", f"fps={8 / info['seconds']:.5f},scale=270:-2,tile=4x2",
                                            "-frames:v", "1", "-q:v", "4", str(tile)], timeout=120)
                            info["frames"] = str(tile)
                            ctx.register(tile, "frames", keep=ns.keep_media)
                    except Exception as e:  # noqa: BLE001
                        info["probe_error"] = str(e)[:80]
                got.append(info)
            summary["downloaded"] = got
            summary["media_is_scratch"] = not ns.keep_media
            if (ns.frames or ns.download) and not (ffprobe and ffmpeg):
                summary["note"] = "ffmpeg/ffprobe not found: videos were saved but not probed/tiled"
    finally:
        if not ns.keep_tab:
            try:
                run_js(ctx, ns, "const t = await getTab(A.tab_id); await t.close(); out({ closed: A.tab_id });", title="meta-ads close", tab=True)
            except SystemExit:
                pass
            ctx.untrack(tid)
            summary["tab_closed"] = True
    emit(summary)



# ------------------------------------------------------------------ extraction tools
def s_links(sp):
    sp.add_argument("--filter", help="regex matched against link text or href")
    sp.add_argument("--same-site", action="store_true")
    sp.add_argument("--within", help="CSS selector of a container to search inside")
    sp.add_argument("--limit", type=int, default=100)
    common(sp)


@command("links", "list links (text + absolute href); filter by regex, same-site, container", s_links)
def c_links(ctx, ns):
    d = run_js(ctx, ns, r"""
const t = await getTab(A.tab_id);
const sel = (A.within ? A.within + " " : "") + "a[href]";
const rows = await t.playwright.locator(sel).evaluateAll((els) => els.map((e) => ({ text: (e.innerText || e.textContent || "").replace(/\s+/g, " ").trim().slice(0, 120), href: e.href || e.getAttribute("href") || "" })));
out({ page: await t.url(), rows });""", {"within": ns.within}, title="links", tab=True, retry=2)
    rows, host = d["rows"], host_of(d["page"])
    if ns.filter:
        rx = re.compile(ns.filter, re.I)
        rows = [r for r in rows if rx.search(r["text"]) or rx.search(r["href"])]
    if ns.same_site:
        rows = [r for r in rows if host_of(r["href"]) == host]
    seen, uniq = set(), []
    for r in rows:
        if r["href"] and r["href"] not in seen:
            seen.add(r["href"])
            uniq.append(r)
    emit({"count": len(uniq), "links": uniq[: ns.limit]})


def s_table(sp):
    sp.add_argument("--selector", "-s", default="table", help="CSS selector (default: first table)")
    sp.add_argument("--nth", type=int, default=0)
    sp.add_argument("--csv", help="write the rows to this CSV file instead of printing them")
    sp.add_argument("--limit", type=int, default=500)
    common(sp)


@command("table", "extract an HTML table as rows (or --csv file)", s_table)
def c_table(ctx, ns):
    d = run_js(ctx, ns, r"""
const t = await getTab(A.tab_id);
const rows = await t.playwright.locator(A.selector).nth(A.nth).evaluate((el) => [...el.querySelectorAll("tr")].map((tr) => [...tr.querySelectorAll("th,td")].map((c) => (c.innerText || c.textContent || "").replace(/\s+/g, " ").trim())));
out({ rows });""", {"selector": ns.selector, "nth": ns.nth}, title="table", tab=True, retry=2)
    rows = d["rows"]
    if ns.csv:
        with open(ns.csv, "w", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerows(rows)
        emit({"rows": len(rows), "csv": ns.csv})
    else:
        emit({"total": len(rows), "rows": rows[: ns.limit]})


def s_items(sp):
    sp.add_argument("--container", required=True, help="CSS selector of the repeating element (a card, row, result)")
    sp.add_argument("--field", action="append", required=True, help="NAME=css or NAME=css@attr (empty css = the container itself); repeatable")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--next", help="CSS selector of a 'next page' control to click between pages")
    sp.add_argument("--pages", type=int, default=1)
    sp.add_argument("--csv")
    common(sp)


@command("items", "extract repeated cards/rows into JSON/CSV with named fields; optional pagination", s_items)
def c_items(ctx, ns):
    fields = []
    for f in ns.field:
        name, _, spec = f.partition("=")
        fields.append([name.strip(), spec.strip()])
    rows = []
    for page in range(max(1, ns.pages)):
        d = run_js(ctx, ns, r"""
const t = await getTab(A.tab_id);
const rows = await t.playwright.locator(A.container).evaluateAll((els, fields) => els.map((e) => {
  const o = {};
  for (const [name, spec] of fields) {
    const [sel, attr] = spec.split("@");
    let n = e; try { if (sel) n = e.querySelector(sel); } catch (x) { n = null; }
    o[name] = n ? (attr ? n.getAttribute(attr) : (n.innerText || n.textContent || "").replace(/\s+/g, " ").trim()) : null;
  }
  return o;
}), A.fields);
out({ rows });""", {"container": ns.container, "fields": fields}, title="items", tab=True, retry=2)
        rows.extend(d["rows"])
        if len(rows) >= ns.limit or not ns.next or page + 1 >= ns.pages:
            break
        try:
            run_js(ctx, ns, 'const t = await getTab(A.tab_id); await t.playwright.locator(A.next).first().click(TO); out({ ok: true });', {"next": ns.next}, title="items next", tab=True)
        except SystemExit:
            break
        time.sleep(1.5)
    rows = rows[: ns.limit]
    if ns.csv:
        with open(ns.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=[f[0] for f in fields])
            w.writeheader()
            w.writerows(rows)
        emit({"rows": len(rows), "csv": ns.csv})
    else:
        res = {"count": len(rows), "rows": rows}
        empty = [f[0] for f in fields if rows and all(r.get(f[0]) is None for r in rows)]
        if empty:
            res["warning"] = "no match for field(s) " + ", ".join(empty) + " in any row: check the CSS selectors"
        emit(res)


def s_scrollload(sp):
    sp.add_argument("--item", help="CSS selector of the items to count; stops when the count stops growing")
    sp.add_argument("--rounds", type=int, default=12)
    sp.add_argument("--wait", type=int, default=1500, help="ms between scrolls")


@command("scroll-load", "scroll to the bottom repeatedly until no more items load (infinite scroll)", s_scrollload)
def c_scrollload(ctx, ns):
    emit(run_js(ctx, ns, """
const t = await getTab(A.tab_id);
const counts = []; let last = -1, stable = 0;
for (let i = 0; i < A.rounds; i++) {
  try { await t.playwright.locator("body").press("End", { timeoutMs: 3000 }); } catch (e) {}
  await t.playwright.waitForTimeout(A.wait);
  const n = A.item ? await t.playwright.locator(A.item).count() : i + 1;
  counts.push(n);
  if (A.item) { if (n === last) { stable++; if (stable >= 2) break; } else stable = 0; last = n; }
}
out({ rounds: counts.length, counts, final: counts[counts.length - 1] });""", {"item": ns.item, "rounds": ns.rounds, "wait": ns.wait}, title="scroll-load", tab=True,
                timeout_ms=ns.rounds * (ns.wait + 4000) + 30000))


def s_fill(sp):
    sp.add_argument("--data", required=True, help='JSON object, e.g. \'{"First name":"Ada","Country":"Vietnam","I agree":true}\'')
    sp.add_argument("--frame", help="CSS selector of an iframe holding the form")
    add_confirm(sp, "required to fill password fields")
    common(sp)


@command("fill-form", "fill several fields at once by label/placeholder/name/id (never submits)", s_fill)
def c_fill(ctx, ns):
    emit(run_js(ctx, ns, r"""
const t = await getTab(A.tab_id);
const root = rootOf(t, A.frame);
const all = root.locator("input,select,textarea");
const meta = await all.evaluateAll((els) => els.map((e) => {
  const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
  let label = e.getAttribute("aria-label") || "";
  try { if (!label && e.id) { const l = document.querySelector('label[for="' + e.id + '"]'); if (l) label = l.innerText; } } catch (x) {}
  try { if (!label) { const l = e.closest("label"); if (l) label = l.innerText; } } catch (x) {}
  return { tag: e.tagName.toLowerCase(), type: (e.getAttribute("type") || e.tagName).toLowerCase(), label: (label || "").replace(/\s+/g, " ").trim(),
           placeholder: e.getAttribute("placeholder") || "", name: e.getAttribute("name") || "", id: e.id || "",
           visible: r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none" };
}));
const norm = (s) => String(s || "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
const results = [];
for (const [key, val] of Object.entries(A.data)) {
  const k = norm(key); let best = -1, bestScore = 0;
  meta.forEach((m, i) => {
    if (!m.visible || ["hidden", "submit", "button", "image", "reset", "file"].includes(m.type)) return;
    let sc = 0;
    for (const h of [m.label, m.placeholder, m.name, m.id].map(norm).filter(Boolean)) { if (h === k) sc = Math.max(sc, 3); else if (h.includes(k) || k.includes(h)) sc = Math.max(sc, 2); }
    if (sc > bestScore) { bestScore = sc; best = i; }
  });
  if (best < 0) { results.push({ field: key, ok: false, error: "no matching visible field" }); continue; }
  const m = meta[best]; const l = all.nth(best);
  if (m.type === "password" && !A.confirmed) { results.push({ field: key, ok: false, error: "needs confirmation: password field (re-run with --confirmed after the user agreed)" }); continue; }
  try {
    if (m.type === "checkbox" || m.type === "radio") await l.setChecked(!!val, TO);
    else if (m.tag === "select") { try { await l.selectOption({ label: String(val) }, TO); } catch (e) { await l.selectOption(String(val), TO); } }
    else await l.fill(String(val), TO);
    results.push({ field: key, ok: true, matched: m.label || m.placeholder || m.name || m.id, type: m.type });
  } catch (e) { results.push({ field: key, ok: false, error: String(e.message).slice(0, 100) }); }
}
out({ results });""", {"data": json.loads(ns.data), "frame": ns.frame, "confirmed": ns.confirmed or GUARD_OFF}, title="fill-form", tab=True))


SNAP_DIR = STATE_DIR / "snapshots"


def s_snapshot(sp):
    sp.add_argument("action", choices=["save", "diff", "list"])
    sp.add_argument("name", nargs="?")
    sp.add_argument("--selector", "-s", default="body")
    sp.add_argument("--update", action="store_true", help="with diff: store the new text as the baseline afterwards")


@command("snapshot", "save page text under a name, later `diff` it to see what changed (prices, stock, new items)", s_snapshot)
def c_snapshot(ctx, ns):
    import difflib
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    if ns.action == "list":
        emit({"snapshots": sorted(f.stem for f in SNAP_DIR.glob("*.txt"))})
        return
    if not ns.name:
        raise SystemExit("give a snapshot NAME")
    d = run_js(ctx, ns, "const t = await getTab(A.tab_id); out({ ...(await brief(t)), text: await t.playwright.locator(A.sel).first().innerText(TO) });",
               {"sel": ns.selector}, title="snapshot", tab=True, retry=2)
    lines = [ln.strip() for ln in d["text"].splitlines() if ln.strip()]
    stem = re.sub(r"[^\w.\-]+", "_", ns.name)
    f = SNAP_DIR / (stem + ".txt")
    if ns.action == "save" or not f.is_file():
        f.write_text("\n".join(lines), encoding="utf-8")
        emit({"saved": str(f), "lines": len(lines), "url": d["url"], **({"note": "no baseline existed, saved one"} if ns.action == "diff" else {})})
        return
    old = f.read_text(encoding="utf-8").splitlines()
    diff = [ln for ln in difflib.unified_diff(old, lines, "before", "after", lineterm="", n=0) if not ln.startswith(("---", "+++", "@@"))]
    if ns.update:
        f.write_text("\n".join(lines), encoding="utf-8")
    emit({"changed": bool(diff), "added": sum(1 for x in diff if x.startswith("+")), "removed": sum(1 for x in diff if x.startswith("-")),
          "diff": diff[:80], "baseline_updated": bool(ns.update)})


# ------------------------------------------------------------------ tab organiser (dry-run by default)
def s_organize(sp):
    sp.add_argument("plan", choices=["dedupe", "group-by-domain"])
    sp.add_argument("--min", type=int, default=3, help="group-by-domain: minimum tabs of one domain to group")
    sp.add_argument("--window", type=int)
    sp.add_argument("--apply", action="store_true", help="actually do it (default: only show the plan)")
    add_confirm(sp, "the user asked for this reorganisation")


@command("tabs-organize", "plan (dry-run) or apply: close duplicate tabs, or group same-domain tabs; --apply needs --confirmed", s_organize)
def c_organize(ctx, ns):
    if ns.apply:
        _need(ns, "this changes or closes the user's tabs.")
    emit(run_js(ctx, ns, r"""
const mg = await mgmt();
const tabs = await mg.tabs.query(A.window != null ? { windowId: A.window } : {});
const host = (u) => { try { return new URL(u).host.replace(/^www\./, ""); } catch (e) { return ""; } };
if (A.plan === "dedupe") {
  const seen = {}, dups = [];
  for (const t of tabs) {
    if (t.pinned || !t.url || /^chrome/.test(t.url)) continue;
    const k = t.url.split("#")[0];
    if (seen[k]) dups.push({ id: t.id, title: (t.title || "").slice(0, 60), url: t.url.slice(0, 100), keeps: seen[k] }); else seen[k] = t.id;
  }
  if (A.apply) for (const d of dups) await mg.tabs.remove(d.id);
  out({ plan: "dedupe", duplicates: dups, applied: !!A.apply });
} else {
  const byWin = {};
  for (const t of tabs) {
    if (t.pinned || t.groupId !== -1 || !/^https?:/.test(t.url || "")) continue;
    const h = host(t.url); if (!h) continue;
    byWin[t.windowId] = byWin[t.windowId] || {};
    byWin[t.windowId][h] = byWin[t.windowId][h] || [];
    byWin[t.windowId][h].push(t.id);
  }
  const groups = [];
  for (const [w, doms] of Object.entries(byWin)) for (const [h, ids] of Object.entries(doms)) if (ids.length >= A.min) groups.push({ window: Number(w), domain: h, tabs: ids });
  if (A.apply) for (const g of groups) { const gid = await mg.tabs.group({ tabIds: g.tabs }); await mg.tabGroups.update(gid, { title: g.domain }); g.groupId = gid; }
  out({ plan: "group-by-domain", groups, applied: !!A.apply });
}""", {"plan": ns.plan, "min": ns.min, "window": ns.window, "apply": ns.apply}, title="tabs-organize"))


# ------------------------------------------------------------------ playbooks: YouTube search, Google Play profile
def yt_sp(sort, when, kind):
    """YouTube's `sp` filter is a tiny protobuf: sort(1) + filters{upload date(1), type(2), duration(3)}."""
    sort_v = {"relevance": 0, "date": 2, "views": 3}[sort]
    date_v = {"any": 0, "hour": 1, "today": 2, "week": 3, "month": 4, "year": 5}[when]
    flt = b""
    if date_v:
        flt += bytes([0x08, date_v])
    if kind in ("video", "short"):
        flt += bytes([0x10, 1]) if kind == "video" else bytes([0x18, 1])  # short = videos under 4 minutes
    raw = (bytes([0x08, sort_v]) if sort_v else b"") + ((bytes([0x12, len(flt)]) + flt) if flt else b"")
    return base64.urlsafe_b64encode(raw).decode().replace("=", "%3D") if raw else ""


def s_yt(sp):
    sp.add_argument("--query", required=True)
    sp.add_argument("--when", choices=["any", "hour", "today", "week", "month", "year"], default="any")
    sp.add_argument("--sort", choices=["relevance", "date", "views"], default="relevance")
    sp.add_argument("--kind", choices=["any", "video", "short"], default="video", help="short = videos under 4 minutes")
    sp.add_argument("--limit", type=int, default=15)
    sp.add_argument("--keep-tab", action="store_true")


@command("yt-search", "playbook: YouTube search with date/sort/kind filters -> title, url, channel line, views text (own tab, closed after)", s_yt)
def c_yt(ctx, ns):
    import urllib.parse
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(ns.query)
    sp_ = yt_sp(ns.sort, ns.when, ns.kind)
    if sp_:
        url += "&sp=" + sp_
    d = run_js(ctx, ns, 'const t = await cua.createBrowserTab(BID, A.url, { sessionName: "▶️ YouTube search" }); out(await brief(t));', {"url": url}, title="yt open", timeout_ms=90000)
    tid = d["id"]
    ctx.track_opened(tid)
    ctx.tab_override = tid
    try:
        time.sleep(3)
        res = run_js(ctx, ns, r"""
const t = await getTab(A.tab_id);
const rows = await t.playwright.locator("ytd-video-renderer, ytd-reel-item-renderer, ytm-shorts-lockup-view-model").evaluateAll((els, n) => els.slice(0, n).map((e) => {
  const a = e.querySelector("a#video-title") || e.querySelector("a[href*='/watch'], a[href*='/shorts/']");
  return { title: ((a && (a.getAttribute("title") || a.innerText)) || "").replace(/\s+/g, " ").trim().slice(0, 140), href: a ? a.getAttribute("href") : null,
           text: (e.innerText || "").replace(/\n+/g, " | ").slice(0, 200) };
}), A.limit);
out({ rows });""", {"limit": ns.limit}, title="yt extract", tab=True, retry=2)
        rows = []
        for r in res["rows"]:
            views, age = yt_views_age(r["text"])
            rows.append({**r, "url": ("https://www.youtube.com" + r["href"]) if r["href"] and r["href"].startswith("/") else r["href"],
                         "views": views, "age_days": age, "views_per_day": round(views / max(age, 0.5)) if views and age is not None else None})
        hot = sorted([r for r in rows if r["views_per_day"]], key=lambda r: -r["views_per_day"])[:5]
        emit({"query": ns.query, "filters": {"when": ns.when, "sort": ns.sort, "kind": ns.kind}, "count": len(rows),
              "hottest_by_views_per_day": [{"title": r["title"][:80], "views": r["views"], "age_days": r["age_days"], "views_per_day": r["views_per_day"]} for r in hot],
              "results": rows})
    finally:
        if not ns.keep_tab:
            try:
                run_js(ctx, ns, "const t = await getTab(A.tab_id); await t.close(); out({ ok: true });", title="yt close", tab=True)
            except SystemExit:
                pass
            ctx.untrack(tid)


def s_play(sp):
    sp.add_argument("targets", nargs="+", help="package id (com.example.app) or a search phrase")
    sp.add_argument("--lang", default="en")
    sp.add_argument("--out", help="folder: writes play.json (picked up by `report`)")
    sp.add_argument("--keep-tab", action="store_true")


@command("play-profile", "playbook: Google Play listing facts (title, developer, rating, reviews, downloads, ads/IAP) for packages or search phrases", s_play)
def c_play(ctx, ns):
    d = run_js(ctx, ns, 'const t = await cua.createBrowserTab(BID, "https://play.google.com/store", { sessionName: "📱 Play lookup" }); out(await brief(t));', {},
               title="play open", timeout_ms=90000)
    tid = d["id"]
    ctx.track_opened(tid)
    ctx.tab_override = tid
    results = []
    try:
        for target in ns.targets:
            pkg = target if re.match(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$", target) else None
            try:
                r = run_js(ctx, ns, r"""
const t = await getTab(A.tab_id);
let pkg = A.pkg;
if (!pkg) {
  await t.goto("https://play.google.com/store/search?q=" + encodeURIComponent(A.q) + "&c=apps&hl=" + A.lang + "&gl=US");
  await t.playwright.waitForTimeout(2500);
  const href = await t.playwright.evaluate(() => { const a = document.querySelector("a[href*='/store/apps/details?id=']"); return a ? a.getAttribute("href") : null; });
  const m = href && href.match(/id=([^&]+)/); pkg = m ? m[1] : null;
}
if (!pkg) out({ query: A.q, error: "no result" });
else {
  await t.goto("https://play.google.com/store/apps/details?id=" + pkg + "&hl=" + A.lang + "&gl=US");
  await t.playwright.waitForTimeout(2500);
  const info = await t.playwright.evaluate(() => {
    const tx = document.body.innerText.replace(/\n+/g, " | ");
    const g = (re) => { const m = tx.match(re); return m ? m[1] : null; };
    const h1 = document.querySelector("h1");
    return { title: h1 ? h1.innerText : null, developer: g(/\| ([^|]+?) \| (?:Contains ads|In-app purchases)/), contains_ads: /Contains ads/.test(tx), in_app_purchases: /In-app purchases/.test(tx),
             rating: g(/\| (\d\.\d) \| star/), reviews: g(/\| ([\d.,]+[KM]?) reviews/), downloads: g(/\| ([\d.,]+[KMB]?\+) \| Downloads/), age: g(/Downloads \| ([^|]+?) \|/) };
  });
  out({ package: pkg, query: A.q, url: "https://play.google.com/store/apps/details?id=" + pkg, ...info });
}""", {"pkg": pkg, "q": target, "lang": ns.lang}, title="play profile", tab=True, timeout_ms=90000)
                results.append(r)
            except SystemExit as e:
                results.append({"query": target, "error": str(e)[:120]})
        res = {"count": len(results), "apps": results, "note": "search-phrase matches are by name and can be the wrong app: check `title`/`developer`"}
        if getattr(ns, "out", None):
            res["file"] = write_out(ns.out, "play.json", res)
        emit(res)
    finally:
        if not ns.keep_tab:
            try:
                run_js(ctx, ns, "const t = await getTab(A.tab_id); await t.close(); out({ ok: true });", title="play close", tab=True)
            except SystemExit:
                pass
            ctx.untrack(tid)


# ------------------------------------------------------------------ parsing helpers (numbers, ages) shared by the playbooks
_MULT = {"k": 1e3, "n": 1e3, "nghìn": 1e3, "ngàn": 1e3, "m": 1e6, "tr": 1e6, "triệu": 1e6, "b": 1e9, "tỷ": 1e9}


def to_num(text):
    """'1.2M' -> 1200000, '1,6 N' -> 1600, '5,000' -> 5000, '629' -> 629. None when no number."""
    if text is None:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)*)\s*(nghìn|ngàn|triệu|tỷ|tr|k|m|b|n)?\b", str(text).replace("\u00a0", " "), re.I)
    if not m:
        return None
    raw, suf = m.group(1), (m.group(2) or "").lower()
    if suf:
        return int(float(raw.replace(",", ".")) * _MULT[suf]) if raw.count(".") + raw.count(",") <= 1 else int(float(raw.replace(",", "").replace(".", "")) * _MULT[suf])
    return int(re.sub(r"[.,]", "", raw))


_AGE_UNITS = [(r"giây|second|sec", 1 / 86400), (r"phút|minute|min", 1 / 1440), (r"giờ|hour|hr", 1 / 24), (r"ngày|day", 1), (r"tuần|week", 7),
              (r"tháng|month", 30), (r"năm|year", 365)]


def age_days(text):
    """'22 giờ trước' / '3 days ago' / '2w ago' -> days (float). None if no relative age found."""
    if not text:
        return None
    for rx, mult in _AGE_UNITS:
        m = re.search(r"(\d+)\s*(?:" + rx + r")s?\b", str(text), re.I)
        if m:
            return round(int(m.group(1)) * mult, 2)
    m = re.search(r"\b(\d+)\s*([hdw])\s*ago", str(text), re.I)
    if m:
        return int(m.group(1)) * {"h": 1 / 24, "d": 1, "w": 7}[m.group(2).lower()]
    return None


def yt_views_age(card_text):
    """YouTube result card text (' | '-separated) -> (views, age_days). Counts are the first pure number token that is not a duration."""
    views = age = None
    for tok in [x.strip() for x in card_text.split("|")]:
        if views is None and re.fullmatch(r"[\d.,]+\s*(?:N|Tr|K|M|B|tỷ)?\s*(?:lượt xem|views?)?", tok, re.I) and ":" not in tok:
            views = to_num(tok)
        elif age is None and re.search(r"trước|ago", tok, re.I):
            age = age_days(tok)
    return views, age


def date_to_days(text):
    """TikTok-style dates: '2025-11-19', '1-3' (month-day this year), '4d ago' -> days since."""
    t = (text or "").strip()
    today = _dt.date.today()
    try:
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
        if m:
            return (today - _dt.date(int(m[1]), int(m[2]), int(m[3]))).days
        m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", t)
        if m:
            d = _dt.date(today.year, int(m[1]), int(m[2]))
            return (today - (d if d <= today else d.replace(year=today.year - 1))).days
    except ValueError:
        return None
    return age_days(t)


def hashtags(texts, top=20):
    c = {}
    for tx in texts:
        for h in set(re.findall(r"#([\w\u00C0-\u1EF9]+)", tx or "")):
            c[h.lower()] = c.get(h.lower(), 0) + 1
    return [{"tag": "#" + k, "count": v} for k, v in sorted(c.items(), key=lambda kv: -kv[1])[:top]]


# ------------------------------------------------------------------ own-tab helpers for playbooks
def own_tab(ctx, ns, url, session):
    d = run_js(ctx, ns, 'const t = await cua.createBrowserTab(BID, A.url, { sessionName: A.s }); out({ id: t.id });', {"url": url, "s": session},
               title="open", timeout_ms=90000)
    ctx.track_opened(d["id"])  # tracked before anything else can fail
    ctx.tab_override = d["id"]
    return d["id"]


def close_own(ctx, ns, tid, keep=False):
    """Close a playbook's tab. Untrack it only when it is really gone, so `cleanup` can still catch a tab a flaky close missed."""
    if keep:
        return
    ctx.tab_override = tid
    try:
        run_js(ctx, ns, """
try { const t = await getTab(A.tab_id); await t.close(); }
catch (e) { const mg = await mgmt(); await mg.tabs.remove(Number(A.tab_id)); }
out({ ok: true });""", title="close", tab=True, retry=2)
        ctx.untrack(tid)
    except SystemExit as e:
        if "not found" in str(e).lower() or "no tab with id" in str(e).lower():
            ctx.untrack(tid)


def goto(ctx, ns, url, wait=5.0):
    run_js(ctx, ns, "const t = await getTab(A.tab_id); await t.goto(A.url); out({ ok: true });", {"url": url}, title="goto", tab=True, retry=2, timeout_ms=90000)
    time.sleep(wait)


def page_eval(ctx, ns, fn_js, arg=None, title="extract", timeout_ms=90000):
    """Evaluate a JS function literal `(arg) => ...` in the page (read-only DOM scope) and return its value."""
    return run_js(ctx, ns, "const t = await getTab(A.tab_id); out(await t.playwright.evaluate(" + fn_js + ", A.arg));", {"arg": arg}, title=title, tab=True,
                  retry=2, timeout_ms=timeout_ms)


def press_end(ctx, ns, times, wait=1.6):
    for _ in range(max(0, times)):
        try:
            run_js(ctx, ns, 'const t = await getTab(A.tab_id); await t.playwright.locator("body").press("End", { timeoutMs: 3000 }); out({ ok: true });', title="scroll", tab=True)
        except SystemExit:
            pass
        time.sleep(wait)


def write_out(out_dir, name, data):
    if not out_dir:
        return None
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    if name.endswith(".csv"):
        rows = data or []
        keys = []
        for r in rows:
            for k in r:
                if k not in keys:
                    keys.append(k)
        with f.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=keys or ["empty"], extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v) for k, v in r.items()})
    else:
        f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return str(f)


# ------------------------------------------------------------------ google-ads: Google Ads Transparency Center
def s_gads(sp):
    sp.add_argument("--query", required=True, help="advertiser name or website (the center searches advertisers/domains, not ad text)")
    sp.add_argument("--region", default="anywhere", help="anywhere, or a country code like US, VN")
    sp.add_argument("--advertisers", type=int, default=3, help="how many matching advertisers to open")
    sp.add_argument("--details", type=int, default=6, help="creative detail pages to read per advertiser (last shown, format, variants, countries)")
    sp.add_argument("--scrolls", type=int, default=2, help="load more creatives on each advertiser page")
    sp.add_argument("--out", help="folder for google_ads.json / google_ads.csv")
    sp.add_argument("--keep-tab", action="store_true")


GADS_SUGGEST = r"""() => [...document.querySelectorAll("material-select-item[role=option]")].map((e) => (e.innerText || "").split("\n").map((x) => x.trim()).filter(Boolean))"""
GADS_GRID = r"""() => {
  const info = document.querySelector("advertiser-info-card");
  const cards = [...document.querySelectorAll("creative-preview")].map((c) => {
    const a = c.querySelector("a[href*='/creative/']");
    const mi = c.querySelector("material-icon, i.material-icons, .material-icons");
    const raw = ((mi && mi.innerText) || "").trim().toLowerCase();
    const icon = raw === "videocam" ? "video" : raw === "image" || raw === "photo" ? "image" : raw === "text_fields" || raw === "notes" ? "text" : raw ? raw : "image/text";
    const img = c.querySelector("img");
    return { href: a ? a.getAttribute("href") : null, format_icon: icon, preview: img ? img.src : null };
  });
  return { info: info ? info.innerText.replace(/\n+/g, " | ") : "", cards, url: location.href };
}"""
GADS_DETAIL = r"""() => {
  const tx = document.body.innerText.replace(/\n+/g, " | ");
  const g = (re) => { const m = tx.match(re); return m ? m[1].trim() : null; };
  const flags = [...document.querySelectorAll("img[src*='/images/flags/']")].map((i) => (i.src.match(/flags\/(\w+)_flag/) || [])[1]).filter(Boolean);
  const previews = [...document.querySelectorAll("img[src*='simgad'], img[src*='googleusercontent'], img[src*='ytimg']")].map((i) => i.src).filter((u) => !/ogw\//.test(u)).slice(0, 3);
  return { last_shown: g(/(?:Lần hiển thị gần đây nhất|Last shown):\s*([^|]+)/), first_shown: g(/(?:Lần hiển thị đầu tiên|First shown):\s*([^|]+)/),
           format: g(/(?:Định dạng|Format):\s*([^|]+)/), variants: g(/\d+\/(\d+)\s*(?:biến thể|variations?)/), countries: [...new Set(flags)], previews };
}"""


@command("google-ads", "playbook: Google Ads Transparency Center - advertisers matching a name/domain, their creatives, last shown, format, variants, countries (own tab, closed after)", s_gads)
def c_gads(ctx, ns):
    home = f"https://adstransparency.google.com/?region={ns.region}"
    tid = own_tab(ctx, ns, home, "📊 Google Ads scan")
    result = {"source": "Google Ads Transparency Center", "query": ns.query, "region": ns.region, "advertisers": []}
    try:
        time.sleep(4)

        def suggestions():
            goto(ctx, ns, home, 3)
            run_js(ctx, ns, "const t = await getTab(A.tab_id); const l = t.playwright.locator('search-input input').first(); await l.fill('', TO); await l.pressSequentially(A.q, TO); out({ ok: true });",
                   {"q": ns.query}, title="google-ads type", tab=True, retry=2)
            for _ in range(8):
                time.sleep(1.2)
                opts = page_eval(ctx, ns, GADS_SUGGEST, title="google-ads suggest")
                if opts:
                    return opts
            return []

        opts = suggestions()
        parsed = []
        for lines in opts:
            joined = " | ".join(lines)
            parsed.append({"name": lines[0], "verified": bool(re.search(r"xác minh|verified", joined, re.I)),
                           "approx_ads": to_num(re.search(r"(?:Xấp xỉ|About|~)\s*([^|]+?)\s*(?:quảng cáo|ads)", joined, re.I).group(1)) if re.search(r"(?:Xấp xỉ|About|~)\s*([^|]+?)\s*(?:quảng cáo|ads)", joined, re.I) else None,
                           "raw": joined[:160]})
        result["suggestions"] = parsed
        if not parsed:
            result["note"] = "no advertiser matched; the center searches advertiser names and domains (try the brand/company or the website)"
        for i in range(min(ns.advertisers, len(parsed))):
            if i > 0:
                suggestions()
            run_js(ctx, ns, "const t = await getTab(A.tab_id); await t.playwright.locator('material-select-item[role=option]').nth(A.i).click(TO); out({ ok: true });",
                   {"i": i}, title="google-ads open advertiser", tab=True, retry=2)
            time.sleep(5)
            press_end(ctx, ns, ns.scrolls)
            grid = page_eval(ctx, ns, GADS_GRID, title="google-ads grid")
            fmt = {}
            for c in grid["cards"]:
                fmt[c["format_icon"]] = fmt.get(c["format_icon"], 0) + 1
            adv = {**parsed[i], "page": grid["url"].split("?")[0], "info": grid["info"][:200], "creatives_loaded": len(grid["cards"]),
                   "all_creatives": ["https://adstransparency.google.com" + c["href"] for c in grid["cards"] if c["href"]],
                   "format_icons": fmt, "creatives": []}
            for c in grid["cards"][: ns.details]:
                if not c["href"]:
                    continue
                url = "https://adstransparency.google.com" + c["href"]
                try:
                    goto(ctx, ns, url, 3.5)
                    d = page_eval(ctx, ns, GADS_DETAIL, title="google-ads detail")
                except SystemExit as e:
                    d = {"error": str(e)[:100]}
                adv["creatives"].append({"url": url, "format_icon": c["format_icon"], **d})
            result["advertisers"].append(adv)
        rows = [{"advertiser": a["name"], "approx_ads": a.get("approx_ads"), "verified": a.get("verified"), **{k: v for k, v in c.items()}}
                for a in result["advertisers"] for c in a["creatives"]]
        result["files"] = {"json": write_out(ns.out, "google_ads.json", result), "csv": write_out(ns.out, "google_ads.csv", rows)} if ns.out else None
    finally:
        close_own(ctx, ns, tid, ns.keep_tab)
    emit(result)


# ------------------------------------------------------------------ tiktok-ads: TikTok Ad Library (+ Creative Center Top Ads)
def s_tads(sp):
    sp.add_argument("--query", required=True, help="advertiser name or keyword")
    sp.add_argument("--days", type=int, default=30, help="last shown within N days")
    sp.add_argument("--region", default="all", help="'all' or an EU/EEA country code the library supports")
    sp.add_argument("--more", type=int, default=2, help="click 'View more' this many times (~12 cards each)")
    sp.add_argument("--details", type=int, default=5, help="detail pages to read (targeting, paid by, category, landing page)")
    sp.add_argument("--top-ads", action="store_true", help="also read the public part of TikTok Creative Center Top Ads (likes, CTR, budget)")
    sp.add_argument("--out")
    sp.add_argument("--keep-tab", action="store_true")


TADS_CARDS = r"""() => ({
  total: ((document.body.innerText.match(/Total ads:\s*([\d,.]+)/) || [])[1] || null),
  cards: [...document.querySelectorAll(".ad_card")].map((c) => {
    const a = c.querySelector("a[href*='ad_id=']");
    const vals = [...c.querySelectorAll(".ad_item_value")].map((v) => v.innerText.trim());
    const img = c.querySelector("img");
    return { ad_id: a ? (a.getAttribute("href").match(/ad_id=(\d+)/) || [])[1] : null, advertiser: ((c.querySelector(".ad_info_text") || {}).innerText || "").trim(),
             first_shown: vals[0] || null, last_shown: vals[1] || null, unique_users_seen: vals[2] || null, thumbnail: img && !/^data:/.test(img.src) ? img.src : null };
  }),
})"""
TADS_DETAIL = r"""() => {
  const tx = document.body.innerText;
  const after = (label) => { const i = tx.indexOf(label); if (i < 0) return null; return tx.slice(i + label.length).split("\n").map((x) => x.trim()).filter(Boolean)[0] || null; };
  const countries = [...new Set([...document.querySelectorAll("table tr")].map((tr) => [...tr.querySelectorAll("td")].map((td) => td.innerText.trim())).filter((r) => r.length >= 2 && /^\d+$/.test(r[0])).map((r) => r[1]))];
  return { paid_by: after("Ad paid for by"), registered_location: after("Advertiser's registered location"), landing_page: after("External website linked"),
           category: after("Ad Category"), target_countries: countries };
}"""
TOPADS = r"""() => {
  const tx = document.body.innerText.replace(/\n+/g, " | ");
  return tx.split("See analytics").slice(0, -1).map((chunk) => {
    const toks = chunk.split(" | ").map((x) => x.trim()).filter(Boolean).slice(-9);
    const c = toks.join(" | ");
    const g = (re) => { const m = c.match(re); return m ? m[1] : null; };
    const li = toks.findIndex((x) => /^Likes$/i.test(x));
    return { objective: li >= 3 ? toks[li - 3] : null, industry: li >= 2 ? toks[li - 2] : null,
             likes: g(/([\d.,]+[KM]?)\s*\|?\s*Likes/i), ctr_rank: g(/Top\s*(\d+%)\s*\|?\s*CTR/i), budget: g(/(Low|Medium|High)\s*\|?\s*Budget/i) };
  }).filter((r) => r.likes || r.ctr_rank);
}"""


@command("tiktok-ads", "playbook: TikTok Ad Library search (advertiser, first/last shown, users seen, targeting, paid by, landing page) + optional Creative Center Top Ads", s_tads)
def c_tads(ctx, ns):
    import urllib.parse
    end = int(time.time() * 1000)
    start = end - ns.days * 86400 * 1000
    url = (f"https://library.tiktok.com/ads?region={ns.region}&start_time={start}&end_time={end}&adv_name={urllib.parse.quote(ns.query)}"
           "&adv_biz_ids=&query_type=1&sort_type=last_shown_date,desc&ad_type=0&ad_status=1&ages=all&ad_reach=all&gender=ALL")
    tid = own_tab(ctx, ns, url, "📊 TikTok Ads scan")
    result = {"source": "TikTok Ad Library (mostly ads shown in the EU/EEA)", "query": ns.query, "days": ns.days}
    try:
        time.sleep(7)
        for _ in range(max(0, ns.more)):
            try:
                run_js(ctx, ns, "const t = await getTab(A.tab_id); await t.playwright.getByText('View more', { exact: true }).first().click({ timeoutMs: 4000 }); out({ ok: true });",
                       title="tiktok-ads more", tab=True)
                time.sleep(3)
            except SystemExit:
                break
        res = page_eval(ctx, ns, TADS_CARDS, title="tiktok-ads cards")
        cards = res["cards"]
        for c in cards:
            c["users_seen_num"] = to_num(c.get("unique_users_seen"))
            c["detail_url"] = f"https://library.tiktok.com/ads/detail/?ad_id={c['ad_id']}" if c.get("ad_id") else None
        by = {}
        for c in cards:
            by[c["advertiser"]] = by.get(c["advertiser"], 0) + 1
        result.update({"total_reported": to_num(res.get("total")), "cards_loaded": len(cards),
                       "top_advertisers": [{"advertiser": k, "cards": v} for k, v in sorted(by.items(), key=lambda kv: -kv[1])[:10]]})
        for c in cards[: ns.details]:
            if not c["detail_url"]:
                continue
            try:
                goto(ctx, ns, c["detail_url"], 4)
                c.update(page_eval(ctx, ns, TADS_DETAIL, title="tiktok-ads detail"))
            except SystemExit as e:
                c["detail_error"] = str(e)[:100]
        result["ads"] = cards
        if ns.top_ads:
            try:
                goto(ctx, ns, "https://ads.tiktok.com/business/creativecenter/inspiration/topads/pc/en?period=30&region=US&keyword=" + urllib.parse.quote(ns.query), 7)
                cards = page_eval(ctx, ns, TOPADS, title="tiktok topads")
                for c in cards:
                    c["likes_num"] = to_num(c.get("likes"))
                result["creative_center_top_ads"] = {"note": "public view shows only a few cards and the keyword filter may be ignored; logging in to TikTok for Business shows all",
                                                     "cards": cards}
            except SystemExit as e:
                result["creative_center_top_ads"] = {"error": str(e)[:120]}
        result["files"] = {"json": write_out(ns.out, "tiktok_ads.json", result), "csv": write_out(ns.out, "tiktok_ads.csv", cards)} if ns.out else None
    finally:
        close_own(ctx, ns, tid, ns.keep_tab)
    emit(result)


# ------------------------------------------------------------------ tiktok-trends: organic videos, likes/comments/shares, hashtags, Creative Center trends
def s_ttrends(sp):
    sp.add_argument("--query", required=True)
    sp.add_argument("--scrolls", type=int, default=2)
    sp.add_argument("--details", type=int, default=6, help="open the top N videos (by likes) for comments/shares/saves/date")
    sp.add_argument("--region", default="US", help="Creative Center trending hashtags region")
    sp.add_argument("--no-hashtag-trends", action="store_true")
    sp.add_argument("--out")
    sp.add_argument("--keep-tab", action="store_true")


TT_SEARCH = r"""() => [...document.querySelectorAll("[data-e2e='search_video-item']")].map((it) => {
  const a = it.querySelector("a[href*='/video/']") || it.closest("a");
  const strong = it.querySelector("strong");
  const icon = it.querySelector("svg");
  const card = it.closest("[class*='DivItemContainer']") || it.parentElement.parentElement || it;
  const desc = card.querySelector("[data-e2e='search-card-desc']") || card.querySelector("[data-e2e='search-card-video-caption']");
  const user = card.querySelector("[data-e2e='search-card-user-unique-id']");
  const meta = (card.innerText || "").split("\n").map((x) => x.trim()).filter(Boolean);
  return { url: a ? a.href : null, grid_count: strong ? strong.innerText.trim() : null, grid_metric: icon && /like/i.test(icon.getAttribute("class") || "") ? "likes" : "views",
           caption: desc ? desc.innerText.replace(/\s+/g, " ").trim().slice(0, 300) : "", author: user ? user.innerText.trim() : null, date_text: meta[meta.length - 1] || null };
})"""
TT_VIDEO = r"""() => {
  const g = (k) => { const e = document.querySelector("[data-e2e='" + k + "']"); return e ? e.innerText.trim() : null; };
  const nick = g("browser-nickname") || "";
  return { likes: g("like-count"), comments: g("comment-count"), shares: g("share-count"), saves: g("favorite-count") || g("undefined-count"),
           date_text: (nick.split("·").pop() || "").trim(), music: (g("browse-music") || g("video-music") || "").slice(0, 80) };
}"""
CC_HASHTAGS = r"""() => {
  const tx = document.body.innerText;
  const k = tx.split("\n").map((x) => x.trim()).filter(Boolean);
  const rows = [];
  for (let i = 1; i < k.length; i++) {
    if (!/^#\S+/.test(k[i]) || !/^\d+$/.test(k[i - 1])) continue;
    const row = { rank: Number(k[i - 1]), hashtag: k[i], industry: null, posts: null, views: null };
    for (let j = i + 1; j < Math.min(i + 8, k.length); j++) {
      if (/^Posts$/i.test(k[j])) row.posts = k[j - 1];
      else if (/^Views$/i.test(k[j])) { row.views = k[j - 1]; break; }
      else if (j === i + 1 && !/^[\d.,]+[KMB]?$/.test(k[j])) row.industry = k[j];
    }
    rows.push(row);
  }
  return { rows, login_wall: /Log in or sign up/i.test(tx) };
}"""


@command("tiktok-trends", "playbook: TikTok organic heat for a keyword - videos with likes/comments/shares/saves, engagement ranking, top hashtags, + Creative Center trending hashtags", s_ttrends)
def c_ttrends(ctx, ns):
    import urllib.parse
    tid = own_tab(ctx, ns, "https://www.tiktok.com/search/video?q=" + urllib.parse.quote(ns.query), "🔥 TikTok trends")
    result = {"source": "tiktok.com search (uses the TikTok login of this Chrome profile)", "query": ns.query}
    try:
        time.sleep(7)
        press_end(ctx, ns, ns.scrolls, 2.2)
        vids = page_eval(ctx, ns, TT_SEARCH, title="tiktok search")
        if not vids:
            result["note"] = "no video results (TikTok may require login in this profile, or show a captcha: check with `look`)"
        for v in vids:
            v[v["grid_metric"]] = to_num(v.pop("grid_count"))
            v["age_days"] = date_to_days(v.get("date_text"))
        vids.sort(key=lambda v: -(v.get("likes") or v.get("views") or 0))
        for v in vids[: ns.details]:
            if not v.get("url"):
                continue
            try:
                goto(ctx, ns, v["url"], 4)
                d = page_eval(ctx, ns, TT_VIDEO, title="tiktok video")
                for k in ("likes", "comments", "shares", "saves"):
                    if d.get(k) is not None:
                        v[k] = to_num(d[k])
                if d.get("date_text"):
                    v["age_days"] = date_to_days(d["date_text"]) if date_to_days(d["date_text"]) is not None else v.get("age_days")
                v["music"] = d.get("music")
                v["engagement"] = sum(v.get(k) or 0 for k in ("likes", "comments", "shares", "saves"))
                if v.get("age_days") is not None:
                    v["engagement_per_day"] = round(v["engagement"] / max(v["age_days"], 0.5))
            except SystemExit as e:
                v["detail_error"] = str(e)[:100]
        result.update({"videos": vids, "video_count": len(vids), "top_hashtags": hashtags([v["caption"] for v in vids]),
                       "hottest": [{"url": v["url"], "likes": v.get("likes"), "engagement": v.get("engagement"), "engagement_per_day": v.get("engagement_per_day"),
                                    "caption": v["caption"][:90]} for v in sorted(vids, key=lambda v: -(v.get("engagement_per_day") or 0))[:5] if v.get("engagement_per_day")]})
        if not ns.no_hashtag_trends:
            try:
                goto(ctx, ns, f"https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/en?period=7&region={ns.region}", 7)
                cc = page_eval(ctx, ns, CC_HASHTAGS, title="cc hashtags")
                for r in cc["rows"]:
                    r["posts_num"], r["views_num"] = to_num(r["posts"]), to_num(r["views"])
                result["creative_center_trending_hashtags"] = {"region": ns.region, "period_days": 7, **cc,
                                                               **({"note": "logged out: TikTok shows only the top few; log in to TikTok for Business for the full list"} if cc["login_wall"] else {})}
            except SystemExit as e:
                result["creative_center_trending_hashtags"] = {"error": str(e)[:120]}
        result["files"] = {"json": write_out(ns.out, "tiktok_trends.json", result), "csv": write_out(ns.out, "tiktok_videos.csv", vids)} if ns.out else None
    finally:
        close_own(ctx, ns, tid, ns.keep_tab)
    emit(result)


# ------------------------------------------------------------------ google-trends: keyword ideas (autocomplete), daily trending searches, Explore (best effort)
def _http_json(url, timeout=15):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="replace")


def keyword_ideas(query, lang="en", expand=False, youtube=True):
    import urllib.parse
    def sugg(q, ds=""):
        try:
            data = json.loads(_http_json("https://suggestqueries.google.com/complete/search?client=firefox&hl=" + lang + ("&ds=" + ds if ds else "") + "&q=" + urllib.parse.quote(q)))
            return data[1]
        except Exception:  # noqa: BLE001
            return []
    google = sugg(query)
    yt = sugg(query, "yt") if youtube else []
    expanded = {}
    if expand:
        for ch in "abcdefghijklmnopqrstuvwxyz":
            for t in sugg(f"{query} {ch}"):
                expanded[t] = expanded.get(t, 0) + 1
            time.sleep(0.15)
    # words that co-occur with the query across all suggestions = modifiers people search with
    words = {}
    base = set(query.lower().split())
    for t in google + yt + list(expanded):
        for w in re.findall(r"[\w\u00C0-\u1EF9]+", t.lower()):
            if w not in base and len(w) > 2:
                words[w] = words.get(w, 0) + 1
    return {"google_autocomplete": google, "youtube_autocomplete": yt, "expanded": sorted(expanded, key=lambda k: -expanded[k])[:120] if expand else None,
            "top_modifiers": [{"word": k, "count": v} for k, v in sorted(words.items(), key=lambda kv: -kv[1])[:25]]}


def trending_rss(geo, limit=20):
    import xml.etree.ElementTree as ET
    ns_ = {"ht": "https://trends.google.com/trending/rss"}
    root = ET.fromstring(_http_json(f"https://trends.google.com/trending/rss?geo={geo}", 20))
    items = []
    for it in root.iter("item"):
        items.append({"term": (it.findtext("title") or "").strip(), "approx_traffic": it.findtext("ht:approx_traffic", namespaces=ns_),
                      "traffic_num": to_num(it.findtext("ht:approx_traffic", namespaces=ns_)), "published": it.findtext("pubDate"),
                      "news": [n.findtext("ht:news_item_title", namespaces=ns_) for n in it.findall("ht:news_item", ns_)][:2]})
    return items[:limit]


def s_gtrends(sp):
    sp.add_argument("--query", help="keyword for ideas (autocomplete) and the Explore page")
    sp.add_argument("--geo", default="US", help="country for daily trending searches and Explore")
    sp.add_argument("--lang", default="en")
    sp.add_argument("--expand", action="store_true", help="a-z expansion of autocomplete (26 extra lookups) for long-tail keywords")
    sp.add_argument("--trending", type=int, default=20, help="daily trending searches to list (0 = skip)")
    sp.add_argument("--explore", action="store_true", help="also try the Google Trends Explore page in the browser (often empty for automated sessions)")
    sp.add_argument("--out")


GT_EXPLORE = r"""() => {
  const tx = document.body.innerText;
  const lines = tx.split("\n").map((x) => x.trim()).filter(Boolean);
  const rows = []; for (let i = 0; i < lines.length - 1; i++) { if (/^\d+$/.test(lines[i]) && i + 1 < lines.length) { const ch = lines.slice(i + 1, i + 6).find((x) => /[+\-−‎]\s*[\d.,]+\s*%|Breakout|Tăng vọt/i.test(x)); if (ch) rows.push({ rank: Number(lines[i]), term: lines[i + 1], change: ch.replace(/[‎]/g, "") }); } }
  const paths = [...document.querySelectorAll("svg path")].filter((p) => (p.getAttribute("d") || "").length > 300 && !/C/.test((p.getAttribute("d") || "").slice(0, 40))).length;
  return { rows: rows.slice(0, 40), chart_has_line: paths > 0 };
}"""


@command("google-trends", "playbook: keyword ideas from Google + YouTube autocomplete (+ a-z long tail), daily trending searches by country, optional Trends Explore", s_gtrends)
def c_gtrends(ctx, ns):
    import urllib.parse
    result = {"source": "Google autocomplete, Google Trends daily RSS", "geo": ns.geo}
    if ns.query:
        result["keyword_ideas"] = keyword_ideas(ns.query, ns.lang, ns.expand)
    if ns.trending:
        try:
            result["trending_now"] = trending_rss(ns.geo, ns.trending)
        except Exception as e:  # noqa: BLE001
            result["trending_now"] = {"error": str(e)[:120]}
    if ns.explore and ns.query:
        tid = own_tab(ctx, ns, f"https://trends.google.com/explore?q={urllib.parse.quote(ns.query)}&date=today%203-m&geo={ns.geo}&hl=en", "📈 Google Trends")
        try:
            time.sleep(12)
            ex = page_eval(ctx, ns, GT_EXPLORE, title="trends explore")
            if not ex["chart_has_line"] and not ex["rows"]:
                ex["note"] = "Trends returned no data to this automated session (rate limit / bot check). Open the page yourself or rely on keyword_ideas + trending_now."
            result["explore"] = ex
        except SystemExit as e:
            result["explore"] = {"error": str(e)[:120]}
        finally:
            close_own(ctx, ns, tid)
    result["files"] = {"json": write_out(ns.out, "google_trends.json", result)} if ns.out else None
    emit(result)


# ------------------------------------------------------------------ meta-trends: Facebook video search (organic posts, hashtags)
def s_mtrends(sp):
    sp.add_argument("--query", required=True)
    sp.add_argument("--scrolls", type=int, default=2)
    sp.add_argument("--out")
    sp.add_argument("--keep-tab", action="store_true")


FB_VIDEOS = r"""() => {
  const main = document.querySelector("[role=main]") || document.body;
  const caps = [...new Set([...main.querySelectorAll("[aria-label]")].map((e) => e.getAttribute("aria-label")).filter((x) => x && x.length > 25))];
  const links = [...new Set([...main.querySelectorAll("a[href]")].map((a) => a.href).filter((h) => /\/(reel|videos|watch)\b|watch\/\?v=/.test(h)).map((h) => h.split("&__")[0]))];
  const nums = (main.innerText.match(/[\d.,]+\s*(?:K|N|Tr|M|triệu|nghìn)?\s*(?:lượt xem|views|lượt bày tỏ cảm xúc|reactions)/gi) || []);
  const blocks = main.innerText.split("\n").map((x) => x.trim()).filter(Boolean);
  return { captions: caps.slice(0, 60), video_links: links.slice(0, 60), metric_snippets: nums.slice(0, 40), text_blocks: blocks.slice(0, 120) };
}"""


@command("meta-trends", "playbook: Facebook video search for a keyword (uses this profile's Facebook login) - captions, pages, hashtags; view counts only when Facebook shows them", s_mtrends)
def c_mtrends(ctx, ns):
    import urllib.parse
    tid = own_tab(ctx, ns, "https://www.facebook.com/search/videos/?q=" + urllib.parse.quote(ns.query), "🔥 Facebook trends")
    result = {"source": "facebook.com video search", "query": ns.query}
    try:
        time.sleep(8)
        press_end(ctx, ns, ns.scrolls, 2.5)
        d = page_eval(ctx, ns, FB_VIDEOS, title="facebook videos")
        result.update({"posts_found": len(d["captions"]), "captions": d["captions"], "video_links": d["video_links"], "top_hashtags": hashtags(d["captions"]),
                       "metric_snippets": d["metric_snippets"],
                       **({} if d["metric_snippets"] else {"note": "Facebook search did not show view/reaction counts; use meta-ads for paid activity and tiktok-trends/yt-search for engagement numbers"})})
        result["files"] = {"json": write_out(ns.out, "meta_trends.json", result)} if ns.out else None
    finally:
        close_own(ctx, ns, tid, ns.keep_tab)
    emit(result)


# ------------------------------------------------------------------ market-scan: everything for one keyword, one folder, one summary
def s_market(sp):
    sp.add_argument("--query", required=True, help="keyword, e.g. 'arrow puzzle'")
    sp.add_argument("--advertiser", help="advertiser/brand name for google-ads (the center needs a company or domain, not a keyword)")
    sp.add_argument("--geo", default="US")
    sp.add_argument("--out", required=True, help="output folder")
    sp.add_argument("--skip", action="append", default=[], choices=["keywords", "youtube", "tiktok", "meta-ads", "tiktok-ads", "google-ads", "meta-trends"],
                    help="skip a part (repeatable)")


@command("market-scan", "playbook: one keyword across Google (keywords, trending), YouTube (views/day), TikTok (engagement, hashtags, ads), Meta (ads, organic), Google Ads -> folder + summary.md", s_market)
def c_market(ctx, ns):
    import contextlib
    import io
    out = Path(ns.out)
    out.mkdir(parents=True, exist_ok=True)
    steps = [("keywords", "google-trends", ["--query", ns.query, "--geo", ns.geo, "--expand"]),
             ("youtube", "yt-search", ["--query", ns.query, "--when", "month", "--sort", "views", "--limit", "20"]),
             ("tiktok", "tiktok-trends", ["--query", ns.query, "--region", ns.geo]),
             ("meta-ads", "meta-ads", ["--query", ns.query, "--scrolls", "4", "--out", str(out / "meta_ads")]),
             ("tiktok-ads", "tiktok-ads", ["--query", ns.query, "--details", "3"]),
             ("google-ads", "google-ads", ["--query", ns.advertiser or ns.query, "--advertisers", "2", "--details", "4"]),
             ("meta-trends", "meta-trends", ["--query", ns.query])]
    parser = build_parser()
    summary = {"query": ns.query, "geo": ns.geo, "parts": {}}
    for key, cmd, args in steps:
        if key in ns.skip:
            continue
        sub = parser.parse_args([cmd, *args])
        for k in ("browser_sub", "tab_sub", "task_sub"):
            if hasattr(sub, k):
                delattr(sub, k)
        sub.browser, sub.tab, sub.task = None, None, ctx.task
        t0 = time.time()
        for attempt in (1, 2):  # a part that hits a transient browser error gets one more try
            buf = io.StringIO()
            try:
                ctx.tab_override = None
                with contextlib.redirect_stdout(buf):
                    COMMANDS[cmd][0](ctx, sub)
                data = json.loads(buf.getvalue().strip().splitlines()[-1])
                (out / f"{key}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                summary["parts"][key] = {"ok": True, "seconds": round(time.time() - t0), "file": str(out / f"{key}.json"), **({"attempts": 2} if attempt == 2 else {})}
                break
            except (SystemExit, Exception) as e:  # noqa: BLE001 - one source failing must not stop the scan
                summary["parts"][key] = {"ok": False, "error": str(e)[:200]}
                if attempt == 1 and any(k in str(e) for k in TRANSIENT):
                    time.sleep(3)
                    continue
                break
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    md = build_market_md(out, summary)
    (out / "summary.md").write_text(md, encoding="utf-8")
    emit({**summary, "summary_md": str(out / "summary.md")})


def build_market_md(out, summary):
    def load(k):
        f = out / f"{k}.json"
        try:
            return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None
        except ValueError:
            return None
    L = [f"# Market scan: {summary['query']} ({summary['geo']}) - {_dt.date.today().isoformat()}", ""]
    L += ["| Part | Status |", "|---|---|"] + [f"| {k} | {'ok' if v['ok'] else 'failed: ' + v['error'][:80]} |" for k, v in summary["parts"].items()] + [""]
    kw = load("keywords")
    if kw and kw.get("keyword_ideas"):
        ki = kw["keyword_ideas"]
        L += ["## Keywords (Google / YouTube autocomplete)", "", "Google: " + ", ".join(ki["google_autocomplete"][:10]), "",
              "YouTube: " + ", ".join(ki["youtube_autocomplete"][:10]), "", "Top modifiers: " + ", ".join(f"{m['word']} ({m['count']})" for m in ki["top_modifiers"][:15]), ""]
    yt = load("youtube")
    if yt:
        L += ["## YouTube - hottest by views/day (last month)", "", "| Title | Views | Age (d) | Views/day |", "|---|---|---|---|"]
        L += [f"| {r['title'][:60]} | {r['views']} | {r['age_days']} | {r['views_per_day']} |" for r in yt.get("hottest_by_views_per_day", [])] + [""]
    tt = load("tiktok")
    if tt:
        L += ["## TikTok - organic engagement", "", f"Videos read: {tt.get('video_count')}", "", "| Caption | Likes | Engagement | /day |", "|---|---|---|---|"]
        L += [f"| {h['caption'][:60]} | {h['likes']} | {h['engagement']} | {h['engagement_per_day']} |" for h in tt.get("hottest", [])]
        L += ["", "Top hashtags: " + ", ".join(f"{h['tag']} ({h['count']})" for h in tt.get("top_hashtags", [])[:12]), ""]
        cc = tt.get("creative_center_trending_hashtags") or {}
        if cc.get("rows"):
            L += ["Creative Center trending hashtags: " + ", ".join(f"{r['hashtag']} {r['views']} views" for r in cc["rows"][:10]), ""]
    ma = load("meta-ads")
    if ma:
        L += ["## Meta Ad Library", "", f"Active video ads found: {ma.get('cards')} from {ma.get('advertisers')} advertisers", "",
              "| Advertiser | Cards | Earliest start |", "|---|---|---|"] + [f"| {a['advertiser']} | {a['cards']} | {a['earliest']} |" for a in ma.get("top_advertisers", [])[:10]] + [""]
    ta = load("tiktok-ads")
    if ta:
        L += ["## TikTok Ad Library", "", f"Reported total: {ta.get('total_reported')} · loaded: {ta.get('cards_loaded')}", "",
              "| Advertiser | Cards |", "|---|---|"] + [f"| {a['advertiser']} | {a['cards']} |" for a in ta.get("top_advertisers", [])[:10]] + [""]
    ga = load("google-ads")
    if ga:
        L += ["## Google Ads Transparency", ""]
        for a in ga.get("advertisers", []):
            L += [f"- **{a['name']}** (~{a.get('approx_ads')} ads, verified={a.get('verified')}): formats {a.get('format_icons')}; "
                  f"latest shown: {', '.join(sorted({c.get('last_shown') or '' for c in a.get('creatives', []) if c.get('last_shown')})[-3:])}"]
        if not ga.get("advertisers"):
            L += [ga.get("note", "no advertiser matched")]
        L += [""]
    mt = load("meta-trends")
    if mt:
        L += ["## Facebook organic videos", "", f"Posts found: {mt.get('posts_found')}", "", "Top hashtags: " + ", ".join(f"{h['tag']} ({h['count']})" for h in mt.get("top_hashtags", [])[:12]), ""]
    L += ["## How to read the heat signals", "",
          "- YouTube: views/day = views ÷ age, the best single 'how hot right now' number.",
          "- TikTok: engagement = likes + comments + shares + saves; per day uses the post date. TikTok web does not show views on video pages.",
          "- Ad libraries publish no spend: use number of active ads, start dates (longevity) and recent bursts as signals.",
          "- Google autocomplete order reflects popularity, not volume; Trends Explore numbers may be unavailable to automated sessions."]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ run another command in-process and capture its JSON
def run_sub(ctx, cmd, args):
    import contextlib
    import io
    sub = build_parser().parse_args([cmd, *args])
    for k in ("browser_sub", "tab_sub", "task_sub"):
        if hasattr(sub, k):
            delattr(sub, k)
    sub.browser, sub.tab, sub.task = None, None, ctx.task
    err = None
    for attempt in (1, 2):
        buf = io.StringIO()
        try:
            ctx.tab_override = None
            with contextlib.redirect_stdout(buf):
                COMMANDS[cmd][0](ctx, sub)
            return json.loads(buf.getvalue().strip().splitlines()[-1])
        except (SystemExit, Exception) as e:  # noqa: BLE001
            err = e
            if attempt == 1 and any(k in str(e) for k in TRANSIENT):
                time.sleep(3)
                continue
            break
    raise SystemExit(f"{cmd} failed: {err}")


def _safe_name(x):
    return re.sub(r"[^\w.\-]+", "_", x).strip("_")[:60] or "watch"


# ------------------------------------------------------------------ appstore-profile (iTunes Search API, no browser)
def s_appstore(sp):
    sp.add_argument("targets", nargs="+", help="numeric App Store id, bundle id (com.example.app) or a search phrase")
    sp.add_argument("--country", default="us")
    sp.add_argument("--search", type=int, default=1, help="results to keep per search phrase")
    sp.add_argument("--out", help="folder: writes appstore.json (picked up by `report`)")


@command("appstore-profile", "iOS App Store facts via Apple's public lookup API: rating, rating count, developer, genre, price, version dates, size (no download counts on iOS)", s_appstore)
def c_appstore(ctx, ns):
    import urllib.parse
    apps = []
    for t in ns.targets:
        if re.fullmatch(r"\d{6,}", t):
            url = f"https://itunes.apple.com/lookup?id={t}&country={ns.country}"
        elif re.fullmatch(r"[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)+", t):
            url = f"https://itunes.apple.com/lookup?bundleId={t}&country={ns.country}"
        else:
            url = f"https://itunes.apple.com/search?term={urllib.parse.quote(t)}&entity=software&country={ns.country}&limit={max(1, ns.search)}"
        try:
            data = json.loads(_http_json(url, 20))
        except Exception as e:  # noqa: BLE001
            apps.append({"query": t, "error": str(e)[:120]})
            continue
        if not data.get("results"):
            apps.append({"query": t, "error": "no result in this country store"})
        for r in data.get("results", []):
            apps.append({"query": t, "id": r.get("trackId"), "bundle_id": r.get("bundleId"), "title": r.get("trackName"), "developer": r.get("artistName"),
                         "seller": r.get("sellerName"), "rating": round(r["averageUserRating"], 2) if r.get("averageUserRating") is not None else None,
                         "rating_count": r.get("userRatingCount"), "rating_current_version": r.get("averageUserRatingForCurrentVersion"),
                         "rating_count_current_version": r.get("userRatingCountForCurrentVersion"), "price": r.get("formattedPrice"),
                         "genre": r.get("primaryGenreName"), "genres": r.get("genres"), "released": (r.get("releaseDate") or "")[:10],
                         "updated": (r.get("currentVersionReleaseDate") or "")[:10], "version": r.get("version"),
                         "size_mb": round(int(r["fileSizeBytes"]) / 1e6, 1) if r.get("fileSizeBytes") else None, "age_rating": r.get("contentAdvisoryRating"),
                         "languages": len(r.get("languageCodesISO2A") or []), "url": r.get("trackViewUrl"), "icon": r.get("artworkUrl100"),
                         "description": (r.get("description") or "")[:300]})
    res = {"source": "Apple iTunes lookup/search API", "country": ns.country, "count": len(apps), "apps": apps,
           "note": "search phrases match by name: check title/developer; the App Store publishes no download numbers"}
    if ns.out:
        res["file"] = write_out(ns.out, "appstore.json", res)
    emit(res)


# ------------------------------------------------------------------ ads-watch: history of ad scans (first/last observed, times seen, new / not seen)
WATCH_DIR = STATE_DIR / "watch"


def _watch_records(source, data, run_dir):
    recs = []
    if source == "meta":
        f = run_dir / "meta" / "cards.json"
        for c in (json.loads(f.read_text(encoding="utf-8")) if f.is_file() else []):
            r = parse_meta_card(c)
            recs.append({"key": "meta:" + r["id"], "source": "meta", "id": r["id"], "advertiser": r["advertiser"], "start": r["start"],
                         "text": (r["primary_text"] or "")[:160], "cta": r["cta"], "video_seconds": r["video_seconds"], "url": r["library_url"]})
    elif source == "tiktok":
        for a in data.get("ads", []):
            if a.get("ad_id"):
                fs = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", (a.get("first_shown") or "").strip())
                recs.append({"key": "tiktok:" + a["ad_id"], "source": "tiktok", "id": a["ad_id"], "advertiser": a.get("advertiser"),
                             "start": f"{fs[3]}-{int(fs[1]):02d}-{int(fs[2]):02d}" if fs else a.get("first_shown"),
                             "last_shown": a.get("last_shown"), "users_seen": a.get("users_seen_num"), "url": a.get("detail_url")})
    elif source == "google":
        for adv in data.get("advertisers", []):
            det = {c["url"]: c for c in adv.get("creatives", [])}
            for u in adv.get("all_creatives", []):
                m = re.search(r"/creative/(CR\w+)", u)
                if not m:
                    continue
                d = det.get(u, {})
                recs.append({"key": "google:" + m.group(1), "source": "google", "id": m.group(1), "advertiser": adv.get("name"), "format": d.get("format"),
                             "last_shown": d.get("last_shown"), "url": u})
    return recs


def s_watch(sp):
    sp.add_argument("action", choices=["run", "show", "list", "forget"])
    sp.add_argument("name", nargs="?", help="watch name, e.g. arrow-puzzle")
    sp.add_argument("--query", help="keyword for Meta and TikTok (saved with the watch on the first run)")
    sp.add_argument("--advertiser", help="company or domain for Google Ads Transparency (saved)")
    sp.add_argument("--sources", help="comma list of meta,tiktok,google (saved; default all)")
    sp.add_argument("--scrolls", type=int, default=4, help="keep this the same between runs so 'not seen' stays meaningful")
    sp.add_argument("--out", help="also copy history.csv and the latest diff here")
    sp.add_argument("--limit", type=int, default=25, help="rows of new/not-seen ads to print")
    add_confirm(sp, "required for forget (deletes the stored history)")


@command("ads-watch", "competitor-ad history: run (scan Meta/TikTok/Google ad libraries and merge) | show | list | forget; records first/last observed, times seen, new and no-longer-seen creatives", s_watch)
def c_watch(ctx, ns):
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    if ns.action == "list":
        rows = []
        for d in sorted(x for x in WATCH_DIR.iterdir() if x.is_dir()):
            cfg = json.loads((d / "config.json").read_text(encoding="utf-8")) if (d / "config.json").is_file() else {}
            h = json.loads((d / "history.json").read_text(encoding="utf-8")) if (d / "history.json").is_file() else {"ads": {}, "runs": []}
            rows.append({"name": d.name, **cfg, "runs": len(h["runs"]), "last_run": h["runs"][-1]["ts"] if h["runs"] else None,
                         "ads_tracked": len(h["ads"]), "active": sum(1 for a in h["ads"].values() if a.get("status") == "active")})
        emit({"watches": rows, "dir": str(WATCH_DIR)})
        return
    if not ns.name:
        raise SystemExit("give a watch NAME (e.g. arrow-puzzle)")
    wdir = WATCH_DIR / _safe_name(ns.name)
    cfgf, histf = wdir / "config.json", wdir / "history.json"
    if ns.action == "forget":
        _need(ns, f"this deletes the stored history of watch '{wdir.name}'.")
        shutil.rmtree(wdir, ignore_errors=True)
        emit({"forgotten": wdir.name})
        return
    hist = json.loads(histf.read_text(encoding="utf-8")) if histf.is_file() else {"ads": {}, "runs": []}
    if ns.action == "show":
        if not hist["runs"]:
            raise SystemExit(f"watch '{wdir.name}' has no runs yet: `ads-watch run {wdir.name} --query ...`")
        last = hist["runs"][-1]["ts"]
        ads = list(hist["ads"].values())
        res = {"name": wdir.name, "runs": hist["runs"][-5:], "ads_tracked": len(ads),
               "by_status": {k: sum(1 for a in ads if a.get("status") == k) for k in ("active", "not_seen", "likely_stopped")},
               "new_in_last_run": [a for a in ads if a.get("first_observed") == last][: ns.limit],
               "not_seen_in_last_run": [a for a in ads if a.get("status") in ("not_seen", "likely_stopped") and a.get("not_seen_since") == last][: ns.limit],
               "longest_running": sorted([a for a in ads if a.get("status") == "active"], key=lambda a: -(a.get("times_seen") or 0))[: ns.limit],
               "history_csv": str(wdir / "history.csv")}
        if ns.out:
            Path(ns.out).mkdir(parents=True, exist_ok=True)
            shutil.copy2(wdir / "history.csv", Path(ns.out) / "watch_history.csv")
            res["copied_to"] = str(Path(ns.out) / "watch_history.csv")
        emit(res)
        return

    # ---- run
    cfg = json.loads(cfgf.read_text(encoding="utf-8")) if cfgf.is_file() else {}
    query = ns.query or cfg.get("query")
    if not query:
        raise SystemExit("first run needs --query (and --advertiser for Google)")
    advertiser = ns.advertiser or cfg.get("advertiser")
    sources = [x.strip() for x in (ns.sources or cfg.get("sources") or "meta,tiktok,google").split(",") if x.strip()]
    cfg = {"query": query, "advertiser": advertiser, "sources": ",".join(sources), "scrolls": ns.scrolls}
    wdir.mkdir(parents=True, exist_ok=True)
    cfgf.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    ts = _dt.datetime.now().isoformat(timespec="seconds")
    run_dir = wdir / "runs" / ts.replace(":", "")
    run_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for src in sources:
        try:
            if src == "meta":
                data = run_sub(ctx, "meta-ads", ["--query", query, "--scrolls", str(ns.scrolls), "--out", str(run_dir / "meta")])
            elif src == "tiktok":
                data = run_sub(ctx, "tiktok-ads", ["--query", query, "--more", str(max(1, ns.scrolls // 2)), "--details", "0"])
            elif src == "google":
                if not advertiser:
                    results[src] = {"ok": False, "error": "needs --advertiser (company or domain)"}
                    continue
                data = run_sub(ctx, "google-ads", ["--query", advertiser, "--advertisers", "2", "--details", "0", "--scrolls", str(ns.scrolls)])
            else:
                results[src] = {"ok": False, "error": "unknown source"}
                continue
            (run_dir / f"{src}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            results[src] = {"ok": True, "records": _watch_records(src, data, run_dir)}
        except SystemExit as e:
            results[src] = {"ok": False, "error": str(e)[:200]}
    new, back, missing = [], [], []
    # "launched" uses the ads' own start dates (Meta, TikTok), so it is not fooled by which subset the scan happened to load
    since = hist["runs"][-1]["ts"][:10] if hist["runs"] else (_dt.date.today() - _dt.timedelta(days=7)).isoformat()
    launched = []
    for src, res in results.items():
        if not res["ok"]:
            continue  # a failed source must not mark its ads as gone
        seen = set()
        for r in res["records"]:
            k = r["key"]
            if k in seen:
                continue  # the same creative can be listed under several advertisers/domains
            seen.add(k)
            if r.get("start") and r["start"] >= since and r["source"] in ("meta", "tiktok"):
                launched.append(k)
            h = hist["ads"].get(k)
            if not h:
                h = {**r, "first_observed": ts, "times_seen": 0}
                new.append(k)
            else:
                if h.get("status") in ("not_seen", "likely_stopped"):
                    back.append(k)
                h.update({kk: vv for kk, vv in r.items() if vv not in (None, "")})
            h.update(last_observed=ts, times_seen=h["times_seen"] + 1, status="active", missed_runs=0)
            h.pop("not_seen_since", None)
            hist["ads"][k] = h
        for k, h in hist["ads"].items():
            if h["source"] != src or k in seen:
                continue
            if h.get("status") == "active":
                h.update(status="not_seen", missed_runs=1, not_seen_since=ts)
                missing.append(k)
            else:
                h["missed_runs"] = h.get("missed_runs", 0) + 1
                if h["missed_runs"] >= 2:
                    h["status"] = "likely_stopped"
    run_info = {"ts": ts, "sources": {src: ({"ok": True, "seen": len({x["key"] for x in r["records"]})} if r["ok"] else {"ok": False, "error": r["error"]}) for src, r in results.items()},
                "launched_since": since, "launched": len(launched), "new": len(new), "returned": len(back), "not_seen": len(missing)}
    hist["runs"].append(run_info)
    hist["query"], hist["advertiser"] = query, advertiser
    histf.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
    cols = ["key", "source", "advertiser", "status", "first_observed", "last_observed", "times_seen", "missed_runs", "start", "last_shown", "users_seen",
            "format", "video_seconds", "cta", "text", "url"]
    with (wdir / "history.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for a in sorted(hist["ads"].values(), key=lambda a: (a["source"], a.get("advertiser") or "", a.get("first_observed") or "")):
            w.writerow(a)
    diff = {"ts": ts, "launched_since": since, "launched": [hist["ads"][k] for k in launched], "first_seen_by_watch": [hist["ads"][k] for k in new],
            "returned": [hist["ads"][k] for k in back], "not_seen": [hist["ads"][k] for k in missing]}
    (run_dir / "diff.json").write_text(json.dumps(diff, ensure_ascii=False, indent=1), encoding="utf-8")
    # keep the raw files of the last 3 runs only; the merged history keeps everything that matters
    runs = sorted(x for x in (wdir / "runs").iterdir() if x.is_dir())
    for old in runs[:-3]:
        shutil.rmtree(old, ignore_errors=True)
    by_adv = {}
    for k in launched:
        a = hist["ads"][k]
        key = f"{a['source']}:{a.get('advertiser')}"
        by_adv[key] = by_adv.get(key, 0) + 1
    if ns.out:
        Path(ns.out).mkdir(parents=True, exist_ok=True)
        shutil.copy2(wdir / "history.csv", Path(ns.out) / "watch_history.csv")
        shutil.copy2(run_dir / "diff.json", Path(ns.out) / "watch_diff.json")
    slim = lambda a: {k: a.get(k) for k in ("source", "advertiser", "start", "text", "format", "url")}  # noqa: E731
    emit({"name": wdir.name, "run": run_info, "runs_total": len(hist["runs"]), "ads_tracked": len(hist["ads"]),
          "launched_by_advertiser": sorted(({"advertiser": k, "launched": v} for k, v in by_adv.items()), key=lambda x: -x["launched"])[:15],
          "launched": [slim(hist["ads"][k]) for k in launched][: ns.limit],
          "first_seen_by_watch": [slim(hist["ads"][k]) for k in new][: ns.limit], "returned": [slim(hist["ads"][k]) for k in back][: ns.limit],
          "not_seen": [slim(hist["ads"][k]) for k in missing][: ns.limit],
          "note": ("'launched' = the ad's own start date is on/after " + since + " (Meta, TikTok): genuinely new creatives. 'first_seen_by_watch' and 'not_seen' "
                   "depend on which subset the scan loaded (it may have stopped or just ranked lower); 2 misses in a row -> likely_stopped. Keep --scrolls the same between runs."),
          "history_csv": str(wdir / "history.csv")})


# ------------------------------------------------------------------ creative-tag: frames for each creative + a tag sheet Claude fills in
TAXONOMY = {
    "hook": ["Fail", "Satisfying", "Challenge", "Curiosity", "Cute/Emotional", "Nostalgia", "Time pressure", "UGC", "Before/after", "Tutorial/Tip", "Seasonal"],
    "mechanic": ["Arrow/Tap-away", "Maze", "Sort", "Match", "Merge", "Puzzle (other)", "Pull-pin", "Block", "Word", "Not gameplay"],
    "visual": ["ASMR", "3D", "Realistic", "Cartoon", "Minimal", "Neon", "Live-action/UGC", "AI-generated", "Pixel/Retro"],
    "cta": ["Store search bar", "Play button", "Store badges", "Logo only", "Verbal", "None seen"],
}
TAG_FIELDS = ["hook", "mechanic", "visual", "structure", "cta", "emotion", "insight"]
TAG_COLS = ["id", "source", "advertiser", "start", "seconds", "frames", "video", "url"] + TAG_FIELDS + ["tagged_at"]


def _tag_sheet(d):
    return Path(d) / "creative_tags.csv"


def _read_tags(d):
    f = _tag_sheet(d)
    if not f.is_file():
        return []
    with f.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_tags(d, rows):
    with _tag_sheet(d).open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=TAG_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _video_and_tile(src_url, mp4, tile):
    """Download (if missing) and cut an 8-frame 4x2 tile (if missing). Returns (seconds, made_video, made_tile)."""
    import urllib.request
    made_v = made_t = False
    if not mp4.is_file():
        req = urllib.request.Request(src_url, headers={"User-Agent": "Mozilla/5.0"})
        mp4.parent.mkdir(parents=True, exist_ok=True)
        mp4.write_bytes(urllib.request.urlopen(req, timeout=60).read())
        made_v = True
    ffprobe, ffmpeg = _find_tool("ffprobe"), _find_tool("ffmpeg")
    if not (ffprobe and ffmpeg):
        raise SystemExit("ffmpeg/ffprobe not found (install ffmpeg, e.g. `winget install Gyan.FFmpeg`)")
    pr = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(mp4)], capture_output=True, text=True, timeout=60,
                        creationflags=NO_WINDOW)
    secs = float(json.loads(pr.stdout)["format"]["duration"])
    if not tile.is_file():
        tile.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(mp4), "-vf", f"fps={8 / secs:.5f},scale=270:-2,tile=4x2", "-frames:v", "1", "-q:v", "4", str(tile)],
                       timeout=120, creationflags=NO_WINDOW)
        made_t = True
    return round(secs, 1), made_v, made_t


def s_ctag(sp):
    sp.add_argument("action", choices=["prepare", "set", "show", "summary", "taxonomy"])
    sp.add_argument("--dir", help="folder with cards.json (a meta-ads --out folder, or market-scan's meta_ads folder)")
    sp.add_argument("--limit", type=int, default=16, help="prepare: creatives to prepare")
    sp.add_argument("--per-advertiser", type=int, default=3, help="prepare: at most N per advertiser (variety)")
    sp.add_argument("--keep-media", action="store_true", help="prepare: keep videos/frames (default: scratch, removed by cleanup; tags and report keep what matters)")
    sp.add_argument("--id", help="set: creative id")
    for f in TAG_FIELDS:
        sp.add_argument("--" + f, help=f"set: {f}" + (" (comma list; see `creative-tag taxonomy`)" if f in TAXONOMY else ""))


@command("creative-tag", "AI tagging of ad creatives: prepare (download + 8-frame tiles + tag sheet) | set (Claude writes tags after reading a tile) | show | summary | taxonomy", s_ctag)
def c_ctag(ctx, ns):
    if ns.action == "taxonomy":
        emit({"taxonomy": TAXONOMY, "free_text": ["structure", "emotion", "insight"]})
        return
    if not ns.dir:
        raise SystemExit("give --dir (folder with cards.json / creative_tags.csv)")
    d = Path(ns.dir)
    if (d / "meta_ads" / "cards.json").is_file() and not (d / "cards.json").is_file():
        d = d / "meta_ads"
    rows = _read_tags(d)
    by_id = {r["id"]: r for r in rows}
    if ns.action == "prepare":
        f = d / "cards.json"
        if not f.is_file():
            raise SystemExit(f"no cards.json in {d}: run `meta-ads --query ... --out {d}` first")
        cards = json.loads(f.read_text(encoding="utf-8"))
        recs = sorted([(parse_meta_card(c), c.get("src")) for c in cards if c.get("src")], key=lambda x: x[0]["start"] or "", reverse=True)
        per, picked = {}, []
        for r, src in recs:
            if len(picked) >= ns.limit:
                break
            if r["id"] in by_id or per.get(r["advertiser"], 0) >= ns.per_advertiser:
                continue
            per[r["advertiser"]] = per.get(r["advertiser"], 0) + 1
            picked.append((r, src))
        done, errors = [], []
        for r, src in picked:
            mp4, tile = d / "videos" / f"{r['id']}.mp4", d / "frames" / f"{r['id']}.jpg"
            try:
                secs, mv, mt = _video_and_tile(src, mp4, tile)
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001
                errors.append({"id": r["id"], "error": str(e)[:100]})
                continue
            if mv:
                ctx.register(mp4, "video", keep=ns.keep_media)
            if mt:
                ctx.register(tile, "frames", keep=ns.keep_media)
            row = {"id": r["id"], "source": "meta", "advertiser": r["advertiser"], "start": r["start"], "seconds": secs, "frames": str(tile), "video": str(mp4),
                   "url": r["library_url"]}
            rows.append(row)
            by_id[r["id"]] = row
            done.append({"id": r["id"], "advertiser": r["advertiser"], "frames": str(tile)})
        _write_tags(d, rows)
        emit({"prepared": len(done), "errors": errors, "sheet": str(_tag_sheet(d)), "to_tag": done, "taxonomy": TAXONOMY,
              "next": "Read each frames image, then: creative-tag set --dir DIR --id ID --hook ... --mechanic ... --visual ... --structure ... --cta ... --emotion ... --insight ..."})
        return
    if ns.action == "set":
        if not ns.id or ns.id not in by_id:
            raise SystemExit(f"unknown --id {ns.id}: run `creative-tag show --dir {d}` for ids")
        row = by_id[ns.id]
        warn = []
        for f in TAG_FIELDS:
            v = getattr(ns, f)
            if v is None:
                continue
            if f in TAXONOMY:
                vals = [x.strip() for x in v.split(",") if x.strip()]
                warn += [f"{f}: '{x}' not in taxonomy" for x in vals if x not in TAXONOMY[f]]
                v = ", ".join(vals)
            row[f] = v
        row["tagged_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        _write_tags(d, rows)
        emit({"tagged": ns.id, **({"warnings": warn} if warn else {}), "remaining": sum(1 for r in rows if not r.get("tagged_at"))})
        return
    if ns.action == "show":
        emit({"sheet": str(_tag_sheet(d)), "total": len(rows), "untagged": [{"id": r["id"], "advertiser": r["advertiser"], "frames": r["frames"]} for r in rows if not r.get("tagged_at")],
              "tagged": [{k: r.get(k) for k in ["id", "advertiser"] + TAG_FIELDS} for r in rows if r.get("tagged_at")]})
        return
    tagged = [r for r in rows if r.get("tagged_at")]
    counts = {}
    for f in TAXONOMY:
        c = {}
        for r in tagged:
            for x in [y.strip() for y in (r.get(f) or "").split(",") if y.strip()]:
                c[x] = c.get(x, 0) + 1
        counts[f] = sorted(({"value": k, "count": v} for k, v in c.items()), key=lambda x: -x["count"])
    per_adv = {}
    for r in tagged:
        per_adv.setdefault(r["advertiser"], []).append(r.get("hook") or "")
    res = {"tagged": len(tagged), "total": len(rows), "counts": counts, "hooks_by_advertiser": per_adv}
    (d / "tag_summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    emit({**res, "file": str(d / "tag_summary.json")})


# ------------------------------------------------------------------ report: one self-contained HTML from a market-scan folder
REPORT_CSS = """:root{--bg:#f6f7fb;--card:#fff;--ink:#14161f;--mut:#5d6475;--line:#e3e6ef;--c1:#3b6df0;--c2:#10a37f;--c3:#e8590c;--warn:#fff4e0;--warnb:#f2c46d}
@media (prefers-color-scheme:dark){:root{--bg:#0f1117;--card:#181b24;--ink:#eceef6;--mut:#9aa1b5;--line:#2a2f3d;--c1:#7b9dff;--c2:#34d3a6;--c3:#ff9a5a;--warn:#2a2415;--warnb:#6b5a2a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1200px;margin:0 auto;padding:20px 16px 64px}h1{font-size:clamp(24px,4vw,34px);margin:8px 0}h2{margin:40px 0 12px;font-size:22px;border-bottom:2px solid var(--line);padding-bottom:6px}
h3{font-size:17px;margin:20px 0 8px}.mut{color:var(--mut);font-size:13.5px}nav{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:8px 0;overflow-x:auto;white-space:nowrap}
nav a{margin-right:14px;font-size:14px;text-decoration:none;color:var(--c1)}.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;margin:14px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}.kpi b{display:block;font-size:24px}.kpi span{font-size:13px;color:var(--mut)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.callout{background:var(--warn);border:1px solid var(--warnb);border-radius:12px;padding:10px 14px;margin:12px 0}
.chip{display:inline-block;border-radius:999px;padding:1px 9px;margin:0 4px 4px 0;font-size:12.5px;background:color-mix(in srgb,var(--c1) 14%,transparent);color:var(--c1)}
.chip.g{background:color-mix(in srgb,var(--c2) 16%,transparent);color:var(--c2)}.chip.o{background:color-mix(in srgb,var(--c3) 16%,transparent);color:var(--c3)}
table{border-collapse:collapse;width:100%;font-size:13px;background:var(--card)}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{position:sticky;top:0;background:color-mix(in srgb,var(--ink) 6%,var(--card));cursor:pointer;white-space:nowrap}td.n{text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow:auto;max-height:560px;border:1px solid var(--line);border-radius:10px}.bar{display:grid;grid-template-columns:180px 1fr 50px;gap:8px;align-items:center;font-size:13px;margin:3px 0}
.bar i{display:block;height:14px;border-radius:4px;background:var(--c1)}.bar.o i{background:var(--c3)}.bar.g i{background:var(--c2)}
.cr{display:grid;grid-template-columns:minmax(200px,320px) 1fr;gap:14px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin:10px 0}
.cr img{width:100%;border-radius:8px}dl{display:grid;grid-template-columns:90px 1fr;gap:3px 10px;margin:6px 0;font-size:13.5px}dt{color:var(--mut)}dd{margin:0}
a{color:var(--c1)}@media(max-width:720px){.cr{grid-template-columns:1fr}.bar{grid-template-columns:110px 1fr 44px}}"""
REPORT_JS = """document.querySelectorAll('table').forEach(function(t){var hs=t.querySelectorAll('th');hs.forEach(function(h,i){h.addEventListener('click',function(){
var asc=h.dataset.s!=='a';hs.forEach(function(x){delete x.dataset.s});h.dataset.s=asc?'a':'d';var b=t.tBodies[0],r=[].slice.call(b.rows);
r.sort(function(x,y){var a=x.cells[i].dataset.v||x.cells[i].textContent,c=y.cells[i].dataset.v||y.cells[i].textContent,na=parseFloat(a),nc=parseFloat(c);
if(!isNaN(na)&&!isNaN(nc)&&/^-?[\\d.]+$/.test(String(a).trim())&&/^-?[\\d.]+$/.test(String(c).trim())){return asc?na-nc:nc-na}return asc?String(a).localeCompare(c):String(c).localeCompare(a)});
r.forEach(function(x){b.appendChild(x)})})})});"""


def build_report_html(folder, title, watch, max_frames):
    import base64 as _b64
    import html as _h
    esc = lambda x: _h.escape("" if x is None else str(x))  # noqa: E731
    folder = Path(folder)

    def load(name):
        for f in (folder / name, folder / "meta_ads" / name):
            if f.is_file():
                try:
                    return json.loads(f.read_text(encoding="utf-8"))
                except ValueError:
                    return None
        return None

    def bars(items, cls="", top=12):
        items = [i for i in items if i[1]][:top]
        mx = max((v for _, v in items), default=1) or 1
        return "".join(f"<div class='bar {cls}'><span>{esc(str(k)[:34])}</span><i style='width:{max(2, 100 * v / mx):.1f}%'></i><b class='mut'>{v}</b></div>" for k, v in items) or "<p class='mut'>—</p>"

    def table(cols, rows):
        h = "".join(f"<th>{esc(c)}</th>" for c in cols)
        body = ""
        for r in rows:
            body += "<tr>" + "".join((f"<td class='n' data-v='{v}'>{esc(v)}</td>" if isinstance(v, (int, float)) and not isinstance(v, bool) else f"<td>{v if str(v).startswith('<a ') else esc(v)}</td>") for v in r) + "</tr>"
        return f"<div class='scroll'><table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table></div>"

    def link(url, text):
        return f"<a href='{esc(url)}' target='_blank' rel='noopener'>{esc((text or url or '')[:70])}</a>" if url else esc(text)

    summ = load("summary.json") or {}
    kw, yt, tt, ma, ta, ga, mt = (load(n) for n in ("keywords.json", "youtube.json", "tiktok.json", "meta-ads.json", "tiktok-ads.json", "google-ads.json", "meta-trends.json"))
    play, ios = load("play.json"), load("appstore.json")
    query = summ.get("query") or (kw or {}).get("query") or folder.name
    title = title or f"Competitor ads & trends: {query}"
    nav, sec, kpis = [], [], []

    def add(id_, name, html_):
        nav.append(f"<a href='#{id_}'>{esc(name)}</a>")
        sec.append(f"<h2 id='{id_}'>{esc(name)}</h2>{html_}")

    if ma:
        kpis.append((ma.get("cards"), "Meta active video ads", f"{ma.get('advertisers')} advertisers"))
    if ta:
        kpis.append((ta.get("total_reported"), "TikTok library results", f"{ta.get('cards_loaded')} loaded"))
    if ga:
        kpis.append((sum(a.get("creatives_loaded", 0) for a in ga.get("advertisers", [])), "Google creatives loaded", f"{len(ga.get('advertisers', []))} advertisers"))
    if yt and yt.get("hottest_by_views_per_day"):
        kpis.append((yt["hottest_by_views_per_day"][0]["views_per_day"], "top YouTube views/day", yt["hottest_by_views_per_day"][0]["title"][:40]))
    if tt and tt.get("hottest"):
        kpis.append((tt["hottest"][0]["engagement_per_day"], "top TikTok engagement/day", (tt["hottest"][0]["caption"] or "")[:40]))
    if kw and kw.get("keyword_ideas"):
        kpis.append((len(kw["keyword_ideas"].get("expanded") or kw["keyword_ideas"]["google_autocomplete"]), "keyword ideas", "autocomplete"))

    parts = summ.get("parts") or {}
    if parts:
        add("status", "Sources", table(["Part", "Status", "Seconds"], [[k, "ok" if v.get("ok") else "failed: " + v.get("error", "")[:90], v.get("seconds", "")] for k, v in parts.items()]))

    if ma:
        rows = []
        f = folder / "meta_ads" / "ads.csv"
        if not f.is_file():
            f = folder / "ads.csv"
        weekly = {}
        if f.is_file():
            with f.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            for r in rows:
                if r.get("start"):
                    dd = _dt.date.fromisoformat(r["start"])
                    wk = (dd - _dt.timedelta(days=dd.weekday())).strftime("%d/%m")
                    weekly[wk] = weekly.get(wk, 0) + 1
        adv_rows = [[a["advertiser"], a["cards"], a.get("earliest") or ""] for a in ma.get("top_advertisers", [])]
        weeks = sorted(weekly.items(), key=lambda kv: (kv[0][3:], kv[0][:2]))
        html_ = (f"<div class='grid'><div class='card'><h3>Active video ads by advertiser</h3>{bars([(a[0], a[1]) for a in adv_rows])}</div>"
                 f"<div class='card'><h3>Ads by start week (still running)</h3>{bars(weeks[-12:], 'g', 12)}</div></div>"
                 + table(["Advertiser", "Ads", "Earliest start"], adv_rows))
        if rows:
            html_ += "<h3>All ads</h3>" + table(["Advertiser", "Start", "Days", "Seconds", "CTA", "Text", "Library"],
                                                 [[r["advertiser"], r["start"], int(r["days_running"]) if (r.get("days_running") or "").isdigit() else "", int(r["video_seconds"]) if (r.get("video_seconds") or "").isdigit() else "",
                                                   r.get("cta", ""), (r.get("primary_text") or "")[:90], link(r["library_url"], r["id"])] for r in rows[:400]])
        add("meta", "Meta Ad Library", html_)

    tags = []
    for cand in (folder / "creative_tags.csv", folder / "meta_ads" / "creative_tags.csv"):
        if cand.is_file():
            tags = _read_tags(cand.parent)
            break
    if tags:
        tagged = [t for t in tags if t.get("tagged_at")]
        cnt = {}
        for fld in ("hook", "mechanic", "visual", "cta"):
            c = {}
            for t in tagged:
                for x in [y.strip() for y in (t.get(fld) or "").split(",") if y.strip()]:
                    c[x] = c.get(x, 0) + 1
            cnt[fld] = sorted(c.items(), key=lambda kv: -kv[1])
        cards_html = ""
        shown = 0
        for t in tagged:
            img = ""
            fp = Path(t.get("frames") or "")
            if fp.is_file() and shown < max_frames:
                img = f"<img src='data:image/jpeg;base64,{_b64.b64encode(fp.read_bytes()).decode()}' alt='frames {esc(t['id'])}'>"
                shown += 1
            chips = "".join(f"<span class='chip'>{esc(x.strip())}</span>" for x in (t.get("hook") or "").split(",") if x.strip())
            chips += "".join(f"<span class='chip g'>{esc(x.strip())}</span>" for x in (t.get("mechanic") or "").split(",") if x.strip())
            chips += "".join(f"<span class='chip o'>{esc(x.strip())}</span>" for x in (t.get("visual") or "").split(",") if x.strip())
            cards_html += (f"<div class='cr'><div>{img}</div><div><b>{esc(t['advertiser'])}</b> <span class='mut'>· start {esc(t.get('start'))} · {esc(t.get('seconds'))}s · {link(t.get('url'), t['id'])}</span>"
                           f"<p>{chips}</p><dl><dt>Structure</dt><dd>{esc(t.get('structure'))}</dd><dt>CTA</dt><dd>{esc(t.get('cta'))}</dd><dt>Emotion</dt><dd>{esc(t.get('emotion'))}</dd>"
                           f"<dt>Insight</dt><dd>{esc(t.get('insight'))}</dd></dl></div></div>")
        add("creatives", f"Creative analysis ({len(tagged)} tagged)",
            "<p class='mut'>Tags written by Claude after reading 8 evenly spaced frames of each video.</p>"
            f"<div class='grid'><div class='card'><h3>Hook</h3>{bars(cnt['hook'])}</div><div class='card'><h3>Mechanic</h3>{bars(cnt['mechanic'], 'g')}</div>"
            f"<div class='card'><h3>Visual</h3>{bars(cnt['visual'], 'o')}</div><div class='card'><h3>CTA</h3>{bars(cnt['cta'])}</div></div>" + cards_html)

    if ta:
        html_ = table(["Advertiser", "Ads loaded"], [[a["advertiser"], a["cards"]] for a in ta.get("top_advertisers", [])])
        html_ += "<h3>Ads</h3>" + table(["Advertiser", "First shown", "Last shown", "Users seen", "Paid by", "Category", "Countries", "Detail"],
                                       [[a.get("advertiser"), a.get("first_shown"), a.get("last_shown"), a.get("users_seen_num") or "", a.get("paid_by") or "", a.get("category") or "",
                                         ", ".join(a.get("target_countries") or []), link(a.get("detail_url"), a.get("ad_id"))] for a in ta.get("ads", [])[:200]])
        top = (ta.get("creative_center_top_ads") or {}).get("cards") or []
        if top:
            html_ += "<h3>Creative Center Top Ads (public sample)</h3>" + table(["Objective", "Industry", "Likes", "CTR rank", "Budget"],
                                                                                [[c.get("objective"), c.get("industry"), c.get("likes_num") or "", c.get("ctr_rank"), c.get("budget")] for c in top])
        add("tiktok-ads", "TikTok Ad Library", f"<p class='mut'>Reported total {esc(ta.get('total_reported'))}; mostly ads shown in the EU/EEA.</p>" + html_)

    if ga:
        html_ = ""
        for a in ga.get("advertisers", []):
            html_ += (f"<h3>{esc(a.get('name'))} <span class='mut'>~{esc(a.get('approx_ads'))} ads · verified {esc(a.get('verified'))} · {esc(a.get('format_icons'))}</span></h3>"
                      + table(["Format", "Last shown", "Variants", "Countries", "Creative"], [[c.get("format") or c.get("format_icon"), c.get("last_shown"), c.get("variants") or "",
                                                                                               len(c.get("countries") or []), link(c.get("url"), "open")] for c in a.get("creatives", [])]))
        add("google-ads", "Google Ads Transparency", html_ or f"<p>{esc(ga.get('note', 'no advertiser matched'))}</p>")

    if watch:
        lastrun = watch["runs"][-1] if watch.get("runs") else {}
        since = lastrun.get("launched_since") or ""
        ads = list(watch.get("ads", {}).values())
        new = sorted([a for a in ads if a.get("start") and a["source"] in ("meta", "tiktok") and a["start"] >= since and a.get("status") == "active"],
                     key=lambda a: a["start"], reverse=True)
        gone = [a for a in ads if a.get("status") in ("not_seen", "likely_stopped")]
        html_ = (table(["Run", "Launched (by start date)", "First seen by watch", "Returned", "Not seen", "Sources"],
                       [[r["ts"], r.get("launched", ""), r["new"], r["returned"], r["not_seen"], ", ".join(f"{k}:{'ok' if v.get('ok') else 'failed'}" for k, v in r["sources"].items())]
                        for r in watch.get("runs", [])])
                 + f"<h3>Launched since {esc(since)} (ad start date)</h3>" + table(["Source", "Advertiser", "Start", "Text / format", "Link"], [[a["source"], a.get("advertiser"), a.get("start") or "", a.get("text") or a.get("format") or "", link(a.get("url"), "open")] for a in new[:200]])
                 + "<h3>Not seen any more</h3>" + table(["Source", "Advertiser", "Status", "First observed", "Last observed", "Times seen"], [[a["source"], a.get("advertiser"), a.get("status"), a.get("first_observed"), a.get("last_observed"), a.get("times_seen")] for a in gone[:200]]))
        add("watch", "Ad history (ads-watch)", "<p class='mut'>'Not seen' can mean stopped or simply not loaded by the scan; two misses in a row are marked likely_stopped.</p>" + html_)

    if tt:
        html_ = table(["Caption", "Author", "Likes", "Comments", "Shares", "Saves", "Age (d)", "Engagement/day", "Video"],
                      [[(v.get("caption") or "")[:80], v.get("author") or "", v.get("likes") or "", v.get("comments") or "", v.get("shares") or "", v.get("saves") or "", v.get("age_days") or "",
                        v.get("engagement_per_day") or "", link(v.get("url"), "open")] for v in tt.get("videos", [])])
        html_ += "<h3>Top hashtags</h3><p>" + "".join(f"<span class='chip'>{esc(h['tag'])} {h['count']}</span>" for h in tt.get("top_hashtags", [])) + "</p>"
        cc = (tt.get("creative_center_trending_hashtags") or {}).get("rows") or []
        if cc:
            html_ += "<h3>Creative Center trending hashtags</h3>" + table(["#", "Hashtag", "Industry", "Posts", "Views"], [[r["rank"], r["hashtag"], r.get("industry") or "", r.get("posts_num") or "", r.get("views_num") or ""] for r in cc])
        add("tiktok", "TikTok organic heat", html_)

    if yt:
        add("youtube", "YouTube heat", table(["Title", "Views", "Age (d)", "Views/day", "Video"],
                                             [[r["title"][:90], r.get("views") or "", r.get("age_days") if r.get("age_days") is not None else "", r.get("views_per_day") or "", link(r.get("url"), "open")] for r in yt.get("results", [])]))

    if kw:
        ki = kw.get("keyword_ideas") or {}
        html_ = ("<div class='grid'><div class='card'><h3>Google autocomplete</h3><p>" + "".join(f"<span class='chip'>{esc(x)}</span>" for x in ki.get("google_autocomplete", [])) + "</p></div>"
                 + "<div class='card'><h3>YouTube autocomplete</h3><p>" + "".join(f"<span class='chip g'>{esc(x)}</span>" for x in ki.get("youtube_autocomplete", [])) + "</p></div>"
                 + f"<div class='card'><h3>Top modifiers</h3>{bars([(m['word'], m['count']) for m in ki.get('top_modifiers', [])], 'o', 15)}</div></div>")
        if ki.get("expanded"):
            html_ += "<h3>Long-tail ideas</h3><p>" + "".join(f"<span class='chip'>{esc(x)}</span>" for x in ki["expanded"][:80]) + "</p>"
        tn = kw.get("trending_now")
        if isinstance(tn, list) and tn:
            html_ += f"<h3>Trending searches today ({esc(kw.get('geo'))})</h3>" + table(["Term", "Traffic", "News"], [[t["term"], t.get("traffic_num") or "", "; ".join(x for x in t.get("news") or [] if x)[:120]] for t in tn])
        add("keywords", "Keywords & search trends", html_)

    if mt:
        add("facebook", "Facebook organic", f"<p>Posts found: {esc(mt.get('posts_found'))}</p><p>" + "".join(f"<span class='chip'>{esc(h['tag'])} {h['count']}</span>" for h in mt.get("top_hashtags", [])) + "</p>"
            + "<div class='scroll'><table><thead><tr><th>Caption</th></tr></thead><tbody>" + "".join(f"<tr><td>{esc(c[:220])}</td></tr>" for c in mt.get("captions", [])[:40]) + "</tbody></table></div>")

    if play or ios:
        rows = []
        for a in (play or {}).get("apps", []):
            rows.append(["Google Play", a.get("title"), a.get("developer"), a.get("rating"), a.get("reviews"), a.get("downloads"), link(a.get("url"), "open")])
        for a in (ios or {}).get("apps", []):
            rows.append(["App Store", a.get("title"), a.get("developer"), a.get("rating"), a.get("rating_count"), "n/a", link(a.get("url"), "open")])
        add("apps", "Store listings", table(["Store", "App", "Developer", "Rating", "Ratings / reviews", "Downloads", "Link"], rows))

    add("method", "Method and limits", "<ul><li>Collected through the user's Chrome profile on " + _dt.date.today().isoformat() + " with the cua-chrome skill.</li>"
        "<li>No ad library publishes spend: advertisers are compared by active ads, start dates (longevity) and recent bursts; TikTok adds unique users seen.</li>"
        "<li>Heat: YouTube views per day; TikTok engagement (likes+comments+shares+saves) per day. TikTok search results are personalised to the logged-in account.</li>"
        "<li>Gaps: Google Trends Explore numbers are not available to automated sessions; Facebook search hides view counts; TikTok Ad Library mostly covers the EU/EEA; "
        "Google Ads Transparency is searched by advertiser, not by ad text.</li></ul>")

    kpi_html = "".join(f"<div class='kpi'><b>{esc(v)}</b><span>{esc(l)}</span><div class='mut'>{esc(sub)}</div></div>" for v, l, sub in kpis if v is not None)
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{REPORT_CSS}</style></head><body><div class='wrap'><h1>{esc(title)}</h1>"
            f"<p class='mut'>Generated {_dt.datetime.now():%Y-%m-%d %H:%M} from {esc(folder)}</p><nav>{''.join(nav)}</nav><div class='kpis'>{kpi_html}</div>"
            + "".join(sec) + f"</div><script>{REPORT_JS}</script></body></html>")


def s_report(sp):
    sp.add_argument("--from", dest="src", required=True, help="a market-scan folder (or any folder with the playbooks' JSON files)")
    sp.add_argument("--watch", help="include the history of this ads-watch name")
    sp.add_argument("--title")
    sp.add_argument("--out", help="HTML file (default: <folder>/report.html)")
    sp.add_argument("--max-frames", type=int, default=30, help="frame tiles to embed (each ~100 KB)")


@command("report", "build one self-contained HTML report from a market-scan folder (+ creative tags, frames, ads-watch history, store listings)", s_report)
def c_report(ctx, ns):
    folder = Path(ns.src)
    if not folder.is_dir():
        raise SystemExit(f"not a folder: {folder}")
    watch = None
    if ns.watch:
        hf = WATCH_DIR / _safe_name(ns.watch) / "history.json"
        if not hf.is_file():
            raise SystemExit(f"no ads-watch history named '{ns.watch}' (see `ads-watch list`)")
        watch = json.loads(hf.read_text(encoding="utf-8"))
    html_doc = build_report_html(folder, ns.title, watch, ns.max_frames)
    out = Path(ns.out) if ns.out else folder / "report.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_doc, encoding="utf-8")
    emit({"report": str(out), "kb": round(len(html_doc.encode()) / 1024), "sections": re.findall(r"<h2 id='([\w-]+)'", html_doc)})


def s_batch(sp):
    sp.add_argument("file", nargs="?", default="-", help="file with one command per line (shell quoting), '-' = stdin; # comments allowed")


@command("batch", "run many commands in ONE runtime (faster, shares the tab); lines are normal cua.py commands without the program name", s_batch)
def c_batch(ctx, ns):
    text = sys.stdin.read() if ns.file == "-" else Path(ns.file).read_text(encoding="utf-8")
    parser = build_parser()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        print(f"### {line}")
        try:
            sub = parser.parse_args(shlex.split(line))
        except SystemExit as e:
            print(f"(bad command: {e})")
            continue
        if sub.cmd == "batch":
            print("(nested batch ignored)")
            continue
        if getattr(sub, "tab_sub", None):
            sub.tab = sub.tab_sub
        sb = getattr(sub, "browser_sub", None) or sub.browser
        if sb:
            ctx.browser = sb
        if sub.tab:
            ctx.tab_override = sub.tab
        try:
            COMMANDS[sub.cmd][0](ctx, sub)
        except SystemExit as e:
            print(f"ERROR: {e}")
        except (RuntimeError, TimeoutError, ConnectionError, OSError, ValueError) as e:
            print(f"ERROR: {e}")


# ------------------------------------------------------------------ main
def build_parser():
    ap = argparse.ArgumentParser(prog="cua.py", description="Drive the user's real Chrome (ChatGPT extension runtime). See references/commands.md.")
    ap.add_argument("--browser", help="browser id/name for this call (see `browsers`); default from state/CUA_BROWSER/chrome")
    ap.add_argument("--tab", help="tab id for this call (default: current tab)")
    ap.add_argument("--task", help="state namespace (current tab, opened tabs) so parallel jobs do not collide; default from CUA_TASK or 'default'")
    ap.add_argument("--no-daemon", action="store_true", help="run in a one-shot runtime (tabs are closed when the process exits)")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")
    for name, (fn, help_, setup) in COMMANDS.items():
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.add_argument("--browser", default=argparse.SUPPRESS, dest="browser_sub", help=argparse.SUPPRESS)
        sp.add_argument("--tab", default=argparse.SUPPRESS, dest="tab_sub", help=argparse.SUPPRESS)
        sp.add_argument("--task", default=argparse.SUPPRESS, dest="task_sub", help=argparse.SUPPRESS)
        if setup:
            setup(sp)
    return ap


def main(argv=None):
    parser = build_parser()
    ns = parser.parse_args(argv)
    if getattr(ns, "browser_sub", None):
        ns.browser = ns.browser_sub
    if getattr(ns, "tab_sub", None):
        ns.tab = ns.tab_sub
    if getattr(ns, "task_sub", None):
        ns.task = ns.task_sub
    ctx = Ctx(ns)
    try:  # housekeeping: old scratch files and a long log never pile up
        gc_scratch()
        prune_log()
    except Exception:  # noqa: BLE001
        pass
    ok = False
    try:
        COMMANDS[ns.cmd][0](ctx, ns)
        ok = True
    except (RuntimeError, TimeoutError, ConnectionError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if ns.cmd not in ("_daemon",):
            log_command(ctx.task, ns.cmd, ok)
        if ctx.rt:
            ctx.rt.close()


if __name__ == "__main__":
    main()
