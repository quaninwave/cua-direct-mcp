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
- `scripts/cua_core.py` - bản sao nguyên văn `scripts/cua.py` của skill `cua-chrome` (chỉ dùng
  thư viện chuẩn). Các tool mới (look, links, playbook nghiên cứu, cleanup...) gọi thẳng lệnh của
  nó qua một adapter, nên MCP và CLI có cùng tính năng. Muốn cập nhật thì chép đè file từ skill.
- `scripts/extract_meta_cards.js` - script đọc thẻ quảng cáo Meta Ad Library (dùng bởi `meta_ads`).
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
| Browser và tab | `browser_list` (có giá trị `use` ổn định), `browser_select` (nhận `ext:<prefix>`), `tab_list`, `tab_select`, `tab_open`, `tab_close`, `tab_navigate` (goto/back/forward/reload) |
| Tổng quan trang | `look` (1 lần gọi: tiêu đề, heading, nội dung chính, input/nút kèm `target` dùng ngay, link, gợi ý login/captcha) |
| Trích xuất | `links`, `table`, `items` (thẻ/dòng lặp lại, có phân trang), `scroll_load` (cuộn vô hạn), `fill_form` (điền nhiều ô, không bao giờ submit), `snapshot` (save/diff/list), `page_assets_save` |
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
| Dọn dẹp | `cleanup` (đóng tab server đã mở + xóa file tạm), `artifacts` (list/keep/drop/gc), `note` (ghi chú theo website) |
| Sắp xếp tab | `tabs_organize` (dedupe / group-by-domain; mặc định chỉ xem kế hoạch) |
| Nghiên cứu (playbook) | `meta_ads`, `tiktok_ads`, `google_ads`, `tiktok_trends`, `yt_search`, `google_trends`, `meta_trends`, `play_profile`, `appstore_profile`, `market_scan`, `ads_watch`, `creative_tag`, `report` |
| Thô | `js` (như proxy gốc) |

Tool thao tác trên tab nhận `tab_id`; bỏ trống thì dùng tab vừa `tab_open`/`tab_select`.
`target` (locator): `selector` | `role`+`name` | `text` | `label` | `placeholder` | `test_id`,
kèm `frame`, `nth`, `has_text`, `exact`, `regex`. Thêm `observe=true` để nhận lại cây AX mới.
Khi locator không tìm thấy phần tử, lỗi trả về kèm `hint: closest elements -> ...` liệt kê các phần
tử gần giống nhất dưới dạng `target={...}` để dùng lại ngay. Tool chỉ đọc (`tab_list`, `page_read`,
`page_find`, `page_ax_tree`, `page_screenshot`...) tự thử lại khi gặp lỗi tạm thời của trình duyệt
("Debugger detached", "Execution context was destroyed"...).

### Quy trình gợi ý

    tab_open(url) -> look -> page_click / page_type / fill_form -> page_read hoặc page_screenshot để kiểm tra -> cleanup

- Bắt đầu bằng `look` thay vì `page_ax_tree` + `page_read` + `page_find`: mỗi input/nút đã có sẵn
  `target`. `look` tự lấy cả `document.body` khi `<main>` gần như trống.
- Ghi chú theo website (`note add example.com "..."`) tự hiện ra dạng `site_notes` khi `tab_open`,
  `tab_navigate`, `tab_select`, `look` vào site đó.
- Chọn profile Chrome bằng giá trị `use` trong `browser_list` (vd `ext:bb48927d`): id dạng số đổi
  sau mỗi lần runtime khởi động, còn `ext:` thì không. `browser_select` tự đổi id số sang `ext:`.

### Playbook nghiên cứu thị trường / đối thủ

Mỗi playbook tự mở tab riêng, đọc, đóng tab rồi trả về số liệu đã phân tích:

| Cần | Tool | Kết quả |
|---|---|---|
| Tất cả cho 1 từ khóa | `market_scan(query, out_dir, advertiser=...)` | mỗi nguồn 1 file JSON + `summary.json` + `summary.md` (chạy vài phút) |
| Quảng cáo Meta | `meta_ads(query)` | quảng cáo đang chạy theo nhà quảng cáo, ngày bắt đầu, nội dung, CTA; `download`/`frames` để lấy video + ảnh 8 khung hình (cần ffmpeg) |
| Quảng cáo TikTok | `tiktok_ads(query, top_ads=...)` | Ad Library: first/last shown, số người đã xem, quốc gia, người trả tiền, landing page; Top Ads: likes/CTR/budget |
| Quảng cáo Google | `google_ads(query=tên công ty hoặc domain)` | nhà quảng cáo (~số quảng cáo, đã xác minh), creative, last shown, định dạng, biến thể, quốc gia |
| Độ hot trên TikTok | `tiktok_trends(query)` | video với likes/comments/shares/saves, engagement/ngày, hashtag nổi bật, hashtag thịnh hành Creative Center |
| Độ hot trên YouTube | `yt_search(query, when="month", sort="views")` | lượt xem, tuổi video, lượt xem/ngày |
| Từ khóa / Google Trends | `google_trends(query, expand=...)` | autocomplete Google + YouTube, long tail a-z, từ bổ nghĩa hay gặp, tìm kiếm thịnh hành trong ngày theo quốc gia (không cần trình duyệt) |
| Facebook tự nhiên | `meta_trends(query)` | bài video, caption, hashtag |
| Thông tin cửa hàng ứng dụng | `play_profile(targets)`, `appstore_profile(targets)` | Android: rating, reviews, downloads; iOS (API Apple, không cần trình duyệt): rating, số lượt rating, nhà phát triển, thể loại, ngày cập nhật |
| Theo dõi đối thủ theo thời gian | `ads_watch("run", name, query=..., advertiser=...)` | lịch sử gộp: lần đầu/cuối thấy, số lần thấy, quảng cáo mới ra theo ngày bắt đầu, không còn thấy / có thể đã dừng |
| Phân tích creative bằng AI | `creative_tag("prepare", dir=...)` -> xem từng ảnh khung hình -> `creative_tag("set", ...)` -> `creative_tag("summary", ...)` | `creative_tags.csv`: hook, mechanic, visual, structure, CTA, emotion, insight |
| Báo cáo chia sẻ được | `report(from_dir, watch=...)` | 1 file HTML tự chứa (biểu đồ, bảng sắp xếp được, ảnh khung hình nhúng sẵn) |

