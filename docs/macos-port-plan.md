<!-- Working plan for the macOS port (mac branch). Not part of architecture.md: it describes intended work, not current behavior. Delete when the port lands. -->

# Plan: a native macOS desktop body for Lumen

**Summary.** No files were changed. The plan rests on four decisions:

1. **Keep the Windows module where it is.** `app/desktop_body.py` stays the Windows module. About 120 test patches target its module globals, so moving them would break the suite.
2. **Add three modules:**
   - `app/desktop_common.py`: the platform-neutral helpers plus one shared body class, `DesktopBodyCore`.
   - `app/desktop_body_macos.py`: the macOS primitives. It calls pyobjc only through a thin `app/macos_native.py` adapter so tests can fake it on any OS.
   - `app/desktop_platform.py`: a facade that picks the right body for the host OS. Every caller goes through it.
3. **The platform module is the backend.** `DesktopBodyCore` holds the receipt and read-back logic once and calls primitives as `self._native.<name>`, where `_native` is the platform module. A test that runs `monkeypatch.setattr(desktop_body, "_focused_state", ...)` keeps working unchanged.
4. **Coordinates always mean the pixels of the last capture, as on Windows (`dpr: 1`).** On a Retina display the capture is that display's backing pixels. Accessibility frames and CoreGraphics points are multiplied by the view display's scale factor `S`; posted mouse events are divided by `S`.

There is one existing Windows bug to fix along the way; it is described at the end of section 3.

---

## 1. The contract

### 1a. Module-level functions other modules import from `app.desktop_body`

| Function | Callers | Keys callers read |
|---|---|---|
| `desktop_control_status()` (desktop_body.py:106) | main.py:1089 `GET /operator/desktop/status` (adds `keep_awake`, rewrites `control_indicator.message`); main.py:1111 `POST /operator/desktop/control`; extension_run.py:13380, 13514 (`_chrome_launch_available`), 13912, 17360/17370/17381, 35853; Electron main.mjs:418/429/508 (`desktopStatus` at :503); remote_access.py:148 | **Required:** `available`, `enabled`, `control_indicator.{active, message, run_ids, desktop_surface_active}`. **Informational:** `configured`, `platform`, `dependency_ready`, `dependency_error`, `device_id`, `capabilities`, `safety.{consequential_actions, dangerous_system_shortcuts, shell_execution, raw_clipboard_read:"run_copies_only"}` |
| `set_runtime_enabled(bool)` (:90) | main.py:1111 | — |
| `pointer_events(since)` (:218) | main.py:1120, polled by main.mjs:469 | `{last_seq, events:[{kind, x, y, dy, label, ts, seq}]}`. Windows publishes physical virtual-screen pixels, which main.mjs:448 converts with `screen.screenToDipPoint`. That call doesn't exist on macOS, so the identity fallback applies. **The macOS body must publish global points.** |
| `window_ids_snapshot()` (:437) | extension_run.py:17269 `_windows_open_at_run_start` | — |
| `launch_application_named(name)` (:3018) | extension_run.py:13536 | — |
| `chrome_profiles()` (:2985) | extension_run.py:13596 | — |
| `DesktopBody`, `DesktopBody.device_id` | extension_run.py:13384, 13935, 13972, 13981, 17391; 35679 (`DesktopBody().set_control_indicator(..., surface="browser")` mirrors browser runs) | — |
| `_highlights_inverted` | extension_run.py:3441 | — |
| `_changed_outside`, `_ocr_locate` | agent_eval/osworld/body.py:728 (lazy import) | — |

The loop also checks `isinstance(body, DesktopBody)` at extension_run.py:17370, 17372 and 17394, and assigns the mutable `body.windows_before_task` field.

### 1b. Body methods and attributes

`SurfaceSwitchingBody.__getattr__` (surface_body.py:80) forwards every probe to the active body. The loop probes with `getattr` and `callable`, and checks parameter names with `inspect.signature`:
- extension_run.py:7178 `_unsupported_gesture_params`, :7221 `_pointer_button_kwargs`
- :9906 `_method_accepts`, :9914 `_type_mode_kwargs`
- :27238, 27294, 27300, 27702, 27748, 27812, 28229

`_on_native_thread` uses `functools.wraps`, so real signatures stay visible. **The macOS body must use the same parameter names as Windows.**

**Attributes:** `device_id`, `execution_surface = "desktop"`, `windows_before_task` (frozenset or None).

**Sync methods:**
- `tool_context()`, called through `asyncio.to_thread` at extension_run.py:30913.
  - Keys: `execution_surface`, `windows`, `applications[{app_id, name}]`, `key_format`, `typing_requires_caret`, `tool_targets`, and `os`.
  - `os` switches the prompt wording through `desktop_os_name` and `desktop_os_text` (vision_agent.py:914-938). Without it the prompt says "Windows". The OSWorld body sends `os: "Ubuntu"`.
- `downloads_snapshot()` and `downloads_listing(snapshot)`, probed at extension_run.py:17296 and 17324. If `downloads_listing` is missing, `read_download` is listed as unavailable (:6285).

