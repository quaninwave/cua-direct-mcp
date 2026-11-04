# Changelog

## 0.3.0
- Feature parity with the `cua-chrome` skill CLI. `scripts/cua_core.py` is a verbatim copy of the
  skill's `cua.py` (plus `scripts/extract_meta_cards.js`); new tools call its commands through an
  adapter: the sync command runs in a worker thread, its `call_js` hops back to the server loop
  (`run_coroutine_threadsafe`) and uses the same proxy session and lock as the native tools. Core
  commands run one at a time; the skill's background daemon is never started.
- 25 new tools (85 in total): `look`, `links`, `table`, `items`, `scroll_load`, `fill_form`,
  `snapshot`, `tabs_organize`, `page_assets_save`, `note`, `cleanup`, `artifacts`, and the research
  playbooks `meta_ads`, `tiktok_ads`, `google_ads`, `tiktok_trends`, `yt_search`, `google_trends`,
  `meta_trends`, `play_profile`, `appstore_profile`, `market_scan`, `ads_watch`, `creative_tag`,
  `report`.
- Stable browser selection: `ext:<extension instance prefix>` resolved in the JS prelude;
  `browser_list` shows a `use` value; `browser_select` converts numeric ids to the `ext:` form.
- Tab and file hygiene: tabs opened by `tab_open` and the playbooks are tracked; `cleanup` closes
  them and deletes scratch files (videos, frame tiles, scratch downloads, default playbook output
  folders). `tab_mark` removes a tab from that list. The list is reset on server start (the old
  runtime's tabs are gone); scratch older than 24 h and old log lines are pruned at start.
- Safety gates (`needs confirmation` unless `confirmed=true`): risky-looking clicks in `page_click`
  (send/submit/post/pay/buy/delete/subscribe..., or a submit button), `file_upload`,
  `browser_history`, password fields in `fill_form`, `tab_close` on tabs this server did not open,
  destructive `management_call` methods, `tabs_organize` apply, `ads_watch` forget.
  `CUA_DIRECT_NO_GUARD=1` disables them for automated tests.
- Read-only tools retry transient browser errors; a failed locator returns the closest elements as
  ready-to-use `target` dicts; results that describe a page include saved `site_notes`.
- Existing tool names and parameters are unchanged. New optional parameters: `confirmed` on
  `page_click`, `file_upload`, `browser_history`, `management_call`, `tab_close`; `scratch` on
  `file_download`.
- State lives in `~/.cua-direct-mcp` (`CUA_DIRECT_STATE_DIR`, namespace `CUA_DIRECT_TASK`=mcp),
  separate from the CLI's `~/.cua-chrome`.
- `tools_test.py` passes `confirmed=True` where the new gates apply and covers the new page tools.
- Fixes from an end-to-end run against a real Chrome:
  - `page_click` with a locator no longer wedges the tab when the click opens a synchronous
    `alert`/`confirm`/`prompt` (the Playwright click blocked on `Input.dispatchMouseEvent`, after which
    every call timed out on `Emulation.setFocusEmulationEnabled` and `prompt_text` was lost). Plain
    clicks on a visible, unobscured, enabled element go through native input at its centre; iframes,
    `force` and `modifiers` keep the Playwright click. A click that opens a dialog returns
    `{ok, dialog}` instead of an error. The runtime ignores `{ trial: true }` (it clicks), so the
    actionability checks are done in the page.
  - `page_assets_bundle` and `content_export_youtube_transcript` fall back when the runtime sandbox
    refuses the export (EPERM): assets are saved over HTTP into a scratch folder; the transcript is read
    from the page's transcript panel into `~/.cua-direct-mcp/exports`.
  - `bookmark_remove` without `confirmed` returns a `needs confirmation` result (it raised a generic
    error). `tab_sort` returns only the sorted tabs, not the whole window. `page_assets_list` returns
    at most 20 inline SVG entries unless `include_inline_svg`. `items` warns about fields that matched
    nothing in any row. `tiktok_ads` drops `data:` placeholder thumbnails. `js` documents that it runs
    statements (no top-level `return`) and prints only what `nodeRepl.write` writes.
  - `scripts/cua_core.py` is no longer byte-identical to the skill's `cua.py`: the `items` warning and
    the `tiktok-ads` thumbnail filter were changed there.
  - `tools_test.py`: new `sync_dialogs` step; the download step uses `scratch` and no longer depends
    on the file name; the gate step covers `bookmark_remove`; the items step covers the warning.

## 0.2.1
- `browser_select` accepts any browser id from `browser_list` (needed when several Chrome profiles are connected, each with its own id).
- Documented `CUA_DIRECT_MAX_OUTPUT_CHARS` for scripts that need large tool output.

## 0.2.0
- Added `scripts/cua_direct_tools.py`: MCP server wrapping the proxy with 59 dedicated browser
  tools (tabs, page reading, actions, screenshots, files, clipboard/dialogs, tab groups,
  bookmarks/windows, history/logs, viewport, page assets, content export, CDP, session
  lifecycle) plus the raw `js` tool.
- Added `scripts/tools_test.py` (end-to-end test), `scripts/build_package.py`, `install.ps1`.
- `register_claude.py`: registers the tools server by default; `--raw` keeps the bare proxy.
- `cua_direct_proxy.py` unchanged.
- Known limits through the proxy: raw CDP needs Browser Use approval, `page_assets_bundle`
  hits the runtime's fs sandbox (EPERM), mobile viewport floors at ~416px.

## 0.1.0
- Proxy only (`cua_direct_proxy.py`, single `js` tool), smoke test, tab group relabel helper.
