# cua-direct MCP

Cho phép Claude điều khiển Chrome trực tiếp qua extension ChatGPT, không cần mở
chat Codex và không cần chữ ký cấp quyền của Codex app. Claude gọi tool
`mcp__cua_direct__js` và dùng trọn API trình duyệt (`cua.getState`,
`createBrowserTab`, `getTab`, `listTabs`, `tab.goto/click/typeText`, capability
`management` để sắp xếp tab/nhóm tab).

## Hoạt động thế nào

Runtime `cua_repl` (trong plugin Codex `unified-computer-use`) vốn đã điều khiển
được Chrome qua extension ChatGPT. Bình thường có hai chốt chặn người gọi bên ngoài:

1. Loader `--import require-browser-access.mjs` bắt biến
   `BROWSER_USE_ACCESS_EXECUTOR_ID`, kích hoạt bước kiểm tra chữ ký theo từng
   lượt. Chỉ Codex app giữ private key nên người ngoài bị từ chối.
2. Bước đo trình duyệt yêu cầu mỗi `tools/call` mang metadata
   `x-codex-turn-metadata` gồm `session_id` và `turn_id`.

Proxy `scripts/cua_direct_proxy.py` khởi động `cua_repl` mà bỏ loader (chốt #1 không
bật) và tự chèn `x-codex-turn-metadata` vào mỗi `tools/call` (thỏa chốt #2).
Metadata chỉ là hai ID ngẫu nhiên, không phải chữ ký. Mọi JSON-RPC khác được forward
nguyên vẹn.

    Claude -> cua_direct_proxy.py -> cua_repl (không loader)
                 (chèn session/turn)        |
                                            v
              extension ChatGPT -> Chrome (profile inwave.vn)

## Cấu trúc

- `scripts/cua_direct_proxy.py` - proxy MCP stdio (chỉ dùng thư viện chuẩn). Không bị sửa.
- `scripts/cua_direct_tools.py` - MCP server mới: chạy proxy làm client, biến các tool
  chuyên biệt (bảng bên dưới) thành JS gọi qua tool `js` của proxy. Vẫn giữ tool `js` thô.
- `scripts/tools_test.py` - test end-to-end cho các tool (trang test cục bộ + 1 tab dùng một lần).
- `scripts/smoke_test.py` - test end-to-end: mở tab, điều hướng, liệt kê tab.
- `scripts/relabel_group.py` - đổi nhãn tab group "ChatGPT" -> "Claude".
- `scripts/register_claude.py` - thêm/xóa `cua_direct` trong `~/.claude.json`.
- `scripts/build_package.py` - đóng gói bản phát hành (file zip).
- `install.ps1` - tạo `.venv`, cài thư viện và (tùy chọn) đăng ký vào Claude Code.
- `mcp-config.example.json` - config mẫu cho `claude --mcp-config`
  (thay `<INSTALL_DIR>` bằng thư mục cài đặt).
- `requirements.txt`, `VERSION`, `CHANGELOG.md`.

## Cài đặt

Yêu cầu: Windows, Python 3, Chrome đang mở với extension ChatGPT đã bật trong profile
tương ứng, và plugin Codex `unified-computer-use` đã được cài trên máy.

    .\install.ps1              # chỉ tạo .venv và cài thư viện
    .\install.ps1 -Register    # cài và đăng ký luôn vào ~/.claude.json (có giữ bản backup)
    .\install.ps1 -Register -Raw   # đăng ký proxy thô (chỉ tool js)

## Dùng nhanh

Chạy thử end-to-end:

    .\.venv\Scripts\python.exe scripts\smoke_test.py

Test các tool chuyên biệt (mở/đóng 1 tab thử riêng, không đụng vào tab của bạn):

    $env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe scripts\tools_test.py
    # chạy từng phần: --only open,shot,close

Đăng ký vào Claude Code:

    .\.venv\Scripts\python.exe scripts\register_claude.py          # tool chuyên biệt + js
    .\.venv\Scripts\python.exe scripts\register_claude.py --raw    # chỉ proxy (1 tool js)
    # gỡ: thêm --remove

Đăng ký xong cần khởi động lại Claude Code. Tool hiện ra dạng `mcp__cua_direct__<tên>`.

## Các tool (theo bảng chức năng)

| Nhóm | Tool |
|---|---|
| Browser và tab | `browser_list`, `browser_select`, `tab_list`, `tab_select`, `tab_open`, `tab_close`, `tab_navigate` (goto/back/forward/reload) |
| Đọc trang | `page_ax_tree`, `page_dom_snapshot`, `page_read`, `page_find`, `page_evaluate` (iframe: truyền `frame` trong `target`) |
| Thao tác | `page_click` (+double), `page_type`, `page_press_key`, `page_check`, `page_select_option`, `page_scroll`, `page_drag`, `page_wait` |
| Ảnh chụp | `page_screenshot` (viewport / full_page / clip / target, `save_path`) |
| Tệp | `file_upload`, `file_download` (click hoặc media, `copy_to`) |
| Clipboard và dialog | `clipboard_read`, `clipboard_write`, `dialog_get`, `dialog_handle` |
| Nhóm tab | `tab_group_list/create/update/move`, `tab_ungroup`, `tab_pin`, `tab_move`, `tab_sort`, `tabs_query` |
| Bookmark và cửa sổ | `bookmark_search/list/create/move/update/remove`, `window_list/create/update`, `management_call` (API còn lại) |
| Lịch sử và log | `browser_history`, `page_console_logs`, `browser_audit_trail` |
| Responsive | `viewport_set` (width/height hoặc preset), `viewport_reset` |
| Asset trang | `page_assets_list`, `page_assets_bundle` |
| Xuất nội dung | `content_export_gsuite`, `content_export_youtube_transcript` |
| CDP | `cdp_send`, `cdp_events` |
| Vòng đời phiên | `session_name`, `tab_mark` (deliverable/handoff) |
| Thô | `js` (như proxy gốc) |

Tool thao tác trên tab nhận `tab_id`; bỏ trống thì dùng tab vừa `tab_open`/`tab_select`.
`target` (locator): `selector` | `role`+`name` | `text` | `label` | `placeholder` | `test_id`,
kèm `frame`, `nth`, `has_text`, `exact`, `regex`. Thêm `observe=true` để nhận lại cây AX mới.

## Giới hạn đã gặp khi chạy qua proxy (không phải lỗi của tool)

- `cdp_send` / `cdp_events`: Browser Use đòi bước phê duyệt CDP (approval gate); không có
  Codex app thì trả `CDP approval gate did not grant access`. Tool đi đến được cổng này nhưng
  không (và không nên) vượt qua.
- `page_assets_bundle`: `page_assets_list` chạy được, nhưng bước bundle bị
  `EPERM ... mkdir ...\Temp\browser-use` do sandbox file-system của runtime (Node thường tạo
  được thư mục đó, nên thiếu grant của Codex). Cần xử lý ở phía proxy/runtime.
- `viewport_set`: áp dụng được, nhưng Chrome không cho cửa sổ hẹp hơn ~416px nên preset mobile
  (375) thực tế ra ~416.
- `page_evaluate` chạy trong môi trường DOM chỉ đọc, cô lập: không thấy biến `window.*` của
  trang và một số thuộc tính (vd `el.files`).

Đổi nhãn tab group hiện trên thanh tab Chrome:

    .\.venv\Scripts\python.exe scripts\relabel_group.py Claude

## Biến môi trường

- `CUA_DIRECT_SPEC_PATH` - ép đường dẫn `.mcp.json` của plugin. Bỏ trống thì tự
  dò bản mới nhất trong
  `~/.codex/plugins/cache/openai-bundled/unified-computer-use`.
- `CUA_DIRECT_SESSION_ID` - cố định `session_id` chèn vào metadata (mặc định
  sinh ngẫu nhiên mỗi lần khởi động).
- `CUA_DIRECT_MAX_OUTPUT_CHARS` - giới hạn số ký tự output của tool (mặc định 60000);
  tăng lên nếu script cần output lớn.

## Lưu ý

- Cách này cố ý bỏ qua lớp cấp quyền của Codex, nên chỉ chạy đúng trên máy này
  với Chrome/extension của bạn, và không còn Codex gác cổng.
- Chrome vẫn gán nhãn mặc định "ChatGPT" cho mỗi group mới Claude mở; chạy
  `relabel_group.py` sau khi mở tab để đổi thành "Claude".
- Ép `visible:true` khi tạo tab sẽ lỗi `Capability is not available: visibility`;
  dùng `cua.createBrowserTab('chrome', url)` không kèm cờ đó.
- Cần Chrome đang mở với extension ChatGPT đã bật trong profile tương ứng.