**Async methods** (each takes a `timeout` that is ignored):
- `set_control_indicator(active, *, run_id, message, timeout, surface)` → `{ok, active, visible, surface}`
- `navigate(url, *, timeout)`; `observe(*, timeout)`
- `capture_screenshot(*, full_page, timeout)`. Do not add `lossless`: zoom probes it only off the desktop surface (:28469-28475).
- `mark_sensitive_screenshot_control(selector, *, expected_page_context, timeout)`, `ensure_sensitive_screenshot_control(...)` and `ensure_sensitive_screenshot_controls(selectors, ...)` → `{ok, selector_bound, page_context_bound, bound_selectors, mask_surface_signature, mask_mode, binding_rebound?, failure_reason?}`
- `vision_click(x, y, *, button, clicks, hold_ms, modifiers, allow_final_submit, allow_guarded_intermediate, expected_target_identity, expected_page_context, label_text, timeout)`
- `vision_click_control(selector, fallback_x, fallback_y, *, button, clicks, hold_ms, modifiers, **kwargs)`
- `vision_type(text, *, at_caret, restore_caret, sensitive, target_point, timeout)`
- `vision_type_control(selector, text, *, at_caret, sensitive, **kwargs)`
- `vision_scroll(dy, *, dx, timeout)`; `vision_wheel(x, y, dx, dy, *, modifiers, timeout)`
- `vision_key(key, *, repeat, hold_ms, timeout)`
- `vision_drag(x, y, to_x, to_y, *, button, path, hold_ms, modifiers, timeout)`
- `vision_select_text(text, x, y, *, timeout)`; `vision_hover(x, y, *, dwell_ms, timeout)`
- `vision_upload(x, y, document, *, timeout)`; `vision_upload_control(selector, document, fallback_x, fallback_y, **kwargs)`
- `vision_open_file(path, *, timeout)`; `vision_focus_window(window_id, *, timeout)`; `vision_launch_app(app_id, *, timeout)`
- `vision_read_clipboard(*, timeout)`; `vision_wait(wait_ms, *, timeout, text)`
- `vision_run_command(command, *, timeout_seconds, run_id)`: **omit this on macOS.** There is no WSL sandbox, and with the method absent `_actions_unavailable_on_body` (extension_run.py:6265) hides `run_command` from the model.

**Not provided by desktop bodies:** `navigate_back` / `navigate_forward`. The loop falls back to pressing `ALT+LEFT` / `ALT+RIGHT` (extension_run.py:27941). On macOS, either add those methods pressing CMD+[ and CMD+], or translate the chord.

### 1c. `capture_screenshot` result (built at desktop_body.py:3347-3396)

**Required top-level keys:**
- `ok`; `image` (base64 JPEG, already masked); `width`; `height`; `dpr` = 1.
- `url`, which must start with `desktop://` (extension_run.py:6627). Use `desktop://macos/<window_id>`.
- `title`.
- `surface_signature` and `stable_surface_signature` (extension_run.py:2165, 2258, 6925, 29681).
- `scroll_y` = 0; `scroll_height`; `page_evidence`.

**Required `page_evidence` keys:**
- `url`, `title`, `viewport_text` (the window lines), `width`, `height`, `scroll_y`, `scroll_height`
- `controls`, `windows`, `caret` (a dict, possibly empty), `password_controls_masked`
- `execution_surface: "desktop"`, which gates OCR of the front window's text (extension_run.py:3329)

**Optional `page_evidence` keys:**
- `focused_text_lines` `{control, line_count, lines: ["N|text"], caret_line}`: vision_agent.py:793-857, 2029; extension_run.py:4560, 14846, 15881.
- `folder_view` `{path, items, item_count}`: vision_agent.py:796-802, 2035.
- `document_url`: extension_run.py:3335, 7552, 14173-14216, 28438.
- `monitors` `[{monitor, primary, size, in_view, pinned}]`, present only with more than one monitor. `compact_page_evidence` (vision_agent.py:616) does not forward it today; the model only sees "on monitor N" inside the window lines.

**`controls[]` fields** (desktop_body.py:948-974):
- **Required:**
  - `selector` must match `;rid=([-0-9.]+)$` (`_selector_runtime_id`, extension_run.py:9861). It is compared against a key receipt's `focused_rid` at :20596.
  - `index` is relabelled by `ControlIdRegistry` in control_ids.py, keyed on selector, tag, role, type and name.
  - `tag`, `role`, `type`, `text`, `name`, `value`.
  - `x`, `y`, `width`, `height` in capture pixels, plus `box`.
  - `focused`, `password`, `enabled` / `disabled`, `visible`.
- **Optional:** `accepts_text`, `affordances`, `checked`, `selected`, `expanded` (vision_agent.py:398-446; extension_run.py:1582, 2399, 14982, 15275, 20594).
- **Set when masked:** `value_redacted`.
- **Never read by the loop:** `accessible_name`, `automation_id`, `framework_id`.

**The tag vocabulary matters.**
- The loop binds typing to the focused control when `tag` is `edit`, `document` or `combobox`, or when `accepts_text` is true (extension_run.py:20592; `_TEXT_TAKING_ROLES` at :14904).
- The body itself types at the caret into a `document` (desktop_body.py:1348, 3805) and pastes into it (:3824).
- Recommendation: map accessibility roles onto the Windows control-type names through an `_AX_ROLE_TO_TAG` table, and put the raw `AXRole/AXSubrole` in `type` (the slot Windows uses for its class name). Proposed mapping:

| macOS role | Windows-style `role` / `tag` |
|---|---|
| AXTextField, AXSearchField | Edit |
| AXTextArea | Document |
| AXComboBox, AXPopUpButton | ComboBox |
| AXCheckBox | CheckBox |
| AXRadioButton | RadioButton |
| AXLink | Hyperlink |
| AXMenuItem | MenuItem |
| AXRow | ListItem |
| AXButton | Button |
| AXStaticText | Text |
| AXWebArea | Document |

**`windows[]` fields:** `window_id`, `title`, `pid`, `active`, `box` (absolute), `monitor?`, `opened_before_task?`. The active window also carries:
- `document_url?`
- `image_box`, in capture pixels
- `drawn_over`, a list of capture-pixel rectangles; `[[-1,-1,0,0]]` means the stacking is unknown

The loop needs these for OCR crops and view memory (extension_run.py:3331-3340, 10441-10482, 10685-10688). When the desktop or shell is in front, the active window gets no `image_box`.

**`caret` keys:** `line_text`, `column`, `selection_length`, `selected_text`, `document`, `text_redacted?` (vision_agent.py:784-791; receipt hint at extension_run.py:734-760).

### 1d. Action results the loop reads

The main readers are `_vision_result_hint` (extension_run.py:568), `_vision_action_verified` (:945) and the chaining logic (:28575-28620).

