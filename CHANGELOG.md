# Changelog

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