Thư viện quảng cáo không công bố chi tiêu: so sánh bằng số quảng cáo đang chạy, ngày bắt đầu và đợt
tung mới. Google Ads Transparency tìm theo tên nhà quảng cáo/domain, không theo nội dung quảng cáo;
TikTok Ad Library chủ yếu có quảng cáo hiển thị ở EU/EEA; Google Trends Explore thường không trả số
cho phiên tự động. Các playbook dài (nhất là `market_scan`) có thể vượt thời gian chờ mặc định của
MCP client: tăng timeout của client hoặc bỏ bớt phần bằng `skip`.

### Dọn dẹp tab và file (bắt buộc)

- Mọi tab do `tab_open` hoặc playbook mở đều được ghi lại. Gọi `cleanup` trước khi trả lời xong:
  nó đóng các tab đó và xóa file tạm (video, ảnh khung hình, file tải với `scratch=true`, thư mục
  output mặc định của `meta_ads`). Tab đã `tab_mark` được giữ lại.
- Tab chỉ sống cùng runtime: khi server khởi động lại, danh sách tab cũ được xóa vì runtime cũ đã
  đóng chúng. File tạm cũ hơn 24 giờ (`CUA_SCRATCH_HOURS`) và log cũ hơn 7 ngày (`CUA_LOG_DAYS`) tự
  được dọn khi server khởi động.
- `artifacts list` xem file đã tạo; `artifacts keep PATH` giữ lại file người dùng muốn.

### Chốt an toàn (`confirmed`)

Các thao tác sau bị từ chối với `needs confirmation: ...` trừ khi truyền `confirmed=true`, và chỉ
được truyền sau khi người dùng đồng ý rõ ràng với đúng thao tác đó:

- `page_click` vào nút trông như gửi/submit/đăng/thanh toán/mua/đặt hàng/xác nhận/xóa/đăng ký
  (kiểm tra chữ trên nút, `aria-label`, `title`, hoặc `type=submit`);
- `file_upload` (gửi nội dung file lên website), `browser_history` (dữ liệu nhạy cảm);
- `fill_form` vào ô mật khẩu; `tab_close` tab mà server không mở (tab của người dùng);
- `management_call` với method remove/delete/clear; `tabs_organize` với `apply=true`;
  `ads_watch("forget")`; `bookmark_remove` (như trước).

Các tham số `confirmed` (và `scratch` của `file_download`) là tham số mới, tùy chọn: tên tool và
tham số cũ giữ nguyên.

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
- `CUA_DIRECT_BROWSER` - browser mặc định (`chrome`, id, hoặc `ext:<prefix>`).
- `CUA_DIRECT_STATE_DIR` - thư mục trạng thái (tab đã mở, file tạm, ghi chú site, snapshot,
  lịch sử `ads_watch`, log). Mặc định `~/.cua-direct-mcp`, tách biệt với CLI `cua-chrome`
  (`~/.cua-chrome`). Trỏ về `~/.cua-chrome` nếu muốn dùng chung ghi chú và lịch sử với CLI; danh
  sách tab vẫn tách riêng nhờ namespace `CUA_DIRECT_TASK`.
- `CUA_DIRECT_TASK` - namespace trạng thái (mặc định `mcp`, file `state-mcp.json`).
- `CUA_DIRECT_NO_GUARD=1` - tắt các chốt an toàn `confirmed` (chỉ dùng khi test tự động).
- `CUA_SCRATCH_HOURS` (mặc định 24), `CUA_LOG_DAYS` (mặc định 7) - tuổi tối đa của file tạm và log.

## Lưu ý

- Cách này cố ý bỏ qua lớp cấp quyền của Codex, nên chỉ chạy đúng trên máy này
  với Chrome/extension của bạn, và không còn Codex gác cổng.
- Chrome vẫn gán nhãn mặc định "ChatGPT" cho mỗi group mới Claude mở; chạy
  `relabel_group.py` sau khi mở tab để đổi thành "Claude".
- Ép `visible:true` khi tạo tab sẽ lỗi `Capability is not available: visibility`;
  dùng `cua.createBrowserTab('chrome', url)` không kèm cờ đó.
- Cần Chrome đang mở với extension ChatGPT đã bật trong profile tương ứng.
- Các tool lấy từ `cua_core.py` chạy trong thread riêng và lần lượt từng cái một (một playbook dài
  sẽ khiến tool core khác chờ); tool gốc vẫn chạy xen kẽ được vì mọi lệnh JS đều đi qua cùng một
  phiên proxy, mỗi lần một lệnh. Nếu client hủy một playbook đang chạy, thread đó vẫn chạy nốt.
- `meta_ads` / `creative_tag` tải video và cắt khung hình cần `ffmpeg`/`ffprobe` trong PATH (hoặc
  cài bằng `winget install Gyan.FFmpeg`).