**Every result:** `ok`, `action`, `desktop_surface: True`, and `failure_reason` on failure. When the result fails, the hint shows `failure_reason`, plus `failure_detail` if present.

**click and click_control:**
- `activation_attempted`, `activation_dispatched`, `activation_count`.
- `surface_changed`, `title_changed`.
- `active_typable` (True, False or None), `active_tag`.
- `focus_changed` (True, False or None), `caret`.
- click_control also: `selection_committed`, `selected_value`.
- point click also: `label_bound` and `label_text`. On refusal, `failure_reason: "label_elsewhere"` with `label_point` (:802-813).
- The copy-recording wrapper adds `clipboard_changed` (:968, 1002).
- Informational only: `click_snap_applied`, `clicked_control_*`, `rebound_by_point`.

**type:**
- `active_value_matches_expected`, `active_typable`, `active_has_value`, `active_value`, `active_value_length`, `active_value_redacted?`.
- `value_read_back`, and `value_read_by` = "screen_text" when the value was read from pixels.
- `typed_where_caret_is` (:690); `edited_line_before` / `edited_line_after` (:703).
- `text_delivery`: keystrokes, paste or paste_unconfirmed (:6898).
- `focus_point` in capture pixels (`_body_focus_point`, :28587); `input_dispatched`; `surface_changed`.
- Diagnostics: `focused_before`, `focused_after`, `focus_repair`, `caret_restored`, `typed_at_caret`.
- Failure reasons:
  - `typed_text_corrupted` (:28759), `type_focus_elsewhere` (:797), `type_focus_unverified` (:792)
  - `desktop_untargeted_type_window`, `desktop_type_had_no_effect`, `desktop_uia_target_stale`
  - `desktop_type_target_not_focused`, `desktop_type_focus_left_target`

**key:**
- `surface_changed`, `focus_changed`.
- `focused_rid`: the dotted id of the focused element (:28616).
- `caret`, `title_changed`, `active_window_title`, `active_typable`, `presses`, `hold_ms`.
- An unknown key or blocked shortcut gives `failure_reason: "unsupported_key"`, and nothing is pressed.

**scroll and wheel:** `before_scroll_y`, `scroll_y` (0, or 1 when movement was proven), `scroll_changed`, `surface_changed`, `wheel_point`.

**drag:** `activation_*`, `surface_changed`, `active_typable`, `active_tag`.

**hover:** `surface_changed`.

**select_text:**
- Selected natively: `mode: "text_pattern"`, `selected_text`.
- Selected via OCR: `mode: "screen_text"`, `occurrences`, `highlight_changed`, `changed_outside_text`, `read_approximately?`.
- Failures: `select_text_not_found`, `select_text_not_text`, `select_text_mismatch`.

**upload:** `observed_present`, `filename`.

**open_file and launch_app:** `surface_changed`, `title_changed`, `new_window_id`, `new_window_title`, and `opened_file` or `launched_app_id`.

**focus_window:**
- `focus_changed`, `title_changed`.
- For `monitor:N`: `monitor`, `view_changed`.
- Failures: `invalid_window_id`, `desktop_window_stale`, `invalid_monitor`.

**read_clipboard:**
- `text`, which the loop pops off the result (:28447).
- `chars` (required for verification), `truncated`, `copied_by_run_action`.
- Failures: `clipboard_not_from_this_run`, `clipboard_sensitive_source`, `clipboard_unavailable`, `clipboard_holds_no_text`, `clipboard_busy`.

**wait:**
- `surface_changed`.
- With text: `text_matched`, `waited_ms`, `present_at_start`, and on failure `wait_text_not_seen`.

### 1e. Identifier formats

- `window_id` is opaque to the loop. Use `cgw:<CGWindowID>` on macOS.
- `app_id` is `app:<sha16>`.
- `monitor:N` is parsed by the body.
- Selector proposal: `ax:pid=<pid>;win=<cgwid>;rid=<n>`. `n` comes from a process-wide registry of accessibility element references, and `focused_rid` uses the same `n`.

---

## 2. How the body is selected today, and where macOS plugs in

**The enable gate:**
- `_windows_available()` is `platform.system() == "windows"` (desktop_body.py:82).
- `_configured()` requires both Windows and `settings.desktop_control_enabled` (:86). That setting comes from the env var `LUMEN_DESKTOP_CONTROL` (config.py:34); the packaged host sets it true at main.mjs:179, and conftest sets it false.
- `runtime_enabled()` (:101) also requires the runtime switch. `_require_enabled()` (:251) raises `RuntimeError` when unavailable or paused.
- Status is computed at :106-185. `dependency_ready` comes from importing pywinauto and win32, and `safety.raw_clipboard_read` is "run_copies_only" (:183).

**The runtime switch:** `POST /operator/desktop/control?enabled=` (main.py:1109), called from the host's IPC handler (main.mjs:712).

**Where bodies are constructed:**
- Desktop route: extension_run.py:13911-13990 checks `available` and `enabled`, reserves `device_id`, then builds `DesktopBody()`.
- Resume: :13379-13396.
- Surface switching: `_surface_switching_body` (:17356) builds a `SurfaceSwitchingBody` with `_enter_desktop` and `_enter_browser` factories.
- Browser runs mirror the screen indicator through `_publish_screen_indicator` (:35669).
- Reachability of a parked ask: :35841.

**Windows-only wording and paths that need a macOS branch:**
- `_extension_body_required_message` (:13739) says "Windows" for `desktop_*` reason codes.
- Chrome self-launch hard-codes `chrome.exe` paths (:13522).
- runtime.py:178 and cognition.py:1715 store `desktop://windows` as the run's target. Keep that as an opaque marker.

**Plug-in point: `app/desktop_platform.py`.**
- `module()` returns `desktop_body` on win32 and `desktop_body_macos` on darwin, and can be pinned by tests.
- Its wrappers look up attributes at call time: `desktop_control_status`, `set_runtime_enabled`, `pointer_events`, `window_ids_snapshot`, `launch_application_named`, `chrome_profiles`, `body_class()`, `device_id()`.
- Point main.py:1089, 1111, 1120 and every extension_run import in section 1a at the facade.
- Replace the `isinstance(body, DesktopBody)` checks with a marker base class, `LocalDesktopBody`, defined in `desktop_common`.
- Existing patches on `app.desktop_body.*` keep applying while the facade is pinned to `desktop_body`. These include test_bench_lanes.py:125, test_in_browser_routing.py:215/258/279 and test_extension_run.py:23000.
- Add one autouse fixture in conftest, next to the existing one at :132-139, that pins the facade to `desktop_body`. The Windows suite then behaves the same on a Mac.

**What macOS status should report:**
- `platform: "Darwin"`, `device_id: "macos-desktop"`.
- `dependency_ready`: Quartz, ApplicationServices, AppKit, Vision and ScreenCaptureKit import. The repo's `.venv` already has pyobjc 12.2.2, and every needed symbol is present.
- `permissions: {accessibility: AXIsProcessTrusted(), screen_recording: CGPreflightScreenCaptureAccess(), post_events: CGPreflightPostEventAccess()}` and `permissions_missing: [...]`.
- `available` = configured and dependencies present and no permission missing.
- `safety.shell_execution: "unavailable"`, `raw_clipboard_read: "run_copies_only"`.
- Capabilities include "active_window_ax_inventory".
- Add a reason code `desktop_permission_missing` to `_extension_body_required_message`, with OS-neutral text.

---

## 3. Shared code versus Windows-only code, and the refactor shape

### Pure helpers to move into `desktop_common.py`

Re-export them from `desktop_body` (tests call them as `desktop_body.X`).

- **View and monitor math:** `_monitor_of_box` (326-339), `_view_crop` (367-373), and the pure core of `_select_view` (342-364), with the foreground box passed in as a parameter.
- **Text helpers:**
  - `_safe_text` (376); `_document_url` and `_DOCUMENT_URL_SCHEMES` (448-463)
  - `_focus_summary` (1264), `_focus_change` (1306), `_typable_receipt` (1205)
  - `text_line_window` and its limits (1403-1458)
  - `_paragraph_edit` (1814-1839), `_edit_excerpts` (1842-1853)
- **Image helpers:** `_changed_outside` (1677), `_changed_near_point` (1937), `_changed_within_box` (1964), `_spread` and `_highlights_inverted` (1976-2017), `_image_changed` (2020).
- **OCR geometry:** `_ocr_locate` (1687-1724), `_typed_text_read_near_point` (1911-1934).
- **Key and gesture parsing:** `_normalize_key_parts` (2492-2527), `_gesture_label` (2803), `_GESTURE_MODIFIER_KEYS`.
- **Clipboard bookkeeping:** `CLIPBOARD_READ_LIMIT_CHARS` and the copy ledger (2239-2241, 3169-3172).
- **App filtering:** `_app_name_allowed` (2901-2917), with a per-OS list of blocked names. Also the app-inventory cache and id hashing (2961-2982).
- **Native thread:** `_NativeLoop` and `_on_native_thread` (3072-3141).
  - Make the per-thread setup a parameter: `_join_com_apartment` on Windows, none on macOS.
  - Make the wrapper walk the class hierarchy so inherited coroutines are wrapped too.

One rule: never move a helper whose caller also moves while a test patches that helper on `desktop_body`. No such case exists today.

### Orchestration to lift into `DesktopBodyCore(LocalDesktopBody)`

This logic depends only on primitives. The `DesktopBody` class spans lines 3146-4700.
- Screenshot assembly (3244-3396) and sensitive-control marking (3398-3481).
- `vision_click`, including label binding and its result (3483-3582); `vision_click_control` (3584-3707).
- `vision_type`, the whole read-back state machine (3709-4058); `vision_type_control` (4060-4151).
- Scroll, wheel, key, drag, select_text, hover and upload.
- read_clipboard (4575-4619); wait and `_wait_for_window_text` (4621-4700).
- `set_control_indicator` (3174-3205).
- `_records_clipboard_copies` (2331-2361) and the `_deliver_text` policy (2364-2393).
- The `_ClipboardHold` state (2150-2185), with the clipboard writer passed in.

Mechanically, every module global the methods use becomes `n = self._native; n._focused_state()`; section 3's protocol list names them all. Windows then becomes `class DesktopBody(DesktopBodyCore)` with `_native = sys.modules[__name__]`, the same dataclass fields, and `device_id = "windows-desktop"`.

Every existing patch on `desktop_body.*` still takes effect: about 120 targets, including `_capture_image`, `_require_enabled`, `_focused_state`, `_LAST_TARGET_WINDOW`, `_LAST_CLICK_POINT`, `_deliver_text`, `_send_key` and `_publish_pointer`.

### Windows-only code (stays in `desktop_body.py`)

- **Desktop, view and monitors:** `_desktop`, `_active_wrapper` (259-279); `_desktop_union`, `_virtual_screen`, `_monitors` (290-323).
- **Window list and stacking:** `_window_inventory` (399-434); `_window_class`, `_explorer_folder_view` (466-520); `_window_drawn_in_capture`, `_windows_drawn_over`, `_foreground_stacking` (527-591).
- **Control inventory:** UI Automation patterns, hit testing, hidden documents and popups: `_control_affordances` through `_active_controls` (594-1012).
- **Focus, caret and typability:**
  - Caret restore and focus repair (1015-1111); `_foreground_caret_present` (1150-1191).
  - `_typability`, `_readable_active_values`, `_element_box`, `_element_center` (1194-1261).
  - `_os_focused_wrapper`, `_focus_identity`, `_focused_state`, `_caret_state` (1272-1398).
  - `_inside_web_document`, `_focused_text_reading`, `_focused_text_lines`, `_caret_paragraph` (1466-1639).
  - `_control_type_at`, `_double_click_seconds` (1653-1674); `_select_text_by_uia` (1736-1811).
  - `_focus_within`, `_point_within_control`, `_resolve_control`, `_capture_image`, `_window_capture_box` (1856-1961).
- **Input:**
  - `_send_unicode` (2030-2088).
  - Virtual-key tables, `_key_code`, `_check_key`, `_key_steps`, `_send_key` (2398-2656).
  - `_set_foreground_window`, `_absolute_point`, `_remember_target_window`, `_same_app_window`, `_foreground_window`, and the mouse functions (2658-2898).
- **Clipboard I/O:** 2098-2147, 2188-2235, 2244-2328.
- **Apps:** Start-menu and shell app enumeration, the `chrome_profiles` path, and `_launch_inventory_target` (2920-3069).
- **Direct win32 calls inside methods that need new hooks:**
  - `_wheel_at` (4192)
  - `vision_focus_window` (4504, the `hwnd:` regex)
  - `vision_open_file` (`os.startfile`, 4479)
  - `_wait_for_window_text` (4659)

### The native protocol both platform modules provide

New names are in bold.

- **Shared state:** `_VIEW`, `_CONTROL_LOCK`, `_LAST_TARGET_WINDOW`, `_LAST_CLICK_POINT`, `_LAST_FOCUS_REPAIR`, and the indicator globals.
- **View and capture:** `_monitors`, `_select_view`, `_virtual_screen`, `_absolute_point`, **`_pixel_scale()`** (1.0 on Windows, `S` on macOS), `_capture_image`, `_foreground_stacking`.
- **Windows:**
  - `_window_inventory`, `_foreground_window`, **`_foreground_handle`**, `_window_capture_box`.
  - **`_is_shell_window(win)`** and **`_folder_view(active)`**, replacing the Windows class-name checks.
  - **`_parse_window_id`**, `_set_foreground_window`, `_remember_target_window`, `_same_app_window`.
- **Inventory and focus:**
  - Inventory: `_active_controls`, `_focused_text_lines`, `_parse_selector`, `_resolve_control`, `_readable_active_values`.
  - Focus and caret: `_focused_state`, `_focus_identity`, `_caret_state`, `_caret_paragraph`, `_restore_caret_at_last_click`, `_focus_text_control_within_focus`.
  - Targeting and selection: `_exact_click_target`, `_control_type_at`, `_NOT_TEXT_CONTROL_TYPES`, `_select_text_by_uia`.
- **Element operations** (replacing direct use of pywinauto wrapper objects inside methods): **`_element_rect`**, **`_element_is_password`**, **`_element_selected`**, **`_element_set_focus`**, `_control_value`, `_clickable_point`, `_point_within_control`, `_point_hits_element`, `_focus_within`.
- **Input:**
  - `_mouse_click`, `_mouse_drag`, `_mouse_move`, **`_wheel(abs_x, abs_y, dx, dy)`**, `_held_modifiers`.
  - `_send_unicode`, `_send_key`, `_send_key_async`, `_check_key`, `_double_click_seconds`.
  - **`_SELECT_ALL_KEY`** and **`_PASTE_KEY`**: "CTRL+A" / "CTRL+V" on Windows, "CMD+A" / "CMD+V" on macOS.
- **Clipboard:** `_clipboard_sequence`, `_read_clipboard_text`, `_clipboard_owner_process`, `_foreground_process_id`, and the `_ClipboardHold` writer.
- **Apps and files:** `_application_inventory`, `_launch_inventory_target`, **`_open_path`**.
- **Status and identity:** `_require_enabled`, `runtime_enabled`, `_publish_pointer`, `DEVICE_ID`, `SURFACE_URL_PREFIX`, `DESKTOP_TITLE`, `MASK_MODE`, and `TOOL_CONTEXT_EXTRA` (holding `os` and `key_format`).

**Pixel constants.** The core should multiply its tuned pixel constants by `_pixel_scale()`:
- `_TYPE_POINT_SLACK` (12), `_TYPE_CLICK_REACH` (40), `_LABEL_CLICK_REACH` (200), `_UIA_SELECT_REACH` (400)
- the search radii in `_changed_near_point` and `_typed_text_read_near_point`
- the 360×240 band used to prove a wheel scrolled

Windows output stays byte-identical because its scale is 1.0.

**Existing Windows bug to fix during the lift.** `vision_type` passes `_LAST_CLICK_POINT` to image functions that expect capture pixels: `_changed_near_point(...)` and `_typed_text_read_near_point(...)` at desktop_body.py:3952/3954. But `_mouse_click` stores that point as an absolute screen point via `_absolute_point` (:2833-2835). The result is wrong whenever the view's origin isn't (0,0): a Windows monitor other than the top-left one, or a macOS secondary display. Add a `_capture_point(abs)` conversion in the core.

---

## 4. The `screen_ocr.py` interface and a Vision backend

**Public API (unchanged):**
- `available()`.
- `read_lines(images: list[bytes], *, timeout) -> list[list[str]]`, cached by SHA-256 in `_cache` (limit 160).
- `read_text_lines(images, *, timeout) -> list[list[{"text", "words": [{"t","x","y","w","h"}]}]]`, cached in `_words_cache`; it also fills `_cache`.
- `read_words(image) -> list[list[word]]`.
- The pure functions `find_text(value, images, *, zero_misreads)`, `find_in_lines` and `locate_phrase(text, lines, near)`.

**Callers:** desktop_body.py:1717, 1719, 1931, 4669; extension_run.py:2579, 3353, 7976, 10628, 21772, 21902; osworld/body.py:841, 852. A failure reads as "no text" and is not cached.

**The Windows-specific seam is two functions:** `available()` (:82) and `_invoke_ocr(paths, timeout, words)` (:86).
- `_invoke_ocr` returns PowerShell's JSON: a list of line-string lists per image, or in word mode one `{"lines": [{"text", "words": [...]}]}` object per image.
- `_run_ocr` and `_text_lines` normalize that output.

**macOS backend:**
1. On darwin, `available()` returns True when `Vision` imports.
2. Route to an in-process `_vision_ocr(images, words)`. No temp files are needed: use `VNImageRequestHandler.alloc().initWithData_options_(NSData, None)`.
3. Configure `VNRecognizeTextRequest` with `recognitionLevel = Accurate` and `usesLanguageCorrection = False`, so codes aren't "corrected". Set `automaticallyDetectsLanguage`, or take `recognitionLanguages` from the user's preferences.
4. Call `performRequests_error_` synchronously inside `objc.autorelease_pool()`, and enforce `timeout` with a worker-thread join.
5. Each `VNRecognizedTextObservation` becomes one line via `topCandidates_(1)[0].string()`. Sort lines by vertical band, then x.
6. Word boxes come from `candidate.boundingBoxForRange_error_(NSRange)` for each whitespace token. Vision's boxes are normalized with a bottom-left origin; convert them to top-left pixels: `x = bx*W`, `y = (1-by-bh)*H`, `w = bw*W`, `h = bh*H`.
7. If a word's box comes back equal to the whole line's box (a known quirk), split the line box by character count, the same way `locate_phrase` already estimates character width.
8. Emit exactly the existing structures, so the caches, `_text_lines` and the find/locate logic are reused unchanged.

The slashed-zero and capital-I folding stays harmless. Measure Vision's actual misreads on SF Mono and Menlo before extending `_ZERO_MISREADS`.

---

## 5. Tests and how they fake the platform

**Files:**
- test_desktop_body.py (3060 lines, 95 tests); test_clipboard_read.py; test_keep_awake.py.
- test_screen_ocr.py (pure functions only).
- test_osworld_body.py, which patches `desktop_body._ocr_locate`, `screen_ocr.available` and `screen_ocr.read_lines`.
- The tests in test_extension_run.py, test_bench_lanes.py and test_in_browser_routing.py listed in section 2.

**Faking techniques in use:**
1. **Module-global patches** with `monkeypatch.setattr(desktop_body, "<helper>", fake)`. Methods resolve globals at call time, so the patch takes effect. Most-patched names:
   - `_capture_image` 27, `_require_enabled` 24, `_publish_pointer` 16
   - `_send_key` 14, `_focused_state` 14, `_LAST_TARGET_WINDOW` 11
   - `_window_inventory` 10, `_configured` 10, `_deliver_text` 10
2. **Fake platform modules** with `monkeypatch.setitem(sys.modules, ...)` for win32api, win32gui, win32clipboard, win32con, pywinauto and win32com.client. This works because every platform import is lazy, inside a function.
3. **Duck-typed wrapper doubles:** `_FakeWrapper`, `_FakeInfo`, `_FakeControl` (test_desktop_body.py:18-75).
4. **Monitor reset:** conftest.py:132-139 forces `_monitors` to return [] and resets `_VIEW`.
5. **No real sleeps:** tests patch `desktop_body.time.sleep`.
6. **Windows-only tests are skipped elsewhere:** `@pytest.mark.skipif(platform.system() != "Windows")` at :407 and :460, and `pytest.importorskip("win32gui")` at :2996.
7. **Fake bodies for loop-level tests:** `FakeDesktopBody` (:322) and `_CopyingDesktopBody` in test_clipboard_read.py.

**macOS test plan (runs on any OS):**
- **Lazy adapter.** Keep every pyobjc import lazy and behind `app/macos_native.py`: `ax_copy(el, attr)`, `ax_copy_many`, `ax_children`, `ax_element_at(pt, app_pid)`, `ax_set`, `ax_action`, `cg_window_list(option, rel)`, `displays()`, `capture_display(id, exclude_pids)`, `post_mouse`, `post_key`, `post_unicode`, `post_scroll`, `pasteboard_*`, `vision_ocr`.
- **`FakeMac`.** Tests replace `desktop_body_macos._native_api` with a fake holding:
  - an in-memory accessibility tree (role, subrole, frame in points, value, settable flags, actions, children)
  - a CoreGraphics window list and a display list with `scale=2`
  - a pasteboard with a `changeCount`
  - an event log
- **Assertions:**
  - Retina conversion: an AX frame in points lands in capture pixels; a click at capture (200,100) posts a CGEvent at (100,50) points on a 2x main display; a secondary display's origin is offset correctly.
  - Selector and rid stability across captures; the role-to-tag mapping; password masking via AXSecureTextField.
  - `drawn_over` built from `kCGWindowListOptionOnScreenAboveWindow`.
  - CMD chord translation and the blocked chords.
  - Multi-clicks set `kCGMouseEventClickState`; drags use `LeftMouseDragged` events.
  - The clipboard ledger tracks `changeCount`; concealed markers are refused; transient markers are written.
  - Vision's bottom-left boxes convert to top-left.
- **Run the shared result tests against both modules,** through a parametrized fixture (`native = desktop_body` or `desktop_body_macos`), for the names that are faked anyway.
- **Gate live smoke tests** with `skipif(sys.platform != "darwin" or not os.environ.get("LUMEN_MAC_LIVE"))`.
- **conftest:** an autouse fixture that resets the macOS module's `_VIEW` and `_monitors` and pins the facade.

---

## 6. Phased order and permissions

### Phase 0: refactor with no behaviour change
Add `desktop_common`, the core lift, the facade and the `LocalDesktopBody` marker. Fix the absolute-versus-capture point bug and add `_pixel_scale`. Gate: the full Windows suite passes on Windows and on a Mac.

### Phase 1: smallest end-to-end slice (capture, windows, click, type, key, scroll)

**Displays and view:**
- Read displays with `CGGetActiveDisplayList` and `CGDisplayBounds` (points). `S` = `CGDisplayModeGetPixelWidth` divided by the bounds width.
- Each capture is one display, so it has a single scale. Store monitor rectangles as points × S so the `_monitor_of_box` overlap math is unchanged; report `size` as the display's true pixel size.

**Capture:**
- Use `SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_`, waited on with a `threading.Event`.
- The content filter excludes the Lumen host app's windows by PID; pass `LUMEN_HOST_PID` from main.mjs.
- Fallback: `/usr/bin/screencapture -x -D n`.
- Don't rely on `CGWindowListCreateImage`, which is deprecated and obsoleted in the macOS 15 SDK.
- Don't rely only on the overlays' `setContentProtection` to hide them.

**Windows:**
- List with `CGWindowListCopyWindowInfo(OnScreenOnly | ExcludeDesktopElements)`, filtered to layer 0. Titles need Screen Recording; fall back to the accessibility `AXTitle`.
- `window_id` = `cgw:<n>`.
- Map a CGWindowID to its accessibility window with the private `_AXUIElementGetWindow` through ctypes, falling back to matching frame and title.

**Frontmost app:**
- Read it from the system-wide accessibility element's `AXFocusedApplication`, or from the owner of the first layer-0 window in z-order.
- Don't use `NSWorkspace.frontmostApplication`: it goes stale in a process with no main run loop.

**Input:**
- **Click:** `CGEventCreateMouseEvent`, setting `kCGMouseEventClickState` to 1, 2 or 3 for multi-clicks. Right button is RightMouse; middle is OtherMouse with button 2. Modifiers go in `CGEventSetFlags` plus flags-changed key events.
- **Hover:** post `MouseMoved` events; `CGWarpMouseCursorPosition` doesn't generate them.
- **Typing:** `CGEventKeyboardSetUnicodeString` per UTF-16 unit (surrogate pairs in one event), 18 ms apart, with modifier flags forced to 0.
- **Keys:** a macOS virtual-key table:

  | Key | Code |
  |---|---|
  | Return | 36 |
  | Tab | 48 |
  | Space | 49 |
  | Delete (backspace) | 51 |
  | Esc | 53 |
  | Forward delete | 117 |
  | Home | 115 |
  | End | 119 |
  | Page Up | 116 |
  | Page Down | 121 |
  | Arrows | 123-126 |
  | F-keys | their own table |

  CTRL maps to Control; ALT and OPTION to Option; CMD, META, WIN and SUPER to Command. Letters use an ANSI table first; a later phase builds a layout-aware map with `UCKeyTranslate`.
- **Blocked chords:** CMD+Q, CMD+OPTION+ESC, CTRL+CMD+Q, CMD+SHIFT+Q, CMD+OPTION+SHIFT+Q, CMD+SPACE.
- **Scroll:** post `MouseMoved` to the point, then `CGEventCreateScrollWheelEvent` in pixel units. Calibrate `dy` against Windows wheel deltas, and check the direction empirically because natural scrolling may interfere.

**Overlay, tool context and prompt:**
- `_publish_pointer` emits global points.
- `tool_context` sends `os: "macOS"` and a `key_format` that names CMD.
- In vision_agent.py, split `_DESKTOP_OS_PHRASES` into per-OS tables, keeping the Ubuntu table identical. Add macOS replacements:
  - "CTRL+A, CTRL+C, CTRL+V, or ALT+TAB" becomes the CMD equivalents.
  - "File Explorer" becomes "Finder".
  - The run_command sentence (vision_agent.py:1895) is unconditional today and false on macOS.

**Electron host:** allow darwin where main.mjs currently gates on win32 (:330, :417, :487).

### Phase 2: accessibility inventory, caret and typed read-back

**Setup and walk:**
- Set `AXUIElementSetMessagingTimeout` to about 0.5 s on the system-wide and app elements. Batch reads with `AXUIElementCopyMultipleAttributeValues`.
- Walk the focused window's tree, using `AXVisibleChildren` for lists and tables, capped at 140 controls.
- Add open menus and popups: AXMenu children, and windows of the same PID above the active one (mirroring `_popup_windows_above`).
- For Chromium and Electron apps, first set `AXManualAccessibility = True` on the app element so the web content's accessibility tree is populated.

**Control fields:**
- `value` = `AXValue`, or empty for `AXSecureTextField`.
- States come from `AXEnabled`, `AXFocused`, `AXExpanded` and `AXSelected`. A checkbox's `AXValue` of 0 or 1 gives `checked`.
- `AXPress` maps to the "invoke" affordance. A settable text `AXValue` gives `accepts_text` and "set_value".

**Element ids:** `rid` comes from a process-wide LRU registry keyed by CFHash/CFEqual of the element reference. A dead element returns `kAXErrorInvalidUIElement`; report it as `desktop_uia_target_stale`.

**Click targeting and focus repair:**
- Hit-test with `AXUIElementCopyElementAtPosition` on the target app's element, which skips the Lumen overlays.
- Click at `AXActivationPoint` when present, else the frame center.
- Repair focus by setting `AXFocused`.

**Caret and read-back:**
- Read `AXSelectedTextRange` (a CFRange, via `AXValueGetValue`) and `AXSelectedText`.
- Get the visual line from `AXLineForIndex`, `AXRangeForLine` and `AXStringForRange`.
- Compute the paragraph, `focused_text_lines` and the caret's line from `AXValue`, split on `\n`, `\r` or `\u2029`.
- Accessibility ranges count UTF-16 units; convert them before indexing Python strings.

**Typability:**
- Text roles are typable; the listed non-text roles are not.
- Anything else counts as typable only when the focused element exposes `AXSelectedTextRange`.

**Web content:**
- An `AXWebArea` ancestor plays the role of `_inside_web_document`.
- `document_url` comes from the web area's `AXURL`.

### Phase 3: clipboard

- **Reading:** the sequence number is `NSPasteboard.generalPasteboard().changeCount()`. Read text with `stringForType_(NSPasteboardTypeString)`.
- **Private copies:** a copy counts as private when its types include `org.nspasteboard.ConcealedType` or `TransientType`.
- **Writing:**
  1. Call `prepareForNewContentsWithOptions_(NSPasteboardContentsCurrentHostOnly)`, which keeps it out of Universal Clipboard.
  2. `clearContents`, then write the string.
  3. Add empty markers: `org.nspasteboard.TransientType`, `ConcealedType` and `AutoGeneratedType`.
- **Text-or-nothing check:** treat file URLs and images as non-text content.
- **Copy ownership:** macOS has no clipboard-owner API.
  - Attribute a copy to the run when `changeCount` changed during the action and the frontmost PID was the same before and after.
  - Mark it sensitive when any of these hold: the frontmost app is a credential store (1Password, Bitwarden, KeePassXC, com.apple.Passwords and similar); the concealed marker is set; or Carbon's `IsSecureEventInputEnabled()` reports secure input.
- The body's own select-all and paste become CMD+A and CMD+V.

### Phase 4: launch, focus, open_file, OCR, keep-awake

**Apps:**
- **Inventory:** scan `.app` bundles in /Applications, /System/Applications (and Utilities) and ~/Applications. Read names from Info.plist with plistlib.
- **Blocked:** Terminal, iTerm, Warp, kitty, Alacritty, Script Editor, Automator, Shortcuts, Keychain Access, Passwords, Disk Utility, Migration Assistant, Directory Utility, Python and IDLE.
- **Launch:** `/usr/bin/open -a <exact .app path>` with no shell, or NSWorkspace `openApplicationAtURL`.

**open_file:** `/usr/bin/open <validated path>`.

**focus_window:**
- Set `AXMinimized = False`, perform `AXRaise`, and set `AXFrontmost = True` on the app.
- Don't rely on `NSRunningApplication.activate`; macOS 14+ cooperative activation can refuse it.
- Verify the result through the accessibility focused window.

**folder_view:** the Finder window's `AXDocument` URL plus `os.listdir` names. An AppleScript fallback would need Automation permission, so make it opt-in.

**Chrome:**
- `chrome_profiles` reads `~/Library/Application Support/Google/Chrome/Local State`.
- Add a darwin branch to `_launch_chrome_for_extension_body`.

**OCR:** the Vision backend from section 4.

**Keep-awake (keep_awake.py):**
- `_enabled()` also allows darwin.
- `_create_request` and `_clear_request` call `IOPMAssertionCreateWithName("PreventUserIdleDisplaySleep", ...)` and `IOPMAssertionRelease` through ctypes on IOKit. An alternative is `caffeinate -di -w <pid>`.
- The existing fakes keep working.

**Downloads (run_downloads.py):** `has_mark_of_the_web` checks quarantine through the `NSURLQuarantinePropertiesKey` resource value.

### Phase 5: select_text, drag, hover and upload

- **select_text:**
  - Find the text in `AXValue`, take the occurrence nearest the point by `AXBoundsForRange`, set `AXSelectedTextRange`, and read `AXSelectedText` back.
  - Otherwise use the OCR fallback through `_ocr_locate`, with the double-click time from `NSEvent.doubleClickInterval()`.
- **Drag:** `LeftMouseDown`, then `LeftMouseDragged` steps, then `LeftMouseUp`, always released.
- **Upload in the open panel:** CMD+SHIFT+G, type the absolute path, ENTER, a short pause, ENTER. Confirm the sheet exists through accessibility before typing.

### Packaging

- **requirements.txt:** pyobjc-core and the Quartz, ApplicationServices, Cocoa, Vision and ScreenCaptureKit frameworks, all ==12.2.2 with `; sys_platform == "darwin"`.
- **lumen-api.spec:** make `collect_submodules("pywinauto")` and the win32 hidden imports conditional, and add objc and the frameworks on darwin.
- **Signing:** sign the app and sidecar with a stable identity. Ad-hoc rebuilds reset the TCC (privacy) grants.

### Permissions

**What the backend needs:**
- **Accessibility:** for accessibility reads and writes and for posting CGEvents. Check with `AXIsProcessTrusted` and `CGPreflightPostEventAccess`.
- **Screen Recording:** for capture and for other apps' window titles. Check with `CGPreflightScreenCaptureAccess`. It takes effect only after the app is relaunched.
- **Automation:** only for the optional Finder AppleScript.
- **Input Monitoring:** not needed.

**Who gets the grant:** macOS's privacy system (TCC) charges permissions to the "responsible" process. For the packaged sidecar spawned by Electron that is Lumen.app. In development it is Terminal or VS Code.

**How missing permissions are reported:**
- `desktop_control_status()` returns `available: false` with `permissions` and `permissions_missing`, so the desktop route refuses the run with the `desktop_permission_missing` reason.
- A new endpoint, `POST /operator/desktop/permissions/request`, calls `AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})` and `CGRequestScreenCaptureAccess()`.
- The host app can open the System Settings panes at `x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility` and `?Privacy_ScreenCapture`.

**At run time:**
- `_require_enabled` raises a message naming the missing permission.
- The accessibility error `kAXErrorAPIDisabled` (-25211) maps to `failure_reason: "desktop_accessibility_permission_missing"`.
- A capture without Screen Recording refuses rather than returning wallpaper-only pixels.
- macOS 15+ may periodically ask the user to re-confirm Screen Recording, so the status must cope with the permission switching off mid-run.

---

## Critical files

- `apps/api/app/desktop_body.py`
- `apps/api/app/extension_run.py`, especially lines 6265, 7178-7245, 9861, 13379-13990, 17265-17410, 20580-20600, 27230-28480, 28575-28620, 30913, 35669, 35841
- `apps/api/app/screen_ocr.py`
- `apps/api/tests/test_desktop_body.py`, plus `tests/conftest.py:132-139` and `tests/test_clipboard_read.py`
- `apps/desktop/main.mjs` (lines 292-360, 417-460, 487), plus `lumen-api.spec` and the requirements files
