// Headless in-browser body (background service worker).
//
// Holds a persistent WebSocket to the Lumen brain and executes the typed commands it sends against
// the one tab it owns. No planning, no value decisions: all user I/O and narration live in the
// desktop/side-panel UI. Dumb body, smart brain.

const DEFAULT_BACKEND = "http://127.0.0.1:8001";
const NOTIFICATION_ICON = "icons/icon128.png";
const LAST_HUD_PAYLOAD_KEY = "lumenLastHudPayload";
const OPEN_PANEL_WINDOW_IDS_KEY = "lumenOpenPanelWindowIds";
const TASK_PANEL_NOTIFICATION_PREFIX = "lumen-open-panel";
const TARGET_TAB_STORAGE_KEYS = ["lumenTargetTabId", "lumenTargetOwned", "lumenRunTabIds"];
const CONTROL_INDICATOR_SCRIPT = "src/control_indicator.js";
const CONTROL_INDICATOR_LEASE_MS = 5 * 60 * 1000;
const POINTER_OVERLAY_SCRIPT = "src/pointer_overlay.js";
const EXTENSION_BUILD = "2026-09-28-lumen-review-v130";
let socket = null;
// Bumped whenever the command socket closes. A command still waiting on the page (a label-named
// click's auto-wait) checks it before any input: the server abandoned that command with the socket.
let commandSocketEpoch = 0;
let targetTabId = null;
// The tabs this run owns: the task tab plus tabs its own clicks opened and adopted. Only these
// are ever listed to the model or switched to - never the user's other tabs.
let runTabIds = [];
let lastNotificationKey = "";
let lastNotificationAt = 0;
let latestHudPayload = null;
let transientSensitiveScreenshotBinding = null;
let screenshotCaptureTail = Promise.resolve();
let controlIndicatorState = {
  active: false,
  expires_at: 0,
  message: "Lumen is using your computer",
};

function controlIndicatorPayload(overrides = {}) {
  const active = Object.prototype.hasOwnProperty.call(overrides, "active")
    ? overrides.active === true
    : controlIndicatorState.active === true;
  return {
    active,
    capture_hidden: overrides.capture_hidden === true,
    expires_at: active ? controlIndicatorState.expires_at : 0,
  };
}

async function applyControlIndicatorToTab(tabId, overrides = {}) {
  const normalizedTabId = Number(tabId);
  if (!Number.isFinite(normalizedTabId) || normalizedTabId <= 0) {
    return { ok: false, visible: false, failure_reason: "control_indicator_tab_unavailable" };
  }
  const payload = controlIndicatorPayload(overrides);
  try {
    await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId: normalizedTabId },
        files: [CONTROL_INDICATOR_SCRIPT],
      }),
      2500,
      "control indicator injection timed out",
    );
    const rows = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId: normalizedTabId },
        func: (state) => (
          typeof globalThis.__lumenSetControlIndicator === "function"
            ? globalThis.__lumenSetControlIndicator(state)
            : { ok: false, visible: false, reason: "control_indicator_setter_unavailable" }
        ),
        args: [payload],
      }),
      2500,
      "control indicator update timed out",
    );
    const result = (rows && rows[0] && rows[0].result) || {};
    return {
      ok: result.ok === true,
      visible: result.visible === true,
      tab_id: normalizedTabId,
      capture_hidden: payload.capture_hidden,
      failure_reason: result.reason || "",
    };
  } catch (error) {
    return {
      ok: false,
      visible: false,
      tab_id: normalizedTabId,
      capture_hidden: payload.capture_hidden,
      failure_reason: errorMessage(error) || "control_indicator_injection_failed",
    };
  }
}

async function setControlIndicator(params = {}) {
  const active = params.active === true;
  controlIndicatorState = {
    active,
    expires_at: active ? Date.now() + CONTROL_INDICATOR_LEASE_MS : 0,
    message: String(params.message || "Lumen is using your computer").slice(0, 120),
  };
  const tabId = await activeTabId();
  if (!tabId) {
    return {
      ok: true,
      active,
      visible: false,
      pending_target_tab: active,
    };
  }
  if (!active) {
    hidePointerOverlay(tabId);
    void detachAllDebuggerSessions();
  }
  const applied = await applyControlIndicatorToTab(tabId);
  return {
    ...applied,
    active,
    pending_target_tab: false,
  };
}

async function refreshControlIndicatorLease() {
  if (!controlIndicatorState.active) return { ok: true, active: false };
  controlIndicatorState.expires_at = Date.now() + CONTROL_INDICATOR_LEASE_MS;
  const tabId = await activeTabId();
  if (!tabId) return { ok: true, active: true, pending_target_tab: true };
  // A page frozen behind a browser dialog cannot run the indicator script (the injection would
  // block every command until the dialog closes); its indicator is already drawn.
  if (nativeDialogOpenFor(tabId)) return { ok: true, active: true, native_dialog_open: true };
  return applyControlIndicatorToTab(tabId);
}

// Virtual pointer overlay: shows the user where Lumen clicks, types, and scrolls on the
// task page. Every call is best-effort visual feedback — a failure never blocks or fails
// the action it decorates, and the overlay is capture-hidden for every screenshot so the
// brain's observations stay pointer-free.
function pointerOverlayStatusText() {
  // The moving pill narrates the run: every pointer gesture carries the control
  // indicator's current live line so the description travels with the cursor.
  return controlIndicatorState.active
    ? String(controlIndicatorState.message || "").slice(0, 90)
    : "";
}

async function pointerOverlayCommand(tabId, command) {
  const normalizedTabId = Number(tabId);
  if (!Number.isFinite(normalizedTabId) || normalizedTabId <= 0) {
    return { ok: false, visible: false, reason: "pointer_overlay_tab_unavailable" };
  }
  if (nativeDialogOpenFor(normalizedTabId)) {
    // A page frozen behind a browser dialog cannot run the overlay script.
    return { ok: false, visible: false, reason: "native_dialog_open" };
  }
  try {
    await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId: normalizedTabId },
        files: [POINTER_OVERLAY_SCRIPT],
      }),
      2500,
      "pointer overlay injection timed out",
    );
    const payload = command && command.op !== "hide" && command.op !== "set_capture_hidden"
      ? { status: pointerOverlayStatusText(), ...command }
      : command;
    const rows = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId: normalizedTabId },
        func: (cmd) => (
          typeof globalThis.__lumenPointerOverlay === "function"
            ? globalThis.__lumenPointerOverlay(cmd)
            : { ok: false, visible: false, reason: "pointer_overlay_unavailable" }
        ),
        args: [payload],
      }),
      2500,
      "pointer overlay command timed out",
    );
    return (rows && rows[0] && rows[0].result) || { ok: false, visible: false };
  } catch (error) {
    return { ok: false, visible: false, reason: errorMessage(error) };
  }
}

async function animatePointerToPoint(tabId, x, y, settleMs) {
  const result = await pointerOverlayCommand(tabId, { op: "move", x, y });
  if (result.ok && result.visible) {
    await new Promise((resolve) => setTimeout(resolve, settleMs));
  }
  return result;
}

function showPointerClickPulse(tabId, x, y) {
  void pointerOverlayCommand(tabId, { op: "click", x, y });
}

function pointerOverlayTypeHintInPage(status) {
  try {
    if (typeof globalThis.__lumenPointerOverlay !== "function") return { ok: false, visible: false };
    const deepActiveElement = typeof globalThis.__lumenDeepActiveElement === "function"
      ? globalThis.__lumenDeepActiveElement
      : () => {
        let node = document.activeElement;
        const seen = new Set();
        while (node && node.shadowRoot && node.shadowRoot.activeElement && !seen.has(node)) {
          seen.add(node);
          node = node.shadowRoot.activeElement;
        }
        return node;
      };
    const active = deepActiveElement();
    if (!active || active === document.body || active === document.documentElement) {
      return { ok: false, visible: false, reason: "no_active_control" };
    }
    const rect = (typeof globalThis.__lumenTopRect === "function" ? globalThis.__lumenTopRect(active) : active.getBoundingClientRect());
    if (!rect || (!rect.width && !rect.height)) return { ok: false, visible: false, reason: "no_active_rect" };
    return globalThis.__lumenPointerOverlay({
      op: "type",
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
      status: String(status || ""),
    });
  } catch (error) {
    return { ok: false, visible: false, reason: String(error && error.message || error) };
  }
}

async function showPointerTypingAtActiveControl(tabId) {
  const normalizedTabId = Number(tabId);
  if (!Number.isFinite(normalizedTabId) || normalizedTabId <= 0) {
    return { ok: false, visible: false, reason: "pointer_overlay_tab_unavailable" };
  }
  try {
    await chrome.scripting.executeScript({
      target: { tabId: normalizedTabId },
      files: [POINTER_OVERLAY_SCRIPT],
    });
    const rows = await chrome.scripting.executeScript({
      target: { tabId: normalizedTabId },
      func: pointerOverlayTypeHintInPage,
      args: [pointerOverlayStatusText()],
    });
    return (rows && rows[0] && rows[0].result) || { ok: false, visible: false };
  } catch (error) {
    return { ok: false, visible: false, reason: errorMessage(error) };
  }
}

function pointerOverlayScrollHintInPage(selector, dy, status) {
  try {
    if (typeof globalThis.__lumenPointerOverlay !== "function") return { ok: false, visible: false };
    let x = null;
    let y = null;
    const cleanSelector = String(selector || "").trim();
    if (cleanSelector) {
      const queryAll = typeof globalThis.__lumenDeepQueryAll === "function"
        ? globalThis.__lumenDeepQueryAll
        : (value) => Array.from(document.querySelectorAll(value));
      let matches = [];
      try { matches = Array.from(queryAll(cleanSelector)); } catch (_) { matches = []; }
      if (matches.length === 1) {
        const rect = (typeof globalThis.__lumenTopRect === "function" ? globalThis.__lumenTopRect(matches[0]) : matches[0].getBoundingClientRect());
        if (rect && (rect.width || rect.height)) {
          x = Math.min(Math.max(rect.left + rect.width / 2, 2), (window.innerWidth || 800) - 2);
          y = Math.min(Math.max(rect.top + rect.height / 2, 2), (window.innerHeight || 600) - 2);
        }
      }
    }
    return globalThis.__lumenPointerOverlay({ op: "scroll", dy: Number(dy || 0), x, y, status: String(status || "") });
  } catch (error) {
    return { ok: false, visible: false, reason: String(error && error.message || error) };
  }
}

function showPointerScrollHint(tabId, dy, selector) {
  const normalizedTabId = Number(tabId);
  if (!Number.isFinite(normalizedTabId) || normalizedTabId <= 0) return;
  void (async () => {
    try {
      await chrome.scripting.executeScript({
        target: { tabId: normalizedTabId },
        files: [POINTER_OVERLAY_SCRIPT],
      });
      await chrome.scripting.executeScript({
        target: { tabId: normalizedTabId },
        func: pointerOverlayScrollHintInPage,
        args: [String(selector || ""), Number(dy || 0), pointerOverlayStatusText()],
      });
    } catch (_) {
      /* pointer feedback is best-effort */
    }
  })();
}

async function setPointerOverlayCaptureHidden(tabId, hidden) {
  return pointerOverlayCommand(tabId, { op: "set_capture_hidden", hidden: hidden === true });
}

function hidePointerOverlay(tabId) {
  void pointerOverlayCommand(tabId, { op: "hide" });
}

async function withScreenshotCaptureLock(operation) {
  const previous = screenshotCaptureTail;
  let release = () => {};
  const slot = new Promise((resolve) => {
    release = resolve;
  });
  screenshotCaptureTail = previous.catch(() => {}).then(() => slot);
  await previous.catch(() => {});
  let indicatorTabId = null;
  let pointerTabId = null;
  try {
    if (controlIndicatorState.active) {
      indicatorTabId = await activeTabId();
      if (indicatorTabId) {
        const hidden = await applyControlIndicatorToTab(indicatorTabId, {
          capture_hidden: true,
        });
        if (!hidden.ok || hidden.visible) {
          throw new Error(
            hidden.failure_reason
            || "Lumen could not hide its control indicator from browser evidence.",
          );
        }
      }
    }
    pointerTabId = indicatorTabId || await activeTabId();
    if (pointerTabId) {
      const pointerHidden = await setPointerOverlayCaptureHidden(pointerTabId, true);
      // An injection failure means no overlay is mounted on that document; only a
      // mounted overlay that reports itself still visible can taint the capture.
      if (pointerHidden.visible) {
        throw new Error("Lumen could not hide its pointer overlay from browser evidence.");
      }
    }
    return await operation();
  } finally {
    if (indicatorTabId) {
      await applyControlIndicatorToTab(indicatorTabId, {
        capture_hidden: false,
      }).catch(() => {});
    }
    if (pointerTabId) {
      await setPointerOverlayCaptureHidden(pointerTabId, false).catch(() => {});
    }
    release();
  }
}

function clearTransientSensitiveScreenshotBinding() {
  transientSensitiveScreenshotBinding = null;
}

function normalizeSensitiveScreenshotSelectors(value) {
  const raw = Array.isArray(value) ? value : [value];
  const selectors = [];
  for (const item of raw) {
    const selector = String(item || "").trim();
    if (!selector || selectors.includes(selector)) continue;
    selectors.push(selector);
    if (selectors.length >= 24) break;
  }
  return selectors;
}

function sensitiveScreenshotMaskContextForTab(tabId) {
  const binding = transientSensitiveScreenshotBinding;
  if (!binding) return null;
  if (
    Number(binding.tab_id) !== Number(tabId)
    || !binding.document_token
    || !binding.expected_url
    || !binding.expected_mask_surface_signature
    || !(binding.selectors instanceof Set)
    || binding.selectors.size < 1
  ) {
    clearTransientSensitiveScreenshotBinding();
    return {
      required: true,
      document_token: "",
      expected_url: "",
      expected_mask_surface_signature: "",
      selectors: [],
      top_frame_only: true,
    };
  }
  return {
    required: true,
    document_token: String(binding.document_token),
    expected_url: String(binding.expected_url),
    expected_mask_surface_signature: String(binding.expected_mask_surface_signature),
    selectors: Array.from(binding.selectors).map((selector) => String(selector)),
    top_frame_only: true,
  };
}

// Real-browser ground truth for eval/verification: downloads the body actually triggered. Surfaced
// in observe so the brain + the eval see reality (not a claim). Generic capability, not job-specific.
const downloads = [];
function recordDownload(name) {
  if (!name) return;
  const base = String(name).split(/[\\/]/).pop();
  if (base && !downloads.includes(base)) downloads.push(base);
}
try {
  chrome.downloads.onCreated.addListener((item) => recordDownload((item && (item.filename || item.url)) || ""));
  chrome.downloads.onChanged.addListener((delta) => { if (delta && delta.filename && delta.filename.current) recordDownload(delta.filename.current); });
} catch (_) { /* downloads API unavailable in this context */ }

// A Gauntlet bench lane (a headless Chromium that live_run starts) loads a copy of this extension
// with bench_lane.json beside the manifest. Its device id marks it as a lane: the backend pins only
// bench runs to it and never routes the user's tasks there. The user's install has no such file.
let benchLaneConfig;
async function getBenchLaneConfig() {
  if (benchLaneConfig !== undefined) return benchLaneConfig;
  benchLaneConfig = null;
  try {
    const response = await fetch(chrome.runtime.getURL("bench_lane.json"));
    const lane = response.ok ? await response.json() : null;
    const deviceId = String((lane && lane.device_id) || "");
    const backend = String((lane && lane.backend) || DEFAULT_BACKEND);
    if (/^bench-lane-[a-z0-9-]{1,40}$/.test(deviceId) && /^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(backend)) {
      benchLaneConfig = { backend, deviceId };
    }
  } catch (_) { /* no lane file: the user's own install */ }
  return benchLaneConfig;
}

async function getConfig() {
  const lane = await getBenchLaneConfig();
  if (lane) return lane;
  const stored = await chrome.storage.local.get(["lumenDeviceId"]);
  let deviceId = stored.lumenDeviceId;
  if (!deviceId) {
    deviceId = "dev-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
    await chrome.storage.local.set({ lumenDeviceId: deviceId });
  }
  return { backend: DEFAULT_BACKEND, deviceId };
}

function wsUrl(backend, deviceId) {
  return backend.replace(/^http/, "ws") + "/extension/ws?device_id=" + encodeURIComponent(deviceId);
}

async function setStatus(connected) {
  await chrome.storage.local.set({ lumenConnected: connected });
  try {
    await chrome.action.setBadgeText({ text: connected ? "on" : "" });
    await chrome.action.setBadgeBackgroundColor({ color: connected ? "#1d9e75" : "#999999" });
  } catch (_) { /* action API may be unavailable mid-startup */ }
}

async function configureSidePanel() {
  if (!chrome.sidePanel) return;
  try {
    await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  } catch (_) {
    // Older Chrome builds may not support action-click side-panel behavior. The default panel still
    // remains available from Chrome's side-panel controls.
  }
}

async function openSidePanel(context = {}) {
  if (!chrome.sidePanel || typeof chrome.sidePanel.open !== "function") {
    return { ok: false, error: "Chrome side panel API is unavailable in this browser." };
  }
  try {
    const explicitWindowId = Number(context.windowId);
    const windowId = Number.isFinite(explicitWindowId) && explicitWindowId > 0 ? explicitWindowId : undefined;
    // sidePanel.open() must be invoked while Chrome still associates this call with the
    // originating notification click. Do not await configuration or a tab lookup first: either
    // can consume that transient activation and make Chrome reject the open. The manifest and
    // service-worker startup already configure the global panel. WINDOW_ID_CURRENT is the
    // synchronous fallback when the notification carries no window.
    const currentWindowId = chrome.windows && Number.isFinite(Number(chrome.windows.WINDOW_ID_CURRENT))
      ? Number(chrome.windows.WINDOW_ID_CURRENT)
      : -2;
    await chrome.sidePanel.open({ windowId: windowId != null ? windowId : currentWindowId });
    return { ok: true };
  } catch (error) {
    return { ok: false, error: errorMessage(error) || "Chrome refused to open the side panel." };
  }
}

function taskPanelNotificationId(windowId, tabId) {
  return `${TASK_PANEL_NOTIFICATION_PREFIX}-${windowId}-${tabId}-${Date.now()}`;
}

function parseTaskPanelNotificationId(notificationId) {
  const match = String(notificationId || "").match(/^lumen-open-panel-(-?\d+)-(\d+)-(\d+)$/);
  if (!match) return null;
  const windowId = Number(match[1]);
  const tabId = Number(match[2]);
  if (!Number.isFinite(windowId) || !Number.isFinite(tabId) || tabId <= 0) return null;
  return { windowId, tabId };
}

async function rememberPanelWindowState(windowId, open) {
  const normalizedWindowId = Number(windowId);
  if (!Number.isFinite(normalizedWindowId)) return;
  try {
    const stored = await chrome.storage.local.get([OPEN_PANEL_WINDOW_IDS_KEY]);
    const ids = new Set(
      (Array.isArray(stored[OPEN_PANEL_WINDOW_IDS_KEY]) ? stored[OPEN_PANEL_WINDOW_IDS_KEY] : [])
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value)),
    );
    if (open) ids.add(normalizedWindowId);
    else ids.delete(normalizedWindowId);
    await chrome.storage.local.set({ [OPEN_PANEL_WINDOW_IDS_KEY]: Array.from(ids) });
  } catch (_) {
    // Panel-state memory is only an attention optimization.
  }
}

async function panelIsOpenForWindow(windowId) {
  const normalizedWindowId = Number(windowId);
  if (!Number.isFinite(normalizedWindowId)) return false;
  try {
    const stored = await chrome.storage.local.get([OPEN_PANEL_WINDOW_IDS_KEY]);
    return (Array.isArray(stored[OPEN_PANEL_WINDOW_IDS_KEY]) ? stored[OPEN_PANEL_WINDOW_IDS_KEY] : [])
      .some((value) => Number(value) === normalizedWindowId);
  } catch (_) {
    return false;
  }
}

async function pushTaskPanelNotification(tab) {
  const tabId = Number(tab && tab.id);
  const windowId = Number(tab && tab.windowId);
  if (!Number.isFinite(tabId) || tabId <= 0 || !Number.isFinite(windowId)) {
    return { notified: false, reason: "tab_context_unavailable" };
  }
  if (await panelIsOpenForWindow(windowId)) {
    return { notified: false, reason: "panel_already_open" };
  }
  if (!chrome.notifications || typeof chrome.notifications.create !== "function") {
    return { notified: false, reason: "notifications_unavailable" };
  }
  const notificationId = taskPanelNotificationId(windowId, tabId);
  try {
    await chrome.notifications.create(notificationId, {
      type: "basic",
      iconUrl: notificationIconUrl(),
      title: "Lumen task started in Chrome",
      message: "Click to open Lumen beside the new task tab.",
      priority: 1,
      requireInteraction: true,
    });
    return { notified: true, notification_id: notificationId };
  } catch (error) {
    return { notified: false, reason: errorMessage(error) || "notification_failed" };
  }
}

// --- body commands (act on the tracked application tab) -----------------------------------

async function ensureTab(url, options = {}) {
  const createNewTab = Boolean(options.newTab);
  if (!createNewTab && targetTabId != null) {
    try {
      const tab = await chrome.tabs.get(targetTabId);
      if (tab) {
        if (url) await chrome.tabs.update(targetTabId, { url, active: true });
        await chrome.storage.local.set({ lumenTargetTabId: targetTabId, lumenTargetOwned: true });
        return targetTabId;
      }
    } catch (_) {
      targetTabId = null;
      await chrome.storage.local.remove(TARGET_TAB_STORAGE_KEYS).catch(() => {});
    }
  }
  let hasNormalWindow = true;
  try {
    const normalWindows = await chrome.windows.getAll({ windowTypes: ["normal"] });
    hasNormalWindow = Array.isArray(normalWindows) && normalWindows.length > 0;
  } catch (_) {
    hasNormalWindow = true; // probe unavailable: assume a window exists and let tabs.create decide
  }
  if (!hasNormalWindow) {
    // With multiple Chrome profiles the browser can come up at the profile picker with
    // ZERO normal windows, where tabs.create throws "No current window". A body with
    // hands makes itself a window instead of dying on it.
    const created = await chrome.windows.create({ url: url || "about:blank", focused: true });
    const createdTab = created && Array.isArray(created.tabs) && created.tabs.length ? created.tabs[0] : null;
    if (createdTab && createdTab.id != null) {
      targetTabId = createdTab.id;
      await chrome.storage.local.set({ lumenTargetTabId: targetTabId, lumenTargetOwned: true });
      return targetTabId;
    }
  }
  const tab = await chrome.tabs.create({ url: url || "about:blank", active: true });
  targetTabId = tab.id;
  await chrome.storage.local.set({ lumenTargetTabId: targetTabId, lumenTargetOwned: true });
  return targetTabId;
}

function waitForLoad(tabId, timeoutMs = 30000, options = null) {
  // With startWindowMs, wait for a load only if one starts: an in-page click never fires a load
  // event, so waiting out the whole timeout for it cost every such click the full timeout.
  const startWindowMs = options && Number(options.startWindowMs) > 0 ? Number(options.startWindowMs) : 0;
  return new Promise((resolve) => {
    let done = false;
    let loading = false;
    let quietTimer = null;
    const finish = () => {
      if (done) return;
      done = true;
      if (quietTimer) clearTimeout(quietTimer);
      chrome.tabs.onUpdated.removeListener(onUpdated);
      resolve({ navigation_started: loading });
    };
    const markLoading = () => {
      loading = true;
      if (quietTimer) { clearTimeout(quietTimer); quietTimer = null; }
    };
    const onUpdated = (id, info) => {
      if (id !== tabId) return;
      if (info.status === "loading") markLoading();
      if (info.status === "complete") finish();
    };
    chrome.tabs.onUpdated.addListener(onUpdated);
    setTimeout(finish, timeoutMs);
    if (startWindowMs) {
      quietTimer = setTimeout(() => { if (!loading) finish(); }, startWindowMs);
      try {
        chrome.tabs.get(tabId, (tab) => {
          if (!chrome.runtime.lastError && tab && tab.status === "loading") markLoading();
        });
      } catch (_) {}
    }
  });
}

function domQuietInPage(quietMs = 250, maxMs = 1000) {
  // Resolve once the page stops changing (no DOM mutation for quietMs), or at maxMs for a page
  // that never stops (tickers, animations). Lumen's own overlay is not the page.
  return new Promise((resolve) => {
    const started = Date.now();
    let last = started;
    let observer = null;
    const finish = (settled) => {
      try { if (observer) observer.disconnect(); } catch (_) {}
      resolve({ ok: true, settled, waited_ms: Date.now() - started });
    };
    try {
      observer = new MutationObserver((records) => {
        for (const record of records) {
          const node = record.target && record.target.nodeType === 1 ? record.target : record.target && record.target.parentElement;
          if (node && node.closest && node.closest('[data-lumen-pointer-overlay],[id^="lumen-"]')) continue;
          last = Date.now();
          return;
        }
      });
      observer.observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
    } catch (_) {
      finish("unobservable");
      return;
    }
    const tick = () => {
      const now = Date.now();
      if (now - started >= maxMs) return finish("max_wait");
      if (now - last >= quietMs) return finish("quiet");
      setTimeout(tick, 40);
    };
    setTimeout(tick, 40);
  });
}

async function tabPageEvidence(tabId, semanticSelectorsOverride = null) {
  const evidence = {
    tab_metadata_probe_complete: false,
    page_state_probe_complete: false,
    surface_probe_complete: false,
  };
  const semanticMaskContext = Array.isArray(semanticSelectorsOverride)
    ? null
    : sensitiveScreenshotMaskContextForTab(tabId);
  const semanticSensitiveSelectors = Array.isArray(semanticSelectorsOverride)
    ? semanticSelectorsOverride.map((selector) => String(selector || "").trim()).filter(Boolean)
    : (
      Array.isArray(semanticMaskContext && semanticMaskContext.selectors)
        ? semanticMaskContext.selectors
        : []
    );
  try {
    const tab = await chrome.tabs.get(tabId);
    evidence.url = tab && tab.url ? String(tab.url) : "";
    evidence.title = tab && tab.title ? String(tab.title) : "";
    evidence.tab_metadata_probe_complete = true;
  } catch (_) {
    // Keep the best evidence we can get; callers already handle absent tabs separately.
  }
  try {
    const [row] = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: _pageState }),
      1500,
      "page state timed out",
    );
    const state = (row && row.result) || {};
    if (state.url) evidence.url = String(state.url);
    if (state.title) evidence.title = String(state.title);
    if (state.scroll_y != null) evidence.scroll_y = state.scroll_y;
    if (state.scroll_x != null) evidence.scroll_x = state.scroll_x;
    if (Array.isArray(state.geometry_anchors)) evidence.geometry_anchors = state.geometry_anchors;
    if (state.scroll_height != null) evidence.scroll_height = state.scroll_height;
    if (state.scroll_width != null) evidence.scroll_width = state.scroll_width;
    evidence.page_state_probe_complete = true;
  } catch (_) {
    // Some pages reject script injection during navigation. Tab URL/title remain useful evidence.
  }
  try {
    const [row] = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId },
        func: _visionPageEvidence,
        args: [semanticSensitiveSelectors],
      }),
      // The observation reads the same evidence with 3 s; at 1.8 s a heavy page (iCIMS with its
      // content iframe) timed out here and its blank lifecycle token read as a different document.
      3000,
      "vision surface evidence timed out",
    );
    const pageEvidence = (row && row.result) || {};
    evidence.surface_signature = surfaceSignatureFromEvidence(pageEvidence);
    evidence.stable_surface_signature = stableSurfaceSignatureFromEvidence(pageEvidence);
    evidence.document_state_signature = String(pageEvidence.document_state_signature || "");
    evidence.document_value_signature = String(pageEvidence.document_value_signature || "");
    evidence.document_semantic_signature = String(pageEvidence.document_semantic_signature || "");
    evidence.document_row_digests = Array.isArray(pageEvidence.document_row_digests)
      ? pageEvidence.document_row_digests
      : [];
    evidence.document_lifecycle_token = String(pageEvidence.document_lifecycle_token || "");
    if (semanticSensitiveSelectors.length) {
      evidence.mask_surface_signature = await sensitiveMaskSurfaceSignatureFromEvidence(pageEvidence);
    }
    evidence.surface_control_count = Array.isArray(pageEvidence.controls) ? pageEvidence.controls.length : 0;
    evidence.surface_probe_complete = true;
  } catch (_) {
    // Surface diffing is best-effort verification. URL/title/focus still cover the core action receipt.
  }
  return evidence;
}

function withTimeout(promise, timeoutMs, label) {
  let timer;
  return Promise.race([
    Promise.resolve(promise),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(label || "operation timed out")), timeoutMs);
    }),
  ]).finally(() => clearTimeout(timer));
}

function errorMessage(error) {
  const msg = error && error.message ? String(error.message) : String(error || "");
  return msg || (error && error.name ? String(error.name) : "Unknown error");
}

// The run's owned tabs survive a service-worker restart with its target tab: an empty list after a
// restart made the run's own parent tab unlisted and unswitchable (tab_not_in_run).
function persistRunTabIds() {
  void chrome.storage.local.set({ lumenRunTabIds: runTabIds.slice(0, 32) }).catch(() => {});
}

async function activeTabId() {
  if (targetTabId != null) {
    try { await chrome.tabs.get(targetTabId); return targetTabId; } catch (_) {
      targetTabId = null;
      clearTransientSensitiveScreenshotBinding();
    }
  }
  try {
    const stored = await chrome.storage.local.get(TARGET_TAB_STORAGE_KEYS);
    const storedId = Number(stored && stored.lumenTargetTabId);
    if (stored && stored.lumenTargetOwned === true && Number.isFinite(storedId) && storedId > 0) {
      const tab = await chrome.tabs.get(storedId);
      if (tab) {
        targetTabId = storedId;
        if (!runTabIds.length) {
          const storedRunTabs = Array.isArray(stored.lumenRunTabIds)
            ? stored.lumenRunTabIds.map(Number).filter((id) => Number.isFinite(id) && id > 0)
            : [];
          runTabIds = Array.from(new Set([...storedRunTabs, storedId]));
        }
        return targetTabId;
      }
    }
  } catch (_) {
    targetTabId = null;
    clearTransientSensitiveScreenshotBinding();
  }
  // Never fall back to whichever tab happens to be active. That can be the Lumen cockpit (or any
  // unrelated user tab) and would let a stale run observe or mutate a page it does not own.
  targetTabId = null;
  clearTransientSensitiveScreenshotBinding();
  await chrome.storage.local.remove(TARGET_TAB_STORAGE_KEYS).catch(() => {});
  return null;
}

async function rememberPanelPayload(payload) {
  const data = payload && typeof payload === "object" ? payload : {};
  latestHudPayload = data;
  await chrome.storage.local.set({ [LAST_HUD_PAYLOAD_KEY]: data }).catch(() => {});
  try {
    await chrome.runtime.sendMessage({ type: "LUMEN_PANEL_UPDATE", payload: data });
  } catch (_) {
    // The side panel may not be open; storing the latest payload is sufficient.
  }
}

function cleanNotificationText(value, fallback, limit) {
  const text = String(value || "").replace(/\s+/g, " ").trim() || fallback;
  return text.length > limit ? text.slice(0, Math.max(limit - 3, 0)) + "..." : text;
}

function notificationKey(payload) {
  return [
    payload && payload.run_id,
    payload && payload.kind,
    payload && payload.phase,
    payload && payload.notification_title,
    payload && payload.notification_message,
  ].map((part) => String(part || "")).join("|");
}

function notificationIconUrl() {
  try { return chrome.runtime.getURL(NOTIFICATION_ICON); } catch (_) { return NOTIFICATION_ICON; }
}

async function pushAttentionNotification(payload) {
  if (!payload || !payload.notify) return;
  if (!chrome.notifications || typeof chrome.notifications.create !== "function") return;
  const key = notificationKey(payload);
  const now = Date.now();
  if (key && key === lastNotificationKey && now - lastNotificationAt < 30000) return;
  lastNotificationKey = key;
  lastNotificationAt = now;
  const id = `lumen-attention-${payload.run_id || "run"}-${now}`;
  const title = cleanNotificationText(payload.notification_title || payload.title, "Lumen needs attention.", 80);
  const message = cleanNotificationText(payload.notification_message || payload.detail || payload.title, "Open Lumen to continue.", 180);
  try {
    await chrome.notifications.create(id, {
      type: "basic",
      iconUrl: notificationIconUrl(),
      title,
      message,
      priority: payload.severity === "error" ? 2 : 1,
    });
  } catch (_) {
    // Notifications are an attention aid only; failures must not affect the browser run.
  }
}

async function postHudChatMessage(payload) {
  const content = String((payload && payload.content) || "").trim();
  const threadId = Number(payload && payload.thread_id);
  if (!content) return { ok: false, error: "Message is empty." };
  if (!Number.isFinite(threadId) || threadId <= 0) return { ok: false, error: "Lumen chat thread is not available for this browser session." };
  const taskIds = Array.from(new Set((Array.isArray(payload && payload.task_ids) ? payload.task_ids : [])
    .map((taskId) => Number(taskId))
    .filter((taskId) => Number.isFinite(taskId) && taskId > 0)));
  const requestedScope = String((payload && payload.save_scope) || "").trim().toLowerCase();
  const saveScope = requestedScope === "permanent" ? "permanent" : "one_time";
  const structuredAnswer = {};
  if (payload && payload.keep_proposed === true) structuredAnswer.keep_proposed = true;
  if (payload && payload.skip === true) structuredAnswer.skip = true;
  const selectedOption = String((payload && payload.selected_option) || "").trim();
  if (selectedOption) structuredAnswer.selected_option = selectedOption;
  // Multi-select (checkbox group) answer: exact option strings, display order, at most 20.
  const selectedOptions = Array.from(new Set((Array.isArray(payload && payload.selected_options) ? payload.selected_options : [])
    .filter((option) => typeof option === "string")
    .map((option) => option.trim())
    .filter(Boolean))).slice(0, 20);
  if (selectedOptions.length) structuredAnswer.selected_options = selectedOptions;
  if (taskIds.length) structuredAnswer.task_ids = taskIds;
  if (requestedScope) structuredAnswer.save_scope = saveScope;
  const { backend } = await getConfig();
  const response = await fetch(`${backend}/operator/chat/threads/${encodeURIComponent(String(threadId))}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({
      content,
      payload: {
        source: String(payload.source || ""),
        run_id: payload && payload.run_id != null ? payload.run_id : null,
        ...structuredAnswer,
      },
    }),
    cache: "no-store",
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    return { ok: false, error: detail || `Lumen chat rejected the message (${response.status}).` };
  }
  await response.text().catch(() => "");
  return { ok: true };
}

async function fetchHudChatMessages(payload) {
  const threadId = Number(payload && payload.thread_id);
  if (!Number.isFinite(threadId) || threadId <= 0) return { ok: false, error: "Lumen chat thread is not available for this browser session." };
  const runId = Number(payload && payload.run_id);
  const { backend } = await getConfig();
  const response = await fetch(`${backend}/operator/chat/threads/${encodeURIComponent(String(threadId))}/messages`, {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    return { ok: false, error: detail || `Lumen chat messages are unavailable (${response.status}).` };
  }
  const messages = await response.json().catch(() => []);
  let pendingQuestion = null;
  let pendingQuestionLoaded = false;
  if (Number.isFinite(runId) && runId > 0) {
    try {
      const questionsResponse = await fetch(`${backend}/operator/runs/${encodeURIComponent(String(runId))}/questions`, {
        method: "GET",
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (questionsResponse.ok) {
        pendingQuestionLoaded = true;
        const questions = await questionsResponse.json().catch(() => []);
        pendingQuestion = Array.isArray(questions)
          ? questions.find((question) => question && question.status === "pending") || null
          : null;
      }
    } catch (_) {
      // Transcript mirroring remains available when pending-question sync is transiently unavailable.
    }
  }
  return {
    ok: true,
    messages: Array.isArray(messages) ? messages : [],
    pending_question: pendingQuestion,
    pending_question_loaded: pendingQuestionLoaded,
  };
}

async function panelState() {
  const stored = await chrome.storage.local.get(["lumenConnected", LAST_HUD_PAYLOAD_KEY]);
  if (!latestHudPayload && stored[LAST_HUD_PAYLOAD_KEY]) {
    latestHudPayload = stored[LAST_HUD_PAYLOAD_KEY];
  }
  return {
    ok: true,
    connected: Boolean(stored.lumenConnected) && socket && socket.readyState === WebSocket.OPEN,
    payload: latestHudPayload || null,
  };
}

try {
  chrome.notifications.onClicked.addListener((notificationId) => {
    const taskPanelTarget = parseTaskPanelNotificationId(notificationId);
    if (taskPanelTarget) {
      // The notification click is a Chrome-owned user gesture. Invoke sidePanel.open before any
      // awaited tab/window work so Chrome's transient activation is still valid.
      const panelPromise = openSidePanel({ windowId: taskPanelTarget.windowId });
      Promise.resolve(panelPromise).then(async (result) => {
        try {
          if (result && result.ok) {
            await rememberPanelWindowState(taskPanelTarget.windowId, true);
          }
          const tab = await chrome.tabs.get(taskPanelTarget.tabId);
          if (tab && tab.windowId != null) await chrome.windows.update(tab.windowId, { focused: true });
          await chrome.tabs.update(taskPanelTarget.tabId, { active: true });
          chrome.notifications.clear(notificationId);
        } catch (_) { /* notification click focus is best-effort */ }
      });
      return;
    }
    if (!String(notificationId || "").startsWith("lumen-attention-")) return;
    // Attention notifications are also Chrome-owned user gestures. Open the global Lumen panel
    // synchronously, then focus the tracked task tab.
    const panelPromise = openSidePanel({});
    Promise.resolve(panelPromise).then(async () => {
      try {
        const tabId = await activeTabId();
        if (tabId) {
          const tab = await chrome.tabs.get(tabId);
          if (tab && tab.windowId != null) await chrome.windows.update(tab.windowId, { focused: true });
          await chrome.tabs.update(tabId, { active: true });
        }
        chrome.notifications.clear(notificationId);
      } catch (_) { /* notification click focus is best-effort */ }
    });
  });
} catch (_) { /* notifications API unavailable in this context */ }

async function dismissStrayNativeDialog(tabId) {
  // Lumen is leaving this tab: a browser dialog one of its earlier actions opened is answered
  // Cancel - the safe answer, never OK - so the tab is not left frozen and navigation can proceed.
  const target = { tabId: Number(tabId) };
  try {
    await withTimeout(acquireDebugger(target), 2500, "debugger attach timed out");
  } catch (_) {
    return false;
  }
  try {
    await _cdp(target, "Page.handleJavaScriptDialog", { accept: false });
    return true;
  } catch (_) {
    return false; // no dialog was showing
  } finally {
    nativeDialogs.delete(Number(tabId));
    releaseDebugger(target);
  }
}

async function runTabsEvidence() {
  const tabs = [];
  for (const id of runTabIds) {
    try {
      const tab = await chrome.tabs.get(id);
      if (!tab) continue;
      tabs.push({
        tab_id: String(id),
        title: String(tab.title || "").slice(0, 120),
        url: String(tab.url || tab.pendingUrl || "").slice(0, 300),
        active: Number(id) === Number(targetTabId),
      });
    } catch (_) {
      // closed: no longer part of the run
    }
  }
  runTabIds = runTabIds.filter((id) => tabs.some((tab) => Number(tab.tab_id) === Number(id)));
  persistRunTabIds();
  return tabs;
}

async function runVisionSwitchTab(target) {
  // Bring one of this run's own tabs to the front, like clicking its tab in the strip. Tab
  // shortcuts (Ctrl+Tab) are browser accelerators that page-level key events never reach.
  const tabId = Number(String(target || "").trim());
  if (!Number.isFinite(tabId) || !runTabIds.includes(tabId)) {
    return {
      ok: false,
      action: "switch_tab",
      failure_reason: "tab_not_in_run",
      error: "Only a tab listed in browser_tabs can be switched to.",
    };
  }
  const previousTabId = targetTabId;
  try {
    const tab = await chrome.tabs.update(tabId, { active: true });
    try { if (tab && tab.windowId != null) await chrome.windows.update(tab.windowId, { focused: true }); } catch (_) {}
    targetTabId = tabId;
    await chrome.storage.local.set({ lumenTargetTabId: targetTabId, lumenTargetOwned: true });
    clearTransientSensitiveScreenshotBinding();
    if (controlIndicatorState.active) {
      if (previousTabId != null && Number(previousTabId) !== tabId) {
        await applyControlIndicatorToTab(previousTabId, { active: false }).catch(() => {});
      }
      await applyControlIndicatorToTab(tabId).catch(() => {});
    }
    const current = await chrome.tabs.get(tabId);
    return {
      ok: true,
      action: "switch_tab",
      switched: Number(previousTabId) !== tabId,
      tab_id: String(tabId),
      url: String((current && current.url) || ""),
      title: String((current && current.title) || ""),
      url_changed: Number(previousTabId) !== tabId,
    };
  } catch (error) {
    return { ok: false, action: "switch_tab", failure_reason: "tab_switch_failed", error: errorMessage(error) };
  }
}

async function doNavigate(params) {
  const openInNewTab = params.new_tab !== false;
  const leavingTabId = await activeTabId();
  if (leavingTabId) await dismissStrayNativeDialog(leavingTabId);
  const previousTabId = openInNewTab ? leavingTabId : null;
  if (previousTabId && controlIndicatorState.active) {
    await applyControlIndicatorToTab(previousTabId, { active: false }).catch(() => {});
  }
  const tabId = await ensureTab(params.url, { newTab: openInNewTab });
  runTabIds = [Number(tabId)];
  persistRunTabIds();
  await waitForLoad(tabId);
  const tab = await chrome.tabs.get(tabId);
  const controlIndicator = controlIndicatorState.active
    ? await applyControlIndicatorToTab(tabId)
    : { ok: true, visible: false };
  if (controlIndicatorState.active && (!controlIndicator.ok || !controlIndicator.visible)) {
    return {
      ok: false,
      error: "Lumen could not display its active-control indicator on the task tab.",
      failure_reason: controlIndicator.failure_reason || "control_indicator_unavailable",
      tab_id: tabId,
    };
  }
  const panelNotice = openInNewTab
    ? await pushTaskPanelNotification(tab)
    : { notified: false, reason: "existing_task_tab" };
  clearTransientSensitiveScreenshotBinding();
  return {
    ok: true,
    url: tab.url,
    title: tab.title,
    tab_id: tabId,
    opened_new_tab: openInNewTab,
    panel_notice: panelNotice,
    control_indicator_visible: Boolean(controlIndicator.visible),
  };
}

async function doObserve() {
  const tabId = await activeTabId();
  if (!tabId) return { ok: false, error: "No application tab to observe." };
  if (nativeDialogOpenFor(tabId)) {
    return {
      ok: false,
      error: "A browser dialog is open over the page.",
      native_dialog: publicNativeDialog(nativeDialogOpenFor(tabId)),
    };
  }
  await chrome.scripting.executeScript({ target: { tabId }, files: ["src/observe.js"] });
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => globalThis.__lumenObserveFullPage(),
  });
  const observation = results && results[0] ? results[0].result : null;
  return observation ? { ok: true, ...observation, downloads: downloads.slice() } : { ok: false, error: "Perception returned nothing." };
}

async function doExecute(params) {
  const intent = params.intent || {};
  const tabId = await activeTabId();
  if (!tabId) return { ok: false, error: "No application tab to act on." };
  await chrome.scripting.executeScript({ target: { tabId }, files: ["src/observe.js"] });
  if (intent.action === "fill") {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: (selector, value) => globalThis.__lumenExecuteFill(selector, value),
      args: [intent.selector, intent.value],
    });
    return (results && results[0] && results[0].result) || { ok: false, error: "Fill returned nothing." };
  }
  if (intent.action === "select") {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: (selector, value) => globalThis.__lumenExecuteSelect(selector, value),
      args: [intent.selector, intent.value],
    });
    return (results && results[0] && results[0].result) || { ok: false, error: "Select returned nothing." };
  }
  if (intent.action === "upload") {
    return uploadFile(tabId, intent);
  }
  if (intent.action === "click") {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: (selector) => globalThis.__lumenExecuteClick(selector),
      args: [intent.selector],
    });
    await waitForLoad(tabId, 8000); // a click may navigate; let the next page settle
    return (results && results[0] && results[0].result) || { ok: false, error: "Click returned nothing." };
  }
  return { ok: false, error: `Unsupported intent action: ${intent.action}` };
}

// Upload fallback: a content script can't set a file <input> (browser security), so download the
// document to disk and hand the local path to the input via the debugger protocol (CDP
// DOM.setFileInputFiles). Needs the "downloads" + "debugger" permissions.
function _downloadToLocal(url, filename) {
  return new Promise((resolve) => {
    // Stage the file SILENTLY: saveAs:false suppresses the "Save As" file-chooser even when the user's
    // global "ask where to save each file" setting is on (that dialog blocks the run and gives a
    // non-deterministic path). Land it in an isolated subfolder so we never clobber the user's own
    // downloads. The file must stay on disk until the human submits the form (CDP setFileInputFiles
    // references the path, read at submit time), so we do NOT delete it here.
    const safeName = (filename || "lumen-resume.pdf").replace(/[\\/]+/g, "_");
    chrome.downloads.download({ url, filename: "lumen-upload/" + safeName, saveAs: false, conflictAction: "overwrite" }, (id) => {
      if (chrome.runtime.lastError || !id) return resolve(null);
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        try { chrome.downloads.onChanged.removeListener(onChanged); } catch (_) { /* already removed */ }
        resolve(value);
      };
      const onChanged = (delta) => {
        if (delta.id !== id || !delta.state) return;
        if (delta.state.current === "complete") {
          chrome.downloads.search({ id }, (rows) => finish(rows && rows[0] ? rows[0].filename : null));
        } else if (delta.state.current === "interrupted") {
          finish(null);
        }
      };
      chrome.downloads.onChanged.addListener(onChanged);
      // Don't hang the run if the download never reports a terminal state.
      setTimeout(() => finish(null), 20000);
    });
  });
}

function surfaceSignatureFromEvidence(pageEvidence) {
  try {
    const controls = Array.isArray(pageEvidence && pageEvidence.controls) ? pageEvidence.controls : [];
    const compactControls = controls.slice(0, 50).map((item) => ({
      tag: item.tag || "",
      role: item.role || "",
      type: item.type || "",
      text: item.text || "",
      value: item.value || "",
      checked: Boolean(item.checked),
      disabled: Boolean(item.disabled),
      x: item.x || 0,
      y: item.y || 0,
      width: item.width || 0,
      height: item.height || 0,
    }));
    const raw = JSON.stringify({
      viewport_text: String((pageEvidence && pageEvidence.viewport_text) || "").slice(0, 2500),
      controls: compactControls,
    });
    let hash = 2166136261;
    for (let i = 0; i < raw.length; i += 1) {
      hash ^= raw.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return `${hash >>> 0}:${raw.length}`;
  } catch (_) {
    return "";
  }
}

function stableSurfaceSignatureFromEvidence(pageEvidence) {
  try {
    const controls = Array.isArray(pageEvidence && pageEvidence.controls) ? pageEvidence.controls : [];
    const compactControls = controls.slice(0, 50).map((item) => ({
      selector: item.selector || "",
      tag: item.tag || "",
      role: item.role || "",
      type: item.type || "",
      text: item.text || "",
      value: item.value || "",
      checked: Boolean(item.checked),
      selected: Boolean(item.selected),
      aria_selected: item.aria_selected == null ? "" : String(item.aria_selected),
      aria_expanded: item.aria_expanded == null ? "" : String(item.aria_expanded),
      disabled: Boolean(item.disabled),
    }));
    const raw = JSON.stringify({
      viewport_text: String((pageEvidence && pageEvidence.viewport_text) || "").slice(0, 2500),
      controls: compactControls,
    });
    let hash = 2166136261;
    for (let i = 0; i < raw.length; i += 1) {
      hash ^= raw.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return `${hash >>> 0}:${raw.length}`;
  } catch (_) {
    return "";
  }
}

// Consecutive drift rejections across screenshot commands. A page whose surface never settles
// (tickers, re-rendering panels, live regions) would otherwise make every capture "stale";
// after repeated drift on the SAME document, one capture is accepted and flagged volatile.
let screenshotDriftStreak = 0;

function screenshotDriftTolerable(before, after, expectedMaskSurface) {
  const left = before && typeof before === "object" ? before : {};
  const right = after && typeof after === "object" ? after : {};
  const sameDocument = (
    String(left.url || "") === String(right.url || "")
    && (
      !left.document_lifecycle_token
      || !right.document_lifecycle_token
      || String(left.document_lifecycle_token) === String(right.document_lifecycle_token)
    )
  );
  if (!sameDocument || expectedMaskSurface) {
    return false;
  }
  screenshotDriftStreak += 1;
  return screenshotDriftStreak > 2;
}

function screenshotObservationContextMatches(before, after) {
  const left = before && typeof before === "object" ? before : {};
  const right = after && typeof after === "object" ? after : {};
  if (
    left.url
    && right.url
    && String(left.url) !== String(right.url)
  ) {
    return false;
  }
  const beforeDocumentLifecycle = String(left.document_lifecycle_token || "").trim();
  const afterDocumentLifecycle = String(right.document_lifecycle_token || "").trim();
  if (
    beforeDocumentLifecycle
    && afterDocumentLifecycle
    && beforeDocumentLifecycle !== afterDocumentLifecycle
  ) {
    return false;
  }
  const beforeStable = String(left.stable_surface_signature || "").trim();
  const afterStable = String(right.stable_surface_signature || "").trim();
  if (beforeStable && afterStable) {
    return beforeStable === afterStable;
  }
  const beforeDocument = String(left.document_state_signature || "").trim();
  const afterDocument = String(right.document_state_signature || "").trim();
  if (beforeDocument && afterDocument) {
    return beforeDocument === afterDocument;
  }
  const beforeSurface = String(left.surface_signature || "").trim();
  const afterSurface = String(right.surface_signature || "").trim();
  return !beforeSurface || !afterSurface || beforeSurface === afterSurface;
}

async function sensitiveMaskSurfaceSignatureFromEvidence(pageEvidence) {
  try {
    const controls = Array.isArray(pageEvidence && pageEvidence.controls) ? pageEvidence.controls : [];
    // The evidence was already rendered with every nominated control value redacted in-page.
    // Retain raw control structure/order plus rendered context only for those redacted controls.
    // No control value, whole-viewport text, geometry, or enabled/checked state contributes to the
    // durable identity, so direct entry and unrelated form values cannot create persisted hashes.
    const structuralControls = controls.slice(0, 80).map((item) => ({
      selector: item.selector || "",
      tag: item.tag || "",
      role: item.role || "",
      type: item.type || "",
      id: item.id || "",
      name: item.name || "",
      aria_controls: item.aria_controls || "",
      aria_owns: item.aria_owns || "",
      aria_labelledby: item.aria_labelledby || "",
      aria_describedby: item.aria_describedby || "",
      autocomplete: item.autocomplete || "",
      value_redacted: Boolean(item.value_redacted),
      redacted_control_context: item.value_redacted
        ? {
          text: item.text || "",
          nearby_text: item.nearby_text || "",
          context_text: item.context_text || "",
          accessibility_context: item.accessibility_context || [],
          dom_context: item.dom_context || [],
        }
        : null,
    }));
    const raw = JSON.stringify({
      control_count: controls.length,
      controls: structuralControls,
    });
    if (
      !globalThis.crypto
      || !globalThis.crypto.subtle
      || typeof globalThis.crypto.subtle.digest !== "function"
      || typeof TextEncoder !== "function"
    ) {
      return "";
    }
    const digest = await globalThis.crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(raw),
    );
    const hex = Array.from(new Uint8Array(digest))
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
    return hex ? `sha256:${hex}` : "";
  } catch (_) {
    return "";
  }
}

async function captureTabLineage(tabId) {
  try {
    const rows = await chrome.tabs.query({});
    return {
      source_tab_id: Number(tabId),
      before_tab_ids: (rows || []).map((tab) => Number(tab && tab.id)).filter((id) => Number.isFinite(id) && id > 0),
    };
  } catch (_) {
    return { source_tab_id: Number(tabId), before_tab_ids: [] };
  }
}

async function adoptNewTabAfterClick(tabId, lineage) {
  try {
    const beforeIds = new Set(Array.isArray(lineage && lineage.before_tab_ids) ? lineage.before_tab_ids.map(Number) : []);
    if (!beforeIds.size || !beforeIds.has(Number(tabId))) return { tabId, adopted: false };
    const rows = await chrome.tabs.query({});
    const candidates = (rows || []).filter((tab) => {
      const id = Number(tab && tab.id);
      if (!Number.isFinite(id) || id <= 0 || id === Number(tabId) || beforeIds.has(id)) return false;
      const url = String((tab && (tab.url || tab.pendingUrl)) || "");
      return !/^(chrome|edge|about|devtools):/i.test(url);
    });
    const directChildren = candidates.filter((tab) => Number(tab && tab.openerTabId) === Number(tabId));
    // A new tab Chrome says another tab opened is the user's, not this click's, even when it is
    // the only new tab. Only an opener-less tab (window.open with noopener) may stand in for a child.
    const openerless = candidates.filter((tab) => !(tab && Number.isFinite(Number(tab.openerTabId)) && Number(tab.openerTabId) > 0));
    const adopted = directChildren.length === 1
      ? directChildren[0]
      : (directChildren.length === 0 && candidates.length === 1 && openerless.length === 1 ? openerless[0] : null);
    if (!adopted) return { tabId, adopted: false };
    const url = String(adopted.url || adopted.pendingUrl || "");
    clearTransientSensitiveScreenshotBinding();
    const controlIndicatorActive = controlIndicatorState.active;
    if (controlIndicatorActive) {
      await applyControlIndicatorToTab(tabId, { active: false }).catch(() => {});
    }
    targetTabId = adopted.id;
    if (!runTabIds.includes(Number(tabId))) runTabIds.push(Number(tabId));
    if (!runTabIds.includes(Number(adopted.id))) runTabIds.push(Number(adopted.id));
    persistRunTabIds();
    await chrome.storage.local.set({ lumenTargetTabId: targetTabId, lumenTargetOwned: true });
    const controlIndicator = controlIndicatorActive
      ? await applyControlIndicatorToTab(targetTabId)
      : { ok: true, visible: false };
    return {
      tabId: adopted.id,
      adopted: true,
      adopted_url: url,
      adopted_title: String(adopted.title || ""),
      ...(controlIndicatorActive
        ? { control_indicator_visible: Boolean(controlIndicator.visible) }
        : {}),
    };
  } catch (_) {
    return { tabId, adopted: false };
  }
}

// One debugger session per tab while Lumen is acting. Chrome shows its "started debugging this
// browser" bar while an extension is attached, and the bar shortens the page viewport. Attaching
// and detaching around every primitive made the bar flicker, so an observation and the action
// chosen from it could see viewports 58px apart: the coordinate pixel check refused the action,
// or a double-click landed a line off. The session stays attached across actions and detaches
// after an idle period or when the run releases control.
const DEBUGGER_IDLE_DETACH_MS = 45000;
const debuggerSessions = new Map();

// Browser-drawn JavaScript dialogs (alert/confirm/prompt/beforeunload). They freeze the page's
// renderer: in-page scripts and input dispatch block until the dialog is answered, and the
// dialog is not part of the page's pixels. The session's Page domain reports each dialog; the
// body surfaces it as observation evidence and answers it only on the model's key/type action.
const nativeDialogs = new Map();
const lastObservationByTab = new Map();

function publicNativeDialog(dialog) {
  if (!dialog) return null;
  return {
    type: String(dialog.type || ""),
    message: String(dialog.message || ""),
    default_prompt: String(dialog.default_prompt || ""),
    typed_prompt: dialog.prompt_text == null ? null : String(dialog.prompt_text),
    url: String(dialog.url || ""),
  };
}

try {
  chrome.debugger.onEvent.addListener((source, method, params) => {
    const tabId = Number(source && source.tabId);
    if (!Number.isFinite(tabId)) return;
    if (method === "Page.javascriptDialogOpening") {
      nativeDialogs.set(tabId, {
        type: String((params && params.type) || "alert"),
        message: String((params && params.message) || "").slice(0, 600),
        default_prompt: String((params && params.defaultPrompt) || "").slice(0, 200),
        url: String((params && params.url) || "").slice(0, 500),
        prompt_text: null,
        opened_at: Date.now(),
      });
    } else if (method === "Page.javascriptDialogClosed") {
      nativeDialogs.delete(tabId);
    }
  });
} catch (_) {}

function nativeDialogOpenFor(tabId) {
  return nativeDialogs.get(Number(tabId)) || null;
}

async function dispatchInputUnlessDialog(target, method, params) {
  // An input event whose handler calls alert/confirm/prompt does not return until the dialog is
  // answered; the dialog opening is that event's outcome, so stop waiting as soon as it opens.
  const tabId = Number(target && target.tabId);
  let finished = false;
  const dispatched = _cdp(target, method, params).then(
    () => "dispatched",
    (error) => {
      if (nativeDialogOpenFor(tabId)) return "dialog";
      throw error;
    },
  );
  dispatched.catch(() => {});
  const dialogOpened = new Promise((resolve) => {
    const tick = () => {
      if (finished) return;
      if (nativeDialogOpenFor(tabId)) return resolve("dialog");
      setTimeout(tick, 25);
    };
    tick();
  });
  try {
    return await Promise.race([dispatched, dialogOpened]);
  } finally {
    finished = true;
  }
}

async function answerNativeDialog(tabId, key) {
  // Enter answers OK (with the typed prompt text), Escape answers Cancel - what the keys do in
  // the browser's own dialog. Any other key cannot reach the page behind it.
  const dialog = nativeDialogOpenFor(tabId);
  const normalized = String(key || "").trim().toLowerCase();
  const accept = normalized === "enter" || normalized === "return";
  const dismiss = normalized === "escape" || normalized === "esc";
  if (!dialog) return { ok: false, action: "key", failure_reason: "native_dialog_absent" };
  if (!accept && !dismiss) {
    return {
      ok: false,
      action: "key",
      mode: "native_dialog",
      failure_reason: "native_dialog_open",
      native_dialog: publicNativeDialog(dialog),
      error: "A browser dialog is open; only Enter (OK), Escape (Cancel), or typing into a prompt reach it.",
    };
  }
  const target = { tabId: Number(tabId) };
  let acquired = false;
  try {
    await acquireDebugger(target);
    acquired = true;
    const params = { accept };
    if (accept && dialog.type === "prompt") {
      params.promptText = String(dialog.prompt_text == null ? dialog.default_prompt || "" : dialog.prompt_text);
    }
    await _cdp(target, "Page.handleJavaScriptDialog", params);
    nativeDialogs.delete(Number(tabId));
  } catch (error) {
    return {
      ok: false,
      action: "key",
      mode: "native_dialog",
      failure_reason: "native_dialog_answer_failed",
      native_dialog: publicNativeDialog(dialog),
      error: errorMessage(error),
    };
  } finally {
    if (acquired) releaseDebugger(target);
  }
  try {
    await withTimeout(
      chrome.scripting.executeScript({ target: { tabId: Number(tabId) }, func: domQuietInPage, args: [200, 1000] }),
      1400,
      "post-dialog settle timed out",
    );
  } catch (_) {}
  return {
    ok: true,
    action: "key",
    mode: "native_dialog",
    key: accept ? "Enter" : "Escape",
    trusted_key_attempted: true,
    trusted_key_ok: true,
    native_dialog_answered: { type: dialog.type, message: dialog.message, accepted: accept },
    // The browser's dialog closed and the page resumed: a visible change by construction.
    surface_changed: true,
  };
}

async function acquireDebugger(target) {
  const tabId = Number(target && target.tabId);
  const session = debuggerSessions.get(tabId);
  if (session) {
    if (session.detachTimer) clearTimeout(session.detachTimer);
    session.detachTimer = null;
    return { attached_now: false };
  }
  let attachedNow = true;
  await new Promise((resolve, reject) => chrome.debugger.attach({ tabId }, "1.3", () => {
    const error = chrome.runtime.lastError;
    if (error && /already attached/i.test(String(error.message || ""))) {
      // A session from before a service-worker restart is still ours.
      attachedNow = false;
      resolve();
    } else if (error) {
      reject(new Error(error.message));
    } else {
      resolve();
    }
  }));
  debuggerSessions.set(tabId, { detachTimer: null });
  // Dialog events flow only with the Page domain on.
  try {
    await _cdp({ tabId }, "Page.enable", {});
  } catch (error) {
    if (!attachedNow && /not attached/i.test(errorMessage(error))) {
      // "Already attached" came from another client (DevTools, another extension), not a session of
      // ours that outlived a service-worker restart. Adopting it would fail every later command.
      debuggerSessions.delete(tabId);
      throw new Error("Another debugger is attached to this tab.");
    }
  }
  return { attached_now: attachedNow };
}

function releaseDebugger(target) {
  const tabId = Number(target && target.tabId);
  const session = debuggerSessions.get(tabId);
  if (!session) return;
  if (session.detachTimer) clearTimeout(session.detachTimer);
  session.detachTimer = setTimeout(() => { void idleDetachDebugger(tabId); }, DEBUGGER_IDLE_DETACH_MS);
}

async function idleDetachDebugger(tabId) {
  // A run still holding control keeps its session even when the model thinks for longer than the
  // idle window: detaching drops the debugging bar and grows the viewport between the observation
  // and the action chosen from it. Releasing control detaches every session; a lapsed lease (the
  // backend went away) lets this timer detach as before.
  const session = debuggerSessions.get(Number(tabId));
  if (session && controlIndicatorState.active && Number(controlIndicatorState.expires_at || 0) > Date.now()) {
    session.detachTimer = setTimeout(() => { void idleDetachDebugger(tabId); }, DEBUGGER_IDLE_DETACH_MS);
    return;
  }
  await detachDebuggerNow(tabId);
}

async function detachDebuggerNow(tabId) {
  const key = Number(tabId);
  const session = debuggerSessions.get(key);
  if (session && session.detachTimer) clearTimeout(session.detachTimer);
  debuggerSessions.delete(key);
  await new Promise((resolve) => chrome.debugger.detach({ tabId: key }, () => {
    void chrome.runtime.lastError;
    resolve();
  }));
}

async function detachAllDebuggerSessions() {
  await Promise.all(Array.from(debuggerSessions.keys()).map((tabId) => detachDebuggerNow(tabId)));
}

try {
  chrome.debugger.onDetach.addListener((source) => {
    // The user dismissed the bar, the tab closed, or DevTools took over: forget the session so
    // the next primitive attaches again.
    const key = Number(source && source.tabId);
    const session = debuggerSessions.get(key);
    if (session && session.detachTimer) clearTimeout(session.detachTimer);
    debuggerSessions.delete(key);
  });
} catch (_) {}

function _cdp(target, method, params) {
  return withTimeout(new Promise((resolve, reject) => {
    chrome.debugger.sendCommand(target, method, params || {}, (res) => {
      if (chrome.runtime.lastError) {
        const message = String(chrome.runtime.lastError.message || "");
        if (/not attached/i.test(message)) {
          // Our session is gone; forget it so the next primitive attaches again.
          const key = Number(target && target.tabId);
          const stale = debuggerSessions.get(key);
          if (stale && stale.detachTimer) clearTimeout(stale.detachTimer);
          debuggerSessions.delete(key);
        }
        return reject(new Error(message));
      }
      resolve(res);
    });
  }), 2500, `${method} timed out`);
}

// In-page injection (runs in the page): reconstruct the approved document as a real File and hand
// it to the input via a DataTransfer + change/drop events — exactly what selecting it in
// the file dialog would do, but with no disk download and no "Save As". Returns whether the input
// actually accepted the file so the caller can fall back when a site rejects synthetic Files.
function _injectFileIntoInput(selector, base64, filename, mime) {
  try {
    const queryAll = typeof globalThis.__lumenDeepQueryAll === "function"
      ? globalThis.__lumenDeepQueryAll
      : (value) => Array.from(document.querySelectorAll(value));
    const deepDescendants = typeof globalThis.__lumenDeepDescendants === "function"
      ? globalThis.__lumenDeepDescendants
      : (node, value) => (
        node && node.querySelectorAll ? Array.from(node.querySelectorAll(value)) : []
      );
    const matches = queryAll(selector);
    const el = matches.length === 1 ? matches[0] : null;
    if (!el) return { ok: false, error: "Upload target not found." };
    const bin = atob(base64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
    const file = new File([bytes], filename, { type: mime });
    const dt = new DataTransfer();
    dt.items.add(file);
    const input = (el.tagName || "").toLowerCase() === "input"
      ? el
      : deepDescendants(el, 'input[type="file"]')[0];
    if (input) {
      try { input.files = dt.files; } catch (e) { /* some inputs disallow direct assignment */ }
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }
    // Drop-zones (e.g. "Drop or select") listen for a drop with files rather than an <input> change.
    try { el.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: dt })); } catch (e) { /* DragEvent dataTransfer may be ignored */ }
    return { ok: true, value_present: input ? Boolean(input.files && input.files.length) : true };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

function _arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  return btoa(binary);
}

function _uploadPageEvidence(selector, filename) {
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const queryAll = typeof globalThis.__lumenDeepQueryAll === "function"
    ? globalThis.__lumenDeepQueryAll
    : (value) => Array.from(document.querySelectorAll(value));
  const deepDescendants = typeof globalThis.__lumenDeepDescendants === "function"
    ? globalThis.__lumenDeepDescendants
    : (node, value) => (
      node && node.querySelectorAll ? Array.from(node.querySelectorAll(value)) : []
    );
  const wanted = clean(filename);
  const bodyText = clean([
    (document.body && document.body.innerText) || "",
    ...queryAll("*").map((node) => {
      const tag = (node.tagName || "").toLowerCase();
      if (["script", "style", "noscript", "template"].includes(tag)) return "";
      return Array.from(node.childNodes || [])
        .filter((child) => child && child.nodeType === Node.TEXT_NODE)
        .map((child) => child.textContent || "")
        .join(" ");
    }),
  ].join(" "));
  const uploadSuccessTextVisible = /\b(?:upload|parsing|parse)\s+(?:was\s+)?successful\b/i.test(bodyText)
    || /\bsuccessfully\s+(?:uploaded|attached|parsed)\b/i.test(bodyText)
    || /\b(?:resume|résumé|cv|file|document)\b.{0,90}\b(?:uploaded|attached|selected|accepted|parsed|parsing\s+was\s+successful)\b/i.test(bodyText);
  let fileInputHasFilename = false;
  try {
    const matches = selector ? queryAll(selector) : [];
    const target = matches.length === 1 ? matches[0] : null;
    const input = target && (
      (target.tagName || "").toLowerCase() === "input"
        ? target
        : deepDescendants(target, 'input[type="file"]')[0]
    );
    fileInputHasFilename = Boolean(input && input.files && Array.from(input.files).some((file) => clean(file.name) === wanted));
  } catch (error) {
    fileInputHasFilename = false;
  }
  const pageFilenameVisible = Boolean(wanted && bodyText.includes(wanted));
  return {
    file_input_files_contains_filename: fileInputHasFilename,
    page_filename_visible: pageFilenameVisible,
    page_chip_visible: pageFilenameVisible,
    upload_success_text_visible: uploadSuccessTextVisible,
    observed_present: fileInputHasFilename || pageFilenameVisible || uploadSuccessTextVisible,
  };
}

function _uploadEvidencePresent(evidence) {
  return Boolean(evidence && (evidence.file_input_files_contains_filename || evidence.page_filename_visible || evidence.page_chip_visible || evidence.upload_success_text_visible || evidence.observed_present));
}

async function uploadFile(tabId, intent) {
  const { backend: base } = await getConfig();
  const url = intent.download_path ? base + intent.download_path : "";
  if (!url) return { ok: false, error: "No document file available to upload." };

  // PRIMARY: inject the approved document DIRECTLY into the page — fetch its bytes from
  // the backend (the background has the host permission) and reconstruct it as a File in the page. No
  // download to the user's disk, no Save As dialog. Falls through to the CDP path on any failure.
  let injectionFailure = "";
  try {
    const resp = await fetch(url);
    if (resp.ok) {
      const base64 = _arrayBufferToBase64(await resp.arrayBuffer());
      const mime = resp.headers.get("content-type") || "application/pdf";
      const results = await chrome.scripting.executeScript({
        target: { tabId },
        func: _injectFileIntoInput,
        args: [intent.selector, base64, intent.filename || "document.pdf", mime],
      });
      const r = results && results[0] && results[0].result;
      if (r && r.ok) {
        await new Promise((resolve) => setTimeout(resolve, 450));
        const evidenceRows = await chrome.scripting.executeScript({
          target: { tabId },
          func: _uploadPageEvidence,
          args: [intent.selector, intent.filename || "document.pdf"],
        });
        const evidence = (evidenceRows && evidenceRows[0] && evidenceRows[0].result) || {};
        if (r.value_present || _uploadEvidencePresent(evidence)) {
          return { ok: true, action: "upload", selector: intent.selector, target_label: intent.target_label || "", upload_target_label: intent.target_label || "", value_present: true, observed_present: true, filename: intent.filename, mode: "in_page_injection", staged_file_exists: false, cdp_set_files_ok: false, ...evidence };
        }
        injectionFailure = "in_page_injection_unverified";
      } else if (r && r.error) {
        injectionFailure = "in_page_injection_failed: " + String(r.error);
      }
    } else {
      injectionFailure = "document_fetch_failed: HTTP " + resp.status;
    }
  } catch (error) {
    injectionFailure = "document_fetch_or_injection_exception: " + errorMessage(error);
  }

  // FALLBACK: attach a file on disk via the debugger (works for inputs that reject a synthetic File). The backend runs
  // on this computer and names its own copy (local_path), so nothing is downloaded: a staging download shows Chrome's
  // "Save As" dialog when the user's Chrome asks where to save each file, even with saveAs:false (run 72 on Paycom).
  const rawLocalPath = String(intent.local_path || "");
  const givenPath = rawLocalPath.startsWith("/") || /^[A-Za-z]:[\\/]/.test(rawLocalPath) ? rawLocalPath : "";
  const localPath = givenPath || await _downloadToLocal(url, intent.filename);
  if (!localPath) return { ok: false, error: "Could not stage the document file to upload.", failure_reason: injectionFailure || "download_staging_failed" };
  const target = { tabId };
  try {
    await acquireDebugger(target);
  } catch (error) {
    return { ok: false, error: "Could not attach debugger for upload: " + String(error) };
  }
  try {
    const doc = await _cdp(target, "DOM.getDocument", { depth: 0 });
    const found = await _cdp(target, "DOM.querySelector", { nodeId: doc.root.nodeId, selector: intent.selector });
    if (!found || !found.nodeId) return { ok: false, error: "File input not found for upload.", failure_reason: "file_input_not_found" };
    await _cdp(target, "DOM.setFileInputFiles", { files: [localPath], nodeId: found.nodeId });
    await new Promise((resolve) => setTimeout(resolve, 450));
    const evidenceRows = await chrome.scripting.executeScript({
      target: { tabId },
      func: _uploadPageEvidence,
      args: [intent.selector, intent.filename || "document.pdf"],
    });
    const evidence = (evidenceRows && evidenceRows[0] && evidenceRows[0].result) || {};
    return { ok: true, action: "upload", selector: intent.selector, target_label: intent.target_label || "", upload_target_label: intent.target_label || "", value_present: true, filename: intent.filename, mode: "cdp_set_file", staged_file_exists: true, cdp_set_files_ok: true, local_path_used: Boolean(givenPath), ...evidence };
  } catch (error) {
    return { ok: false, error: "Upload via debugger failed: " + errorMessage(error), failure_reason: "cdp_set_file_failed: " + errorMessage(error) };
  } finally {
    try {
      releaseDebugger(target);
    } catch (_) {
      /* already detached */
    }
  }
}

// The page state the vision loop needs to map the model's screenshot pixels back to the page
// (viewport size, device-pixel-ratio, scroll position).
function _pageState() {
  // Where a few visible controls sit right now (rounded top-left, document order): boxes in the
  // observation are measured at one instant and the screenshot is taken a little later, so a scroll,
  // a smooth-scroll tail, or a layout shift between the two puts every box off the picture by the
  // distance moved. Comparing these anchors before and after the capture detects it.
  const anchors = [];
  try {
    const nodes = document.querySelectorAll("button,input,select,textarea,a[href],[role='button']");
    for (let i = 0; i < nodes.length && anchors.length < 6; i += 1) {
      const box = nodes[i].getBoundingClientRect();
      if (box.width <= 0 || box.height <= 0 || box.bottom < 0 || box.top > window.innerHeight) continue;
      anchors.push([i, Math.round(box.top), Math.round(box.left)]);
    }
  } catch (_) {}
  return {
    url: location.href,
    title: document.title,
    scroll_y: Math.round(window.scrollY),
    scroll_x: Math.round(window.scrollX),
    scroll_height: Math.round(document.documentElement.scrollHeight),
    scroll_width: Math.round(document.documentElement.scrollWidth),
    inner_w: window.innerWidth,
    inner_h: window.innerHeight,
    dpr: window.devicePixelRatio || 1,
    geometry_anchors: anchors,
  };
}

// True when the page moved between two probes: the scroll position changed, or a control that was
// visible at both moments sits somewhere else. Missing evidence on either side is not movement.
function pageGeometryMoved(before, after) {
  const left = before && typeof before === "object" ? before : {};
  const right = after && typeof after === "object" ? after : {};
  for (const key of ["scroll_y", "scroll_x"]) {
    if (left[key] != null && right[key] != null && Math.abs(Number(left[key]) - Number(right[key])) > 1) return true;
  }
  const first = Array.isArray(left.geometry_anchors) ? left.geometry_anchors : [];
  const second = new Map((Array.isArray(right.geometry_anchors) ? right.geometry_anchors : []).map((row) => [row[0], row]));
  for (const row of first) {
    const other = second.get(row[0]);
    if (other && (Math.abs(other[1] - row[1]) > 2 || Math.abs(other[2] - row[2]) > 2)) return true;
  }
  return false;
}

function _visionPageEvidence(modelClassifiedSensitiveSelectors = [], options = null) {
  // options.document_text: return only the whole document's rendered text (read_run_tab_text),
  // redacted by the same rules as the viewport text, instead of the screenshot's evidence.
  const documentTextMode = Boolean(options && typeof options === "object" && options.document_text === true);
  // options.form_outline: every form control of the document (read_form), on screen or not; see formOutlineRows below.
  const formOutlineMode = Boolean(options && typeof options === "object" && options.form_outline === true);
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const documentLifecycleToken = (() => {
    try {
      let token = clean(globalThis.__lumenDocumentLifecycleToken);
      if (!token) {
        token = (
          globalThis.crypto && typeof globalThis.crypto.randomUUID === "function"
            ? globalThis.crypto.randomUUID()
            : `${Math.round(Number(performance.timeOrigin || Date.now()) * 1000)}-${Math.random().toString(36).slice(2)}`
        );
        Object.defineProperty(globalThis, "__lumenDocumentLifecycleToken", {
          value: token,
          configurable: false,
          enumerable: false,
          writable: false,
        });
      }
      return token;
    } catch (_) {
      return `${Math.round(Number(performance.timeOrigin || 0) * 1000)}`;
    }
  })();
  const SHADOW_DELIMITER = " >>> ";
  // A same-origin frame joins the composed tree the way an open shadow root does: the frame
  // element is the host and its document is the root. Cross-origin frames stay opaque.
  const frameDocument = (node) => {
    try {
      const tag = String((node && node.tagName) || "").toLowerCase();
      if (tag !== "iframe" && tag !== "frame") return null;
      const doc = node.contentDocument;
      return doc && doc.documentElement ? doc : null;
    } catch (_) {
      return null;
    }
  };
  const innerRoot = (node) => (node && node.shadowRoot) || frameDocument(node);
  const hostOfRoot = (root) => {
    if (!root) return null;
    if (root.host) return root.host;
    try {
      if (root.nodeType === 9 && root !== document) {
        return (root.defaultView && root.defaultView.frameElement) || null;
      }
    } catch (_) {}
    return null;
  };
  // Where a frame's content box starts, in its owner document's viewport pixels.
  const frameContentOffset = (frameEl) => {
    const rect = frameEl.getBoundingClientRect();
    let padLeft = 0;
    let padTop = 0;
    try {
      const style = (frameEl.ownerDocument.defaultView || window).getComputedStyle(frameEl);
      padLeft = parseFloat(style.paddingLeft) || 0;
      padTop = parseFloat(style.paddingTop) || 0;
    } catch (_) {}
    return {
      left: rect.left + (frameEl.clientLeft || 0) + padLeft,
      top: rect.top + (frameEl.clientTop || 0) + padTop,
    };
  };
  // A node's box in top-level viewport pixels (a frame's getBoundingClientRect is frame-local).
  const topRect = (node) => {
    const rect = node.getBoundingClientRect();
    let left = rect.left;
    let top = rect.top;
    let doc = node.ownerDocument;
    for (let hops = 0; doc && doc !== document && hops < 8; hops += 1) {
      let frameEl = null;
      try { frameEl = doc.defaultView && doc.defaultView.frameElement; } catch (_) { frameEl = null; }
      if (!frameEl) break;
      const offset = frameContentOffset(frameEl);
      left += offset.left;
      top += offset.top;
      doc = frameEl.ownerDocument;
    }
    return {
      left,
      top,
      right: left + rect.width,
      bottom: top + rect.height,
      width: rect.width,
      height: rect.height,
      x: left,
      y: top,
    };
  };
  // Where a node's document lives: {} for the top document, else {frame_origin, frame_depth}. The origin is the
  // browser's own origin for that frame document (window.origin, else location.origin): an about:srcdoc/blank frame
  // inherits its embedder's origin only when the browser says so, and a sandboxed frame reports "null" and stays
  // opaque. Anything unreadable is "null" too, never guessed and never inherited by this code.
  const frameOwnerFields = (node) => {
    try {
      let doc = node && node.ownerDocument;
      if (!doc || doc === document) return {};
      const view = doc.defaultView;
      let origin = "null";
      try {
        const reported = String((view && (view.origin || (view.location && view.location.origin))) || "");
        if (/^https?:\/\/[^/]+$/i.test(reported)) origin = reported;
      } catch (_) {}
      let depth = 0;
      for (let hops = 0; doc && doc !== document && hops < 8; hops += 1) {
        const owner = doc.defaultView && doc.defaultView.frameElement;
        if (!owner) break;
        depth += 1;
        doc = owner.ownerDocument;
      }
      return { frame_origin: origin, frame_depth: depth };
    } catch (_) {
      return { frame_origin: "null", frame_depth: 1 };
    }
  };
  const composedParent = (node) => {
    if (!node) return null;
    if (node.assignedSlot) return node.assignedSlot;
    if (node.parentElement) return node.parentElement;
    try {
      const root = node.getRootNode && node.getRootNode();
      return hostOfRoot(root);
    } catch (_) {
      return null;
    }
  };
  const composedContains = (ancestor, node) => {
    if (!ancestor || !node) return false;
    let current = node;
    const seen = new Set();
    while (current && !seen.has(current)) {
      if (current === ancestor) return true;
      seen.add(current);
      current = composedParent(current);
    }
    return false;
  };
  const openRoots = () => {
    const roots = [document];
    for (let index = 0; index < roots.length; index += 1) {
      const root = roots[index];
      let descendants = [];
      try { descendants = Array.from(root.querySelectorAll("*")); } catch (_) { descendants = []; }
      for (const node of descendants) {
        const inner = innerRoot(node);
        if (inner && !roots.includes(inner)) roots.push(inner);
      }
    }
    return roots;
  };
  const queryAllComposed = (rawSelector) => {
    const selector = String(rawSelector || "").trim();
    if (!selector) return [];
    if (selector.includes(SHADOW_DELIMITER)) {
      const segments = selector.split(SHADOW_DELIMITER).map((part) => part.trim()).filter(Boolean);
      if (!segments.length) return [];
      let roots = [document];
      let matches = [];
      for (let index = 0; index < segments.length; index += 1) {
        matches = [];
        for (const root of roots) {
          try { matches.push(...Array.from(root.querySelectorAll(segments[index]))); } catch (_) { return []; }
        }
        if (index < segments.length - 1) {
          roots = matches.map((node) => innerRoot(node)).filter(Boolean);
          if (!roots.length) return [];
        }
      }
      return matches;
    }
    const matches = [];
    for (const root of openRoots()) {
      try { matches.push(...Array.from(root.querySelectorAll(selector))); } catch (_) { return []; }
    }
    return matches;
  };
  const deepElementFromPoint = (x, y) => {
    let node = null;
    let px = x;
    let py = y;
    try { node = document.elementFromPoint(px, py); } catch (_) { node = null; }
    const seen = new Set();
    while (node && !seen.has(node)) {
      seen.add(node);
      let inner = null;
      if (node.shadowRoot) {
        try { inner = node.shadowRoot.elementFromPoint(px, py); } catch (_) { inner = null; }
      } else {
        const doc = frameDocument(node);
        if (doc) {
          const offset = frameContentOffset(node);
          px -= offset.left;
          py -= offset.top;
          try { inner = doc.elementFromPoint(px, py); } catch (_) { inner = null; }
        }
      }
      if (!inner || inner === node) break;
      node = inner;
    }
    return node;
  };
  const deepActiveElement = () => {
    let node = document.activeElement;
    const seen = new Set();
    while (node && !seen.has(node)) {
      seen.add(node);
      let next = null;
      if (node.shadowRoot && node.shadowRoot.activeElement) {
        next = node.shadowRoot.activeElement;
      } else {
        const doc = frameDocument(node);
        const active = doc && doc.activeElement;
        if (active && active !== doc.body && active !== doc.documentElement) next = active;
      }
      if (!next) break;
      node = next;
    }
    return node;
  };
  const cssEscape = (value) => {
    try {
      if (window.CSS && typeof CSS.escape === "function") return CSS.escape(String(value));
    } catch (_) {}
    return String(value || "").replace(/[^a-zA-Z0-9_-]/g, (ch) => "\\" + ch);
  };
  const attrValue = (value) => String(value || "").replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  const localUniqueSelector = (root, selector, el) => {
    try {
      if (!selector || selector.length > 200) return "";
      const matches = Array.from(root.querySelectorAll(selector));
      return matches.length === 1 && matches[0] === el ? selector : "";
    } catch (_) {
      return "";
    }
  };
  const localStableSelector = (el, root) => {
    try {
      const tag = (el.tagName || "").toLowerCase();
      if (!tag || !root) return "";
      const id = el.getAttribute("id");
      if (id) {
        const selector = localUniqueSelector(root, `#${cssEscape(id)}`, el);
        if (selector) return selector;
      }
      for (const attr of ["data-testid", "data-test", "data-qa", "name", "aria-label"]) {
        const value = el.getAttribute(attr);
        if (!value) continue;
        const selector = localUniqueSelector(root, `${tag}[${attr}="${attrValue(value)}"]`, el);
        if (selector) return selector;
      }
      const parts = [];
      let node = el;
      while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 40) {
        const nodeTag = (node.tagName || "").toLowerCase();
        if (!nodeTag) break;
        let part = nodeTag;
        if (node.parentElement) {
          const siblings = Array.from(node.parentElement.children).filter((child) => child.tagName === node.tagName);
          if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
        }
        parts.unshift(part);
        const selector = localUniqueSelector(root, parts.join(" > "), el);
        if (selector) return selector;
        if (node.parentElement) node = node.parentElement;
        else break;
      }
    } catch (_) {}
    return "";
  };
  const stableSelector = (el) => {
    try {
      const root = el && el.getRootNode ? el.getRootNode() : document;
      const local = localStableSelector(el, root);
      if (!local) return "";
      if (root === document) return local;
      const host = hostOfRoot(root);
      if (!host) return "";
      const hostSelector = stableSelector(host);
      const composed = hostSelector ? `${hostSelector}${SHADOW_DELIMITER}${local}` : "";
      return composed.length <= 200 ? composed : "";
    } catch (_) {
      return "";
    }
  };
  const deepDescendants = (node, selector) => {
    if (!node) return [];
    const roots = [];
    if (innerRoot(node)) roots.push(innerRoot(node));
    if (node.querySelectorAll) roots.push(node);
    const matches = [];
    for (let index = 0; index < roots.length; index += 1) {
      const root = roots[index];
      try { matches.push(...Array.from(root.querySelectorAll(selector))); } catch (_) {}
      let descendants = [];
      try { descendants = Array.from(root.querySelectorAll("*")); } catch (_) { descendants = []; }
      for (const child of descendants) {
        const inner = innerRoot(child);
        if (inner && !roots.includes(inner)) roots.push(inner);
      }
    }
    return matches;
  };
  try {
    Object.defineProperties(globalThis, {
      __lumenDeepQueryAll: { value: queryAllComposed, configurable: true, enumerable: false },
      __lumenDeepElementFromPoint: { value: deepElementFromPoint, configurable: true, enumerable: false },
      __lumenDeepActiveElement: { value: deepActiveElement, configurable: true, enumerable: false },
      __lumenStableDeepSelector: { value: stableSelector, configurable: true, enumerable: false },
      __lumenComposedParent: { value: composedParent, configurable: true, enumerable: false },
      __lumenComposedContains: { value: composedContains, configurable: true, enumerable: false },
      __lumenDeepDescendants: { value: deepDescendants, configurable: true, enumerable: false },
      __lumenTopRect: { value: topRect, configurable: true, enumerable: false },
    });
  } catch (_) {}
  const semanticSelectors = Array.isArray(modelClassifiedSensitiveSelectors)
    ? modelClassifiedSensitiveSelectors.map((selector) => String(selector || "").trim()).filter(Boolean)
    : [];
  let semanticSelectorError = "";
  for (const selector of semanticSelectors) {
    try {
      queryAllComposed(selector);
    } catch (error) {
      semanticSelectorError = String(error);
      break;
    }
  }
  const semanticSensitiveNodes = new Set(
    semanticSelectors.flatMap((selector) => queryAllComposed(selector)),
  );
  const structurallySensitiveValueControl = (el) => {
    try {
      const tag = (el.tagName || "").toLowerCase();
      const inputType = clean(el.getAttribute && el.getAttribute("type")).toLowerCase();
      if (tag === "input" && inputType === "password") return true;
      const autocompleteTokens = clean(el.getAttribute && el.getAttribute("autocomplete"))
        .toLowerCase()
        .split(/\s+/)
        .filter(Boolean);
      return autocompleteTokens.some((token) => (
        token === "current-password"
        || token === "new-password"
        || token === "one-time-code"
        || token === "transaction-amount"
        || token === "transaction-currency"
        || token === "webauthn"
        || token.startsWith("cc-")
      ));
    } catch (_) {
      return false;
    }
  };
  const modelClassifiedSensitiveValueControl = (el) => {
    if (!el || !semanticSelectors.length) return false;
    return semanticSensitiveNodes.has(el);
  };
  const sensitiveValueControl = (el) => (
    structurallySensitiveValueControl(el) || modelClassifiedSensitiveValueControl(el)
  );
  // The outline (form_outline) redacts with every control it masks: a select and a model-marked control of any tag
  // join the set, with each option's label and value, so their text never reaches a neighbour's question through the
  // ancestor text. The viewport and document-text paths keep the input-only set: widening it there would also drop
  // whole observation lines that happen to contain a short option label.
  const OWNED_CONTROL_SELECTOR = "input,textarea,[contenteditable='true'],[autocomplete]";
  const ownedControlCandidates = formOutlineMode
    ? [...new Set([...queryAllComposed(`${OWNED_CONTROL_SELECTOR},select`), ...semanticSensitiveNodes])]
    : queryAllComposed(OWNED_CONTROL_SELECTOR);
  const ownedOptionTextsOf = (node) => {
    try {
      const isSelect = (node.tagName || "").toLowerCase() === "select";
      const options = [...(isSelect ? Array.from(node.options || []) : []), ...(deepDescendants(node, "[role='option']") || [])];
      return options.flatMap((option) => [clean(option.label), clean(option.value), clean(option.textContent)]);
    } catch (_) {
      return [];
    }
  };
  const sensitiveOwnedTexts = ownedControlCandidates
    .filter(sensitiveValueControl)
    .flatMap((node) => [
      clean(node.value),
      clean(node.getAttribute && node.getAttribute("value")),
      clean(node.innerText),
      clean(node.textContent),
      ...(formOutlineMode ? ownedOptionTextsOf(node) : []),
    ])
    .filter(Boolean)
    .sort((a, b) => b.length - a.length);
  const redactSensitiveText = (value) => {
    let safe = clean(value);
    for (const secret of sensitiveOwnedTexts) {
      safe = clean(safe.split(secret).join(" [REDACTED] "));
    }
    return safe;
  };
  const nodeTouchesSensitiveValue = (node) => {
    try {
      const owner = node && node.closest
        ? node.closest("input,textarea,[contenteditable='true'],[autocomplete]")
        : null;
      if (owner && sensitiveValueControl(owner)) return true;
      return deepDescendants(
        node,
        "input,textarea,[contenteditable='true'],[autocomplete]",
      ).some(sensitiveValueControl);
    } catch (_) {
      return true;
    }
  };
  const visible = (el) => {
    try {
      const rect = topRect(el);
      const style = window.getComputedStyle(el);
      // On screen in both axes: a control parked thousands of pixels off the left edge is not visible.
      return rect.width > 0 && rect.height > 0
        && rect.bottom >= 0 && rect.top <= window.innerHeight
        && rect.right >= 0 && rect.left <= window.innerWidth
        && style.visibility !== "hidden" && style.display !== "none";
    } catch (_) {
      return false;
    }
  };
  const composedText = (node) => {
    const pieces = [];
    const seen = new Set();
    const visit = (current) => {
      if (!current || seen.has(current) || pieces.join(" ").length >= 700) return;
      seen.add(current);
      if (current.nodeType === Node.TEXT_NODE) {
        const text = clean(current.textContent);
        if (text) pieces.push(text);
        return;
      }
      if (current.nodeType !== Node.ELEMENT_NODE && current.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) {
        return;
      }
      const tag = (current.tagName || "").toLowerCase();
      if (["script", "style", "noscript", "template"].includes(tag)) return;
      if (tag === "slot" && typeof current.assignedNodes === "function") {
        const assigned = current.assignedNodes({ flatten: true });
        if (assigned.length) {
          assigned.forEach(visit);
          return;
        }
      }
      if (current.shadowRoot) {
        Array.from(current.shadowRoot.childNodes || []).forEach(visit);
        return;
      }
      Array.from(current.childNodes || []).forEach(visit);
    };
    try { visit(node); } catch (_) {}
    return clean(pieces.join(" ")).slice(0, 700);
  };
  // Action probes read a field's <label> the way this observer does: through slots (a shadow label's
  // own innerText is just "*" while "Name (Signature Field):" is slotted in, so typing into it drifted).
  try {
    Object.defineProperty(globalThis, "__lumenComposedText", { value: composedText, configurable: true, enumerable: false });
  } catch (_) {}
  const nearbyText = (el) => {
    const parts = [];
    try {
      const id = el.getAttribute("id");
      if (id && window.CSS && CSS.escape) {
        const root = el.getRootNode && el.getRootNode();
        const labels = Array.from(
          root && root.querySelectorAll
            ? root.querySelectorAll(`label[for="${CSS.escape(id)}"]`)
            : [],
        ).map((label) => composedText(label));
        parts.push(...labels);
      }
      let node = el;
      for (let i = 0; i < 4 && node; i += 1) {
        const text = clean(composedText(node) || node.getAttribute("aria-label") || node.getAttribute("placeholder") || node.getAttribute("name"));
        if (text) parts.push(text);
        node = composedParent(node);
      }
      let prev = el.previousElementSibling;
      for (let i = 0; i < 3 && prev; i += 1) {
        const text = clean(composedText(prev) || prev.getAttribute("aria-label"));
        if (text) parts.push(text);
        prev = prev.previousElementSibling;
      }
    } catch (_) {}
    const joined = [];
    for (const part of parts) {
      const safePart = redactSensitiveText(part);
      if (safePart && !joined.includes(safePart)) joined.push(safePart);
      if (joined.join(" ").length > 600) break;
    }
    return joined.join(" ").slice(0, 700);
  };
  const accessibilityContext = (el) => {
    const items = [];
    const push = (relation, value) => {
      const text = redactSensitiveText(value);
      if (!text) return;
      if (items.some((item) => item.relation === relation && item.text === text)) return;
      items.push({ relation, text: text.slice(0, 260) });
    };
    try {
      for (const label of Array.from(el.labels || [])) {
        push("label", composedText(label));
      }
      const labelledBy = clean(el.getAttribute("aria-labelledby"));
      const root = el.getRootNode && el.getRootNode();
      for (const id of labelledBy.split(/\s+/).filter(Boolean)) {
        const source = root && typeof root.getElementById === "function"
          ? root.getElementById(id)
          : document.getElementById(id);
        if (source) push("aria-labelledby", composedText(source));
      }
      push("aria-label", el.getAttribute("aria-label"));
      push("placeholder", el.getAttribute("placeholder"));
    } catch (_) {}
    return items.slice(0, 6);
  };
  const domContext = (el) => {
    // Enumerate raw structural relationships nearest-first. This deliberately does not decide
    // which text is the field label; the model receives the relationships and interprets them.
    const items = [];
    const seen = new Set();
    const push = (relation, depth, value) => {
      const text = redactSensitiveText(value);
      if (!text) return;
      const key = `${relation}:${depth}:${text}`;
      if (seen.has(key)) return;
      seen.add(key);
      items.push({ relation, depth, text: text.slice(0, 300) });
    };
    try {
      let node = el;
      for (let depth = 0; depth <= 6 && node; depth += 1) {
        let previous = node.previousElementSibling;
        for (let ordinal = 1; ordinal <= 2 && previous; ordinal += 1) {
          push(
            ordinal === 1 ? "previous_sibling" : "previous_sibling_2",
            depth,
            composedText(previous) || previous.getAttribute("aria-label")
          );
          previous = previous.previousElementSibling;
        }
        node = composedParent(node);
      }
    } catch (_) {}
    return items.slice(0, 8);
  };
  // A popup option's question is the field that owns its list (an element whose aria-controls / aria-owns names the
  // list's id), not the text nearest it on screen: an open dropdown covers the next question, whose label is then the
  // nearest text above an option (runs 68-69: SmartRecruiters' "No" option was named after the relocation question).
  // A claim is made only when exactly one name comes back; otherwise the position-based text stands.
  const POPUP_OPTION_ROLES = new Set(["option", "menuitem", "menuitemradio", "menuitemcheckbox"]);
  // Roles whose accessible name is their own content: in shadow DOM that content often arrives through a <slot>,
  // which innerText/textContent do not follow (an option row read "" while its "No" was slotted in).
  const NAME_FROM_CONTENT_ROLES = new Set(["option", "menuitem", "menuitemradio", "menuitemcheckbox", "tab", "treeitem", "switch", "button", "link"]);
  // A custom radio/checkbox keeps its `text` as every click-time identity check reads it (run 74: reading its shadow
  // label into `text` made each click a target drift); its visible label is shown to the brain as `derived_label`.
  const SHOWN_LABEL_ROLES = new Set(["radio", "checkbox"]);
  const popupOwnerCache = new Map();
  const fieldName = (owner) => (
    accessibilityContext(owner).filter((item) => item.relation !== "placeholder").map((item) => item.text).find(Boolean) || ""
  );
  const popupOwnerText = (el) => {
    try {
      const role = clean(el.getAttribute && el.getAttribute("role")).toLowerCase();
      if (!POPUP_OPTION_ROLES.has(role)) return "";
      let list = null;
      let node = composedParent(el);
      for (let hops = 0; node && hops < 10; hops += 1, node = composedParent(node)) {
        const nodeRole = clean(node.getAttribute && node.getAttribute("role")).toLowerCase();
        if (nodeRole === "listbox" || nodeRole === "menu") {
          list = node;
          break;
        }
      }
      if (!list) return "";
      if (popupOwnerCache.has(list)) return popupOwnerCache.get(list);
      let result = "";
      const id = clean(list.getAttribute("id"));
      if (id) {
        const quoted = id.replace(/["\\]/g, "\\$&");
        const ownerSelector = `[aria-controls~="${quoted}"],[aria-owns~="${quoted}"]`;
        let owners = [];
        try {
          const root = list.getRootNode && list.getRootNode();
          if (root && typeof root.querySelectorAll === "function") owners = Array.from(root.querySelectorAll(ownerSelector));
        } catch (_) {
          owners = [];
        }
        if (!owners.length) owners = queryAllComposed(ownerSelector);
        owners = owners.filter((owner) => owner !== list && !composedContains(list, owner));
        const fields = owners.filter((owner) => {
          const tag = (owner.tagName || "").toLowerCase();
          return clean(owner.getAttribute("role")).toLowerCase() === "combobox" || ["input", "textarea", "select"].includes(tag);
        });
        const names = new Set((fields.length ? fields : owners).map(fieldName).filter(Boolean));
        if (names.size === 1) result = Array.from(names)[0];
      }
      if (!result) result = fieldName(list);
      popupOwnerCache.set(list, result);
      return result;
    } catch (_) {
      return "";
    }
  };
  // A custom radio or checkbox's question is its group's label (a radiogroup/group role or a fieldset, named by
  // aria-labelledby, aria-label or its legend), not the text above it: the second choice's nearest text is the first
  // choice's own label, so SmartRecruiters' "No" radio read as "Yes" (run 73). Native inputs keep their <label>.
  const choiceGroupText = (el) => {
    try {
      const role = clean(el.getAttribute && el.getAttribute("role")).toLowerCase();
      const tag = (el.tagName || "").toLowerCase();
      if (!(role === "radio" || role === "checkbox") || tag === "input") return "";
      let node = composedParent(el);
      for (let hops = 0; node && hops < 6; hops += 1, node = composedParent(node)) {
        const nodeRole = clean(node.getAttribute && node.getAttribute("role")).toLowerCase();
        const nodeTag = (node.tagName || "").toLowerCase();
        if (nodeRole === "radiogroup" || nodeRole === "group" || nodeTag === "fieldset") {
          const named = fieldName(node);
          if (named) return named;
          const legend = nodeTag === "fieldset" && node.querySelector ? node.querySelector("legend") : null;
          return legend ? composedText(legend) : "";
        }
      }
    } catch (_) {
      return "";
    }
    return "";
  };
  const ancestorChain = (el) => {
    // Enumerate raw containment and ARIA identifiers. The server-side brain interprets whether
    // these relationships associate a popup option with a field; this body does not classify them.
    const items = [];
    try {
      let node = composedParent(el);
      for (let depth = 1; depth <= 5 && node; depth += 1) {
        items.push({
          depth,
          tag: (node.tagName || "").toLowerCase(),
          id: clean(node.getAttribute("id")).slice(0, 200),
          role: clean(node.getAttribute("role")).slice(0, 80),
          aria_controls: clean(node.getAttribute("aria-controls")).slice(0, 300),
          aria_owns: clean(node.getAttribute("aria-owns")).slice(0, 300),
          aria_labelledby: clean(node.getAttribute("aria-labelledby")).slice(0, 300),
          aria_modal: clean(node.getAttribute("aria-modal")).slice(0, 10),
        });
        node = composedParent(node);
      }
    } catch (_) {}
    return items;
  };
  // The highest explicit z-index among the control's positioned ancestors: raw stacking evidence. A popup
  // or picker sits in a layer above the page; the server fills the inventory from the newest such layer.
  const stackLayer = (el) => {
    let layer = 0;
    try {
      let node = composedParent(el);
      for (let depth = 0; depth < 24 && node && node.nodeType === 1; depth += 1) {
        const style = window.getComputedStyle(node);
        if (style && style.position !== "static") {
          const z = parseInt(style.zIndex, 10);
          if (Number.isFinite(z) && z > layer) layer = z;
        }
        node = composedParent(node);
      }
    } catch (_) {}
    return layer;
  };
  // Whether a control reads as switched on: the native checked state, or the page's own marking of a
  // chosen option - aria-checked / aria-pressed / aria-selected, a data-state / data-checked /
  // data-selected value, or a state class (selected, active, checked, on, current). A segmented
  // choice built from buttons carries its selection only in one of these, and a checked:false beside
  // a highlighted option misled the brain. Raw markers only; what the control means stays with the model.
  const STATE_CLASS_TOKENS = new Set([
    "selected", "active", "checked", "on", "current", "chosen", "pressed",
    "is-selected", "is-active", "is-checked", "is-on", "is-current", "is-chosen", "is-pressed",
    "selected-option", "active-option",
  ]);
  const toggledOn = (el) => {
    try {
      for (const name of ["aria-checked", "aria-pressed", "aria-selected"]) {
        if (clean(el.getAttribute(name)).toLowerCase() === "true") return true;
      }
      if (clean(el.getAttribute("aria-checked")) || clean(el.getAttribute("aria-pressed")) || clean(el.getAttribute("aria-selected"))) {
        return false;
      }
      const dataState = clean(el.getAttribute("data-state")).toLowerCase();
      if (["checked", "on", "selected", "active"].includes(dataState)) return true;
      for (const name of ["data-checked", "data-selected", "data-active"]) {
        if (clean(el.getAttribute(name)).toLowerCase() === "true") return true;
      }
      const tag = String(el.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return false;
      const classes = clean(typeof el.className === "string" ? el.className : el.getAttribute("class")).toLowerCase().split(" ");
      return classes.some((token) => STATE_CLASS_TOKENS.has(token));
    } catch (_) {
      return false;
    }
  };
  const textCandidates = () => {
    const selector = [
      "label",
      "legend",
      "p",
      "span",
      "strong",
      "b",
      "small",
      "div",
      "h1",
      "h2",
      "h3",
      "h4",
    ].join(",");
    const items = [];
    for (const node of queryAllComposed(selector)) {
      try {
        if (nodeTouchesSensitiveValue(node)) continue;
        if (node.matches("input,textarea,select,button,a,[role='button'],[role='combobox'],[role='option'],[role='radio'],[role='checkbox']")) continue;
        if (deepDescendants(
          node,
          "input,textarea,select,button,a,[role='button'],[role='combobox'],[role='option'],[role='radio'],[role='checkbox']",
        ).length) continue;
        const text = redactSensitiveText(node.innerText || node.textContent || node.getAttribute("aria-label"));
        if (!text || text.length > 260) continue;
        const rect = topRect(node);
        const style = window.getComputedStyle(node);
        if (rect.width <= 0 || rect.height <= 0 || rect.bottom < -180 || rect.top > window.innerHeight + 120) continue;
        if (style.visibility === "hidden" || style.display === "none") continue;
        items.push({ node, text, rect });
      } catch (_) {}
    }
    return items;
  };
  const contextText = (rect, candidates) => {
    try {
      const dpr = window.devicePixelRatio || 1;
      const useful = [];
      const centerX = rect.left + rect.width / 2;
      for (const item of candidates) {
        const above = item.rect.bottom <= rect.top + 8;
        const closeY = item.rect.bottom >= rect.top - 220;
        const overlap = Math.max(0, Math.min(item.rect.right, rect.right) - Math.max(item.rect.left, rect.left));
        const aligned = overlap > Math.min(rect.width, item.rect.width) * 0.15 || Math.abs((item.rect.left + item.rect.width / 2) - centerX) < 180 || Math.abs(item.rect.left - rect.left) < 80;
        if (above && closeY && aligned) {
          useful.push({
            text: item.text,
            gap: Math.max(0, rect.top - item.rect.bottom),
            y: Math.round(item.rect.top * dpr),
            x: Math.round(item.rect.left * dpr),
          });
        }
      }
      useful.sort((a, b) => a.gap - b.gap || b.y - a.y || a.x - b.x);
      const joined = [];
      for (const item of useful.slice(0, 4)) {
        if (item.text && !joined.includes(item.text)) joined.push(item.text);
      }
      return joined.join(" | ").slice(0, 700);
    } catch (_) {
      return "";
    }
  };
  try {
    if (semanticSelectorError) {
      return {
        viewport_text: "",
        controls: [],
        error: "model_classified_sensitive_selector_invalid",
      };
    }
    const dpr = window.devicePixelRatio || 1;
    const selector = [
      "input",
      "textarea",
      "select",
      "button",
      "a[href]",
      "[role='button']",
      "[role='combobox']",
      "[role='option']",
      "[role='radio']",
      "[role='checkbox']",
      "[role='slider']",
      "[contenteditable='true']",
      // A spreadsheet-style grid: its cells are what an edit starts from (a double-click opens the
      // inline editor), and a div/td cell names no interactive tag.
      "[role='gridcell']",
      "[role='grid'] td",
      // A plain table whose cells take focus or editing (tabindex, or below: a cell cursor) is a grid too.
      "td[tabindex]",
      "[role='cell'][tabindex]",
      "[role='textbox']",
      // Tab and disclosure headers (an accordion's collapsed h3, a tab strip) are listed whatever
      // state they are in; the control, not its panel, is what a click expands.
      "[role='tab']",
      "[aria-expanded]",
      // A slider handle whose track is plain divs (jQuery-UI) names no role.
      ".ui-slider-handle",
      // A canvas is one surface a page may paint fields and buttons on: listed from the first
      // observation (not only once something focuses it), so the brain can bind its painted targets.
      "canvas",
    ].join(",");
    // Inline formatting must not break a sentence: "PIN is <strong>5239</strong>." reads as one
    // line, in DOM order, not as "PIN is ." plus a stray "5239" somewhere later. Inline children
    // fold into their parent's line; block-level elements stay separate pieces.
    const INLINE_TAGS = new Set([
      "a", "span", "strong", "em", "b", "i", "u", "code", "small", "abbr", "sup", "sub", "time",
      "mark", "kbd", "q", "cite", "dfn", "var", "samp", "s", "del", "ins", "bdi", "bdo", "label",
      "button", "output", "font",
    ]);
    const isInlineElement = (el) => {
      try {
        const tag = (el.tagName || "").toLowerCase();
        if (["script", "style", "noscript", "template"].includes(tag)) return false;
        const display = String(window.getComputedStyle(el).display || "");
        if (display === "none") return false;
        return INLINE_TAGS.has(tag) || display.startsWith("inline");
      } catch (_) {
        return false;
      }
    };
    const consumedInline = new Set();
    const lineTextOf = (node) => {
      const parts = [];
      for (const child of Array.from(node.childNodes || [])) {
        if (!child) continue;
        if (child.nodeType === Node.TEXT_NODE) {
          const text = clean(child.textContent);
          if (text) parts.push(text);
        } else if (child.nodeType === Node.ELEMENT_NODE && isInlineElement(child)) {
          consumedInline.add(child);
          const text = lineTextOf(child);
          if (text) parts.push(text);
        }
      }
      return parts.join(" ");
    };
    const redactedViewportText = () => {
      try {
        const pieces = [];
        // Visible text only: a line scrolled out of its panel's window (a list row above the panel's
        // scrollTop, a card off the side of a sideways lane) is inside the page band but not on screen.
        // The model read such lines as visible and as neighbours of unrelated controls.
        let labelledLeaves = 0;
        const clipBoxes = new Map();
        const clipBoxOf = (el) => {
          if (clipBoxes.has(el)) return clipBoxes.get(el);
          let box = null;
          try {
            const css = window.getComputedStyle(el);
            const clips = [css.overflow, css.overflowX, css.overflowY].some((value) => ["hidden", "scroll", "auto", "clip"].includes(value));
            if (clips && (el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1)) box = topRect(el);
          } catch (_) {
            box = null;
          }
          clipBoxes.set(el, box);
          return box;
        };
        // Text a person cannot see is not "visible text": a fully transparent ancestor, a transparent text
        // colour (unless it is a background-clip:text gradient) or a sub-3px font. Page text that gates a
        // credential must be something on screen, not an attacker's invisible line.
        const opacityCache = new Map();
        const effectiveOpacity = (el) => {
          if (!el || el === document.documentElement) return 1;
          if (opacityCache.has(el)) return opacityCache.get(el);
          let own = 1;
          try {
            const raw = String(window.getComputedStyle(el).opacity ?? "").trim();
            own = raw === "" ? 1 : parseFloat(raw);
          } catch (_) { own = 1; }
          if (!Number.isFinite(own)) own = 1;
          const value = own * effectiveOpacity(el.parentElement);
          opacityCache.set(el, value);
          return value;
        };
        const invisibleText = (node, style) => {
          try {
            if (effectiveOpacity(node) < 0.1) return true;
            if (parseFloat(style.fontSize) < 3) return true;
            const color = String(style.color || "").replace(/\s+/g, "").toLowerCase();
            const clipText = String(style.webkitBackgroundClip || style.backgroundClip || "").toLowerCase() === "text";
            if (!clipText && (color === "transparent" || /^rgba\(\d+,\d+,\d+,0(\.0+)?\)$/.test(color))) return true;
          } catch (_) {
            return false;
          }
          return false;
        };
        const clippedOut = (node, rect) => {
          for (let el = node.parentElement; el && el !== document.body && el !== document.documentElement; el = el.parentElement) {
            const box = clipBoxOf(el);
            if (box && (rect.bottom <= box.top || rect.top >= box.bottom || rect.right <= box.left || rect.left >= box.right)) return true;
          }
          return false;
        };
        // Column headers: a line inside a column of a grid or table says nothing without its header
        // (a week view's event "Customer call 14:30-15:30" names no day; the day heads are another
        // element). Found structurally: th / role=columnheader cells, and the top row of short
        // non-interactive children of a CSS grid whose other children are a different kind of box
        // below it. A line under a header's horizontal span, in the same region, carries that label.
        const allNodes = queryAllComposed("*");
        const columnRegions = [];
        try {
          const headerLabel = (el) => redactSensitiveText(clean(el.innerText || el.textContent || "")).slice(0, 40);
          const shapeOf = (el) => `${String(el.tagName || "").toLowerCase()}|${String(el.getAttribute("class") || "").split(/\s+/)[0]}`;
          const regionsByEl = new Map();
          let scanned = 0;
          for (const el of allNodes) {
            if (scanned >= 4000) break;
            scanned += 1;
            const tag = String(el.tagName || "").toLowerCase();
            const role = clean(el.getAttribute && el.getAttribute("role")).toLowerCase();
            if (tag === "th" || role === "columnheader") {
              if (el.matches && el.matches(selector)) continue;
              const label = headerLabel(el);
              if (!label || nodeTouchesSensitiveValue(el)) continue;
              const box = topRect(el);
              if (box.width <= 0 || box.height <= 0) continue;
              const regionEl = (el.closest && el.closest("table,[role='grid'],[role='table'],[role='treegrid']")) || el.parentElement;
              if (!regionEl) continue;
              if (!regionsByEl.has(regionEl)) regionsByEl.set(regionEl, []);
              regionsByEl.get(regionEl).push({ el, label, left: box.left, right: box.right, bottom: box.bottom });
              continue;
            }
            const kids = el.children;
            if (!kids || kids.length < 4 || kids.length > 80) continue;
            let display = "";
            try { display = String(window.getComputedStyle(el).display || ""); } catch (_) { display = ""; }
            if (display !== "grid" && display !== "inline-grid") continue;
            const rows = new Map();
            for (const kid of Array.from(kids)) {
              try {
                const box = topRect(kid);
                if (box.width <= 0 || box.height <= 0 || box.height > 80) continue;
                const text = clean(kid.innerText || kid.textContent || "");
                if (!text || text.length > 32) continue;
                if ((kid.matches && kid.matches(selector)) || kid.querySelector(selector)) continue;
                const key = Math.round(box.top / 4);
                if (!rows.has(key)) rows.set(key, []);
                rows.get(key).push({ el: kid, label: headerLabel(kid), left: box.left, right: box.right, bottom: box.bottom, top: box.top });
              } catch (_) {}
            }
            const candidates = Array.from(rows.entries()).filter(([, members]) => members.length >= 3).sort((a, b) => a[0] - b[0]);
            if (!candidates.length) continue;
            const heads = candidates[0][1].filter((member) => member.label).sort((a, b) => a.left - b.left);
            if (heads.length < 3) continue;
            let tiled = true;
            for (let i = 1; i < heads.length; i += 1) if (heads[i].left < heads[i - 1].right - 2) tiled = false;
            if (!tiled) continue;
            const headEls = new Set(heads.map((member) => member.el));
            const headShapes = new Set(heads.map((member) => shapeOf(member.el)));
            const headerTop = Math.min(...heads.map((member) => member.top));
            // The rest of the grid below the header row must be a different kind of box: a grid of
            // same-shaped chips or cards has rows, not column headers.
            const below = Array.from(kids).filter((kid) => !headEls.has(kid) && topRect(kid).bottom > headerTop + 1);
            if (!below.length || below.some((kid) => headShapes.has(shapeOf(kid)))) continue;
            if (!regionsByEl.has(el)) {
              regionsByEl.set(el, heads.map((member) => ({ el: member.el, label: member.label, left: member.left, right: member.right, bottom: member.bottom })));
            }
          }
          for (const [regionEl, heads] of regionsByEl.entries()) columnRegions.push({ el: regionEl, heads });
        } catch (_) {
          columnRegions.length = 0;
        }
        let columnLabelChars = 0;
        const columnLabelFor = (node, rect) => {
          if (!columnRegions.length || columnLabelChars >= 1500) return "";
          let best = null;
          for (const region of columnRegions) {
            if (region.el === node || !composedContains(region.el, node)) continue;
            for (const head of region.heads) {
              // A header's own text (and anything inside it) is not a cell of its column.
              if (head.el === node || composedContains(head.el, node)) return "";
              const centre = (rect.left + rect.right) / 2;
              if (centre < head.left - 2 || centre > head.right + 2) continue;
              if (rect.width > (head.right - head.left) * 1.6 + 4) continue;
              if (rect.top < head.bottom - 2) continue;
              if (!best || head.bottom > best.bottom) best = head;
            }
          }
          return best ? best.label : "";
        };
        for (const node of allNodes) {
          const tag = (node.tagName || "").toLowerCase();
          if (["script", "style", "noscript", "template"].includes(tag)) continue;
          if (consumedInline.has(node)) continue;
          let rect;
          let style;
          try {
            rect = topRect(node);
            style = window.getComputedStyle(node);
          } catch (_) {
            continue;
          }
          if (
            !rect
            || rect.width <= 0
            || rect.height <= 0
            || rect.bottom < -180
            || rect.top > window.innerHeight + 120
            || rect.right < -180
            || rect.left > window.innerWidth + 120
            || style.visibility === "hidden"
            || style.display === "none"
            || invisibleText(node, style)
            || clippedOut(node, rect)
          ) continue;
          let safe = redactSensitiveText(lineTextOf(node));
          // A graphic that carries its meaning in a title / aria-label (a calendar's busy bar, a status dot)
          // has no text of its own: that label is what the page says about it. Leaf, non-control elements only.
          // The label is page-authored and not on screen: a graphic too small or too faint to see does not speak.
          if (labelledLeaves < 40 && node.children.length === 0 && !node.matches(selector) && rect.width >= 3 && rect.height >= 3 && Number(style.opacity || 1) >= 0.2) {
            const named = clean(node.getAttribute("aria-label") || node.getAttribute("title") || "");
            if (named && named.length <= 100 && !safe.includes(named)) {
              labelledLeaves += 1;
              safe = redactSensitiveText(safe ? `${safe} (${named})` : named);
            }
          }
          // Each piece is one visible element's own line, so a repeated value is data, not an echo:
          // a table's second "No" or a form's second "Yes" stays. Only a back-to-back repeat collapses.
          if (!safe || pieces[pieces.length - 1] === safe) continue;
          if (columnRegions.length && safe.length <= 160) {
            const label = columnLabelFor(node, rect);
            if (label && !safe.includes(label)) {
              columnLabelChars += label.length + 3;
              safe = `${label} \u00b7 ${safe}`;
            }
          }
          pieces.push(safe);
          if (pieces.join(" ").length >= 5200) break;
        }
        return pieces.join(" ");
      } catch (_) {
        return "";
      }
    };
    // Off-canvas text is not on the page a reader can reach: an element wholly outside the scrollable
    // area of the document, or of the scroll container that holds it (a block at left:-9999px, a line
    // above a panel's scroll origin), is left out, and so is everything inside a container that is
    // itself off canvas. Areas are in top-level viewport pixels, like topRect. A right-to-left axis is
    // not bounded horizontally (its scroll origin is on the right).
    const SCROLL_CONTAINER_OVERFLOW = ["hidden", "scroll", "auto", "clip"];
    const scrollAreas = new Map();
    const scrollAreaOf = (el) => {
      // null: the document itself. false: el is not a scroll container.
      if (scrollAreas.has(el)) return scrollAreas.get(el);
      let area = false;
      try {
        if (el === null) {
          const root = document.scrollingElement || document.documentElement;
          const left = -(Number(window.scrollX) || 0);
          const top = -(Number(window.scrollY) || 0);
          area = {
            left,
            top,
            right: left + Math.max(Number(root.scrollWidth) || 0, Number(window.innerWidth) || 0),
            bottom: top + Math.max(Number(root.scrollHeight) || 0, Number(window.innerHeight) || 0),
            rtl: window.getComputedStyle(document.documentElement).direction === "rtl",
          };
        } else {
          const css = window.getComputedStyle(el);
          const shorthand = String(css.overflow || "").trim().split(/\s+/);
          const overflowX = String(css.overflowX || shorthand[0] || "visible");
          const overflowY = String(css.overflowY || shorthand[1] || shorthand[0] || "visible");
          const clipsX = SCROLL_CONTAINER_OVERFLOW.includes(overflowX);
          const clipsY = SCROLL_CONTAINER_OVERFLOW.includes(overflowY);
          if (clipsX || clipsY) {
            const box = topRect(el);
            // overflow:clip shows only its box; a scroll container's area is all it can scroll to.
            const width = (overflowX === "clip"
              ? Number(el.clientWidth) || 0
              : Math.max(Number(el.scrollWidth) || 0, Number(el.clientWidth) || 0)) || box.width;
            const height = (overflowY === "clip"
              ? Number(el.clientHeight) || 0
              : Math.max(Number(el.scrollHeight) || 0, Number(el.clientHeight) || 0)) || box.height;
            const left = box.left + (Number(el.clientLeft) || 0) - (overflowX === "clip" ? 0 : Number(el.scrollLeft) || 0);
            const top = box.top + (Number(el.clientTop) || 0) - (overflowY === "clip" ? 0 : Number(el.scrollTop) || 0);
            area = {
              left: clipsX ? left : -Infinity,
              top: clipsY ? top : -Infinity,
              right: clipsX ? left + width : Infinity,
              bottom: clipsY ? top + height : Infinity,
              rtl: css.direction === "rtl",
            };
          }
        }
      } catch (_) {
        area = false;
      }
      scrollAreas.set(el, area);
      return area;
    };
    const outsideArea = (rect, area) => (
      rect.bottom <= area.top
      || rect.top >= area.bottom
      || (!area.rtl && (rect.right <= area.left || rect.left >= area.right))
    );
    const reachableContainers = new Map();
    const onCanvas = (node, rect) => {
      let container = null;
      for (
        let el = composedParent(node);
        el && el !== document.body && el !== document.documentElement;
        el = composedParent(el)
      ) {
        if (scrollAreaOf(el)) {
          container = el;
          break;
        }
      }
      const area = scrollAreaOf(container);
      if (area && outsideArea(rect, area)) return false;
      if (!container) return true;
      if (!reachableContainers.has(container)) {
        reachableContainers.set(container, false);
        let box = null;
        try { box = topRect(container); } catch (_) { box = null; }
        reachableContainers.set(container, box ? onCanvas(container, box) : true);
      }
      return reachableContainers.get(container);
    };
    // The whole document's rendered text in document order, with each element's own line redacted
    // exactly as the viewport text is: every rendered element on canvas (no viewport or panel
    // clipping), but nothing display:none, visibility:hidden, or off canvas.
    const redactedDocumentText = (limit, lineSeparator = " ") => {
      try {
        const pieces = [];
        let length = 0;
        for (const node of queryAllComposed("*")) {
          const tag = (node.tagName || "").toLowerCase();
          if (["script", "style", "noscript", "template"].includes(tag)) continue;
          if (consumedInline.has(node)) continue;
          let rect;
          let style;
          try {
            rect = topRect(node);
            style = window.getComputedStyle(node);
          } catch (_) {
            continue;
          }
          if (
            !rect
            || rect.width <= 0
            || rect.height <= 0
            || style.visibility === "hidden"
            || style.display === "none"
            || !onCanvas(node, rect)
          ) continue;
          const safe = redactSensitiveText(lineTextOf(node));
          if (!safe || pieces[pieces.length - 1] === safe) continue;
          pieces.push(safe);
          length += safe.length + 1;
          if (length > limit) break;
        }
        return pieces.join(lineSeparator);
      } catch (_) {
        return "";
      }
    };
    if (documentTextMode) {
      // A model-classified selector must still name exactly one element of this document; otherwise
      // the value it was bound to cannot be shown to be covered, and no text is returned.
      for (const semanticSelector of semanticSelectors) {
        if (queryAllComposed(semanticSelector).length !== 1) {
          return { document_text: "", error: "sensitive_selector_drift" };
        }
      }
      const limit = Math.max(500, Math.min(20000, Math.round(Number(options.max_chars) || 6000)));
      const text = redactedDocumentText(limit, options.line_separator === "\n" ? "\n" : " ");
      return {
        document_text: text.slice(0, limit),
        document_text_truncated: text.length > limit,
        url: String(location.href || ""),
        title: String(document.title || ""),
        document_lifecycle_token: documentLifecycleToken,
      };
    }
    const contextCandidates = textCandidates();
    // Text of a container that is not inside any control it holds (a section heading, a card's
    // name and price): what an action control sits next to. Cached per container for one capture.
    const nonControlTextCache = new Map();
    const nonControlText = (container) => {
      if (nonControlTextCache.has(container)) return nonControlTextCache.get(container);
      const parts = [];
      let length = 0;
      try {
        const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
        for (let node = walker.nextNode(); node && length < 200; node = walker.nextNode()) {
          const parent = node.parentElement;
          if (!parent) continue;
          const parentTag = String(parent.tagName || "").toLowerCase();
          if (["script", "style", "noscript", "template"].includes(parentTag)) continue;
          const owningControl = parent.closest ? parent.closest(selector) : null;
          if (owningControl && container.contains(owningControl)) continue;
          const text = clean(node.textContent);
          if (!text) continue;
          parts.push(text);
          length += text.length + 1;
        }
      } catch (_) {}
      const value = redactSensitiveText(parts.join(" ")).slice(0, 200);
      nonControlTextCache.set(container, value);
      return value;
    };
    // The nearest enclosing container with text of its own outside its controls.
    const controlContext = (el) => {
      let node = composedParent(el);
      for (let depth = 0; depth < 6 && node && node.nodeType === 1; depth += 1) {
        const tag = String(node.tagName || "").toLowerCase();
        if (tag === "body" || tag === "html") break;
        const text = nonControlText(node);
        if (text) return text;
        node = composedParent(node);
      }
      return "";
    };
    // The nearest text before a control in document order that is not inside any control: in one
    // flat container of cards, each button's own card name.
    const precedingText = (el) => {
      try {
        const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
        walker.currentNode = el;
        for (let node = walker.previousNode(), steps = 0; node && steps < 400; node = walker.previousNode(), steps += 1) {
          const parent = node.parentElement;
          if (!parent) continue;
          const parentTag = String(parent.tagName || "").toLowerCase();
          if (["script", "style", "noscript", "template"].includes(parentTag)) continue;
          if (parent.closest && parent.closest(selector)) continue;
          const text = clean(node.textContent);
          if (text) return redactSensitiveText(text).slice(0, 160);
        }
      } catch (_) {}
      return "";
    };
    // Every rendered text piece in the document, as an unordered list; null past either bound
    // (pieces kept, or text nodes visited).
    const renderedTextPieces = (limit) => {
      const pieces = [];
      try {
        const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
        let visited = 0;
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
          visited += 1;
          if (visited > 5000) return null;
          const parent = node.parentElement;
          if (!parent) continue;
          const parentTag = String(parent.tagName || "").toLowerCase();
          if (["script", "style", "noscript", "template"].includes(parentTag)) continue;
          const text = clean(node.textContent);
          if (!text) continue;
          const rect = topRect(parent);
          if (!rect || rect.width <= 0 || rect.height <= 0) continue;
          pieces.push(redactSensitiveText(text).slice(0, 200));
          if (pieces.length > limit) return null;
        }
      } catch (_) {
        return null;
      }
      return pieces.sort();
    };
    const hashSignature = (prefix, raw) => {
      let hash = 2166136261;
      for (let index = 0; index < raw.length; index += 1) {
        hash ^= raw.charCodeAt(index);
        hash = Math.imul(hash, 16777619);
      }
      return `${prefix}:${hash >>> 0}:${raw.length}`;
    };
    // Three bindings of the document's state, from one enumeration of its rendered controls.
    // state (v1): every control's selector, id, class, label, value, and state, in document order.
    // values (vv1): what a submit would carry (field values, checked and selected states), not labels
    // or status text that live pages keep rewriting.
    // semantic (sv2): what a page that re-renders itself with fresh ids and reshuffled sections keeps
    // the same - each control's label, value, state, link target, form, and the text around it, with
    // no DOM ids or selectors; value-bearing controls in document order, buttons and links as an
    // unordered set; plus every rendered text piece and hidden form value. It is withheld (empty) when
    // it cannot vouch: a redacted value it cannot compare, more controls, text, or hidden values than
    // it enumerates, text it cannot read (an open shadow root, a frame), or two action controls nothing
    // tells apart (a state moving between them would hide).
    // Final-action approval must survive viewport resizing and scrolling: no geometry or viewport
    // clipping. This records state; it does not infer what any control means.
    const documentSignatures = (withSemantic = true) => {
      const result = { state: "", values: "", semantic: "", rows: [] };
      try {
        const rendered = queryAllComposed(selector).filter((el) => {
          try {
            const rect = topRect(el);
            const style = window.getComputedStyle(el);
            return (
              rect.width > 0
              && rect.height > 0
              && style.visibility !== "hidden"
              && style.display !== "none"
            );
          } catch (_) {
            return false;
          }
        });
        const elements = rendered.slice(0, 240);
        const rows = elements.map((el) => {
          const valueRedacted = sensitiveValueControl(el);
          const rawValue = clean(
            (el.isContentEditable || clean(el.getAttribute("contenteditable")).toLowerCase() === "true")
              ? (el.innerText || el.textContent || "")
              : (el.value || el.getAttribute("value")),
          ).slice(0, 180).trim();
          return {
            selector: stableSelector(el),
            tag: (el.tagName || "").toLowerCase(),
            role: clean(el.getAttribute("role")),
            type: clean(el.getAttribute("type")),
            id: clean(el.getAttribute("id")).slice(0, 160),
            name: clean(el.getAttribute("name")).slice(0, 160),
            class_name: clean(
              typeof el.className === "string" ? el.className : el.getAttribute("class"),
            ).slice(0, 240),
            text: clean(
              valueRedacted
                ? (el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.getAttribute("name") || "")
                : (el.innerText || el.textContent || el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.getAttribute("name")),
            ).slice(0, 180).trim(),
            value: valueRedacted && rawValue ? "[REDACTED]" : rawValue,
            checked: Boolean(el.checked),
            selected: Boolean(el.selected),
            aria_checked: clean(el.getAttribute("aria-checked")),
            aria_pressed: clean(el.getAttribute("aria-pressed")),
            aria_selected: clean(el.getAttribute("aria-selected")),
            aria_expanded: clean(el.getAttribute("aria-expanded")),
            disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true"),
          };
        });
        const valueBearing = (row) => (
          ["input", "textarea", "select"].includes(row.tag)
          || row.aria_checked
          || row.aria_selected
          || (row.value && !["button", "a"].includes(row.tag))
        );
        const title = String(document.title || "").slice(0, 240);
        result.state = hashSignature("v1", JSON.stringify({ version: 1, title, controls: rows }));
        result.values = hashSignature("vv1", JSON.stringify({
          version: 1,
          controls: rows.filter(valueBearing).map((row) => ({
            selector: row.selector,
            value: row.value,
            checked: row.checked,
            selected: row.selected,
            aria_checked: row.aria_checked,
            aria_selected: row.aria_selected,
          })),
        }));
        // One digest per control row (its selector, whether it sits in a frame, and the row itself) plus the
        // title, so a live check can name the single row that changed. Withheld (empty) when the enumeration
        // is capped or a selector is missing or repeated: then no row can be shown to be the only change.
        result.rows = (() => {
          if (rendered.length > 240) return [];
          const out = [];
          const seen = new Set();
          for (let index = 0; index < rows.length; index += 1) {
            const key = String(rows[index].selector || "");
            if (!key || seen.has(key)) return [];
            seen.add(key);
            out.push({
              selector: key,
              frame: elements[index].ownerDocument !== document ? 1 : 0,
              digest: hashSignature("row1", JSON.stringify(rows[index])),
            });
          }
          out.push({ selector: "", frame: 0, digest: hashSignature("title1", title) });
          return out;
        })();
        result.semantic = (() => {
          if (!withSemantic) return "";
          if (rendered.length > 240) return "";
          if (rows.some((row) => row.value === "[REDACTED]")) return "";
          // Text inside an open shadow root or a frame is outside the walk below: no proof there.
          if (openRoots().length > 1) return "";
          if (document.querySelector("iframe, frame, object, embed")) return "";
          const texts = renderedTextPieces(800);
          if (!texts) return "";
          const semanticRows = rows.map((row, index) => {
            const el = elements[index];
            const { selector: _selector, id: _id, ...rest } = row;
            const form = el.form || (el.closest ? el.closest("form") : null);
            return {
              ...rest,
              label: accessibilityContext(el).map((item) => `${item.relation}:${item.text}`).join("|").slice(0, 400),
              href: clean(el.getAttribute("href")).slice(0, 300),
              form_action: form ? clean(form.getAttribute("action")).slice(0, 300) : "",
              formaction: clean(el.getAttribute("formaction")).slice(0, 300),
              context: controlContext(el),
              preceding: precedingText(el),
            };
          });
          const actions = semanticRows.filter((row) => !valueBearing(row));
          const identities = new Set();
          for (const row of actions) {
            const identity = JSON.stringify([
              row.tag, row.type, row.role, row.name, row.text, row.label, row.href, row.form_action,
              row.formaction, row.context, row.preceding,
            ]);
            if (identities.has(identity)) return "";
            identities.add(identity);
          }
          const hiddenInputs = queryAllComposed("input[type='hidden']");
          if (hiddenInputs.length > 200) return "";
          const hidden = [];
          for (const input of hiddenInputs) {
            const form = input.form || (input.closest ? input.closest("form") : null);
            hidden.push(JSON.stringify([
              clean(input.getAttribute("name")).slice(0, 160),
              String(input.value || "").slice(0, 300),
              form ? clean(form.getAttribute("action")).slice(0, 300) : "",
            ]));
          }
          return hashSignature("sv2", JSON.stringify({
            version: 2,
            title,
            values: semanticRows.filter(valueBearing),
            actions: actions.map((row) => JSON.stringify(row)).sort(),
            hidden: hidden.sort(),
            texts,
          }));
        })();
      } catch (_) {}
      return result;
    };
    const documentStateSignature = (valuesOnly = false) => {
      const signatures = documentSignatures(false);
      return valuesOnly ? signatures.values : signatures.state;
    };
    try {
      Object.defineProperty(globalThis, "__lumenDocumentStateSignature", {
        value: documentStateSignature,
        configurable: true,
        enumerable: false,
      });
    } catch (_) {}
    const DRAG_CURSOR_PATTERN = /^(grab|grabbing|move|all-scroll|crosshair|[a-z]+-resize)$/;
    const focusedNode = deepActiveElement();
    const selectedElements = queryAllComposed(selector);
    // Table cells the page marks editable by their cursor (cursor: cell), with no tabindex or role: the
    // computed style is the only raw sign an editing grid leaves. Visible cells only, bounded.
    try {
      const have = new Set(selectedElements);
      let checked = 0;
      for (const cell of queryAllComposed("td,[role='cell']")) {
        if (checked >= 400) break;
        if (have.has(cell)) continue;
        checked += 1;
        let cursor = "";
        try { cursor = String(window.getComputedStyle(cell).cursor || ""); } catch (_) {}
        if (cursor === "cell") selectedElements.push(cell);
      }
    } catch (_) {}
    // A grid's cells would fill the cap before the controls after them: cells yield to other controls
    // once the cap is near, and the server lists them nearest the focus after every other row.
    const onScreenElements = selectedElements.filter(visible);
    const isGridCell = (el) => {
      try {
        const role = clean(el.getAttribute("role")).toLowerCase();
        return role === "gridcell" || role === "cell" || el.tagName === "TD";
      } catch (_) { return false; }
    };
    const listedElements = [
      ...onScreenElements.filter((el) => !isGridCell(el)).slice(0, 80),
      ...onScreenElements.filter(isGridCell).slice(0, 60),
    ].sort((a, b) => {
      if (a === b) return 0;
      return (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1;
    });
    // An open listbox or menu's options, including those scrolled out of the popup's own window (or the
    // page's): listed with `list_position`, so the brain knows an option exists beyond the clip and scrolls
    // that list to it. The newest visible popup first; bounded.
    const popupOptionEls = new Set();
    try {
      const popups = queryAllComposed("[role='listbox'],[role='menu'],ul.ui-autocomplete,ul.ui-menu").filter(visible);
      const popup = popups[popups.length - 1];
      if (popup) {
        const listed = new Set(listedElements);
        for (const opt of deepDescendants(popup, "[role='option'],[role='menuitem'],[role='menuitemradio'],li,.ui-menu-item")) {
          if (popupOptionEls.size >= 40) break;
          popupOptionEls.add(opt);
          if (!listed.has(opt)) listedElements.push(opt);
        }
      }
    } catch (_) {}
    // Icon-only controls of the newest open overlay (a datepicker's prev/next arrows, a dialog's close
    // x): a link or button without an href (so the interactive selector misses it) that carries text, a
    // title or an aria-label is listed, bounded; with no text it is named by the attribute (derived_label).
    try {
      const overlays = queryAllComposed(
        ".ui-datepicker,dialog,[role='dialog'],[role='alertdialog'],[aria-modal='true'],.modal,.popover,.popup",
      ).filter(visible);
      const overlay = overlays[overlays.length - 1];
      if (overlay) {
        const listed = new Set(listedElements);
        let added = 0;
        for (const el of deepDescendants(overlay, "a,button,[role='button']")) {
          if (added >= 12) break;
          if (listed.has(el) || !visible(el)) continue;
          if (!clean(el.innerText || el.textContent || el.getAttribute("title") || el.getAttribute("aria-label") || "")) continue;
          listedElements.push(el);
          added += 1;
        }
      }
    } catch (_) {}
    // Whatever holds keyboard focus is always listed, even a custom widget (a tabindex listbox)
    // the interactive selector does not name: keys act on it.
    if (
      focusedNode
      && focusedNode.nodeType === 1
      && !["body", "html"].includes(String(focusedNode.tagName || "").toLowerCase())
      && !listedElements.includes(focusedNode)
      && visible(focusedNode)
    ) {
      listedElements.push(focusedNode);
    }
    // An accessible label for a control that names itself nowhere in text: aria-label, title, alt,
    // then (only when asked) its class tokens read as words ("ui-spinner-up" -> "spinner up").
    // Derived, never page-authored copy: the row carries it as `derived_label`, apart from `text`.
    const CLASS_WORD_NOISE = new Set([
      "ui", "state", "corner", "widget", "button", "btn", "icon", "default", "hover", "focus", "active",
      "all", "tl", "tr", "bl", "br", "top", "bottom", "left", "right", "helper", "reset", "clearfix",
    ]);
    const deriveLabel = (node, useClasses = true) => {
      try {
        const named = clean(node.getAttribute("aria-label") || node.getAttribute("title") || node.getAttribute("alt") || "");
        if (named) return named.slice(0, 80);
        if (!useClasses) return "";
        const raw = typeof node.className === "string" ? node.className : String(node.getAttribute("class") || "");
        const words = [];
        for (const token of raw.split(/\s+/)) {
          if (!token || /\d/.test(token) || /^ui-(state|corner|widget)/.test(token)) continue;
          for (const word of token.toLowerCase().split(/[^a-z]+/)) {
            if (word && !CLASS_WORD_NOISE.has(word) && !words.includes(word)) words.push(word);
          }
        }
        if (!words.length) {
          // An icon span often carries only an id ("send-reply"): its words are the page's own name for it.
          const rawId = clean(node.getAttribute("id")).toLowerCase();
          for (const word of rawId.split(/[^a-z]+/)) {
            if (word && !CLASS_WORD_NOISE.has(word) && !words.includes(word)) words.push(word);
          }
        }
        return words.join(" ").slice(0, 80);
      } catch (_) {
        return "";
      }
    };
    // options.form_outline (read_form): every form control of the document and its same-origin frames, on screen or
    // not, as the page holds it: label, kind, required and disabled flags, value or chosen options, and whether it is
    // in view. Read-only: nothing is scrolled, focused or changed. A sensitive control's value is masked and its
    // options are withheld, as in the viewport text. A row names its element by selector only, so the server can match
    // it to the latest observation's stable control id; the selector never reaches the brain.
    const formOutlineRows = () => {
      const CONTROL_SELECTOR = [
        "input", "select", "textarea", "[contenteditable='true']", "[role='combobox']", "[role='listbox']",
        "[role='checkbox']", "[role='radio']", "[role='switch']", "[role='textbox']", "[role='spinbutton']",
      ].join(",");
      const SKIPPED_INPUT_TYPES = ["hidden", "submit", "reset", "button", "image"];
      const DATE_TYPES = ["date", "datetime-local", "month", "time", "week"];
      const FORM_ELEMENT_CAP = 300;
      const OUTLINE_JSON_CAP = 20000;
      const limitText = (value, limit) => clean(value).slice(0, limit);
      const attrOf = (el, name) => clean(el.getAttribute(name));
      const tagOf = (el) => String(el.tagName || "").toLowerCase();
      const elements = [];
      const listed = new Set();
      for (const el of queryAllComposed(CONTROL_SELECTOR)) {
        if (elements.length >= FORM_ELEMENT_CAP) break;
        if (!el || listed.has(el)) continue;
        listed.add(el);
        if (tagOf(el) === "input" && SKIPPED_INPUT_TYPES.includes(attrOf(el, "type").toLowerCase())) continue;
        elements.push(el);
      }
      const kindOf = (el) => {
        const tag = tagOf(el);
        const role = attrOf(el, "role").toLowerCase();
        if (tag === "select") return el.multiple ? "multi_select" : "select";
        if (tag === "textarea") return "textarea";
        if (tag === "input") {
          const type = attrOf(el, "type").toLowerCase() || "text";
          if (DATE_TYPES.includes(type)) return "date";
          if (["text", "search", "email", "tel", "url"].includes(type) && (role === "combobox" || el.hasAttribute("list") || attrOf(el, "aria-autocomplete"))) return "combobox";
          return type;
        }
        if (role === "checkbox" || role === "switch") return "checkbox";
        if (role === "radio") return "radio";
        if (role === "combobox") return "combobox";
        if (role === "listbox") return "listbox";
        if (role === "spinbutton") return "number";
        if (el.isContentEditable || attrOf(el, "contenteditable").toLowerCase() === "true") return "rich_text";
        return "text";
      };
      const checkedOf = (el) => Boolean(el.checked) || attrOf(el, "aria-checked").toLowerCase() === "true";
      // The text of a <label> without the control it wraps: a wrapped <select>'s options are not the question.
      const labelTextOf = (label, control) => {
        const parts = [];
        try {
          const walker = label.ownerDocument.createTreeWalker(label, 4);
          for (let node = walker.nextNode(); node; node = walker.nextNode()) {
            if (control && control.contains(node)) continue;
            const parent = node.parentElement;
            if (!parent || ["script", "style", "noscript", "template"].includes(tagOf(parent))) continue;
            const text = clean(node.textContent);
            if (text) parts.push(text);
          }
        } catch (_) {}
        return clean(parts.join(" "));
      };
      // The inventory's label order (own text, then the associated label, then aria-label, placeholder, name); a
      // field's own text is its options or value, so it is not a label. A masked control keeps the attribute order only.
      const labelOf = (el, masked, hidden = false) => {
        let text = "";
        if (!masked) {
          // A hidden non-form element's own text is its value (a contenteditable), so it is never its label.
          if (!hidden && tagOf(el) !== "input" && tagOf(el) !== "select" && tagOf(el) !== "textarea") {
            text = clean(el.innerText || el.textContent || "");
          }
          if (!text) {
            try {
              const label = el.labels && el.labels[0];
              text = label ? labelTextOf(label, el) : "";
            } catch (_) {
              text = "";
            }
          }
        }
        text = text || attrOf(el, "aria-label") || attrOf(el, "placeholder") || attrOf(el, "name");
        const safe = limitText(redactSensitiveText(text), 180);
        if (safe) return safe;
        try {
          return limitText(redactSensitiveText(deriveLabel(el, true) || ""), 180);
        } catch (_) {
          return "";
        }
      };
      // The question a group of options sits under: its fieldset's legend, else the nearby text the inventory reads.
      const questionOf = (el) => {
        let legend = "";
        try {
          const fieldset = el.closest ? el.closest("fieldset") : null;
          const legendEl = fieldset ? fieldset.querySelector(":scope > legend") : null;
          legend = legendEl ? composedText(legendEl) : "";
        } catch (_) {
          legend = "";
        }
        return limitText(redactSensitiveText(legend || nearbyText(el)), 180);
      };
      // Not shown to the user: no box, type=hidden, visibility hidden or collapsed, the hidden attribute, aria-hidden,
      // or display:none on the control or a composed ancestor. A hidden control's value is never read into the
      // outline (value_withheld); a scrolled-away control is not hidden. Any failure to decide counts as hidden.
      const styleOf = (node) => (node.ownerDocument && node.ownerDocument.defaultView || window).getComputedStyle(node);
      const hiddenNowOf = (el) => {
        try {
          if (attrOf(el, "type").toLowerCase() === "hidden") return true;
          const rect = topRect(el);
          if (rect.width <= 0 && rect.height <= 0) return true;
          if (["hidden", "collapse"].includes(String(styleOf(el).visibility || ""))) return true;
          for (let node = el; node && node.nodeType === 1; node = composedParent(node)) {
            if (node.hasAttribute("hidden") || attrOf(node, "aria-hidden").toLowerCase() === "true") return true;
            if (styleOf(node).display === "none") return true;
          }
          return false;
        } catch (_) {
          return true;
        }
      };
      const placementOf = (el) => {
        const rect = topRect(el);
        return {
          inView: rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight
            && rect.right > 0 && rect.left < window.innerWidth,
          hidden: hiddenNowOf(el),
        };
      };
      const optionsOf = (el) => {
        const nodes = tagOf(el) === "select" ? Array.from(el.options || []) : Array.from(deepDescendants(el, "[role='option']") || []);
        const isSelected = (node) => Boolean(node.selected) || attrOf(node, "aria-selected").toLowerCase() === "true";
        return {
          rows: nodes.slice(0, 30).map((node) => ({
            label: limitText(redactSensitiveText(node.label || node.textContent || ""), 80),
            selected: isSelected(node),
          })),
          total: nodes.length,
          selectedCount: nodes.filter(isSelected).length,
        };
      };
      // A hidden control's free-text value, file names, chosen option and text are withheld and reported as present.
      // Its checked state (a checkbox, a radio, a group's selection) is not a secret and is reported.
      const rowOf = (el, kind, masked) => {
        const placement = placementOf(el);
        const hidden = placement.hidden;
        const row = {
          selector: stableSelector(el),
          tag: tagOf(el),
          kind,
          label: labelOf(el, masked, hidden),
          required: Boolean(el.required) || attrOf(el, "aria-required").toLowerCase() === "true",
          disabled: Boolean(el.disabled) || attrOf(el, "aria-disabled").toLowerCase() === "true",
          in_view: placement.inView,
          // Which form owns it (a final action's required check counts only its own form's fields).
          form: ownerOrdinal(el),
        };
        // The browser's own verdict on a required control: a select left on its placeholder is missing too.
        if (el.validity && el.validity.valueMissing) row.value_missing = true;
        if (hidden) row.hidden = true;
        if (masked) row.value_redacted = true;
        if (kind === "checkbox" || kind === "radio") {
          // A checked state is not a secret value: a hidden or zero-size box (a styled custom control) still reports it.
          if (masked) row.value = "[REDACTED]";
          else row.checked = checkedOf(el);
          return row;
        }
        if (kind === "file") {
          if (hidden) {
            if (el.files && el.files.length) row.value_withheld = "hidden";
            return row;
          }
          if (!masked) row.files = Array.from(el.files || []).slice(0, 10).map((file) => limitText(redactSensitiveText(file.name), 80));
          return row;
        }
        if (kind === "select" || kind === "multi_select" || kind === "listbox") {
          const options = optionsOf(el);
          if (hidden) {
            if (options.selectedCount > 0) row.value_withheld = "hidden";
            return row;
          }
          if (masked) {
            if (options.selectedCount > 0) row.value = "[REDACTED]";
            return row;
          }
          row.selected = options.rows.filter((option) => option.selected).map((option) => option.label).slice(0, 10);
          row.options = options.rows;
          if (options.total > options.rows.length) row.options_total = options.total;
          return row;
        }
        const tag = tagOf(el);
        const raw = (tag === "input" || tag === "textarea")
          ? String(el.value || "")
          : ((kind === "rich_text" || el.isContentEditable) ? String(el.innerText || el.textContent || "") : attrOf(el, "aria-valuetext"));
        if (hidden) {
          if (clean(raw)) row.value_withheld = "hidden";
          return row;
        }
        if (clean(raw)) {
          if (masked) row.value = "[REDACTED]";
          else {
            row.value = limitText(redactSensitiveText(raw), 200);
            if (clean(raw).length > 200) row.value_truncated = true;
          }
        }
        return row;
      };
      // Radio buttons (and checkboxes) that share a name in one form are one group of options.
      const ownerIds = new Map();
      const ownerOrdinal = (el) => {
        const owner = el.form || (el.getRootNode ? el.getRootNode() : document);
        if (!ownerIds.has(owner)) ownerIds.set(owner, ownerIds.size + 1);
        return ownerIds.get(owner);
      };
      const groupKeyOf = (el) => {
        const kind = kindOf(el);
        if (tagOf(el) !== "input" || (kind !== "radio" && kind !== "checkbox")) return "";
        const name = attrOf(el, "name");
        return name ? `${kind}|${ownerOrdinal(el)}|${name}` : "";
      };
      const groupRowOf = (group, kind) => {
        const masked = group.some((el) => sensitiveValueControl(el));
        const placements = group.map(placementOf);
        const row = {
          kind: kind === "radio" ? "radio_group" : "checkbox_group",
          label: questionOf(group[0]),
          required: group.some((el) => Boolean(el.required) || attrOf(el, "aria-required").toLowerCase() === "true"),
          disabled: group.every((el) => Boolean(el.disabled)),
          in_view: placements.some((placement) => placement.inView),
          form: ownerOrdinal(group[0]),
          ...(group.some((el) => el.validity && el.validity.valueMissing) ? { value_missing: true } : {}),
          options: group.slice(0, 30).map((el, index) => {
            const hidden = placements[index].hidden;
            const option = { label: labelOf(el, masked, hidden), selector: stableSelector(el) };
            if (!masked) option.checked = checkedOf(el);
            return option;
          }),
        };
        if (group.length > 30) row.options_total = group.length;
        if (placements.every((placement) => placement.hidden)) row.hidden = true;
        if (masked) {
          row.value_redacted = true;
          if (group.some(checkedOf)) row.value = "[REDACTED]";
        } else {
          row.selected = group.filter(checkedOf).map((el) => labelOf(el, false)).slice(0, 30);
        }
        return row;
      };
      const members = new Map();
      for (const el of elements) {
        const key = groupKeyOf(el);
        if (!key) continue;
        if (!members.has(key)) members.set(key, []);
        members.get(key).push(el);
      }
      const emitted = new Set();
      const rows = [];
      for (const el of elements) {
        const kind = kindOf(el);
        const key = groupKeyOf(el);
        const group = key ? members.get(key) : null;
        if (group && group.length > 1) {
          if (emitted.has(key)) continue;
          emitted.add(key);
          rows.push(groupRowOf(group, kind));
          continue;
        }
        rows.push(rowOf(el, kind, sensitiveValueControl(el)));
      }
      // The form a final action's target belongs to, by the same ordinals (null when the selector names no one element).
      let targetForm = null;
      const targetSelector = String((options && options.form_target_selector) || "").trim();
      if (targetSelector) {
        try {
          const matches = queryAllComposed(targetSelector);
          if (matches.length === 1) targetForm = ownerOrdinal(matches[0]);
        } catch (_) {
          targetForm = null;
        }
      }
      const out = [];
      let size = 0;
      for (const row of rows) {
        size += JSON.stringify(row).length;
        if (size > OUTLINE_JSON_CAP) return { rows: out, truncated: true, targetForm };
        out.push(row);
      }
      return { rows: out, truncated: false, targetForm };
    };
    if (formOutlineMode) {
      // A model-classified selector must still name exactly one element, as in the document text read.
      for (const semanticSelector of semanticSelectors) {
        if (queryAllComposed(semanticSelector).length !== 1) {
          return { form_outline: [], error: "sensitive_selector_drift" };
        }
      }
      const outline = formOutlineRows();
      return { form_outline: outline.rows, form_outline_truncated: outline.truncated, form_target: outline.targetForm };
    }
    // A slider whose position the page keeps only in layout (a jQuery-UI handle in its track): the
    // handle's share of the track along the track's long axis, as a derived value for keys or a drag.
    const deriveSliderFraction = (node) => {
      try {
        const role = clean(node.getAttribute("role")).toLowerCase();
        const classes = typeof node.className === "string" ? node.className : "";
        if (role !== "slider" && !/(^|\s)ui-slider-handle(\s|$)/.test(classes)) return "";
        if (clean(node.getAttribute("aria-valuenow"))) return "";
        const track = node.parentElement;
        if (!track) return "";
        const box = topRect(track);
        const handle = topRect(node);
        const horizontal = box.width >= box.height;
        const span = horizontal ? box.width : box.height;
        if (span <= 0) return "";
        const centre = horizontal ? (handle.left + handle.right) / 2 - box.left : (handle.top + handle.bottom) / 2 - box.top;
        const fraction = Math.max(0, Math.min(1, horizontal ? centre / span : 1 - centre / span));
        return `${Math.round(fraction * 100)}% along the track`;
      } catch (_) {
        return "";
      }
    };
    // Custom widgets (div menus, calendar cells, cards) carry no interactive tag or role; the page
    // marks them clickable with a pointer cursor. List the outermost visible pointer element with
    // short text of its own that is not already inside (or around) a listed control.
    // Drag surfaces (a dial knob, a slider thumb, a splitter, a draggable card) often carry no text
    // and no interactive tag; the page marks them with a grab, move, crosshair, or resize cursor, or
    // draggable. List the outermost visible one with its exact box, text or not, so a drag can start
    // at its real center. Raw style facts only: what either kind is for is the model's call. One walk
    // of the document finds both.
    try {
      const pointerElements = [];
      const dragElements = [];
      const listedSet = new Set(listedElements);
      for (const node of queryAllComposed("*")) {
        if (pointerElements.length >= 60 && dragElements.length >= 30) break;
        if (!node || node.nodeType !== 1 || listedSet.has(node)) continue;
        const tag = String(node.tagName || "").toLowerCase();
        if (["html", "body", "script", "style", "noscript", "template", "path"].includes(tag)) continue;
        let style = null;
        try { style = window.getComputedStyle(node); } catch (_) { style = null; }
        if (!style) continue;
        const pointerMarked = style.cursor === "pointer" && pointerElements.length < 60;
        const cursorDrag = DRAG_CURSOR_PATTERN.test(String(style.cursor || ""));
        const draggable = clean(node.getAttribute && node.getAttribute("draggable")).toLowerCase() === "true";
        const dragMarked = (cursorDrag || draggable) && dragElements.length < 30;
        if (!pointerMarked && !dragMarked) continue;
        const parent = composedParent(node);
        let parentCursor = "";
        try { parentCursor = parent && parent.nodeType === 1 ? String(window.getComputedStyle(parent).cursor || "") : ""; } catch (_) {}
        if (!visible(node)) continue;
        if (listedElements.some((listed) => composedContains(listed, node) || composedContains(node, listed))) continue;
        if (pointerMarked && parentCursor !== "pointer") {
          const ownText = clean((tag === "svg" ? "" : node.innerText || node.textContent) || (node.getAttribute && node.getAttribute("aria-label")) || deriveLabel(node, false));
          if (ownText && ownText.length <= 80) {
            pointerElements.push(node);
            // A pointer row whose children sit side by side, each with its own short text ("Reply" "Forward"),
            // is several controls: list each beside the row so an action can bind to the one it presses.
            try {
              const kids = Array.from(node.children || []).filter((kid) => visible(kid) && clean(kid.innerText || kid.textContent || "").length > 0
                && clean(kid.innerText || kid.textContent || "").length <= 40);
              if (kids.length >= 2 && kids.length <= 6) {
                const boxes = kids.map((kid) => topRect(kid)).sort((a, b) => a.left - b.left);
                const oneRow = boxes.every((box, i) => Math.abs(box.top - boxes[0].top) <= 6 && (i === 0 || box.left >= boxes[i - 1].right - 1));
                if (oneRow) {
                  for (const kid of kids) {
                    if (pointerElements.length < 60 && !pointerElements.includes(kid)) pointerElements.push(kid);
                  }
                }
              }
            } catch (_) {}
            continue;
          }
          // An icon-only clickable (a span or svg painted as an arrow, a trash can) has no text: the
          // page's own title, aria-label, alt, class or id words are its name. Small boxes only.
          const iconLabel = ownText || deriveLabel(node, true);
          if (iconLabel) {
            const box = topRect(node);
            if (box.width > 0 && box.height > 0 && box.width <= 96 && box.height <= 96) {
              pointerElements.push(node);
              continue;
            }
          }
        }
        if (dragMarked && !(cursorDrag && parentCursor === style.cursor)) dragElements.push(node);
      }
      listedElements.push(...pointerElements, ...dragElements);
    } catch (_) {}
    // What CSS actually paints for a control's label, as raw computed-style facts: generated
    // ::before/::after content, and whether the element's own text is invisible (transparent or
    // zero-size). A page can paint a different label than its DOM text; the model decides which
    // one the page means.
    const paintedLabel = (node) => {
      try {
        const own = window.getComputedStyle(node);
        const generated = (pseudo) => {
          let content = "";
          try { content = String(window.getComputedStyle(node, pseudo).content || ""); } catch (_) { content = ""; }
          if (!content || content === "none" || content === "normal") return "";
          return content.replace(/^["']|["']$/g, "").replace(/\\"/g, '"').slice(0, 120);
        };
        const before = generated("::before");
        const after = generated("::after");
        const color = String(own.color || "").replace(/\s+/g, "");
        const ownTextHidden = color === "transparent"
          || /^rgba\([^)]*,0(\.0+)?\)$/.test(color)
          || Number.parseFloat(own.fontSize || "1") === 0;
        if (!before && !after && !ownTextHidden) return null;
        return { before, after, own_text_hidden: ownTextHidden };
      } catch (_) {
        return null;
      }
    };
    // Scrollable regions (panels, nested holds, lists with their own scrollbar) are listed with
    // their raw scroll geometry, so the model can move one exactly by index and read where it is.
    // Structure only: which region matters is the model's call.
    const scrollGeometry = (node) => {
      try {
        const style = window.getComputedStyle(node);
        const overflowY = String(style.overflowY || style.overflow || "").toLowerCase();
        const overflowX = String(style.overflowX || style.overflow || "").toLowerCase();
        const canY = /^(auto|scroll|overlay)$/.test(overflowY) && node.scrollHeight > node.clientHeight + 1;
        const canX = /^(auto|scroll|overlay)$/.test(overflowX) && node.scrollWidth > node.clientWidth + 1;
        if (!canY && !canX) return null;
        return {
          top: Math.round(node.scrollTop),
          height: Math.round(node.scrollHeight),
          visible_height: Math.round(node.clientHeight),
          left: Math.round(node.scrollLeft),
          width: Math.round(node.scrollWidth),
          visible_width: Math.round(node.clientWidth),
        };
      } catch (_) {
        return null;
      }
    };
    try {
      const regionSet = new Set(listedElements);
      const regions = [];
      for (const node of queryAllComposed("*")) {
        if (regions.length >= 12) break;
        const tag = String(node && node.tagName || "").toLowerCase();
        if (!tag || ["html", "body", "script", "style", "noscript", "template"].includes(tag) || regionSet.has(node)) continue;
        if (!scrollGeometry(node) || !visible(node)) continue;
        regions.push(node);
      }
      listedElements.push(...regions);
    } catch (_) {}
    // One widget listed twice: a wrapper with no role of its own (a menu's <li role=presentation>)
    // and the element inside it that carries the role (<div role=menuitem>), same box and same text.
    // The role-bearing element is the control; the bare wrapper is dropped. Elements that both carry
    // a role (or neither does) are left alone, as is whatever holds keyboard focus.
    try {
      const roleBearing = (el) => {
        const tag = String(el.tagName || "").toLowerCase();
        const role = clean(el.getAttribute && el.getAttribute("role")).toLowerCase();
        if (role && role !== "presentation" && role !== "none") return true;
        return ["button", "select", "textarea", "input", "summary"].includes(tag) || (tag === "a" && el.hasAttribute("href"));
      };
      const textOf = (el) => clean(el.innerText || el.textContent || "");
      const sameBox = (a, b) => {
        const x = topRect(a);
        const y = topRect(b);
        return Math.abs(x.left - y.left) <= 2 && Math.abs(x.top - y.top) <= 2
          && Math.abs(x.right - y.right) <= 2 && Math.abs(x.bottom - y.bottom) <= 2;
      };
      const drop = new Set();
      for (const outer of listedElements) {
        if (roleBearing(outer) || outer === focusedNode) continue;
        const text = textOf(outer);
        if (!text) continue;
        const inner = listedElements.find((other) => other !== outer && roleBearing(other)
          && composedContains(outer, other) && textOf(other) === text && sameBox(outer, other));
        if (inner) drop.add(outer);
      }
      if (drop.size) {
        const kept = listedElements.filter((el) => !drop.has(el));
        listedElements.length = 0;
        listedElements.push(...kept);
      }
    } catch (_) {}
    // Dialog containers in document order (role dialog / alertdialog, aria-modal, <dialog>): raw containment,
    // so the server can fill the inventory from the newest open one. A closed <dialog> is not open.
    let dialogContainers = [];
    try {
      dialogContainers = queryAllComposed("dialog,[role='dialog'],[role='alertdialog'],[aria-modal='true']")
        .filter((node) => node && node.nodeType === 1 && !(node.tagName === "DIALOG" && !node.open) && visible(node));
    } catch (_) {}
    const dialogOrdinal = (el) => {
      let found = 0;
      try {
        dialogContainers.forEach((container, position) => {
          if (container === el || composedContains(container, el)) found = Math.max(found, position + 1);
        });
      } catch (_) {}
      return found;
    };
    const controls = listedElements
      .map((el, index) => {
        const rect = topRect(el);
        // A click at this control's center would not reach it: something is painted over it (an
        // open menu, a sticky bar, a modal), or it is clipped out of view inside a scrolled list so
        // the point hits the container around it. Raw hit-test geometry only; a hit on one of the
        // control's own descendants still reaches it.
        // The reason is kept apart: covered (another element is drawn over it), behind_modal (an open
        // dialog is drawn over it and it is outside that dialog), clipped (a container around it paints
        // where it should be: scrolled or overflow-hidden out of view), pointer_events_none (the page has
        // it not taking pointer input). Raw geometry and style only.
        let occluded = false;
        let covered = false;
        let clipped = false;
        let behindModal = false;
        let pointerEventsNone = false;
        let coveredBy = "";
        let coveredById = "";
        let coveredByText = "";
        try {
          const centerX = rect.left + rect.width / 2;
          const centerY = rect.top + rect.height / 2;
          if (centerX >= 0 && centerY >= 0 && centerX <= window.innerWidth && centerY <= window.innerHeight) {
            const hit = deepElementFromPoint(centerX, centerY);
            occluded = Boolean(hit && hit !== el && !composedContains(el, hit));
            if (occluded) {
              try { pointerEventsNone = window.getComputedStyle(el).pointerEvents === "none"; } catch (_) {}
              if (pointerEventsNone) {
                // what the pointer reaches instead is whatever lies beneath; it is not the cause
              } else if (composedContains(hit, el)) {
                clipped = true;
              } else if (dialogOrdinal(el) === 0 && dialogOrdinal(hit) > 0) {
                behindModal = true;
              } else {
                covered = true;
              }
              if (covered || behindModal) {
                const hitClasses = clean(typeof hit.className === "string" ? hit.className : "").split(" ").filter(Boolean).slice(0, 2);
                coveredBy = ((hit.tagName || "").toLowerCase() + (hitClasses.length ? "." + hitClasses.join(".") : "")).slice(0, 80);
                coveredById = clean(hit.id).slice(0, 40);
                coveredByText = clean(hit.innerText || hit.textContent || hit.getAttribute("aria-label") || "").slice(0, 80);
              }
            }
          }
        } catch (_) {}
        const computedStyle = (() => {
          try {
            return window.getComputedStyle(el);
          } catch (_) {
            return null;
          }
        })();
        const redactOwnedText = (value) => {
          if (Array.isArray(value)) {
            return value.map((item) => (
              item && typeof item === "object"
                ? { ...item, text: redactOwnedText(item.text) }
                : redactOwnedText(item)
            ));
          }
          return redactSensitiveText(value);
        };
        const context_text = redactOwnedText(popupOwnerText(el) || choiceGroupText(el) || contextText(rect, contextCandidates));
        const selector = stableSelector(el);
        const textFieldTag = ["input", "textarea"].includes((el.tagName || "").toLowerCase());
        const editableText = el.isContentEditable || clean(el.getAttribute("contenteditable")).toLowerCase() === "true";
        const valueSource = editableText ? (el.innerText || el.textContent || "") : (el.value || el.getAttribute("value"));
        // An editable text value is verbatim (leading/trailing spaces, runs, newlines): the exact string is the
        // task data. Other controls' values are labels and stay whitespace-normalized.
        const fullValue = textFieldTag || editableText ? String(valueSource || "") : clean(valueSource).trim();
        // A field's value is task data (text to copy, a draft to check): long text fields keep far more
        // than a label's worth, and any cut is reported on the row instead of passing as the whole value.
        const valueCap = ["input", "textarea"].includes((el.tagName || "").toLowerCase()) || el.isContentEditable ? 2000 : 180;
        const rawValue = fullValue.slice(0, valueCap);
        const valueTruncated = fullValue.length > valueCap;
        const valueRedacted = sensitiveValueControl(el);
        const row = {
          index: index + 1,
          // The origin of the frame document that owns this control, read from the browser's own record of that
          // document (never from page content); absent for the top document. A credential is only replayed into
          // the top document or a frame whose origin is the page's.
          ...frameOwnerFields(el),
          tag: (el.tagName || "").toLowerCase(),
          // Same rule the click's live identity uses, so an SVG descendant (text, path, g) is
          // expected and live as the same dispatch path.
          dispatch_path: ((n) => { const a = n.closest && n.closest('button,a[href],input,textarea,select,label,[role="button"],[role="option"],[role="radio"],[role="checkbox"],[role="combobox"],[tabindex]'); const s = n.closest && n.closest("canvas,svg"); return s && (!a || s === a) ? "cdp_trusted_click" : "dom_activation"; })(el),
          role: clean(el.getAttribute("role")),
          type: clean(el.getAttribute("type")),
          id: clean(el.getAttribute("id")).slice(0, 200),
          name: clean(el.getAttribute("name")).slice(0, 200),
          aria_controls: clean(el.getAttribute("aria-controls")).slice(0, 300),
          aria_owns: clean(el.getAttribute("aria-owns")).slice(0, 300),
          aria_labelledby: clean(el.getAttribute("aria-labelledby")).slice(0, 300),
          aria_describedby: clean(el.getAttribute("aria-describedby")).slice(0, 300),
          autocomplete: clean(el.getAttribute("autocomplete")).slice(0, 40),
          class_name: clean(
            typeof el.className === "string" ? el.className : el.getAttribute("class"),
          ).slice(0, 240),
          data_state: clean(el.getAttribute("data-state")).slice(0, 80),
          data_selected: clean(el.getAttribute("data-selected")).slice(0, 40),
          data_checked: clean(el.getAttribute("data-checked")).slice(0, 40),
          // Same naming order as every in-page probe: own text, then the associated <label>,
          // then aria-label / placeholder / name. (A label-wrapped radio is "Business", not its
          // group name "acct".)
          text: clean(
            valueRedacted
              ? (el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.getAttribute("name") || "")
              : (
                el.innerText
                || el.textContent
                || (
                  NAME_FROM_CONTENT_ROLES.has(clean(el.getAttribute("role")).toLowerCase())
                  || ["button", "a"].includes((el.tagName || "").toLowerCase())
                    ? composedText(el)
                    : ""
                )
                || (() => {
                  try {
                    const label = el.labels && el.labels[0];
                    return label ? composedText(label) : "";
                  } catch (_) {
                    return "";
                  }
                })()
                || el.getAttribute("aria-label")
                || el.getAttribute("placeholder")
                || el.getAttribute("name")
              ),
          ).slice(0, 180).trim(),
          value: valueRedacted && rawValue ? "[REDACTED]" : rawValue,
          ...(valueTruncated && !valueRedacted ? { value_truncated: true, value_length: fullValue.length } : {}),
          // A multi-select's .value is only its first selected option: every selected one is listed.
          ...((!valueRedacted && String(el.tagName || "").toLowerCase() === "select" && el.multiple)
            ? { selected_values: Array.from(el.selectedOptions || []).slice(0, 40).map((o) => clean(o.label || o.textContent).slice(0, 80)) }
            : {}),
          // A native select's option texts in order: the exact list a click or type naming one option is matched against
          // (the same match the select type path makes in the page). Absent on every other control.
          ...((!valueRedacted && String(el.tagName || "").toLowerCase() === "select")
            ? { options: Array.from(el.options || []).map((o) => clean(o.text || o.label || o.value)).filter(Boolean).slice(0, 40) }
            : {}),
          checked: Boolean(el.checked) || toggledOn(el),
          selected: Boolean(el.selected),
          aria_checked: clean(el.getAttribute("aria-checked")).slice(0, 40),
          aria_pressed: clean(el.getAttribute("aria-pressed")).slice(0, 40),
          aria_selected: clean(el.getAttribute("aria-selected")).slice(0, 40),
          aria_valuenow: clean(el.getAttribute("aria-valuenow")).slice(0, 40),
          aria_valuetext: clean(el.getAttribute("aria-valuetext")).slice(0, 80),
          aria_valuemin: clean(el.getAttribute("aria-valuemin")).slice(0, 40),
          aria_valuemax: clean(el.getAttribute("aria-valuemax")).slice(0, 40),
          disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true"),
          focused: el === focusedNode,
          occluded,
          covered,
          clipped,
          behind_modal: behindModal,
          pointer_events_none: pointerEventsNone,
          covered_by: coveredBy,
          covered_by_id: coveredById,
          covered_by_text: coveredByText,
          scroll: scrollGeometry(el),
          painted: paintedLabel(el),
          visual_state: computedStyle ? {
            background_color: clean(computedStyle.backgroundColor).slice(0, 80),
            color: clean(computedStyle.color).slice(0, 80),
            border_color: clean(computedStyle.borderColor).slice(0, 80),
            opacity: clean(computedStyle.opacity).slice(0, 40),
            font_weight: clean(computedStyle.fontWeight).slice(0, 40),
          } : {},
          x: Math.round(rect.left * dpr),
          y: Math.round(rect.top * dpr),
          width: Math.round(rect.width * dpr),
          height: Math.round(rect.height * dpr),
          nearby_text: redactOwnedText(nearbyText(el)),
          context_text,
          accessibility_context: redactOwnedText(accessibilityContext(el)),
          dom_context: redactOwnedText(domContext(el)),
          ancestor_chain: ancestorChain(el),
          stack_layer: stackLayer(el),
        };
        if (!row.text || (SHOWN_LABEL_ROLES.has(clean(el.getAttribute("role")).toLowerCase()) && (el.tagName || "").toLowerCase() !== "input")) {
          const shown = SHOWN_LABEL_ROLES.has(clean(el.getAttribute("role")).toLowerCase()) && (el.tagName || "").toLowerCase() !== "input"
            ? redactOwnedText(composedText(el)).slice(0, 80)
            : "";
          // An option whose own text is all template whitespace around a <slot> reads "" here (ServiceNow's
          // Race/Ethnicity rows, run 75, were offered by class name and the wrong row was picked by position):
          // its slotted words name it. `text` stays as every click-time identity check reads it.
          const slotted = !shown && !row.text && NAME_FROM_CONTENT_ROLES.has(clean(el.getAttribute("role")).toLowerCase())
            ? redactOwnedText(composedText(el)).slice(0, 80)
            : "";
          const derived = shown || slotted || (!row.text ? deriveLabel(el, true) : "");
          if (derived) row.derived_label = derived;
          // The control's own words, read through its slots, never attribute or class-name words: a choice judged
          // on them is judged on what the person sees beside it.
          if (shown || slotted) row.derived_label_visible = true;
        }
        const derivedFraction = deriveSliderFraction(el);
        if (derivedFraction) row.derived_value = derivedFraction;
        const inDialogOrdinal = dialogOrdinal(el);
        if (inDialogOrdinal) {
          row.in_dialog = true;
          row.dialog_ordinal = inDialogOrdinal;
        }
        // The record this control belongs to (its table row, list item, or grid row): several controls
        // can share one label ("Link", "Edit"), and only their row tells them apart.
        try {
          let rowEl = el.closest && el.closest("tr,[role='row'],li,[role='listitem']");
          if (!rowEl || rowEl === el) {
            // A card layout: the nearest ancestor that is one of several same-shaped siblings (a result
            // card among result cards) and says more than this control does.
            rowEl = null;
            const own = clean(el.innerText || el.textContent || "");
            let node = composedParent(el);
            for (let hops = 0; node && node.nodeType === 1 && hops < 6; hops += 1, node = composedParent(node)) {
              const parent = composedParent(node);
              if (!parent || parent === document.body || parent === document.documentElement) break;
              const shape = `${node.tagName}|${String(node.getAttribute("class") || "").split(/\s+/)[0]}`;
              let like = 0;
              for (const sibling of Array.from(parent.children || [])) {
                if (`${sibling.tagName}|${String(sibling.getAttribute("class") || "").split(/\s+/)[0]}` === shape) like += 1;
              }
              if (like >= 2 && like <= 200 && clean(node.innerText || node.textContent || "").length > own.length + 3) {
                rowEl = node;
                break;
              }
            }
          }
          if (rowEl && rowEl !== el) {
            const rowText = redactOwnedText(composedText(rowEl)).replace(/\s+/g, " ").trim().slice(0, 140);
            if (rowText && rowText !== row.text) row.row_text = rowText;
          }
        } catch (_) {}
        // Where a link goes and where a form posts: what the click-consequence judge reads to tell a
        // link or submit that leaves the page from one that stays.
        try {
          const href = clean(el.getAttribute("href")).slice(0, 300);
          if (href) row.href = href;
          const owningForm = el.form || (el.closest && el.closest("form"));
          const formAction = owningForm ? clean(owningForm.getAttribute("action")).slice(0, 300) : "";
          if (formAction) row.form_action = formAction;
          // The owning form itself (its position among the document's forms), stable for the document's life:
          // two forms may share an action, and a script-driven form may have none. Absent when the control has
          // no ancestor form, which the runtime reads as unknown.
          if (owningForm) {
            const formIndex = Array.prototype.indexOf.call(document.forms || [], owningForm);
            if (formIndex >= 0) row.form_id = `form${formIndex}`;
          }
        } catch (_) {}
        if (valueRedacted) {
          row.value_redacted = true;
          row.value_redaction_reason = modelClassifiedSensitiveValueControl(el)
            ? "model_classified_sensitive_control"
            : "sensitive_input_structure";
        }
        // An option of an open popup list that lies outside the list's own window: it exists but is not shown.
        if (popupOptionEls.has(el)) {
          try {
            let box = null;
            for (let node = composedParent(el); node && node.nodeType === 1; node = composedParent(node)) {
              if (scrollGeometry(node)) { box = topRect(node); break; }
            }
            const own = topRect(el);
            const outside = (box && (own.bottom <= box.top + 1 || own.top >= box.bottom - 1))
              || own.bottom <= 0 || own.top >= window.innerHeight;
            if (outside) row.list_position = "in list, scrolled out";
          } catch (_) {}
        }
        // How the page lets this be dragged: its raw cursor and draggable attribute.
        const affordances = [];
        if (computedStyle && DRAG_CURSOR_PATTERN.test(String(computedStyle.cursor || ""))) {
          affordances.push(`cursor:${computedStyle.cursor}`);
        }
        if (clean(el.getAttribute("draggable")).toLowerCase() === "true") affordances.push("draggable");
        if (affordances.length) row.affordances = affordances;
        if (selector && selector.length <= 200) row.selector = selector;
        return row;
      });
    // The focused multi-line text control's own text, line by line as `N|text` (raw: spaces and
    // underscores kept). The inventory carries a one-line excerpt of its value, which cannot show
    // which line an edit changes. Bounded to 40 lines / ~3000 characters, around the caret's line
    // when the text is longer. A sensitive control's text is never included.
    const focusedTextLines = (controlIndex) => {
      try {
        const el = focusedNode;
        if (!el || el.nodeType !== 1 || !(controlIndex > 0)) return null;
        const tag = String(el.tagName || "").toLowerCase();
        const editable = el.isContentEditable === true
          || clean(el.getAttribute("contenteditable")).toLowerCase() === "true";
        if (tag !== "textarea" && !editable) return null;
        if (sensitiveValueControl(el) || nodeTouchesSensitiveValue(el)) return null;
        const raw = String(
          tag === "textarea" ? (el.value || "") : (el.innerText || el.textContent || ""),
        ).replace(/\r\n?/g, "\n");
        if (!raw.includes("\n")) return null;
        // A textarea reports its caret exactly; an editable region's caret is located approximately
        // (rendered line breaks are not in a DOM range's text), so it only centres the window.
        let caretOffset = null;
        let caretExact = false;
        if (tag === "textarea") {
          const start = Number(el.selectionStart);
          if (Number.isFinite(start) && start >= 0) {
            caretOffset = start;
            caretExact = true;
          }
        } else {
          const doc = el.ownerDocument || document;
          const root = el.getRootNode ? el.getRootNode() : doc;
          const selection = (root && typeof root.getSelection === "function" ? root.getSelection() : null)
            || (typeof doc.getSelection === "function" ? doc.getSelection() : null);
          if (selection && selection.rangeCount && selection.focusNode && el.contains(selection.focusNode)) {
            const range = doc.createRange();
            range.selectNodeContents(el);
            range.setEnd(selection.focusNode, selection.focusOffset);
            const before = String(range.toString() || "").replace(/\s+/g, "");
            let seen = 0;
            let index = 0;
            while (index < raw.length && seen < before.length) {
              if (!/\s/.test(raw[index])) seen += 1;
              index += 1;
            }
            caretOffset = index;
          }
        }
        // Redaction as everywhere else: a line holding a sensitive control's value reads redacted, and
        // a value that only appears across lines withholds the whole view.
        const lines = raw.split("\n").map((line) => (
          sensitiveOwnedTexts.some((secret) => clean(line).includes(secret)) ? redactSensitiveText(line) : line
        ));
        if (sensitiveOwnedTexts.some((secret) => clean(lines.join(" ")).includes(secret))) return null;
        const LINE_CHARS = 400;
        const MAX_LINES = 40;
        const MAX_CHARS = 3000;
        const shown = lines.map((line) => (
          line.length <= LINE_CHARS ? line : `${line.slice(0, LINE_CHARS)}...[+${line.length - LINE_CHARS} chars]`
        ));
        const count = shown.length;
        const caretLine = caretOffset == null ? 0 : raw.slice(0, caretOffset).split("\n").length;
        const anchor = caretLine ? Math.min(Math.max(caretLine, 1), count) - 1 : 0;
        let start = anchor;
        let end = anchor + 1;
        let used = shown[anchor].length + 1;
        while (end - start < MAX_LINES) {
          let grew = false;
          if (end < count && used + shown[end].length + 1 <= MAX_CHARS) {
            used += shown[end].length + 1;
            end += 1;
            grew = true;
          }
          if (end - start < MAX_LINES && start > 0 && used + shown[start - 1].length + 1 <= MAX_CHARS) {
            start -= 1;
            used += shown[start].length + 1;
            grew = true;
          }
          if (!grew) break;
        }
        const view = {
          control: controlIndex,
          line_count: count,
          lines: shown.slice(start, end).map((line, offset) => `${start + offset + 1}|${line}`),
        };
        if (caretExact && caretLine) view.caret_line = anchor + 1;
        return view;
      } catch (_) {
        return null;
      }
    };
    // Where the caret is in the focused text field, as the desktop body reports a native one: its
    // line's text, column, and selection. A page that pre-selects part of a value (a rename editor
    // selecting the name before ".txt") shows here what a keystroke would replace.
    const focusedCaret = () => {
      try {
        const el = focusedNode;
        if (!el || el.nodeType !== 1) return null;
        const tag = String(el.tagName || "").toLowerCase();
        if (tag !== "input" && tag !== "textarea") return null;
        if (sensitiveValueControl(el) || nodeTouchesSensitiveValue(el)) return null;
        let start = null;
        let end = null;
        try {
          start = el.selectionStart;
          end = el.selectionEnd;
        } catch (_) { return null; }
        if (typeof start !== "number" || typeof end !== "number") return null;
        const value = String(el.value || "").replace(/\r\n?/g, "\n");
        const lineStart = start > 0 ? value.lastIndexOf("\n", start - 1) + 1 : 0;
        const lineBreak = value.indexOf("\n", start);
        const line = value.slice(lineStart, lineBreak < 0 ? value.length : lineBreak);
        const selected = value.slice(start, end);
        if (sensitiveOwnedTexts.some((secret) => clean(`${line} ${selected}`).includes(secret))) return null;
        return {
          line_text: line.slice(0, 200),
          column: start - lineStart,
          selection_length: Math.max(0, end - start),
          selected_text: selected.slice(0, 200),
        };
      } catch (_) {
        return null;
      }
    };
    const focusedListedIndex = focusedNode ? listedElements.indexOf(focusedNode) : -1;
    const focusedLines = focusedListedIndex >= 0 ? focusedTextLines(focusedListedIndex + 1) : null;
    const caret = focusedCaret();
    const signatures = documentSignatures();
    // Controls the page renders but the viewport does not show (above or below it), as a light list: a
    // label, a kind, and how far away. They are not in `controls` (nothing there acts on them without a
    // scroll); the server summarizes them so the brain knows what lies off screen and where.
    const offScreenControls = [];
    try {
      const shown = new Set(listedElements);
      for (const el of selectedElements) {
        if (offScreenControls.length >= 40) break;
        if (shown.has(el)) continue;
        const rect = topRect(el);
        const style = window.getComputedStyle(el);
        if (!rect || rect.width <= 0 || rect.height <= 0 || style.visibility === "hidden" || style.display === "none") continue;
        if (rect.right < 0 || rect.left > window.innerWidth) continue;
        const above = rect.bottom < 0;
        if (!above && rect.top <= window.innerHeight) continue;
        if (isGridCell(el)) continue;
        const redacted = sensitiveValueControl(el);
        const label = clean(
          redacted
            ? (el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.getAttribute("name") || "")
            : (
              el.innerText || el.textContent
              || (el.labels && el.labels[0] ? composedText(el.labels[0]) : "")
              || el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.getAttribute("name") || ""
            ),
        ).slice(0, 60).trim();
        offScreenControls.push({
          tag: (el.tagName || "").toLowerCase(),
          role: clean(el.getAttribute("role")),
          type: clean(el.getAttribute("type")),
          text: redactSensitiveText(label),
          side: above ? "above" : "below",
          distance: Math.round((above ? -rect.bottom : rect.top - window.innerHeight) * dpr),
        });
      }
    } catch (_) {}
    // The short headings, labels and buttons just below the viewport (within three screens): the server
    // compares them with the previous step's to say when content appeared only under the fold.
    const belowFoldText = [];
    try {
      for (const node of queryAllComposed("h1,h2,h3,h4,h5,h6,[role='heading'],legend,th,button,label,p")) {
        if (belowFoldText.length >= 12) break;
        const rect = topRect(node);
        if (!rect || rect.width <= 0 || rect.height <= 0 || rect.top < window.innerHeight || rect.top > window.innerHeight * 4) continue;
        if (nodeTouchesSensitiveValue(node)) continue;
        const text = redactSensitiveText(clean(node.innerText || node.textContent || "")).slice(0, 60);
        if (text && !belowFoldText.includes(text)) belowFoldText.push(text);
      }
    } catch (_) {}
    // Formatted spans (bold, italic, underline, strike, mark) in the viewport with their EXACT text,
    // punctuation included ("Neque," not "Neque"): viewport_text flattens formatting. A separate field;
    // viewport_text and everything that quotes it are untouched.
    const emphasizedText = [];
    try {
      const styleOf = (el) => {
        const tag = String(el.tagName || "").toLowerCase();
        if (tag === "b" || tag === "strong") return "bold";
        if (tag === "i" || tag === "em") return "italic";
        if (tag === "u") return "underline";
        if (tag === "s" || tag === "del") return "strike";
        if (tag === "mark") return "mark";
        const css = window.getComputedStyle(el);
        const weight = css.fontWeight === "bold" || css.fontWeight === "bolder" ? 700 : parseInt(css.fontWeight, 10);
        if (weight >= 600) return "bold";
        if (css.fontStyle === "italic" || css.fontStyle === "oblique") return "italic";
        const deco = String(css.textDecorationLine || css.textDecoration || "");
        if (deco.includes("underline")) return "underline";
        if (deco.includes("line-through")) return "strike";
        return "";
      };
      // Text a person cannot see is never listed (as viewport_text drops it): a near-transparent ancestor,
      // a transparent text colour, a sub-3px font, or a hidden element. Bounded: the on-screen rect check
      // runs before any computed style, and at most 2000 elements are visited.
      const invisible = (el) => {
        try {
          const css = window.getComputedStyle(el);
          if (css.visibility === "hidden" || css.display === "none" || parseFloat(css.fontSize) < 3) return true;
          const color = String(css.color || "").replace(/\s+/g, "").toLowerCase();
          const clipText = String(css.webkitBackgroundClip || css.backgroundClip || "").toLowerCase() === "text";
          if (!clipText && (color === "transparent" || /^rgba\(\d+,\d+,\d+,0(\.0+)?\)$/.test(color))) return true;
          let opacity = 1;
          for (let node = el, depth = 0; node && node !== document.documentElement && depth < 40; node = node.parentElement, depth += 1) {
            const raw = parseFloat(String(window.getComputedStyle(node).opacity ?? "1"));
            if (Number.isFinite(raw)) opacity *= raw;
            if (opacity < 0.1) return true;
          }
        } catch (_) {
          return true;
        }
        return false;
      };
      const seen = new Set();
      let visited = 0;
      for (const el of queryAllComposed("body *")) {
        if (emphasizedText.length >= 30 || visited >= 2000) break;
        visited += 1;
        const tag = String(el.tagName || "").toLowerCase();
        if (["script", "style", "noscript", "template", "button", "a", "input", "textarea", "select", "option", "label", "th", "summary", "h1", "h2", "h3", "h4", "h5", "h6"].includes(tag)) continue;
        const rect = topRect(el);
        if (!rect || rect.width <= 0 || rect.height <= 0 || rect.bottom < 0 || rect.top > window.innerHeight || rect.right < 0 || rect.left > window.innerWidth) continue;
        if (el.closest && el.closest("h1,h2,h3,h4,h5,h6,[role='heading'],button,[role='button'],input,textarea,select")) continue;
        const style = styleOf(el);
        if (!style) continue;
        if (invisible(el)) continue;
        if (nodeTouchesSensitiveValue(el)) continue;
        const raw = String(el.innerText || el.textContent || "").replace(/[\r\n]+/g, " ");
        const safe = raw.trim() ? redactSensitiveText(raw) : "";
        const text = (safe === clean(raw) ? raw.trim() : safe).slice(0, 120);
        if (!text || seen.has(`${style}|${text}`)) continue;
        seen.add(`${style}|${text}`);
        emphasizedText.push({ text, style });
      }
    } catch (_) {}
    return {
      viewport_text: redactedViewportText().slice(0, 4500),
      controls,
      ...(belowFoldText.length ? { below_fold_text: belowFoldText } : {}),
      ...(emphasizedText.length ? { emphasized_text: emphasizedText } : {}),
      ...(offScreenControls.length ? { off_screen_controls: offScreenControls } : {}),
      ...(focusedLines ? { focused_text_lines: focusedLines } : {}),
      ...(caret ? { caret } : {}),
      document_state_signature: signatures.state,
      document_value_signature: signatures.values,
      document_semantic_signature: signatures.semantic,
      document_row_digests: signatures.rows,
      document_lifecycle_token: documentLifecycleToken,
    };
  } catch (error) {
    return { viewport_text: "", controls: [], error: String(error) };
  }
}

async function captureVisibleViewportWithDebugger(tabId, quality = 55, lossless = false) {
  const target = { tabId };
  let attachedHere = false;
  let attachError = null;
  try {
    try {
      await withTimeout(
        new Promise((resolve, reject) => {
          acquireDebugger(target).then(() => {
            attachedHere = true;
            resolve();
          }, reject);
        }),
        2000,
        "debugger attach timed out",
      );
    } catch (error) {
      // A previous Lumen primitive can still own the debugger channel briefly. In that case the
      // command below proves whether this extension already owns the attachment.
      attachError = error;
    }
    const result = await withTimeout(
      new Promise((resolve, reject) => {
        chrome.debugger.sendCommand(
          target,
          "Page.captureScreenshot",
          lossless
            ? { format: "png", fromSurface: true, captureBeyondViewport: false }
            : {
              format: "jpeg",
              quality: Math.max(1, Math.min(100, Math.round(Number(quality) || 55))),
              fromSurface: true,
              captureBeyondViewport: false,
            },
          (response) => {
            const err = chrome.runtime.lastError;
            if (err) reject(new Error(err.message));
            else resolve(response || {});
          },
        );
      }),
      6000,
      "Page.captureScreenshot timed out",
    );
    const data = String((result && result.data) || "").trim();
    if (!data) throw new Error("Page.captureScreenshot returned no image data");
    return {
      ok: true,
      data_url: `data:image/${lossless ? "png" : "jpeg"};base64,${data}`,
      mode: "debugger_capture",
      reused_debugger_attachment: Boolean(attachError && !attachedHere),
    };
  } catch (error) {
    const prefix = attachError ? `${errorMessage(attachError)}; ` : "";
    return { ok: false, error: prefix + errorMessage(error), mode: "debugger_capture" };
  } finally {
    if (attachedHere) releaseDebugger(target);
  }
}

function bindSensitiveScreenshotControlsInPage(selectors, documentToken) {
  try {
    const queryAll = typeof globalThis.__lumenDeepQueryAll === "function"
      ? globalThis.__lumenDeepQueryAll
      : (value) => Array.from(document.querySelectorAll(value));
    const requested = Array.isArray(selectors)
      ? selectors.map((selector) => String(selector || "").trim()).filter(Boolean)
      : [];
    const token = String(documentToken || "").trim();
    if (!requested.length || !token) {
      return { ok: false, failure_reason: "sensitive_mask_binding_required" };
    }
    let matchCount = 0;
    for (const exactSelector of requested) {
      let matches;
      try {
        matches = Array.from(queryAll(exactSelector));
      } catch (_) {
        return { ok: false, failure_reason: "sensitive_mask_selector_invalid" };
      }
      if (matches.length !== 1) {
        return {
          ok: false,
          failure_reason: "sensitive_mask_selector_drift",
          match_count: matches.length,
        };
      }
      matchCount += 1;
    }
    Object.defineProperty(globalThis, "__lumenSensitiveScreenshotDocumentToken", {
      value: token,
      configurable: true,
      enumerable: false,
      writable: false,
    });
    return {
      ok: true,
      selector_bound: true,
      match_count: matchCount,
      bound_selector_count: requested.length,
    };
  } catch (_) {
    return { ok: false, failure_reason: "sensitive_mask_binding_failed" };
  }
}

async function runMarkSensitiveScreenshotControl(
  tabId,
  selectorOrSelectors,
  expectedPageContext,
  bindingMode = "mark",
) {
  const requestedSelectors = normalizeSensitiveScreenshotSelectors(
    selectorOrSelectors,
  );
  const expected = expectedPageContext && typeof expectedPageContext === "object"
    ? expectedPageContext
    : {};
  const ensureExistingOrRebind = bindingMode === "ensure";
  const action = ensureExistingOrRebind
    ? "ensure_sensitive_screenshot_control"
    : "mark_sensitive_screenshot_control";
  if (!tabId || !requestedSelectors.length) {
    return {
      ok: false,
      action,
      failure_reason: "sensitive_mask_selector_required",
      selector_bound: false,
    };
  }
  const expectedUrl = String(expected.url || "").trim();
  const expectedSurface = String(expected.surface_signature || "").trim();
  const expectedMaskSurface = String(expected.mask_surface_signature || "").trim();
  if (
    !expectedUrl
    || (ensureExistingOrRebind ? !expectedMaskSurface : !expectedSurface)
  ) {
    return {
      ok: false,
      action,
      failure_reason: "sensitive_mask_page_context_required",
      selector_bound: false,
    };
  }
  const pageVerdict = ensureExistingOrRebind
    ? await observedSensitiveMaskPageContextVerdict(
      tabId,
      requestedSelectors,
      expected,
    )
    : await observedPageContextVerdict(tabId, expected);
  if (!pageVerdict.ok) {
    clearTransientSensitiveScreenshotBinding();
    return {
      ok: false,
      action,
      failure_reason: pageVerdict.reason || "sensitive_mask_page_context_drift",
      page_context_mismatch_field: pageVerdict.mismatch_field || "",
      selector_bound: false,
    };
  }
  const existing = transientSensitiveScreenshotBinding;
  const reuseExisting = Boolean(
    existing
    && Number(existing.tab_id) === Number(tabId)
    && String(existing.expected_url || "") === expectedUrl
    && (
      !ensureExistingOrRebind
      || String(existing.expected_mask_surface_signature || "") === expectedMaskSurface
    )
    && existing.document_token
    && existing.selectors instanceof Set
  );
  const documentToken = reuseExisting
    ? String(existing.document_token)
    : (
      globalThis.crypto && typeof globalThis.crypto.randomUUID === "function"
        ? globalThis.crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}`
    );
  const selectors = reuseExisting ? new Set(existing.selectors) : new Set();
  for (const selector of requestedSelectors) selectors.add(selector);
  const selectorsToBind = Array.from(selectors);
  let rows;
  try {
    rows = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId },
        func: bindSensitiveScreenshotControlsInPage,
        args: [selectorsToBind, documentToken],
      }),
      2500,
      "sensitive screenshot control binding timed out",
    );
  } catch (_) {
    clearTransientSensitiveScreenshotBinding();
    return {
      ok: false,
      action,
      failure_reason: "sensitive_mask_binding_failed",
      selector_bound: false,
    };
  }
  const result = (rows && rows[0] && rows[0].result) || {};
  if (!result.ok || result.selector_bound !== true) {
    clearTransientSensitiveScreenshotBinding();
    return {
      ok: false,
      action,
      failure_reason: result.failure_reason || "sensitive_mask_binding_failed",
      match_count: Number(result.match_count || 0),
      selector_bound: false,
    };
  }
  const boundPage = await tabPageEvidence(tabId, Array.from(selectors));
  const boundMaskSurface = String(boundPage.mask_surface_signature || "").trim();
  if (
    String(boundPage.url || "").trim() !== expectedUrl
    || !boundMaskSurface
    || (ensureExistingOrRebind && boundMaskSurface !== expectedMaskSurface)
  ) {
    clearTransientSensitiveScreenshotBinding();
    return {
      ok: false,
      action,
      failure_reason: "sensitive_mask_page_context_drift",
      page_context_mismatch_field: (
        String(boundPage.url || "").trim() !== expectedUrl
          ? "url"
          : "mask_surface_signature"
      ),
      selector_bound: false,
    };
  }
  transientSensitiveScreenshotBinding = {
    tab_id: Number(tabId),
    expected_url: expectedUrl,
    expected_mask_surface_signature: boundMaskSurface,
    document_token: documentToken,
    selectors,
  };
  return {
    ok: true,
    action,
    selector_bound: true,
    page_context_bound: true,
    mask_surface_signature: boundMaskSurface,
    marked_control_count: selectors.size,
    bound_selectors: Array.from(selectors),
    binding_reused: reuseExisting,
    binding_rebound: !reuseExisting,
  };
}

async function runEnsureSensitiveScreenshotControl(tabId, selectors, expectedPageContext) {
  return runMarkSensitiveScreenshotControl(
    tabId,
    selectors,
    expectedPageContext,
    "ensure",
  );
}

function nativeDialogObservation(tabId) {
  // The page cannot run scripts or repaint while a browser dialog is open, so its last atomic
  // observation is still its exact state; the dialog is added as evidence beside it.
  const dialog = publicNativeDialog(nativeDialogOpenFor(tabId));
  if (!dialog) return null;
  const cached = lastObservationByTab.get(Number(tabId));
  if (!cached) {
    return {
      ok: false,
      action: "screenshot",
      failure_reason: "native_dialog_open_without_observation",
      native_dialog: dialog,
      error: "A browser dialog is open and the page has no prior observation.",
    };
  }
  return {
    ...cached,
    captured_at: Date.now(),
    native_dialog: dialog,
    page_evidence: { ...(cached.page_evidence || {}), native_dialog: dialog },
  };
}

async function runScreenshotWithSensitiveMaskRebind(params) {
  const commandParams = params && typeof params === "object" ? params : {};
  const dialogTabId = await activeTabId();
  if (dialogTabId) {
    // Hold the tab's debugger session from the first observation: its Page domain reports
    // browser dialogs, and the viewport keeps one size for the whole run.
    try {
      await acquireDebugger({ tabId: dialogTabId });
      releaseDebugger({ tabId: dialogTabId });
    } catch (_) {}
    const dialogObservation = nativeDialogObservation(dialogTabId);
    if (dialogObservation) return dialogObservation;
  }
  const bindingProvided = Object.prototype.hasOwnProperty.call(
    commandParams,
    "required_sensitive_mask_binding",
  );
  const requiredBinding = (
    commandParams.required_sensitive_mask_binding
    && typeof commandParams.required_sensitive_mask_binding === "object"
  )
    ? commandParams.required_sensitive_mask_binding
    : null;
  if (bindingProvided && !requiredBinding) {
    return {
      ok: false,
      action: "screenshot",
      failure_reason: "sensitive_mask_page_context_required",
      image_discarded: true,
      sensitive_mask_rebind_required: true,
    };
  }
  if (requiredBinding) {
    const reboundTabId = await activeTabId();
    const requiredSelectors = normalizeSensitiveScreenshotSelectors(
      requiredBinding.selectors || requiredBinding.selector,
    );
    const ensured = await runEnsureSensitiveScreenshotControl(
      reboundTabId,
      requiredSelectors,
      requiredBinding.expected_page_context || null,
    );
    if (
      !ensured
      || !ensured.ok
      || ensured.selector_bound !== true
      || ensured.page_context_bound !== true
    ) {
      return {
        ok: false,
        action: "screenshot",
        failure_reason: (
          (ensured && ensured.failure_reason)
          || "sensitive_mask_rebind_failed"
        ),
        page_context_mismatch_field: (
          (ensured && ensured.page_context_mismatch_field)
          || ""
        ),
        image_discarded: true,
        sensitive_mask_rebind_required: true,
      };
    }
    return withScreenshotCaptureLock(() => doScreenshot({
      tab_id: reboundTabId,
      selectors: requiredSelectors,
      expected_page_context: requiredBinding.expected_page_context || null,
    }, { lossless: commandParams.lossless === true }));
  }
  return withScreenshotCaptureLock(() => doScreenshot(null, { lossless: commandParams.lossless === true }));
}

// The run's own tab's whole rendered text, read where the tab is: no activation, focus, scroll,
// debugger, or capture, so the real screen the run is working on is undisturbed. While the run works
// on the desktop surface the backend reads it each step to keep the task page's text current.
// Only the run's target tab (never another tab); redacted in-page by the rules the screenshot's
// evidence uses, with every model-classified sensitive selector of the tab's binding (and of the
// backend's required binding) still naming exactly one element, or nothing is returned.
async function runReadRunTabText(params) {
  const commandParams = params && typeof params === "object" ? params : {};
  const action = "read_run_tab_text";
  const tabId = await activeTabId();
  if (!tabId) return { ok: false, action, failure_reason: "run_tab_unavailable" };
  // A browser dialog freezes the page's scripts; its text is unreadable until it is answered.
  if (nativeDialogOpenFor(tabId)) return { ok: false, action, failure_reason: "native_dialog_open" };
  const bindingProvided = Object.prototype.hasOwnProperty.call(
    commandParams,
    "required_sensitive_mask_binding",
  );
  const requiredBinding = (
    commandParams.required_sensitive_mask_binding
    && typeof commandParams.required_sensitive_mask_binding === "object"
  )
    ? commandParams.required_sensitive_mask_binding
    : null;
  if (bindingProvided && !requiredBinding) {
    return { ok: false, action, failure_reason: "sensitive_mask_page_context_required" };
  }
  const maskContext = sensitiveScreenshotMaskContextForTab(tabId);
  const boundSelectors = Array.isArray(maskContext && maskContext.selectors) ? maskContext.selectors : [];
  if (maskContext && maskContext.required && !boundSelectors.length) {
    return { ok: false, action, failure_reason: "sensitive_mask_binding_lost" };
  }
  const requiredSelectors = requiredBinding
    ? normalizeSensitiveScreenshotSelectors(requiredBinding.selectors || requiredBinding.selector)
    : [];
  if (requiredBinding && !requiredSelectors.length) {
    return { ok: false, action, failure_reason: "sensitive_mask_selector_required" };
  }
  const selectors = Array.from(new Set([...boundSelectors, ...requiredSelectors].map((item) => String(item))));
  const maxChars = Math.max(500, Math.min(20000, Math.round(Number(commandParams.max_chars) || 6000)));
  let evidence;
  try {
    const [row] = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId },
        func: _visionPageEvidence,
        args: [selectors, { document_text: true, max_chars: maxChars }],
      }),
      4000,
      "run tab text timed out",
    );
    evidence = (row && row.result) || {};
  } catch (error) {
    return {
      ok: false,
      action,
      failure_reason: "run_tab_text_unavailable",
      error: errorMessage(error).slice(0, 240),
    };
  }
  if (evidence.error || typeof evidence.document_text !== "string") {
    return {
      ok: false,
      action,
      failure_reason: evidence.error ? String(evidence.error).slice(0, 80) : "run_tab_text_unavailable",
    };
  }
  return {
    ok: true,
    action,
    tab_id: tabId,
    url: String(evidence.url || ""),
    title: String(evidence.title || ""),
    text: evidence.document_text,
    truncated: evidence.document_text_truncated === true,
  };
}

async function runVisionCommandWithSensitiveMaskRebind(
  params,
  method,
  operation,
) {
  const commandParams = params && typeof params === "object" ? params : {};
  const bindingProvided = Object.prototype.hasOwnProperty.call(
    commandParams,
    "required_sensitive_mask_binding",
  );
  const requiredBinding = (
    commandParams.required_sensitive_mask_binding
    && typeof commandParams.required_sensitive_mask_binding === "object"
  )
    ? commandParams.required_sensitive_mask_binding
    : null;
  if (!bindingProvided) return operation();
  if (!requiredBinding) {
    return {
      ok: false,
      action: String(method || "").replace(/^vision_/, "").split("_")[0] || "execute",
      failure_reason: "sensitive_mask_page_context_required",
      browser_mutation_attempted: false,
      operation_result_discarded: true,
      sensitive_mask_rebind_required: true,
    };
  }
  const tabId = await activeTabId();
  const selectors = normalizeSensitiveScreenshotSelectors(
    requiredBinding.selectors || requiredBinding.selector,
  );
  const expectedPageContext = requiredBinding.expected_page_context || null;
  const action = String(method || "").replace(/^vision_/, "").split("_")[0] || "execute";
  const ensured = await runEnsureSensitiveScreenshotControl(
    tabId,
    selectors,
    expectedPageContext,
  );
  if (
    !ensured
    || !ensured.ok
    || ensured.selector_bound !== true
    || ensured.page_context_bound !== true
  ) {
    return {
      ok: false,
      action,
      failure_reason: (
        (ensured && ensured.failure_reason)
        || "sensitive_mask_rebind_failed"
      ),
      page_context_mismatch_field: (
        (ensured && ensured.page_context_mismatch_field)
        || ""
      ),
      browser_mutation_attempted: false,
      operation_result_discarded: true,
      sensitive_mask_rebind_required: true,
    };
  }
  let result;
  try {
    result = await operation();
  } catch (_) {
    return {
      ok: false,
      action,
      failure_reason: "sensitive_guarded_command_failed",
      operation_result_discarded: true,
      sensitive_mask_rebind_required: true,
    };
  }
  const postVerdict = await observedSensitiveMaskPageContextVerdict(
    tabId,
    selectors,
    expectedPageContext,
  );
  if (!postVerdict.ok) {
    // The operation may have changed the document or same-URL surface. Do not return action
    // verification assembled after semantic redaction was lost or became stale.
    const browserMutationAttempted = Boolean(
      result
      && (
        result.browser_mutation_attempted === true
        || result.activation_attempted === true
        || result.upload_attempted === true
        || result.ok === true
      )
    );
    clearTransientSensitiveScreenshotBinding();
    return {
      ok: false,
      action,
      failure_reason: (
        postVerdict.reason
        || "sensitive_mask_page_context_drift"
      ),
      page_context_mismatch_field: postVerdict.mismatch_field || "",
      operation_attempted: true,
      browser_mutation_attempted: browserMutationAttempted,
      execution_outcome_unknown: browserMutationAttempted,
      operation_result_discarded: true,
      sensitive_mask_rebind_required: true,
    };
  }
  return result;
}

function maskSensitiveScreenshotRegionsInPage(semanticBinding = null) {
  try {
    document.querySelectorAll("[data-lumen-sensitive-screenshot-mask]").forEach((node) => node.remove());
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const sensitive = (node) => {
      const tag = (node && node.tagName || "").toLowerCase();
      const inputType = clean(node && node.getAttribute && node.getAttribute("type")).toLowerCase();
      if (tag === "input" && inputType === "password") return true;
      const tokens = clean(node && node.getAttribute && node.getAttribute("autocomplete"))
        .toLowerCase()
        .split(/\s+/)
        .filter(Boolean);
      return tokens.some((token) => (
        token === "current-password"
        || token === "new-password"
        || token === "one-time-code"
        || token === "transaction-amount"
        || token === "transaction-currency"
        || token === "webauthn"
        || token.startsWith("cc-")
      ));
    };
    const semantic = semanticBinding && typeof semanticBinding === "object" ? semanticBinding : null;
    const isTopFrame = (() => {
      try { return window === window.top; } catch (_) { return false; }
    })();
    const semanticAppliesToFrame = Boolean(semantic && (!semantic.top_frame_only || isTopFrame));
    const semanticSelectors = semanticAppliesToFrame && Array.isArray(semantic.selectors)
      ? semantic.selectors.map((selector) => String(selector || "").trim()).filter(Boolean)
      : [];
    if (semanticAppliesToFrame && semantic.required) {
      const expectedToken = String(semantic.document_token || "");
      const expectedUrl = String(semantic.expected_url || "");
      const expectedMaskSurface = String(semantic.expected_mask_surface_signature || "");
      const liveToken = String(globalThis.__lumenSensitiveScreenshotDocumentToken || "");
      if (!expectedToken || !expectedUrl || !expectedMaskSurface || !semanticSelectors.length) {
        return { ok: false, count: 0, rectangles: [], error: "sensitive_mask_context_required" };
      }
      if (String(location.href || "") !== expectedUrl || liveToken !== expectedToken) {
        return { ok: false, count: 0, rectangles: [], error: "sensitive_mask_page_context_drift" };
      }
    }
    const semanticNodes = [];
    const queryAllComposed = (selector) => {
      if (typeof globalThis.__lumenDeepQueryAll === "function") {
        return globalThis.__lumenDeepQueryAll(selector);
      }
      return Array.from(document.querySelectorAll(selector));
    };
    for (const selector of semanticSelectors) {
      let matches;
      try {
        matches = queryAllComposed(selector);
      } catch (_) {
        return { ok: false, count: 0, rectangles: [], error: "sensitive_mask_selector_invalid" };
      }
      if (matches.length !== 1) {
        return { ok: false, count: 0, rectangles: [], error: "sensitive_mask_selector_drift" };
      }
      semanticNodes.push(matches[0]);
    }
    const maskNodes = Array.from(new Set([
      ...queryAllComposed("input,textarea,[contenteditable='true'],[autocomplete]")
        .filter(sensitive),
      ...semanticNodes,
    ]));
    const rectangles = [];
    maskNodes
      .forEach((node) => {
        const rect = (typeof globalThis.__lumenTopRect === "function" ? globalThis.__lumenTopRect(node) : node.getBoundingClientRect());
        if (!rect || rect.width <= 0 || rect.height <= 0) return;
        if (rect.right <= 0 || rect.bottom <= 0 || rect.left >= window.innerWidth || rect.top >= window.innerHeight) return;
        const left = Math.max(0, rect.left);
        const top = Math.max(0, rect.top);
        const right = Math.min(window.innerWidth, rect.right);
        const bottom = Math.min(window.innerHeight, rect.bottom);
        const mask = document.createElement("div");
        mask.setAttribute("data-lumen-sensitive-screenshot-mask", "true");
        mask.setAttribute("aria-hidden", "true");
        Object.assign(mask.style, {
          position: "fixed",
          left: `${left}px`,
          top: `${top}px`,
          width: `${Math.max(1, right - left)}px`,
          height: `${Math.max(1, bottom - top)}px`,
          background: "#111111",
          opacity: "1",
          zIndex: "2147483647",
          pointerEvents: "none",
          color: "transparent",
          boxShadow: "none",
          filter: "none",
        });
        (document.documentElement || document.body).appendChild(mask);
        rectangles.push({
          x: Math.round(left),
          y: Math.round(top),
          width: Math.round(Math.max(1, right - left)),
          height: Math.round(Math.max(1, bottom - top)),
        });
      });
    return { ok: true, count: rectangles.length, rectangles };
  } catch (error) {
    return { ok: false, count: 0, rectangles: [], error: String(error) };
  }
}

function clearSensitiveScreenshotRegionsInPage() {
  try {
    const masks = Array.from(document.querySelectorAll("[data-lumen-sensitive-screenshot-mask]"));
    masks.forEach((node) => node.remove());
    return { ok: true, removed: masks.length };
  } catch (error) {
    return { ok: false, removed: 0, error: String(error) };
  }
}

function sensitiveScreenshotMaskPaintBarrierInPage() {
  return new Promise((resolve) => {
    if (typeof requestAnimationFrame !== "function") {
      resolve({
        ok: false,
        mask_count: 0,
        error: "animation_frame_unavailable",
      });
      return;
    }
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        try {
          resolve({
            ok: true,
            mask_count: document.querySelectorAll(
              "[data-lumen-sensitive-screenshot-mask]",
            ).length,
          });
        } catch (_) {
          resolve({
            ok: false,
            mask_count: 0,
            error: "sensitive_mask_paint_probe_failed",
          });
        }
      });
    });
  });
}

async function targetSurfaceForegroundVerdict(tabId, windowId) {
  try {
    const [tab, browserWindow] = await Promise.all([
      chrome.tabs.get(tabId),
      chrome.windows.get(windowId),
    ]);
    const tabActive = Boolean(
      tab
      && tab.active
      && Number(tab.windowId) === Number(windowId)
    );
    const windowFocused = Boolean(browserWindow && browserWindow.focused);
    return {
      ok: tabActive && windowFocused,
      tab_active: tabActive,
      window_focused: windowFocused,
      window_id: Number(windowId),
    };
  } catch (error) {
    return {
      ok: false,
      tab_active: false,
      window_focused: false,
      window_id: Number(windowId),
      error: errorMessage(error),
    };
  }
}

async function acquireTargetSurfaceAndWaitForPaint(tabId, requireVerifiedPaint = false) {
  let windowId = null;
  try {
    const tab = await chrome.tabs.get(tabId);
    windowId = Number(tab && tab.windowId);
    if (!Number.isFinite(windowId)) {
      return { ok: false, reason: "target_window_unavailable" };
    }
    // captureVisibleTab captures the active tab of a window, but an already-active tab can still
    // belong to an unfocused Chrome window whose compositor has not painted recent DOM changes.
    // Acquire the two pieces independently; tab.active alone is not a sufficient pixel-freshness
    // guarantee.
    await chrome.tabs.update(tabId, { active: true });
    await chrome.windows.update(windowId, { focused: true });
    const foreground = await targetSurfaceForegroundVerdict(tabId, windowId);
    if (!foreground.ok) {
      return {
        ...foreground,
        ok: false,
        reason: "target_surface_not_foreground",
      };
    }
    // Two animation frames prove the compositor painted the latest DOM. Frames stop entirely
    // when the OS treats the window as hidden (minimized, fully occluded, or a focus request
    // Windows refused), so a barrier timeout is a fact about window visibility, not a broken
    // page: restore the window once, then continue with the paint marked unverified rather
    // than failing the whole run. Sensitive-mask captures pass requireVerifiedPaint and still
    // fail closed.
    let painted = {};
    let barrierError = "";
    // A window the OS is not painting stays unpainted for a while; once a barrier has timed out,
    // the next captures probe briefly instead of re-waiting 3s each (sensitive-mask captures,
    // which must prove paint, keep the full wait).
    const recentlyStalled = !requireVerifiedPaint
      && Date.now() - Number(globalThis.__lumenPaintStalledAt || 0) < 20000;
    const barrierAttempts = recentlyStalled ? 1 : 2;
    const barrierTimeoutMs = recentlyStalled ? 400 : 1500;
    for (let attempt = 0; attempt < barrierAttempts && !painted.ok; attempt += 1) {
      try {
        const rows = await withTimeout(
          chrome.scripting.executeScript({
            target: { tabId },
            func: sensitiveScreenshotMaskPaintBarrierInPage,
          }),
          barrierTimeoutMs,
          "target surface paint barrier timed out",
        );
        painted = (rows && rows[0] && rows[0].result) || {};
        barrierError = String(painted.error || "");
      } catch (error) {
        painted = {};
        barrierError = errorMessage(error);
        try {
          await chrome.windows.update(windowId, { state: "normal", focused: true });
          await chrome.tabs.update(tabId, { active: true });
        } catch (_) {}
      }
    }
    globalThis.__lumenPaintStalledAt = painted.ok ? 0 : Date.now();
    if (!painted.ok) {
      if (requireVerifiedPaint) {
        return {
          ok: false,
          reason: "target_surface_paint_unverified",
          window_id: windowId,
          error: barrierError,
        };
      }
      const unverified = await targetSurfaceForegroundVerdict(tabId, windowId);
      return {
        ...unverified,
        ok: true,
        paint_barrier_verified: false,
        paint_barrier_error: barrierError,
        window_id: windowId,
      };
    }
    const afterPaint = await targetSurfaceForegroundVerdict(tabId, windowId);
    if (!afterPaint.ok) {
      return {
        ...afterPaint,
        ok: false,
        reason: "target_surface_lost_during_paint",
      };
    }
    return {
      ...afterPaint,
      ok: true,
      paint_barrier_verified: true,
      window_id: windowId,
    };
  } catch (error) {
    return {
      ok: false,
      reason: "target_surface_acquisition_failed",
      window_id: windowId,
      error: errorMessage(error),
    };
  }
}

async function proveSensitiveScreenshotMasksPainted(tabId, expectedFrameMasks) {
  const expectedRows = Array.isArray(expectedFrameMasks) ? expectedFrameMasks : [];
  const maskedRows = expectedRows
    .map((expected, index) => ({
      frameId: Number.isFinite(Number(expected && expected.frame_id))
        ? Number(expected.frame_id)
        : (index === 0 ? 0 : -1),
      expectedCount: Number((expected && expected.result && expected.result.count) || 0),
    }))
    .filter((row) => row.frameId >= 0 && row.expectedCount > 0);
  // A frame with no injected mask has no sensitive pixels whose paint must be proven. Hidden
  // zero-mask frames may never receive requestAnimationFrame callbacks, so waiting on them can
  // deadlock an otherwise safe capture. Every frame that actually has a mask remains fail-closed.
  if (!maskedRows.length) return true;
  let rows;
  try {
    rows = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId, frameIds: maskedRows.map((row) => row.frameId) },
        func: sensitiveScreenshotMaskPaintBarrierInPage,
      }),
      1500,
      "sensitive screenshot mask paint barrier timed out",
    );
  } catch (_) {
    return false;
  }
  const paintedRows = Array.isArray(rows) ? rows : [];
  if (paintedRows.length !== maskedRows.length || paintedRows.some((row) => !(row && row.result && row.result.ok))) {
    return false;
  }
  return maskedRows.every(({ frameId, expectedCount }, index) => {
    const painted = paintedRows.find((row, paintedIndex) => (
      Number.isFinite(Number(row && row.frameId))
        ? Number(row.frameId) === frameId
        : paintedIndex === index
    ));
    return Boolean(
      painted
      && painted.result
      && Number(painted.result.mask_count || 0)
        >= expectedCount
    );
  });
}

async function screenshotDataSha256(dataUrl) {
  const encoded = String(dataUrl || "").includes(",")
    ? String(dataUrl || "").slice(String(dataUrl || "").indexOf(",") + 1)
    : String(dataUrl || "");
  if (!encoded) return "";
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest)).map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

// A chained click's own earlier click leaves its mark (a selection highlight, a ticked box). The
// viewport may then differ from the observed screenshot only inside the padded boxes of those earlier
// clicks, and only there: every pixel elsewhere must match, and the click about to land must lie outside
// them. Decodes the observed image (cached at observation) and the live capture, so it never relaxes
// anything when either is unavailable.
async function pixelsMatchOutsideRegions(tabId, expectedHash, liveDataUrl, regions, point) {
  try {
    const cached = lastObservationByTab.get(Number(tabId));
    if (!cached || !cached.image || String(cached.screenshot_sha256 || "").toLowerCase() !== expectedHash) {
      return { ok: false, why: "observed_image_unavailable" };
    }
    const pad = 16;
    const boxes = (Array.isArray(regions) ? regions : []).slice(0, 8).map((r) => ({
      x0: Math.floor(Number(r.x) - pad), y0: Math.floor(Number(r.y) - pad),
      x1: Math.ceil(Number(r.x) + Number(r.width) + pad), y1: Math.ceil(Number(r.y) + Number(r.height) + pad),
    })).filter((b) => [b.x0, b.y0, b.x1, b.y1].every(Number.isFinite));
    if (!boxes.length) return { ok: false, why: "no_regions" };
    const inside = (b, x, y) => x >= b.x0 && x < b.x1 && y >= b.y0 && y < b.y1;
    if (!point || !Number.isFinite(Number(point.x)) || !Number.isFinite(Number(point.y))) {
      return { ok: false, why: "click_point_unknown" };
    }
    if (boxes.some((b) => inside(b, Number(point.x), Number(point.y)))) {
      return { ok: false, why: "target_inside_prior_click_region" };
    }
    const decode = async (dataUrl) => {
      const blob = await (await fetch(dataUrl)).blob();
      const bitmap = await createImageBitmap(blob);
      const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(bitmap, 0, 0);
      return ctx.getImageData(0, 0, bitmap.width, bitmap.height);
    };
    const a = await decode(`data:image/jpeg;base64,${cached.image}`);
    const b = await decode(liveDataUrl);
    if (a.width !== b.width || a.height !== b.height) return { ok: false, why: "size_changed" };
    let outside = 0;
    for (let y = 0; y < a.height; y += 1) {
      for (let x = 0; x < a.width; x += 1) {
        if (boxes.some((box) => inside(box, x, y))) continue;
        const i = (y * a.width + x) * 4;
        if (
          Math.abs(a.data[i] - b.data[i]) > 3
          || Math.abs(a.data[i + 1] - b.data[i + 1]) > 3
          || Math.abs(a.data[i + 2] - b.data[i + 2]) > 3
        ) outside += 1;
      }
    }
    return outside === 0 ? { ok: true, regions: boxes.length } : { ok: false, why: "changed_outside_prior_click", pixels: outside };
  } catch (error) {
    return { ok: false, why: "compare_failed" };
  }
}

async function captureMaskedViewportFingerprint(tabId, expectedPageContext) {
  const expected = expectedPageContext && typeof expectedPageContext === "object"
    ? expectedPageContext
    : {};
  const expectedHash = String(expected.screenshot_sha256 || "").trim().toLowerCase();
  const expectedMode = String(expected.screenshot_mode || "").trim();
  const expectedQuality = Math.max(1, Math.min(100, Math.round(Number(expected.screenshot_quality) || 0)));
  if (!expectedHash || !expectedMode || !expectedQuality) {
    return { ok: false, reason: "coordinate_pixel_context_required" };
  }
  const semanticMaskContext = sensitiveScreenshotMaskContextForTab(tabId);
  const initialSurface = await acquireTargetSurfaceAndWaitForPaint(tabId, true);
  if (!initialSurface.ok) {
    return {
      ok: false,
      reason: "coordinate_pixel_recapture_failed",
      surface_failure_reason: initialSurface.reason || "target_surface_acquisition_failed",
      error: String(initialSurface.error || ""),
    };
  }
  let windowId = initialSurface.window_id;
  // A focused text control can make two otherwise identical viewport captures alternate only
  // because the native caret blinked. Keep this recovery temporal and exact: a bounded set of
  // fresh captures may recover only when one SHA-256 is byte-for-byte the observed screenshot.
  // There is deliberately no pixel threshold, crop, or perceptual comparison here.
  const maxCaptureAttempts = 3;
  // Chrome rate-limits captureVisibleTab across the whole extension. The observation immediately
  // preceding a final action can consume that budget, so each bounded retry must also leave enough
  // time for the browser capture quota to recover. This remains an exact-hash check; the delay only
  // recovers the screenshot tool and never relaxes the approval binding.
  const captureRetryDelayMs = 650;
  let captureAttempts = 0;
  let liveHash = "";
  let lastCaptureError = "";
  let lastLiveDataUrl = "";
  let captureErrorRetryAttempted = false;
  try {
    for (let attempt = 1; attempt <= maxCaptureAttempts; attempt += 1) {
      if (attempt > 1) {
        // Span both phases of a normal caret blink without making the action wait unboundedly.
        // The prior masks remain mounted during this pause; the next all-frame injection refreshes
        // and re-proves them immediately before the fresh capture.
        await new Promise((resolve) => setTimeout(resolve, captureRetryDelayMs));
      }
      const maskRows = await withTimeout(
        chrome.scripting.executeScript({
          target: { tabId, allFrames: true },
          func: maskSensitiveScreenshotRegionsInPage,
          args: [semanticMaskContext],
        }),
        2500,
        "coordinate screenshot masking timed out",
      );
      const maskResults = Array.isArray(maskRows) ? maskRows : [];
      if (
        !maskResults.length
        || maskResults.some((row) => !(row && row.result && row.result.ok))
      ) {
        return { ok: false, reason: "coordinate_screenshot_mask_failed" };
      }
      const expectedFrameMasks = maskResults.map((row, index) => ({
        frame_id: Number.isFinite(Number(row && row.frameId))
          ? Number(row.frameId)
          : (index === 0 ? 0 : -1),
        result: (row && row.result) || { ok: false, count: 0 },
      }));
      if (!await proveSensitiveScreenshotMasksPainted(tabId, expectedFrameMasks)) {
        return { ok: false, reason: "coordinate_screenshot_mask_paint_unverified" };
      }
      const captureSurface = await acquireTargetSurfaceAndWaitForPaint(tabId, true);
      if (!captureSurface.ok) {
        return {
          ok: false,
          reason: "coordinate_pixel_recapture_failed",
          surface_failure_reason: captureSurface.reason || "target_surface_acquisition_failed",
          error: String(captureSurface.error || ""),
        };
      }
      windowId = captureSurface.window_id;
      captureAttempts = attempt;
      let dataUrl = "";
      if (expectedMode === "capture_visible_tab") {
        try {
          dataUrl = await withTimeout(
            new Promise((resolve, reject) => {
              chrome.tabs.captureVisibleTab(windowId, { format: "jpeg", quality: expectedQuality }, (url) => {
                const err = chrome.runtime.lastError;
                if (err) reject(new Error(err.message));
                else resolve(url);
              });
            }),
            4000,
            "coordinate screenshot recapture timed out",
          );
          lastCaptureError = "";
        } catch (error) {
          lastCaptureError = errorMessage(error);
          captureErrorRetryAttempted = captureErrorRetryAttempted || attempt < maxCaptureAttempts;
          if (attempt < maxCaptureAttempts) continue;
          break;
        }
      } else if (expectedMode === "debugger_capture") {
        const captured = await captureVisibleViewportWithDebugger(tabId, expectedQuality);
        if (!captured.ok || !captured.data_url) {
          return {
            ok: false,
            reason: "coordinate_pixel_recapture_failed",
            error: String(captured.error || ""),
          };
        }
        dataUrl = captured.data_url;
      } else {
        return { ok: false, reason: "coordinate_pixel_context_required" };
      }
      const capturedForeground = await targetSurfaceForegroundVerdict(tabId, windowId);
      if (!capturedForeground.ok) {
        return {
          ok: false,
          reason: "coordinate_pixel_recapture_failed",
          surface_failure_reason: "target_surface_lost_during_capture",
          capture_attempts: captureAttempts,
        };
      }
      liveHash = await screenshotDataSha256(dataUrl);
      lastLiveDataUrl = dataUrl;
      if (liveHash && liveHash === expectedHash) {
        return {
          ok: true,
          expected_screenshot_sha256: expectedHash,
          live_screenshot_sha256: liveHash,
          capture_attempts: captureAttempts,
          capture_error_retry_attempted: captureErrorRetryAttempted,
          capture_error_recovered: captureErrorRetryAttempted,
          temporal_exact_hash_retry_attempted: captureAttempts > 1,
          temporal_exact_hash_recovered: captureAttempts > 1,
        };
      }
    }
  } catch (error) {
    return {
      ok: false,
      reason: "coordinate_pixel_recapture_failed",
      error: errorMessage(error),
      capture_attempts: captureAttempts,
      temporal_exact_hash_retry_attempted: captureAttempts > 1,
      temporal_exact_hash_recovered: false,
    };
  } finally {
    try {
      await withTimeout(
        chrome.scripting.executeScript({
          target: { tabId, allFrames: true },
          func: clearSensitiveScreenshotRegionsInPage,
        }),
        2500,
        "coordinate screenshot mask cleanup timed out",
      );
    } catch (_) {}
  }
  if (!liveHash && lastCaptureError) {
    return {
      ok: false,
      reason: "coordinate_pixel_recapture_failed",
      error: lastCaptureError,
      capture_attempts: captureAttempts,
      capture_error_retry_attempted: captureErrorRetryAttempted,
      capture_error_recovered: false,
      temporal_exact_hash_retry_attempted: captureAttempts > 1,
      temporal_exact_hash_recovered: false,
    };
  }
  let priorRegionVerdict = null;
  const priorRegions = Array.isArray(expected.chain_prior_click_regions) ? expected.chain_prior_click_regions : [];
  if (liveHash && lastLiveDataUrl && priorRegions.length) {
    priorRegionVerdict = await pixelsMatchOutsideRegions(
      tabId, expectedHash, lastLiveDataUrl, priorRegions, expected.chain_click_point,
    );
    if (priorRegionVerdict.ok) {
      return {
        ok: true,
        expected_screenshot_sha256: expectedHash,
        live_screenshot_sha256: liveHash,
        capture_attempts: captureAttempts,
        pixel_change_confined_to_prior_click: true,
        prior_click_regions: priorRegionVerdict.regions,
      };
    }
  }
  return {
    ok: false,
    reason: "coordinate_pixel_drift",
    ...(priorRegionVerdict ? { prior_click_region_check: priorRegionVerdict.why || "" } : {}),
    expected_screenshot_sha256: expectedHash,
    live_screenshot_sha256: liveHash,
    capture_attempts: captureAttempts,
    temporal_exact_hash_retry_attempted: captureAttempts > 1,
    temporal_exact_hash_recovered: false,
  };
}

// Resolves once the page's scroll position is unchanged between polls and no finite CSS animation or
// transition is still running (an endless one is ignored), or after maxMs. Never throws: a page that
// cannot be probed is captured as it is.
async function settlePageMotion(tabId, maxMs = 900) {
  try {
    const [row] = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId },
        func: async (limit) => {
          const started = performance.now();
          let last = "";
          while (performance.now() - started < limit) {
            const root = document.scrollingElement || document.documentElement;
            const position = [window.scrollX, window.scrollY, root ? root.scrollTop : 0].join(",");
            let moving = 0;
            try {
              for (const animation of document.getAnimations()) {
                const timing = animation.effect && animation.effect.getComputedTiming && animation.effect.getComputedTiming();
                const remaining = timing && Number.isFinite(timing.endTime) ? timing.endTime - Number(timing.localTime || 0) : 0;
                if (animation.playState === "running" && remaining > 16 && timing.endTime <= 2000) moving += 1;
              }
            } catch (_) {}
            if (position === last && !moving) return { settled: true, waited: Math.round(performance.now() - started) };
            last = position;
            await new Promise((resolve) => setTimeout(resolve, 50));
          }
          return { settled: false, waited: Math.round(performance.now() - started) };
        },
        args: [maxMs],
      }),
      maxMs + 600,
      "page motion settle timed out",
    );
    return (row && row.result) || {};
  } catch (_) {
    return {};
  }
}

async function doScreenshot(requiredSensitiveCapture = null, captureOptions = null) {
  // lossless: the same masked, atomic observation captured as PNG - what a zoom crops from, so
  // faint fine print is not lost to JPEG compression before it is enlarged.
  const lossless = Boolean(captureOptions && captureOptions.lossless);
  // Phase timings ride on every result so a slow observation names the phase that stalled.
  const captureStarted = Date.now();
  const captureTiming = {};
  const markCapture = (phase) => { captureTiming[phase] = Date.now() - captureStarted; };
  const result = await (async () => {
    const tabId = await activeTabId();
    if (!tabId) return { ok: false, error: "No application tab to screenshot." };
    const semanticMaskContext = sensitiveScreenshotMaskContextForTab(tabId);
    const requiredCapture = (
      requiredSensitiveCapture
      && typeof requiredSensitiveCapture === "object"
    )
      ? requiredSensitiveCapture
      : null;
    if (requiredCapture) {
      const expectedContext = (
        requiredCapture.expected_page_context
        && typeof requiredCapture.expected_page_context === "object"
      )
        ? requiredCapture.expected_page_context
        : {};
      const requiredSelectors = normalizeSensitiveScreenshotSelectors(
        requiredCapture.selectors || requiredCapture.selector,
      );
      const liveSelectors = Array.isArray(semanticMaskContext && semanticMaskContext.selectors)
        ? semanticMaskContext.selectors.map((selector) => String(selector || "").trim())
        : [];
      if (
        Number(requiredCapture.tab_id) !== Number(tabId)
        || !semanticMaskContext
        || semanticMaskContext.required !== true
        || !requiredSelectors.length
        || requiredSelectors.some((selector) => !liveSelectors.includes(selector))
        || String(semanticMaskContext.expected_url || "")
          !== String(expectedContext.url || "")
        || String(semanticMaskContext.expected_mask_surface_signature || "")
          !== String(expectedContext.mask_surface_signature || "")
      ) {
        return {
          ok: false,
          action: "screenshot",
          failure_reason: "sensitive_mask_rebind_lost_before_capture",
          retryable: false,
          image_discarded: true,
          sensitive_mask_rebind_required: true,
        };
      }
    }
    const semanticSensitiveSelectors = Array.isArray(semanticMaskContext && semanticMaskContext.selectors)
      ? semanticMaskContext.selectors
      : [];
    let windowId;
    let state = {};
    try {
      const tab = await chrome.tabs.get(tabId);
      windowId = tab.windowId;
      // captureVisibleTab grabs the ACTIVE tab of the window — so the application tab must be foreground, or
      // we'd screenshot whatever the user is looking at (e.g. the Lumen chat) and hand the model the wrong
      // page. Bring the target tab forward and let it paint before capturing.
      const acquired = await acquireTargetSurfaceAndWaitForPaint(tabId);
      if (!acquired.ok) {
        return {
          ok: false,
          action: "screenshot",
          failure_reason: "screenshot_target_surface_unavailable",
          target_surface_failure_reason: acquired.reason || "target_surface_acquisition_failed",
          retryable: true,
          image_discarded: true,
          error: String(acquired.error || ""),
        };
      }
      windowId = acquired.window_id;
      markCapture("acquire_surface");
      // A click's own consequence is often an animation: a smooth scroll, a transition, a panel sliding
      // in. The fixed settle pause before this capture is shorter than those, so the screenshot showed
      // the page before the motion while the page evidence already said the surface changed (v6_shop
      // run 1796: after Start the top looked unchanged). Wait, bounded, until the scroll position holds
      // and finite animations have ended.
      await settlePageMotion(tabId);
      markCapture("settle_motion");
    } catch (_) {
      return { ok: false, error: "Screenshot tab not found." };
    }
    try {
      const [stateProbe, evidenceProbe] = await Promise.all([
        (async () => {
          try {
            const [r] = await withTimeout(
              chrome.scripting.executeScript({ target: { tabId }, func: _pageState }),
              3000,
              "page state timed out",
            );
            return { ok: true, value: (r && r.result) || {} };
          } catch (error) {
            return { ok: false, value: {}, error: errorMessage(error) };
          }
        })(),
        (async () => {
          try {
            const [r] = await withTimeout(
              chrome.scripting.executeScript({
                target: { tabId },
                func: _visionPageEvidence,
                args: [semanticSensitiveSelectors],
              }),
              3000,
              "vision page evidence timed out",
            );
            return { ok: true, value: (r && r.result) || {} };
          } catch (error) {
            return { ok: false, value: {}, error: errorMessage(error) };
          }
        })(),
      ]);
      if (!stateProbe.ok || !evidenceProbe.ok) {
        return {
          ok: false,
          action: "screenshot",
          failure_reason: "screenshot_observation_evidence_unavailable",
          evidence_stage: "initial_observation",
          page_state_probe_complete: Boolean(stateProbe.ok),
          surface_probe_complete: Boolean(evidenceProbe.ok),
          retryable: true,
          image_discarded: true,
          error: String(stateProbe.error || evidenceProbe.error || "").slice(0, 240),
        };
      }
      state = stateProbe.value;
      markCapture("page_probes");
      const pageEvidence = evidenceProbe.value;
      const initialSurfaceSignature = surfaceSignatureFromEvidence(pageEvidence);
      const initialStableSurfaceSignature = stableSurfaceSignatureFromEvidence(pageEvidence);
      const initialDocumentStateSignature = String(pageEvidence.document_state_signature || "");
      const initialDocumentLifecycleToken = String(pageEvidence.document_lifecycle_token || "");
      const preCaptureState = await tabPageEvidence(tabId);
      markCapture("pre_capture_state");
      if (
        preCaptureState.page_state_probe_complete === false
        || preCaptureState.surface_probe_complete === false
        || preCaptureState.tab_metadata_probe_complete === false
      ) {
        return {
          ok: false,
          action: "screenshot",
          failure_reason: "screenshot_observation_evidence_unavailable",
          evidence_stage: "pre_capture_context",
          tab_metadata_probe_complete: preCaptureState.tab_metadata_probe_complete !== false,
          page_state_probe_complete: preCaptureState.page_state_probe_complete !== false,
          surface_probe_complete: preCaptureState.surface_probe_complete !== false,
          retryable: true,
          image_discarded: true,
        };
      }
      const expectedMaskSurface = String(
        (semanticMaskContext && semanticMaskContext.expected_mask_surface_signature) || "",
      ).trim();
      if (
        semanticMaskContext
        && semanticMaskContext.required
        && (
          !expectedMaskSurface
          || String(preCaptureState.url || "").trim()
            !== String(semanticMaskContext.expected_url || "").trim()
          || String(preCaptureState.mask_surface_signature || "").trim() !== expectedMaskSurface
        )
      ) {
        return {
          ok: false,
          action: "screenshot",
          failure_reason: "sensitive_mask_page_context_drift",
          retryable: false,
          image_discarded: true,
          page_context_mismatch_field: (
            String(preCaptureState.url || "").trim()
              !== String(semanticMaskContext.expected_url || "").trim()
              ? "url"
              : "mask_surface_signature"
          ),
        };
      }
      const initialContext = {
        url: state.url || "",
        surface_signature: initialSurfaceSignature,
        stable_surface_signature: initialStableSurfaceSignature,
        document_state_signature: initialDocumentStateSignature,
        document_lifecycle_token: initialDocumentLifecycleToken,
      };
      let volatileObservation = false;
      if (!screenshotObservationContextMatches(initialContext, preCaptureState)) {
        if (!screenshotDriftTolerable(initialContext, preCaptureState, expectedMaskSurface)) {
          return {
            ok: false,
            action: "screenshot",
            failure_reason: "screenshot_observation_drift",
            retryable: true,
            image_discarded: true,
            before_url: state.url || "",
            after_url: preCaptureState.url || "",
            before_surface_signature: initialSurfaceSignature,
            after_surface_signature: preCaptureState.surface_signature || "",
            before_stable_surface_signature: initialStableSurfaceSignature,
            after_stable_surface_signature: preCaptureState.stable_surface_signature || "",
          };
        }
        volatileObservation = true;
      }
      let dataUrl = "";
      let screenshotMode = "capture_visible_tab";
      let screenshotQuality = 60;
      let lastError = null;
      let maskResult = { ok: false, count: 0, rectangles: [] };
      try {
        const maskRows = await withTimeout(
          chrome.scripting.executeScript({
            target: { tabId, allFrames: true },
            func: maskSensitiveScreenshotRegionsInPage,
            args: [semanticMaskContext],
          }),
          2500,
          "sensitive screenshot masking timed out",
        );
        const frameMaskRows = Array.isArray(maskRows) ? maskRows : [];
        const frameMaskResults = frameMaskRows.map((row, index) => ({
          frame_id: Number.isFinite(Number(row && row.frameId)) ? Number(row.frameId) : (index === 0 ? 0 : -1),
          result: (row && row.result) || { ok: false, error: "mask injection returned no frame result" },
        }));
        maskResult = {
          ok: Boolean(frameMaskResults.length) && frameMaskResults.every((row) => row.result.ok),
          count: frameMaskResults.reduce((total, row) => total + Number(row.result.count || 0), 0),
          rectangles: frameMaskResults.flatMap((row) => (
            Array.isArray(row.result.rectangles)
              ? row.result.rectangles.map((rect) => ({ ...rect, frame_id: row.frame_id }))
              : []
          )),
          frame_count: frameMaskResults.length,
          failed_frame_ids: frameMaskResults.filter((row) => !row.result.ok).map((row) => row.frame_id),
          error: frameMaskResults.filter((row) => !row.result.ok).map((row) => String(row.result.error || "")).filter(Boolean).join("; "),
        };
        const sensitiveControlsPresent = Array.isArray(pageEvidence.controls)
          && pageEvidence.controls.some((control) => control && control.value_redacted);
        const topFrameMask = frameMaskResults.find((row) => row.frame_id === 0);
        if (
          !maskResult.ok
          || (sensitiveControlsPresent && Number(topFrameMask && topFrameMask.result.count || 0) < 1)
        ) {
          return {
            ok: false,
            action: "screenshot",
            failure_reason: "sensitive_screenshot_mask_failed",
            retryable: true,
            image_discarded: true,
            sensitive_controls_present: sensitiveControlsPresent,
            mask_error: String(maskResult.error || ""),
          };
        }
        markCapture("mask");
        if (!await proveSensitiveScreenshotMasksPainted(tabId, frameMaskResults)) {
          return {
            ok: false,
            action: "screenshot",
            failure_reason: "sensitive_screenshot_mask_paint_unverified",
            retryable: true,
            image_discarded: true,
          };
        }
        // captureVisibleTab waits for a composited frame that an unpainted window never produces
        // (two timeouts, ~7s). When the paint barrier just proved the stall and no sensitive mask
        // needs a verified paint, go straight to the surface capture below.
        const paintStalled = Number(maskResult.count || 0) === 0
          && Date.now() - Number(globalThis.__lumenPaintStalledAt || 0) < 20000;
        for (let attempt = 0; attempt < 2 && !dataUrl && !paintStalled; attempt += 1) {
          try {
            const captureSurface = await acquireTargetSurfaceAndWaitForPaint(
              tabId,
              Number(maskResult.count || 0) > 0,
            );
            if (!captureSurface.ok) {
              return {
                ok: false,
                action: "screenshot",
                failure_reason: "screenshot_target_surface_unavailable",
                target_surface_failure_reason: captureSurface.reason || "target_surface_acquisition_failed",
                retryable: true,
                image_discarded: true,
                error: String(captureSurface.error || ""),
              };
            }
            windowId = captureSurface.window_id;
            dataUrl = await withTimeout(
              new Promise((resolve, reject) => {
                chrome.tabs.captureVisibleTab(windowId, lossless ? { format: "png" } : { format: "jpeg", quality: attempt === 0 ? 60 : 45 }, (url) => {
                  const err = chrome.runtime.lastError;
                  if (err) reject(new Error(err.message));
                  else resolve(url);
                });
              }),
              attempt === 0 ? 3000 : 4000,
              "captureVisibleTab timed out",
            );
            if (dataUrl) screenshotQuality = attempt === 0 ? 60 : 45;
          } catch (error) {
            lastError = error;
          }
        }
        if (!dataUrl) {
          const fallback = await captureVisibleViewportWithDebugger(tabId, 55, lossless);
          if (fallback.ok && fallback.data_url) {
            dataUrl = fallback.data_url;
            screenshotMode = fallback.mode || "debugger_capture";
            screenshotQuality = 55;
          } else {
            const primaryError = lastError ? errorMessage(lastError) : "Screenshot capture returned nothing";
            const fallbackError = String(fallback.error || "debugger screenshot returned nothing");
            throw new Error(`${primaryError}; debugger fallback failed: ${fallbackError}`);
          }
        }
        markCapture("capture");
        const capturedForeground = await targetSurfaceForegroundVerdict(tabId, windowId);
        if (!capturedForeground.ok) {
          return {
            ok: false,
            action: "screenshot",
            failure_reason: "screenshot_target_surface_lost",
            retryable: true,
            image_discarded: true,
            tab_active: Boolean(capturedForeground.tab_active),
            window_focused: Boolean(capturedForeground.window_focused),
          };
        }
      } finally {
        try {
          await withTimeout(
            chrome.scripting.executeScript({
              target: { tabId, allFrames: true },
              func: clearSensitiveScreenshotRegionsInPage,
            }),
            2500,
            "sensitive screenshot mask cleanup timed out",
          );
        } catch (_) {}
      }
      if (!dataUrl) return { ok: false, error: "Screenshot capture returned nothing." };
      const postCaptureState = await tabPageEvidence(tabId);
      markCapture("post_capture_state");
      if (
        postCaptureState.page_state_probe_complete === false
        || postCaptureState.surface_probe_complete === false
        || postCaptureState.tab_metadata_probe_complete === false
      ) {
        return {
          ok: false,
          action: "screenshot",
          failure_reason: "screenshot_observation_evidence_unavailable",
          evidence_stage: "post_capture_context",
          tab_metadata_probe_complete: postCaptureState.tab_metadata_probe_complete !== false,
          page_state_probe_complete: postCaptureState.page_state_probe_complete !== false,
          surface_probe_complete: postCaptureState.surface_probe_complete !== false,
          retryable: true,
          image_discarded: true,
        };
      }
      const captureStable = Boolean(
        screenshotObservationContextMatches(preCaptureState, postCaptureState)
        && (
          !expectedMaskSurface
          || String(postCaptureState.mask_surface_signature || "").trim() === expectedMaskSurface
        )
      );
      if (!captureStable) {
        if (!screenshotDriftTolerable(preCaptureState, postCaptureState, expectedMaskSurface)) {
          return {
            ok: false,
            action: "screenshot",
            failure_reason: "screenshot_observation_drift",
            retryable: true,
            image_discarded: true,
            before_url: preCaptureState.url || "",
            after_url: postCaptureState.url || "",
            before_surface_signature: preCaptureState.surface_signature || "",
            after_surface_signature: postCaptureState.surface_signature || "",
            before_stable_surface_signature: preCaptureState.stable_surface_signature || "",
            after_stable_surface_signature: postCaptureState.stable_surface_signature || "",
          };
        }
        volatileObservation = true;
      }
      // The boxes were measured before the capture: if the page moved since (a scroll that was still
      // finishing, a layout shift), they no longer sit on the picture. Observe again; after a few
      // tries the observation is returned as volatile instead.
      if (pageGeometryMoved(state, postCaptureState)) {
        if (!screenshotDriftTolerable(preCaptureState, postCaptureState, expectedMaskSurface)) {
          return {
            ok: false,
            action: "screenshot",
            failure_reason: "screenshot_observation_drift",
            drift_kind: "geometry_moved_during_capture",
            retryable: true,
            image_discarded: true,
            before_url: preCaptureState.url || "",
            after_url: postCaptureState.url || "",
          };
        }
        volatileObservation = true;
      } else {
        screenshotDriftStreak = 0;
      }
      const comma = dataUrl.indexOf(",");
      const image = comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl; // strip the data: prefix, send raw base64
      const screenshotSha256 = await screenshotDataSha256(dataUrl);
      const dpr = state.dpr || 1;
      const width = Math.round((state.inner_w || 0) * dpr);
      const height = Math.round((state.inner_h || 0) * dpr);
      return {
        ok: true,
        image,
        format: lossless ? "png" : "jpeg",
        width,
        height,
        captured_at: Date.now(),
        observation_volatile: volatileObservation,
        screenshot_mode: screenshotMode,
        screenshot_quality: screenshotQuality,
        screenshot_sha256: screenshotSha256,
        // The per-row digests travel at the top level (document_row_digests); the stored page evidence keeps
        // its existing shape.
        page_evidence: (({ document_row_digests: _rowDigests, ...rest }) => rest)(pageEvidence),
        surface_signature: preCaptureState.surface_signature || initialSurfaceSignature,
        stable_surface_signature: (
          preCaptureState.stable_surface_signature || initialStableSurfaceSignature
        ),
        document_state_signature: String(
          preCaptureState.document_state_signature || pageEvidence.document_state_signature || "",
        ),
        document_semantic_signature: String(
          preCaptureState.document_semantic_signature || pageEvidence.document_semantic_signature || "",
        ),
        // The same enumeration as document_state_signature (preCaptureState), for the one-row check.
        document_row_digests: Array.isArray(preCaptureState.document_row_digests)
          ? preCaptureState.document_row_digests
          : [],
        document_lifecycle_token: String(
          preCaptureState.document_lifecycle_token || pageEvidence.document_lifecycle_token || "",
        ),
        sensitive_regions_masked: true,
        sensitive_region_count: Number(maskResult.count || 0),
        sensitive_masked_frame_count: Number(maskResult.frame_count || 0),
        sensitive_region_rectangles: Array.isArray(maskResult.rectangles) ? maskResult.rectangles.slice(0, 80) : [],
        atomic_observation_verified: true,
        ...state,
      };
    } catch (error) {
      return { ok: false, error: "Screenshot failed: " + errorMessage(error) };
    }
  })();
  if (result && typeof result === "object") {
    markCapture("total");
    result.capture_timing_ms = captureTiming;
  }
  if (result && result.ok === true && !result.native_dialog && result.format !== "png") {
    try {
      const tabs = await runTabsEvidence();
      if (tabs.length > 1) result.page_evidence = { ...(result.page_evidence || {}), browser_tabs: tabs };
    } catch (_) {}
    try {
      const observedTabId = await activeTabId();
      if (observedTabId) lastObservationByTab.set(Number(observedTabId), result);
    } catch (_) {}
  }
  return result;
}

// --- pure-vision coordinate actions (run in the page) -------------------------------------------
function visionPointInfoInPage(
  imgX,
  imgY,
  allowFinalSubmit = false,
  allowGuardedIntermediate = false,
  expectedTarget = null,
  strictTwinRebind = false,
  armDispatchGuard = false,
  multiEventGesture = false,
) {
  try {
    // A guard left by an earlier probe that never pressed must not outlive it.
    try {
      const stale = globalThis.__lumenDispatchGuard;
      if (stale && typeof stale.disarm === "function") stale.disarm();
      globalThis.__lumenDispatchGuard = null;
    } catch (_) {}
    // Second pre-click reading of the values near the control (taken by the preflight): whatever
    // differs now changed without the click, so the outcome never counts it as the click's effect.
    try {
      const linkedProbe = globalThis.__lumenLinkedProbe;
      if (linkedProbe && Array.isArray(linkedProbe.items)) {
        for (const item of linkedProbe.items) {
          const n = item.el;
          const now = String(
            n && n.value !== undefined && n.value !== "" && typeof n.value === "string"
              ? n.value
              : ((n && (n.getAttribute("aria-valuetext") || n.innerText || n.textContent)) || ""),
          ).replace(/\s+/g, " ").trim().slice(0, 80);
          if (!n || !n.isConnected || now !== item.before) item.spontaneous = true;
        }
      }
    } catch (_) {}
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const associatedLabelText = (node) => {
      try {
        if (!node || !node.tagName) return "";
        const tag = String(node.tagName || "").toLowerCase();
        if (!["input", "select", "textarea"].includes(tag)) return "";
        const squash = (value) => String(value || "").replace(/\s+/g, " ").trim();
        // The observer names a label by its composed text (slotted content included); read it the same way.
        const labelShownText = (ref) => (
          (typeof globalThis.__lumenComposedText === "function" ? globalThis.__lumenComposedText(ref) : "")
          || ref.innerText || ref.textContent || ""
        );
        const root = node.getRootNode && node.getRootNode();
        const byId = (id) => (root && root.getElementById ? root.getElementById(id) : null) || document.getElementById(id);
        const labelledBy = squash(node.getAttribute && node.getAttribute("aria-labelledby"));
        if (labelledBy) {
          const text = squash(labelledBy.split(/\s+/).map((id) => { const ref = byId(id); return ref ? labelShownText(ref) : ""; }).join(" "));
          if (text) return text;
        }
        const id = node.getAttribute && node.getAttribute("id");
        if (id) {
          const escaped = (window.CSS && typeof CSS.escape === "function") ? CSS.escape(id) : String(id).split('"').join('\\"');
          const explicit = (root && root.querySelector ? root.querySelector(`label[for="${escaped}"]`) : null)
            || document.querySelector(`label[for="${escaped}"]`);
          const text = explicit ? squash(labelShownText(explicit)) : "";
          if (text) return text;
        }
        const wrapper = node.closest && node.closest("label");
        const wrapped = wrapper ? squash(labelShownText(wrapper)) : "";
        return wrapped;
      } catch (_) {
        return "";
      }
    };
    const queryAllComposed = (selector) => {
      if (typeof globalThis.__lumenDeepQueryAll === "function") {
        return globalThis.__lumenDeepQueryAll(selector);
      }
      return Array.from(document.querySelectorAll(selector));
    };
    const deepElementFromPoint = (x, y) => (
      typeof globalThis.__lumenDeepElementFromPoint === "function"
        ? globalThis.__lumenDeepElementFromPoint(x, y)
        : document.elementFromPoint(x, y)
    );
    const composedParent = (node) => (
      typeof globalThis.__lumenComposedParent === "function"
        ? globalThis.__lumenComposedParent(node)
        : (node && node.parentElement)
    );
    const composedContains = (ancestor, node) => {
      if (typeof globalThis.__lumenComposedContains === "function") {
        return globalThis.__lumenComposedContains(ancestor, node);
      }
      return Boolean(ancestor && node && ancestor.contains && ancestor.contains(node));
    };
    const deepDescendants = (node, selector) => (
      typeof globalThis.__lumenDeepDescendants === "function"
        ? globalThis.__lumenDeepDescendants(node, selector)
        : Array.from((node && node.querySelectorAll && node.querySelectorAll(selector)) || [])
    );
    const sensitiveValueControl = (node) => {
      try {
        const tag = (node && node.tagName || "").toLowerCase();
        const inputType = clean(node && node.getAttribute && node.getAttribute("type")).toLowerCase();
        if (tag === "input" && inputType === "password") return true;
        const autocompleteTokens = clean(node && node.getAttribute && node.getAttribute("autocomplete"))
          .toLowerCase()
          .split(/\s+/)
          .filter(Boolean);
        return autocompleteTokens.some((token) => (
          token === "current-password"
          || token === "new-password"
          || token === "one-time-code"
          || token === "transaction-amount"
          || token === "transaction-currency"
          || token === "webauthn"
          || token.startsWith("cc-")
        ));
      } catch (_) {
        return false;
      }
    };
    const controlText = (node) => clean(
      node
        ? (
          sensitiveValueControl(node)
            ? (
              node.getAttribute && (
                node.getAttribute("aria-label")
                || node.getAttribute("placeholder")
                || node.getAttribute("name")
              )
              || ""
            )
            : (
              node.innerText
              || node.textContent
              || associatedLabelText(node)
              || node.getAttribute && node.getAttribute("aria-label")
              || node.getAttribute && node.getAttribute("placeholder")
              || node.getAttribute && node.getAttribute("name")
              || ""
            )
        )
        : "",
    );
    const cssEscape = (value) => {
      try {
        if (window.CSS && typeof CSS.escape === "function") return CSS.escape(String(value));
      } catch (_) {}
      return String(value || "").replace(/["\\]/g, "\\$&");
    };
    const attrValue = (value) => String(value || "").replace(/\\/g, "\\\\").replace(/"/g, '\\"');
    const uniqueSelector = (selector, node) => {
      try {
        const matches = queryAllComposed(selector);
        return Boolean(
          selector
          && selector.length <= 200
          && matches.length === 1
          && matches[0] === node
        );
      } catch (_) {
        return false;
      }
    };
    const stableSelector = (node) => {
      try {
        if (!node || !node.tagName) return "";
        if (typeof globalThis.__lumenStableDeepSelector === "function") {
          const composed = globalThis.__lumenStableDeepSelector(node);
          if (composed) return composed;
        }
        const tag = (node.tagName || "").toLowerCase();
        const id = node.getAttribute("id");
        if (id) {
          const selector = `#${cssEscape(id)}`;
          if (uniqueSelector(selector, node)) return selector;
        }
        const name = node.getAttribute("name");
        if (name) {
          const selector = `${tag}[name="${attrValue(name)}"]`;
          if (uniqueSelector(selector, node)) return selector;
        }
        const type = node.getAttribute("type");
        if (type && tag === "input") {
          const selector = `${tag}[type="${attrValue(type)}"]`;
          if (uniqueSelector(selector, node)) return selector;
        }
        let current = node;
        const parts = [];
        for (let depth = 0; current && current.nodeType === 1 && depth < 5; depth += 1, current = current.parentElement) {
          const currentTag = (current.tagName || "").toLowerCase();
          if (!currentTag || currentTag === "html") break;
          const currentId = current.getAttribute("id");
          if (currentId) {
            parts.unshift(`#${cssEscape(currentId)}`);
          } else {
            const parent = current.parentElement;
            const siblings = parent ? Array.from(parent.children).filter((sib) => (sib.tagName || "").toLowerCase() === currentTag) : [];
            const nth = siblings.length > 1 ? `:nth-of-type(${siblings.indexOf(current) + 1})` : "";
            parts.unshift(`${currentTag}${nth}`);
          }
          const selector = parts.join(" > ");
          if (uniqueSelector(selector, node)) return selector;
          if (currentId) break;
          if (currentTag === "body") break;
        }
        return "";
      } catch (_) {
        return "";
      }
    };
    const isEnabledFileInput = (node) => Boolean(
      node
      && node.matches
      && node.matches('input[type="file"]')
      && !node.disabled
    );
    const firstEnabledFileInput = (node) => {
      if (!node) return null;
      return deepDescendants(node, 'input[type="file"]').find((input) => !input.disabled) || null;
    };
    const enabledFileInputs = (node) => {
      if (!node) return [];
      return deepDescendants(node, 'input[type="file"]').filter((input) => !input.disabled);
    };
    const fileInputForLabel = (label) => {
      if (!label || !label.matches || !label.matches("label")) return null;
      if (label.htmlFor) {
        const root = label.getRootNode && label.getRootNode();
        const byFor = (root && root.getElementById && root.getElementById(label.htmlFor))
          || document.getElementById(label.htmlFor);
        if (isEnabledFileInput(byFor)) return byFor;
      }
      return firstEnabledFileInput(label);
    };
    const associatedFileInput = (anchor) => {
      if (!anchor) return null;
      if (isEnabledFileInput(anchor)) return anchor;
      const nested = firstEnabledFileInput(anchor);
      if (nested) return nested;
      const labelInput = fileInputForLabel(anchor);
      if (labelInput) return labelInput;
      const containingLabel = anchor.closest && anchor.closest("label");
      const containingLabelInput = fileInputForLabel(containingLabel);
      if (containingLabelInput) return containingLabelInput;
      let ancestor = composedParent(anchor);
      for (let depth = 0; ancestor && depth < 4; depth += 1, ancestor = composedParent(ancestor)) {
        const tag = (ancestor.tagName || "").toLowerCase();
        if (tag === "body" || tag === "html" || tag === "form") continue;
        const inputs = enabledFileInputs(ancestor);
        if (inputs.length !== 1) continue;
        const input = inputs[0];
        if (input.parentElement === ancestor || anchor.parentElement === ancestor) return input;
      }
      return null;
    };
    const labelFor = (input, anchor) => {
      if (input && input.id) {
        const root = input.getRootNode && input.getRootNode();
        const explicit = (root && root.querySelector && root.querySelector(`label[for="${cssEscape(input.id)}"]`))
          || document.querySelector(`label[for="${cssEscape(input.id)}"]`);
        const text = explicit ? clean(explicit.innerText || explicit.textContent) : "";
        if (text) return text.slice(0, 120);
      }
      const inputLabel = input && input.closest && input.closest("label");
      const inputLabelText = inputLabel ? clean(inputLabel.innerText || inputLabel.textContent) : "";
      if (inputLabelText) return inputLabelText.slice(0, 120);
      let node = anchor || input;
      for (let i = 0; node && i < 5; i += 1, node = composedParent(node)) {
        const text = clean(node.innerText || node.textContent || node.getAttribute("aria-label") || node.getAttribute("name") || "");
        if (text) return text.slice(0, 120);
      }
      return "";
    };
    const dpr = window.devicePixelRatio || 1;
    let x = Math.round(imgX / dpr), y = Math.round(imgY / dpr); // image px -> CSS px
    const pointHit = deepElementFromPoint(x, y);
    let el = pointHit;
    let exactlyBound = false;
    const actionableSelector = 'button,a[href],input,textarea,select,label,[role="button"],[role="option"],[role="radio"],[role="checkbox"],[role="combobox"],[tabindex]';
    // Verbatim, never whitespace-collapsed: an attribute value in a selector can hold a double space
    // (SmartRecruiters' spl-icon[aria-label="Clear  value"], run 75, then matched nothing and every click drifted).
    const expectedSelectorAtPoint = String((expectedTarget && expectedTarget.target_selector) || "").trim();
    if (pointHit && expectedSelectorAtPoint) {
      let exactMatches = [];
      try { exactMatches = queryAllComposed(expectedSelectorAtPoint); } catch (_) { exactMatches = []; }
      if (exactMatches.length === 1) {
        const exactTarget = exactMatches[0];
        const rect = (typeof globalThis.__lumenTopRect === "function" ? globalThis.__lumenTopRect(exactTarget) : exactTarget.getBoundingClientRect());
        const pointInsideTarget = Boolean(
          rect
          && rect.width > 0
          && rect.height > 0
          && x >= rect.left
          && x <= rect.right
          && y >= rect.top
          && y <= rect.bottom
        );
        if (
          pointInsideTarget
          && (
            pointHit === exactTarget
            || composedContains(pointHit, exactTarget)
            || composedContains(exactTarget, pointHit)
              // Layered pieces of one SVG drawing (a label over its slice, an icon over its button shape): the
              // hit shape and the enumerated piece sit under the same point in the same <svg>.
              || (() => {
                try {
                  // Never for a final or guarded click: its dispatch guard lets what sits at the point through, so a
                  // sibling piece could receive the click while the judged piece is a different one.
                  if (allowFinalSubmit || allowGuardedIntermediate) return false;
                  const surface = exactTarget.closest && exactTarget.closest("svg");
                  if (!surface || !pointHit.closest || pointHit.closest("svg") !== surface) return false;
                  return document.elementsFromPoint(x, y).includes(exactTarget);
                } catch (_) { return false; }
              })()
          )
        ) {
          el = exactTarget;
          exactlyBound = true;
        }
      }
    }
    // Semantic identity of the control the model chose: tag + type + role + visible text. A page
    // that re-renders (fresh ids, shuffled positions) keeps that identity even though the exact
    // selector and the coordinates go stale. Ordinary clicks may follow the control. Final-submit
    // and guarded clicks follow it only under strictTwinRebind (the caller proved every control
    // still shows the same label, value, and state), only by its observed visible text, and only to
    // the one rendered node in the whole page with that identity.
    const expectedIdentityTag = clean(expectedTarget && expectedTarget.target_tag).toLowerCase();
    const expectedIdentityRawType = clean(expectedTarget && expectedTarget.target_type).toLowerCase();
    const expectedIdentity = {
      tag: expectedIdentityTag,
      // A type equal to the tag is the observer's "no type attribute" fallback.
      type: expectedIdentityRawType === expectedIdentityTag ? "" : expectedIdentityRawType,
      role: clean(expectedTarget && expectedTarget.target_role).toLowerCase(),
      text: clean(expectedTarget && expectedTarget.target_text).slice(0, 180).trim(),
      names: (expectedTarget && Array.isArray(expectedTarget.target_names) ? expectedTarget.target_names : [])
        .map((name) => clean(name).slice(0, 180).trim())
        .filter(Boolean),
    };
    const strictAuthority = Boolean(allowFinalSubmit || allowGuardedIntermediate);
    const semanticRebindAllowed = Boolean(
      expectedTarget
      && expectedIdentity.tag
      && expectedIdentity.text
      && (!strictAuthority || strictTwinRebind)
    );
    const sameIdentity = (node) => {
      try {
        if (!node || !node.tagName) return false;
        return (
          (node.tagName || "").toLowerCase() === expectedIdentity.tag
          && clean(node.getAttribute && node.getAttribute("type")).toLowerCase() === expectedIdentity.type
          && clean(node.getAttribute && node.getAttribute("role")).toLowerCase() === expectedIdentity.role
          && (
            controlText(node).slice(0, 180).trim() === expectedIdentity.text
            || (!strictAuthority && expectedIdentity.names.includes(controlText(node).slice(0, 180).trim()))
          )
        );
      } catch (_) {
        return false;
      }
    };
    // The widget's visible row is often the wrapper the observer listed (a jQuery UI menu's li) while the
    // point lands on a focusable child (its div): the same row, one to three levels down.
    const sameIdentityAncestor = (node) => {
      let up = node && node.parentElement;
      for (let depth = 0; up && depth < 3; depth += 1, up = up.parentElement) {
        if (sameIdentity(up)) return true;
      }
      return false;
    };
    let rebound = false;
    let reboundFrom = null;
    let strictTwin = null;
    // A strict click follows a twin only when its approved node is gone from the whole document.
    let approvedNodeGone = false;
    if (strictAuthority && expectedSelectorAtPoint) {
      try {
        approvedNodeGone = queryAllComposed(expectedSelectorAtPoint).length === 0;
      } catch (_) {
        approvedNodeGone = false;
      }
    }
    if (semanticRebindAllowed && !exactlyBound && strictAuthority && approvedNodeGone) {
      const rectOfNode = (node) => (
        typeof globalThis.__lumenTopRect === "function" ? globalThis.__lumenTopRect(node) : node.getBoundingClientRect()
      );
      let renderedTwins = [];
      let sameNamedCount = 0;
      try {
        const sameNamed = queryAllComposed(expectedIdentity.tag).filter((node) => sameIdentity(node));
        sameNamedCount = sameNamed.length;
        renderedTwins = sameNamed.filter((node) => {
          const rect = rectOfNode(node);
          const style = getComputedStyle(node);
          return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
        });
      } catch (_) {
        renderedTwins = [];
      }
      if (
        sameNamedCount === 1
        && renderedTwins.length === 1
        && !renderedTwins[0].disabled
        && clean(renderedTwins[0].getAttribute && renderedTwins[0].getAttribute("aria-disabled")).toLowerCase() !== "true"
      ) {
        const twin = renderedTwins[0];
        const rect = rectOfNode(twin);
        const fullyInView = rect.left >= 0
          && rect.top >= 0
          && rect.right <= (window.innerWidth || 0)
          && rect.bottom <= (window.innerHeight || 0);
        if (fullyInView) {
          if (pointHit && (pointHit === twin || composedContains(twin, pointHit))) {
            el = twin;
            strictTwin = twin;
          } else {
            const cx = Math.round(rect.left + rect.width / 2);
            const cy = Math.round(rect.top + rect.height / 2);
            const topAtCenter = deepElementFromPoint(cx, cy);
            if (topAtCenter && (topAtCenter === twin || composedContains(twin, topAtCenter))) {
              reboundFrom = { x, y };
              x = cx;
              y = cy;
              el = twin;
              rebound = true;
              strictTwin = twin;
            }
          }
          // Bound to that one node exactly as if its selector had resolved at the point.
          if (strictTwin) exactlyBound = true;
        }
      }
    } else if (semanticRebindAllowed && !exactlyBound && !strictAuthority) {
      const hitActionable = pointHit && pointHit.closest ? (pointHit.closest(actionableSelector) || pointHit) : pointHit;
      if (!(hitActionable && sameIdentity(hitActionable))) {
        let candidates = [];
        try {
          candidates = queryAllComposed(expectedIdentity.tag).filter((node) => {
            if (!sameIdentity(node) || node.disabled) return false;
            const rect = (typeof globalThis.__lumenTopRect === "function" ? globalThis.__lumenTopRect(node) : node.getBoundingClientRect());
            return (
              rect.width > 0
              && rect.height > 0
              && rect.left >= 0
              && rect.top >= 0
              && rect.right <= (window.innerWidth || 0)
              && rect.bottom <= (window.innerHeight || 0)
              && getComputedStyle(node).visibility !== "hidden"
            );
          });
        } catch (_) {
          candidates = [];
        }
        if (candidates.length === 1) {
          const rect = (typeof globalThis.__lumenTopRect === "function" ? globalThis.__lumenTopRect(candidates[0]) : candidates[0].getBoundingClientRect());
          const cx = Math.round(rect.left + rect.width / 2);
          const cy = Math.round(rect.top + rect.height / 2);
          const topAtCenter = deepElementFromPoint(cx, cy);
          if (topAtCenter && (topAtCenter === candidates[0] || composedContains(candidates[0], topAtCenter))) {
            reboundFrom = { x, y };
            x = cx;
            y = cy;
            el = topAtCenter;
            rebound = true;
          }
        }
      }
    }
    if (!el) return { ok: false, error: "nothing at point", x, y };
    // SAFETY FLOOR: a coordinate click must never land on a final submit. Walk a few ancestors.
    const submitDetection = (n) => {
      try {
        const tag = (n.tagName || "").toLowerCase();
        const typeAttribute = clean(n.getAttribute && n.getAttribute("type")).toLowerCase();
        const resolvedType = clean(n.type || typeAttribute).toLowerCase();
        const role = clean(n.getAttribute && n.getAttribute("role")).toLowerCase();
        const text = controlText(n).slice(0, 120);
        const insideForm = Boolean(n.closest && n.closest("form"));
        const raw = {
          tag,
          type_attribute: typeAttribute,
          resolved_type: resolvedType,
          role,
          inside_form: insideForm,
          form_owner: clean((n.form && n.form.id) || (n.getAttribute && n.getAttribute("form"))),
          href: clean(n.getAttribute && n.getAttribute("href")).slice(0, 240),
          text,
        };
        const explicitSubmitType = typeAttribute === "submit" || (tag === "input" && typeAttribute === "image");
        const formAssociatedSubmit = Boolean(n.form) && (
          (tag === "button" && resolvedType === "submit")
          || (tag === "input" && (resolvedType === "submit" || resolvedType === "image"))
        );
        const nativeSubmit = explicitSubmitType || formAssociatedSubmit;
        if (nativeSubmit) return { matches: true, basis: "native_submit", ...raw };
        const t = text.toLowerCase();
        const formFieldCount = queryAllComposed("input, textarea, select, [contenteditable='true']").length;
        const formPage = formFieldCount >= 3 || /apply|application|checkout|booking|reservation|purchase/.test(String(location.href || "").toLowerCase());
        const contextualMatch = (formPage && /^(apply|submit|send application|complete application)$/i.test(t))
          || (/\bsubmit\b|\bplace order\b|\bpay now\b|\bconfirm and submit\b/.test(t) && t.length < 40);
        return { matches: Boolean(contextualMatch), basis: contextualMatch ? "contextual" : "", form_field_count: formFieldCount, ...raw };
      } catch (_) { return { matches: false, basis: "" }; }
    };
    let probe = el;
    let firstSubmitLikeTarget = null;
    let firstSubmitDetection = null;
    let finalSubmitTarget = null;
    let finalSubmitDetection = null;
    const closestActionable = el.closest && el.closest('button,input[type="submit"],input[type="button"],input[type="image"],[role="button"]');
    if (closestActionable) {
      const detection = submitDetection(closestActionable);
      if (detection.matches) {
        finalSubmitTarget = closestActionable;
        finalSubmitDetection = detection;
      }
    }
    // A plain wrapper whose own text collapses to a sibling button's label ("Apply" beside a coupon field) is not a
    // submit: the click landed on a control inside it (a field, another button), never on the wrapper itself.
    const wrapsOtherControl = (node) => {
      try {
        const tag = (node.tagName || "").toLowerCase();
        const role = clean(node.getAttribute && node.getAttribute("role")).toLowerCase();
        if (tag === "button" || tag === "input" || role === "button") return false;
        return Boolean(node.querySelector && node.querySelector('button,input,select,textarea,[role="button"],[contenteditable="true"]'));
      } catch (_) { return false; }
    };
    for (let i = 0; i < 8 && probe && !finalSubmitTarget; i++) {
      const detection = submitDetection(probe);
      if (detection.matches && !wrapsOtherControl(probe)) {
        if (!firstSubmitLikeTarget) {
          firstSubmitLikeTarget = probe;
          firstSubmitDetection = detection;
        }
        const tag = (probe.tagName || "").toLowerCase();
        const role = clean(probe.getAttribute && probe.getAttribute("role")).toLowerCase();
        if (tag === "button" || tag === "input" || role === "button") {
          finalSubmitTarget = probe;
          finalSubmitDetection = detection;
          break;
        }
      }
      probe = composedParent(probe);
    }
    const guardedIntermediateRevalidated = Boolean(
      allowGuardedIntermediate
      && finalSubmitTarget
      && finalSubmitDetection
      && finalSubmitDetection.basis === "contextual"
      && finalSubmitDetection.tag === "button"
      // A formless typeless button resolves to "submit" but has nothing to natively submit.
      && (finalSubmitDetection.resolved_type === "button" || finalSubmitDetection.resolved_type === "submit")
      && !finalSubmitDetection.inside_form
      && !finalSubmitDetection.form_owner
    );
    if (finalSubmitTarget && !allowFinalSubmit && !guardedIntermediateRevalidated) {
      const label = controlText(finalSubmitTarget).slice(0, 60);
      const targetSelector = stableSelector(finalSubmitTarget);
      const targetTag = (finalSubmitTarget.tagName || "").toLowerCase();
      return {
        ok: false,
        blocked_submit: true,
        selector: targetSelector,
        target_selector: targetSelector,
        target_text: controlText(finalSubmitTarget).slice(0, 180).trim(),
        target_tag: targetTag,
        target_type: clean(finalSubmitTarget.getAttribute && finalSubmitTarget.getAttribute("type")).toLowerCase(),
        target_role: clean(finalSubmitTarget.getAttribute && finalSubmitTarget.getAttribute("role")).toLowerCase(),
        dispatch_path: "dom_activation",
        page_url: String(location.href || ""),
        target_label: label,
        label,
        x,
        y,
        submit_detection_basis: (finalSubmitDetection || {}).basis || "",
        submit_detection: finalSubmitDetection || {},
      };
    }
    if (firstSubmitLikeTarget && !allowFinalSubmit && !guardedIntermediateRevalidated) {
      const label = controlText(firstSubmitLikeTarget).slice(0, 60);
      return {
        ok: false,
        blocked_submit: true,
        selector: "",
        target_label: label,
        label,
        x,
        y,
        submit_detection_basis: (firstSubmitDetection || {}).basis || "",
        submit_detection: firstSubmitDetection || {},
      };
    }
    const input = associatedFileInput(el);
    if (input) {
      const targetLabel = labelFor(input, el);
      return {
        ok: false,
        blocked_file_dialog: true,
        failure_reason: "click_would_open_file_dialog",
        target_label: targetLabel,
        label: targetLabel,
        x,
        y,
      };
    }
    // Final authority now reaches any consequential control the model's consequence judge
    // withheld ("Dispatch shipment", "Delete records"), not only keyword-shaped submits. The
    // authority is bound to one exact element, so an exactly bound target is sufficient; the
    // keyword detector only has to vouch for an unbound point.
    if (allowFinalSubmit && !finalSubmitTarget && !exactlyBound) {
      const label = controlText(el).slice(0, 60);
      return {
        ok: false,
        failure_reason: "final_submit_target_mismatch",
        target_label: label,
        label,
        selector: stableSelector(el),
        x,
        y,
      };
    }
    if (allowGuardedIntermediate && !guardedIntermediateRevalidated) {
      const label = controlText(el).slice(0, 60);
      return {
        ok: false,
        failure_reason: "guarded_intermediate_target_mismatch",
        target_label: label,
        label,
        selector: stableSelector(el),
        x,
        y,
      };
    }
    const domActionableTarget = (el.closest && el.closest(actionableSelector)) || null;
    const coordinateSurface = (el.closest && el.closest("canvas,svg")) || null;
    const coordinateSurfaceIsTarget = Boolean(
      coordinateSurface
      && (!domActionableTarget || coordinateSurface === domActionableTarget),
    );
    const expectedSelector = String((expectedTarget && expectedTarget.target_selector) || "").trim();
    // Same rule as visionClickAtPointInPage: an observed control nested between the hit and its
    // actionable ancestor (a suggestion list inside a field's <label>) is the clicked control.
    let nestedExpected = null;
    if (expectedSelector && domActionableTarget) {
      try {
        const named = queryAllComposed(expectedSelector);
        if (named.length === 1 && named[0] !== domActionableTarget && named[0].contains(el) && domActionableTarget.contains(named[0])) {
          nestedExpected = named[0];
        }
      } catch (_) {
        nestedExpected = null;
      }
    }
    const interactionTarget = nestedExpected || domActionableTarget || el;
    const tag = (interactionTarget.tagName || "").toLowerCase();
    let boundTargetSelector = "";
    if (expectedSelector) {
      let expectedNodes = [];
      try {
        expectedNodes = queryAllComposed(expectedSelector);
      } catch (_) {
        expectedNodes = [];
      }
      const exactMatch = expectedNodes.length === 1 && expectedNodes[0] === interactionTarget;
      const semanticMatch = !exactMatch
        && semanticRebindAllowed
        && (
          (sameIdentity(interactionTarget) && (!strictAuthority || (strictTwin !== null && interactionTarget === strictTwin)))
          || (!strictAuthority && sameIdentityAncestor(interactionTarget))
        );
      if (!exactMatch && !semanticMatch) {
        return {
          ok: false,
          action: "click",
          mode: "click_target_revalidation",
          failure_reason: "click_target_drift",
          target_identity_matched: false,
          target_identity_mismatch_field: "target_selector",
          preflight_target_selector: expectedSelector,
          live_target_selector: stableSelector(interactionTarget),
          activation_attempted: false,
          activation_dispatched: false,
          activation_count: 0,
          x,
          y,
        };
      }
      boundTargetSelector = exactMatch ? expectedSelector : stableSelector(interactionTarget);
    }
    const targetText = controlText(interactionTarget).slice(0, 180).trim();
    // A final or guarded click is bound to this one node. Until its gesture's click lands, each
    // trusted pointer or mouse event whose path does not run through that node (or start on what this
    // probe hit at the point) is stopped at the window, ahead of every page listener registered after
    // it: the page swapped what sits under the point after this probe. Read back and disarmed right
    // after the press.
    let dispatchGuardArmed = false;
    if (armDispatchGuard) {
      try {
        const guarded = interactionTarget;
        const hitAtArm = deepElementFromPoint(x, y);
        const report = { armed: true, blocked: false, blocked_events: 0, passed_events: 0, partial: false, first_blocked: "", intercepted_by: "", focus_moved: false, focus_restored: false, x, y };
        const types = ["pointerdown", "mousedown", "pointerup", "mouseup", "click", "dblclick", "contextmenu", "auxclick"];
        // A double-click or right/middle click stays guarded through every press and release of the
        // gesture (refuse-only); a plain click is verified on its first event.
        const gestureEnd = multiEventGesture ? ["dblclick", "contextmenu", "auxclick"] : ["click"];
        let timer = null;
        // Focus is part of what a refused press must not change: the prior focus is put back.
        let focusBefore = null;
        try { focusBefore = document.activeElement || null; } catch (_) {}
        const restoreFocus = () => {
          try {
            const now = document.activeElement || null;
            if (now === focusBefore) return;
            report.focus_moved = true;
            if (focusBefore && focusBefore.isConnected && typeof focusBefore.focus === "function") {
              focusBefore.focus({ preventScroll: true });
              if ((document.activeElement || null) === focusBefore) report.focus_restored = true;
            } else if (now && typeof now.blur === "function") {
              now.blur();
              report.focus_restored = (document.activeElement || null) !== now;
            }
          } catch (_) {}
        };
        const entry = { report, disarm: () => {}, restoreFocus };
        // tag.class / role "name": what drew itself over the target, as the page names it.
        const describeNode = (node) => {
          try {
            if (!node || !node.tagName) return "";
            const classes = clean(typeof node.className === "string" ? node.className : node.getAttribute("class")).split(" ").filter(Boolean).slice(0, 2);
            const head = (node.tagName || "").toLowerCase() + (classes.length ? "." + classes.join(".") : "");
            const role = clean(node.getAttribute("role"));
            const name = clean(node.getAttribute("aria-label") || node.innerText || node.textContent || "").slice(0, 40);
            return head + (role || name ? " / " + [role, name ? '"' + name + '"' : ""].filter(Boolean).join(" ") : "");
          } catch (_) { return ""; }
        };
        // A label activates its control: events on it reach the control it names or wraps.
        const labelActivates = (node, control) => {
          try {
            const label = node && node.closest ? node.closest("label") : null;
            return Boolean(label && (label.control === control || composedContains(label, control)));
          } catch (_) { return false; }
        };
        const onEvent = (event) => {
          if (event.isTrusted === false && globalThis.__lumenDispatchGuardCountsSyntheticEvents !== true) return;
          const path = typeof event.composedPath === "function" ? event.composedPath() : [];
          const origin = path[0] || event.target;
          const insideTarget = path.includes(guarded)
            || Boolean(origin && (origin === guarded || composedContains(guarded, origin) || labelActivates(origin, guarded)));
          // A final or guarded click is bound to this one node: what the probe hit at the point also
          // passes (the page swapping it afterwards is the drift this guard is for). Any other click is
          // verified on its first event only: the target or something inside it, else the event is
          // stopped and the receipt names what intercepts it.
          const reaches = insideTarget || (strictAuthority && Boolean(origin && origin === hitAtArm));
          if (reaches) {
            report.passed_events += 1;
            if (!strictAuthority && !report.blocked && !multiEventGesture) { entry.disarm(); return; }
          } else {
            if (!report.blocked_events) {
              report.first_blocked = String(event.type || "");
              report.intercepted_by = describeNode(origin);
            }
            report.blocked_events += 1;
            report.blocked = true;
            event.preventDefault();
            event.stopImmediatePropagation();
            setTimeout(restoreFocus, 0);
          }
          report.partial = report.blocked && report.passed_events > 0;
          if (gestureEnd.includes(event.type)) entry.disarm();
        };
        entry.disarm = () => {
          if (report.blocked) restoreFocus();
          for (const type of types) window.removeEventListener(type, onEvent, true);
          if (timer) clearTimeout(timer);
          report.armed = false;
        };
        for (const type of types) window.addEventListener(type, onEvent, true);
        timer = setTimeout(() => entry.disarm(), 4000);
        globalThis.__lumenDispatchGuard = entry;
        dispatchGuardArmed = true;
      } catch (_) {
        dispatchGuardArmed = false;
      }
    }
    return {
      ok: true,
      x,
      y,
      rebound,
      rebound_from: reboundFrom,
      strict_twin_rebound: Boolean(strictTwin),
      dispatch_guard_armed: dispatchGuardArmed,
      hit_tag: (el.tagName || "").toLowerCase(),
      tag,
      target_tag: tag,
      target_type: clean(interactionTarget.getAttribute && interactionTarget.getAttribute("type")).toLowerCase(),
      target_role: clean(interactionTarget.getAttribute && interactionTarget.getAttribute("role")).toLowerCase(),
      target_selector: boundTargetSelector || stableSelector(interactionTarget),
      target_text: targetText,
      label: targetText.slice(0, 60),
      // What the page itself says about the target's availability (the disabled attribute or
      // aria-disabled on it or an ancestor, plus its own accessible description), so a click with no
      // effect can be reported with its likely reason. Raw reading, no interpretation.
      target_disabled: Boolean(
        interactionTarget.disabled
        || (interactionTarget.closest && interactionTarget.closest('[disabled],[aria-disabled="true"]')),
      ),
      target_description: (() => {
        try {
          const own = clean(
            interactionTarget.getAttribute("aria-description") || interactionTarget.getAttribute("title"),
          );
          if (own) return own.slice(0, 160);
          const ids = clean(interactionTarget.getAttribute("aria-describedby")).split(" ").filter(Boolean);
          return clean(ids.map((id) => (document.getElementById(id) || {}).textContent || "").join(" ")).slice(0, 160);
        } catch (_) { return ""; }
      })(),
      final_submit_revalidated: Boolean(finalSubmitTarget),
      final_submit_selector: finalSubmitTarget ? stableSelector(finalSubmitTarget) : "",
      guarded_intermediate_revalidated: guardedIntermediateRevalidated,
      submit_detection_basis: (finalSubmitDetection || {}).basis || "",
      submit_detection: finalSubmitDetection || {},
      // Same rule as every other identity builder (and the observer-side identity on the server):
      // only a rendered surface (canvas/SVG) is a coordinate surface; any other element is a DOM
      // control, whether or not it carries an interactive tag or role.
      dispatch_path: coordinateSurfaceIsTarget ? "cdp_trusted_click" : "dom_activation",
      dispatch_basis: coordinateSurfaceIsTarget
        ? "coordinate_rendered_surface"
        : domActionableTarget
          ? "dom_actionable_target"
          : "coordinate_target",
      page_url: String(location.href || ""),
    };
  } catch (e) { return { ok: false, error: String(e) }; }
}

function visionControlCenterInPage(selector, shouldScroll = true, requireVisiblePoint = false, surfaceOffset = null) {
  try {
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const associatedLabelText = (node) => {
      try {
        if (!node || !node.tagName) return "";
        const tag = String(node.tagName || "").toLowerCase();
        if (!["input", "select", "textarea"].includes(tag)) return "";
        const squash = (value) => String(value || "").replace(/\s+/g, " ").trim();
        // The observer names a label by its composed text (slotted content included); read it the same way.
        const labelShownText = (ref) => (
          (typeof globalThis.__lumenComposedText === "function" ? globalThis.__lumenComposedText(ref) : "")
          || ref.innerText || ref.textContent || ""
        );
        const root = node.getRootNode && node.getRootNode();
        const byId = (id) => (root && root.getElementById ? root.getElementById(id) : null) || document.getElementById(id);
        const labelledBy = squash(node.getAttribute && node.getAttribute("aria-labelledby"));
        if (labelledBy) {
          const text = squash(labelledBy.split(/\s+/).map((id) => { const ref = byId(id); return ref ? labelShownText(ref) : ""; }).join(" "));
          if (text) return text;
        }
        const id = node.getAttribute && node.getAttribute("id");
        if (id) {
          const escaped = (window.CSS && typeof CSS.escape === "function") ? CSS.escape(id) : String(id).split('"').join('\\"');
          const explicit = (root && root.querySelector ? root.querySelector(`label[for="${escaped}"]`) : null)
            || document.querySelector(`label[for="${escaped}"]`);
          const text = explicit ? squash(labelShownText(explicit)) : "";
          if (text) return text;
        }
        const wrapper = node.closest && node.closest("label");
        const wrapped = wrapper ? squash(labelShownText(wrapper)) : "";
        return wrapped;
      } catch (_) {
        return "";
      }
    };
    const queryAll = typeof globalThis.__lumenDeepQueryAll === "function"
      ? globalThis.__lumenDeepQueryAll
      : (value) => Array.from(document.querySelectorAll(value));
    const elementFromPoint = typeof globalThis.__lumenDeepElementFromPoint === "function"
      ? globalThis.__lumenDeepElementFromPoint
      : (x, y) => document.elementFromPoint(x, y);
    const composedContains = typeof globalThis.__lumenComposedContains === "function"
      ? globalThis.__lumenComposedContains
      : (ancestor, node) => Boolean(ancestor && node && ancestor.contains && ancestor.contains(node));
    const sensitiveValueControl = (node) => {
      try {
        const tag = (node && node.tagName || "").toLowerCase();
        const inputType = clean(node && node.getAttribute && node.getAttribute("type")).toLowerCase();
        if (tag === "input" && inputType === "password") return true;
        const autocompleteTokens = clean(node && node.getAttribute && node.getAttribute("autocomplete"))
          .toLowerCase()
          .split(/\s+/)
          .filter(Boolean);
        return autocompleteTokens.some((token) => (
          token === "current-password"
          || token === "new-password"
          || token === "one-time-code"
          || token === "transaction-amount"
          || token === "transaction-currency"
          || token === "webauthn"
          || token.startsWith("cc-")
        ));
      } catch (_) {
        return false;
      }
    };
    const exactSelector = String(selector || "").trim();
    let matches = [];
    try {
      matches = exactSelector ? Array.from(queryAll(exactSelector)) : [];
    } catch (_) {
      matches = [];
    }
    if (matches.length !== 1) {
      return {
        ok: false,
        action: "click",
        failure_reason: matches.length > 1 ? "control_selector_ambiguous" : "control_not_found",
      };
    }
    const el = matches[0];
    const fullyVisibleInViewport = (node) => {
      // Entirely inside the viewport and inside every scrolling ancestor's box: centring it would
      // only move the page under the model's other points.
      try {
        const box = node.getBoundingClientRect();
        if (!box || box.width <= 0 || box.height <= 0) return false;
        if (box.left < 0 || box.top < 0 || box.right > window.innerWidth || box.bottom > window.innerHeight) return false;
        for (let parent = node.parentElement; parent; parent = parent.parentElement) {
          if (parent.scrollHeight > parent.clientHeight + 1 || parent.scrollWidth > parent.clientWidth + 1) {
            const outer = parent.getBoundingClientRect();
            if (box.left < outer.left || box.top < outer.top || box.right > outer.right || box.bottom > outer.bottom) return false;
          }
        }
        return true;
      } catch (_) {
        return false;
      }
    };
    let scrolledIntoView = false;
    let scrollDelta = [0, 0];
    let scrolledWithinList = false;
    if (shouldScroll && !fullyVisibleInViewport(el)) {
      const scrollers = [];
      for (let parent = el.parentElement; parent; parent = parent.parentElement) {
        if (parent.scrollHeight > parent.clientHeight + 1 || parent.scrollWidth > parent.clientWidth + 1) {
          scrollers.push([parent, parent.scrollLeft, parent.scrollTop]);
        }
      }
      const pageBefore = [window.scrollX || 0, window.scrollY || 0];
      try {
        // An option of a list or menu moves only as far as its own list needs (nearest), never centred, so the
        // list and page stay where the model last saw them.
        const listOption = Boolean(el.closest && el.closest("[role='option'],[role='menuitem'],[role='menuitemradio'],[role='menuitemcheckbox']"))
          && Boolean(el.closest && el.closest("[role='listbox'],[role='menu']"));
        if (typeof el.scrollIntoView === "function") el.scrollIntoView(listOption
          ? { block: "nearest", inline: "nearest", behavior: "auto" }
          : { block: "center", inline: "center", behavior: "auto" });
        scrolledWithinList = listOption;
      } catch (_) {}
      let dx = (window.scrollX || 0) - pageBefore[0];
      let dy = (window.scrollY || 0) - pageBefore[1];
      for (const [parent, left, top] of scrollers) {
        dx += parent.scrollLeft - left;
        dy += parent.scrollTop - top;
      }
      scrolledIntoView = dx !== 0 || dy !== 0;
      scrollDelta = [dx, dy];
    }
    const rect = (typeof globalThis.__lumenTopRect === "function" ? globalThis.__lumenTopRect(el) : el.getBoundingClientRect());
    if (!rect || rect.width <= 0 || rect.height <= 0) {
      return { ok: false, action: "click", failure_reason: "control_not_found" };
    }
    const dpr = window.devicePixelRatio || 1;
    let x = rect.left + rect.width / 2;
    let y = rect.top + rect.height / 2;
    // The model's point, kept as an offset from the element's observed corner, says which part of the
    // element it means: a painted part of a canvas/SVG, or a row of a list box whose rows are not
    // controls themselves (v4_selection run 1682: every row click landed on the list's centre row).
    // The hit test below still requires the point to land on the element or inside it.
    if (Array.isArray(surfaceOffset) && surfaceOffset.length === 2) {
      const offsetX = Number(surfaceOffset[0] || 0) / dpr;
      const offsetY = Number(surfaceOffset[1] || 0) / dpr;
      if (offsetX >= 0 && offsetY >= 0 && offsetX <= rect.width && offsetY <= rect.height) {
        x = rect.left + offsetX;
        y = rect.top + offsetY;
      }
    }
    if (requireVisiblePoint) {
      const pointInViewport = x >= 0 && y >= 0 && x < window.innerWidth && y < window.innerHeight;
      if (!pointInViewport) {
        return {
          ok: false,
          action: "click",
          failure_reason: "control_outside_viewport",
          control_selector_resolved: true,
          x,
          y,
        };
      }
      const hit = elementFromPoint(x, y);
      if (
        !hit
        || !(
          hit === el
          || composedContains(el, hit)
          || composedContains(hit, el)
        )
      ) {
        return {
          ok: false,
          action: "click",
          failure_reason: "control_not_at_resolved_point",
          control_selector_resolved: true,
          x,
          y,
          ...(() => {
            // What the point hit instead, as raw facts: its tag/id/class, its own text, and whether it
            // spans (nearly) the whole viewport.
            try {
              if (!hit || hit.nodeType !== 1) return {};
              const box = hit.getBoundingClientRect();
              const clientW = window.innerWidth || 1;
              const clientH = window.innerHeight || 1;
              const w = Math.max(0, Math.min(box.right, clientW) - Math.max(box.left, 0));
              const h = Math.max(0, Math.min(box.bottom, clientH) - Math.max(box.top, 0));
              const classes = clean(typeof hit.className === "string" ? hit.className : "").split(" ").filter(Boolean).slice(0, 2);
              return {
                hit_tag: (hit.tagName || "").toLowerCase(),
                hit_id: clean(hit.id).slice(0, 40),
                hit_class: classes.join(".").slice(0, 60),
                hit_text: clean(hit.innerText || hit.textContent || hit.getAttribute("aria-label") || "").slice(0, 80),
                hit_covers_viewport: (w * h) / (clientW * clientH) >= 0.8,
              };
            } catch (_) {
              return {};
            }
          })(),
        };
      }
    }
    return {
      ok: true,
      action: "click",
      x,
      y,
      img_x: Math.round(x * dpr),
      img_y: Math.round(y * dpr),
      // How far the content moved to bring the control into view (screenshot px): anything the
      // model pointed at in the same screenshot is now that much higher / further left.
      scrolled_into_view: scrolledIntoView,
      scrolled_within_list: scrolledIntoView && scrolledWithinList,
      scroll_delta_img: [Math.round(scrollDelta[0] * dpr), Math.round(scrollDelta[1] * dpr)],
      target_tag: (el.tagName || "").toLowerCase(),
      target_type: clean(el.getAttribute && el.getAttribute("type")).toLowerCase(),
      target_role: clean(el.getAttribute && el.getAttribute("role")).toLowerCase(),
      target_text: clean(
        sensitiveValueControl(el)
          ? (
            el.getAttribute && (
              el.getAttribute("aria-label")
              || el.getAttribute("placeholder")
              || el.getAttribute("name")
            )
            || ""
          )
          : (
            el.innerText
            || el.textContent
            || associatedLabelText(el)
            || el.getAttribute && el.getAttribute("aria-label")
            || el.getAttribute && el.getAttribute("placeholder")
            || el.getAttribute && el.getAttribute("name")
            || ""
          ),
      ).slice(0, 180).trim(),
      target_selector: exactSelector,
      dispatch_path: ((n) => { const a = n.closest && n.closest('button,a[href],input,textarea,select,label,[role="button"],[role="option"],[role="radio"],[role="checkbox"],[role="combobox"],[tabindex]'); const s = n.closest && n.closest("canvas,svg"); return s && (!a || s === a) ? "cdp_trusted_click" : "dom_activation"; })(el),
      page_url: String(location.href || ""),
    };
  } catch (e) {
    return { ok: false, action: "click", failure_reason: "control_not_found", error: String(e) };
  }
}

function resolveUploadTargetInPage(params) {
  // Self-contained (this runs in the page): the same leading label at a word boundary, or the same
  // characters with different spacing ("First Name *" vs "First Name*").
  const sameLeadingIdentityTextInPage = (left, right) => {
    const a = String(left || "").replace(/\s+/g, " ").trim();
    const b = String(right || "").replace(/\s+/g, " ").trim();
    if (a && a.replace(/\s+/g, "") === b.replace(/\s+/g, "")) return true;
    const [shorter, longer] = a.length <= b.length ? [a, b] : [b, a];
    return shorter.length >= 4 && longer.startsWith(shorter) && (longer.length === shorter.length || longer[shorter.length] === " ");
  };
  try {
    params = params || {};
    const mode = String(params.mode || "point");
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const queryAll = typeof globalThis.__lumenDeepQueryAll === "function"
      ? globalThis.__lumenDeepQueryAll
      : (value) => Array.from(document.querySelectorAll(value));
    const deepElementFromPoint = typeof globalThis.__lumenDeepElementFromPoint === "function"
      ? globalThis.__lumenDeepElementFromPoint
      : (x, y) => document.elementFromPoint(x, y);
    const deepDescendants = typeof globalThis.__lumenDeepDescendants === "function"
      ? globalThis.__lumenDeepDescendants
      : (node, value) => (
        node && node.querySelectorAll ? Array.from(node.querySelectorAll(value)) : []
      );
    const composedParent = typeof globalThis.__lumenComposedParent === "function"
      ? globalThis.__lumenComposedParent
      : (node) => node && node.parentElement;
    const stableSelector = typeof globalThis.__lumenStableDeepSelector === "function"
      ? globalThis.__lumenStableDeepSelector
      : (node) => {
        if (!node) return "";
        const id = node.getAttribute && node.getAttribute("id");
        return id ? `#${id}` : "";
      };
    const cssEscape = (value) => {
      try {
        if (window.CSS && typeof CSS.escape === "function") return CSS.escape(String(value));
      } catch (_) {}
      return String(value).replace(/["\\]/g, "\\$&");
    };
    const isEnabledFileInput = (node) => Boolean(
      node
      && node.matches
      && node.matches('input[type="file"]')
      && !node.disabled
    );
    const firstEnabledFileInput = (node) => {
      if (!node) return null;
      return deepDescendants(node, 'input[type="file"]').find((input) => !input.disabled) || null;
    };
    const enabledFileInputs = (node) => {
      if (!node) return [];
      return deepDescendants(node, 'input[type="file"]').filter((input) => !input.disabled);
    };
    const fileInputForLabel = (label) => {
      if (!label || !label.matches || !label.matches("label")) return null;
      if (label.htmlFor) {
        const root = label.getRootNode && label.getRootNode();
        const byFor = root && typeof root.getElementById === "function"
          ? root.getElementById(label.htmlFor)
          : document.getElementById(label.htmlFor);
        if (isEnabledFileInput(byFor)) return byFor;
      }
      return firstEnabledFileInput(label);
    };
    const associatedFileInput = (anchor) => {
      if (!anchor) return null;
      if (isEnabledFileInput(anchor)) return anchor;
      const nested = firstEnabledFileInput(anchor);
      if (nested) return nested;
      const labelInput = fileInputForLabel(anchor);
      if (labelInput) return labelInput;
      const containingLabel = anchor.closest && anchor.closest("label");
      const containingLabelInput = fileInputForLabel(containingLabel);
      if (containingLabelInput) return containingLabelInput;
      let ancestor = composedParent(anchor);
      for (let depth = 0; ancestor && depth < 6; depth += 1, ancestor = composedParent(ancestor)) {
        const tag = (ancestor.tagName || "").toLowerCase();
        if (tag === "body" || tag === "html" || tag === "form") continue;
        const inputs = enabledFileInputs(ancestor);
        if (inputs.length !== 1) continue;
        const input = inputs[0];
        if (input.parentElement === ancestor || anchor.parentElement === ancestor) return input;
      }
      return null;
    };
    const labelFor = (input, anchor) => {
      if (input && input.id) {
        const root = input.getRootNode && input.getRootNode();
        const explicit = root && root.querySelector
          ? root.querySelector(`label[for="${cssEscape(input.id)}"]`)
          : null;
        const text = explicit ? clean(explicit.innerText || explicit.textContent) : "";
        if (text) return text.slice(0, 120);
      }
      const inputLabel = input && input.closest && input.closest("label");
      const inputLabelText = inputLabel ? clean(inputLabel.innerText || inputLabel.textContent) : "";
      if (inputLabelText) return inputLabelText.slice(0, 120);
      let node = anchor || input;
      for (let i = 0; node && i < 7; i += 1, node = composedParent(node)) {
        const text = clean(node.innerText || node.textContent || node.getAttribute("aria-label") || node.getAttribute("name") || "");
        if (text) return text.slice(0, 120);
      }
      return "";
    };
    const mark = (input, anchor) => {
      const marker = "lumen-vision-upload-" + Date.now() + "-" + Math.random().toString(36).slice(2);
      input.setAttribute("data-lumen-vision-upload-target", marker);
      const targetLabel = labelFor(input, anchor);
      return {
        ok: true,
        action: "upload",
        selector: stableSelector(input) || `[data-lumen-vision-upload-target="${marker}"]`,
        target_label: targetLabel,
        label: targetLabel,
      };
    };

    let anchor = null;
    let x = 0;
    let y = 0;
    if (mode === "selector") {
      const selector = String(params.selector || "");
      let matches = [];
      try {
        matches = selector ? Array.from(queryAll(selector)) : [];
      } catch (_) {
        matches = [];
      }
      if (matches.length !== 1) {
        return {
          ok: false,
          action: "upload",
          failure_reason: matches.length > 1 ? "control_selector_ambiguous" : "upload_control_not_found",
          error: "Upload control was not uniquely resolved.",
        };
      }
      anchor = matches[0];
      const expected = params.expectedTarget && typeof params.expectedTarget === "object" ? params.expectedTarget : {};
      const liveIdentity = {
        target_selector: selector.trim(),
        target_text: clean(
          anchor.innerText
          || anchor.textContent
          || anchor.getAttribute("aria-label")
          || anchor.getAttribute("placeholder")
          || anchor.getAttribute("name")
          || "",
        ).slice(0, 180).trim(),
        target_tag: (anchor.tagName || "").toLowerCase(),
        target_type: clean(anchor.getAttribute("type")).toLowerCase(),
        target_role: clean(anchor.getAttribute("role")).toLowerCase(),
        dispatch_path: ((n) => { const a = n.closest && n.closest('button,a[href],input,textarea,select,label,[role="button"],[role="option"],[role="radio"],[role="checkbox"],[role="combobox"],[tabindex]'); const s = n.closest && n.closest("canvas,svg"); return s && (!a || s === a) ? "cdp_trusted_click" : "dom_activation"; })(anchor),
        page_url: String(location.href || ""),
      };
      const expectedNames = (Array.isArray(expected.target_names) ? expected.target_names : [])
        .map((name) => clean(name))
        .filter(Boolean);
      const mismatchField = ["target_selector", "target_text", "target_tag", "target_type", "target_role", "dispatch_path", "page_url"]
        .find((field) => (
          clean(expected[field]) !== clean(liveIdentity[field])
          && !(field === "target_text" && clean(liveIdentity.target_text) && expectedNames.includes(clean(liveIdentity.target_text)))
          // Same element (the selector is compared exactly): the same label with a changed live tail or
          // different spacing, as for typing and clicks.
          && !(field === "target_text" && clean(liveIdentity.target_text)
            && [expected.target_text, ...expectedNames].some((name) => sameLeadingIdentityTextInPage(name, liveIdentity.target_text)))
        ));
      if (mismatchField) {
        return {
          ok: false,
          action: "upload",
          failure_reason: "control_target_drift",
          target_identity_mismatch_field: mismatchField,
          upload_attempted: false,
        };
      }
    } else {
      const dpr = window.devicePixelRatio || 1;
      x = Math.round(Number(params.imgX || 0) / dpr);
      y = Math.round(Number(params.imgY || 0) / dpr);
      anchor = deepElementFromPoint(x, y);
      if (!anchor) return { ok: false, action: "upload", failure_reason: "upload_target_ambiguous", error: "No upload control under point.", x, y };
    }

    const input = associatedFileInput(anchor);
    if (!input) {
      return {
        ok: false,
        action: "upload",
        failure_reason: mode === "selector" ? "upload_target_not_found" : "upload_target_ambiguous",
        error: mode === "selector" ? "No associated file input for upload control." : "No associated file input under upload point.",
        x,
        y,
      };
    }
    return { ...mark(input, anchor), x, y };
  } catch (e) {
    return { ok: false, action: "upload", failure_reason: "upload_target_ambiguous", error: String(e) };
  }
}

function activeElementEvidenceInPage() {
  try {
    const active = typeof globalThis.__lumenDeepActiveElement === "function"
      ? globalThis.__lumenDeepActiveElement()
      : document.activeElement;
    const activeTag = active ? (active.tagName || "").toLowerCase() : "";
    const activeTypable = Boolean(active && (activeTag === "input" || activeTag === "textarea" || active.isContentEditable));
    const activeHint = activeTypable ? String(active.getAttribute("aria-label") || active.getAttribute("placeholder") || active.getAttribute("name") || "").trim().slice(0, 80) : "";
    const activeHasValue = activeTypable ? Boolean(String(active.value || active.textContent || "").trim()) : false;
    // Which element holds focus, and how much text is selected (lengths only, never the text).
    let activeSelector = "";
    try {
      activeSelector = active && active !== document.body && typeof globalThis.__lumenStableDeepSelector === "function"
        ? String(globalThis.__lumenStableDeepSelector(active) || "")
        : "";
    } catch (_) { activeSelector = ""; }
    let selectionStart = null;
    let selectionEnd = null;
    try {
      if (active && typeof active.selectionStart === "number" && typeof active.selectionEnd === "number") {
        selectionStart = active.selectionStart;
        selectionEnd = active.selectionEnd;
      }
    } catch (_) { selectionStart = null; selectionEnd = null; }
    let selectionLength = selectionStart != null && selectionEnd != null ? Math.max(0, selectionEnd - selectionStart) : 0;
    if (selectionStart == null) {
      try { selectionLength = String(window.getSelection ? window.getSelection() : "").length; } catch (_) { selectionLength = 0; }
    }
    // Sensitive by structure (password, one-time code, payment, WebAuthn token, or a secret-named field):
    // a key never types or pastes into it, whatever the server observed as focused.
    let activeSensitive = false;
    if (activeTypable) {
      const inType = String(active.getAttribute("type") || "").toLowerCase();
      const tokens = String(active.getAttribute("autocomplete") || "").toLowerCase().split(/\s+/).filter(Boolean);
      activeSensitive = (activeTag === "input" && inType === "password")
        || tokens.some((token) => (
          token === "current-password" || token === "new-password" || token === "one-time-code"
          || token === "transaction-amount" || token === "transaction-currency" || token === "webauthn"
          || token.startsWith("cc-")
        ))
        || /pass(word|code|phrase)?|pin|cvv|cvc|csc|ssn|social.?sec|secret|otp|2fa|security.?code|card.?(num|no)|account.?(num|no)|routing|iban|tax.?id|token|api.?key/i.test(
          [active.getAttribute("name"), active.id, active.getAttribute("aria-label"), active.getAttribute("placeholder"), active.getAttribute("data-testid")].join(" "),
        );
    }
    return {
      active_sensitive: activeSensitive,
      active_tag: activeTag,
      active_typable: activeTypable,
      active_input_type: activeTag === "input" ? String(active.type || "").toLowerCase() : "",
      active_hint: activeHint,
      active_has_value: activeHasValue,
      active_selector: activeSelector,
      selection_start: selectionStart,
      selection_end: selectionEnd,
      selection_length: selectionLength,
    };
  } catch (e) { return { ok: false, error: String(e) }; }
}

// The text in the focused editor, read only around a key that commits an edit (Enter, Tab) so the
// receipt can say where it went. Never from a field whose structure is sensitive (password, one-time
// code, payment, WebAuthn): those report no text.
function focusedEditorValueInPage() {
  try {
    const active = typeof globalThis.__lumenDeepActiveElement === "function"
      ? globalThis.__lumenDeepActiveElement()
      : document.activeElement;
    const tag = active ? (active.tagName || "").toLowerCase() : "";
    if (!active || !(tag === "input" || tag === "textarea" || active.isContentEditable)) return { value: "" };
    const inputType = String(active.getAttribute("type") || "").toLowerCase();
    const tokens = String(active.getAttribute("autocomplete") || "").toLowerCase().split(/\s+/).filter(Boolean);
    const sensitive = (tag === "input" && inputType === "password")
      || tokens.some((token) => (
        token === "current-password" || token === "new-password" || token === "one-time-code"
        || token === "transaction-amount" || token === "transaction-currency" || token === "webauthn"
        || token.startsWith("cc-")
      ))
      // Fail closed on a secret field that declares no autocomplete token (a CVV, PIN, SSN, account or
      // token box typed as plain text): withholding the value only costs the read-back.
      || /pass(word|code|phrase)?|pin|cvv|cvc|csc|ssn|social.?sec|secret|otp|2fa|security.?code|card.?(num|no)|account.?(num|no)|routing|iban|tax.?id|token|api.?key/i.test(
        [active.getAttribute("name"), active.id, active.getAttribute("aria-label"), active.getAttribute("placeholder"), active.getAttribute("data-testid")].join(" "),
      );
    if (sensitive) return { value: "" };
    const raw = active.isContentEditable ? active.textContent : active.value;
    return { value: String(raw || "").replace(/\s+/g, " ").trim().slice(0, 160) };
  } catch (_) { return { value: "" }; }
}

function activeValueEvidenceInPage(expectedText) {
  try {
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const deepActiveElement = typeof globalThis.__lumenDeepActiveElement === "function"
      ? globalThis.__lumenDeepActiveElement
      : () => document.activeElement;
    const composedSelector = typeof globalThis.__lumenStableDeepSelector === "function"
      ? globalThis.__lumenStableDeepSelector
      : null;
    const sensitiveValueControl = (node) => {
      try {
        const tag = (node && node.tagName || "").toLowerCase();
        const inputType = clean(node && node.getAttribute && node.getAttribute("type")).toLowerCase();
        if (tag === "input" && inputType === "password") return true;
        const autocompleteTokens = clean(node && node.getAttribute && node.getAttribute("autocomplete"))
          .toLowerCase()
          .split(/\s+/)
          .filter(Boolean);
        return autocompleteTokens.some((token) => (
          token === "current-password"
          || token === "new-password"
          || token === "one-time-code"
          || token === "transaction-amount"
          || token === "transaction-currency"
          || token === "webauthn"
          || token.startsWith("cc-")
        ));
      } catch (_) {
        return false;
      }
    };
    const cssEscape = (value) => {
      try {
        if (window.CSS && typeof CSS.escape === "function") return CSS.escape(String(value));
      } catch (_) {}
      return String(value || "").replace(/[^a-zA-Z0-9_-]/g, (ch) => "\\" + ch);
    };
    const attrValue = (value) => String(value || "").replace(/\\/g, "\\\\").replace(/"/g, '\\"');
    const uniqueSelector = (selector, el) => {
      try {
        if (!selector || selector.length > 200) return "";
        const matches = document.querySelectorAll(selector);
        return matches.length === 1 && matches[0] === el ? selector : "";
      } catch (_) {
        return "";
      }
    };
    const stableSelector = (el) => {
      try {
        const tag = (el.tagName || "").toLowerCase();
        if (!tag) return "";
        const id = el.getAttribute("id");
        if (id) {
          const selector = uniqueSelector(`#${cssEscape(id)}`, el);
          if (selector) return selector;
        }
        for (const attr of ["data-testid", "data-test", "data-qa", "name", "aria-label"]) {
          const value = el.getAttribute(attr);
          if (!value) continue;
          const selector = uniqueSelector(`${tag}[${attr}="${attrValue(value)}"]`, el);
          if (selector) return selector;
        }
        const parts = [];
        let node = el;
        while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 8) {
          const nodeTag = (node.tagName || "").toLowerCase();
          if (!nodeTag) break;
          let part = nodeTag;
          if (node.parentElement) {
            const siblings = Array.from(node.parentElement.children).filter((child) => child.tagName === node.tagName);
            if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
          }
          parts.unshift(part);
          const selector = uniqueSelector(parts.join(" > "), el);
          if (selector) return selector;
          if (nodeTag === "body" || nodeTag === "html") break;
          node = node.parentElement;
        }
      } catch (_) {}
      return "";
    };
    const activeLabel = (el) => {
      const parts = [];
      try {
        const id = el.getAttribute("id");
        if (id) {
          const root = el.getRootNode && el.getRootNode();
          const labels = Array.from(
            root && root.querySelectorAll
              ? root.querySelectorAll(`label[for="${cssEscape(id)}"]`)
              : [],
          ).map((label) => clean(label.innerText || label.textContent));
          parts.push(...labels);
        }
        const containingLabel = el.closest && el.closest("label");
        if (containingLabel) parts.push(clean(containingLabel.innerText || containingLabel.textContent));
        for (const attr of ["aria-label", "placeholder"]) {
          const value = clean(el.getAttribute(attr));
          if (value) parts.push(value);
        }
        let prev = el.previousElementSibling;
        for (let i = 0; i < 3 && prev; i += 1, prev = prev.previousElementSibling) {
          const text = clean(prev.innerText || prev.textContent || prev.getAttribute("aria-label"));
          if (text) parts.push(text);
        }
        const composedParent = typeof globalThis.__lumenComposedParent === "function"
          ? globalThis.__lumenComposedParent
          : (node) => node && node.parentElement;
        let parent = composedParent(el);
        for (let i = 0; i < 3 && parent; i += 1, parent = composedParent(parent)) {
          const text = clean(parent.innerText || parent.textContent || parent.getAttribute("aria-label"));
          if (text && text.length <= 300) parts.push(text);
        }
      } catch (_) {}
      for (const part of parts) {
        if (part) return part.slice(0, 120);
      }
      return "";
    };
    const el = deepActiveElement();
    const typedNode = globalThis.__lumenTypedNode;
    try { globalThis.__lumenTypedNode = null; } catch (_) {}
    const typedElementTag = (node) => String((node && node.tagName) || "").toLowerCase();
    if (
      typedNode
      && typedNode.isConnected === false
      && (!el || !["input", "textarea", "select"].includes(typedElementTag(el)) && !el.isContentEditable)
    ) {
      // The page removed the field it was typed into (a success step swapping it out): the text
      // was probably accepted, but there is no field left to read it back from.
      return {
        ok: true,
        action: "type",
        typed_control_replaced: true,
        active_typable: false,
        active_value_unverified: true,
        note: "control replaced after typing (likely accepted); the typed value could not be read back",
      };
    }
    if (!el) return { ok: false, error: "no focused element" };
    const tag = (el.tagName || "").toLowerCase();
    const typable = Boolean(tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable);
    const selectedOption = tag === "select" && el.selectedIndex >= 0 && el.options ? el.options[el.selectedIndex] : null;
    const value = !typable
      ? ""
      : tag === "select"
        ? clean(selectedOption && (selectedOption.text || selectedOption.label || selectedOption.value))
        : String(el.value || el.textContent || "");
    const valueRedacted = typable && sensitiveValueControl(el);
    const ariaInvalid = typable ? clean(el.getAttribute("aria-invalid")).toLowerCase() : "";
    let validityValid = null;
    try {
      if (typable && el.validity && typeof el.validity.valid === "boolean") validityValid = Boolean(el.validity.valid);
    } catch (_) {}
    // A value read back from a field nobody can see (opacity 0, hidden, zero-size, outside the viewport)
    // proves the field holds it, not that the page consumed it: a page that reacts to key presses (a
    // terminal) leaves such a field unchanged on screen. Reported so the receipt never calls it confirmed.
    let hiddenReason = "";
    if (typable) {
      try {
        const style = window.getComputedStyle(el);
        const box = el.getBoundingClientRect();
        let faded = false;
        for (let node = el, depth = 0; node && node.nodeType === 1 && depth < 12; node = node.parentElement, depth += 1) {
          const opacity = window.getComputedStyle(node).opacity;
          if (opacity !== "" && Number(opacity) === 0) { faded = true; break; }
        }
        if (style.display === "none" || style.visibility === "hidden") hiddenReason = "hidden";
        else if (faded) hiddenReason = "opacity 0";
        else if (typeof el.getClientRects === "function" && el.getClientRects().length === 0) hiddenReason = "";
        else if (box.width < 2 || box.height < 2) hiddenReason = "zero size";
        else if (box.bottom <= 0 || box.right <= 0 || box.top >= window.innerHeight || box.left >= window.innerWidth) hiddenReason = "offscreen";
      } catch (_) {}
    }
    const result = {
      ok: typable,
      action: "type",
      active_tag: tag,
      active_typable: typable,
      ...(hiddenReason ? { active_hidden: true, active_hidden_reason: hiddenReason } : {}),
      active_has_value: Boolean(value.trim()),
      active_selector: typable ? ((composedSelector && composedSelector(el)) || stableSelector(el)) : "",
      active_label: typable ? activeLabel(el) : "",
      active_name: typable ? clean(el.getAttribute("name")).slice(0, 120) : "",
      active_aria_invalid: ariaInvalid,
      active_validity_valid: validityValid,
    };
    if (valueRedacted) {
      result.active_value_redacted = true;
      result.active_value_redaction_reason = "sensitive_input_structure";
      result.active_value_length = value.length;
      result.active_value_matches_expected = value === String(expectedText);
    } else {
      result.active_value = value.slice(0, 500);
      result.active_value_ledger = value.slice(0, 4000);
      // The full length: a capped read-back proves a match only when the field holds as much as was typed.
      result.active_value_length = value.length;
    }
    return result;
  } catch (e) { return { ok: false, error: String(e) }; }
}

function activeValueSetInPage(text) {
  try {
    const deepActiveElement = typeof globalThis.__lumenDeepActiveElement === "function"
      ? globalThis.__lumenDeepActiveElement
      : () => document.activeElement;
    const sensitiveValueControl = (node) => {
      try {
        const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
        const tag = (node && node.tagName || "").toLowerCase();
        const inputType = clean(node && node.getAttribute && node.getAttribute("type")).toLowerCase();
        if (tag === "input" && inputType === "password") return true;
        const autocompleteTokens = clean(node && node.getAttribute && node.getAttribute("autocomplete"))
          .toLowerCase()
          .split(/\s+/)
          .filter(Boolean);
        return autocompleteTokens.some((token) => (
          token === "current-password"
          || token === "new-password"
          || token === "one-time-code"
          || token === "transaction-amount"
          || token === "transaction-currency"
          || token === "webauthn"
          || token.startsWith("cc-")
        ));
      } catch (_) {
        return false;
      }
    };
    const valueEvidence = (node, tag, before, after) => {
      const base = {
        ok: true,
        action: "type",
        active_tag: tag,
        active_typable: true,
        active_has_value: Boolean(String(after || "").trim()),
        active_value_matches_expected: String(after || "") === String(text || ""),
      };
      if (sensitiveValueControl(node)) {
        return {
          ...base,
          active_value_redacted: true,
          active_value_redaction_reason: "sensitive_input_structure",
          active_value_length: String(after || "").length,
          before_has_value: Boolean(String(before || "").trim()),
          before_value_length: String(before || "").length,
          before_value_redacted: true,
        };
      }
      return {
        ...base,
        active_value: String(after || "").slice(0, 500),
        before_value: String(before || "").slice(0, 500),
      };
    };
    const el = deepActiveElement();
    if (!el) return { ok: false, action: "type", error: "no focused element" };
    // Remembered so the read-back can tell a control the page replaced after accepting the text.
    try { globalThis.__lumenTypedNode = el; } catch (_) {}
    const tag = (el.tagName || "").toLowerCase();
    const value = String(text || "");
    if (["input", "textarea", "select"].includes(tag) && (el.readOnly === true || el.disabled === true)) {
      return {
        ok: false,
        action: "type",
        active_tag: tag,
        active_typable: false,
        failure_reason: el.disabled === true ? "control_disabled" : "control_readonly",
        error: el.disabled === true
          ? "The focused field is disabled; typing cannot change it."
          : "The focused field is read-only; typing cannot change it.",
      };
    }
    const dispatch = () => {
      el.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      el.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    };
    if (tag === "input" && ["date", "time", "datetime-local", "month", "week", "range", "color"].includes(String(el.type || "").toLowerCase())) {
      // Special input types hold one canonical string and drop anything else. The typed text is
      // converted to that format; an ambiguous or unrecognised one is refused (never guessed), and
      // the stored value is read back against the canonical form.
      const inputType = String(el.type || "").toLowerCase();
      const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
      const pad = (n, width = 2) => String(n).padStart(width, "0");
      const monthNumber = (word) => {
        const raw = String(word || "").toLowerCase();
        const index = MONTHS.indexOf(raw.slice(0, 3));
        return index >= 0 && /^[a-z]{3,9}$/.test(raw) ? index + 1 : 0;
      };
      const validDate = (y, m, d) => {
        if (!(y >= 1 && y <= 9999 && m >= 1 && m <= 12 && d >= 1)) return false;
        const leap = (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;
        return d <= [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1];
      };
      const realmIntl = (el.ownerDocument && el.ownerDocument.defaultView && el.ownerDocument.defaultView.Intl) || Intl;
      const localeOrder = () => {
        try {
          const holder = el.closest && el.closest("[lang]");
          const lang = String((holder && holder.getAttribute("lang")) || document.documentElement.getAttribute("lang") || "").toLowerCase();
          const [base, region] = lang.split("-");
          if (region === "us" || region === "ph") return "mdy";
          if (["gb", "au", "nz", "ie", "in", "za", "sg", "hk"].includes(region)) return "dmy";
          if (!region && ["fr", "de", "es", "it", "pt", "nl", "ru", "pl", "tr", "sv", "da", "nb", "fi", "cs", "id", "vi", "el", "ro", "uk"].includes(base)) return "dmy";
        } catch (_) {}
        // No page hint: a native date control shows its segments in the browser's own locale order,
        // which is the order the page's own instruction and the user's typing follow.
        try {
          if (!navigator.language) return "";
          const parts = new realmIntl.DateTimeFormat(navigator.language).formatToParts(new Date(2001, 10, 25));
          const names = parts.filter((p) => ["day", "month", "year"].includes(p.type)).map((p) => p.type);
          if (names.join(",") === "month,day,year") return "mdy";
          if (names.join(",") === "day,month,year") return "dmy";
        } catch (_) {}
        return "";
      };
      const fail = (reason, message) => ({
        ok: false,
        action: "type",
        active_tag: tag,
        active_input_type: inputType,
        active_typable: true,
        failure_reason: reason,
        error: message,
      });
      const parseDate = (raw) => {
        const text = String(raw || "").trim().replace(/,/g, " ").replace(/\s+/g, " ");
        let m = text.match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/);
        if (!m && (m = text.match(/^(\d{4})(\d{2})(\d{2})$/)) && !validDate(+m[1], +m[2], +m[3])) m = null;
        if (m) return validDate(+m[1], +m[2], +m[3]) ? { iso: `${pad(m[1], 4)}-${pad(m[2])}-${pad(m[3])}` } : { invalid: true };
        m = text.match(/^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$/) || text.match(/^(\d{2})(\d{2})(\d{4})$/);
        if (m) {
          const a = +m[1];
          const b = +m[2];
          const y = +m[3];
          let order = "";
          if (a > 12 && b <= 12) order = "dmy";
          else if (b > 12 && a <= 12) order = "mdy";
          else if (a === b) order = "mdy";
          else order = localeOrder();
          if (!order) return { ambiguous: true };
          const month = order === "mdy" ? a : b;
          const day = order === "mdy" ? b : a;
          return validDate(y, month, day) ? { iso: `${pad(y, 4)}-${pad(month)}-${pad(day)}` } : { invalid: true };
        }
        m = text.match(/^([A-Za-z]{3,9})\.? (\d{1,2})(?:st|nd|rd|th)? (\d{4})$/);
        if (m && monthNumber(m[1])) return validDate(+m[3], monthNumber(m[1]), +m[2]) ? { iso: `${pad(m[3], 4)}-${pad(monthNumber(m[1]))}-${pad(m[2])}` } : { invalid: true };
        m = text.match(/^(\d{1,2})(?:st|nd|rd|th)? ([A-Za-z]{3,9})\.? (\d{4})$/);
        if (m && monthNumber(m[2])) return validDate(+m[3], monthNumber(m[2]), +m[1]) ? { iso: `${pad(m[3], 4)}-${pad(monthNumber(m[2]))}-${pad(m[1])}` } : { invalid: true };
        return { unrecognized: true };
      };
      const parseTime = (raw) => {
        const m = String(raw || "").trim().toLowerCase().match(/^(\d{1,2})(?::(\d{2}))?(?::(\d{2})(?:\.\d{1,3})?)?\s*([ap]\.?m\.?)?$/);
        if (!m || (m[2] === undefined && !m[4])) return { unrecognized: true };
        let hour = +m[1];
        const minute = m[2] === undefined ? 0 : +m[2];
        const second = m[3] === undefined ? null : +m[3];
        if (m[4]) {
          if (hour < 1 || hour > 12) return { invalid: true };
          hour = (hour % 12) + (m[4][0] === "p" ? 12 : 0);
        }
        if (hour > 23 || minute > 59 || (second !== null && second > 59)) return { invalid: true };
        return { iso: `${pad(hour)}:${pad(minute)}${second === null ? "" : ":" + pad(second)}` };
      };
      let canonical = "";
      let problem = null;
      const text = String(value || "").trim();
      if (inputType === "date") {
        const r = parseDate(text);
        if (r.iso) canonical = r.iso; else problem = r;
      } else if (inputType === "time") {
        const r = parseTime(text);
        if (r.iso) canonical = r.iso; else problem = r;
      } else if (inputType === "datetime-local") {
        const m = text.match(/^(.*?\d{4}|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})(?:T|\s+at\s+|\s+)(.+)$/i);
        const d = m ? parseDate(m[1]) : { unrecognized: true };
        const t = m ? parseTime(m[2]) : { unrecognized: true };
        if (d.iso && t.iso) canonical = `${d.iso}T${t.iso}`; else problem = d.iso ? t : d;
      } else if (inputType === "month") {
        let m = text.match(/^(\d{4})[-/.](\d{1,2})$/);
        let year = 0;
        let month = 0;
        if (m) { year = +m[1]; month = +m[2]; }
        else if ((m = text.match(/^(\d{1,2})[-/.](\d{4})$/))) { year = +m[2]; month = +m[1]; }
        else if ((m = text.match(/^([A-Za-z]{3,9})\.?,? (\d{4})$/)) && monthNumber(m[1])) { year = +m[2]; month = monthNumber(m[1]); }
        if (year && month >= 1 && month <= 12) canonical = `${pad(year, 4)}-${pad(month)}`;
        else problem = year || month ? { invalid: true } : { unrecognized: true };
      } else if (inputType === "week") {
        const m = text.match(/^(\d{4})[-\s]?W(\d{1,2})$/i);
        if (m && +m[2] >= 1 && +m[2] <= 53) canonical = `${m[1]}-W${pad(m[2])}`;
        else problem = m ? { invalid: true } : { unrecognized: true };
      } else if (inputType === "range") {
        if (/^-?\d+(\.\d+)?$/.test(text)) canonical = String(Number(text)); else problem = { unrecognized: true };
      } else if (inputType === "color") {
        let m = text.match(/^#?([0-9a-f]{6})$/i);
        if (m) canonical = `#${m[1].toLowerCase()}`;
        else if ((m = text.match(/^#?([0-9a-f])([0-9a-f])([0-9a-f])$/i))) canonical = `#${m[1]}${m[1]}${m[2]}${m[2]}${m[3]}${m[3]}`.toLowerCase();
        else if ((m = text.match(/^rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)$/i)) && [m[1], m[2], m[3]].every((n) => +n <= 255)) canonical = `#${pad((+m[1]).toString(16))}${pad((+m[2]).toString(16))}${pad((+m[3]).toString(16))}`;
        else problem = { unrecognized: true };
      }
      if (problem) {
        if (problem.ambiguous) {
          return fail("ambiguous_date_format", "ambiguous date format: the typed date reads as either month/day or day/month and the page gives no locale hint; retype it unambiguously (YYYY-MM-DD, or with the month spelled out).");
        }
        return fail(
          problem.invalid ? "special_input_value_invalid" : "special_input_format_unrecognized",
          `The typed text is not a valid ${inputType} value; this ${inputType} input only takes its canonical format.`,
        );
      }
      const before = String(el.value || "");
      const realm = (el.ownerDocument && el.ownerDocument.defaultView) || window;
      const descriptor = Object.getOwnPropertyDescriptor(realm.HTMLInputElement.prototype, "value");
      if (descriptor && typeof descriptor.set === "function") descriptor.set.call(el, canonical);
      else el.value = canonical;
      dispatch();
      const after = String(el.value || "");
      const same = inputType === "range" ? Number(after) === Number(canonical) : after === canonical;
      const evidence = {
        ...valueEvidence(el, tag, before, after),
        active_input_type: inputType,
        normalized_expected_value: canonical,
        active_value_matches_expected: same,
      };
      if (!same) {
        return {
          ...evidence,
          ok: false,
          failure_reason: "value_not_accepted",
          error: `The ${inputType} input holds "${after}" after setting "${canonical}"; it rejected or clamped the value.`,
        };
      }
      return evidence;
    }
    if (tag === "input" || tag === "textarea") {
      const before = String(el.value || "");
      const realm = (el.ownerDocument && el.ownerDocument.defaultView) || window;
      const proto = tag === "textarea" ? realm.HTMLTextAreaElement.prototype : realm.HTMLInputElement.prototype;
      const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
      if (descriptor && typeof descriptor.set === "function") descriptor.set.call(el, value);
      else el.value = value;
      dispatch();
      return valueEvidence(el, tag, before, String(el.value || ""));
    }
    if (el.isContentEditable) {
      const before = String(el.textContent || "");
      el.textContent = value;
      dispatch();
      return valueEvidence(el, tag || "contenteditable", before, String(el.textContent || ""));
    }
    if (tag === "select") {
      // A native select's option list is drawn by the browser, outside the page and every
      // screenshot. Typing an option's exact visible text (or value) selects that option;
      // anything else fails with the options the select really has.
      const squash = (raw) => String(raw || "").replace(/\s+/g, " ").trim();
      const optionText = (option) => squash(option && (option.text || option.label || option.value));
      const options = Array.from(el.options || []);
      const current = el.selectedIndex >= 0 ? options[el.selectedIndex] : null;
      const before = optionText(current);
      const wanted = squash(value).toLowerCase();
      const match = options.find((option) => optionText(option).toLowerCase() === wanted)
        || options.find((option) => squash(option.value).toLowerCase() === wanted);
      if (!wanted || !match) {
        return {
          ok: false,
          action: "type",
          active_tag: tag,
          active_typable: true,
          failure_reason: "select_option_not_found",
          select_options: options.slice(0, 40).map(optionText),
          before_value: before.slice(0, 500),
          error: "No option of this select reads exactly the typed text; type one of select_options.",
        };
      }
      if (match.disabled) {
        return {
          ok: false,
          action: "type",
          active_tag: tag,
          active_typable: true,
          failure_reason: "select_option_disabled",
          select_options: options.slice(0, 40).map(optionText),
          before_value: before.slice(0, 500),
          error: "That option is disabled on this select.",
        };
      }
      const realm = (el.ownerDocument && el.ownerDocument.defaultView) || window;
      const descriptor = Object.getOwnPropertyDescriptor(realm.HTMLSelectElement.prototype, "selectedIndex");
      if (descriptor && typeof descriptor.set === "function") descriptor.set.call(el, match.index);
      else el.selectedIndex = match.index;
      dispatch();
      const selectedNow = el.selectedIndex >= 0 ? options[el.selectedIndex] : null;
      const after = optionText(selectedNow);
      return {
        ...valueEvidence(el, tag, before, after),
        active_value_matches_expected: selectedNow === match,
      };
    }
    return { ok: false, action: "type", active_tag: tag, active_typable: false, error: "focused element is not typable" };
  } catch (e) {
    return { ok: false, action: "type", error: String(e) };
  }
}

function selectActiveTextInPage() {
  try {
    const deepActiveElement = typeof globalThis.__lumenDeepActiveElement === "function"
      ? globalThis.__lumenDeepActiveElement
      : () => document.activeElement;
    const sensitiveValueControl = (node) => {
      try {
        const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
        const tag = (node && node.tagName || "").toLowerCase();
        const inputType = clean(node && node.getAttribute && node.getAttribute("type")).toLowerCase();
        if (tag === "input" && inputType === "password") return true;
        const autocompleteTokens = clean(node && node.getAttribute && node.getAttribute("autocomplete"))
          .toLowerCase()
          .split(/\s+/)
          .filter(Boolean);
        return autocompleteTokens.some((token) => (
          token === "current-password"
          || token === "new-password"
          || token === "one-time-code"
          || token === "transaction-amount"
          || token === "transaction-currency"
          || token === "webauthn"
          || token.startsWith("cc-")
        ));
      } catch (_) {
        return false;
      }
    };
    const selectionEvidence = (node, tag, before) => {
      const base = {
        ok: true,
        action: "select_active_text",
        active_tag: tag,
        active_typable: true,
      };
      if (sensitiveValueControl(node)) {
        return {
          ...base,
          before_has_value: Boolean(String(before || "").trim()),
          before_value_length: String(before || "").length,
          before_value_redacted: true,
        };
      }
      return { ...base, before_value: String(before || "").slice(0, 500) };
    };
    const el = deepActiveElement();
    if (!el) return { ok: false, error: "no focused element" };
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") {
      const before = String(el.value || "");
      try {
        el.focus();
        if (typeof el.select === "function") el.select();
        if (typeof el.setSelectionRange === "function") el.setSelectionRange(0, before.length);
      } catch (_) {}
      return selectionEvidence(el, tag, before);
    }
    if (tag === "select") {
      const current = el.selectedIndex >= 0 && el.options ? el.options[el.selectedIndex] : null;
      return selectionEvidence(el, tag, current ? String(current.text || current.label || "").replace(/\s+/g, " ").trim() : "");
    }
    if (el.isContentEditable) {
      const before = String(el.textContent || "");
      const selection = window.getSelection && window.getSelection();
      if (selection) {
        const range = document.createRange();
        range.selectNodeContents(el);
        selection.removeAllRanges();
        selection.addRange(range);
      }
      return selectionEvidence(el, tag || "contenteditable", before);
    }
    return { ok: false, action: "select_active_text", active_tag: tag, active_typable: false, error: "focused element is not typable" };
  } catch (e) {
    return { ok: false, action: "select_active_text", error: String(e) };
  }
}

function visionClickAtPointInPage(
  imgX,
  imgY,
  allowFinalSubmit = false,
  allowGuardedIntermediate = false,
  expectedTarget = null,
) {
  try {
    const pointInfo = () => {
      try {
        const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
        const associatedLabelText = (node) => {
          try {
            if (!node || !node.tagName) return "";
            const tag = String(node.tagName || "").toLowerCase();
            if (!["input", "select", "textarea"].includes(tag)) return "";
            const squash = (value) => String(value || "").replace(/\s+/g, " ").trim();
            // The observer names a label by its composed text (slotted content included); read it the same way.
            const labelShownText = (ref) => (
              (typeof globalThis.__lumenComposedText === "function" ? globalThis.__lumenComposedText(ref) : "")
              || ref.innerText || ref.textContent || ""
            );
            const root = node.getRootNode && node.getRootNode();
            const byId = (id) => (root && root.getElementById ? root.getElementById(id) : null) || document.getElementById(id);
            const labelledBy = squash(node.getAttribute && node.getAttribute("aria-labelledby"));
            if (labelledBy) {
              const text = squash(labelledBy.split(/\s+/).map((id) => { const ref = byId(id); return ref ? labelShownText(ref) : ""; }).join(" "));
              if (text) return text;
            }
            const id = node.getAttribute && node.getAttribute("id");
            if (id) {
              const escaped = (window.CSS && typeof CSS.escape === "function") ? CSS.escape(id) : String(id).split('"').join('\\"');
              const explicit = (root && root.querySelector ? root.querySelector(`label[for="${escaped}"]`) : null)
                || document.querySelector(`label[for="${escaped}"]`);
              const text = explicit ? squash(labelShownText(explicit)) : "";
              if (text) return text;
            }
            const wrapper = node.closest && node.closest("label");
            const wrapped = wrapper ? squash(labelShownText(wrapper)) : "";
            return wrapped;
          } catch (_) {
            return "";
          }
        };
        const queryAllComposed = (selector) => {
          if (typeof globalThis.__lumenDeepQueryAll === "function") {
            return globalThis.__lumenDeepQueryAll(selector);
          }
          return Array.from(document.querySelectorAll(selector));
        };
        const deepElementFromPoint = (x, y) => {
          if (typeof globalThis.__lumenDeepElementFromPoint === "function") {
            return globalThis.__lumenDeepElementFromPoint(x, y);
          }
          return document.elementFromPoint(x, y);
        };
        const deepActiveElement = () => (
          typeof globalThis.__lumenDeepActiveElement === "function"
            ? globalThis.__lumenDeepActiveElement()
            : document.activeElement
        );
        const composedParent = (node) => (
          typeof globalThis.__lumenComposedParent === "function"
            ? globalThis.__lumenComposedParent(node)
            : (node && node.parentElement)
        );
        const composedContains = (ancestor, node) => {
          if (typeof globalThis.__lumenComposedContains === "function") {
            return globalThis.__lumenComposedContains(ancestor, node);
          }
          return Boolean(ancestor && node && ancestor.contains && ancestor.contains(node));
        };
        const deepDescendants = (node, selector) => (
          typeof globalThis.__lumenDeepDescendants === "function"
            ? globalThis.__lumenDeepDescendants(node, selector)
            : Array.from((node && node.querySelectorAll && node.querySelectorAll(selector)) || [])
        );
        const sensitiveValueControl = (node) => {
          try {
            const tag = (node && node.tagName || "").toLowerCase();
            const inputType = clean(node && node.getAttribute && node.getAttribute("type")).toLowerCase();
            if (tag === "input" && inputType === "password") return true;
            const autocompleteTokens = clean(node && node.getAttribute && node.getAttribute("autocomplete"))
              .toLowerCase()
              .split(/\s+/)
              .filter(Boolean);
            return autocompleteTokens.some((token) => (
              token === "current-password"
              || token === "new-password"
              || token === "one-time-code"
              || token === "transaction-amount"
              || token === "transaction-currency"
              || token === "webauthn"
              || token.startsWith("cc-")
            ));
          } catch (_) {
            return false;
          }
        };
        const controlText = (node) => clean(
          node
            ? (
              sensitiveValueControl(node)
                ? (
                  node.getAttribute && (
                    node.getAttribute("aria-label")
                    || node.getAttribute("placeholder")
                    || node.getAttribute("name")
                  )
                  || ""
                )
                : (
                  node.innerText
                  || node.textContent
                  || associatedLabelText(node)
                  || node.getAttribute && node.getAttribute("aria-label")
                  || node.getAttribute && node.getAttribute("placeholder")
                  || node.getAttribute && node.getAttribute("name")
                  || ""
                )
            )
            : "",
        );
        const cssEscape = (value) => {
          try {
            if (window.CSS && typeof CSS.escape === "function") return CSS.escape(String(value));
          } catch (_) {}
          return String(value || "").replace(/["\\]/g, "\\$&");
        };
        const attrValue = (value) => String(value || "").replace(/\\/g, "\\\\").replace(/"/g, '\\"');
        const uniqueSelector = (selector, node) => {
          try {
            const matches = queryAllComposed(selector);
            return Boolean(
              selector
              && selector.length <= 200
              && matches.length === 1
              && matches[0] === node
            );
          } catch (_) {
            return false;
          }
        };
        const stableSelector = (node) => {
          try {
            if (!node || !node.tagName) return "";
            if (typeof globalThis.__lumenStableDeepSelector === "function") {
              const composed = globalThis.__lumenStableDeepSelector(node);
              if (composed) return composed;
            }
            const tag = (node.tagName || "").toLowerCase();
            const id = node.getAttribute("id");
            if (id) {
              const selector = `#${cssEscape(id)}`;
              if (uniqueSelector(selector, node)) return selector;
            }
            const name = node.getAttribute("name");
            if (name) {
              const selector = `${tag}[name="${attrValue(name)}"]`;
              if (uniqueSelector(selector, node)) return selector;
            }
            const type = node.getAttribute("type");
            if (type && tag === "input") {
              const selector = `${tag}[type="${attrValue(type)}"]`;
              if (uniqueSelector(selector, node)) return selector;
            }
            let current = node;
            const parts = [];
            for (let depth = 0; current && current.nodeType === 1 && depth < 5; depth += 1, current = current.parentElement) {
              const currentTag = (current.tagName || "").toLowerCase();
              if (!currentTag || currentTag === "html") break;
              const currentId = current.getAttribute("id");
              if (currentId) {
                parts.unshift(`#${cssEscape(currentId)}`);
              } else {
                const parent = current.parentElement;
                const siblings = parent ? Array.from(parent.children).filter((sib) => (sib.tagName || "").toLowerCase() === currentTag) : [];
                const nth = siblings.length > 1 ? `:nth-of-type(${siblings.indexOf(current) + 1})` : "";
                parts.unshift(`${currentTag}${nth}`);
              }
              const selector = parts.join(" > ");
              if (uniqueSelector(selector, node)) return selector;
              if (currentId) break;
              if (currentTag === "body") break;
            }
            return "";
          } catch (_) {
            return "";
          }
        };
        const isEnabledFileInput = (node) => Boolean(
          node
          && node.matches
          && node.matches('input[type="file"]')
          && !node.disabled
        );
        const firstEnabledFileInput = (node) => {
          if (!node) return null;
          return deepDescendants(node, 'input[type="file"]').find((input) => !input.disabled) || null;
        };
        const enabledFileInputs = (node) => {
          if (!node) return [];
          return deepDescendants(node, 'input[type="file"]').filter((input) => !input.disabled);
        };
        const fileInputForLabel = (label) => {
          if (!label || !label.matches || !label.matches("label")) return null;
          if (label.htmlFor) {
            const root = label.getRootNode && label.getRootNode();
            const byFor = (root && root.getElementById && root.getElementById(label.htmlFor))
              || document.getElementById(label.htmlFor);
            if (isEnabledFileInput(byFor)) return byFor;
          }
          return firstEnabledFileInput(label);
        };
        const associatedFileInput = (anchor) => {
          if (!anchor) return null;
          if (isEnabledFileInput(anchor)) return anchor;
          const nested = firstEnabledFileInput(anchor);
          if (nested) return nested;
          const labelInput = fileInputForLabel(anchor);
          if (labelInput) return labelInput;
          const containingLabel = anchor.closest && anchor.closest("label");
          const containingLabelInput = fileInputForLabel(containingLabel);
          if (containingLabelInput) return containingLabelInput;
          let ancestor = composedParent(anchor);
          for (let depth = 0; ancestor && depth < 4; depth += 1, ancestor = composedParent(ancestor)) {
            const tag = (ancestor.tagName || "").toLowerCase();
            if (tag === "body" || tag === "html" || tag === "form") continue;
            const inputs = enabledFileInputs(ancestor);
            if (inputs.length !== 1) continue;
            const input = inputs[0];
            if (input.parentElement === ancestor || anchor.parentElement === ancestor) return input;
          }
          return null;
        };
        const labelFor = (input, anchor) => {
          if (input && input.id) {
            const root = input.getRootNode && input.getRootNode();
            const explicit = (root && root.querySelector && root.querySelector(`label[for="${cssEscape(input.id)}"]`))
              || document.querySelector(`label[for="${cssEscape(input.id)}"]`);
            const text = explicit ? clean(explicit.innerText || explicit.textContent) : "";
            if (text) return text.slice(0, 120);
          }
          const inputLabel = input && input.closest && input.closest("label");
          const inputLabelText = inputLabel ? clean(inputLabel.innerText || inputLabel.textContent) : "";
          if (inputLabelText) return inputLabelText.slice(0, 120);
          let node = anchor || input;
          for (let i = 0; node && i < 5; i += 1, node = composedParent(node)) {
            const text = clean(node.innerText || node.textContent || node.getAttribute("aria-label") || node.getAttribute("name") || "");
            if (text) return text.slice(0, 120);
          }
          return "";
        };
        const dpr = window.devicePixelRatio || 1;
        let x = Math.round(imgX / dpr), y = Math.round(imgY / dpr); // image px -> CSS px
        const pointHit = deepElementFromPoint(x, y);
        let el = pointHit;
        let exactlyBound = false;
        const actionableSelector = 'button,a[href],input,textarea,select,label,[role="button"],[role="option"],[role="radio"],[role="checkbox"],[role="combobox"],[tabindex]';
        // Verbatim, never whitespace-collapsed: an attribute value in a selector can hold a double space
        // (SmartRecruiters' spl-icon[aria-label="Clear  value"], run 75, then matched nothing and every click drifted).
        const expectedSelectorAtPoint = String((expectedTarget && expectedTarget.target_selector) || "").trim();
        if (pointHit && expectedSelectorAtPoint) {
          let exactMatches = [];
          try { exactMatches = queryAllComposed(expectedSelectorAtPoint); } catch (_) { exactMatches = []; }
          if (exactMatches.length === 1) {
            const exactTarget = exactMatches[0];
            const rect = (typeof globalThis.__lumenTopRect === "function" ? globalThis.__lumenTopRect(exactTarget) : exactTarget.getBoundingClientRect());
            const pointInsideTarget = Boolean(
              rect
              && rect.width > 0
              && rect.height > 0
              && x >= rect.left
              && x <= rect.right
              && y >= rect.top
              && y <= rect.bottom
            );
            if (
              pointInsideTarget
              && (
                pointHit === exactTarget
                || composedContains(pointHit, exactTarget)
                || composedContains(exactTarget, pointHit)
                  // Layered pieces of one SVG drawing (a label over its slice, an icon over its button shape): the
                  // hit shape and the enumerated piece sit under the same point in the same <svg>.
                  || (() => {
                    try {
                      if (allowFinalSubmit || allowGuardedIntermediate) return false; // strict dispatch lets the point's hit through: no sibling piece
                      const surface = exactTarget.closest && exactTarget.closest("svg");
                      if (!surface || !pointHit.closest || pointHit.closest("svg") !== surface) return false;
                      return document.elementsFromPoint(x, y).includes(exactTarget);
                    } catch (_) { return false; }
                  })()
              )
            ) {
              el = exactTarget;
              exactlyBound = true;
            }
          }
        }
        // Semantic identity of the control the model chose: tag + type + role + visible text. A page
        // that re-renders (fresh ids, shuffled positions) keeps that identity even though the exact
        // selector and the coordinates go stale. Ordinary clicks may follow the control; final-submit
        // and guarded clicks never rebind - their authority was granted for one exact element.
        const expectedIdentityTag = clean(expectedTarget && expectedTarget.target_tag).toLowerCase();
        const expectedIdentityRawType = clean(expectedTarget && expectedTarget.target_type).toLowerCase();
        const expectedIdentity = {
          tag: expectedIdentityTag,
          // A type equal to the tag is the observer's "no type attribute" fallback.
          type: expectedIdentityRawType === expectedIdentityTag ? "" : expectedIdentityRawType,
          role: clean(expectedTarget && expectedTarget.target_role).toLowerCase(),
          text: clean(expectedTarget && expectedTarget.target_text).slice(0, 180).trim(),
          // Every name the observer recorded for this control (its own text, label, aria-label): an exact
          // live name equal to one of them is the same control. sameIdentity reads this list.
          names: (Array.isArray(expectedTarget && expectedTarget.target_names) ? expectedTarget.target_names : [])
            .map((name) => clean(name).slice(0, 180).trim())
            .filter(Boolean),
        };
        const semanticRebindAllowed = Boolean(
          expectedTarget
          && expectedIdentity.tag
          && expectedIdentity.text
          && !allowFinalSubmit
          && !allowGuardedIntermediate
        );
        const sameIdentity = (node) => {
          try {
            if (!node || !node.tagName) return false;
            return (
              (node.tagName || "").toLowerCase() === expectedIdentity.tag
              && clean(node.getAttribute && node.getAttribute("type")).toLowerCase() === expectedIdentity.type
              && clean(node.getAttribute && node.getAttribute("role")).toLowerCase() === expectedIdentity.role
              && (
            controlText(node).slice(0, 180).trim() === expectedIdentity.text
            || expectedIdentity.names.includes(controlText(node).slice(0, 180).trim())
          )
            );
          } catch (_) {
            return false;
          }
        };
        const sameIdentityAncestor = (node) => {
          let up = node && node.parentElement;
          for (let depth = 0; up && depth < 3; depth += 1, up = up.parentElement) {
            if (sameIdentity(up)) return true;
          }
          return false;
        };
        let rebound = false;
        let reboundFrom = null;
        if (semanticRebindAllowed && !exactlyBound) {
          const hitActionable = pointHit && pointHit.closest ? (pointHit.closest(actionableSelector) || pointHit) : pointHit;
          if (!(hitActionable && sameIdentity(hitActionable))) {
            let candidates = [];
            try {
              candidates = queryAllComposed(expectedIdentity.tag).filter((node) => {
                if (!sameIdentity(node) || node.disabled) return false;
                const rect = (typeof globalThis.__lumenTopRect === "function" ? globalThis.__lumenTopRect(node) : node.getBoundingClientRect());
                return (
                  rect.width > 0
                  && rect.height > 0
                  && rect.left >= 0
                  && rect.top >= 0
                  && rect.right <= (window.innerWidth || 0)
                  && rect.bottom <= (window.innerHeight || 0)
                  && getComputedStyle(node).visibility !== "hidden"
                );
              });
            } catch (_) {
              candidates = [];
            }
            if (candidates.length === 1) {
              const rect = (typeof globalThis.__lumenTopRect === "function" ? globalThis.__lumenTopRect(candidates[0]) : candidates[0].getBoundingClientRect());
              const cx = Math.round(rect.left + rect.width / 2);
              const cy = Math.round(rect.top + rect.height / 2);
              const topAtCenter = deepElementFromPoint(cx, cy);
              if (topAtCenter && (topAtCenter === candidates[0] || composedContains(candidates[0], topAtCenter))) {
                reboundFrom = { x, y };
                x = cx;
                y = cy;
                el = topAtCenter;
                rebound = true;
              }
            }
          }
        }
        if (!el) return { ok: false, error: "nothing at point", x, y };
        // SAFETY FLOOR: a coordinate click must never land on a final submit. Walk a few ancestors.
        const submitDetection = (n) => {
          try {
            const tag = (n.tagName || "").toLowerCase();
            const typeAttribute = clean(n.getAttribute && n.getAttribute("type")).toLowerCase();
            const resolvedType = clean(n.type || typeAttribute).toLowerCase();
            const role = clean(n.getAttribute && n.getAttribute("role")).toLowerCase();
            const text = controlText(n).slice(0, 120);
            const insideForm = Boolean(n.closest && n.closest("form"));
            const raw = {
              tag,
              type_attribute: typeAttribute,
              resolved_type: resolvedType,
              role,
              inside_form: insideForm,
              form_owner: clean((n.form && n.form.id) || (n.getAttribute && n.getAttribute("form"))),
              href: clean(n.getAttribute && n.getAttribute("href")).slice(0, 240),
              text,
            };
            const explicitSubmitType = typeAttribute === "submit" || (tag === "input" && typeAttribute === "image");
            const formAssociatedSubmit = Boolean(n.form) && (
              (tag === "button" && resolvedType === "submit")
              || (tag === "input" && (resolvedType === "submit" || resolvedType === "image"))
            );
            const nativeSubmit = explicitSubmitType || formAssociatedSubmit;
            if (nativeSubmit) return { matches: true, basis: "native_submit", ...raw };
            const t = text.toLowerCase();
            const formFieldCount = queryAllComposed("input, textarea, select, [contenteditable='true']").length;
            const formPage = formFieldCount >= 3 || /apply|application|checkout|booking|reservation|purchase/.test(String(location.href || "").toLowerCase());
            const contextualMatch = (formPage && /^(apply|submit|send application|complete application)$/i.test(t))
              || (/\bsubmit\b|\bplace order\b|\bpay now\b|\bconfirm and submit\b/.test(t) && t.length < 40);
            return { matches: Boolean(contextualMatch), basis: contextualMatch ? "contextual" : "", form_field_count: formFieldCount, ...raw };
          } catch (_) { return { matches: false, basis: "" }; }
        };
        let probe = el;
        let firstSubmitLikeTarget = null;
        let firstSubmitDetection = null;
        let finalSubmitTarget = null;
        let finalSubmitDetection = null;
        const closestActionable = el.closest && el.closest('button,input[type="submit"],input[type="button"],input[type="image"],[role="button"]');
        if (closestActionable) {
          const detection = submitDetection(closestActionable);
          if (detection.matches) {
            finalSubmitTarget = closestActionable;
            finalSubmitDetection = detection;
          }
        }
        // A plain wrapper whose own text collapses to a sibling button's label ("Apply" beside a coupon field) is not a
        // submit: the click landed on a control inside it (a field, another button), never on the wrapper itself.
        const wrapsOtherControl = (node) => {
          try {
            const tag = (node.tagName || "").toLowerCase();
            const role = clean(node.getAttribute && node.getAttribute("role")).toLowerCase();
            if (tag === "button" || tag === "input" || role === "button") return false;
            return Boolean(node.querySelector && node.querySelector('button,input,select,textarea,[role="button"],[contenteditable="true"]'));
          } catch (_) { return false; }
        };
        for (let i = 0; i < 8 && probe && !finalSubmitTarget; i++) {
          const detection = submitDetection(probe);
          if (detection.matches && !wrapsOtherControl(probe)) {
            if (!firstSubmitLikeTarget) {
              firstSubmitLikeTarget = probe;
              firstSubmitDetection = detection;
            }
            const tag = (probe.tagName || "").toLowerCase();
            const role = clean(probe.getAttribute && probe.getAttribute("role")).toLowerCase();
            if (tag === "button" || tag === "input" || role === "button") {
              finalSubmitTarget = probe;
              finalSubmitDetection = detection;
              break;
            }
          }
          probe = composedParent(probe);
        }
        const guardedIntermediateRevalidated = Boolean(
          allowGuardedIntermediate
          && finalSubmitTarget
          && finalSubmitDetection
          && finalSubmitDetection.basis === "contextual"
          && finalSubmitDetection.tag === "button"
          // A formless typeless button resolves to "submit" but has nothing to natively submit.
          && (finalSubmitDetection.resolved_type === "button" || finalSubmitDetection.resolved_type === "submit")
          && !finalSubmitDetection.inside_form
          && !finalSubmitDetection.form_owner
        );
        if (finalSubmitTarget && !allowFinalSubmit && !guardedIntermediateRevalidated) {
          const label = controlText(finalSubmitTarget).slice(0, 60);
          const targetSelector = stableSelector(finalSubmitTarget);
          const targetTag = (finalSubmitTarget.tagName || "").toLowerCase();
          return {
            ok: false,
            blocked_submit: true,
            selector: targetSelector,
            target_selector: targetSelector,
            target_text: controlText(finalSubmitTarget).slice(0, 180).trim(),
            target_tag: targetTag,
            target_type: clean(finalSubmitTarget.getAttribute && finalSubmitTarget.getAttribute("type")).toLowerCase(),
            target_role: clean(finalSubmitTarget.getAttribute && finalSubmitTarget.getAttribute("role")).toLowerCase(),
            dispatch_path: "dom_activation",
            page_url: String(location.href || ""),
            target_label: label,
            label,
            x,
            y,
            submit_detection_basis: (finalSubmitDetection || {}).basis || "",
            submit_detection: finalSubmitDetection || {},
          };
        }
        if (firstSubmitLikeTarget && !allowFinalSubmit && !guardedIntermediateRevalidated) {
          const label = controlText(firstSubmitLikeTarget).slice(0, 60);
          return {
            ok: false,
            blocked_submit: true,
            selector: "",
            target_label: label,
            label,
            x,
            y,
            submit_detection_basis: (firstSubmitDetection || {}).basis || "",
            submit_detection: firstSubmitDetection || {},
          };
        }
        const input = associatedFileInput(el);
        if (input) {
          const targetLabel = labelFor(input, el);
          return {
            ok: false,
            blocked_file_dialog: true,
            failure_reason: "click_would_open_file_dialog",
            target_label: targetLabel,
            label: targetLabel,
            x,
            y,
          };
        }
        if (allowFinalSubmit && !finalSubmitTarget && !exactlyBound) {
          const label = controlText(el).slice(0, 60);
          return {
            ok: false,
            failure_reason: "final_submit_target_mismatch",
            target_label: label,
            label,
            selector: stableSelector(el),
            x,
            y,
          };
        }
        if (allowGuardedIntermediate && !guardedIntermediateRevalidated) {
          const label = controlText(el).slice(0, 60);
          return {
            ok: false,
            failure_reason: "guarded_intermediate_target_mismatch",
            target_label: label,
            label,
            selector: stableSelector(el),
            x,
            y,
          };
        }
        const domActionableTarget = (el.closest && el.closest(actionableSelector)) || null;
        const coordinateSurface = (el.closest && el.closest("canvas,svg")) || null;
        const coordinateSurfaceIsTarget = Boolean(
          coordinateSurface
          && (!domActionableTarget || coordinateSurface === domActionableTarget),
        );
        const expectedSelector = String((expectedTarget && expectedTarget.target_selector) || "").trim();
        // The observed control can sit between the hit and its actionable ancestor: Lever renders a
        // location field's suggestion list inside the field's <label>, so the ancestor was the whole
        // label and every click on a suggestion was refused as drift (run 1792). The one node the
        // expected selector names that contains the hit, inside that ancestor, is the clicked control.
        let nestedExpected = null;
        if (expectedSelector && domActionableTarget) {
          try {
            const named = queryAllComposed(expectedSelector);
            if (named.length === 1 && named[0] !== domActionableTarget && named[0].contains(el) && domActionableTarget.contains(named[0])) {
              nestedExpected = named[0];
            }
          } catch (_) {
            nestedExpected = null;
          }
        }
        const interactionTarget = nestedExpected || domActionableTarget || el;
        const tag = (interactionTarget.tagName || "").toLowerCase();
        const role = clean(interactionTarget.getAttribute("role")).toLowerCase();
        let boundTargetSelector = "";
        if (expectedSelector) {
          let expectedNodes = [];
          try {
            expectedNodes = queryAllComposed(expectedSelector);
          } catch (_) {
            expectedNodes = [];
          }
          const exactMatch = expectedNodes.length === 1 && expectedNodes[0] === interactionTarget;
          const semanticMatch = !exactMatch && semanticRebindAllowed
            && (sameIdentity(interactionTarget) || sameIdentityAncestor(interactionTarget));
          if (!exactMatch && !semanticMatch) {
            return {
              ok: false,
              action: "click",
              mode: "click_target_revalidation",
              failure_reason: "click_target_drift",
              target_identity_matched: false,
              target_identity_mismatch_field: "target_selector",
              preflight_target_selector: expectedSelector,
              live_target_selector: stableSelector(interactionTarget),
              activation_attempted: false,
              activation_dispatched: false,
              activation_count: 0,
              x,
              y,
            };
          }
          boundTargetSelector = exactMatch ? expectedSelector : stableSelector(interactionTarget);
        }
        const menu = interactionTarget.closest && interactionTarget.closest('[role="listbox"],[role="menu"]');
        const active = deepActiveElement();
        const activeTag = active ? (active.tagName || "").toLowerCase() : "";
        const activeTypable = Boolean(active && (activeTag === "input" || activeTag === "textarea" || active.isContentEditable));
        const activeValue = activeTypable ? String(active.value || active.textContent || "") : "";
        const activeValueRedacted = activeTypable && sensitiveValueControl(active);
        const rawControlValue = (node) => clean(
          node
            ? (node.value || node.getAttribute("aria-valuetext") || node.innerText || node.textContent || "")
            : "",
        ).slice(0, 500);
        const optionOwner = () => {
          if (!(role === "option" || tag === "option")) return null;
          if (tag === "option" && interactionTarget.parentElement && (interactionTarget.parentElement.tagName || "").toLowerCase() === "select") {
            return interactionTarget.parentElement;
          }
          const menuId = clean(menu && menu.getAttribute("id"));
          if (menuId) {
            let related = [];
            try {
              related = queryAllComposed(`[aria-controls="${attrValue(menuId)}"],[aria-owns="${attrValue(menuId)}"]`);
            } catch (_) {
              related = [];
            }
            if (active && related.includes(active)) return active;
            if (related.length === 1) return related[0];
          }
          const activeRole = clean(active && active.getAttribute && active.getAttribute("role")).toLowerCase();
          if (active && activeRole === "combobox") return active;
          return null;
        };
        const owner = optionOwner();
        const ownerTag = owner ? (owner.tagName || "").toLowerCase() : "";
        const ownerTypable = Boolean(owner && (ownerTag === "input" || ownerTag === "textarea" || owner.isContentEditable));
        const ownerValueRedacted = Boolean(owner && sensitiveValueControl(owner));
        const targetText = controlText(interactionTarget).slice(0, 180).trim();
        const ownerValueBefore = rawControlValue(owner);
        const ownerMatchesBefore = Boolean(targetText && ownerValueBefore && ownerValueBefore === targetText);
        let activeValidityValid = null;
        try {
          if (activeTypable && active.validity && typeof active.validity.valid === "boolean") activeValidityValid = Boolean(active.validity.valid);
        } catch (_) {}
        return {
          ok: true,
          x,
          y,
          rebound,
          rebound_from: reboundFrom,
          hit_tag: (el.tagName || "").toLowerCase(),
          tag,
          label: targetText.slice(0, 60),
          target_selector: boundTargetSelector || stableSelector(interactionTarget),
          target_type: clean(interactionTarget.getAttribute && interactionTarget.getAttribute("type")).toLowerCase(),
          target_role: role,
          target_text: targetText,
          target_option_like: Boolean(role === "option" || tag === "option"),
          // Structural value control at the point (mirrors the server's _control_is_structural_choice).
          target_value_control: (() => {
            try {
              const inputType = clean(interactionTarget.getAttribute("type")).toLowerCase();
              const tokens = [tag, inputType, role];
              if (tokens.some((t) => ["radio", "checkbox", "option", "switch", "menuitemradio", "menuitemcheckbox"].includes(t))) return true;
              if (tokens.includes("tab")) return false;
              return ["aria-checked", "aria-pressed", "aria-selected"].some((a) => ["true", "false", "mixed"].includes(clean(interactionTarget.getAttribute(a)).toLowerCase()));
            } catch (_) { return false; }
          })(),
          // Values near the control (a quantity beside a stepper, an aria-controls target) before the click.
          linked_before: (() => {
            const out = [];
            try {
              const node = interactionTarget;
              if (!node) return out;
              const seen = new Set([node]);
              const items = [];
              const read = (n) => clean(
                n.value !== undefined && n.value !== "" && typeof n.value === "string"
                  ? n.value
                  : (n.getAttribute("aria-valuetext") || n.innerText || n.textContent || ""),
              ).slice(0, 80);
              // A clock, timer or live counter changes by itself and proves nothing about the click.
              const selfChanging = (n) => {
                try {
                  const role = clean(n.getAttribute("role")).toLowerCase();
                  if (role === "timer" || (n.tagName || "").toLowerCase() === "time") return true;
                  if (n.querySelector && n.querySelector('time,[role="timer"]')) return true;
                  return /^\d{1,2}:\d{2}(:\d{2})?(\s?[ap]m)?$/i.test(read(n));
                } catch (_) { return true; }
              };
              // tier "linked": named by the control (aria-controls / aria-describedby) or in its form;
              // tier "sibling": positional neighbours, the weakest linkage.
              const add = (n, tier) => {
                if (!n || n.nodeType !== 1 || seen.has(n) || items.length >= 8) return;
                seen.add(n);
                if (n.contains(node) || node.contains(n)) return;
                if (n.matches && n.matches('button,a[href],[role="button"],script,style,head,body,html')) return;
                if (clean(n.textContent).length > 120) return;
                if (sensitiveValueControl(n) || Array.from(n.querySelectorAll("input,textarea,select")).some(sensitiveValueControl)) return;
                if (selfChanging(n)) return;
                items.push({ el: n, tier, before: read(n), spontaneous: false });
              };
              for (const attr of ["aria-controls", "aria-describedby"]) {
                for (const id of clean(node.getAttribute(attr)).split(" ").filter(Boolean)) add(document.getElementById(id), "linked");
              }
              try {
                if (node.form) for (const n of Array.from(node.form.querySelectorAll("output,[aria-live]"))) add(n, "linked");
              } catch (_) {}
              const parent = node.parentElement;
              if (parent) for (const child of Array.from(parent.children)) add(child, "sibling");
              const grand = parent && parent.parentElement;
              if (grand) for (const child of Array.from(grand.children)) add(child, "sibling");
              // Element identity, not position, is what the outcome compares; the immediate probe takes a
              // second reading so anything that changes without the click is excluded.
              globalThis.__lumenLinkedProbe = { node, items, at: Date.now() };
              for (const item of items) out.push(item.before);
            } catch (_) {}
            return out;
          })(),
          // The control's own state attributes before the click; the outcome compares them after.
          target_state_before: (() => {
            try {
              const g = (n) => clean(interactionTarget.getAttribute && interactionTarget.getAttribute(n));
              return ["aria-pressed", "aria-expanded", "aria-current", "aria-checked", "aria-selected", "data-state", "data-selected", "data-checked", "data-active"]
                .map((n) => n + "=" + g(n)).join("|") + "|class=" + g("class").split(/\s+/).filter(Boolean).sort().join(" ");
            } catch (_) { return ""; }
          })(),
          target_selected_before: Boolean(
            interactionTarget.selected
            || interactionTarget.checked
            || interactionTarget.getAttribute("aria-selected") === "true"
            || interactionTarget.getAttribute("aria-checked") === "true"
            || (!ownerTypable && ownerMatchesBefore)
          ),
          menu_selector: stableSelector(menu),
          menu_was_open: Boolean(menu),
          active_selector_before: activeTypable ? stableSelector(active) : "",
          active_value_before: activeValueRedacted ? "[REDACTED]" : activeValue.slice(0, 500),
          active_value_redacted: activeValueRedacted,
          active_aria_invalid_before: activeTypable ? clean(active.getAttribute("aria-invalid")).toLowerCase() : "",
          active_validity_valid_before: activeValidityValid,
          owner_selector: stableSelector(owner),
          owner_tag: ownerTag,
          owner_typable: ownerTypable,
          owner_value_before: ownerValueRedacted ? "[REDACTED]" : ownerValueBefore,
          owner_value_redacted: ownerValueRedacted,
          owner_has_value_before: Boolean(ownerValueBefore),
          owner_matches_target_before: ownerMatchesBefore,
          final_submit_revalidated: Boolean(finalSubmitTarget),
          final_submit_selector: finalSubmitTarget ? stableSelector(finalSubmitTarget) : "",
          guarded_intermediate_revalidated: guardedIntermediateRevalidated,
          submit_detection_basis: (finalSubmitDetection || {}).basis || "",
          submit_detection: finalSubmitDetection || {},
          dispatch_path: coordinateSurfaceIsTarget ? "cdp_trusted_click" : "dom_activation",
          dispatch_basis: coordinateSurfaceIsTarget
            ? "coordinate_rendered_surface"
            : domActionableTarget
              ? "dom_actionable_target"
              : "coordinate_target",
          page_url: String(location.href || ""),
        };
      } catch (e) { return { ok: false, error: String(e) }; }
    };
    const targetIdentityVerdict = (expected, live) => {
      if (!expected || typeof expected !== "object") return { ok: true, reason: "" };
      const text = (value) => String(value || "").trim();
      const expectedSelector = text(expected.target_selector);
      const liveSelector = text(live && live.target_selector);
      const expectedTag = text(expected.target_tag || expected.tag);
      const liveTag = text(live && (live.target_tag || live.tag));
      const expectedDispatch = text(expected.dispatch_path);
      const liveDispatch = text(live && live.dispatch_path);
      const expectedUrl = text(expected.page_url);
      const liveUrl = text(live && live.page_url);
      if (!expectedTag || !liveTag || !expectedDispatch || !liveDispatch || !expectedUrl || !liveUrl) {
        return {
          ok: false,
          reason: "click_target_identity_unavailable",
          expected_selector: expectedSelector,
          live_selector: liveSelector,
        };
      }
      // Same semantics as clickTargetIdentityVerdict: a control keeps its identity through a
      // re-render (tag + type + role + visible text); the exact selector only decides for strict
      // authority (final submit / guarded intermediate) or controls with no visible text. A type
      // equal to the tag is the observer's "no type attribute" fallback.
      const normalizeType = (type, tag) => (type === tag ? "" : type);
      const strictIdentity = Boolean(allowFinalSubmit || allowGuardedIntermediate) || !text(expected.target_text);
      const fields = [
        ...(strictIdentity ? [["target_selector", expectedSelector, liveSelector]] : []),
        ["target_text", text(expected.target_text), text(live && live.target_text)],
        ["target_tag", expectedTag.toLowerCase(), liveTag.toLowerCase()],
        [
          "target_type",
          normalizeType(text(expected.target_type).toLowerCase(), expectedTag.toLowerCase()),
          normalizeType(text(live && live.target_type).toLowerCase(), liveTag.toLowerCase()),
        ],
        ["target_role", text(expected.target_role), text(live && live.target_role)],
        ["dispatch_path", expectedDispatch, liveDispatch],
        ["page_url", expectedUrl, liveUrl],
      ];
      // Same as clickTargetIdentityVerdict for the exact same element: any observed name, or the same
      // leading label whose live widget tail changed (run 1792's Lever location label carried the
      // typeahead's "No location found ... Loading" status).
      const squash = (value) => String(value || "").replace(/\s+/g, " ").trim();
      const sameLeading = (left, right) => {
        const a = squash(left);
        const b = squash(right);
        if (a && a.replace(/\s+/g, "") === b.replace(/\s+/g, "")) return true;
        const [shorter, longer] = a.length <= b.length ? [a, b] : [b, a];
        return shorter.length >= 4 && longer.startsWith(shorter) && (longer.length === shorter.length || longer[shorter.length] === " ");
      };
      const expectedNames = (Array.isArray(expected.target_names) ? expected.target_names : []).map(squash).filter(Boolean);
      const liveText = squash(live && live.target_text);
      // An observed name equal to the live name is the same control even when a re-render gave it a
      // new selector (clickTargetIdentityVerdict's rule: a selector-rebound control matches by name).
      // The leading-label tail (a live widget status after the label) is accepted only for the same selector.
      const sameElementName = !strictIdentity && Boolean(liveText)
        && (
          expectedNames.includes(liveText)
          || (
            Boolean(expectedSelector) && expectedSelector === liveSelector
            && [expected.target_text, ...expectedNames].some((name) => sameLeading(name, liveText))
          )
        );
      const mismatch = fields.find(([field, expectedValue, liveValue]) => (
        expectedValue !== liveValue && !(field === "target_text" && sameElementName)
      ));
      if (mismatch) {
        return {
          ok: false,
          reason: "click_target_drift",
          mismatch_field: mismatch[0],
          expected_value: mismatch[1],
          live_value: mismatch[2],
          expected_selector: expectedSelector,
          live_selector: liveSelector,
        };
      }
      return {
        ok: true,
        reason: "",
        expected_selector: expectedSelector,
        live_selector: liveSelector,
      };
    };
    const point = pointInfo();
    if (!point.ok || point.blocked_submit || point.blocked_file_dialog) return point;
    const identityVerdict = targetIdentityVerdict(expectedTarget, point);
    if (!identityVerdict.ok) {
      return {
        ...point,
        ok: false,
        action: "click",
        mode: "click_target_revalidation",
        failure_reason: identityVerdict.reason,
        target_identity_matched: false,
        target_identity_mismatch_field: identityVerdict.mismatch_field || "",
        preflight_target_selector: identityVerdict.expected_selector || "",
        live_target_selector: identityVerdict.live_selector || "",
        activation_attempted: false,
        activation_dispatched: false,
        activation_count: 0,
      };
    }
    return {
      ...point,
      ok: true,
      action: "click",
      mode: "click_preflight",
      activation_dispatched: false,
      activation_count: 0,
    };
  } catch (e) {
    return { ok: false, action: "click", mode: "dom_activation", error: String(e) };
  }
}

function clickOutcomeEvidenceInPage(probe) {
  try {
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const sensitiveValueControl = (node) => {
      try {
        const tag = (node && node.tagName || "").toLowerCase();
        const inputType = clean(node && node.getAttribute && node.getAttribute("type")).toLowerCase();
        if (tag === "input" && inputType === "password") return true;
        const autocompleteTokens = clean(node && node.getAttribute && node.getAttribute("autocomplete"))
          .toLowerCase()
          .split(/\s+/)
          .filter(Boolean);
        return autocompleteTokens.some((token) => (
          token === "current-password"
          || token === "new-password"
          || token === "one-time-code"
          || token === "transaction-amount"
          || token === "transaction-currency"
          || token === "webauthn"
          || token.startsWith("cc-")
        ));
      } catch (_) {
        return false;
      }
    };
    const visible = (el) => {
      if (!el) return false;
      try {
        const rect = (typeof globalThis.__lumenTopRect === "function" ? globalThis.__lumenTopRect(el) : el.getBoundingClientRect());
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
      } catch (_) {
        return false;
      }
    };
    const query = (selector) => {
      try {
        if (!selector) return null;
        if (typeof globalThis.__lumenDeepQueryAll === "function") {
          const matches = globalThis.__lumenDeepQueryAll(String(selector));
          return matches.length === 1 ? matches[0] : null;
        }
        return document.querySelector(String(selector));
      } catch (_) { return null; }
    };
    const target = query(probe && probe.target_selector);
    const menu = query(probe && probe.menu_selector);
    const owner = query(probe && probe.owner_selector);
    const active = typeof globalThis.__lumenDeepActiveElement === "function"
      ? globalThis.__lumenDeepActiveElement()
      : document.activeElement;
    const activeTag = active ? (active.tagName || "").toLowerCase() : "";
    const activeTypable = Boolean(active && (activeTag === "input" || activeTag === "textarea" || active.isContentEditable));
    const activeValue = activeTypable ? String(active.value || active.textContent || "") : "";
    const activeValueRedacted = activeTypable && sensitiveValueControl(active);
    // The focused typable element's identity as a later type step re-proves it (the same fields and the same text
    // rule as focusControlForTypeInPage), read now: a click that opens an inline editor leaves an element the
    // observation before the click never listed.
    const activeIdentity = (() => {
      if (!activeTypable || activeValueRedacted) return null;
      try {
        const stable = typeof globalThis.__lumenStableDeepSelector === "function" ? globalThis.__lumenStableDeepSelector : null;
        const selector = stable ? String(stable(active) || "") : "";
        if (!selector) return null;
        const labelText = (() => {
          try {
            if (!["input", "select", "textarea"].includes(activeTag)) return "";
            const labelShownText = (ref) => (
              (typeof globalThis.__lumenComposedText === "function" ? globalThis.__lumenComposedText(ref) : "")
              || ref.innerText || ref.textContent || ""
            );
            const root = active.getRootNode && active.getRootNode();
            const byId = (id) => (root && root.getElementById ? root.getElementById(id) : null) || document.getElementById(id);
            const labelledBy = clean(active.getAttribute("aria-labelledby"));
            if (labelledBy) {
              const text = clean(labelledBy.split(/\s+/).map((id) => { const ref = byId(id); return ref ? labelShownText(ref) : ""; }).join(" "));
              if (text) return text;
            }
            const id = active.getAttribute("id");
            if (id) {
              const escaped = (window.CSS && typeof CSS.escape === "function") ? CSS.escape(id) : String(id).split('"').join('\\"');
              const explicit = (root && root.querySelector ? root.querySelector(`label[for="${escaped}"]`) : null)
                || document.querySelector(`label[for="${escaped}"]`);
              const text = explicit ? clean(labelShownText(explicit)) : "";
              if (text) return text;
            }
            const wrapper = active.closest && active.closest("label");
            return wrapper ? clean(labelShownText(wrapper)) : "";
          } catch (_) { return ""; }
        })();
        const text = clean(
          active.innerText || active.textContent || labelText
          || active.getAttribute("aria-label") || active.getAttribute("placeholder") || active.getAttribute("name") || "",
        ).slice(0, 180).trim();
        return {
          target_selector: selector,
          target_text: text,
          target_tag: activeTag,
          target_type: clean(active.getAttribute("type")).toLowerCase(),
          target_role: clean(active.getAttribute("role")).toLowerCase(),
          dispatch_path: ((n) => { const a = n.closest && n.closest('button,a[href],input,textarea,select,label,[role="button"],[role="option"],[role="radio"],[role="checkbox"],[role="combobox"],[tabindex]'); const s = n.closest && n.closest("canvas,svg"); return s && (!a || s === a) ? "cdp_trusted_click" : "dom_activation"; })(active),
          page_url: String(location.href || ""),
        };
      } catch (_) { return null; }
    })();
    const rawControlValue = (node) => clean(
      node
        ? (node.value || node.getAttribute("aria-valuetext") || node.innerText || node.textContent || "")
        : "",
    ).slice(0, 500);
    const ownerValueAfter = rawControlValue(owner);
    const ownerValueRedacted = Boolean(
      (probe && probe.owner_value_redacted)
      || (owner && sensitiveValueControl(owner)),
    );
    const targetText = clean(probe && probe.target_text);
    const ownerMatchesTargetAfter = Boolean(targetText && ownerValueAfter && ownerValueAfter === targetText);
    const targetSelectedDirect = Boolean(target && (target.selected || target.checked || target.getAttribute("aria-selected") === "true" || target.getAttribute("aria-checked") === "true"));
    const targetSelected = Boolean(targetSelectedDirect || (!Boolean(probe && probe.owner_typable) && ownerMatchesTargetAfter));
    const menuOpen = visible(menu);
    let activeValidityValid = null;
    try {
      if (activeTypable && active.validity && typeof active.validity.valid === "boolean") activeValidityValid = Boolean(active.validity.valid);
    } catch (_) {}
    const activeAriaInvalid = activeTypable ? clean(active.getAttribute("aria-invalid")).toLowerCase() : "";
    const ownerReadbackChanged = ownerValueRedacted
      ? Boolean(
        (ownerMatchesTargetAfter && !(probe && probe.owner_matches_target_before))
        || (Boolean(ownerValueAfter) !== Boolean(probe && probe.owner_has_value_before))
      )
      : Boolean(ownerValueAfter && ownerValueAfter !== clean(probe && probe.owner_value_before));
    const selectedStateChanged = Boolean(
      (targetSelectedDirect && !(probe && probe.target_selected_before))
      || (ownerMatchesTargetAfter && !(probe && probe.owner_matches_target_before))
    );
    const targetStateAfter = (() => {
      try {
        if (!target) return "";
        const g = (n) => clean(target.getAttribute && target.getAttribute(n));
        return ["aria-pressed", "aria-expanded", "aria-current", "aria-checked", "aria-selected", "data-state", "data-selected", "data-checked", "data-active"]
          .map((n) => n + "=" + g(n)).join("|") + "|class=" + g("class").split(/\s+/).filter(Boolean).sort().join(" ");
      } catch (_) { return ""; }
    })();
    // The control's own state attributes changed under the click (a size or option button highlighting).
    const targetStateChanged = Boolean(probe && probe.target_state_before && targetStateAfter && probe.target_state_before !== targetStateAfter);
    // A tab or disclosure header (role=tab, aria-expanded) is activated, not chosen: its own
    // expanded state is what the click changes, and that state is the read-back. It never reads as
    // a committed choice, even though such widgets also flip aria-selected.
    const stateAttribute = (state, name) => {
      const found = String(state || "").match(new RegExp("(?:^|\\|)" + name + "=([^|]*)"));
      return found ? found[1] : "";
    };
    const expandedBefore = stateAttribute(probe && probe.target_state_before, "aria-expanded");
    const expandedAfter = stateAttribute(targetStateAfter, "aria-expanded");
    const targetRoleAfter = clean(target && target.getAttribute && target.getAttribute("role")).toLowerCase();
    const disclosureTarget = Boolean(
      target
      && probe
      && !probe.target_option_like
      && targetRoleAfter !== "combobox"
      && (targetRoleAfter === "tab" || expandedBefore || expandedAfter),
    );
    const expansionChanged = Boolean(disclosureTarget && expandedBefore !== expandedAfter && (expandedBefore || expandedAfter));
    const menuClosed = Boolean(probe && probe.menu_was_open && !menuOpen);
    const validityImproved = Boolean(
      probe
      && probe.target_option_like
      && (
        (probe.active_aria_invalid_before === "true" && activeAriaInvalid !== "true")
        || (probe.active_validity_valid_before === false && activeValidityValid === true)
      )
    );
    const targetDetached = Boolean(probe && probe.target_selector && !target);
    // Values near the control that changed under the click. Compared by element identity against the
    // preflight snapshot (never by index), skipping anything that also changed before the click and
    // anything that is sensitive (structural or model-classified: sensitiveValueControl covers both).
    // Named linkage (aria-controls / aria-describedby / same form) outranks positional siblings.
    let linkedValueBefore = "";
    let linkedValueAfter = "";
    let linkedBasis = "";
    try {
      const stash = globalThis.__lumenLinkedProbe;
      if (stash && target && stash.node === target && Array.isArray(stash.items) && Date.now() - Number(stash.at || 0) < 20000) {
        const read = (n) => clean(
          n.value !== undefined && n.value !== "" && typeof n.value === "string"
            ? n.value
            : (n.getAttribute("aria-valuetext") || n.innerText || n.textContent || ""),
        ).slice(0, 80);
        for (const tier of ["linked", "sibling"]) {
          for (const item of stash.items) {
            if (item.tier !== tier || item.spontaneous || !item.el || !item.el.isConnected) continue;
            if (sensitiveValueControl(item.el) || Array.from(item.el.querySelectorAll("input,textarea,select")).some(sensitiveValueControl)) continue;
            const after = read(item.el);
            if (after !== item.before) {
              linkedValueBefore = item.before;
              linkedValueAfter = after;
              linkedBasis = tier;
              break;
            }
          }
          if (linkedBasis) break;
        }
      }
    } catch (_) {}
    const linkedValueChanged = Boolean(linkedValueBefore || linkedValueAfter);
    const selectionCommitted = Boolean(
      probe
      && !disclosureTarget
      && (
        // A browser-owned selected/checked state transition is authoritative on its own. Do not
        // require a deterministic role/tag classifier to first decide that the raw control is an
        // "option"; native radios, custom choices, and future controls expose the same state proof.
        selectedStateChanged
        || (
          probe.target_option_like
          &&
          ownerMatchesTargetAfter
          && (menuClosed || validityImproved || targetDetached)
          && (Boolean(probe.owner_typable) || ownerReadbackChanged)
        )
      )
    );
    // Focus and text in an arbitrary active input are useful execution evidence, but they are not
    // choice read-back. Composite controls often place a populated text/tel input beside a popup;
    // borrowing that sibling value can falsely verify an unrelated option click. A committed choice
    // must be proved by the target's own selected state or its explicit accessibility-linked owner.
    const selectedValue = ownerValueRedacted && selectionCommitted
      ? "[REDACTED]"
      : selectedStateChanged && !disclosureTarget
        ? clean(ownerMatchesTargetAfter ? ownerValueAfter : targetText).slice(0, 180).trim()
        : selectionCommitted
          ? clean(ownerValueAfter || targetText).slice(0, 180).trim()
          : "";
    return {
      active_tag: activeTag,
      active_typable: activeTypable,
      active_identity: activeIdentity,
      active_has_value: Boolean(clean(activeValue)),
      active_value: activeValueRedacted ? "[REDACTED]" : activeValue.slice(0, 500),
      active_value_redacted: activeValueRedacted,
      active_value_redaction_reason: activeValueRedacted ? "sensitive_input_structure" : "",
      active_aria_invalid: activeAriaInvalid,
      active_validity_valid: activeValidityValid,
      target_present_after: Boolean(target),
      target_selected_after: targetSelected,
      target_state_changed: targetStateChanged,
      linked_value_changed: linkedValueChanged,
      linked_value_basis: linkedValueChanged ? linkedBasis : "",
      linked_value_before: linkedValueChanged ? linkedValueBefore : "",
      linked_value_after: linkedValueChanged ? linkedValueAfter : "",
      option_menu_open_after: menuOpen,
      option_menu_closed: menuClosed,
      owner_selector: String((probe && probe.owner_selector) || ""),
      owner_tag: String((probe && probe.owner_tag) || ""),
      owner_value_before: ownerValueRedacted ? "[REDACTED]" : String((probe && probe.owner_value_before) || "").slice(0, 500),
      owner_value_after: ownerValueRedacted ? "[REDACTED]" : ownerValueAfter,
      owner_value_redacted: ownerValueRedacted,
      owner_value_redaction_reason: ownerValueRedacted ? "sensitive_input_structure" : "",
      owner_has_value_after: Boolean(ownerValueAfter),
      owner_matches_target_before: Boolean(probe && probe.owner_matches_target_before),
      owner_matches_target_after: ownerMatchesTargetAfter,
      owner_readback_changed: ownerReadbackChanged,
      selection_committed: selectionCommitted,
      selected_value: selectedValue,
      selected_value_redacted: Boolean(ownerValueRedacted && selectionCommitted),
      target_disclosure: disclosureTarget,
      target_expanded_before: disclosureTarget ? expandedBefore : "",
      target_expanded_after: disclosureTarget ? expandedAfter : "",
      expansion_changed: expansionChanged,
      selection_evidence: disclosureTarget
        ? ""
        : selectedStateChanged
        ? "selected_state_changed"
        : menuClosed
          ? "menu_closed"
          : validityImproved
            ? "validity_improved"
            : targetDetached
              ? "option_detached"
              : "",
    };
  } catch (e) {
    return { selection_committed: false, error: String(e) };
  }
}

function scrollEvidenceInPage() {
  try { return { ok: true, action: "scroll", scroll_y: Math.round(window.scrollY), scroll_x: Math.round(window.scrollX), scroll_height: Math.round(document.documentElement.scrollHeight), scroll_width: Math.round(document.documentElement.scrollWidth), inner_w: window.innerWidth, inner_h: window.innerHeight }; }
  catch (e) { return { ok: false, error: String(e) }; }
}

function scrollByInPage(dy, dx = 0) {
  let mutationStarted = false;
  try {
    const before = Math.round(window.scrollY);
    const beforeX = Math.round(window.scrollX);
    const scrolling = document.scrollingElement || document.documentElement || document.body;
    mutationStarted = true;
    if (scrolling && typeof scrolling.scrollBy === "function") {
      scrolling.scrollBy({ top: Number(dy || 0), left: Number(dx || 0), behavior: "auto" });
    } else {
      window.scrollBy(Number(dx || 0), Number(dy || 0));
    }
    const after = Math.round(window.scrollY);
    const afterX = Math.round(window.scrollX);
    return {
      ok: true,
      action: "scroll",
      mode: "document_scroll",
      scroll_target_kind: "page",
      scroll_attempted: true,
      browser_mutation_attempted: true,
      execution_outcome_unknown: false,
      before_scroll_y: before,
      scroll_y: after,
      before_scroll_x: beforeX,
      scroll_x: afterX,
      scroll_axis: Number(dx || 0) && !Number(dy || 0) ? "horizontal" : "vertical",
      before_scroll_position: Number(dx || 0) && !Number(dy || 0) ? beforeX : before,
      scroll_position: Number(dx || 0) && !Number(dy || 0) ? afterX : after,
      scroll_changed: before !== after || beforeX !== afterX,
      scroll_height: Math.round(document.documentElement.scrollHeight),
      scroll_width: Math.round(document.documentElement.scrollWidth),
      inner_w: window.innerWidth,
      inner_h: window.innerHeight,
    };
  } catch (e) {
    return {
      ok: false,
      action: "scroll",
      mode: "document_scroll",
      failure_reason: mutationStarted
        ? "scroll_execution_outcome_unknown"
        : "scroll_execution_failed",
      scroll_attempted: mutationStarted,
      browser_mutation_attempted: mutationStarted,
      execution_outcome_unknown: mutationStarted,
    };
  }
}

function scrollTargetInPage(selector, dy, expectedTarget, expectedPageContext, dx = 0) {
  // Self-contained (this runs in the page): the same leading label at a word boundary, or the same
  // characters with different spacing ("First Name *" vs "First Name*").
  const sameLeadingIdentityTextInPage = (left, right) => {
    const a = String(left || "").replace(/\s+/g, " ").trim();
    const b = String(right || "").replace(/\s+/g, " ").trim();
    if (a && a.replace(/\s+/g, "") === b.replace(/\s+/g, "")) return true;
    const [shorter, longer] = a.length <= b.length ? [a, b] : [b, a];
    return shorter.length >= 4 && longer.startsWith(shorter) && (longer.length === shorter.length || longer[shorter.length] === " ");
  };
  let mutationStarted = false;
  try {
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const exactSelector = String(selector || "").trim();
    const queryAll = typeof globalThis.__lumenDeepQueryAll === "function"
      ? globalThis.__lumenDeepQueryAll
      : (value) => Array.from(document.querySelectorAll(value));
    const composedParent = typeof globalThis.__lumenComposedParent === "function"
      ? globalThis.__lumenComposedParent
      : (node) => node && node.parentElement;
    const deepDescendants = typeof globalThis.__lumenDeepDescendants === "function"
      ? globalThis.__lumenDeepDescendants
      : (node, descendantSelector) => Array.from(
        (node && node.querySelectorAll && node.querySelectorAll(descendantSelector)) || [],
      );
    const stableSelector = typeof globalThis.__lumenStableDeepSelector === "function"
      ? globalThis.__lumenStableDeepSelector
      : () => "";
    const liveDocumentStateSignature = typeof globalThis.__lumenDocumentStateSignature === "function"
      ? globalThis.__lumenDocumentStateSignature
      : () => "";
    const expectedPage = expectedPageContext && typeof expectedPageContext === "object"
      ? expectedPageContext
      : {};
    const expectedUrl = String(expectedPage.url || "").trim();
    const expectedLifecycleToken = String(expectedPage.document_lifecycle_token || "").trim();
    const expectedDocumentState = String(expectedPage.document_state_signature || "").trim();
    const initialDocument = document;
    const lifecycleToken = () => {
      try {
        return clean(globalThis.__lumenDocumentLifecycleToken);
      } catch (_) {
        return "";
      }
    };
    const pageContextVerdict = () => {
      if (!expectedUrl || !expectedLifecycleToken || !expectedDocumentState) {
        return { ok: false, reason: "page_context_required", mismatch_field: "" };
      }
      if (document !== initialDocument || String(location.href || "") !== expectedUrl) {
        return { ok: false, reason: "page_context_drift", mismatch_field: "url" };
      }
      const liveLifecycleToken = lifecycleToken();
      if (!liveLifecycleToken || liveLifecycleToken !== expectedLifecycleToken) {
        return {
          ok: false,
          reason: "page_context_drift",
          mismatch_field: "document_lifecycle_token",
        };
      }
      const liveDocumentState = String(liveDocumentStateSignature() || "").trim();
      if (!liveDocumentState || liveDocumentState !== expectedDocumentState) {
        return {
          ok: false,
          reason: "page_context_drift",
          mismatch_field: "document_state_signature",
        };
      }
      return { ok: true, reason: "", mismatch_field: "" };
    };
    const initialPageVerdict = pageContextVerdict();
    if (!initialPageVerdict.ok) {
      return {
        ok: false,
        action: "scroll",
        mode: "element_scroll",
        failure_reason: initialPageVerdict.reason,
        page_context_mismatch_field: initialPageVerdict.mismatch_field,
        scroll_attempted: false,
        target_identity_matched: false,
      };
    }
    const sensitiveValueControl = (node) => {
      try {
        const tag = (node && node.tagName || "").toLowerCase();
        const inputType = clean(node && node.getAttribute && node.getAttribute("type")).toLowerCase();
        if (tag === "input" && inputType === "password") return true;
        const tokens = clean(node && node.getAttribute && node.getAttribute("autocomplete"))
          .toLowerCase()
          .split(/\s+/)
          .filter(Boolean);
        return tokens.some((token) => (
          token === "current-password"
          || token === "new-password"
          || token === "one-time-code"
          || token === "transaction-amount"
          || token === "transaction-currency"
          || token === "webauthn"
          || token.startsWith("cc-")
        ));
      } catch (_) {
        return false;
      }
    };
    let matches = [];
    try {
      matches = exactSelector ? Array.from(queryAll(exactSelector)) : [];
    } catch (_) {
      matches = [];
    }
    if (matches.length !== 1) {
      return {
        ok: false,
        action: "scroll",
        mode: "element_scroll",
        failure_reason: matches.length > 1 ? "control_selector_ambiguous" : "control_not_found",
        scroll_attempted: false,
        target_identity_matched: false,
      };
    }
    const el = matches[0];
    const liveIdentityFor = (node) => ({
      target_selector: exactSelector,
      target_text: clean(
        sensitiveValueControl(node)
          ? (
            node.getAttribute && (
              node.getAttribute("aria-label")
              || node.getAttribute("placeholder")
              || node.getAttribute("name")
            )
            || ""
          )
          : (
            node.innerText
            || node.textContent
            || node.getAttribute && node.getAttribute("aria-label")
            || node.getAttribute && node.getAttribute("placeholder")
            || node.getAttribute && node.getAttribute("name")
            || ""
          ),
      ).slice(0, 180).trim(),
      target_tag: (node.tagName || "").toLowerCase(),
      target_type: clean(node.getAttribute && node.getAttribute("type")).toLowerCase(),
      target_role: clean(node.getAttribute && node.getAttribute("role")).toLowerCase(),
      dispatch_path: ((n) => { const a = n.closest && n.closest('button,a[href],input,textarea,select,label,[role="button"],[role="option"],[role="radio"],[role="checkbox"],[role="combobox"],[tabindex]'); const s = n.closest && n.closest("canvas,svg"); return s && (!a || s === a) ? "cdp_trusted_click" : "dom_activation"; })(node),
      page_url: String(location.href || ""),
    });
    const expected = expectedTarget && typeof expectedTarget === "object" ? expectedTarget : {};
    const expectedIdentity = {
      target_selector: String(expected.target_selector || "").trim(),
      target_text: clean(expected.target_text),
      target_tag: clean(expected.target_tag || expected.tag).toLowerCase(),
      target_type: clean(expected.target_type).toLowerCase(),
      target_role: clean(expected.target_role).toLowerCase(),
      dispatch_path: String(expected.dispatch_path || "").trim(),
      page_url: String(expected.page_url || "").trim(),
    };
    const targetIdentityVerdict = () => {
      const liveIdentity = liveIdentityFor(el);
      const expectedNames = (Array.isArray(expected.target_names) ? expected.target_names : [])
        .map((name) => clean(name))
        .filter(Boolean);
      for (const field of ["target_selector", "target_text", "target_tag", "target_type", "target_role", "dispatch_path", "page_url"]) {
        const matches = expectedIdentity[field] === liveIdentity[field]
          || (field === "target_text" && Boolean(liveIdentity.target_text) && expectedNames.includes(liveIdentity.target_text))
          // Same element (the selector is compared exactly): the same label with a changed live tail or
          // different spacing, as for typing and clicks.
          || (field === "target_text" && Boolean(liveIdentity.target_text)
            && [expectedIdentity.target_text, ...expectedNames].some((name) => sameLeadingIdentityTextInPage(name, liveIdentity.target_text)));
        if (!matches) {
          return { ok: false, mismatch_field: field };
        }
      }
      return { ok: true, mismatch_field: "" };
    };
    const initialTargetVerdict = targetIdentityVerdict();
    if (!initialTargetVerdict.ok) {
      return {
        ok: false,
        action: "scroll",
        mode: "element_scroll",
        failure_reason: "scroll_target_drift",
        target_identity_mismatch_field: initialTargetVerdict.mismatch_field,
        scroll_attempted: false,
        target_identity_matched: false,
      };
    }
    // The requested axis picks which overflow counts: a sideways request moves the nearest
    // horizontally scrollable ancestor, exactly as shift+wheel would.
    const horizontal = Number(dx || 0) !== 0 && Number(dy || 0) === 0;
    const isScrollable = (node) => {
      if (!node || node === document.body || node === document.documentElement) return false;
      const style = getComputedStyle(node);
      if (horizontal) {
        const overflowX = String(style.overflowX || style.overflow || "").toLowerCase();
        return /^(auto|scroll|overlay)$/.test(overflowX)
          && Number(node.scrollWidth || 0) > Number(node.clientWidth || 0) + 1;
      }
      const overflowY = String(style.overflowY || style.overflow || "").toLowerCase();
      return /^(auto|scroll|overlay)$/.test(overflowY)
        && Number(node.scrollHeight || 0) > Number(node.clientHeight || 0) + 1;
    };
    let container = el;
    while (container && !isScrollable(container)) container = composedParent(container);
    if (!container) {
      // A popup or virtualized region can be a composed-DOM sibling rather than an ancestor of
      // the control the model selected. Follow only explicit raw accessibility ownership/control
      // edges from that exact bound control. Requiring one unique scrollable related node keeps
      // this execution binding structural, fail-closed, and independent of menu/field semantics.
      const attributeSelectorValue = (value) => String(value || "")
        .replace(/\\/g, "\\\\")
        .replace(/"/g, '\\"');
      const relationTokens = Array.from(new Set(
        ["aria-controls", "aria-owns"]
          .flatMap((name) => clean(el.getAttribute && el.getAttribute(name)).split(/\s+/))
          .filter(Boolean),
      )).slice(0, 32);
      const relatedRoots = [];
      let relatedRootAmbiguous = false;
      for (const token of relationTokens) {
        let relatedMatches = [];
        try {
          relatedMatches = Array.from(queryAll(`[id="${attributeSelectorValue(token)}"]`));
        } catch (_) {
          relatedMatches = [];
        }
        if (relatedMatches.length > 1) {
          relatedRootAmbiguous = true;
          continue;
        }
        if (relatedMatches.length === 1 && !relatedRoots.includes(relatedMatches[0])) {
          relatedRoots.push(relatedMatches[0]);
        }
      }
      const relatedScrollable = [];
      if (!relatedRootAmbiguous) {
        for (const root of relatedRoots) {
          for (const candidate of [root, ...deepDescendants(root, "*")]) {
            if (
              candidate
              && candidate.isConnected
              && isScrollable(candidate)
              && !relatedScrollable.includes(candidate)
            ) {
              relatedScrollable.push(candidate);
            }
          }
        }
      }
      if (relatedScrollable.length === 1) container = relatedScrollable[0];
    }
    if (!container) {
      return {
        ok: false,
        action: "scroll",
        mode: "element_scroll",
        failure_reason: "scroll_container_unavailable",
        scroll_attempted: false,
        target_identity_matched: true,
      };
    }
    const positionKey = horizontal ? "scrollLeft" : "scrollTop";
    const before = Math.round(Number(container[positionKey] || 0));
    const maxScroll = horizontal
      ? Math.max(0, Number(container.scrollWidth || 0) - Number(container.clientWidth || 0))
      : Math.max(0, Number(container.scrollHeight || 0) - Number(container.clientHeight || 0));
    const requested = Number((horizontal ? dx : dy) || 0);
    const next = Math.max(0, Math.min(maxScroll, before + requested));
    const opaqueHash = (value) => {
      const raw = String(value || "");
      const seeds = [0x811c9dc5, 0x9e3779b9, 0x85ebca6b, 0xc2b2ae35];
      return seeds.map((seed) => {
        let hash = seed >>> 0;
        for (let index = 0; index < raw.length; index += 1) {
          hash ^= raw.charCodeAt(index);
          hash = Math.imul(hash, 16777619);
        }
        return (hash >>> 0).toString(16).padStart(8, "0");
      }).join("");
    };
    const containerStableSelector = String(stableSelector(container) || "").trim();
    const containerIdentityHash = containerStableSelector
      ? opaqueHash(JSON.stringify({
        version: 1,
        document_lifecycle_token: expectedLifecycleToken,
        container_selector: containerStableSelector,
      }))
      : "";
    if (
      !el.isConnected
      || !container.isConnected
      || el.ownerDocument !== document
      || container.ownerDocument !== document
    ) {
      return {
        ok: false,
        action: "scroll",
        mode: "element_scroll",
        failure_reason: "scroll_target_drift",
        target_identity_mismatch_field: "target_document",
        scroll_attempted: false,
        target_identity_matched: false,
      };
    }
    const immediateTargetVerdict = targetIdentityVerdict();
    if (!immediateTargetVerdict.ok) {
      return {
        ok: false,
        action: "scroll",
        mode: "element_scroll",
        failure_reason: "scroll_target_drift",
        target_identity_mismatch_field: immediateTargetVerdict.mismatch_field,
        scroll_attempted: false,
        target_identity_matched: false,
      };
    }
    // This is the mutation gate. Re-read exact URL, document lifecycle, and the raw structural
    // state in this same injected task after target/container resolution and immediately before
    // assigning scrollTop. A separate service-worker preflight is not mutation authority.
    const immediatePageVerdict = pageContextVerdict();
    if (!immediatePageVerdict.ok) {
      return {
        ok: false,
        action: "scroll",
        mode: "element_scroll",
        failure_reason: immediatePageVerdict.reason,
        page_context_mismatch_field: immediatePageVerdict.mismatch_field,
        scroll_attempted: false,
        target_identity_matched: true,
      };
    }
    let after;
    try {
      mutationStarted = true;
      container[positionKey] = next;
      after = Math.round(Number(container[positionKey] || 0));
    } catch (_) {
      // The setter may have run before a browser/page exception surfaced. Never downgrade that
      // post-dispatch ambiguity to a definite non-attempt, because doing so could authorize a
      // duplicate scroll against the same observation.
      return {
        ok: false,
        action: "scroll",
        mode: "element_scroll",
        failure_reason: "scroll_execution_outcome_unknown",
        scroll_attempted: true,
        browser_mutation_attempted: true,
        execution_outcome_unknown: true,
        target_identity_matched: true,
        page_fallback: false,
      };
    }
    return {
      ok: true,
      action: "scroll",
      mode: "element_scroll",
      scroll_target_kind: "container",
      scroll_container_identity_hash: containerIdentityHash,
      scroll_container_scroll_height: Math.round(Number(container.scrollHeight || 0)),
      scroll_container_client_height: Math.round(Number(container.clientHeight || 0)),
      scroll_container_scroll_width: Math.round(Number(container.scrollWidth || 0)),
      scroll_container_client_width: Math.round(Number(container.clientWidth || 0)),
      scroll_axis: horizontal ? "horizontal" : "vertical",
      before_scroll_position: before,
      scroll_position: after,
      before_scroll_y: Math.round(window.scrollY),
      scroll_y: Math.round(window.scrollY),
      scroll_changed: before !== after,
      scroll_attempted: true,
      browser_mutation_attempted: true,
      execution_outcome_unknown: false,
      target_identity_matched: true,
      page_fallback: false,
    };
  } catch (_) {
    return {
      ok: false,
      action: "scroll",
      mode: "element_scroll",
      failure_reason: mutationStarted
        ? "scroll_execution_outcome_unknown"
        : "scroll_execution_failed",
      scroll_attempted: mutationStarted,
      browser_mutation_attempted: mutationStarted,
      execution_outcome_unknown: mutationStarted,
    };
  }
}

// Visible text of this document (innerText skips hidden nodes), for a model-authored wait condition:
// how many times that text is shown (whitespace collapsed, any letter case: CSS may show "READY" for
// a DOM "Ready", and the model reads either).
function pageTextContainsInPage(text) {
  try {
    const norm = (value) => String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
    const wanted = norm(text);
    const body = document.body;
    const haystack = norm(body && body.innerText);
    let count = 0;
    if (wanted) {
      for (let at = haystack.indexOf(wanted); at >= 0; at = haystack.indexOf(wanted, at + wanted.length)) count += 1;
    }
    return { ok: true, found: count > 0, count };
  } catch (error) {
    return { ok: false, found: false, count: 0, error: String(error) };
  }
}

// A wait the model authors: a plain pause, or "until this exact text is visible" (every frame),
// so a time-critical follow-up can fire the moment the page shows it is ready. The body only
// reports whether the text appeared; the model chose what to wait for.
function watchElementTextInPage(selector, point, durationMs, sensitiveSelectors = []) {
  // Eyes over time: read one element's rendered text continuously and report each distinct value
  // with its time. The element is re-found by selector if the page replaces it while watched.
  // A watch reads values as text, so it never targets a sensitive control: a password or other
  // structurally sensitive input, a model-marked control (sensitiveSelectors, the tab's bindings),
  // an element inside one, or one holding one. Such a target is refused and nothing is read.
  return new Promise((resolve) => {
    try {
      const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
      const queryAll = typeof globalThis.__lumenDeepQueryAll === "function"
        ? globalThis.__lumenDeepQueryAll
        : (value) => Array.from(document.querySelectorAll(value));
      const SENSITIVE_AUTOCOMPLETE_TOKENS = [
        "current-password", "new-password", "one-time-code", "transaction-amount", "transaction-currency", "webauthn",
      ];
      const structurallySensitive = (node) => {
        try {
          const tag = String(node.tagName || "").toLowerCase();
          const inputType = clean(node.getAttribute && node.getAttribute("type")).toLowerCase();
          if (tag === "input" && inputType === "password") return true;
          return clean(node.getAttribute && node.getAttribute("autocomplete"))
            .toLowerCase()
            .split(/\s+/)
            .filter(Boolean)
            .some((token) => SENSITIVE_AUTOCOMPLETE_TOKENS.includes(token) || token.startsWith("cc-"));
        } catch (_) {
          return true;
        }
      };
      const marked = [];
      let markedUnreadable = false;
      for (const raw of Array.isArray(sensitiveSelectors) ? sensitiveSelectors : []) {
        const markedSelector = String(raw || "").trim();
        if (!markedSelector) continue;
        try {
          marked.push(...Array.from(queryAll(markedSelector)));
        } catch (_) {
          markedUnreadable = true;
        }
      }
      const composedContains = typeof globalThis.__lumenComposedContains === "function"
        ? globalThis.__lumenComposedContains
        : (ancestor, node) => {
          try {
            return Boolean(ancestor && node && (ancestor === node || ancestor.contains(node)));
          } catch (_) {
            return false;
          }
        };
      const sensitiveTarget = (node) => {
        if (markedUnreadable || structurallySensitive(node)) return true;
        const sensitiveControls = [
          ...marked,
          ...Array.from(queryAll("input,textarea,[contenteditable],[autocomplete]")).filter(structurallySensitive),
        ];
        return sensitiveControls.some((control) => composedContains(node, control) || composedContains(control, node));
      };
      const refuse = () => resolve({ ok: false, action: "watch", failure_reason: "watch_target_sensitive" });
      const find = () => {
        if (selector) {
          const matches = Array.from(queryAll(String(selector)));
          if (matches.length === 1) return matches[0];
        }
        if (Array.isArray(point) && point.length === 2) {
          const dpr = window.devicePixelRatio || 1;
          const x = Number(point[0] || 0) / dpr;
          const y = Number(point[1] || 0) / dpr;
          return typeof globalThis.__lumenDeepElementFromPoint === "function"
            ? globalThis.__lumenDeepElementFromPoint(x, y)
            : document.elementFromPoint(x, y);
        }
        return null;
      };
      let el = find();
      if (!el) {
        resolve({ ok: false, action: "watch", failure_reason: "watch_target_not_found" });
        return;
      }
      if (sensitiveTarget(el)) {
        refuse();
        return;
      }
      const budget = Math.max(200, Math.min(15000, Math.round(Number(durationMs) || 0)));
      const started = performance.now();
      const values = [];
      let last = null;
      let samples = 0;
      let refused = false;
      const read = () => {
        if (!el || !el.isConnected) {
          el = find();
          if (el && sensitiveTarget(el)) refused = true;
        } else if (structurallySensitive(el) || marked.includes(el)) {
          refused = true;
        }
        if (refused || !el) return "";
        return clean(("value" in el && typeof el.value === "string" && el.value) || el.innerText || el.textContent || "").slice(0, 160);
      };
      const done = () => resolve({
        ok: true,
        action: "watch",
        values,
        samples,
        watched_ms: Math.round(performance.now() - started),
        target_tag: el ? String(el.tagName || "").toLowerCase() : "",
      });
      const tick = () => {
        samples += 1;
        const text = read();
        if (refused) {
          // The target turned sensitive (or was replaced by a sensitive one): nothing read is kept.
          refuse();
          return;
        }
        if (text !== last) {
          values.push({ t_ms: Math.round(performance.now() - started), text });
          last = text;
        }
        if (values.length >= 200 || performance.now() - started >= budget) {
          done();
          return;
        }
        setTimeout(tick, 40);
      };
      tick();
    } catch (error) {
      resolve({ ok: false, action: "watch", failure_reason: "watch_failed", error: String(error) });
    }
  });
}

// The tab's model-marked sensitive controls (its own binding and any the backend requires) ride into
// the page so the watch can refuse them; a binding the tab requires but lost refuses the watch.
async function runVisionWatch(tabId, selector, point, durationMs, params = null) {
  if (!tabId) return { ok: false, action: "watch", failure_reason: "watch_failed", error: "No application tab." };
  const budget = Math.max(200, Math.min(15000, Math.round(Number(durationMs) || 0)));
  const commandParams = params && typeof params === "object" ? params : {};
  const maskContext = sensitiveScreenshotMaskContextForTab(tabId);
  const boundSelectors = Array.isArray(maskContext && maskContext.selectors) ? maskContext.selectors : [];
  if (maskContext && maskContext.required && !boundSelectors.length) {
    return { ok: false, action: "watch", failure_reason: "sensitive_mask_binding_lost" };
  }
  const requiredBinding = (
    commandParams.required_sensitive_mask_binding
    && typeof commandParams.required_sensitive_mask_binding === "object"
  )
    ? commandParams.required_sensitive_mask_binding
    : null;
  const requiredSelectors = requiredBinding
    ? normalizeSensitiveScreenshotSelectors(requiredBinding.selectors || requiredBinding.selector)
    : [];
  const sensitiveSelectors = Array.from(new Set([...boundSelectors, ...requiredSelectors].map((item) => String(item))));
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId },
        func: watchElementTextInPage,
        args: [String(selector || ""), point, budget, sensitiveSelectors],
      }),
      budget + 4000,
      "watch timed out",
    );
    return (rows && rows[0] && rows[0].result) || { ok: false, action: "watch", failure_reason: "watch_failed" };
  } catch (error) {
    return { ok: false, action: "watch", failure_reason: "watch_failed", error: errorMessage(error) };
  }
}

// Lines (trimmed, non-empty) the print rendering holds that the screen rendering does not, in print order.
function printOnlyLines(screenText, printText) {
  const onScreen = new Set(String(screenText || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean));
  const seen = new Set();
  const out = [];
  for (const raw of String(printText || "").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || onScreen.has(line) || seen.has(line)) continue;
    seen.add(line);
    out.push(line);
  }
  return out.join("\n");
}

const PRINT_VIEW_TEXT_CAP = 4000;
const PRINT_VIEW_READ_CHARS = 20000;

// The page as it renders for print: the main frame's text on screen, then under print media emulation
// (a CDP toggle on the run's debugger session, restored in a finally), and the lines only print shows.
// Both reads go through the redacted document-text path read_run_tab_text uses (sensitive control values masked,
// the run's sensitive-mask binding honoured). Read-only: no click, no window.print() (so a page's beforeprint
// handlers do not run), no page change beyond the emulated media.
// The sensitive-mask selectors a whole-document read redacts with: the run tab's bound selectors plus the request's
// required binding. A lost, malformed or selector-less binding fails closed, before anything is read.
function documentReadMaskSelectors(tabId, commandParams) {
  const bindingProvided = Object.prototype.hasOwnProperty.call(commandParams, "required_sensitive_mask_binding");
  const requiredBinding = (
    commandParams.required_sensitive_mask_binding
    && typeof commandParams.required_sensitive_mask_binding === "object"
  )
    ? commandParams.required_sensitive_mask_binding
    : null;
  if (bindingProvided && !requiredBinding) return { failure_reason: "sensitive_mask_page_context_required" };
  const maskContext = sensitiveScreenshotMaskContextForTab(tabId);
  const boundSelectors = Array.isArray(maskContext && maskContext.selectors) ? maskContext.selectors : [];
  if (maskContext && maskContext.required && !boundSelectors.length) return { failure_reason: "sensitive_mask_binding_lost" };
  const requiredSelectors = requiredBinding
    ? normalizeSensitiveScreenshotSelectors(requiredBinding.selectors || requiredBinding.selector)
    : [];
  if (requiredBinding && !requiredSelectors.length) return { failure_reason: "sensitive_mask_selector_required" };
  return { selectors: Array.from(new Set([...boundSelectors, ...requiredSelectors].map((item) => String(item)))) };
}

async function runVisionReadPrintView(tabId, params = null) {
  const commandParams = params && typeof params === "object" ? params : {};
  const action = "read_print_view";
  if (!tabId) return { ok: false, action, failure_reason: "run_tab_unavailable" };
  if (nativeDialogOpenFor(tabId)) return { ok: false, action, failure_reason: "native_dialog_open" };
  const mask = documentReadMaskSelectors(tabId, commandParams);
  if (mask.failure_reason) return { ok: false, action, failure_reason: mask.failure_reason };
  const selectors = mask.selectors;
  const target = { tabId };
  const readText = async () => {
    const [row] = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId },
        func: _visionPageEvidence,
        args: [selectors, { document_text: true, max_chars: PRINT_VIEW_READ_CHARS, line_separator: "\n" }],
      }),
      4000,
      "print view text read timed out",
    );
    const result = (row && row.result) || {};
    if (result.error || typeof result.document_text !== "string") {
      throw new Error(result.error ? String(result.error).slice(0, 80) : "print view text unavailable");
    }
    return {
      text: result.document_text,
      url: String(result.url || ""),
      lifecycle: String(result.document_lifecycle_token || ""),
    };
  };
  let emulated = false;
  try {
    await acquireDebugger(target);
    const screen = await readText();
    emulated = true;
    await _cdp(target, "Emulation.setEmulatedMedia", { media: "print" });
    const print = await readText();
    if (print.lifecycle !== screen.lifecycle) throw new Error("document changed during the print view read");
    const printOnly = printOnlyLines(screen.text, print.text);
    return {
      ok: true,
      action,
      url: print.url || screen.url,
      document_lifecycle_token: print.lifecycle,
      print_text: print.text.slice(0, PRINT_VIEW_TEXT_CAP),
      print_text_truncated: print.text.length > PRINT_VIEW_TEXT_CAP,
      print_only_text: printOnly.slice(0, PRINT_VIEW_TEXT_CAP),
      print_only_text_truncated: printOnly.length > PRINT_VIEW_TEXT_CAP,
    };
  } catch (error) {
    return { ok: false, action, failure_reason: "print_view_unavailable", error: errorMessage(error).slice(0, 240) };
  } finally {
    if (emulated) {
      try { await _cdp(target, "Emulation.setEmulatedMedia", { media: "" }); } catch (_) {}
    }
    releaseDebugger(target);
  }
}

// The whole form of the run tab (read_form): every form control of its main document and same-origin frames, on
// screen or not, through the same redaction as the print view. Read-only: nothing is scrolled, focused or changed.
async function runVisionReadForm(tabId, params = null) {
  const commandParams = params && typeof params === "object" ? params : {};
  const action = "read_form";
  if (!tabId) return { ok: false, action, failure_reason: "run_tab_unavailable" };
  if (nativeDialogOpenFor(tabId)) return { ok: false, action, failure_reason: "native_dialog_open" };
  const mask = documentReadMaskSelectors(tabId, commandParams);
  if (mask.failure_reason) return { ok: false, action, failure_reason: mask.failure_reason };
  try {
    const [row] = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId },
        func: _visionPageEvidence,
        args: [mask.selectors, { form_outline: true, form_target_selector: String(commandParams.target_selector || "") }],
      }),
      6000,
      "form outline read timed out",
    );
    const result = (row && row.result) || {};
    if (result.error) {
      return {
        ok: false,
        action,
        failure_reason: result.error === "sensitive_selector_drift" ? "sensitive_selector_drift" : "form_outline_unavailable",
      };
    }
    return {
      ok: true,
      action,
      form_outline: Array.isArray(result.form_outline) ? result.form_outline : [],
      form_outline_truncated: result.form_outline_truncated === true,
      form_target: Number.isInteger(result.form_target) ? result.form_target : null,
    };
  } catch (error) {
    return { ok: false, action, failure_reason: "form_outline_unavailable", error: errorMessage(error).slice(0, 240) };
  }
}

async function runVisionWait(tabId, waitMs, text) {
  const started = Date.now();
  const budget = Math.max(100, Math.min(30000, Math.round(Number(waitMs) || 750)));
  const wanted = String(text || "").replace(/\s+/g, " ").trim().slice(0, 200);
  if (!wanted || !tabId) {
    await new Promise((resolve) => setTimeout(resolve, budget));
    return { ok: true, action: "wait", waited_ms: Date.now() - started, text_matched: false };
  }
  // The wait is for the text to appear: copies already shown when it starts (instructions, a label,
  // an old status) never satisfy it. It fires when the text's count in some frame rises above the
  // lowest count that frame showed so far, so a status that goes away and comes back counts too, and a
  // frame that misses one probe (reloading) is not mistaken for a disappearance.
  let presentAtStart = null;
  const lowestByFrame = new Map();
  while (Date.now() - started < budget) {
    try {
      const rows = await withTimeout(
        chrome.scripting.executeScript({ target: { tabId, allFrames: true }, func: pageTextContainsInPage, args: [wanted] }),
        1500,
        "wait text probe timed out",
      );
      const answered = Array.isArray(rows)
        ? rows.filter((row) => row && row.result && row.result.ok)
        : [];
      if (answered.length) {
        const countOf = (row) => Math.max(0, Number((row.result && row.result.count) || 0));
        if (presentAtStart === null) presentAtStart = answered.reduce((sum, row) => sum + countOf(row), 0);
        let appeared = false;
        for (const row of answered) {
          const frame = String(row.frameId === undefined ? "main" : row.frameId);
          const count = countOf(row);
          const lowest = lowestByFrame.has(frame) ? lowestByFrame.get(frame) : null;
          if (lowest !== null && count > lowest) appeared = true;
          lowestByFrame.set(frame, lowest === null ? count : Math.min(lowest, count));
        }
        if (appeared) {
          const matchedAt = Date.now();
          return {
            ok: true,
            action: "wait",
            waited_ms: matchedAt - started,
            text_matched: true,
            matched_at_ms: matchedAt,
            present_at_start: presentAtStart,
          };
        }
      }
    } catch (_) {}
    await new Promise((resolve) => setTimeout(resolve, 60));
  }
  return {
    ok: false,
    action: "wait",
    failure_reason: "wait_text_not_seen",
    waited_ms: Date.now() - started,
    text_matched: false,
    // Already shown when the wait began, and never newly shown: the condition was not an appearance.
    present_at_start: presentAtStart === null ? 0 : presentAtStart,
  };
}

function visionKeyInPage(key) {
  try {
    const el = (
      typeof globalThis.__lumenDeepActiveElement === "function"
        ? globalThis.__lumenDeepActiveElement()
        : document.activeElement
    ) || document.body;
    ["keydown", "keypress", "keyup"].forEach((t) => el.dispatchEvent(new KeyboardEvent(t, { key, bubbles: true })));
    return { ok: true, action: "key", key };
  } catch (e) { return { ok: false, error: String(e) }; }
}

function keyDispatchDescription(rawKey) {
  // One key or a chord (Ctrl+A, Shift+Tab, Ctrl+Shift+End). Modifiers become CDP bit flags on
  // the main key's events (Alt 1, Ctrl 2, Meta 4, Shift 8) - the chord a person's keyboard sends.
  // A printable key (and Enter) carries its text so it types; a chord with Ctrl/Alt/Meta does
  // not, leaving the page and the browser to interpret the shortcut (select all, copy, paste).
  const aliases = {
    esc: "Escape",
    escape: "Escape",
    return: "Enter",
    enter: "Enter",
    space: " ",
    spacebar: " ",
    up: "ArrowUp",
    down: "ArrowDown",
    left: "ArrowLeft",
    right: "ArrowRight",
    arrowup: "ArrowUp",
    arrowdown: "ArrowDown",
    arrowleft: "ArrowLeft",
    arrowright: "ArrowRight",
    del: "Delete",
    delete: "Delete",
    backspace: "Backspace",
    tab: "Tab",
    home: "Home",
    end: "End",
    pageup: "PageUp",
    pagedown: "PageDown",
    pgup: "PageUp",
    pgdn: "PageDown",
    ins: "Insert",
    insert: "Insert",
    plus: "+",
    minus: "-",
  };
  const modifierBits = {
    alt: 1,
    option: 1,
    ctrl: 2,
    control: 2,
    meta: 4,
    cmd: 4,
    command: 4,
    win: 4,
    windows: 4,
    super: 4,
    shift: 8,
  };
  const requested = String(rawKey || "").trim();
  let modifiers = 0;
  let main = requested;
  if (requested.length > 1 && requested.includes("+")) {
    const tokens = requested.split("+").map((token) => token.trim());
    // "Ctrl++" ends in the literal plus key.
    const last = tokens[tokens.length - 1] === "" ? "+" : tokens[tokens.length - 1];
    const heads = tokens.slice(0, tokens[tokens.length - 1] === "" ? -2 : -1).filter(Boolean);
    if (heads.length && heads.every((token) => Object.prototype.hasOwnProperty.call(modifierBits, token.toLowerCase()))) {
      for (const token of heads) modifiers |= modifierBits[token.toLowerCase()];
      main = last;
    }
  }
  // A name is read without its spaces, underscores, or hyphens ("Page Up", "page_down"), as on the desktop.
  const squashed = main.length > 1 ? main.toLowerCase().replace(/[\s_-]+/g, "") : main.toLowerCase();
  let key = aliases[main.toLowerCase()] || aliases[squashed] || main;
  const virtualKeys = {
    Backspace: 8,
    Tab: 9,
    Enter: 13,
    Shift: 16,
    Control: 17,
    Alt: 18,
    Escape: 27,
    " ": 32,
    PageUp: 33,
    PageDown: 34,
    End: 35,
    Home: 36,
    ArrowLeft: 37,
    ArrowUp: 38,
    ArrowRight: 39,
    ArrowDown: 40,
    Insert: 45,
    Delete: 46,
    Meta: 91,
    ";": 186,
    "=": 187,
    "+": 187,
    ",": 188,
    "-": 189,
    ".": 190,
    "/": 191,
    "`": 192,
    "[": 219,
    "\\": 220,
    "]": 221,
    "'": 222,
  };
  for (let n = 1; n <= 12; n += 1) virtualKeys[`F${n}`] = 111 + n;
  let code = key;
  if (/^[a-z]$/i.test(key)) {
    code = `Key${key.toUpperCase()}`;
    key = modifiers & 8 ? key.toUpperCase() : key.toLowerCase();
  } else if (/^[0-9]$/.test(key)) code = `Digit${key}`;
  else if (key === " ") code = "Space";
  const windowsVirtualKeyCode = virtualKeys[key] || (key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0);
  const description = {
    key,
    code,
    windowsVirtualKeyCode,
    nativeVirtualKeyCode: windowsVirtualKeyCode,
    modifiers,
  };
  const commandChord = Boolean(modifiers & (1 | 2 | 4));
  if (!commandChord) {
    if (key === "Enter") description.text = "\r";
    else if (key.length === 1) description.text = key;
    if (description.text) description.unmodifiedText = description.text;
  }
  return description;
}

function chordModifierKeys(bits) {
  // Modifier bitmask (alt 1, ctrl 2, meta 4, shift 8) -> the modifier keys pressed first, in the
  // order a person presses them.
  const mask = Number(bits || 0);
  const table = [
    { bit: 2, key: "Control", code: "ControlLeft", vk: 17 },
    { bit: 1, key: "Alt", code: "AltLeft", vk: 18 },
    { bit: 4, key: "Meta", code: "MetaLeft", vk: 91 },
    { bit: 8, key: "Shift", code: "ShiftLeft", vk: 16 },
  ];
  return table.filter((entry) => mask & entry.bit);
}

function clickTargetIdentityFromPoint(point) {
  const source = point && typeof point === "object" ? point : {};
  const targetTag = String(source.target_tag || source.tag || "").trim().toLowerCase();
  const rawType = String(source.target_type || "").trim().toLowerCase();
  return {
    target_selector: String(source.target_selector || "").trim(),
    target_text: String(source.target_text || "").replace(/\s+/g, " ").trim(),
    target_tag: targetTag,
    // The observer reports a missing type attribute as the tag name (input_type fallback) while
    // in-page probes read the attribute itself; both mean "no explicit type".
    target_type: rawType === targetTag ? "" : rawType,
    target_role: String(source.target_role || "").trim().toLowerCase(),
    dispatch_path: String(source.dispatch_path || "").trim(),
    page_url: String(source.page_url || "").trim(),
    target_names: Array.isArray(source.target_names)
      ? source.target_names.map((name) => String(name || "").replace(/\s+/g, " ").trim()).filter(Boolean)
      : [],
  };
}

// A control's text can carry live widget text after its label (a typeahead's status inside the
// wrapping <label>), which differs between observation and action; the same leading label at a word
// boundary is the same control. Never used for strict (final-submit, guarded) identity.
function sameLeadingIdentityText(left, right) {
  const a = String(left || "").replace(/\s+/g, " ").trim();
  const b = String(right || "").replace(/\s+/g, " ").trim();
  // "First Name *" observed vs "First Name*" live (Greenhouse, run 1795): the same characters.
  if (a && a.replace(/\s+/g, "") === b.replace(/\s+/g, "")) return true;
  const [shorter, longer] = a.length <= b.length ? [a, b] : [b, a];
  return shorter.length >= 4 && longer.startsWith(shorter) && (longer.length === shorter.length || longer[shorter.length] === " ");
}

function clickTargetIdentityVerdict(expected, live, strict = false) {
  if (!expected || typeof expected !== "object") return { ok: true, reason: "" };
  const expectedIdentity = clickTargetIdentityFromPoint(expected);
  const liveIdentity = clickTargetIdentityFromPoint(live);
  if (
    !expectedIdentity.target_tag
    || !liveIdentity.target_tag
    || !expectedIdentity.dispatch_path
    || !liveIdentity.dispatch_path
    || !expectedIdentity.page_url
    || !liveIdentity.page_url
  ) {
    return {
      ok: false,
      reason: "click_target_identity_unavailable",
      expected: expectedIdentity,
      live: liveIdentity,
    };
  }
  // A control that keeps its tag, type, role, and visible text IS the same control even when a
  // re-render regenerated its id or moved it in the tree; only a control with no visible text to
  // compare (icon buttons) falls back to exact selector identity. Final-submit and guarded clicks
  // stay strict: their authority was bound to one exact element.
  const semanticFields = ["target_text", "target_tag", "target_type", "target_role", "dispatch_path", "page_url"];
  // A strict probe that bound the one page-wide exact-identity twin (allowed only after its caller
  // proved the page's semantic state unchanged) is that element; its fresh selector is expected.
  const strictTwin = Boolean(strict && live && live.strict_twin_rebound === true && expectedIdentity.target_text);
  const exactFields = (strict && !strictTwin) || !expectedIdentity.target_text
    ? ["target_selector", ...semanticFields]
    : semanticFields;
  // The observer and each in-page probe name a control with different fallback orders (own
  // text, associated label, aria-label, placeholder, name). The runtime sends every raw name it
  // observed for the control; a live name matching any of them is the same control.
  for (const field of exactFields) {
    const matches = expectedIdentity[field] === liveIdentity[field]
      || (
        field === "target_text"
        && !strict
        && Boolean(liveIdentity.target_text)
        && (
          expectedIdentity.target_names.includes(liveIdentity.target_text)
          || (
            // Only for the exact same element: a selector-rebound control must match by name.
            Boolean(expectedIdentity.target_selector)
            && expectedIdentity.target_selector === liveIdentity.target_selector
            && [expectedIdentity.target_text, ...expectedIdentity.target_names]
              .some((name) => sameLeadingIdentityText(name, liveIdentity.target_text))
          )
        )
      );
    if (!matches) {
      return {
        ok: false,
        reason: "click_target_drift",
        mismatch_field: field,
        expected: expectedIdentity,
        live: liveIdentity,
      };
    }
  }
  return {
    ok: true,
    reason: "",
    expected: expectedIdentity,
    live: liveIdentity,
    selector_rebound: expectedIdentity.target_selector !== liveIdentity.target_selector,
  };
}

function clickPointIdentityComplete(value) {
  if (!value || typeof value !== "object") return false;
  const requiredFields = [
    "target_selector",
    "target_text",
    "target_tag",
    "target_type",
    "target_role",
    "dispatch_path",
    "page_url",
  ];
  if (!requiredFields.every((field) => Object.prototype.hasOwnProperty.call(value, field))) return false;
  const identity = clickTargetIdentityFromPoint(value);
  return Boolean(identity.target_tag && identity.dispatch_path && identity.page_url);
}

function clickTargetIdentityComplete(value) {
  if (!value || typeof value !== "object") return false;
  const requiredFields = [
    "target_selector",
    "target_text",
    "target_tag",
    "target_type",
    "target_role",
    "dispatch_path",
    "page_url",
  ];
  if (!requiredFields.every((field) => Object.prototype.hasOwnProperty.call(value, field))) return false;
  const identity = clickTargetIdentityFromPoint(value);
  return Boolean(identity.target_selector && identity.target_tag && identity.dispatch_path && identity.page_url);
}

// Whether a document-state drift is confined to the one control the action names. Both sides are the
// per-row digests of the same enumeration (selector, frame flag, row digest), plus the title as the row
// with an empty selector. Every row other than the target must be present on both sides with the same
// frame flag and digest, so any other control, the title, or a row added or removed refuses. The target
// row itself may change in any field (its value, its validation text, its class): the action's own
// identity gate still runs on the live control before anything is typed or clicked. A row list that is
// missing, capped, or has a repeated selector proves nothing and refuses. With topDocumentOnly (a
// credential), the target must sit in the top document on both sides.
function targetOnlyStateDriftConfined(expectedRows, liveRows, targetScope) {
  const target = String((targetScope && targetScope.selector) || "").trim();
  if (!target || !Array.isArray(expectedRows) || !Array.isArray(liveRows)) return false;
  if (!expectedRows.length || !liveRows.length) return false;
  const indexRows = (rows) => {
    const map = new Map();
    for (const row of rows) {
      if (!row || typeof row !== "object") return null;
      const key = String(row.selector == null ? "" : row.selector);
      if (map.has(key)) return null;
      map.set(key, row);
    }
    return map;
  };
  const expectedMap = indexRows(expectedRows);
  const liveMap = indexRows(liveRows);
  if (!expectedMap || !liveMap || expectedMap.size !== liveMap.size) return false;
  if (!expectedMap.has("") || !liveMap.has("")) return false;
  const targetExpected = expectedMap.get(target);
  const targetLive = liveMap.get(target);
  if (!targetExpected || !targetLive) return false;
  if (targetExpected.frame !== targetLive.frame) return false;
  if (targetScope.topDocumentOnly === true && (targetExpected.frame !== 0 || targetLive.frame !== 0)) {
    return false;
  }
  for (const [key, row] of expectedMap) {
    if (key === target) continue;
    const live = liveMap.get(key);
    if (!live || live.frame !== row.frame || live.digest !== row.digest) return false;
  }
  return true;
}

async function observedPageContextVerdict(
  tabId,
  expectedPageContext,
  strict = true,
  valuesSuffice = false,
  semanticSuffices = false,
  targetScope = null,
) {
  const expected = expectedPageContext && typeof expectedPageContext === "object"
    ? expectedPageContext
    : {};
  const expectedUrl = String(expected.url || "").trim();
  if (!expectedUrl) {
    return { ok: false, reason: "page_context_required", expected, live: {} };
  }
  const live = await tabPageEvidence(tabId);
  if (String(live.url || "").trim() !== expectedUrl) {
    return { ok: false, reason: "page_context_drift", mismatch_field: "url", expected, live };
  }
  // A probe that did not finish says nothing about the document: report it as unavailable (still
  // fail-closed) rather than as a changed document, which no retry on the same page could clear.
  if (live.surface_probe_complete === false) {
    return { ok: false, reason: "page_context_unavailable", mismatch_field: "surface_probe", expected, live };
  }
  const expectedSurface = String(expected.surface_signature || "").trim();
  const expectedStableSurface = String(expected.stable_surface_signature || "").trim();
  const expectedDocumentState = String(expected.document_state_signature || "").trim();
  const expectedDocumentLifecycle = String(expected.document_lifecycle_token || "").trim();
  if (
    expectedDocumentLifecycle
    && String(live.document_lifecycle_token || "").trim() !== expectedDocumentLifecycle
  ) {
    // The eight leading characters of each token are diagnostics only (random per document): they show
    // whether the live read disagreed with the observation's token or agreed with a different document.
    return {
      ok: false,
      reason: "page_context_drift",
      mismatch_field: "document_lifecycle_token",
      expected,
      live,
      lifecycle_expected_prefix: expectedDocumentLifecycle.slice(0, 8),
      lifecycle_live_prefix: String(live.document_lifecycle_token || "").trim().slice(0, 8),
    };
  }
  // Same document, same URL: content drift (tickers, re-rendering panels, live regions) is a
  // fact about the page, not proof the intended control is gone. Ordinary clicks report the
  // drift and let the per-target identity gate decide; strict callers (final submit, guarded
  // intermediates, sensitive masks) still require the exact surface they were bound to.
  let driftField = "";
  if (
    expectedDocumentState
    && String(live.document_state_signature || "").trim() !== expectedDocumentState
  ) {
    driftField = "document_state_signature";
  } else if (
    !expectedDocumentState
    && expectedStableSurface
    && String(live.stable_surface_signature || "").trim() !== expectedStableSurface
  ) {
    driftField = "stable_surface_signature";
  } else if (
    !expectedDocumentState
    && !expectedStableSurface
    && expectedSurface
    && String(live.surface_signature || "").trim() !== expectedSurface
  ) {
    driftField = "surface_signature";
  }
  const expectedValues = String(expected.document_value_signature || "").trim();
  if (
    driftField === "document_state_signature"
    && valuesSuffice
    && expectedValues
    && String(live.document_value_signature || "").trim() === expectedValues
  ) {
    // Only labels or status text moved (a live page's tickers, reshuffled buttons): every field value
    // the click would carry is exactly as observed, and the target's own identity is checked next.
    driftField = "";
  }
  // Every control still shows the same label, value, and state; only DOM ids, selectors, or the
  // order of buttons and links changed (a page re-rendering itself). That proof also lets a final or
  // guarded click follow its target to its one exact-identity twin (semantic_state_verified).
  const expectedSemantic = String(expected.document_semantic_signature || "").trim();
  const semanticStateVerified = Boolean(
    semanticSuffices
    && expectedSemantic
    && String(live.document_semantic_signature || "").trim() === expectedSemantic
  );
  if (driftField === "document_state_signature" && semanticStateVerified) {
    driftField = "";
  }
  // Same document, same URL, same title, and every control but the named target unchanged: the only drift is
  // the target's own row (its typed value, its validation text). The target's identity is still re-proven by the
  // caller before any dispatch, so the check accepts the drift only for that one row.
  let targetStateDrift = false;
  if (
    driftField === "document_state_signature"
    && targetScope
    && targetOnlyStateDriftConfined(expected.document_row_digests, live.document_row_digests, targetScope)
  ) {
    driftField = "";
    targetStateDrift = true;
  }
  if (driftField && strict) {
    return { ok: false, reason: "page_context_drift", mismatch_field: driftField, expected, live };
  }
  return {
    ok: true,
    reason: "",
    page_drifted: Boolean(driftField),
    drift_field: driftField,
    semantic_state_verified: semanticStateVerified,
    target_state_drift: targetStateDrift,
    expected,
    live,
  };
}

async function observedSensitiveMaskPageContextVerdict(
  tabId,
  selectorOrSelectors,
  expectedPageContext,
) {
  const expected = expectedPageContext && typeof expectedPageContext === "object"
    ? expectedPageContext
    : {};
  const expectedUrl = String(expected.url || "").trim();
  const expectedMaskSurface = String(expected.mask_surface_signature || "").trim();
  if (!expectedUrl || !expectedMaskSurface) {
    return { ok: false, reason: "sensitive_mask_page_context_required", expected, live: {} };
  }
  // Compute the comparison with the nominated control value-redacted inside the page. The secret
  // never crosses the extension channel and never contributes to this structural signature.
  const live = await tabPageEvidence(
    tabId,
    normalizeSensitiveScreenshotSelectors(selectorOrSelectors),
  );
  if (String(live.url || "").trim() !== expectedUrl) {
    return {
      ok: false,
      reason: "sensitive_mask_page_context_drift",
      mismatch_field: "url",
      expected,
      live: {
        url: String(live.url || ""),
        mask_surface_signature: String(live.mask_surface_signature || ""),
      },
    };
  }
  if (String(live.mask_surface_signature || "").trim() !== expectedMaskSurface) {
    return {
      ok: false,
      reason: "sensitive_mask_page_context_drift",
      mismatch_field: "mask_surface_signature",
      expected,
      live: {
        url: String(live.url || ""),
        mask_surface_signature: String(live.mask_surface_signature || ""),
      },
    };
  }
  return {
    ok: true,
    reason: "",
    expected,
    live: {
      url: String(live.url || ""),
      mask_surface_signature: String(live.mask_surface_signature || ""),
    },
  };
}

// Reads and disarms a final or guarded click's dispatch guard (armed by the adjacent point probe).
// An unreadable page (a navigation the click started) reports nothing blocked.
async function readDispatchGuard(tabId) {
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: dispatchGuardReportInPage }),
      1500,
      "dispatch guard read-back timed out",
    );
    return (rows && rows[0] && rows[0].result) || { present: false, blocked: false };
  } catch (_) {
    return { present: false, blocked: false };
  }
}

function dispatchGuardReportInPage() {
  try {
    const guard = globalThis.__lumenDispatchGuard;
    if (!guard || !guard.report) return { present: false, blocked: false };
    const report = { present: true, ...guard.report };
    if (typeof guard.disarm === "function") guard.disarm();
    globalThis.__lumenDispatchGuard = null;
    return report;
  } catch (_) {
    return { present: false, blocked: false };
  }
}

// Evaluation hygiene: close every tab (and so every popup window) showing an Interaction Gauntlet page
// served from this machine, so the next page never starts beside a stale copy pointing at a harness that
// is gone. Only loopback /eval/bench/ pages match; nothing else the user has open can.
function isLocalBenchUrl(rawUrl) {
  try {
    const url = new URL(String(rawUrl || ""));
    const loopback = ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
    return loopback && ["http:", "https:"].includes(url.protocol) && url.pathname.startsWith("/eval/bench/");
  } catch (_) {
    return false;
  }
}

async function closeBenchTabs() {
  let tabs = [];
  try {
    tabs = await chrome.tabs.query({});
  } catch (error) {
    return { ok: false, action: "close_bench_tabs", error: errorMessage(error) };
  }
  const ids = tabs.filter((tab) => tab && tab.id !== undefined && isLocalBenchUrl(tab.url || tab.pendingUrl)).map((tab) => tab.id);
  if (ids.length && ids.length === tabs.length) {
    // Closing every tab would close the browser itself, and with it this extension's connection: keep
    // one blank tab open first.
    try {
      await chrome.tabs.create({ url: "about:blank", active: false });
    } catch (error) {
      return { ok: false, action: "close_bench_tabs", error: errorMessage(error) };
    }
  }
  let closed = 0;
  for (const id of ids) {
    try {
      await chrome.tabs.remove(id);
      closed += 1;
    } catch (_) {}
  }
  return { ok: true, action: "close_bench_tabs", closed };
}

// Before a click: the target must hold still, be visible and enabled, and be the thing a pointer at the
// click point reaches. Returns the point to press (it moves when the target had to be scrolled into the
// clear) or a refusal naming exactly which condition failed (covered, disabled, pointer_events_none,
// detached, not_visible). Reads and scrolls only; never presses anything.
async function clickActionabilityInPage(selector, cssX, cssY) {
  try {
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const deepAt = (x, y) => (
      typeof globalThis.__lumenDeepElementFromPoint === "function"
        ? globalThis.__lumenDeepElementFromPoint(x, y)
        : document.elementFromPoint(x, y)
    );
    const contains = (ancestor, node) => (
      typeof globalThis.__lumenComposedContains === "function"
        ? globalThis.__lumenComposedContains(ancestor, node)
        : Boolean(ancestor && node && ancestor.contains && ancestor.contains(node))
    );
    const rectOf = (node) => (typeof globalThis.__lumenTopRect === "function" ? globalThis.__lumenTopRect(node) : node.getBoundingClientRect());
    const describe = (node) => {
      try {
        if (!node || !node.tagName) return "";
        const classes = clean(typeof node.className === "string" ? node.className : node.getAttribute("class")).split(" ").filter(Boolean).slice(0, 2);
        const head = (node.tagName || "").toLowerCase() + (classes.length ? "." + classes.join(".") : "");
        const role = clean(node.getAttribute("role"));
        const name = clean(node.getAttribute("aria-label") || node.innerText || node.textContent || "").slice(0, 40);
        return head + (role || name ? " / " + [role, name ? '"' + name + '"' : ""].filter(Boolean).join(" ") : "");
      } catch (_) { return ""; }
    };
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const dpr = window.devicePixelRatio || 1;
    let node = null;
    if (selector) {
      try {
        const matches = typeof globalThis.__lumenDeepQueryAll === "function"
          ? globalThis.__lumenDeepQueryAll(String(selector))
          : Array.from(document.querySelectorAll(String(selector)));
        if (matches.length === 1) node = matches[0];
      } catch (_) {}
    }
    const bound = Boolean(node);
    if (!node) {
      const hit = deepAt(cssX, cssY);
      node = hit && hit.closest ? (hit.closest('button,a[href],input,textarea,select,label,[role],[tabindex]') || hit) : hit;
    }
    if (!node) return { ok: true, checked: false };
    const refuse = (code, detail, extra = {}) => ({ ok: false, code, failure_reason: "click_" + code, detail, actionability_code: code, ...extra });

    // stable: the same box over two animation frames and no finite animation still running, capped.
    const frame = () => new Promise((resolve) => {
      let done = false;
      const finish = () => { if (!done) { done = true; resolve(); } };
      try { requestAnimationFrame(finish); } catch (_) {}
      setTimeout(finish, 50);
    });
    const sameBox = (a, b) => Math.abs(a.left - b.left) < 0.5 && Math.abs(a.top - b.top) < 0.5
      && Math.abs(a.width - b.width) < 0.5 && Math.abs(a.height - b.height) < 0.5;
    const animating = () => {
      try {
        return typeof node.getAnimations === "function" && node.getAnimations().some((animation) => {
          if (animation.playState !== "running") return false;
          const end = animation.effect && animation.effect.getComputedTiming ? animation.effect.getComputedTiming().endTime : 0;
          return Number.isFinite(Number(end));
        });
      } catch (_) { return false; }
    };
    const started = Date.now();
    let previous = rectOf(node);
    let matches = 0;
    let stable = false;
    while (Date.now() - started < 500) {
      await frame();
      if (!node.isConnected) break;
      const current = rectOf(node);
      if (sameBox(previous, current) && !animating()) {
        matches += 1;
        if (matches >= 1) { stable = true; break; }
      } else {
        matches = 0;
      }
      previous = current;
    }
    const waitedMs = Date.now() - started;
    if (!node.isConnected) return refuse("detached", "the target left the page while it settled", { actionability_waited_ms: waitedMs });
    let rect = rectOf(node);
    const style = window.getComputedStyle(node);
    if (rect.width <= 0 || rect.height <= 0 || style.visibility === "hidden" || style.display === "none") {
      return refuse("not_visible", "the target has no visible box (" + describe(node) + ")", { actionability_waited_ms: waitedMs });
    }
    try {
      if (node.matches && node.matches(":disabled")) {
        return refuse("disabled", describe(node) + " is disabled and takes no pointer input", { actionability_waited_ms: waitedMs });
      }
    } catch (_) {}
    if (style.pointerEvents === "none") {
      let owner = node;
      try {
        while (owner.parentElement && window.getComputedStyle(owner.parentElement).pointerEvents === "none") owner = owner.parentElement;
      } catch (_) {}
      return refuse("pointer_events_none", describe(owner) + " sets pointer-events: none on the target", { actionability_waited_ms: waitedMs });
    }
    const readonly = Boolean(node.readOnly || clean(node.getAttribute && node.getAttribute("aria-readonly")).toLowerCase() === "true");
    const facts = { actionability_waited_ms: waitedMs, actionability_stable: stable, ...(readonly ? { target_readonly: true } : {}) };
    const centerOf = (box) => {
      const left = Math.max(box.left, 0);
      const right = Math.min(box.right, window.innerWidth);
      const top = Math.max(box.top, 0);
      const bottom = Math.min(box.bottom, window.innerHeight);
      return { x: Math.round((left + right) / 2), y: Math.round((top + bottom) / 2) };
    };
    const reach = (x, y) => {
      if (x < 0 || y < 0 || x >= window.innerWidth || y >= window.innerHeight) return { clear: false, by: null, offscreen: true };
      const hit = deepAt(x, y);
      if (!hit || hit === node || contains(node, hit) || contains(hit, node)) return { clear: true };
      try {
        const label = hit.closest ? hit.closest("label") : null;
        if (label && (label.control === node || contains(label, node))) return { clear: true };
      } catch (_) {}
      return { clear: false, by: hit };
    };
    let x = Math.round(cssX);
    let y = Math.round(cssY);
    let scrolled = 0;
    if (bound) {
      const inside = x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
      if (!inside) ({ x, y } = centerOf(rect));
      let verdict = reach(x, y);
      if (!verdict.clear) {
        const alignments = ["center", "end", "start"];
        const backoff = [0, 20, 100, 100, 500];
        for (let attempt = 0; attempt < backoff.length && !verdict.clear; attempt += 1) {
          await sleep(backoff[attempt]);
          if (!node.isConnected) return refuse("detached", "the target left the page while it was scrolled into view", facts);
          try { node.scrollIntoView({ block: alignments[attempt % 3], inline: "nearest", behavior: "instant" }); } catch (_) {}
          scrolled += 1;
          rect = rectOf(node);
          ({ x, y } = centerOf(rect));
          verdict = reach(x, y);
        }
      }
      if (!verdict.clear) {
        if (verdict.offscreen) return refuse("not_visible", "the target stays outside the viewport after scrolling it into view", { ...facts, actionability_scrolled: scrolled });
        return refuse(
          "covered",
          describe(verdict.by) + " covers the target at (" + x + "," + y + ")",
          { ...facts, actionability_scrolled: scrolled, covering_element: describe(verdict.by) },
        );
      }
    }
    const moved = scrolled > 0 || x !== Math.round(cssX) || y !== Math.round(cssY);
    return {
      ok: true,
      checked: true,
      moved,
      x,
      y,
      img_x: Math.round(x * dpr),
      img_y: Math.round(y * dpr),
      ...facts,
      ...(scrolled ? { actionability_scrolled: scrolled } : {}),
    };
  } catch (error) {
    return { ok: true, checked: false, error: String(error) };
  }
}

async function runTrustedVisionClick(
  tabId,
  imgX,
  imgY,
  allowFinalSubmit = false,
  allowGuardedIntermediate = false,
  expectedTarget = null,
  pointer = null,
  strictTwinRebind = false,
) {
  const target = { tabId };
  let attached = false;
  let activationAttempted = false;
  let activationDispatched = false;
  // Set once the press reaches Chrome. Its CDP ack only returns after the page's handlers ran, so
  // a timeout after this point cannot prove the click did not happen.
  let pressSent = false;
  let dispatchGuardArmed = false;
  // Whether this click's dispatch guard (if any) has been read back and disarmed.
  let guardSettled = false;
  try {
    const pointRows = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId },
        func: visionPointInfoInPage,
        args: [
          imgX,
          imgY,
          Boolean(allowFinalSubmit),
          Boolean(allowGuardedIntermediate),
          expectedTarget,
          Boolean(strictTwinRebind),
          false,
        ],
      }),
      2500,
      "trusted click point probe timed out",
    );
    let point = (pointRows && pointRows[0] && pointRows[0].result) || { ok: false, action: "click", error: "trusted click point probe returned nothing" };
    if (!point.ok || point.blocked_submit) {
      return {
        ...point,
        action: "click",
        mode: "cdp_trusted_click",
        trusted_click: true,
        activation_attempted: false,
        activation_dispatched: false,
        activation_count: 0,
      };
    }
    const strictIdentity = Boolean(allowFinalSubmit || allowGuardedIntermediate);
    const identityVerdict = clickTargetIdentityVerdict(expectedTarget, point, strictIdentity);
    if (!identityVerdict.ok) {
      return {
        ...point,
        action: "click",
        mode: "cdp_trusted_click",
        trusted_click: true,
        ok: false,
        failure_reason: identityVerdict.reason,
        target_identity_matched: false,
        target_identity_mismatch_field: identityVerdict.mismatch_field || "",
        preflight_target_selector: (identityVerdict.expected || {}).target_selector || "",
        live_target_selector: (identityVerdict.live || {}).target_selector || "",
        activation_attempted: false,
        activation_dispatched: false,
        activation_count: 0,
      };
    }
    // Actionability: hold still, visible, enabled, and reachable at the point, or a refusal that names
    // which one failed. A scroll that uncovers the target moves the press point with it.
    let actionability = null;
    try {
      const actionRows = await withTimeout(
        chrome.scripting.executeScript({
          target: { tabId },
          func: clickActionabilityInPage,
          args: [point.target_selector || "", point.x, point.y],
        }),
        3000,
        "click actionability check timed out",
      );
      actionability = (actionRows && actionRows[0] && actionRows[0].result) || null;
    } catch (_) {
      actionability = null;
    }
    if (actionability && actionability.ok === false) {
      return {
        ...point,
        action: "click",
        mode: "cdp_trusted_click",
        trusted_click: true,
        ok: false,
        failure_reason: actionability.failure_reason,
        failure_detail: "not_actionable: " + String(actionability.detail || ""),
        actionability_code: actionability.code,
        covering_element: actionability.covering_element || "",
        actionability_waited_ms: actionability.actionability_waited_ms || 0,
        actionability_scrolled: actionability.actionability_scrolled || 0,
        activation_attempted: false,
        activation_dispatched: false,
        activation_count: 0,
      };
    }
    if (actionability && actionability.moved && Number.isFinite(actionability.img_x) && Number.isFinite(actionability.img_y)) {
      imgX = actionability.img_x;
      imgY = actionability.img_y;
    }
    const actionabilityFacts = actionability && actionability.checked
      ? {
        actionability_waited_ms: actionability.actionability_waited_ms || 0,
        ...(actionability.actionability_scrolled ? { actionability_scrolled: actionability.actionability_scrolled } : {}),
        ...(actionability.target_readonly ? { target_readonly: true } : {}),
      }
      : {};
    // Let the user watch the pointer glide to the click target. Purely visual
    // (pointer-events: none) and finished here, so nothing sits between the
    // adjacent identity gate below and the single trusted activation.
    // A reaction to a moment (a click right after its wait saw the text) skips the visual glide.
    const glideMs = pointer && pointer.glide_ms !== undefined && pointer.glide_ms !== null && Number.isFinite(Number(pointer.glide_ms))
      ? Math.max(0, Math.min(1000, Number(pointer.glide_ms)))
      : 240;
    if (glideMs > 0) await animatePointerToPoint(tabId, point.x, point.y, glideMs);
    await acquireDebugger(target);
    attached = true;
    // A double-click or right/middle click keeps the guard through the whole gesture.
    const gestureWillBeMultiEvent = Boolean(
      pointer
      && (Number(pointer.clicks || 1) > 1 || ["right", "middle"].includes(String(pointer.button || ""))),
    );
    // A final or guarded click re-binds here and arms its dispatch guard on the bound node.
    const immediateRows = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId },
        func: visionPointInfoInPage,
        args: [
          imgX,
          imgY,
          Boolean(allowFinalSubmit),
          Boolean(allowGuardedIntermediate),
          expectedTarget || point,
          Boolean(strictTwinRebind),
          true,
          !allowFinalSubmit && (gestureWillBeMultiEvent),
        ],
      }),
      2500,
      "immediate trusted click target probe timed out",
    );
    const immediatePoint = (immediateRows && immediateRows[0] && immediateRows[0].result)
      || { ok: false, action: "click", failure_reason: "click_target_identity_unavailable" };
    dispatchGuardArmed = Boolean(immediatePoint.dispatch_guard_armed);
    const immediateVerdict = clickTargetIdentityVerdict(expectedTarget || point, immediatePoint, strictIdentity);
    if (!immediatePoint.ok || immediatePoint.blocked_submit || immediatePoint.blocked_file_dialog || !immediateVerdict.ok) {
      if (dispatchGuardArmed) await readDispatchGuard(tabId);
      guardSettled = true;
      return {
        ...immediatePoint,
        action: "click",
        mode: "cdp_trusted_click",
        trusted_click: true,
        ok: false,
        failure_reason: immediatePoint.failure_reason || immediateVerdict.reason || "click_target_drift",
        target_identity_matched: false,
        target_identity_mismatch_field: immediatePoint.target_identity_mismatch_field || immediateVerdict.mismatch_field || "",
        preflight_target_selector: immediatePoint.preflight_target_selector || (immediateVerdict.expected || {}).target_selector || "",
        live_target_selector: immediatePoint.live_target_selector || (immediateVerdict.live || {}).target_selector || "",
        activation_attempted: false,
        activation_dispatched: false,
        activation_count: 0,
      };
    }
    point = immediatePoint;
    // Keep the immediate identity gate adjacent to the first activating event for every click.
    // A synthetic hover is not required to activate the observed target, can replace reactive
    // controls after the gate, and creates a pre-activation CDP timeout with no useful receipt.
    activationAttempted = true;
    // One human gesture: the model's button and click count (a double-click is press/release with
    // clickCount 1 then 2, which is how Chrome produces dblclick). Final authority stays one plain click.
    const gestureButton = !allowFinalSubmit && pointer && ["right", "middle"].includes(String(pointer.button || ""))
      ? String(pointer.button)
      : "left";
    const gestureButtons = { left: 1, right: 2, middle: 4 }[gestureButton];
    const gestureClicks = allowFinalSubmit ? 1 : Math.max(1, Math.min(3, Math.round(Number((pointer && pointer.clicks) || 1))));
    // Press-and-hold and modifier-clicks (shift/ctrl/alt/meta held during the click) are part of
    // the gesture; final authority stays one plain click.
    const modifierBits = allowFinalSubmit
      ? 0
      : (Array.isArray(pointer && pointer.modifiers) ? pointer.modifiers : []).reduce(
        (bits, name) => bits | ({ alt: 1, ctrl: 2, meta: 4, shift: 8 }[String(name || "").toLowerCase()] || 0),
        0,
      );
    const holdMs = allowFinalSubmit ? 0 : Math.max(0, Math.min(5000, Math.round(Number((pointer && pointer.hold_ms) || 0))));
    let dialogOpened = false;
    let pressedAtMs = 0;
    for (let clickCount = 1; clickCount <= gestureClicks && !dialogOpened; clickCount += 1) {
      if (!pressedAtMs) pressedAtMs = Date.now();
      pressSent = true;
      const pressed = await dispatchInputUnlessDialog(target, "Input.dispatchMouseEvent", { type: "mousePressed", x: point.x, y: point.y, button: gestureButton, buttons: gestureButtons, clickCount, modifiers: modifierBits });
      if (pressed === "dialog") { dialogOpened = true; break; }
      if (holdMs > 0) await new Promise((resolve) => setTimeout(resolve, holdMs));
      const released = await dispatchInputUnlessDialog(target, "Input.dispatchMouseEvent", { type: "mouseReleased", x: point.x, y: point.y, button: gestureButton, buttons: 0, clickCount, modifiers: modifierBits });
      if (released === "dialog") dialogOpened = true;
    }
    activationDispatched = true;
    if (dialogOpened) {
      // The page is frozen behind a browser dialog: no in-page read-back is possible until it
      // is answered. The dialog itself is the observed effect.
      return {
        ok: true,
        action: "click",
        mode: "cdp_trusted_click",
        trusted_click: true,
        ...point,
        pressed_at_ms: pressedAtMs,
        target_identity_matched: Boolean(expectedTarget),
        preflight_target_selector: (identityVerdict.expected || {}).target_selector || "",
        live_target_selector: (identityVerdict.live || {}).target_selector || point.target_selector || "",
        activation_attempted: true,
        activation_dispatched: true,
        activation_count: 1,
        surface_changed: true,
        verification_state: "observed_effect",
        native_dialog: publicNativeDialog(nativeDialogOpenFor(tabId)),
      };
    }
    if (dispatchGuardArmed) {
      const guard = await readDispatchGuard(tabId);
      guardSettled = true;
      if (guard.blocked && !strictIdentity && !guard.partial) {
        // The first pointer event did not reach the target: something else sits at the point (an
        // overlay, a sticky bar, a layout that shifted). The event was stopped ahead of every page
        // listener, so the page saw nothing; the receipt names what intercepts.
        const by = String(guard.intercepted_by || "another element");
        return {
          ...point,
          ok: false,
          action: "click",
          mode: "cdp_trusted_click",
          trusted_click: true,
          failure_reason: "click_intercepted",
          failure_detail: "intercepted: " + by + " intercepts pointer events at the click point",
          intercepted_by: by,
          dispatch_intercepted: true,
          ...(guard.focus_restored ? { focus_restored: true } : {}),
          ...(guard.focus_moved && !guard.focus_restored ? { focus_may_have_moved: true } : {}),
          target_identity_matched: false,
          activation_attempted: false,
          activation_dispatched: false,
          activation_count: 0,
        };
      }
      if (guard.blocked) {
        // The page swapped what sat under the point between the adjacent probe and the press. The
        // guard stopped those events before the page's own listeners (any window capture listener the
        // page registered earlier still saw them), so the outcome is verified, never assumed. Part of
        // the gesture reaching the bound node first makes its outcome unknown.
        return {
          ...point,
          ok: false,
          action: "click",
          mode: "cdp_trusted_click",
          trusted_click: true,
          failure_reason: guard.partial ? "click_execution_outcome_unknown" : "click_target_drift_at_dispatch",
          dispatch_guard_blocked: true,
          dispatch_guard_partial: Boolean(guard.partial),
          dispatch_guard_blocked_events: Number(guard.blocked_events || 0),
          ...(guard.focus_restored ? { focus_restored: true } : {}),
          ...(guard.focus_moved && !guard.focus_restored ? { focus_may_have_moved: true } : {}),
          target_identity_matched: false,
          activation_attempted: true,
          activation_dispatched: true,
          activation_count: guard.partial ? 1 : 0,
          ...(guard.partial ? { execution_outcome_unknown: true } : {}),
        };
      }
    }
    // Pulse only after a real activation so a gate-blocked click never shows a
    // click visual that did not happen.
    showPointerClickPulse(tabId, point.x, point.y);
    // Settle on what the click actually started: a navigation's load, else the page's own
    // reaction (a quiet DOM), instead of a fixed wait for a load event that never comes. The quiet
    // watch starts after the 200 ms load-start window on purpose: overlapping them (tried
    // 2026-09-28) moves the post-click capture earlier into a page's delayed reaction. A click the
    // model chained into a watch skips the settle: the watch is how it observes the reaction, and a
    // live gauge never goes quiet, so settling would spend the readings the watch is for.
    const skipSettle = Boolean(pointer && pointer.settle === false);
    const navigation = skipSettle ? null : await waitForLoad(tabId, 1500, { startWindowMs: 200 });
    if (!skipSettle && !(navigation && navigation.navigation_started)) {
      try {
        await withTimeout(
          chrome.scripting.executeScript({ target: { tabId }, func: domQuietInPage, args: [250, 1000] }),
          1600,
          "post-click settle timed out",
        );
      } catch (_) {}
    }
    let active = {};
    try {
      const afterRows = await withTimeout(
        chrome.scripting.executeScript({ target: { tabId }, func: activeElementEvidenceInPage }),
        2500,
        "trusted click active element evidence timed out",
      );
      active = (afterRows && afterRows[0] && afterRows[0].result) || {};
    } catch (_) {
      active = {};
    }
    return {
      pressed_at_ms: pressedAtMs,
      ok: true,
      action: "click",
      mode: "cdp_trusted_click",
      trusted_click: true,
      ...point,
      ...active,
      target_identity_matched: Boolean(expectedTarget),
      preflight_target_selector: (identityVerdict.expected || {}).target_selector || "",
      live_target_selector: (identityVerdict.live || {}).target_selector || point.target_selector || "",
      activation_attempted: true,
      activation_dispatched: true,
      activation_count: 1,
      ...actionabilityFacts,
    };
  } catch (error) {
    if (pressSent) {
      // A busy page can take longer than the CDP ack timeout to run its click handlers. Reporting a
      // plain failure would invite a second click on "Add" or "Next".
      return {
        ok: false,
        action: "click",
        mode: "cdp_trusted_click",
        trusted_click: true,
        activation_attempted: true,
        activation_dispatched: true,
        activation_count: 1,
        execution_outcome_unknown: true,
        failure_reason: "click_execution_outcome_unknown",
        error: "Trusted click outcome unknown: " + errorMessage(error),
      };
    }
    return {
      ok: false,
      action: "click",
      mode: "cdp_trusted_click",
      trusted_click: true,
      activation_attempted: activationAttempted,
      activation_dispatched: activationDispatched,
      activation_count: activationAttempted ? 1 : 0,
      error: "Trusted click failed: " + errorMessage(error),
    };
  } finally {
    // A probe that armed the guard and then stopped (a timeout, an error, a refused verdict) never
    // leaves it standing to swallow later clicks. Not while a browser dialog freezes the page.
    if ((allowFinalSubmit || allowGuardedIntermediate) && !guardSettled && !nativeDialogOpenFor(tabId)) {
      try {
        await readDispatchGuard(tabId);
      } catch (_) {}
    }
    if (attached) {
      try {
        releaseDebugger(target);
      } catch (_) {
        /* already detached */
      }
    }
  }
}

function matchTemplateSad(frame, template, step = 2, exclude = null) {
  // Where in the frame the template's pixels are now: sum of absolute RGB differences, searched
  // on a coarse grid and refined around the best cell. Both are {width, height, data RGBA}.
  // exclude = {x, y, r}: skip offsets within r of (x, y) (the best elsewhere, for a runner-up).
  const fw = frame.width;
  const fh = frame.height;
  const tw = template.width;
  const th = template.height;
  if (!fw || !fh || !tw || !th || tw > fw || th > fh) return null;
  const f = frame.data;
  const t = template.data;
  const sample = 2;
  const cost = (ox, oy, limit) => {
    let sum = 0;
    let count = 0;
    for (let y = 0; y < th; y += sample) {
      const fr = ((oy + y) * fw + ox) * 4;
      const tr = y * tw * 4;
      for (let x = 0; x < tw; x += sample) {
        const fi = fr + x * 4;
        const ti = tr + x * 4;
        sum += Math.abs(f[fi] - t[ti]) + Math.abs(f[fi + 1] - t[ti + 1]) + Math.abs(f[fi + 2] - t[ti + 2]);
        count += 3;
        if (sum > limit) return Infinity;
      }
    }
    return sum / Math.max(1, count);
  };
  let best = { x: 0, y: 0, score: Infinity };
  const budget = (currentBest) => (Number.isFinite(currentBest) ? currentBest * ((tw * th) / (sample * sample)) * 3 : Infinity);
  const excluded = (ox, oy) => Boolean(exclude) && Math.abs(ox - exclude.x) < exclude.r && Math.abs(oy - exclude.y) < exclude.r;
  for (let oy = 0; oy <= fh - th; oy += step) {
    for (let ox = 0; ox <= fw - tw; ox += step) {
      if (excluded(ox, oy)) continue;
      const score = cost(ox, oy, budget(best.score));
      if (score < best.score) best = { x: ox, y: oy, score };
    }
  }
  if (!Number.isFinite(best.score)) return best;
  const coarse = { ...best };
  for (let oy = Math.max(0, coarse.y - step); oy <= Math.min(fh - th, coarse.y + step); oy += 1) {
    for (let ox = Math.max(0, coarse.x - step); ox <= Math.min(fw - tw, coarse.x + step); ox += 1) {
      if (excluded(ox, oy)) continue;
      const score = cost(ox, oy, budget(best.score));
      if (score < best.score) best = { x: ox, y: oy, score };
    }
  }
  return best;
}

function poolImage2(image) {
  // A 2x2 box average: compression noise and sub-pixel shifts average out, and matching is 16x cheaper.
  const width = Math.floor(image.width / 2);
  const height = Math.floor(image.height / 2);
  const data = new Uint8ClampedArray(width * height * 4);
  const src = image.data;
  const row = image.width * 4;
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const a = (2 * y * image.width + 2 * x) * 4;
      const o = (y * width + x) * 4;
      for (let c = 0; c < 3; c += 1) {
        data[o + c] = (src[a + c] + src[a + 4 + c] + src[a + row + c] + src[a + row + 4 + c]) >> 2;
      }
      data[o + 3] = 255;
    }
  }
  return { width, height, data };
}

function cropImage(image, x, y, width, height) {
  const data = new Uint8ClampedArray(width * height * 4);
  for (let row = 0; row < height; row += 1) {
    const from = ((y + row) * image.width + x) * 4;
    data.set(image.data.subarray(from, from + width * 4), row * width * 4);
  }
  return { width, height, data };
}

function followMatch(frame, template) {
  // Find the template's pixels in a fresh frame: coarse on pooled copies, refined at full resolution.
  // It is accepted when it matches closely (score <= 38) or clearly beats the best match anywhere else
  // (at most 0.6x the runner-up and no worse than 80), and never when another place is about as good.
  const pooled = template.width >= 16 && template.height >= 16 && frame.width >= 32 && frame.height >= 32;
  const k = pooled ? 2 : 1;
  const pf = pooled ? poolImage2(frame) : frame;
  const pt = pooled ? poolImage2(template) : template;
  const coarse = matchTemplateSad(pf, pt, 2);
  if (!coarse || !Number.isFinite(coarse.score)) return null;
  const runner = matchTemplateSad(pf, pt, 2, { x: coarse.x, y: coarse.y, r: Math.max(pt.width, pt.height) / 2 });
  const runnerScore = runner && Number.isFinite(runner.score) ? runner.score : Infinity;
  // Refine at full resolution within a small window around a pooled position.
  const pad = k + 2;
  const refine = (at) => {
    const x0 = Math.max(0, at.x * k - pad);
    const y0 = Math.max(0, at.y * k - pad);
    const x1 = Math.min(frame.width, at.x * k + template.width + pad);
    const y1 = Math.min(frame.height, at.y * k + template.height + pad);
    const fine = matchTemplateSad(cropImage(frame, x0, y0, x1 - x0, y1 - y0), template, 1);
    return fine && Number.isFinite(fine.score)
      ? { x: x0 + fine.x, y: y0 + fine.y, score: fine.score }
      : { x: at.x * k, y: at.y * k, score: at.score };
  };
  const best = refine(coarse);
  const fineRunner = Number.isFinite(runnerScore) ? refine(runner).score : Infinity;
  // Two exact matches (a runner-up scoring 0) are as ambiguous as it gets: ratio 1, not 0.
  const ratioOf = (winner, second) => (Number.isFinite(second) ? (second > 0 ? winner / second : 1) : 0);
  const coarseRatio = ratioOf(coarse.score, runnerScore);
  // Pooling blurs a small target into its background; judge ambiguity on full-resolution scores.
  const ratio = ratioOf(best.score, fineRunner);
  // Another place about as good as the winner is ambiguity, however close the match: never pick one.
  const distinct = !Number.isFinite(fineRunner) || ratio <= 0.85;
  const accepted = distinct && (best.score <= 38 || (best.score <= 80 && Number.isFinite(fineRunner) && ratio <= 0.6));
  return {
    x: best.x,
    y: best.y,
    score: best.score,
    runner_score: fineRunner,
    ratio,
    coarse_ratio: coarseRatio,
    accepted,
  };
}

async function decodeImageData(base64Png) {
  const binary = atob(String(base64Png || ""));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  const bitmap = await createImageBitmap(new Blob([bytes], { type: "image/png" }));
  const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
  const context = canvas.getContext("2d");
  context.drawImage(bitmap, 0, 0);
  return context.getImageData(0, 0, bitmap.width, bitmap.height);
}

async function resizeImageData(image, width, height) {
  // Bring a capture onto another pixel grid (a device-pixel clip onto screenshot pixels).
  const source = new OffscreenCanvas(image.width, image.height);
  source.getContext("2d").putImageData(image, 0, 0);
  const target = new OffscreenCanvas(width, height);
  const context = target.getContext("2d");
  context.imageSmoothingEnabled = true;
  context.drawImage(source, 0, 0, width, height);
  return context.getImageData(0, 0, width, height);
}

async function imageDataToPngBase64(image) {
  // A debug artifact of what the matcher compared (never model input).
  const data = image instanceof ImageData ? image : new ImageData(new Uint8ClampedArray(image.data), image.width, image.height);
  const canvas = new OffscreenCanvas(data.width, data.height);
  canvas.getContext("2d").putImageData(data, 0, 0);
  const blob = await canvas.convertToBlob({ type: "image/png" });
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(index, index + 0x8000));
  }
  return btoa(binary);
}

async function relocateFollowTarget(tabId, follow) {
  // The model saw the target a few seconds ago; a moving one is elsewhere now. Capture the search
  // region fresh and find the same pixels there (screenshot pixels in, screenshot pixels out).
  const region = Array.isArray(follow && follow.region) ? follow.region.map((value) => Number(value) || 0) : null;
  if (!region || region[2] - region[0] < 8 || region[3] - region[1] < 8) {
    return { ok: false, failure_reason: "follow_region_invalid" };
  }
  const target = { tabId };
  let attached = false;
  try {
    await acquireDebugger(target);
    attached = true;
    // Capture the whole viewport - the same view the observation captured - and cut the search
    // region out of it here: a CDP clip is placed in document coordinates, which a scrolled page
    // shifts away from the screenshot's viewport coordinates.
    const shot = await _cdp(target, "Page.captureScreenshot", { format: "png", fromSurface: true });
    let [full, templ] = await Promise.all([decodeImageData(shot && shot.data), decodeImageData(follow.template)]);
    const captureSize = Array.isArray(follow.capture_size) ? follow.capture_size.map((value) => Number(value) || 0) : [];
    const freshSize = [full.width, full.height];
    const frameScale = captureSize[0] ? full.width / captureSize[0] : 1;
    if (captureSize[0] > 0 && captureSize[1] > 0 && Math.abs(full.width - captureSize[0]) > 2) {
      // Only a different width is a different pixel grid (display scaling): rescale then. A height
      // difference alone is chrome (an infobar) taking or giving back viewport rows - stretching the
      // frame for it would distort every pixel; search the fresh frame as captured instead.
      const height = Math.round(full.height * (captureSize[0] / full.width));
      full = await resizeImageData(full, Math.round(captureSize[0]), height);
    }
    const x0 = Math.max(0, Math.min(full.width - 1, Math.round(region[0])));
    const y0 = Math.max(0, Math.min(full.height - 1, Math.round(region[1])));
    const x1 = Math.max(x0 + 1, Math.min(full.width, Math.round(region[2])));
    const y1 = Math.max(y0 + 1, Math.min(full.height, Math.round(region[3])));
    const cropCanvas = new OffscreenCanvas(full.width, full.height);
    const cropContext = cropCanvas.getContext("2d");
    cropContext.putImageData(full, 0, 0);
    const frame = cropContext.getImageData(x0, y0, x1 - x0, y1 - y0);
    const scale = 1;
    region[0] = x0;
    region[1] = y0;
    const match = followMatch(frame, templ);
    const diagnostics = {
      follow_capture_size: freshSize,
      follow_template_size: [templ.width, templ.height],
      follow_region: [x0, y0, x1, y1],
      follow_frame_scale: Math.round(frameScale * 100) / 100,
      follow_coarse_ratio: match && Number.isFinite(match.coarse_ratio) ? Math.round(match.coarse_ratio * 100) / 100 : null,
    };
    if (!match || !match.accepted) {
      let debugFrame = "";
      let debugTemplate = "";
      try {
        [debugFrame, debugTemplate] = await Promise.all([imageDataToPngBase64(frame), imageDataToPngBase64(templ)]);
      } catch (_) {}
      return {
        ok: false,
        failure_reason: "follow_target_not_found",
        follow_score: match && Number.isFinite(match.score) ? Math.round(match.score * 10) / 10 : null,
        follow_ratio: match ? Math.round(match.ratio * 100) / 100 : null,
        ...diagnostics,
        follow_debug_frame: debugFrame,
        follow_debug_template: debugTemplate,
      };
    }
    const center = Array.isArray(follow.template_center) ? follow.template_center : [templ.width / 2, templ.height / 2];
    return {
      ok: true,
      img_x: Math.round(region[0] + (match.x + Number(center[0] || 0)) * scale),
      img_y: Math.round(region[1] + (match.y + Number(center[1] || 0)) * scale),
      follow_score: Math.round(match.score * 10) / 10,
      follow_ratio: Math.round(match.ratio * 100) / 100,
      ...diagnostics,
    };
  } catch (error) {
    return { ok: false, failure_reason: "follow_capture_failed", error: errorMessage(error) };
  } finally {
    if (attached) {
      try { releaseDebugger(target); } catch (_) {}
    }
  }
}

function findLabeledClickableInPage(wanted) {
  // Enumeration only: which visible, enabled buttons/links carry exactly this label right now.
  try {
    // Words must match exactly; decorative glyphs around them (a caret, a check, a close mark) are not
    // part of the label. A label that is only glyphs keeps them.
    const norm = (value) => {
      const spaced = String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
      const bare = spaced.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "").trim();
      return bare || spaced;
    };
    const target = norm(wanted);
    if (!target) return { ok: false, failure_reason: "target_text_missing" };
    const nodes = Array.from(document.querySelectorAll(
      'button, [role="button"], a[href], input[type="button"], input[type="submit"], summary, '
      + '[role="option"], [role="menuitem"], [role="menuitemcheckbox"], [role="menuitemradio"], [role="tab"], [role="treeitem"], [role="link"]',
    ));
    const rendered = (el) => {
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return false;
      const style = window.getComputedStyle(el);
      return !(style.visibility === "hidden" || style.display === "none" || Number(style.opacity || 1) === 0);
    };
    // On screen means inside the viewport AND inside every ancestor that clips its overflow: an option
    // scrolled out of its own list's window is not on screen, whatever its box says.
    const inViewport = (el) => {
      const rect = el.getBoundingClientRect();
      if (!(rect.bottom > 0 && rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth)) return false;
      for (let node = el.parentElement; node && node !== document.body && node !== document.documentElement; node = node.parentElement) {
        let clip = null;
        try { clip = window.getComputedStyle(node); } catch (_) { clip = null; }
        if (!clip) continue;
        if (![clip.overflowX, clip.overflowY].some((value) => ["hidden", "scroll", "auto", "clip"].includes(value))) continue;
        const frame = node.getBoundingClientRect();
        if (frame.width <= 0 || frame.height <= 0) continue;
        if (rect.bottom <= frame.top || rect.top >= frame.bottom || rect.right <= frame.left || rect.left >= frame.right) return false;
      }
      return true;
    };
    const enabled = (el) => !el.disabled && String(el.getAttribute("aria-disabled") || "").toLowerCase() !== "true";
    const label = (el) => norm(
      String(el.tagName || "").toLowerCase() === "input"
        ? (el.value || el.getAttribute("aria-label"))
        : (el.innerText || el.textContent || el.getAttribute("aria-label")),
    );
    const matching = nodes.filter((el) => rendered(el) && enabled(el) && label(el) === target);
    const onScreen = matching.filter(inViewport);
    let hits = onScreen;
    let scrolledIntoView = false;
    if (!onScreen.length && matching.length > 1) {
      // Several matches, none on screen: which one is meant is the brain's call, not the body's.
      return { ok: false, failure_reason: "target_text_ambiguous", candidates: matching.length, off_screen_candidates: matching.length };
    }
    if (!onScreen.length && matching.length === 1) {
      // The only match is off screen: bring it into view (nearest) and use it where it lands.
      const rect = matching[0].getBoundingClientRect();
      const side = rect.bottom <= 0 ? "above" : rect.top >= window.innerHeight ? "below" : rect.right <= 0 ? "left" : "right";
      const distance = Math.round(side === "above" ? -rect.bottom : side === "below" ? rect.top - window.innerHeight : side === "left" ? -rect.right : rect.left - window.innerWidth);
      try { matching[0].scrollIntoView({ block: "nearest", inline: "nearest", behavior: "instant" }); } catch (_) {}
      if (!inViewport(matching[0])) {
        return { ok: false, failure_reason: "target_text_not_visible", off_screen_side: side, off_screen_px: Math.abs(distance) };
      }
      hits = [matching[0]];
      scrolledIntoView = true;
    }
    if (!hits.length) return { ok: false, failure_reason: "target_text_not_found" };
    if (hits.length > 1) return { ok: false, failure_reason: "target_text_ambiguous", candidates: hits.length };
    const rect = hits[0].getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    // CSS px -> screenshot px (the probe converts back with the same ratio).
    return {
      ok: true,
      img_x: Math.round((rect.left + rect.width / 2) * dpr),
      img_y: Math.round((rect.top + rect.height / 2) * dpr),
      ...(scrolledIntoView ? { scrolled_into_view: true } : {}),
    };
  } catch (error) {
    return { ok: false, failure_reason: "target_text_probe_failed", error: String(error) };
  }
}

async function resolveTargetTextClick(tabId, wanted, waitMs = 0) {
  // Find the labeled control at dispatch and take the live identity at its centre as the one to click:
  // the ordinary identity-bound click path then guards it like any observed control.
  const failed = (reason, extra = {}) => ({
    ok: false,
    action: "click",
    mode: "target_text",
    failure_reason: reason,
    activation_attempted: false,
    activation_dispatched: false,
    activation_count: 0,
    ...extra,
  });
  // Actionability auto-wait: a control that appears only when its moment arrives is pressed as soon as
  // it exists. Polling only reads the page (no input), so it never breaks a "stay still" hold.
  const budget = Math.max(0, Math.min(30000, Math.round(Number(waitMs) || 0)));
  const started = Date.now();
  const epoch = commandSocketEpoch;
  let found = null;
  for (;;) {
    if (commandSocketEpoch !== epoch) return failed("command_connection_lost");
    try {
      const rows = await withTimeout(
        chrome.scripting.executeScript({ target: { tabId }, func: findLabeledClickableInPage, args: [wanted] }),
        2500,
        "target text probe timed out",
      );
      found = rows && rows[0] && rows[0].result;
    } catch (error) {
      return failed("target_text_probe_failed", { error: errorMessage(error) });
    }
    if ((found && found.ok) || (found && found.failure_reason === "target_text_ambiguous")) break;
    if (Date.now() - started >= budget) break;
    await new Promise((resolve) => setTimeout(resolve, 60));
  }
  const waitedForTargetMs = Date.now() - started;
  if (!found || !found.ok) {
    return failed((found && found.failure_reason) || "target_text_not_found", {
      target_text_candidates: (found && found.candidates) || 0,
      waited_for_target_ms: waitedForTargetMs,
      ...(found && found.off_screen_side ? { target_text_off_screen_side: found.off_screen_side, target_text_off_screen_px: found.off_screen_px } : {}),
      ...(found && found.off_screen_candidates ? { target_text_off_screen_candidates: found.off_screen_candidates } : {}),
    });
  }
  let point = null;
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: visionPointInfoInPage, args: [found.img_x, found.img_y, false, false, null] }),
      2500,
      "target text point probe timed out",
    );
    point = rows && rows[0] && rows[0].result;
  } catch (error) {
    return failed("target_text_probe_failed", { error: errorMessage(error) });
  }
  // The same word-exact comparison findLabeledClickableInPage made: edge glyphs are not part of the label.
  const norm = (value) => {
    const spaced = String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
    const bare = spaced.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "").trim();
    return bare || spaced;
  };
  if (!point || !point.ok) {
    return failed(point && point.blocked_submit ? "target_text_final_submit" : "target_text_point_unavailable", { label: point && point.label });
  }
  if (norm(point.target_text) !== norm(wanted)) {
    // Something else is drawn over the labeled control's centre.
    return failed("target_text_covered", { live_target_text: String(point.target_text || "").slice(0, 80) });
  }
  return {
    ok: true,
    img_x: found.img_x,
    img_y: found.img_y,
    waited_for_target_ms: waitedForTargetMs,
    ...(found.scrolled_into_view ? { target_text_scrolled_into_view: true } : {}),
    identity: {
      target_selector: point.target_selector || "",
      target_text: point.target_text || "",
      target_tag: point.target_tag || "",
      target_type: point.target_type || "",
      target_role: point.target_role || "",
      dispatch_path: point.dispatch_path || "",
      page_url: point.page_url || "",
    },
  };
}

async function runVisionClick(
  tabId,
  imgX,
  imgY,
  allowFinalSubmit = false,
  allowGuardedIntermediate = false,
  expectedTarget = null,
  expectedPageContext = null,
  pointer = null,
) {
  if (!tabId) return { ok: false, error: "No application tab." };
  if (pointer && pointer.target_text && !allowFinalSubmit && !allowGuardedIntermediate) {
    // A control named by its visible label (it appeared after the model's screenshot).
    const epoch = commandSocketEpoch;
    const resolved = await resolveTargetTextClick(tabId, pointer.target_text, pointer.target_wait_ms || 0);
    if (!resolved.ok) return resolved;
    if (commandSocketEpoch !== epoch) {
      // The control appeared after the server lost this command with its socket: never click for it.
      return { ok: false, action: "click", mode: "target_text", failure_reason: "command_connection_lost", activation_attempted: false, activation_dispatched: false, activation_count: 0 };
    }
    const result = await runVisionClickAt(
      tabId,
      resolved.img_x,
      resolved.img_y,
      false,
      false,
      resolved.identity,
      expectedPageContext,
      { ...pointer, follow: null, target_text: "" },
    );
    return result && typeof result === "object"
      ? {
        ...result,
        target_text_resolved: true,
        ...(resolved.target_text_scrolled_into_view ? { target_text_scrolled_into_view: true, scrolled_into_view: true } : {}),
        resolved_point: [resolved.img_x, resolved.img_y],
        waited_for_target_ms: resolved.waited_for_target_ms,
      }
      : result;
  }
  let followed = null;
  if (pointer && pointer.follow && pointer.follow.template && !allowFinalSubmit && !allowGuardedIntermediate) {
    // A target that moves on its own: click it where it is at dispatch, not where it was seen.
    followed = await relocateFollowTarget(tabId, pointer.follow);
    if (!followed.ok) {
      return {
        ok: false,
        action: "click",
        mode: "follow",
        failure_reason: followed.failure_reason || "follow_target_not_found",
        follow_score: followed.follow_score == null ? null : followed.follow_score,
        follow_frame_scale: followed.follow_frame_scale == null ? null : followed.follow_frame_scale,
        follow_ratio: followed.follow_ratio == null ? null : followed.follow_ratio,
        follow_coarse_ratio: followed.follow_coarse_ratio == null ? null : followed.follow_coarse_ratio,
        follow_capture_size: followed.follow_capture_size || null,
        follow_template_size: followed.follow_template_size || null,
        follow_region: followed.follow_region || null,
        follow_debug_frame: followed.follow_debug_frame || "",
        follow_debug_template: followed.follow_debug_template || "",
        activation_attempted: false,
        activation_dispatched: false,
        activation_count: 0,
      };
    }
    imgX = followed.img_x;
    imgY = followed.img_y;
  }
  const result = await runVisionClickAt(
    tabId,
    imgX,
    imgY,
    allowFinalSubmit,
    allowGuardedIntermediate,
    expectedTarget,
    expectedPageContext,
    // A followed target was just found in a fresh capture: that match is its pixel identity, and
    // comparing against the observation's pixels (where it no longer is) would always refuse it.
    followed ? { ...pointer, followed_fresh: true } : pointer,
  );
  if (!followed || !result || typeof result !== "object") return result;
  // Where the followed target was found, alongside what the click did there.
  return {
    ...result,
    follow_score: followed.follow_score,
    follow_ratio: followed.follow_ratio,
    follow_coarse_ratio: followed.follow_coarse_ratio,
    follow_capture_size: followed.follow_capture_size || null,
    follow_region: followed.follow_region || null,
    followed_to: [followed.img_x, followed.img_y],
  };
}

async function runVisionClickAt(
  tabId,
  imgX,
  imgY,
  allowFinalSubmit = false,
  allowGuardedIntermediate = false,
  expectedTarget = null,
  expectedPageContext = null,
  pointer = null,
) {
  if (allowFinalSubmit && allowGuardedIntermediate) {
    return { ok: false, action: "click", failure_reason: "conflicting_click_authority" };
  }
  if (expectedTarget && !clickTargetIdentityComplete(expectedTarget)) {
    return {
      ok: false,
      action: "click",
      mode: "click_preflight",
      failure_reason: allowFinalSubmit ? "final_submit_identity_required" : "control_identity_required",
      activation_attempted: false,
      activation_dispatched: false,
      activation_count: 0,
    };
  }
  if (allowFinalSubmit && !clickTargetIdentityComplete(expectedTarget)) {
    return {
      ok: false,
      action: "click",
      mode: "cdp_trusted_click",
      failure_reason: "final_submit_identity_required",
      activation_attempted: false,
      activation_dispatched: false,
      activation_count: 0,
    };
  }
  const strictClick = Boolean(allowFinalSubmit || allowGuardedIntermediate);
  const pageVerdict = await observedPageContextVerdict(
    tabId,
    expectedPageContext,
    strictClick,
    Boolean(allowGuardedIntermediate && !allowFinalSubmit),
    strictClick,
  );
  // Proof that every control still shows the same label, value, and state lets the adjacent trusted
  // probes follow a re-rendered target to its one page-wide exact-identity twin.
  const strictTwinRebind = Boolean(strictClick && pageVerdict.ok && pageVerdict.semantic_state_verified);
  if (!pageVerdict.ok) {
    return {
      ok: false,
      action: "click",
      mode: "page_context_revalidation",
      failure_reason: pageVerdict.reason,
      page_context_mismatch_field: pageVerdict.mismatch_field || "",
      activation_attempted: false,
      activation_dispatched: false,
      activation_count: 0,
    };
  }
  let coordinatePixelEvidence = {};
  // Pixel equality is the identity gate for a truly coordinate-only surface. A selector-backed
  // final action already has stronger live bindings: exact URL + stable/document state, followed
  // by selector re-resolution and a complete target-identity comparison immediately before the
  // single trusted activation. Requiring a byte-identical viewport as well makes harmless cursor,
  // focus, animation, or paint changes defeat an otherwise exact DOM target.
  // A canvas/SVG target's DOM identity says nothing about which painted button sits at the point (canvases
  // are listed as controls now), so a coordinate-surface click keeps the pixel gate even when bound.
  const coordinateSurfaceTarget = Boolean(expectedTarget && expectedTarget.dispatch_path === "cdp_trusted_click");
  if ((!expectedTarget || coordinateSurfaceTarget) && !(pointer && pointer.followed_fresh)) {
    const pixelVerdict = await withScreenshotCaptureLock(
      () => captureMaskedViewportFingerprint(tabId, expectedPageContext),
    );
    coordinatePixelEvidence = {
      coordinate_pixel_capture_attempts: Number(pixelVerdict.capture_attempts || 0),
      coordinate_pixel_temporal_retry_attempted: Boolean(
        pixelVerdict.temporal_exact_hash_retry_attempted,
      ),
      coordinate_pixel_temporal_exact_hash_recovered: Boolean(
        pixelVerdict.temporal_exact_hash_recovered,
      ),
    };
    if (!pixelVerdict.ok) {
      return {
        ok: false,
        action: "click",
        mode: "coordinate_pixel_revalidation",
        failure_reason: pixelVerdict.reason,
        expected_screenshot_sha256: pixelVerdict.expected_screenshot_sha256 || "",
        live_screenshot_sha256: pixelVerdict.live_screenshot_sha256 || "",
        error: pixelVerdict.error || "",
        ...coordinatePixelEvidence,
        activation_attempted: false,
        activation_dispatched: false,
        activation_count: 0,
      };
    }
  }
  const beforeState = await tabPageEvidence(tabId);
  const clickLineage = await captureTabLineage(tabId);
  // An irreversible final action gets exactly one browser activation. The trusted path performs
  // its own live structural submit probe before dispatching one CDP mouse sequence. Never perform
  // the normal DOM activation followed by a trusted fallback under one approval.
  if (allowFinalSubmit) {
    const trusted = await runTrustedVisionClick(tabId, imgX, imgY, true, false, expectedTarget, null, strictTwinRebind);
    if (!trusted.ok || trusted.blocked_submit || trusted.blocked_file_dialog || trusted.native_dialog) {
      return {
        action: "click",
        mode: trusted.mode || "cdp_trusted_click",
        trusted_click_attempted: true,
        trusted_click_ok: Boolean(trusted.ok),
        before_url: beforeState.url || "",
        before_title: beforeState.title || "",
        before_surface_signature: beforeState.surface_signature || "",
        ...coordinatePixelEvidence,
        ...trusted,
      };
    }
    const adopted = await adoptNewTabAfterClick(tabId, clickLineage);
    const afterTabId = adopted.tabId || tabId;
    const afterState = await tabPageEvidence(afterTabId);
    let trustedOutcome = {};
    try {
      const outcomeRows = await withTimeout(
        chrome.scripting.executeScript({ target: { tabId: afterTabId }, func: clickOutcomeEvidenceInPage, args: [trusted] }),
        2500,
        "trusted final click outcome evidence timed out",
      );
      trustedOutcome = (outcomeRows && outcomeRows[0] && outcomeRows[0].result) || {};
    } catch (_) {
      trustedOutcome = {};
    }
    return {
      ok: true,
      action: "click",
      mode: "cdp_trusted_click",
      trusted_click_attempted: true,
      trusted_click_ok: true,
      ...trusted,
      ...trustedOutcome,
      url_changed: Boolean(beforeState.url && afterState.url && beforeState.url !== afterState.url && String(beforeState.url).split("#")[0] !== String(afterState.url).split("#")[0]),
      title_changed: Boolean(beforeState.title && afterState.title && beforeState.title !== afterState.title),
      surface_changed: Boolean(beforeState.surface_signature && afterState.surface_signature && beforeState.surface_signature !== afterState.surface_signature),
      before_url: beforeState.url || "",
      after_url: afterState.url || "",
      before_title: beforeState.title || "",
      after_title: afterState.title || "",
      before_surface_signature: beforeState.surface_signature || "",
      after_surface_signature: afterState.surface_signature || "",
      ...coordinatePixelEvidence,
      adopted_new_tab: Boolean(adopted.adopted),
      adopted_tab_url: adopted.adopted_url || "",
    };
  }
  let preflight = {};
  try {
    const preflightRows = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId },
        func: visionClickAtPointInPage,
        args: [imgX, imgY, false, Boolean(allowGuardedIntermediate), expectedTarget],
      }),
      2500,
      "click dispatch preflight timed out",
    );
    preflight = {
      ...(
        (preflightRows && preflightRows[0] && preflightRows[0].result)
        || { ok: false, action: "click", error: "click dispatch preflight returned nothing" }
      ),
      ...coordinatePixelEvidence,
    };
    if (!preflight.ok || preflight.blocked_submit || preflight.blocked_file_dialog) {
      return {
        ...preflight,
        action: "click",
        activation_attempted: false,
        activation_dispatched: false,
        activation_count: 0,
      };
    }
    if (pointer && pointer.refuse_value_control && preflight.target_value_control) {
      // A chained point click has no observed target: what is actually hit decides, before any dispatch.
      return {
        ...preflight,
        ok: false,
        action: "click",
        mode: "click_preflight",
        failure_reason: "value_control_needs_grounding",
        activation_attempted: false,
        activation_dispatched: false,
        activation_count: 0,
      };
    }
    const preflightIdentity = clickTargetIdentityFromPoint(preflight);
    const bindableIdentity = clickPointIdentityComplete(preflightIdentity)
      ? clickTargetIdentityVerdict(preflightIdentity, preflightIdentity)
      : { ok: false, reason: "click_target_identity_unavailable", expected: preflightIdentity, live: preflightIdentity };
    if (!bindableIdentity.ok) {
      return {
        ...preflight,
        ok: false,
        action: "click",
        mode: "click_preflight",
        failure_reason: bindableIdentity.reason,
        target_identity_matched: false,
        preflight_target_selector: (bindableIdentity.expected || {}).target_selector || "",
        live_target_selector: (bindableIdentity.live || {}).target_selector || "",
        activation_attempted: false,
        activation_dispatched: false,
        activation_count: 0,
      };
    }
    // Execute the model's exact, observation-bound point as a real browser mouse gesture.
    // Programmatic HTMLElement.click()/synthetic events are not equivalent to a user click:
    // some reactive controls intentionally ignore untrusted events. Safety comes from the
    // adjacent URL, target-identity, file-dialog, and final-action preflights, and there is
    // exactly one activation so an inconclusive read-back can never cause a second click.
    const trusted = await runTrustedVisionClick(
      tabId,
      imgX,
      imgY,
      false,
      Boolean(allowGuardedIntermediate),
      expectedTarget || preflightIdentity,
      pointer,
      strictTwinRebind,
    );
    if (!trusted.ok || trusted.blocked_submit || trusted.blocked_file_dialog || trusted.native_dialog) {
      return {
        ...preflight,
        ...trusted,
        action: "click",
        mode: trusted.mode || "cdp_trusted_click",
        dispatch_path_selected: "cdp_trusted_click",
        trusted_click_attempted: true,
        trusted_click_ok: Boolean(trusted.ok),
        before_url: beforeState.url || "",
        before_title: beforeState.title || "",
        before_surface_signature: beforeState.surface_signature || "",
      };
    }
    const adopted = await adoptNewTabAfterClick(tabId, clickLineage);
    const afterTabId = adopted.tabId || tabId;
    const afterState = await tabPageEvidence(afterTabId);
    let trustedOutcome = {};
    try {
      const outcomeRows = await withTimeout(
        chrome.scripting.executeScript({
          target: { tabId: afterTabId },
          func: clickOutcomeEvidenceInPage,
          args: [preflight],
        }),
        2500,
        "trusted click outcome evidence timed out",
      );
      trustedOutcome = (outcomeRows && outcomeRows[0] && outcomeRows[0].result) || {};
    } catch (_) {
      trustedOutcome = {};
    }
    const urlChanged = Boolean(beforeState.url && afterState.url && beforeState.url !== afterState.url);
    const titleChanged = Boolean(beforeState.title && afterState.title && beforeState.title !== afterState.title);
    const surfaceChanged = Boolean(
      beforeState.surface_signature
      && afterState.surface_signature
      && beforeState.surface_signature !== afterState.surface_signature
    );
    const documentLifecycleChanged = Boolean(
      beforeState.document_lifecycle_token
      && afterState.document_lifecycle_token
      && beforeState.document_lifecycle_token !== afterState.document_lifecycle_token
    );
    const verificationObserved = Boolean(
      urlChanged
      || titleChanged
      || surfaceChanged
      || documentLifecycleChanged
      || trustedOutcome.selection_committed
      || trustedOutcome.target_state_changed
      || trustedOutcome.linked_value_changed
      || trustedOutcome.active_typable
    );
    return {
      ...preflight,
      ...trusted,
      ...trustedOutcome,
      ok: true,
      action: "click",
      mode: "cdp_trusted_click",
      dispatch_path_selected: "cdp_trusted_click",
      trusted_click_attempted: true,
      trusted_click_ok: true,
      url_changed: urlChanged,
      title_changed: titleChanged,
      surface_changed: surfaceChanged,
      document_lifecycle_changed: documentLifecycleChanged,
      before_url: beforeState.url || "",
      after_url: afterState.url || "",
      before_title: beforeState.title || "",
      after_title: afterState.title || "",
      before_surface_signature: beforeState.surface_signature || "",
      after_surface_signature: afterState.surface_signature || "",
      after_document_state_signature: afterState.document_state_signature || "",
      after_document_lifecycle_token: afterState.document_lifecycle_token || "",
      adopted_new_tab: Boolean(adopted.adopted),
      adopted_tab_url: adopted.adopted_url || "",
      verification_observed: verificationObserved,
      verification_state: verificationObserved ? "observed_effect" : "inconclusive",
      visual_verification_required: !verificationObserved,
    };
  } catch (error) {
    return {
      ok: false,
      action: "click",
      mode: String(preflight.dispatch_path || "click_preflight"),
      error: "Click failed: " + errorMessage(error),
      ...preflight,
    };
  }
}

// The one visible control that still carries the observed identity (tag, type, role, and one of
// its observed names), wherever it is now: a roving button or a re-rendered list moves the
// control the model chose. Exact identity only; zero or several twins means no rebind.
// strict (final and guarded clicks): only when the approved selector matches nothing anywhere in the
// document (a hidden or moved approved node is never swapped for a lookalike), by the observed visible
// text only, never an alternate name, and the twin must be the only rendered node with that identity
// anywhere in the page, enabled or not.
function findIdentityTwinInPage(expectedTarget, strict = false) {
  try {
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const expected = expectedTarget && typeof expectedTarget === "object" ? expectedTarget : {};
    const tag = clean(expected.target_tag).toLowerCase();
    const rawType = clean(expected.target_type).toLowerCase();
    const type = rawType === tag ? "" : rawType;
    const role = clean(expected.target_role).toLowerCase();
    const names = [
      clean(expected.target_text),
      ...(!strict && Array.isArray(expected.target_names) ? expected.target_names.map(clean) : []),
    ].filter(Boolean);
    if (!tag || !names.length) return { ok: false, reason: "identity_incomplete" };
    const queryAll = typeof globalThis.__lumenDeepQueryAll === "function"
      ? globalThis.__lumenDeepQueryAll
      : (selector) => Array.from(document.querySelectorAll(selector));
    // The page-evidence probe installs the composed selector builder; before it ran, a unique id is
    // the only address used.
    const stable = typeof globalThis.__lumenStableDeepSelector === "function"
      ? globalThis.__lumenStableDeepSelector
      : (node) => {
        try {
          const id = node && node.getAttribute && node.getAttribute("id");
          if (!id) return "";
          const selector = `#${(window.CSS && typeof CSS.escape === "function") ? CSS.escape(id) : id}`;
          return document.querySelectorAll(selector).length === 1 ? selector : "";
        } catch (_) {
          return "";
        }
      };
    const rectOf = (node) => (
      typeof globalThis.__lumenTopRect === "function" ? globalThis.__lumenTopRect(node) : node.getBoundingClientRect()
    );
    const sameIdentity = (node) => {
      if (!node) return false;
      if (clean(node.getAttribute && node.getAttribute("type")).toLowerCase() !== type) return false;
      if (clean(node.getAttribute && node.getAttribute("role")).toLowerCase() !== role) return false;
      const text = clean(
        node.innerText || node.textContent || (node.getAttribute && node.getAttribute("aria-label")) || "",
      ).slice(0, 180).trim();
      return names.includes(text);
    };
    const rendered = (node) => {
      const rect = rectOf(node);
      if (!(rect.width > 0 && rect.height > 0)) return false;
      try {
        const style = window.getComputedStyle(node);
        return style.visibility !== "hidden" && style.display !== "none";
      } catch (_) {
        return true;
      }
    };
    if (strict) {
      const approvedSelector = String(expected.target_selector || "").trim();
      if (!approvedSelector) return { ok: false, reason: "identity_twin_unverifiable" };
      try {
        if (queryAll(approvedSelector).length) return { ok: false, reason: "approved_target_still_present" };
      } catch (_) {
        return { ok: false, reason: "identity_twin_unverifiable" };
      }
      const sameNamed = queryAll(tag).filter((node) => sameIdentity(node));
      const everywhere = sameNamed.filter((node) => rendered(node));
      const disabled = (node) => Boolean(
        node.disabled || clean(node.getAttribute && node.getAttribute("aria-disabled")).toLowerCase() === "true",
      );
      if (sameNamed.length !== 1 || everywhere.length !== 1 || disabled(everywhere[0])) {
        return {
          ok: false,
          reason: sameNamed.length > 1 ? "identity_twin_ambiguous" : (everywhere.length ? "identity_twin_disabled" : "identity_twin_absent"),
          count: sameNamed.length,
        };
      }
      const selector = stable(everywhere[0]);
      return selector ? { ok: true, selector } : { ok: false, reason: "identity_twin_unaddressable" };
    }
    const twins = queryAll(tag).filter((node) => {
      if (!node || node.disabled) return false;
      if (!sameIdentity(node)) return false;
      const rect = rectOf(node);
      return rect.width > 0 && rect.height > 0
        && rect.bottom >= 0 && rect.top <= window.innerHeight
        && rect.right >= 0 && rect.left <= window.innerWidth;
    });
    if (twins.length !== 1) {
      return { ok: false, reason: twins.length ? "identity_twin_ambiguous" : "identity_twin_absent", count: twins.length };
    }
    const selector = stable(twins[0]);
    return selector ? { ok: true, selector } : { ok: false, reason: "identity_twin_unaddressable" };
  } catch (_) {
    return { ok: false, reason: "identity_twin_probe_failed" };
  }
}

async function runVisionClickControl(
  tabId,
  selector,
  fallbackX,
  fallbackY,
  allowFinalSubmit = false,
  allowGuardedIntermediate = false,
  expectedTarget = null,
  expectedPageContext = null,
  pointer = null,
) {
  const rebindDeadline = Date.now() + 6000; // retries stop well inside the server's command timeout
  if (!tabId) return { ok: false, action: "click", mode: "control_selector", error: "No application tab." };
  if (!clickTargetIdentityComplete(expectedTarget)) {
    return {
      ok: false,
      action: "click",
      mode: "control_selector",
      failure_reason: allowFinalSubmit ? "final_submit_identity_required" : "control_identity_required",
      activation_attempted: false,
      activation_dispatched: false,
      activation_count: 0,
    };
  }
  const ordinaryReversible = !allowFinalSubmit && !allowGuardedIntermediate;
  const surfaceOffset = pointer && Array.isArray(pointer.surface_offset) ? pointer.surface_offset : null;
  // Guarded intermediates (a "Submit peak", an "Apply filter") need the field values they carry to be
  // unchanged; final submits keep the exact surface, or a surface whose every control still shows the
  // same label, value, and state (a page that re-renders itself with fresh ids).
  const pageVerdict = await observedPageContextVerdict(
    tabId,
    expectedPageContext,
    !ordinaryReversible,
    allowGuardedIntermediate && !allowFinalSubmit,
    !ordinaryReversible,
  );
  // A final or guarded click may follow its target to the one page-wide node with its exact identity
  // only while that semantic page state is proven unchanged.
  const strictTwinRebind = Boolean(!ordinaryReversible && pageVerdict.ok && pageVerdict.semantic_state_verified);
  // The control the model chose moved (its old node now shows something else, or a re-render gave
  // it a fresh id): follow it to the one visible node that still has its exact identity.
  const identityTwinRebind = async () => {
    try {
      const twinRows = await withTimeout(
        chrome.scripting.executeScript({
          target: { tabId },
          func: findIdentityTwinInPage,
          args: [expectedTarget, !ordinaryReversible],
        }),
        2000,
        "identity twin probe timed out",
      );
      const twin = (twinRows && twinRows[0] && twinRows[0].result) || {};
      if (!(twin.ok && twin.selector)) return null;
      const reboundRows = await withTimeout(
        chrome.scripting.executeScript({ target: { tabId }, func: visionControlCenterInPage, args: [String(twin.selector), true, false] }),
        2500,
        "rebound control point probe timed out",
      );
      const rebound = (reboundRows && reboundRows[0] && reboundRows[0].result) || {};
      const reboundTarget = { ...(expectedTarget || {}), target_selector: String(twin.selector) };
      const reboundVerdict = rebound.ok
        ? clickTargetIdentityVerdict(reboundTarget, rebound, !ordinaryReversible)
        : { ok: false };
      return reboundVerdict.ok
        ? { selector: String(twin.selector), expectedTarget: reboundTarget, center: rebound, identity: reboundVerdict }
        : null;
    } catch (_) {
      return null;
    }
  };
  const expectedContextUrl = String(
    pageVerdict && pageVerdict.expected && pageVerdict.expected.url || "",
  ).trim();
  const liveContextUrl = String(
    pageVerdict && pageVerdict.live && pageVerdict.live.url || "",
  ).trim();
  const expectedSurfaceSignature = String(
    pageVerdict && pageVerdict.expected && pageVerdict.expected.surface_signature || "",
  ).trim();
  const liveSurfaceSignature = String(
    pageVerdict && pageVerdict.live && pageVerdict.live.surface_signature || "",
  ).trim();
  const reversibleSurfaceDrift = Boolean(
    ordinaryReversible
    && !pageVerdict.ok
    && pageVerdict.reason === "page_context_drift"
    && pageVerdict.mismatch_field === "surface_signature"
    && expectedContextUrl
    && liveContextUrl === expectedContextUrl
    && expectedSurfaceSignature
    && liveSurfaceSignature,
  );
  if (!pageVerdict.ok && !reversibleSurfaceDrift) {
    return {
      ok: false,
      action: "click",
      mode: "control_selector",
      failure_reason: allowFinalSubmit ? "final_submit_page_context_drift" : pageVerdict.reason,
      page_context_mismatch_field: pageVerdict.mismatch_field || "",
      activation_attempted: false,
      activation_dispatched: false,
      activation_count: 0,
    };
  }
  let center = {};
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: visionControlCenterInPage, args: [String(selector || ""), true, false, surfaceOffset] }),
      2500,
      "control selector point probe timed out",
    );
    center = (rows && rows[0] && rows[0].result) || { ok: false, action: "click", failure_reason: "control_not_found" };
  } catch (error) {
    center = { ok: false, action: "click", failure_reason: "control_not_found", error: errorMessage(error) };
  }
  const scrollInfo = {
    scrolled_into_view: Boolean(center && center.scrolled_into_view),
    scrolled_within_list: Boolean(center && center.scrolled_within_list),
    scroll_delta_img: Array.isArray(center && center.scroll_delta_img) ? center.scroll_delta_img : [0, 0],
  };
  if (scrollInfo.scrolled_into_view && pointer && pointer.follow && Array.isArray(pointer.follow.capture_size)) {
    // The page moved to bring the control into view: search the whole fresh frame for the target.
    const size = pointer.follow.capture_size.map((value) => Number(value) || 0);
    pointer = { ...pointer, follow: { ...pointer.follow, region: [0, 0, size[0], size[1]] } };
  }
  if (!center.ok && strictTwinRebind) {
    // The approved selector no longer resolves (a re-render regenerated ids).
    const twinned = await identityTwinRebind();
    if (twinned) {
      selector = twinned.selector;
      expectedTarget = twinned.expectedTarget;
      center = twinned.center;
    }
  }
  if (center.ok) {
    let initialIdentity = clickTargetIdentityVerdict(expectedTarget, center);
    if (
      !initialIdentity.ok
      && (ordinaryReversible || strictTwinRebind)
      && ["target_text", "target_selector"].includes(initialIdentity.mismatch_field)
    ) {
      const twinned = await identityTwinRebind();
      if (twinned) {
        selector = twinned.selector;
        expectedTarget = twinned.expectedTarget;
        center = twinned.center;
        initialIdentity = twinned.identity;
      }
    }
    if (!initialIdentity.ok) {
      return {
        ok: false,
        action: "click",
        mode: "control_selector",
        failure_reason: initialIdentity.reason,
        target_identity_mismatch_field: initialIdentity.mismatch_field || "",
        activation_attempted: false,
        activation_dispatched: false,
        activation_count: 0,
      };
    }
    await new Promise((resolve) => setTimeout(resolve, 80));
    // scrollIntoView can settle after the injected probe returns, and reactive
    // popups may move or remove an option while that happens. Re-resolve the
    // selector after layout settles and require its own current hit-test point.
    // A once-resolved selector never falls back to stale model coordinates.
    try {
      const settledProbe = async () => {
        const rows = await withTimeout(
          chrome.scripting.executeScript({ target: { tabId }, func: visionControlCenterInPage, args: [String(selector || ""), false, true, surfaceOffset] }),
          2500,
          "settled control selector point probe timed out",
        );
        return (rows && rows[0] && rows[0].result) || { ok: false, action: "click", failure_reason: "control_not_found" };
      };
      let settled = await settledProbe();
      if (strictTwinRebind && (!settled.ok || !clickTargetIdentityVerdict(expectedTarget, settled, true).ok)) {
        // Re-rendered again while layout settled: follow it once more under the same proof.
        const twinned = await identityTwinRebind();
        if (twinned) {
          selector = twinned.selector;
          expectedTarget = twinned.expectedTarget;
          settled = await settledProbe();
        }
      }
      if (!settled.ok) {
        return {
          ok: false,
          action: "click",
          mode: "control_selector",
          failure_reason: settled.failure_reason || "control_not_interactable",
          error: settled.error || "",
          control_selector_resolved: Boolean(settled.control_selector_resolved),
          selector_was_resolved: true,
        };
      }
      const settledIdentity = clickTargetIdentityVerdict(expectedTarget, settled);
      if (!settledIdentity.ok) {
        return {
          ok: false,
          action: "click",
          mode: "control_selector",
          failure_reason: settledIdentity.reason,
          target_identity_mismatch_field: settledIdentity.mismatch_field || "",
          control_selector_resolved: true,
          activation_attempted: false,
          activation_dispatched: false,
          activation_count: 0,
        };
      }
      center = settled;
    } catch (error) {
      return {
        ok: false,
        action: "click",
        mode: "control_selector",
        failure_reason: "control_settled_probe_failed",
        error: errorMessage(error),
        control_selector_resolved: false,
        selector_was_resolved: true,
      };
    }
    // A whole-surface signature includes unrelated viewport text, control values, and geometry.
    // Once an ordinary reversible selector has re-resolved twice and still matches its complete
    // observed identity, keep the exact URL binding but let the final adjacent target probes own
    // activation safety. Final-action and guarded-intermediate capabilities retain the strict
    // surface binding above.
    const clickPageContext = ordinaryReversible
      ? {
        ...(expectedPageContext && typeof expectedPageContext === "object"
          ? expectedPageContext
          : {}),
        url: expectedContextUrl,
        surface_signature: "",
      }
      : expectedPageContext;
    let clicked = await runVisionClick(
      tabId,
      center.img_x || 0,
      center.img_y || 0,
      Boolean(allowFinalSubmit),
      Boolean(allowGuardedIntermediate),
      expectedTarget,
      clickPageContext,
      pointer,
    );
    // A control that moves on its own: the server cleared this click as ordinary and reversible and set
    // allow_rebind_on_drift. Where the refusal is only that the box is no longer at the probed point,
    // re-resolve the same selector, require its complete identity unchanged, and press its current centre.
    // Only a refusal where nothing was dispatched retries: a press the dispatch guard blocked may still have been seen
    // by earlier-registered window capture listeners (activation_dispatched), so it is never pressed again.
    let reboundFrom = null;
    let reboundTo = null;
    for (
      let attempt = 0;
      attempt < 3
        && ordinaryReversible
        && pointer && pointer.rebind_on_drift === true
        && clicked && clicked.ok === false
        && ["click_target_drift", "click_target_drift_at_dispatch", "click_intercepted"].includes(clicked.failure_reason)
        && !clicked.dispatch_guard_partial
        && !clicked.execution_outcome_unknown
        && clicked.activation_dispatched !== true
        && Date.now() < rebindDeadline
        && Number((pointer && pointer.clicks) || 1) === 1
        && !["right", "middle"].includes(String((pointer && pointer.button) || ""))
        && Number(clicked.activation_count || 0) === 0;
      attempt += 1
    ) {
      let moved = null;
      try {
        const rows = await withTimeout(
          chrome.scripting.executeScript({ target: { tabId }, func: visionControlCenterInPage, args: [String(selector || ""), false, true, surfaceOffset] }),
          2500,
          "moved control point probe timed out",
        );
        moved = (rows && rows[0] && rows[0].result) || null;
      } catch (_) {
        moved = null;
      }
      if (!moved || !moved.ok || !Number.isFinite(moved.img_x) || !Number.isFinite(moved.img_y)) break;
      // Exact identity (selector, tag, role, type, accessible name, page): only the box may differ.
      if (!clickTargetIdentityVerdict(expectedTarget, moved, true).ok) break;
      // Only a control that actually changed position is followed; a stationary one that is covered stays refused.
      if (moved.img_x === (center.img_x || 0) && moved.img_y === (center.img_y || 0)) break;
      if (!reboundFrom) reboundFrom = [center.img_x || 0, center.img_y || 0];
      reboundTo = [moved.img_x, moved.img_y];
      center = moved;
      clicked = await runVisionClick(
        tabId,
        moved.img_x,
        moved.img_y,
        false,
        false,
        expectedTarget,
        clickPageContext,
        pointer,
      );
    }
    const reboundFacts = reboundFrom && reboundTo
      ? {
        target_moved_rebound_at_dispatch: "from (" + reboundFrom.join(",") + ") to (" + reboundTo.join(",") + ")",
        target_moved_rebound_from: reboundFrom,
        target_moved_rebound_to: reboundTo,
      }
      : {};
    return {
      ...clicked,
      ...reboundFacts,
      ...scrollInfo,
      action: "click",
      mode: "control_selector",
      click_mode: clicked.mode || "",
      control_selector_resolved: true,
      reversible_page_context_basis: ordinaryReversible
        ? "exact_url_and_live_target_identity"
        : "exact_url_and_surface_signature",
      ...(reversibleSurfaceDrift
        ? {
          reversible_surface_drift_tolerated: true,
          observed_surface_signature: expectedSurfaceSignature,
          live_surface_signature: liveSurfaceSignature,
        }
        : {}),
    };
  }
  if ((allowFinalSubmit || allowGuardedIntermediate) && String(selector || "").trim()) {
    return {
      ok: false,
      action: "click",
      mode: "control_selector",
      failure_reason: allowFinalSubmit ? "final_submit_target_mismatch" : "guarded_intermediate_target_mismatch",
      selector_failure_reason: center.failure_reason || "control_not_found",
      error: center.error || "",
      control_selector_resolved: false,
    };
  }
  if (allowGuardedIntermediate) {
    return {
      ok: false,
      action: "click",
      mode: "control_selector",
      failure_reason: "guarded_intermediate_target_mismatch",
      selector_failure_reason: center.failure_reason || "control_not_found",
      error: center.error || "",
      control_selector_resolved: false,
    };
  }
  if (Number(fallbackX || 0) || Number(fallbackY || 0)) {
    const clicked = await runVisionClick(
      tabId,
      Number(fallbackX || 0),
      Number(fallbackY || 0),
      Boolean(allowFinalSubmit),
      false,
      expectedTarget,
      expectedPageContext,
      pointer,
    );
    return { ...clicked, action: "click", mode: "coordinate_fallback", click_mode: clicked.mode || "", control_selector_resolved: false, selector_failure_reason: center.failure_reason || "control_not_found" };
  }
  return {
    ok: false,
    action: "click",
    mode: "control_selector",
    failure_reason: "control_not_found",
    error: center.error || "",
    control_selector_resolved: false,
  };
}

function focusControlForTypeInPage(selector, expectedTarget = null) {
  try {
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const associatedLabelText = (node) => {
      try {
        if (!node || !node.tagName) return "";
        const tag = String(node.tagName || "").toLowerCase();
        if (!["input", "select", "textarea"].includes(tag)) return "";
        const squash = (value) => String(value || "").replace(/\s+/g, " ").trim();
        // The observer names a label by its composed text (slotted content included); read it the same way.
        const labelShownText = (ref) => (
          (typeof globalThis.__lumenComposedText === "function" ? globalThis.__lumenComposedText(ref) : "")
          || ref.innerText || ref.textContent || ""
        );
        const root = node.getRootNode && node.getRootNode();
        const byId = (id) => (root && root.getElementById ? root.getElementById(id) : null) || document.getElementById(id);
        const labelledBy = squash(node.getAttribute && node.getAttribute("aria-labelledby"));
        if (labelledBy) {
          const text = squash(labelledBy.split(/\s+/).map((id) => { const ref = byId(id); return ref ? labelShownText(ref) : ""; }).join(" "));
          if (text) return text;
        }
        const id = node.getAttribute && node.getAttribute("id");
        if (id) {
          const escaped = (window.CSS && typeof CSS.escape === "function") ? CSS.escape(id) : String(id).split('"').join('\\"');
          const explicit = (root && root.querySelector ? root.querySelector(`label[for="${escaped}"]`) : null)
            || document.querySelector(`label[for="${escaped}"]`);
          const text = explicit ? squash(labelShownText(explicit)) : "";
          if (text) return text;
        }
        const wrapper = node.closest && node.closest("label");
        const wrapped = wrapper ? squash(labelShownText(wrapper)) : "";
        return wrapped;
      } catch (_) {
        return "";
      }
    };
    const queryAll = typeof globalThis.__lumenDeepQueryAll === "function"
      ? globalThis.__lumenDeepQueryAll
      : (value) => Array.from(document.querySelectorAll(value));
    const deepActiveElement = typeof globalThis.__lumenDeepActiveElement === "function"
      ? globalThis.__lumenDeepActiveElement
      : () => document.activeElement;
    const deepDescendants = typeof globalThis.__lumenDeepDescendants === "function"
      ? globalThis.__lumenDeepDescendants
      : (node, value) => (
        node && node.querySelectorAll ? Array.from(node.querySelectorAll(value)) : []
      );
    const sensitiveValueControl = (node) => {
      const tag = (node && node.tagName || "").toLowerCase();
      const inputType = clean(node && node.getAttribute && node.getAttribute("type")).toLowerCase();
      if (tag === "input" && inputType === "password") return true;
      const tokens = clean(node && node.getAttribute && node.getAttribute("autocomplete"))
        .toLowerCase()
        .split(/\s+/)
        .filter(Boolean);
      return tokens.some((token) => (
        token === "current-password"
        || token === "new-password"
        || token === "one-time-code"
        || token === "transaction-amount"
        || token === "transaction-currency"
        || token === "webauthn"
        || token.startsWith("cc-")
      ));
    };
    const isTypable = (node) => {
      if (!node) return false;
      const tag = (node.tagName || "").toLowerCase();
      if (tag === "textarea" || tag === "select") return true;
      if (tag === "input") return String(node.getAttribute("type") || "text").toLowerCase() !== "file";
      return Boolean(node.isContentEditable);
    };
    const typableSelector = 'input:not([type="file"]),textarea,select,[contenteditable="true"]';
    let matches = [];
    try {
      matches = selector ? Array.from(queryAll(String(selector))) : [];
    } catch (_) {
      matches = [];
    }
    if (matches.length !== 1) {
      return {
        ok: false,
        action: "type",
        failure_reason: matches.length > 1 ? "control_selector_ambiguous" : "control_not_found",
        error: "Type control was not uniquely resolved.",
      };
    }
    const anchor = matches[0];
    const target = isTypable(anchor)
      ? anchor
      : deepDescendants(anchor, typableSelector).find(isTypable) || anchor;
    const targetText = clean(
      sensitiveValueControl(target)
        ? (target.getAttribute("aria-label") || target.getAttribute("placeholder") || target.getAttribute("name") || "")
        : (
          target.innerText
          || target.textContent
          || associatedLabelText(target)
          || target.getAttribute("aria-label")
          || target.getAttribute("placeholder")
          || target.getAttribute("name")
          || ""
        ),
    ).slice(0, 180).trim();
    const liveIdentity = {
      target_selector: String(selector || "").trim(),
      target_text: targetText,
      target_tag: (target.tagName || "").toLowerCase(),
      target_type: clean(target.getAttribute("type")).toLowerCase(),
      target_role: clean(target.getAttribute("role")).toLowerCase(),
      // The shared rule: a rendered surface (canvas/SVG) is a coordinate surface.
      dispatch_path: ((n) => { const a = n.closest && n.closest('button,a[href],input,textarea,select,label,[role="button"],[role="option"],[role="radio"],[role="checkbox"],[role="combobox"],[tabindex]'); const s = n.closest && n.closest("canvas,svg"); return s && (!a || s === a) ? "cdp_trusted_click" : "dom_activation"; })(target),
      page_url: String(location.href || ""),
    };
    const expected = expectedTarget && typeof expectedTarget === "object" ? expectedTarget : {};
    const fields = ["target_selector", "target_text", "target_tag", "target_type", "target_role", "dispatch_path", "page_url"];
    // A type equal to the tag is the observer's "no type attribute" fallback.
    const normalizedIdentityValue = (identity, field) => {
      const value = clean(identity[field]);
      if (field !== "target_type") return value;
      return value.toLowerCase() === clean(identity.target_tag).toLowerCase() ? "" : value.toLowerCase();
    };
    const expectedNames = (Array.isArray(expected.target_names) ? expected.target_names : [])
      .map((name) => clean(name))
      .filter(Boolean);
    // A field's label can carry live widget text after it (a typeahead's "No location found ...
    // Loading" status inside the wrapping <label>), so the text read at observation and at action
    // differ in that tail; the same leading label at a word boundary is the same field. Selector,
    // tag, type, role, and page still have to match exactly.
    const sameLeadingLabel = (left, right) => {
      const a = clean(left);
      const b = clean(right);
      // "First Name *" observed vs "First Name*" live (Greenhouse, run 1795): the same characters.
      if (a && a.replace(/\s+/g, "") === b.replace(/\s+/g, "")) return true;
      const [shorter, longer] = a.length <= b.length ? [a, b] : [b, a];
      return shorter.length >= 4 && longer.startsWith(shorter) && (longer.length === shorter.length || longer[shorter.length] === " ");
    };
    const mismatchField = fields.find((field) => (
      normalizedIdentityValue(expected, field) !== normalizedIdentityValue(liveIdentity, field)
      && !(field === "target_text" && liveIdentity.target_text && expectedNames.includes(clean(liveIdentity.target_text)))
      && !(field === "target_text" && liveIdentity.target_text
        && [clean(expected.target_text), ...expectedNames].some((name) => sameLeadingLabel(name, liveIdentity.target_text)))
    ));
    if (mismatchField) {
      return {
        ok: false,
        action: "type",
        failure_reason: "control_target_drift",
        target_identity_mismatch_field: mismatchField,
        // The two values that differed (a field's label, never its value), so a refusal can be read back.
        identity_expected: clean(expected[mismatchField]).slice(0, 120),
        identity_live: clean(liveIdentity[mismatchField]).slice(0, 120),
        control_selector_resolved: true,
        focus_attempted: false,
      };
    }
    const fullyVisibleInViewport = (node) => {
      // Entirely inside the viewport and inside every scrolling ancestor's box: centring it would
      // only move the page under the model's other points.
      try {
        const box = node.getBoundingClientRect();
        if (!box || box.width <= 0 || box.height <= 0) return false;
        if (box.left < 0 || box.top < 0 || box.right > window.innerWidth || box.bottom > window.innerHeight) return false;
        for (let parent = node.parentElement; parent; parent = parent.parentElement) {
          if (parent.scrollHeight > parent.clientHeight + 1 || parent.scrollWidth > parent.clientWidth + 1) {
            const outer = parent.getBoundingClientRect();
            if (box.left < outer.left || box.top < outer.top || box.right > outer.right || box.bottom > outer.bottom) return false;
          }
        }
        return true;
      } catch (_) {
        return false;
      }
    };
    try {
      if (typeof target.scrollIntoView === "function" && !fullyVisibleInViewport(target)) {
        target.scrollIntoView({ block: "center", inline: "center", behavior: "auto" });
      }
    } catch (_) {}
    if (isTypable(target) && (target.readOnly === true || target.disabled === true)) {
      // A keyboard cannot change a read-only or disabled field; a script value write would
      // read back as typed while the page never accepts it.
      return {
        ok: false,
        action: "type",
        failure_reason: target.disabled === true ? "control_disabled" : "control_readonly",
        control_selector_resolved: true,
        target_tag: (target.tagName || "").toLowerCase(),
        active_typable: false,
        error: target.disabled === true
          ? "This field is disabled; typing cannot change it."
          : "This field is read-only; typing cannot change it.",
      };
    }
    if (!isTypable(target)) {
      return {
        ok: false,
        action: "type",
        failure_reason: "control_not_typable",
        control_selector_resolved: true,
        target_tag: (target.tagName || "").toLowerCase(),
        active_tag: document.activeElement ? (document.activeElement.tagName || "").toLowerCase() : "",
        active_typable: false,
        error: "Resolved control is not typable.",
      };
    }
    try {
      if (typeof target.focus === "function") target.focus({ preventScroll: true });
    } catch (_) {}
    if (deepActiveElement() !== target) {
      const rect = (typeof globalThis.__lumenTopRect === "function" ? globalThis.__lumenTopRect(target) : target.getBoundingClientRect());
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      const eventInit = { bubbles: true, cancelable: true, composed: true, clientX: x, clientY: y, view: window, button: 0, buttons: 1 };
      target.dispatchEvent(new MouseEvent("mouseover", eventInit));
      target.dispatchEvent(new MouseEvent("mousemove", eventInit));
      target.dispatchEvent(new MouseEvent("mousedown", eventInit));
      try {
        if (typeof target.focus === "function") target.focus({ preventScroll: true });
      } catch (_) {}
      target.dispatchEvent(new MouseEvent("mouseup", { ...eventInit, buttons: 0 }));
      target.dispatchEvent(new MouseEvent("click", { ...eventInit, buttons: 0 }));
      try {
        if (typeof target.focus === "function") target.focus({ preventScroll: true });
      } catch (_) {}
    }
    const active = deepActiveElement();
    const activeTag = active ? (active.tagName || "").toLowerCase() : "";
    const activeTypable = Boolean(isTypable(active));
    return {
      ok: active === target && activeTypable,
      action: "type",
      control_selector_resolved: true,
      target_tag: (target.tagName || "").toLowerCase(),
      active_tag: activeTag,
      active_typable: activeTypable,
      active_hint: activeTypable ? clean(active.getAttribute("aria-label") || active.getAttribute("placeholder") || active.getAttribute("name") || "") : "",
      error: active === target && activeTypable ? "" : "Resolved type control did not receive focus.",
    };
  } catch (e) {
    return { ok: false, action: "type", failure_reason: "control_focus_failed", error: String(e) };
  }
}

function replayCredentialControlInPage(
  selector,
  text,
  expectedTarget = null,
  expectedPageContext = null,
  keyMode = false,
) {
  // Keep the security-sensitive replay in one injected task. A focus handler can synchronously
  // navigate, replace the document, or swap the nominated control; no later injected call may
  // then type into whichever element happens to be active on the replacement page.
  const receipt = (overrides = {}) => ({
    ok: false,
    action: "type",
    mode: "credential_atomic_control_selector",
    control_selector_resolved: false,
    focus_attempted: false,
    value_write_attempted: false,
    value_verified_locally: false,
    active_typable: false,
    active_has_value: false,
    active_value_matches_expected: false,
    active_value_redacted: true,
    active_value_redaction_reason: "credential_replay",
    browser_mutation_attempted: false,
    ...overrides,
  });
  try {
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const associatedLabelText = (node) => {
      try {
        if (!node || !node.tagName) return "";
        const tag = String(node.tagName || "").toLowerCase();
        if (!["input", "select", "textarea"].includes(tag)) return "";
        const squash = (value) => String(value || "").replace(/\s+/g, " ").trim();
        // The observer names a label by its composed text (slotted content included); read it the same way.
        const labelShownText = (ref) => (
          (typeof globalThis.__lumenComposedText === "function" ? globalThis.__lumenComposedText(ref) : "")
          || ref.innerText || ref.textContent || ""
        );
        const root = node.getRootNode && node.getRootNode();
        const byId = (id) => (root && root.getElementById ? root.getElementById(id) : null) || document.getElementById(id);
        const labelledBy = squash(node.getAttribute && node.getAttribute("aria-labelledby"));
        if (labelledBy) {
          const text = squash(labelledBy.split(/\s+/).map((id) => { const ref = byId(id); return ref ? labelShownText(ref) : ""; }).join(" "));
          if (text) return text;
        }
        const id = node.getAttribute && node.getAttribute("id");
        if (id) {
          const escaped = (window.CSS && typeof CSS.escape === "function") ? CSS.escape(id) : String(id).split('"').join('\\"');
          const explicit = (root && root.querySelector ? root.querySelector(`label[for="${escaped}"]`) : null)
            || document.querySelector(`label[for="${escaped}"]`);
          const text = explicit ? squash(labelShownText(explicit)) : "";
          if (text) return text;
        }
        const wrapper = node.closest && node.closest("label");
        const wrapped = wrapper ? squash(labelShownText(wrapper)) : "";
        return wrapped;
      } catch (_) {
        return "";
      }
    };
    const queryAll = typeof globalThis.__lumenDeepQueryAll === "function"
      ? globalThis.__lumenDeepQueryAll
      : (value) => Array.from(document.querySelectorAll(value));
    const deepActiveElement = typeof globalThis.__lumenDeepActiveElement === "function"
      ? globalThis.__lumenDeepActiveElement
      : () => document.activeElement;
    const deepDescendants = typeof globalThis.__lumenDeepDescendants === "function"
      ? globalThis.__lumenDeepDescendants
      : (node, value) => (
        node && node.querySelectorAll ? Array.from(node.querySelectorAll(value)) : []
      );
    const isTypable = (node) => {
      if (!node || node.disabled || clean(node.getAttribute && node.getAttribute("aria-disabled")).toLowerCase() === "true") {
        return false;
      }
      const tag = (node.tagName || "").toLowerCase();
      if (tag === "textarea") return !node.readOnly;
      if (tag === "input") {
        const inputType = clean(node.getAttribute("type") || "text").toLowerCase();
        return inputType !== "file" && inputType !== "hidden" && !node.readOnly;
      }
      return Boolean(node.isContentEditable);
    };
    const lifecycleToken = () => {
      let token = clean(globalThis.__lumenDocumentLifecycleToken);
      if (!token) {
        token = (
          globalThis.crypto && typeof globalThis.crypto.randomUUID === "function"
            ? globalThis.crypto.randomUUID()
            : `${Math.round(Number(performance.timeOrigin || Date.now()) * 1000)}-${Math.random().toString(36).slice(2)}`
        );
        try {
          Object.defineProperty(globalThis, "__lumenDocumentLifecycleToken", {
            value: token,
            configurable: false,
            enumerable: false,
            writable: false,
          });
        } catch (_) {
          globalThis.__lumenDocumentLifecycleToken = token;
        }
      }
      return token;
    };
    const exactSelector = String(selector || "").trim();
    const expected = expectedTarget && typeof expectedTarget === "object" ? expectedTarget : {};
    const expectedPage = expectedPageContext && typeof expectedPageContext === "object"
      ? expectedPageContext
      : {};
    const expectedUrl = String(expectedPage.url || "").trim();
    const initialDocument = document;
    const initialWindow = window;
    const initialUrl = String(location.href || "");
    const initialOrigin = String(location.origin || "");
    const initialLifecycleToken = lifecycleToken();
    const expectedLifecycleToken = String(expectedPage.document_lifecycle_token || "").trim();
    if (!expectedUrl || initialUrl !== expectedUrl) {
      return receipt({
        failure_reason: expectedUrl ? "page_context_drift" : "page_context_required",
        page_context_mismatch_field: expectedUrl ? "url" : "",
        url_changed: Boolean(expectedUrl && initialUrl !== expectedUrl),
      });
    }
    if (expectedLifecycleToken && initialLifecycleToken !== expectedLifecycleToken) {
      return receipt({
        failure_reason: "page_context_drift",
        page_context_mismatch_field: "document_lifecycle_token",
        document_lifecycle_changed: true,
        // Diagnostics only: the leading characters of two extension-generated random tokens.
        lifecycle_expected_prefix: expectedLifecycleToken.slice(0, 8),
        lifecycle_live_prefix: String(initialLifecycleToken || "").slice(0, 8),
      });
    }

    let matches = [];
    try {
      matches = exactSelector ? Array.from(queryAll(exactSelector)) : [];
    } catch (_) {
      matches = [];
    }
    if (matches.length !== 1) {
      return receipt({
        failure_reason: matches.length > 1
          ? "control_selector_ambiguous"
          : "control_not_found",
      });
    }
    const anchor = matches[0];
    const typableSelector = 'input:not([type="file"]):not([type="hidden"]),textarea,[contenteditable="true"]';
    const target = isTypable(anchor)
      ? anchor
      : deepDescendants(anchor, typableSelector).find(isTypable) || anchor;
    const targetText = clean(
      target.getAttribute && (
        target.getAttribute("aria-label")
        || target.getAttribute("placeholder")
        || target.getAttribute("name")
      )
      || target.innerText
      || target.textContent
      || associatedLabelText(target)
      || "",
    ).slice(0, 180).trim();
    const liveIdentity = () => ({
      target_selector: exactSelector,
      target_text: targetText,
      target_tag: (target.tagName || "").toLowerCase(),
      target_type: clean(target.getAttribute && target.getAttribute("type")).toLowerCase(),
      target_role: clean(target.getAttribute && target.getAttribute("role")).toLowerCase(),
      dispatch_path: "dom_activation",
      page_url: String(location.href || ""),
    });
    const identityFields = [
      "target_selector",
      "target_text",
      "target_tag",
      "target_type",
      "target_role",
      "dispatch_path",
      "page_url",
    ];
    const normalizedIdentityValue = (identity, field) => {
      const value = clean(identity[field]);
      if (field !== "target_type") return value;
      return value.toLowerCase() === clean(identity.target_tag).toLowerCase() ? "" : value.toLowerCase();
    };
    const expectedNames = (Array.isArray(expected.target_names) ? expected.target_names : [])
      .map((name) => clean(name))
      .filter(Boolean);
    const identityMismatch = () => {
      const live = liveIdentity();
      return identityFields.find(
        (field) => (
          normalizedIdentityValue(expected, field) !== normalizedIdentityValue(live, field)
          && !(field === "target_text" && live.target_text && expectedNames.includes(clean(live.target_text)))
          // Same characters, different spacing ("First Name *" vs "First Name*"); the selector is exact.
          && !(
            field === "target_text"
            && live.target_text
            && [expected.target_text, ...expectedNames].some(
              (name) => clean(name) && clean(name).replace(/\s+/g, "") === clean(live.target_text).replace(/\s+/g, ""),
            )
          )
        ),
      ) || "";
    };
    const initialIdentityMismatch = identityMismatch();
    if (initialIdentityMismatch) {
      return receipt({
        failure_reason: "control_target_drift",
        target_identity_mismatch_field: initialIdentityMismatch,
        control_selector_resolved: true,
      });
    }
    if (!isTypable(target)) {
      return receipt({
        failure_reason: "control_not_typable",
        control_selector_resolved: true,
      });
    }
    // A control in a same-origin frame (iCIMS renders its sign-in form in #icims_content_iframe)
    // belongs to the frame's document, not the top one: bind the replay to that document and to the
    // frame that hosts it, so the frame field is not refused as a document change (runs 1793/1794).
    // A frame on another origin is never typed into.
    const initialTargetDocument = target.ownerDocument;
    const targetFrameHost = () => {
      if (initialTargetDocument === initialDocument) return true;
      try {
        const frameWindow = initialTargetDocument.defaultView;
        const frameElement = frameWindow && frameWindow.frameElement;
        return Boolean(
          frameElement
          && frameElement.isConnected
          && frameElement.ownerDocument === initialDocument
          && String(frameWindow.location.origin || "") === initialOrigin
        );
      } catch (_) {
        return false;
      }
    };
    if (!initialTargetDocument || !targetFrameHost()) {
      return receipt({
        failure_reason: "page_context_drift",
        page_context_mismatch_field: "url",
        control_selector_resolved: true,
      });
    }

    const contextDrift = () => {
      if (
        document !== initialDocument
        || window !== initialWindow
        || initialDocument.defaultView !== initialWindow
      ) {
        return "document";
      }
      if (String(location.href || "") !== initialUrl) return "url";
      if (String(location.origin || "") !== initialOrigin) return "origin";
      if (lifecycleToken() !== initialLifecycleToken) return "document_lifecycle_token";
      if (
        !target.isConnected
        || target.ownerDocument !== initialTargetDocument
        || (
          target.getRootNode
          && target.getRootNode({ composed: true }) !== initialTargetDocument
        )
        || !targetFrameHost()
      ) {
        return "target_document";
      }
      let currentMatches = [];
      try {
        currentMatches = Array.from(queryAll(exactSelector));
      } catch (_) {
        return "target_selector";
      }
      if (currentMatches.length !== 1 || currentMatches[0] !== anchor) {
        return "target_selector";
      }
      const currentIdentityMismatch = identityMismatch();
      if (currentIdentityMismatch) return `target_identity:${currentIdentityMismatch}`;
      return "";
    };
    const driftReceipt = (field, overrides = {}) => {
      const isUrlDrift = field === "url" || field === "origin";
      const isDocumentDrift = [
        "document",
        "document_lifecycle_token",
        "target_document",
      ].includes(field);
      const targetIdentityField = field.startsWith("target_identity:")
        ? field.slice("target_identity:".length)
        : "";
      return receipt({
        failure_reason: (
          isUrlDrift || isDocumentDrift
            ? "credential_replay_page_context_drift"
            : "credential_replay_target_drift"
        ),
        page_context_mismatch_field: (
          isUrlDrift
            ? (field === "origin" ? "url" : field)
            : isDocumentDrift
              ? "document_lifecycle_token"
              : ""
        ),
        target_identity_mismatch_field: targetIdentityField || (
          field === "target_selector" ? "target_selector" : ""
        ),
        control_selector_resolved: true,
        url_changed: isUrlDrift,
        document_lifecycle_changed: isDocumentDrift,
        ...overrides,
      });
    };

    const fullyVisibleInViewport = (node) => {
      // Entirely inside the viewport and inside every scrolling ancestor's box: centring it would
      // only move the page under the model's other points.
      try {
        const box = node.getBoundingClientRect();
        if (!box || box.width <= 0 || box.height <= 0) return false;
        if (box.left < 0 || box.top < 0 || box.right > window.innerWidth || box.bottom > window.innerHeight) return false;
        for (let parent = node.parentElement; parent; parent = parent.parentElement) {
          if (parent.scrollHeight > parent.clientHeight + 1 || parent.scrollWidth > parent.clientWidth + 1) {
            const outer = parent.getBoundingClientRect();
            if (box.left < outer.left || box.top < outer.top || box.right > outer.right || box.bottom > outer.bottom) return false;
          }
        }
        return true;
      } catch (_) {
        return false;
      }
    };
    try {
      if (typeof target.scrollIntoView === "function" && !fullyVisibleInViewport(target)) {
        target.scrollIntoView({ block: "center", inline: "center", behavior: "auto" });
      }
    } catch (_) {}
    let driftField = contextDrift();
    if (driftField) return driftReceipt(driftField);

    try {
      if (typeof target.focus === "function") target.focus({ preventScroll: true });
    } catch (_) {}
    driftField = contextDrift();
    if (driftField) {
      return driftReceipt(driftField, { focus_attempted: true });
    }
    if (deepActiveElement() !== target) {
      return receipt({
        failure_reason: "control_focus_failed",
        control_selector_resolved: true,
        focus_attempted: true,
      });
    }

    const value = String(text || "");
    const tag = (target.tagName || "").toLowerCase();
    const before = tag === "input" || tag === "textarea"
      ? String(target.value || "")
      : String(target.textContent || "");
    const clearWrittenValue = () => {
      try {
        if (tag === "input" || tag === "textarea") {
          const realm = (target.ownerDocument && target.ownerDocument.defaultView) || initialWindow;
          const proto = tag === "textarea"
            ? realm.HTMLTextAreaElement.prototype
            : realm.HTMLInputElement.prototype;
          const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
          if (descriptor && typeof descriptor.set === "function") descriptor.set.call(target, "");
          else target.value = "";
        } else if (target.isContentEditable) {
          target.textContent = "";
        }
      } catch (_) {}
    };
    if (keyMode) {
      // Key mode: every check above passed and the exact target is focused. Clear it and bind it
      // in this isolated world; the caller then sends real keystrokes, asking before each key that
      // focus is still on this target in an unchanged page (credentialKeyBinding*InPage), and
      // finishes with the same local verification as a value write. The value is never passed here.
      if (!(tag === "input" || tag === "textarea")) {
        return receipt({ failure_reason: "control_not_typable", control_selector_resolved: true, focus_attempted: true });
      }
      if (before) {
        clearWrittenValue();
        try {
          target.dispatchEvent(new initialWindow.Event("input", { bubbles: true, composed: true }));
        } catch (_) {}
      }
      driftField = contextDrift();
      if (driftField || deepActiveElement() !== target) {
        return driftReceipt(driftField || "target_selector", {
          focus_attempted: true,
          browser_mutation_attempted: Boolean(before),
        });
      }
      const bindingId = (
        globalThis.crypto && typeof globalThis.crypto.randomUUID === "function"
          ? globalThis.crypto.randomUUID()
          : String(Date.now()) + "-" + Math.random().toString(36).slice(2)
      );
      const finishKeyed = (expectedValue) => {
        const typedValue = String(expectedValue || "");
        const after = String(target.value || "");
        const locallyVerified = deepActiveElement() === target && after === typedValue && !contextDrift();
        let ariaInvalid = "";
        let validityValid = null;
        try {
          ariaInvalid = clean(target.getAttribute("aria-invalid")).toLowerCase();
          if (target.validity && typeof target.validity.valid === "boolean") validityValid = Boolean(target.validity.valid);
        } catch (_) {}
        if (!locallyVerified) {
          clearWrittenValue();
          return receipt({
            failure_reason: "credential_local_verification_failed",
            control_selector_resolved: true,
            focus_attempted: true,
            value_write_attempted: true,
            browser_mutation_attempted: true,
            active_aria_invalid: ariaInvalid,
            active_validity_valid: validityValid,
          });
        }
        try {
          target.dispatchEvent(new initialWindow.Event("change", { bubbles: true, composed: true }));
        } catch (_) {}
        return receipt({
          ok: true,
          failure_reason: "",
          control_selector_resolved: true,
          focus_attempted: true,
          value_write_attempted: true,
          value_verified_locally: true,
          active_typable: true,
          active_has_value: Boolean(after.trim()),
          active_value_matches_expected: true,
          active_value_length: after.length,
          before_has_value: Boolean(before.trim()),
          before_value_length: before.length,
          before_value_redacted: true,
          active_tag: tag,
          active_selector: exactSelector,
          active_name: clean(target.getAttribute && target.getAttribute("name")).slice(0, 120),
          active_aria_invalid: ariaInvalid,
          active_validity_valid: validityValid,
          browser_mutation_attempted: true,
          page_url: initialUrl,
          document_lifecycle_token: initialLifecycleToken,
        });
      };
      try {
        Object.defineProperty(globalThis, "__lumenCredentialKeyBinding", {
          value: {
            id: bindingId,
            stillBound: () => {
              try {
                return deepActiveElement() === target && !contextDrift();
              } catch (_) {
                return false;
              }
            },
            clear: () => {
              clearWrittenValue();
              try {
                target.dispatchEvent(new initialWindow.Event("input", { bubbles: true, composed: true }));
              } catch (_) {}
            },
            finish: finishKeyed,
          },
          configurable: true,
          enumerable: false,
          writable: true,
        });
      } catch (_) {
        return receipt({ failure_reason: "credential_replay_failed", control_selector_resolved: true, focus_attempted: true });
      }
      return receipt({
        ok: true,
        key_binding_prepared: true,
        key_binding_id: bindingId,
        control_selector_resolved: true,
        focus_attempted: true,
        browser_mutation_attempted: Boolean(before),
      });
    }
    let valueWriteAttempted = false;
    try {
      valueWriteAttempted = true;
      if (tag === "input" || tag === "textarea") {
        // A same-origin frame's control is set through its own realm's native setter.
        const realm = (target.ownerDocument && target.ownerDocument.defaultView) || initialWindow;
        const proto = tag === "textarea"
          ? realm.HTMLTextAreaElement.prototype
          : realm.HTMLInputElement.prototype;
        const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
        if (descriptor && typeof descriptor.set === "function") descriptor.set.call(target, value);
        else target.value = value;
      } else if (target.isContentEditable) {
        target.textContent = value;
      } else {
        return receipt({
          failure_reason: "control_not_typable",
          control_selector_resolved: true,
          focus_attempted: true,
        });
      }
    } catch (_) {
      clearWrittenValue();
      return receipt({
        failure_reason: "credential_value_set_failed",
        control_selector_resolved: true,
        focus_attempted: true,
        value_write_attempted: valueWriteAttempted,
        browser_mutation_attempted: valueWriteAttempted,
      });
    }
    driftField = contextDrift();
    if (driftField) {
      clearWrittenValue();
      return driftReceipt(driftField, {
        focus_attempted: true,
        value_write_attempted: true,
        browser_mutation_attempted: true,
      });
    }

    for (const eventName of ["input", "change"]) {
      try {
        target.dispatchEvent(new initialWindow.Event(eventName, {
          bubbles: true,
          composed: true,
        }));
      } catch (_) {
        clearWrittenValue();
        return receipt({
          failure_reason: "credential_event_dispatch_failed",
          control_selector_resolved: true,
          focus_attempted: true,
          value_write_attempted: true,
          browser_mutation_attempted: true,
        });
      }
      driftField = contextDrift();
      if (driftField) {
        clearWrittenValue();
        return driftReceipt(driftField, {
          focus_attempted: true,
          value_write_attempted: true,
          browser_mutation_attempted: true,
        });
      }
    }

    const after = tag === "input" || tag === "textarea"
      ? String(target.value || "")
      : String(target.textContent || "");
    const locallyVerified = (
      deepActiveElement() === target
      && after === value
      && !contextDrift()
    );
    let ariaInvalid = "";
    let validityValid = null;
    try {
      ariaInvalid = clean(target.getAttribute("aria-invalid")).toLowerCase();
      if (target.validity && typeof target.validity.valid === "boolean") {
        validityValid = Boolean(target.validity.valid);
      }
    } catch (_) {}
    if (!locallyVerified) {
      clearWrittenValue();
      return receipt({
        failure_reason: "credential_local_verification_failed",
        control_selector_resolved: true,
        focus_attempted: true,
        value_write_attempted: true,
        browser_mutation_attempted: true,
        active_aria_invalid: ariaInvalid,
        active_validity_valid: validityValid,
      });
    }
    return receipt({
      ok: true,
      failure_reason: "",
      control_selector_resolved: true,
      focus_attempted: true,
      value_write_attempted: true,
      value_verified_locally: true,
      active_typable: true,
      active_has_value: Boolean(after.trim()),
      active_value_matches_expected: true,
      active_value_length: after.length,
      before_has_value: Boolean(before.trim()),
      before_value_length: before.length,
      before_value_redacted: true,
      active_tag: tag || "contenteditable",
      active_selector: exactSelector,
      active_name: clean(target.getAttribute && target.getAttribute("name")).slice(0, 120),
      active_aria_invalid: ariaInvalid,
      active_validity_valid: validityValid,
      browser_mutation_attempted: true,
      page_url: initialUrl,
      document_lifecycle_token: initialLifecycleToken,
    });
  } catch (_) {
    // Never serialize a page-controlled exception from the credential path: a hostile accessor or
    // event handler could include the transient value in its message.
    return receipt({ failure_reason: "credential_replay_failed" });
  }
}

function typeIntoNativeDialog(tabId, text) {
  const dialog = nativeDialogOpenFor(tabId);
  if (!dialog) return null;
  if (dialog.type !== "prompt") {
    return {
      ok: false,
      action: "type",
      mode: "native_dialog",
      failure_reason: "native_dialog_open",
      native_dialog: publicNativeDialog(dialog),
      error: "A browser dialog without a text field is open; answer it with Enter (OK) or Escape (Cancel).",
    };
  }
  dialog.prompt_text = String(text || "");
  return {
    ok: true,
    action: "type",
    mode: "native_dialog_prompt",
    active_tag: "dialog",
    active_typable: true,
    active_has_value: Boolean(dialog.prompt_text.trim()),
    active_value: dialog.prompt_text.slice(0, 500),
    active_value_matches_expected: true,
    native_dialog: publicNativeDialog(dialog),
  };
}

async function runVisionType(tabId, text) {
  if (!tabId) return { ok: false, action: "type", error: "No application tab." };
  const dialogTyped = typeIntoNativeDialog(tabId, text);
  if (dialogTyped) return dialogTyped;
  try {
    const beforeRows = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId },
        func: selectActiveTextInPage,
      }),
      2500,
      "active text selection timed out",
    );
    const before = (beforeRows && beforeRows[0] && beforeRows[0].result) || {};
    if (!before.ok) return { ok: false, action: "type", mode: "dom_value_replace", ...before };
    await showPointerTypingAtActiveControl(tabId);
    const keyedText = String(text || "");
    // Hidden inputs take up to 200 keys; a visible text input takes keys only for a short value.
    if (keyedText && keyedText.length <= 200 && /^[ -~]+$/.test(keyedText)) {
      let hiddenProbe = {};
      try {
        const probeRows = await withTimeout(chrome.scripting.executeScript({ target: { tabId }, func: hiddenKeyTargetInPage }), 2500, "hidden target probe timed out");
        hiddenProbe = (probeRows && probeRows[0] && probeRows[0].result) || {};
      } catch (_) {}
      if (hiddenProbe.hidden || (hiddenProbe.typeahead && keyedText.length <= 80)) {
        const keyMode = hiddenProbe.hidden ? "hidden_input_keystrokes" : "typeahead_keystrokes";
        const target = { tabId };
        let keysSent = 0;
        let keyed = false;
        const stillFocused = async () => {
          try {
            const rows = await withTimeout(chrome.scripting.executeScript({ target: { tabId }, func: hiddenKeyTargetFocusedInPage }), 2500, "hidden target focus check timed out");
            return Boolean(rows && rows[0] && rows[0].result);
          } catch (_) {
            return false;
          }
        };
        try {
          await acquireDebugger(target);
          try { keysSent = await dispatchTrustedCharacterKeys(target, keyedText, (count) => { keysSent = count; }, stillFocused); keyed = true; } finally { releaseDebugger(target); }
        } catch (error) {
          // Keys already delivered cannot be taken back: a value set now could show the page the text twice.
          if (error && error.focusMoved) {
            return { ok: false, action: "type", mode: keyMode, failure_reason: "hidden_input_focus_moved", keys_sent: keysSent };
          }
          if (keysSent > 0) {
            return { ok: false, action: "type", mode: keyMode, failure_reason: "hidden_input_keystroke_failed", keys_sent: keysSent };
          }
        }
        if (keyed) {
          await new Promise((resolve) => setTimeout(resolve, 120));
          const keyRows = await withTimeout(
            chrome.scripting.executeScript({ target: { tabId }, func: activeValueEvidenceInPage, args: [keyedText] }),
            2500,
            "active value evidence timed out",
          );
          const keyEvidence = (keyRows && keyRows[0] && keyRows[0].result) || {};
          // Delivered as real keystrokes; whether the page consumed them is not confirmed by the field's value.
          return {
            ok: Boolean(keyEvidence.ok),
            mode: keyMode,
            keys_sent: keysSent,
            before_value: before.before_value || "",
            ...keyEvidence,
            ...(hiddenProbe.hidden
              ? { active_hidden: true, active_hidden_reason: keyEvidence.active_hidden_reason || "hidden" }
              : {}),
          };
        }
      }
    }
    const setRows = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId },
        func: activeValueSetInPage,
        args: [String(text || "")],
      }),
      2500,
      "active value set timed out",
    );
    const setResult = (setRows && setRows[0] && setRows[0].result) || {};
    if (!setResult.ok) return {
      ok: false,
      action: "type",
      mode: "dom_value_replace",
      ...setResult,
      before_value: before.before_value || "",
    };
    await new Promise((resolve) => setTimeout(resolve, 120));
    const rows = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId },
        func: activeValueEvidenceInPage,
        args: [String(text || "")],
      }),
      2500,
      "active value evidence timed out",
    );
    const evidence = (rows && rows[0] && rows[0].result) || {};
    if (evidence.typed_control_replaced) {
      // The set-time read-back belonged to a field that is gone: report it as unverified, never as a match or a mismatch.
      const { active_value: _v, active_value_ledger: _l, active_value_matches_expected: _m, active_has_value: _h, ...rest } = setResult;
      return {
        ok: true,
        mode: "dom_value_replace",
        before_value: before.before_value || "",
        ...rest,
        ...evidence,
      };
    }
    return {
      ok: Boolean(evidence.ok),
      mode: "dom_value_replace",
      before_value: before.before_value || "",
      ...setResult,
      ...evidence,
    };
  } catch (error) {
    return { ok: false, action: "type", mode: "dom_value_replace", error: "Native type failed: " + errorMessage(error) };
  }
}

// Type at the focused field's caret: the text replaces only its current selection and the rest
// of the value stays (a blank inside a textarea, a word in an editor).
async function runVisionInsertAtCaret(tabId, text) {
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: insertAtCaretInPage, args: [String(text || "")] }),
      2500,
      "insert at caret timed out",
    );
    const result = (rows && rows[0] && rows[0].result) || { ok: false, failure_reason: "insert_at_caret_failed" };
    return { ...result, action: "type", mode: "insert_at_caret" };
  } catch (error) {
    return { ok: false, action: "type", mode: "insert_at_caret", failure_reason: "insert_at_caret_failed", error: errorMessage(error) };
  }
}

function insertAtCaretInPage(text) {
  try {
    const deepActive = typeof globalThis.__lumenDeepActiveElement === "function"
      ? globalThis.__lumenDeepActiveElement
      : () => document.activeElement;
    const el = deepActive();
    if (!el) return { ok: false, failure_reason: "no_focused_field" };
    const tag = String(el.tagName || "").toLowerCase();
    const inputType = String((el.getAttribute && el.getAttribute("type")) || "text").toLowerCase();
    if (tag === "input" && inputType === "password") return { ok: false, failure_reason: "control_not_typable" };
    const textual = tag === "textarea"
      || (tag === "input" && ["text", "search", "url", "tel", "email", ""].includes(inputType));
    if (textual) {
      const before = String(el.value || "");
      const start = typeof el.selectionStart === "number" ? el.selectionStart : before.length;
      const end = typeof el.selectionEnd === "number" ? el.selectionEnd : start;
      el.setRangeText(String(text), start, end, "end");
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: String(text) }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      const after = String(el.value || "");
      const inserted = after.slice(start, start + String(text).length) === String(text);
      return {
        ok: inserted && after !== before,
        active_tag: tag,
        active_typable: true,
        value_read_back: true,
        inserted_at: start,
        replaced_length: end - start,
        value_length: after.length,
        failure_reason: inserted ? "" : "insert_not_read_back",
      };
    }
    if (el.isContentEditable) {
      const before = String(el.innerText || "");
      const done = document.execCommand("insertText", false, String(text));
      const after = String(el.innerText || "");
      return {
        ok: Boolean(done) && after !== before && after.includes(String(text)),
        active_tag: tag,
        active_typable: true,
        value_read_back: true,
        value_length: after.length,
        failure_reason: done ? "" : "insert_not_read_back",
      };
    }
    return { ok: false, active_tag: tag, failure_reason: "control_not_typable" };
  } catch (error) {
    return { ok: false, failure_reason: "insert_at_caret_failed", error: String(error && error.message || error) };
  }
}

function focusKeystrokeReceiverInPage(selector) {
  try {
    const queryAll = typeof globalThis.__lumenDeepQueryAll === "function"
      ? globalThis.__lumenDeepQueryAll
      : (value) => Array.from(document.querySelectorAll(value));
    const matches = Array.from(queryAll(String(selector || "")));
    if (matches.length !== 1) return { ok: false, reason: "receiver_not_unique" };
    const el = matches[0];
    const tag = String(el.tagName || "").toLowerCase();
    const type = String(el.getAttribute("type") || "").toLowerCase();
    // Keys have their own meaning on these controls; text typed at them is not input.
    if (["button", "a", "select", "option"].includes(tag) || ["checkbox", "radio", "button", "submit", "file"].includes(type)) {
      return { ok: false, reason: "receiver_not_keyboard_input" };
    }
    if (!(el.tabIndex >= 0)) return { ok: false, reason: "receiver_not_focusable" };
    el.focus({ preventScroll: true });
    const active = document.activeElement;
    return { ok: active === el || (el.contains && el.contains(active)), tag };
  } catch (error) {
    return { ok: false, reason: String(error) };
  }
}

function surfacePointInPage(selector, point) {
  // The point is an offset (screenshot pixels) from the receiver's observed top-left corner, so
  // a scroll since the observation moves it with the receiver. CSS pixels out, and only when
  // the point still lies on the receiver itself.
  try {
    const queryAll = typeof globalThis.__lumenDeepQueryAll === "function"
      ? globalThis.__lumenDeepQueryAll
      : (value) => Array.from(document.querySelectorAll(value));
    const matches = Array.from(queryAll(String(selector || "")));
    if (matches.length !== 1) return { ok: false, reason: "receiver_not_unique" };
    const receiver = matches[0];
    const dpr = window.devicePixelRatio || 1;
    const rect = receiver.getBoundingClientRect();
    const x = Math.round(rect.left + Number(point && point[0] || 0) / dpr);
    const y = Math.round(rect.top + Number(point && point[1] || 0) / dpr);
    const el = typeof globalThis.__lumenDeepElementFromPoint === "function"
      ? globalThis.__lumenDeepElementFromPoint(x, y)
      : document.elementFromPoint(x, y);
    if (!el || !(el === receiver || (receiver.contains && receiver.contains(el)))) {
      return { ok: false, reason: "point_off_receiver" };
    }
    return { ok: true, x, y };
  } catch (error) {
    return { ok: false, reason: String(error) };
  }
}

// Trusted per-character key events (keyDown carrying the character, keyUp) through CDP; the caller holds the debugger.
// With stillFocused, focus is re-checked before every key (a Space on a focused button would click it); a moved focus throws
// (focusMoved, keysSent) so no further key reaches the wrong target.
// One printable ASCII character as a US-layout keystroke: its own text, the physical key's code and
// virtual-key code, and Shift where a keyboard needs it. The chord parser lowercased bare letters
// ("Jersey City, NJ" arrived as "jersey city, nj") and gave symbols their char code as the virtual
// key, which Blink reads as navigation ("$" = Home, "!" = PageUp), scrambling values with symbols.
function printableCharacterKeyDescription(character) {
  if (typeof character !== "string" || character.length !== 1 || !/^[ -~]$/.test(character)) return null;
  const shifted = { "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", "\"": "'", "<": ",", ">": ".", "?": "/", "~": "`" };
  const physical = { "-": ["Minus", 189], "=": ["Equal", 187], "[": ["BracketLeft", 219], "]": ["BracketRight", 221],
    "\\": ["Backslash", 220], ";": ["Semicolon", 186], "'": ["Quote", 222], ",": ["Comma", 188], ".": ["Period", 190],
    "/": ["Slash", 191], "`": ["Backquote", 192] };
  let base = character;
  let shift = false;
  if (/^[A-Z]$/.test(character)) {
    base = character.toLowerCase();
    shift = true;
  } else if (Object.prototype.hasOwnProperty.call(shifted, character)) {
    base = shifted[character];
    shift = true;
  }
  let code;
  let vk;
  if (base === " ") {
    code = "Space";
    vk = 32;
  } else if (/^[a-z]$/.test(base)) {
    code = `Key${base.toUpperCase()}`;
    vk = base.toUpperCase().charCodeAt(0);
  } else if (/^[0-9]$/.test(base)) {
    code = `Digit${base}`;
    vk = base.charCodeAt(0);
  } else if (Object.prototype.hasOwnProperty.call(physical, base)) {
    [code, vk] = physical[base];
  } else {
    return null;
  }
  return {
    key: character,
    code,
    windowsVirtualKeyCode: vk,
    nativeVirtualKeyCode: vk,
    modifiers: shift ? 8 : 0,
    text: character,
    unmodifiedText: character,
  };
}

async function dispatchTrustedCharacterKeys(target, value, onProgress = null, stillFocused = null) {
  let sent = 0;
  for (const character of String(value)) {
    if (stillFocused && !(await stillFocused())) {
      const moved = new Error("focus moved off the hidden input");
      moved.focusMoved = true;
      moved.keysSent = sent;
      throw moved;
    }
    const dispatch = printableCharacterKeyDescription(character) || keyDispatchDescription(character === " " ? "space" : character);
    const { text: keyText, unmodifiedText: _unmodified, ...keyUpDispatch } = dispatch;
    await dispatchInputUnlessDialog(target, "Input.dispatchKeyEvent", { type: keyText ? "keyDown" : "rawKeyDown", ...dispatch });
    await dispatchInputUnlessDialog(target, "Input.dispatchKeyEvent", { type: "keyUp", ...keyUpDispatch });
    sent += 1;
    if (onProgress) onProgress(sent);
    // Checked again after the key: a page handler that moved focus during this key's own dispatch
    // stops the burst here, so at most this one character can have landed elsewhere.
    if (stillFocused && !(await stillFocused())) {
      const moved = new Error("focus moved during a key");
      moved.focusMoved = true;
      moved.keysSent = sent;
      throw moved;
    }
    await new Promise((resolve) => setTimeout(resolve, 15));
  }
  return sent;
}

// The focused typable field is one nobody can see (opacity 0, hidden, zero-size, offscreen) and not a
// credential/payment control: a page that listens to key events (a terminal) only reacts to real keystrokes.
function hiddenKeyTargetInPage() {
  try {
    const el = typeof globalThis.__lumenDeepActiveElement === "function" ? globalThis.__lumenDeepActiveElement() : document.activeElement;
    if (!el) return { hidden: false };
    const tag = String(el.tagName || "").toLowerCase();
    if (tag !== "input" && tag !== "textarea") return { hidden: false };
    const attr = (name) => String((el.getAttribute && el.getAttribute(name)) || "").trim().toLowerCase();
    const type = attr("type");
    if (type === "password" || type === "hidden") return { hidden: false };
    const tokens = attr("autocomplete").split(/\s+/).filter(Boolean);
    if (tokens.some((t) => ["current-password", "new-password", "one-time-code", "transaction-amount", "transaction-currency", "webauthn"].includes(t) || t.startsWith("cc-"))) return { hidden: false };
    let hidden = false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") hidden = true;
    for (let node = el, depth = 0; !hidden && node && node.nodeType === 1 && depth < 12; node = node.parentElement, depth += 1) {
      const opacity = window.getComputedStyle(node).opacity;
      if (opacity !== "" && Number(opacity) === 0) hidden = true;
    }
    const box = el.getBoundingClientRect();
    if (!hidden && (box.width < 2 || box.height < 2 || box.bottom <= 0 || box.right <= 0 || box.top >= window.innerHeight || box.left >= window.innerWidth)) hidden = true;
    // A search-as-you-type field looks suggestions up from key events, and nothing in the markup
    // has to say so (Lever's location box is a plain text input that listens for keyup): a value
    // set without keys never opened its list, and run 1792 retyped the city for minutes. A short
    // value into a single-line text input is typed as real keystrokes, the way a person types.
    const typeahead = !hidden && tag === "input" && ["", "text", "search", "email", "url", "tel"].includes(type);
    if (hidden || typeahead) globalThis.__lumenHiddenKeyTarget = el;
    return { hidden, typeahead };
  } catch (_) {
    return { hidden: false };
  }
}

function hiddenKeyTargetFocusedInPage() {
  try {
    const el = typeof globalThis.__lumenDeepActiveElement === "function" ? globalThis.__lumenDeepActiveElement() : document.activeElement;
    return Boolean(el) && el === globalThis.__lumenHiddenKeyTarget;
  } catch (_) {
    return false;
  }
}

async function typeKeystrokesIntoControl(tabId, selector, text, point = null) {
  const value = String(text || "");
  if (!value || value.length > 200) return null;
  let focused = {};
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: focusKeystrokeReceiverInPage, args: [String(selector || "")] }),
      2500,
      "keystroke receiver focus timed out",
    );
    focused = (rows && rows[0] && rows[0].result) || {};
  } catch (_) {
    return null;
  }
  if (!focused.ok) return null;
  // On a rendered surface (canvas/SVG) the fields exist only as pixels: the typed-at point is
  // clicked first, the way a person clicks into a painted field before typing.
  let surfacePoint = null;
  if (["canvas", "svg"].includes(focused.tag) && Array.isArray(point) && point.length === 2) {
    try {
      const rows = await withTimeout(
        chrome.scripting.executeScript({ target: { tabId }, func: surfacePointInPage, args: [String(selector || ""), point] }),
        2500,
        "surface point timed out",
      );
      surfacePoint = (rows && rows[0] && rows[0].result) || {};
    } catch (error) {
      surfacePoint = { ok: false, reason: errorMessage(error) };
    }
    if (!surfacePoint.ok) {
      return {
        ok: false,
        action: "type",
        mode: "keystrokes_to_focused_control",
        keys_sent: 0,
        failure_reason: surfacePoint.reason === "point_off_receiver" ? "type_point_off_control" : "type_point_unavailable",
      };
    }
  }
  const target = { tabId };
  let sent = 0;
  try {
    await acquireDebugger(target);
    if (surfacePoint) {
      await dispatchInputUnlessDialog(target, "Input.dispatchMouseEvent", { type: "mouseMoved", x: surfacePoint.x, y: surfacePoint.y });
      await dispatchInputUnlessDialog(target, "Input.dispatchMouseEvent", { type: "mousePressed", x: surfacePoint.x, y: surfacePoint.y, button: "left", buttons: 1, clickCount: 1 });
      await dispatchInputUnlessDialog(target, "Input.dispatchMouseEvent", { type: "mouseReleased", x: surfacePoint.x, y: surfacePoint.y, button: "left", buttons: 0, clickCount: 1 });
      await new Promise((resolve) => setTimeout(resolve, 60));
    }
    sent = await dispatchTrustedCharacterKeys(target, value, (count) => { sent = count; });
  } catch (error) {
    return {
      ok: false,
      action: "type",
      mode: "keystrokes_to_focused_control",
      keys_sent: sent,
      failure_reason: "keystroke_dispatch_failed",
      error: errorMessage(error),
    };
  } finally {
    releaseDebugger(target);
  }
  return {
    ok: true,
    action: "type",
    mode: "keystrokes_to_focused_control",
    keys_sent: sent,
    clicked_point_first: Boolean(surfacePoint),
    active_tag: focused.tag || "",
    active_typable: false,
  };
}

async function runVisionTypeControl(
  tabId,
  selector,
  text,
  expectedTarget = null,
  expectedPageContext = null,
  point = null,
  atCaret = false,
) {
  if (!tabId) return { ok: false, action: "type", mode: "control_selector", error: "No application tab." };
  const dialogTyped = typeIntoNativeDialog(tabId, text);
  if (dialogTyped) return dialogTyped;
  if (!clickTargetIdentityComplete(expectedTarget)) {
    return { ok: false, action: "type", mode: "control_selector", failure_reason: "control_identity_required", focus_attempted: false };
  }
  // Same URL and document: content drift elsewhere (tickers, live panels, reshuffled labels) is a page
  // fact, as for ordinary clicks; the field's own identity check below decides. Credentials keep the
  // strict check in their own path.
  const pageVerdict = await observedPageContextVerdict(tabId, expectedPageContext, false);
  if (!pageVerdict.ok) {
    return {
      ok: false,
      action: "type",
      mode: "control_selector",
      failure_reason: pageVerdict.reason,
      page_context_mismatch_field: pageVerdict.mismatch_field || "",
      focus_attempted: false,
    };
  }
  const cleanSelector = String(selector || "");
  if (!cleanSelector) {
    return { ok: false, action: "type", mode: "control_selector", failure_reason: "control_identity_required", focus_attempted: false };
  }
  let focusResult = {};
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: focusControlForTypeInPage, args: [cleanSelector, expectedTarget] }),
      2500,
      "type control focus timed out",
    );
    focusResult = (rows && rows[0] && rows[0].result) || { ok: false, action: "type", failure_reason: "control_focus_failed", error: "type control focus returned nothing" };
  } catch (error) {
    focusResult = { ok: false, action: "type", failure_reason: "control_focus_failed", error: errorMessage(error) };
  }
  if (!focusResult.ok && focusResult.failure_reason === "control_not_found") {
    return {
      ...focusResult,
      ok: false,
      action: "type",
      mode: "control_selector",
      control_selector_resolved: false,
      selector_failure_reason: focusResult.failure_reason || "control_not_found",
      focus_attempted: false,
    };
  }
  if (!focusResult.ok && focusResult.failure_reason === "control_not_typable") {
    // A focusable element that is not a text field (a painted canvas form, a custom widget)
    // takes typing the way a person gives it: focus, then keystrokes.
    const keyed = await typeKeystrokesIntoControl(tabId, cleanSelector, text, point);
    if (keyed) return keyed;
  }
  if (!focusResult.ok) {
    return {
      ok: false,
      action: "type",
      mode: "control_selector",
      control_selector_resolved: true,
      focus_error: focusResult.error || "",
      failure_reason: focusResult.failure_reason || "control_focus_failed",
      // Which identity field the in-page check refused (run 1795: eight first-name refusals said only
      // "could not be focused", so the field that differed could not be read back from the receipt).
      target_identity_mismatch_field: focusResult.target_identity_mismatch_field || "",
      identity_expected: focusResult.identity_expected || "",
      identity_live: focusResult.identity_live || "",
      active_tag: focusResult.active_tag || "",
      active_typable: Boolean(focusResult.active_typable),
      error: focusResult.error || "Type control could not be focused.",
    };
  }
  const typed = atCaret ? await runVisionInsertAtCaret(tabId, text) : await runVisionType(tabId, text);
  // The document state this run's own typing left: a later action in the same decision is bound
  // to it rather than to the pre-typing observation.
  let afterDocumentState = "";
  if (typed && typed.ok) {
    try {
      const rows = await withTimeout(
        chrome.scripting.executeScript({ target: { tabId }, func: documentStateSignatureInPage }),
        1500,
        "document state probe timed out",
      );
      afterDocumentState = String((rows && rows[0] && rows[0].result) || "");
    } catch (_) {}
  }
  return {
    ...typed,
    action: "type",
    mode: "control_selector",
    control_selector_resolved: true,
    focus_target_tag: focusResult.target_tag || "",
    after_document_state_signature: afterDocumentState,
  };
}

function documentStateSignatureInPage() {
  try {
    return typeof globalThis.__lumenDocumentStateSignature === "function"
      ? String(globalThis.__lumenDocumentStateSignature() || "")
      : "";
  } catch (_) {
    return "";
  }
}

function credentialReplayCommandReceipt(rawResult) {
  const raw = rawResult && typeof rawResult === "object" ? rawResult : {};
  const failureReasons = new Set([
    "",
    "control_identity_required",
    "page_context_required",
    "page_context_drift",
    "control_selector_ambiguous",
    "control_not_found",
    "control_target_drift",
    "control_not_typable",
    "control_focus_failed",
    "credential_replay_page_context_drift",
    "credential_replay_target_drift",
    "credential_value_set_failed",
    "credential_event_dispatch_failed",
    "credential_local_verification_failed",
    "credential_replay_failed",
    "credential_replay_post_context_unavailable",
    "credential_replay_post_context_drift",
    "page_context_unavailable",
  ]);
  const pageMismatchFields = new Set([
    "",
    "url",
    "surface_probe",
    "document_lifecycle_token",
    "document_state_signature",
    "stable_surface_signature",
    "surface_signature",
  ]);
  const targetMismatchFields = new Set([
    "",
    "target_selector",
    "target_text",
    "target_tag",
    "target_type",
    "target_role",
    "dispatch_path",
    "page_url",
  ]);
  const fixedEnum = (value, allowed, fallback = "") => {
    const normalized = String(value || "");
    return allowed.has(normalized) ? normalized : fallback;
  };
  const boundedLength = (value) => {
    const numeric = Number(value);
    if (!Number.isFinite(numeric) || numeric < 0) return 0;
    return Math.min(Math.trunc(numeric), 1000000);
  };
  const ok = raw.ok === true;
  const tokenPrefix = (value) => (/^[0-9a-f]{8}$/.test(String(value || "")) ? String(value) : "");
  const activeValidity = typeof raw.active_validity_valid === "boolean"
    ? raw.active_validity_valid
    : null;
  return {
    ok,
    action: "type",
    mode: "credential_atomic_control_selector",
    failure_reason: fixedEnum(
      raw.failure_reason,
      failureReasons,
      ok ? "" : "credential_replay_failed",
    ),
    page_context_mismatch_field: fixedEnum(
      raw.page_context_mismatch_field,
      pageMismatchFields,
    ),
    target_identity_mismatch_field: fixedEnum(
      raw.target_identity_mismatch_field,
      targetMismatchFields,
    ),
    control_selector_resolved: raw.control_selector_resolved === true,
    focus_attempted: raw.focus_attempted === true,
    value_write_attempted: raw.value_write_attempted === true,
    value_verified_locally: raw.value_verified_locally === true,
    active_typable: raw.active_typable === true,
    active_has_value: raw.active_has_value === true,
    active_value_matches_expected: raw.active_value_matches_expected === true,
    active_value_redacted: true,
    active_value_redaction_reason: "credential_replay",
    active_value_length: boundedLength(raw.active_value_length),
    before_has_value: raw.before_has_value === true,
    before_value_length: boundedLength(raw.before_value_length),
    before_value_redacted: raw.before_value_redacted === true,
    active_aria_invalid: (
      raw.active_aria_invalid === true
      || String(raw.active_aria_invalid || "").trim().toLowerCase() === "true"
    ),
    active_validity_valid: activeValidity,
    browser_mutation_attempted: raw.browser_mutation_attempted === true,
    execution_outcome_unknown: raw.execution_outcome_unknown === true,
    sensitive_mask_rebind_required: raw.sensitive_mask_rebind_required === true,
    url_changed: raw.url_changed === true,
    document_lifecycle_changed: raw.document_lifecycle_changed === true,
    lifecycle_expected_prefix: tokenPrefix(raw.lifecycle_expected_prefix),
    lifecycle_live_prefix: tokenPrefix(raw.lifecycle_live_prefix),
  };
}

function credentialKeyBindingStillBoundInPage(bindingId) {
  try {
    const binding = globalThis.__lumenCredentialKeyBinding;
    return Boolean(binding && binding.id === bindingId && binding.stillBound());
  } catch (_) {
    return false;
  }
}

function credentialKeyBindingClearInPage(bindingId) {
  try {
    const binding = globalThis.__lumenCredentialKeyBinding;
    if (binding && binding.id === bindingId) {
      binding.clear();
      globalThis.__lumenCredentialKeyBinding = null;
    }
  } catch (_) {}
  return true;
}

function credentialKeyBindingFinishInPage(bindingId, value) {
  try {
    const binding = globalThis.__lumenCredentialKeyBinding;
    if (!binding || binding.id !== bindingId) {
      return { ok: false, action: "type", failure_reason: "credential_replay_failed" };
    }
    globalThis.__lumenCredentialKeyBinding = null;
    return binding.finish(value);
  } catch (_) {
    return { ok: false, action: "type", failure_reason: "credential_replay_failed" };
  }
}

// A credential typed as real keystrokes: sites that validate on key events (iCIMS's password rules
// read "Password: required" after a value write, run 1794) see a person typing. The replay's own
// checks run first and bind the exact focused target; before every key the binding is re-checked
// (same target focused, same document/frame/URL/lifecycle), and the first failure stops the burst
// and clears the field. Only printable ASCII up to 200 characters is keyed; anything else keeps
// the atomic value write.
// Tabs where a keyed credential burst ever lost focus: they go back to the atomic value write, so a
// frame that steals focus can take at most one character per tab, not one per retry.
const credentialKeystrokeDisabledTabs = new Set();
// Well inside the 19 s command timeout: the burst never outlives the command that started it.
const CREDENTIAL_KEYSTROKE_DEADLINE_MS = 12000;

async function credentialKeystrokeReplay(tabId, selector, text, expectedTarget, pageContext) {
  const deadline = Date.now() + CREDENTIAL_KEYSTROKE_DEADLINE_MS;
  const inPage = async (func, args) => {
    const rows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func, args }),
      3500,
      "credential key step timed out",
    );
    return rows && rows[0] ? rows[0].result : null;
  };
  let prepared = null;
  try {
    prepared = await inPage(replayCredentialControlInPage, [selector, "", expectedTarget, pageContext, true]);
  } catch (_) {
    return { ok: false, failure_reason: "credential_replay_failed" };
  }
  if (!prepared || prepared.ok !== true || prepared.key_binding_prepared !== true) {
    return prepared || { ok: false, failure_reason: "credential_replay_failed" };
  }
  const bindingId = String(prepared.key_binding_id || "");
  const target = { tabId };
  let keysSent = 0;
  let attached = false;
  try {
    await withTimeout(acquireDebugger(target), 3000, "debugger attach timed out");
    attached = true;
    try {
      keysSent = await dispatchTrustedCharacterKeys(
        target,
        text,
        (count) => { keysSent = count; },
        async () => {
          if (Date.now() > deadline) return false;
          try {
            const bound = Boolean(await inPage(credentialKeyBindingStillBoundInPage, [bindingId]));
            // Re-checked after the in-page check, which itself can take seconds on a stalled page.
            return bound && Date.now() <= deadline;
          } catch (_) {
            return false;
          }
        },
      );
    } finally {
      releaseDebugger(target);
    }
  } catch (error) {
    try { await inPage(credentialKeyBindingClearInPage, [bindingId]); } catch (_) {}
    if (error && error.focusMoved && keysSent > 0) credentialKeystrokeDisabledTabs.add(tabId);
    if (!attached) {
      return { ok: false, failure_reason: "credential_replay_failed", control_selector_resolved: true, focus_attempted: true };
    }
    return {
      ok: false,
      failure_reason: error && error.focusMoved ? "credential_replay_target_drift" : "credential_replay_failed",
      control_selector_resolved: true,
      focus_attempted: true,
      value_write_attempted: keysSent > 0,
      browser_mutation_attempted: keysSent > 0,
      execution_outcome_unknown: !(error && error.focusMoved) && keysSent > 0,
    };
  }
  try {
    const finished = await inPage(credentialKeyBindingFinishInPage, [bindingId, text]);
    return finished || { ok: false, failure_reason: "credential_local_verification_failed" };
  } catch (_) {
    try { await inPage(credentialKeyBindingClearInPage, [bindingId]); } catch (_) {}
    return { ok: false, failure_reason: "credential_local_verification_failed", browser_mutation_attempted: true };
  }
}

async function runVisionCredentialTypeControl(
  tabId,
  selector,
  text,
  expectedTarget = null,
  expectedPageContext = null,
) {
  const blocked = (overrides = {}) => ({
    ok: false,
    action: "type",
    mode: "credential_atomic_control_selector",
    control_selector_resolved: false,
    focus_attempted: false,
    value_write_attempted: false,
    value_verified_locally: false,
    active_typable: false,
    active_has_value: false,
    active_value_matches_expected: false,
    active_value_redacted: true,
    active_value_redaction_reason: "credential_replay",
    browser_mutation_attempted: false,
    ...overrides,
  });
  const finish = (result) => credentialReplayCommandReceipt(result);
  if (!tabId) return finish(blocked({ failure_reason: "credential_replay_failed" }));
  if (!clickTargetIdentityComplete(expectedTarget)) {
    return finish(blocked({ failure_reason: "control_identity_required" }));
  }
  const cleanSelector = String(selector || "").trim();
  if (!cleanSelector) {
    return finish(blocked({ failure_reason: "control_identity_required" }));
  }
  // A credential accepts a state drift only when the named field is the sole changed row of a top-document
  // page whose URL, title and lifecycle token still match the observation. A field inside a frame keeps the
  // strict whole-page check: the lifecycle token here belongs to the top document, not to the frame.
  const pageVerdict = await observedPageContextVerdict(
    tabId,
    expectedPageContext,
    true,
    false,
    false,
    { selector: cleanSelector, topDocumentOnly: true },
  );
  if (!pageVerdict.ok) {
    const mismatchField = pageVerdict.mismatch_field || "";
    return finish(blocked({
      failure_reason: pageVerdict.reason,
      page_context_mismatch_field: mismatchField,
      url_changed: mismatchField === "url",
      document_lifecycle_changed: mismatchField === "document_lifecycle_token",
      lifecycle_expected_prefix: pageVerdict.lifecycle_expected_prefix || "",
      lifecycle_live_prefix: pageVerdict.lifecycle_live_prefix || "",
    }));
  }
  const livePreflight = pageVerdict.live && typeof pageVerdict.live === "object"
    ? pageVerdict.live
    : {};
  const atomicPageContext = {
    ...(expectedPageContext && typeof expectedPageContext === "object"
      ? expectedPageContext
      : {}),
    url: String((expectedPageContext || {}).url || livePreflight.url || ""),
    document_lifecycle_token: String(
      (expectedPageContext || {}).document_lifecycle_token
      || livePreflight.document_lifecycle_token
      || "",
    ),
  };
  let result;
  const keyable = /^[ -~]{1,200}$/.test(String(text || "")) && !credentialKeystrokeDisabledTabs.has(tabId);
  if (keyable) {
    result = await credentialKeystrokeReplay(tabId, cleanSelector, String(text), expectedTarget, atomicPageContext);
  } else {
    try {
      const rows = await withTimeout(
        chrome.scripting.executeScript({
          target: { tabId },
          func: replayCredentialControlInPage,
          args: [
            cleanSelector,
            String(text || ""),
            expectedTarget,
            atomicPageContext,
          ],
        }),
        3500,
        "atomic credential replay timed out",
      );
      result = (rows && rows[0] && rows[0].result) || blocked({
        failure_reason: "credential_replay_failed",
      });
    } catch (_) {
      return finish(blocked({ failure_reason: "credential_replay_failed" }));
    }
  }
  if (!result || result.ok !== true) {
    return finish({
      ...blocked(),
      ...(result && typeof result === "object" ? result : {}),
      ok: false,
      action: "type",
      mode: "credential_atomic_control_selector",
      active_typable: false,
      active_has_value: false,
      active_value_matches_expected: false,
      active_value_redacted: true,
      active_value_redaction_reason: "credential_replay",
    });
  }

  // Let a focus/input handler's queued navigation commit, then compare exact document identity.
  // The value was written only to the already-validated target reference inside the prior task.
  await new Promise((resolve) => setTimeout(resolve, 120));
  const post = await tabPageEvidence(tabId);
  const replayUrl = String(result.page_url || atomicPageContext.url || "");
  const postUrl = String(post.url || "");
  const replayLifecycle = String(
    result.document_lifecycle_token
    || atomicPageContext.document_lifecycle_token
    || "",
  );
  const postLifecycle = String(post.document_lifecycle_token || "");
  const urlChanged = Boolean(replayUrl && postUrl && replayUrl !== postUrl);
  const documentLifecycleChanged = Boolean(
    replayLifecycle
    && postLifecycle
    && replayLifecycle !== postLifecycle
  );
  const postContextUnavailable = Boolean(
    !postUrl
    || !postLifecycle
    || !replayLifecycle
  );
  if (urlChanged || documentLifecycleChanged || postContextUnavailable) {
    return finish(blocked({
      failure_reason: (
        postContextUnavailable
          ? "credential_replay_post_context_unavailable"
          : "credential_replay_post_context_drift"
      ),
      page_context_mismatch_field: (
        urlChanged
          ? "url"
          : documentLifecycleChanged
            ? "document_lifecycle_token"
            : ""
      ),
      control_selector_resolved: true,
      focus_attempted: true,
      value_write_attempted: true,
      browser_mutation_attempted: true,
      execution_outcome_unknown: true,
      sensitive_mask_rebind_required: true,
      url_changed: urlChanged,
      document_lifecycle_changed: documentLifecycleChanged,
    }));
  }
  return finish({
    ...result,
    action: "type",
    mode: "credential_atomic_control_selector",
    control_selector_resolved: true,
    active_value_redacted: true,
    active_value_redaction_reason: "credential_replay",
  });
}

function scrollCommandReceipt(rawResult, requestedMode = "") {
  const raw = rawResult && typeof rawResult === "object" ? rawResult : {};
  const fixedEnum = (value, allowed, fallback = "") => {
    const normalized = String(value || "");
    return allowed.has(normalized) ? normalized : fallback;
  };
  const boundedPosition = (value) => {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return 0;
    return Math.max(0, Math.min(Math.round(numeric), 1000000000));
  };
  const modes = new Set(["document_scroll", "element_scroll", "wheel_scroll"]);
  const failureReasons = new Set([
    "",
    "control_identity_required",
    "page_context_required",
    "page_context_drift",
    "control_selector_ambiguous",
    "control_not_found",
    "scroll_target_drift",
    "scroll_container_unavailable",
    "scroll_execution_failed",
    "scroll_execution_outcome_unknown",
  ]);
  const pageMismatchFields = new Set([
    "",
    "url",
    "document_lifecycle_token",
    "document_state_signature",
    "stable_surface_signature",
    "surface_signature",
  ]);
  const targetMismatchFields = new Set([
    "",
    "target_selector",
    "target_text",
    "target_tag",
    "target_type",
    "target_role",
    "dispatch_path",
    "page_url",
    "target_document",
  ]);
  const targetKinds = new Set(["", "page", "container"]);
  const mode = fixedEnum(raw.mode, modes, fixedEnum(requestedMode, modes, "document_scroll"));
  const beforeScrollY = boundedPosition(raw.before_scroll_y);
  const afterScrollY = boundedPosition(raw.scroll_y);
  const beforePosition = boundedPosition(
    raw.before_scroll_position == null ? beforeScrollY : raw.before_scroll_position,
  );
  const afterPosition = boundedPosition(
    raw.scroll_position == null ? afterScrollY : raw.scroll_position,
  );
  const rawContainerHash = String(raw.scroll_container_identity_hash || "").trim().toLowerCase();
  const containerHash = /^[a-f0-9]{16,64}$/.test(rawContainerHash)
    ? rawContainerHash.slice(0, 64)
    : "";
  const ok = raw.ok === true;
  return {
    ok,
    action: "scroll",
    mode,
    failure_reason: fixedEnum(
      raw.failure_reason,
      failureReasons,
      ok ? "" : "scroll_execution_failed",
    ),
    page_context_mismatch_field: fixedEnum(
      raw.page_context_mismatch_field,
      pageMismatchFields,
    ),
    target_identity_mismatch_field: fixedEnum(
      raw.target_identity_mismatch_field,
      targetMismatchFields,
    ),
    scroll_target_kind: fixedEnum(raw.scroll_target_kind, targetKinds),
    scroll_container_identity_hash: containerHash,
    scroll_container_scroll_height: boundedPosition(raw.scroll_container_scroll_height),
    scroll_container_client_height: boundedPosition(raw.scroll_container_client_height),
    scroll_container_scroll_width: boundedPosition(raw.scroll_container_scroll_width),
    scroll_container_client_width: boundedPosition(raw.scroll_container_client_width),
    scroll_axis: fixedEnum(raw.scroll_axis, new Set(["", "vertical", "horizontal"])),
    before_scroll_position: beforePosition,
    scroll_position: afterPosition,
    before_scroll_y: beforeScrollY,
    scroll_y: afterScrollY,
    scroll_changed: beforePosition !== afterPosition,
    scroll_attempted: raw.scroll_attempted === true,
    browser_mutation_attempted: raw.browser_mutation_attempted === true,
    execution_outcome_unknown: raw.execution_outcome_unknown === true,
    target_identity_matched: raw.target_identity_matched === true,
    page_fallback: raw.page_fallback === true,
    url_changed: raw.url_changed === true,
    document_lifecycle_changed: raw.document_lifecycle_changed === true,
    scroll_height: boundedPosition(raw.scroll_height),
    scroll_width: boundedPosition(raw.scroll_width),
    before_scroll_x: boundedPosition(raw.before_scroll_x),
    scroll_x: boundedPosition(raw.scroll_x),
    inner_w: boundedPosition(raw.inner_w),
    inner_h: boundedPosition(raw.inner_h),
  };
}

async function runVisionScroll(
  tabId,
  dy,
  selector = "",
  expectedTarget = null,
  expectedPageContext = null,
  dx = 0,
) {
  if (!tabId) return scrollCommandReceipt({
    ok: false,
    mode: selector ? "element_scroll" : "document_scroll",
    failure_reason: "scroll_execution_failed",
  });
  const exactSelector = String(selector || "").trim();
  if (exactSelector) {
    if (!clickTargetIdentityComplete(expectedTarget)) {
      return scrollCommandReceipt({
        ok: false,
        action: "scroll",
        mode: "element_scroll",
        failure_reason: "control_identity_required",
        scroll_attempted: false,
      }, "element_scroll");
    }
    const expectedAtomicPage = expectedPageContext && typeof expectedPageContext === "object"
      ? expectedPageContext
      : {};
    if (
      !String(expectedAtomicPage.url || "").trim()
      || !String(expectedAtomicPage.document_lifecycle_token || "").trim()
      || !String(expectedAtomicPage.document_state_signature || "").trim()
    ) {
      return scrollCommandReceipt({
        ok: false,
        action: "scroll",
        mode: "element_scroll",
        failure_reason: "page_context_required",
        scroll_attempted: false,
      }, "element_scroll");
    }
    const pageVerdict = await observedPageContextVerdict(tabId, expectedPageContext);
    if (!pageVerdict.ok) {
      return scrollCommandReceipt({
        ok: false,
        action: "scroll",
        mode: "element_scroll",
        failure_reason: pageVerdict.reason,
        page_context_mismatch_field: pageVerdict.mismatch_field || "",
        scroll_attempted: false,
      }, "element_scroll");
    }
    const atomicPageContext = {
      url: String(expectedAtomicPage.url),
      document_lifecycle_token: String(expectedAtomicPage.document_lifecycle_token),
      document_state_signature: String(expectedAtomicPage.document_state_signature),
    };
    let mutationDispatchStarted = false;
    try {
      showPointerScrollHint(tabId, dy, exactSelector);
      const execution = chrome.scripting.executeScript({
          target: { tabId },
          func: scrollTargetInPage,
          args: [exactSelector, Number(dy || 0), expectedTarget, atomicPageContext, Number(dx || 0)],
        });
      mutationDispatchStarted = true;
      const rows = await withTimeout(
        execution,
        2500,
        "targeted scroll timed out",
      );
      const result = rows && rows[0] && rows[0].result;
      if (!result || typeof result !== "object") {
        return scrollCommandReceipt({
          ok: false,
          action: "scroll",
          mode: "element_scroll",
          failure_reason: "scroll_execution_outcome_unknown",
          scroll_attempted: true,
          browser_mutation_attempted: true,
          execution_outcome_unknown: true,
        }, "element_scroll");
      }
      return scrollCommandReceipt(result, "element_scroll");
    } catch (_) {
      return scrollCommandReceipt({
        ok: false,
        action: "scroll",
        mode: "element_scroll",
        failure_reason: mutationDispatchStarted
          ? "scroll_execution_outcome_unknown"
          : "scroll_execution_failed",
        scroll_attempted: mutationDispatchStarted,
        browser_mutation_attempted: mutationDispatchStarted,
        execution_outcome_unknown: mutationDispatchStarted,
      }, "element_scroll");
    }
  }
  let before = {};
  let mutationDispatchStarted = false;
  try {
    const beforeRows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: scrollEvidenceInPage }),
      2500,
      "scroll evidence timed out",
    );
    before = (beforeRows && beforeRows[0] && beforeRows[0].result) || {};
    showPointerScrollHint(tabId, dy, "");
    const execution = chrome.scripting.executeScript({
      target: { tabId },
      func: scrollByInPage,
      args: [Number(dy || 0), Number(dx || 0)],
    });
    mutationDispatchStarted = true;
    const afterRows = await withTimeout(
      execution,
      2500,
      "dom scroll timed out",
    );
    await new Promise((resolve) => setTimeout(resolve, 160));
    const after = afterRows && afterRows[0] && afterRows[0].result;
    if (!after || typeof after !== "object") {
      return scrollCommandReceipt({
        ok: false,
        action: "scroll",
        mode: "document_scroll",
        failure_reason: "scroll_execution_outcome_unknown",
        scroll_attempted: true,
        browser_mutation_attempted: true,
        execution_outcome_unknown: true,
        before_scroll_y: before.scroll_y,
      }, "document_scroll");
    }
    return scrollCommandReceipt({
      ...after,
      ok: after.ok === true,
      mode: "document_scroll",
      before_scroll_y: before.scroll_y,
    }, "document_scroll");
  } catch (_) {
    return scrollCommandReceipt({
      ok: false,
      action: "scroll",
      mode: "document_scroll",
      failure_reason: mutationDispatchStarted
        ? "scroll_execution_outcome_unknown"
        : "scroll_execution_failed",
      scroll_attempted: mutationDispatchStarted,
      browser_mutation_attempted: mutationDispatchStarted,
      execution_outcome_unknown: mutationDispatchStarted,
      before_scroll_y: before.scroll_y,
    }, "document_scroll");
  }
}

function wheelPointEvidenceInPage(imgX, imgY) {
  try {
    const dpr = window.devicePixelRatio || 1;
    const x = Math.round(imgX / dpr), y = Math.round(imgY / dpr);
    const deepElementFromPoint = (px, py) => (
      typeof globalThis.__lumenDeepElementFromPoint === "function"
        ? globalThis.__lumenDeepElementFromPoint(px, py)
        : document.elementFromPoint(px, py)
    );
    let node = deepElementFromPoint(x, y);
    let container = null;
    for (let depth = 0; node && depth < 40; depth += 1) {
      try {
        const style = window.getComputedStyle(node);
        const canScrollY = /(auto|scroll)/.test(style.overflowY || "") && node.scrollHeight > node.clientHeight + 1;
        const canScrollX = /(auto|scroll)/.test(style.overflowX || "") && node.scrollWidth > node.clientWidth + 1;
        if (canScrollY || canScrollX) { container = node; break; }
      } catch (_) {}
      node = node.parentElement || (node.getRootNode && node.getRootNode().host) || null;
    }
    const out = {
      ok: true,
      x,
      y,
      dpr,
      scroll_y: Math.round(window.scrollY || 0),
      scroll_x: Math.round(window.scrollX || 0),
      scroll_width: Math.round(document.documentElement.scrollWidth || 0),
      inner_w: window.innerWidth,
      target_kind: container ? "container" : "page",
    };
    if (container) {
      out.container_scroll_top = Math.round(container.scrollTop || 0);
      out.container_scroll_left = Math.round(container.scrollLeft || 0);
      out.container_scroll_height = Math.round(container.scrollHeight || 0);
      out.container_client_height = Math.round(container.clientHeight || 0);
      out.container_scroll_width = Math.round(container.scrollWidth || 0);
      out.container_client_width = Math.round(container.clientWidth || 0);
      out.container_tag = String(container.tagName || "").toLowerCase();
      out.container_id = String(container.id || "").slice(0, 120);
    }
    return out;
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

async function runVisionWheel(tabId, imgX, imgY, dx, dy, wheelModifiers = null) {
  // A mouse wheel at a point scrolls whatever scrollable region sits under the pointer - the
  // innermost container, or the page - exactly as a person's wheel does. No selector, no
  // container heuristics: the browser's own hit-testing decides. Modifiers ride on the wheel
  // (ctrl+wheel zooms a map or canvas, shift+wheel scrolls sideways in many apps).
  const modifiers = gestureModifierBits(wheelModifiers);
  if (!tabId) return scrollCommandReceipt({ ok: false, mode: "wheel_scroll", failure_reason: "scroll_execution_failed" }, "wheel_scroll");
  const target = { tabId };
  let attached = false;
  let before = {};
  let dispatched = false;
  try {
    const beforeRows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: wheelPointEvidenceInPage, args: [Number(imgX || 0), Number(imgY || 0)] }),
      2500,
      "wheel point evidence timed out",
    );
    before = (beforeRows && beforeRows[0] && beforeRows[0].result) || {};
    if (!before.ok) throw new Error(before.error || "wheel point evidence unavailable");
    showPointerScrollHint(tabId, Number(dy || 0), "");
    await acquireDebugger(target);
    attached = true;
    await _cdp(target, "Input.dispatchMouseEvent", { type: "mouseMoved", x: before.x, y: before.y });
    dispatched = true;
    await _cdp(target, "Input.dispatchMouseEvent", {
      type: "mouseWheel",
      x: before.x,
      y: before.y,
      deltaX: Number(dx || 0),
      deltaY: Number(dy || 0),
      modifiers,
    });
    await new Promise((resolve) => setTimeout(resolve, 220));
    const afterRows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: wheelPointEvidenceInPage, args: [Number(imgX || 0), Number(imgY || 0)] }),
      2500,
      "post-wheel evidence timed out",
    );
    const after = (afterRows && afterRows[0] && afterRows[0].result) || {};
    const wheelHorizontal = Number(dx || 0) !== 0 && Number(dy || 0) === 0;
    const beforePos = before.target_kind === "container" ? before.container_scroll_top : before.scroll_y;
    const afterPos = before.target_kind === "container"
      ? (after.target_kind === "container" ? after.container_scroll_top : after.scroll_y)
      : after.scroll_y;
    const beforeX = before.target_kind === "container" ? before.container_scroll_left : before.scroll_x;
    const afterX = before.target_kind === "container"
      ? (after.target_kind === "container" ? after.container_scroll_left : after.scroll_x)
      : after.scroll_x;
    const moved = Number(beforePos || 0) !== Number(afterPos || 0) || Number(beforeX || 0) !== Number(afterX || 0);
    return scrollCommandReceipt({
      ok: moved || Boolean(modifiers),
      action: "scroll",
      mode: "wheel_scroll",
      failure_reason: moved || modifiers ? "" : "scroll_execution_failed",
      wheel_modifiers: modifiers,
      scroll_attempted: true,
      browser_mutation_attempted: true,
      scroll_target_kind: before.target_kind || "page",
      before_scroll_y: before.scroll_y,
      scroll_y: after.scroll_y,
      before_scroll_x: before.scroll_x,
      scroll_x: after.scroll_x,
      scroll_axis: wheelHorizontal ? "horizontal" : "vertical",
      before_scroll_position: wheelHorizontal ? beforeX : beforePos,
      scroll_position: wheelHorizontal ? afterX : afterPos,
      scroll_width: before.scroll_width,
      inner_w: before.inner_w,
      scroll_container_scroll_height: before.container_scroll_height,
      scroll_container_client_height: before.container_client_height,
      scroll_container_scroll_width: before.container_scroll_width,
      scroll_container_client_width: before.container_client_width,
      wheel_x: before.x,
      wheel_y: before.y,
    }, "wheel_scroll");
  } catch (error) {
    return scrollCommandReceipt({
      ok: false,
      action: "scroll",
      mode: "wheel_scroll",
      failure_reason: dispatched ? "scroll_execution_outcome_unknown" : "scroll_execution_failed",
      scroll_attempted: dispatched,
      browser_mutation_attempted: dispatched,
      execution_outcome_unknown: dispatched,
      before_scroll_y: before.scroll_y,
      error: errorMessage(error),
    }, "wheel_scroll");
  } finally {
    if (attached) {
      try {
        releaseDebugger(target);
      } catch (_) {}
    }
  }
}

function pointerPointsInPage(points) {
  // Screenshot (image) pixels -> CSS pixels for CDP input, plus the raw element under each point.
  try {
    const dpr = window.devicePixelRatio || 1;
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const deepElementFromPoint = (px, py) => (
      typeof globalThis.__lumenDeepElementFromPoint === "function"
        ? globalThis.__lumenDeepElementFromPoint(px, py)
        : document.elementFromPoint(px, py)
    );
    return {
      ok: true,
      points: (Array.isArray(points) ? points : []).map((point) => {
        const x = Math.round(Number(point && point[0] || 0) / dpr);
        const y = Math.round(Number(point && point[1] || 0) / dpr);
        const el = deepElementFromPoint(x, y);
        return {
          x,
          y,
          tag: el && el.tagName ? String(el.tagName).toLowerCase() : "",
          text: el ? clean(el.innerText || el.textContent || "").slice(0, 80) : "",
        };
      }),
    };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

function gestureModifierBits(modifiers) {
  // CDP modifier flags for keys held during one pointer gesture (Alt 1, Ctrl 2, Meta 4, Shift 8).
  return (Array.isArray(modifiers) ? modifiers : []).reduce(
    (bits, name) => bits | ({ alt: 1, ctrl: 2, meta: 4, shift: 8 }[String(name || "").toLowerCase()] || 0),
    0,
  );
}

function selectTextPointsInPage(text, imgX, imgY) {
  // Where a person would press and release to drag-select exactly this text: the occurrence nearest
  // the model's point, a quarter of a character in from its first character's left edge and its last
  // character's right edge (the caret lands on the nearest boundary). Read-only.
  try {
    const needle = String(text || "").replace(/\s+/g, " ").trim();
    if (!needle) return { ok: false, failure_reason: "select_text_empty" };
    const dpr = window.devicePixelRatio || 1;
    const px = Number(imgX || 0) / dpr;
    const py = Number(imgY || 0) / dpr;
    const nodes = [];
    const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const parent = node.parentElement;
        if (!parent || !node.nodeValue) return NodeFilter.FILTER_REJECT;
        if (parent.closest("script, style, noscript, template, textarea")) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    let whole = "";
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      nodes.push({ node, start: whole.length });
      whole += node.nodeValue;
    }
    const locate = (index) => {
      let lo = 0;
      let hi = nodes.length - 1;
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (nodes[mid].start <= index) lo = mid; else hi = mid - 1;
      }
      return { node: nodes[lo].node, offset: index - nodes[lo].start };
    };
    const escaped = needle.split(" ").map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s+");
    const pattern = new RegExp(escaped, "g");
    const charRect = (index) => {
      const at = locate(index);
      const range = document.createRange();
      range.setStart(at.node, at.offset);
      range.setEnd(at.node, Math.min(at.offset + 1, at.node.nodeValue.length));
      const rects = Array.from(range.getClientRects()).filter((r) => r.width > 0 && r.height > 0);
      return rects[0] || null;
    };
    const candidates = [];
    let match;
    while ((match = pattern.exec(whole)) && candidates.length < 50) {
      const first = charRect(match.index);
      const last = charRect(match.index + match[0].length - 1);
      if (!first || !last) continue;
      const visible = (r) => r.bottom > 0 && r.right > 0 && r.top < window.innerHeight && r.left < window.innerWidth;
      if (!visible(first) || !visible(last)) continue;
      const cx = (first.left + last.right) / 2;
      const cy = (first.top + last.bottom) / 2;
      candidates.push({
        distance: Math.hypot(cx - px, cy - py),
        from: { x: first.left + first.width * 0.25, y: first.top + first.height / 2 },
        to: { x: last.right - last.width * 0.25, y: last.top + last.height / 2 },
      });
    }
    if (!candidates.length) return { ok: false, failure_reason: "select_text_not_found", occurrences: 0 };
    candidates.sort((a, b) => a.distance - b.distance);
    return { ok: true, from: candidates[0].from, to: candidates[0].to, occurrences: candidates.length, distance: candidates[0].distance };
  } catch (error) {
    return { ok: false, failure_reason: "select_text_failed", error: String(error) };
  }
}

function selectTextInFieldInPage(text, imgX, imgY, apply = true) {
  // Text inside an input or textarea is the field's value, not page text: select exactly the occurrence
  // on the line nearest the model's point with the field's own selection, so a following type at the
  // caret replaces only it (v4_text run 1681: a word in a 160-line textarea).
  try {
    const needle = String(text || "").replace(/\s+/g, " ").trim();
    if (!needle) return { ok: false, failure_reason: "select_text_empty" };
    const dpr = window.devicePixelRatio || 1;
    const px = Number(imgX || 0) / dpr;
    const py = Number(imgY || 0) / dpr;
    const escaped = needle.split(" ").map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s+");
    const fields = Array.from(document.querySelectorAll("textarea, input:not([type]), input[type=text], input[type=search], input[type=url], input[type=email]"))
      .filter((el) => !el.disabled && String(el.value || "").length);  // a read-only field selects too; nothing changes
    const visible = (r) => r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < window.innerHeight && r.left < window.innerWidth;
    let best = null;
    for (const el of fields) {
      const rect = el.getBoundingClientRect();
      if (!visible(rect)) continue;
      const value = String(el.value || "");
      const pattern = new RegExp(escaped, "g");
      const style = window.getComputedStyle(el);
      const fontSize = parseFloat(style.fontSize) || 16;
      const lineHeight = parseFloat(style.lineHeight) || fontSize * 1.2;
      const top = rect.top + (parseFloat(style.paddingTop) || 0) + (parseFloat(style.borderTopWidth) || 0);
      const targetLine = Math.floor(((py - top) + (el.scrollTop || 0)) / lineHeight);
      const dx = px < rect.left ? rect.left - px : px > rect.right ? px - rect.right : 0;
      let match;
      while ((match = pattern.exec(value))) {
        const line = (value.slice(0, match.index).match(/\n/g) || []).length;
        // Where the occurrence's line sits on screen (a textarea's lines scroll; an input is one line).
        const lineCenter = el.tagName === "TEXTAREA"
          ? top + (line + 0.5) * lineHeight - (el.scrollTop || 0)
          : rect.top + rect.height / 2;
        const distance = Math.hypot(dx, py - Math.max(rect.top, Math.min(rect.bottom, lineCenter)));
        const score = distance + (el.tagName === "TEXTAREA" ? Math.abs(line - targetLine) * 0.001 : 0);
        if (!best || score < best.score) best = { el, start: match.index, end: match.index + match[0].length, score, distance };
      }
    }
    if (!best) return { ok: false, failure_reason: "select_text_not_found", occurrences: 0 };
    if (!apply) return { ok: true, field: true, distance: best.distance };
    best.el.focus({ preventScroll: true });
    best.el.setSelectionRange(best.start, best.end);
    const selected = String(best.el.value || "").slice(best.el.selectionStart, best.el.selectionEnd);
    return { ok: true, field: true, selected_text: selected.slice(0, 300), target_tag: best.el.tagName.toLowerCase() };
  } catch (error) {
    return { ok: false, failure_reason: "select_text_failed", error: String(error) };
  }
}

function currentSelectionTextInPage() {
  try {
    return String(window.getSelection ? window.getSelection() : "").slice(0, 2000);
  } catch (_) {
    return "";
  }
}

async function runVisionSelectText(tabId, text, imgX, imgY) {
  // Select exactly this page text the way a person does, with a real pointer drag, aimed at its
  // characters' own edges rather than at pixels guessed from a screenshot (v4_selection run 1679 and
  // v4_text run 1678 kept landing a character or a word off). The selection is read back.
  if (!tabId) return { ok: false, action: "select_text", failure_reason: "select_text_failed", error: "No application tab." };
  const target = { tabId };
  let attached = false;
  let pressed = false;
  let lastPoint = null;
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: selectTextPointsInPage, args: [String(text || ""), Number(imgX || 0), Number(imgY || 0)] }),
      2500,
      "select text evidence timed out",
    );
    const points = (rows && rows[0] && rows[0].result) || {};
    // The text may also be a form field's value (v4_text run 1683: "hold" stood both in the page's
    // instructions and on the textarea line the model meant): the occurrence nearest the point wins.
    let fieldNearer = false;
    if (points.ok) {
      try {
        const probeRows = await withTimeout(
          chrome.scripting.executeScript({ target: { tabId }, func: selectTextInFieldInPage, args: [String(text || ""), Number(imgX || 0), Number(imgY || 0), false] }),
          2500,
          "field text probe timed out",
        );
        const probe = (probeRows && probeRows[0] && probeRows[0].result) || {};
        fieldNearer = Boolean(probe.ok && Number(probe.distance) < Number(points.distance));
      } catch (_) {}
    }
    if ((!points.ok && points.failure_reason === "select_text_not_found") || fieldNearer) {
      // A form field's value.
      const fieldRows = await withTimeout(
        chrome.scripting.executeScript({ target: { tabId }, func: selectTextInFieldInPage, args: [String(text || ""), Number(imgX || 0), Number(imgY || 0)] }),
        2500,
        "field text selection timed out",
      );
      const field = (fieldRows && fieldRows[0] && fieldRows[0].result) || {};
      if (field.ok) {
        const squashField = (value) => String(value || "").replace(/\s+/g, " ").trim();
        const fieldMatches = squashField(field.selected_text) === squashField(text);
        return {
          ok: fieldMatches,
          action: "select_text",
          mode: "field_selection",
          activation_attempted: true,
          activation_dispatched: true,
          selected_text: field.selected_text,
          selection_matches_expected: fieldMatches,
          target_tag: field.target_tag,
          ...(fieldMatches ? {} : { failure_reason: "select_text_mismatch" }),
        };
      }
    }
    if (!points.ok) {
      return { ok: false, action: "select_text", failure_reason: points.failure_reason || "select_text_failed", occurrences: points.occurrences || 0, activation_attempted: false };
    }
    const from = { x: Math.round(points.from.x), y: Math.round(points.from.y) };
    const to = { x: Math.round(points.to.x), y: Math.round(points.to.y) };
    await animatePointerToPoint(tabId, from.x, from.y, 120);
    await acquireDebugger(target);
    attached = true;
    await _cdp(target, "Input.dispatchMouseEvent", { type: "mouseMoved", x: from.x, y: from.y });
    await _cdp(target, "Input.dispatchMouseEvent", { type: "mousePressed", x: from.x, y: from.y, button: "left", buttons: 1, clickCount: 1 });
    pressed = true;
    lastPoint = from;
    const steps = Math.max(6, Math.min(30, Math.round(Math.hypot(to.x - from.x, to.y - from.y) / 12)));
    for (let step = 1; step <= steps; step += 1) {
      const x = Math.round(from.x + ((to.x - from.x) * step) / steps);
      const y = Math.round(from.y + ((to.y - from.y) * step) / steps);
      await _cdp(target, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "left", buttons: 1 });
      lastPoint = { x, y };
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await _cdp(target, "Input.dispatchMouseEvent", { type: "mouseReleased", x: to.x, y: to.y, button: "left", buttons: 0, clickCount: 1 });
    pressed = false;
    const readRows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: currentSelectionTextInPage }),
      1500,
      "selection read-back timed out",
    );
    const selected = String((readRows && readRows[0] && readRows[0].result) || "");
    const squash = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const matches = squash(selected) === squash(text);
    return {
      ok: matches,
      action: "select_text",
      mode: "pointer_drag_select",
      activation_attempted: true,
      activation_dispatched: true,
      occurrences: points.occurrences || 1,
      selected_text: selected.slice(0, 300),
      selection_matches_expected: matches,
      ...(matches ? {} : { failure_reason: "select_text_mismatch" }),
    };
  } catch (error) {
    return {
      ok: false,
      action: "select_text",
      failure_reason: pressed ? "select_text_outcome_unknown" : "select_text_failed",
      activation_attempted: pressed,
      error: errorMessage(error),
    };
  } finally {
    if (attached) {
      if (pressed) {
        const at = lastPoint || { x: 0, y: 0 };
        try { await _cdp(target, "Input.dispatchMouseEvent", { type: "mouseReleased", x: at.x, y: at.y, button: "left", buttons: 0, clickCount: 1 }); } catch (_) {}
      }
      try { releaseDebugger(target); } catch (_) {}
    }
  }
}

async function runVisionDrag(tabId, imgX, imgY, toImgX, toImgY, button = "left", waypoints = null, gesture = null) {
  // Press, move in steps, release: sliders, canvases, and text selection see a real stream of
  // mousemoves. When the press starts a native HTML5 drag, Chrome hands the drag to us
  // (Input.setInterceptDrags) and the drop is dispatched at the release point with that drag's
  // own data, the way a person's drop lands. Modifiers are held for the whole gesture (ctrl-drag
  // copies, shift-drag constrains); hold_ms waits pressed before moving (long-press-to-drag).
  if (!tabId) return { ok: false, action: "drag", failure_reason: "drag_execution_failed", error: "No application tab." };
  const modifiers = gestureModifierBits(gesture && gesture.modifiers);
  const holdMs = Math.max(0, Math.min(5000, Math.round(Number((gesture && gesture.hold_ms) || 0))));
  const target = { tabId };
  let attached = false;
  let pressed = false;
  // Where the held pointer is and which button holds it, so a failed gesture releases in place.
  let lastPoint = null;
  let releaseButton = "left";
  let dragData = null;
  const onEvent = (source, method, params) => {
    if (source && Number(source.tabId) === Number(tabId) && method === "Input.dragIntercepted") {
      dragData = (params && params.data) || null;
    }
  };
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId },
        func: pointerPointsInPage,
        args: [[[imgX, imgY], ...(Array.isArray(waypoints) ? waypoints.slice(0, 40) : []), [toImgX, toImgY]]],
      }),
      2500,
      "drag point evidence timed out",
    );
    const points = (rows && rows[0] && rows[0].result) || {};
    if (!points.ok || !Array.isArray(points.points) || points.points.length < 2) {
      throw new Error(points.error || "drag points unavailable");
    }
    const route = points.points;
    const from = route[0];
    const to = route[route.length - 1];
    // A press and release over the same final control is a click, and a release over one commits it
    // just the same: both ends get the click path's structural final-submit probe (a dry run, nothing
    // is dispatched), so a drag is never a way around a refused click.
    for (const [endName, ex, ey] of [["press", imgX, imgY], ["release", toImgX, toImgY]]) {
      let probeResult = null;
      try {
        const probeRows = await withTimeout(
          chrome.scripting.executeScript({
            target: { tabId },
            func: visionClickAtPointInPage,
            args: [ex, ey, false, false, null],
          }),
          2500,
          "drag endpoint probe timed out",
        );
        probeResult = (probeRows && probeRows[0] && probeRows[0].result) || null;
      } catch (_) {
        probeResult = null;
      }
      if (probeResult && probeResult.blocked_submit) {
        return {
          ok: false,
          action: "drag",
          failure_reason: "drag_endpoint_final_control",
          drag_endpoint: endName,
          target_label: probeResult.target_label || probeResult.label || "",
          activation_attempted: false,
          activation_dispatched: false,
          activation_count: 0,
        };
      }
    }
    const before = await tabPageEvidence(tabId);
    const gestureButton = ["left", "right", "middle"].includes(String(button || "")) ? String(button) : "left";
    const buttons = { left: 1, right: 2, middle: 4 }[gestureButton];
    releaseButton = gestureButton;
    await animatePointerToPoint(tabId, from.x, from.y, 120);
    await acquireDebugger(target);
    attached = true;
    chrome.debugger.onEvent.addListener(onEvent);
    let interceptEnabled = false;
    try {
      await _cdp(target, "Input.setInterceptDrags", { enabled: true });
      interceptEnabled = true;
    } catch (_) {}
    await _cdp(target, "Input.dispatchMouseEvent", { type: "mouseMoved", x: from.x, y: from.y, modifiers });
    await _cdp(target, "Input.dispatchMouseEvent", { type: "mousePressed", x: from.x, y: from.y, button: gestureButton, buttons, clickCount: 1, modifiers });
    pressed = true;
    lastPoint = { x: from.x, y: from.y };
    if (holdMs > 0) await new Promise((resolve) => setTimeout(resolve, holdMs));
    // The held pointer follows the route segment by segment (a straight drag is one segment).
    let distance = 0;
    for (let leg = 1; leg < route.length && !dragData; leg += 1) {
      const start = route[leg - 1];
      const end = route[leg];
      const legLength = Math.hypot(end.x - start.x, end.y - start.y);
      distance += legLength;
      const steps = Math.max(route.length > 2 ? 4 : 8, Math.min(40, Math.round(legLength / 12)));
      for (let step = 1; step <= steps && !dragData; step += 1) {
        const x = Math.round(start.x + ((end.x - start.x) * step) / steps);
        const y = Math.round(start.y + ((end.y - start.y) * step) / steps);
        await _cdp(target, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: gestureButton, buttons, modifiers });
        lastPoint = { x, y };
        await new Promise((resolve) => setTimeout(resolve, 12));
      }
    }
    if (!dragData && interceptEnabled) await new Promise((resolve) => setTimeout(resolve, 60));
    let mode = "pointer_drag";
    if (dragData) {
      mode = "native_drag_and_drop";
      for (const type of ["dragEnter", "dragOver"]) {
        await _cdp(target, "Input.dispatchDragEvent", { type, x: to.x, y: to.y, data: dragData, modifiers });
      }
      await _cdp(target, "Input.dispatchDragEvent", { type: "drop", x: to.x, y: to.y, data: dragData, modifiers });
    }
    await _cdp(target, "Input.dispatchMouseEvent", { type: "mouseReleased", x: to.x, y: to.y, button: gestureButton, buttons: 0, clickCount: 1, modifiers });
    pressed = false;
    try {
      await withTimeout(
        chrome.scripting.executeScript({ target: { tabId }, func: domQuietInPage, args: [200, 800] }),
        1200,
        "post-drag settle timed out",
      );
    } catch (_) {}
    const after = await tabPageEvidence(tabId);
    return {
      ok: true,
      action: "drag",
      mode,
      activation_attempted: true,
      activation_dispatched: true,
      drag_from_tag: from.tag,
      drag_to_tag: to.tag,
      drag_distance_px: Math.round(distance),
      drag_modifiers: modifiers,
      drag_hold_ms: holdMs,
      native_drag_intercepted: Boolean(dragData),
      url_changed: Boolean(before.url && after.url && before.url !== after.url && String(before.url).split("#")[0] !== String(after.url).split("#")[0]),
      surface_changed: Boolean(
        before.surface_signature && after.surface_signature && before.surface_signature !== after.surface_signature,
      ),
    };
  } catch (error) {
    return {
      ok: false,
      action: "drag",
      failure_reason: pressed ? "drag_execution_outcome_unknown" : "drag_execution_failed",
      activation_attempted: pressed,
      error: errorMessage(error),
    };
  } finally {
    try { chrome.debugger.onEvent.removeListener(onEvent); } catch (_) {}
    if (attached) {
      if (pressed) {
        // Release where the pointer is: a mouseup at the viewport corner would drop a slider to 0 or
        // land a native drop on whatever sits there.
        const at = lastPoint || { x: 0, y: 0 };
        try { await _cdp(target, "Input.dispatchMouseEvent", { type: "mouseReleased", x: at.x, y: at.y, button: releaseButton, buttons: 0, clickCount: 1 }); } catch (_) {}
      }
      try { await _cdp(target, "Input.setInterceptDrags", { enabled: false }); } catch (_) {}
      try { releaseDebugger(target); } catch (_) {}
    }
  }
}

async function runVisionHover(tabId, imgX, imgY, dwellMs = 0) {
  // Move the pointer and leave it there: tooltips, hover menus, and hover states. dwellMs rests
  // it there first, for a tooltip or menu that opens only after a delay.
  if (!tabId) return { ok: false, action: "hover", failure_reason: "hover_execution_failed", error: "No application tab." };
  const dwell = Math.max(0, Math.min(5000, Math.round(Number(dwellMs) || 0)));
  const target = { tabId };
  let attached = false;
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: pointerPointsInPage, args: [[[imgX, imgY]]] }),
      2500,
      "hover point evidence timed out",
    );
    const points = (rows && rows[0] && rows[0].result) || {};
    if (!points.ok || !Array.isArray(points.points) || !points.points.length) throw new Error(points.error || "hover point unavailable");
    const point = points.points[0];
    const before = await tabPageEvidence(tabId);
    await animatePointerToPoint(tabId, point.x, point.y, 120);
    await acquireDebugger(target);
    attached = true;
    await _cdp(target, "Input.dispatchMouseEvent", { type: "mouseMoved", x: point.x, y: point.y });
    if (dwell > 0) await new Promise((resolve) => setTimeout(resolve, dwell));
    try {
      await withTimeout(
        chrome.scripting.executeScript({ target: { tabId }, func: domQuietInPage, args: [200, 800] }),
        1200,
        "post-hover settle timed out",
      );
    } catch (_) {}
    const after = await tabPageEvidence(tabId);
    return {
      ok: true,
      action: "hover",
      hover_tag: point.tag,
      dwell_ms: dwell,
      surface_changed: Boolean(
        before.surface_signature && after.surface_signature && before.surface_signature !== after.surface_signature,
      ),
    };
  } catch (error) {
    return { ok: false, action: "hover", failure_reason: "hover_execution_failed", error: errorMessage(error) };
  } finally {
    if (attached) {
      try { releaseDebugger(target); } catch (_) {}
    }
  }
}

function scrollToTextInPage(query, occurrence) {
  // The browser's Find, as a body capability: locate the innermost rendered elements whose own
  // text contains the model's exact text and bring the chosen one into view, scrolling every
  // enclosing region (nested panels, lists, the page). Nothing is interpreted: the model names
  // the text; the body only finds and scrolls.
  try {
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const wanted = clean(query).toLowerCase();
    if (!wanted) return { ok: false, action: "scroll", mode: "scroll_to_text", failure_reason: "scroll_text_required" };
    const all = typeof globalThis.__lumenDeepQueryAll === "function"
      ? globalThis.__lumenDeepQueryAll("*")
      : Array.from(document.querySelectorAll("*"));
    const skip = new Set(["html", "head", "body", "script", "style", "noscript", "template", "svg", "path"]);
    const matches = [];
    for (const el of all) {
      const tag = String(el.tagName || "").toLowerCase();
      if (!tag || skip.has(tag)) continue;
      const own = clean(el.textContent || (el.getAttribute && el.getAttribute("aria-label")) || "").toLowerCase();
      if (!own.includes(wanted)) continue;
      const childHasIt = Array.from(el.children || []).some((child) => clean(child.textContent).toLowerCase().includes(wanted));
      if (childHasIt) continue;
      let style = null;
      try { style = window.getComputedStyle(el); } catch (_) { style = null; }
      const rect = el.getBoundingClientRect();
      if (!style || style.display === "none" || style.visibility === "hidden" || (rect.width === 0 && rect.height === 0)) continue;
      matches.push(el);
      if (matches.length >= 200) break;
    }
    if (!matches.length) {
      return { ok: false, action: "scroll", mode: "scroll_to_text", failure_reason: "scroll_text_not_found", matches: 0 };
    }
    // A visible interactive control whose label holds the text (a button, link, input, or role=*
    // widget) comes before prose that merely mentions it: the model scrolling to "Send to review"
    // means the button, not the intro sentence that names it. Structure only; document order is
    // kept inside each group, and the text-only matches follow as the fallback.
    const CONTROL_SELECTOR = "button,a[href],input,select,textarea,summary,[role='button'],[role='link'],[role='checkbox'],[role='radio'],[role='switch'],[role='tab'],[role='menuitem'],[role='menuitemcheckbox'],[role='menuitemradio'],[role='option'],[role='combobox'],[role='treeitem']";
    const controlMatches = [];
    const proseMatches = [];
    for (const el of matches) {
      let control = null;
      try { control = el.closest ? el.closest(CONTROL_SELECTOR) : null; } catch (_) { control = null; }
      if (control && !controlMatches.includes(control)) controlMatches.push(control);
      else if (!control) proseMatches.push(el);
    }
    matches.length = 0;
    matches.push(...controlMatches, ...proseMatches);
    const controlCount = controlMatches.length;
    const index = Math.min(Math.max(1, Math.round(Number(occurrence) || 1)), matches.length) - 1;
    const target = matches[index];
    // On screen means inside the viewport AND inside every ancestor that clips its overflow: an
    // option scrolled out of its list's visible window is not in view, whatever its box says.
    const visibleOnScreen = (el) => {
      const box = el.getBoundingClientRect();
      if (!(box.bottom > 0 && box.right > 0 && box.top < window.innerHeight && box.left < window.innerWidth)) return false;
      for (let node = el.parentElement; node && node !== document.body && node !== document.documentElement; node = node.parentElement) {
        let clip = null;
        try { clip = window.getComputedStyle(node); } catch (_) { clip = null; }
        if (!clip) continue;
        const clips = [clip.overflowX, clip.overflowY].some((value) => ["hidden", "scroll", "auto", "clip"].includes(value));
        if (!clips) continue;
        const frame = node.getBoundingClientRect();
        if (box.bottom <= frame.top || box.top >= frame.bottom || box.right <= frame.left || box.left >= frame.right) return false;
      }
      return true;
    };
    // Every match's own text and whether it was already on screen, so the model can pick the
    // occurrence it means (the instruction naming the text vs the row that holds it).
    const matchTexts = matches.slice(0, 6).map((el, i) => ({
      occurrence: i + 1,
      text: clean(el.innerText || el.textContent).slice(0, 80),
      in_view: visibleOnScreen(el),
      ...(i < controlCount ? { control: true } : {}),
    }));
    const scrollers = [];
    for (let node = target.parentElement; node; node = node.parentElement) {
      if (node.scrollHeight > node.clientHeight + 1 || node.scrollWidth > node.clientWidth + 1) scrollers.push(node);
    }
    const before = scrollers.map((node) => [node.scrollTop, node.scrollLeft]);
    const beforePage = [window.scrollX, window.scrollY];
    target.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    const moved = scrollers.some((node, i) => node.scrollTop !== before[i][0] || node.scrollLeft !== before[i][1])
      || window.scrollX !== beforePage[0] || window.scrollY !== beforePage[1];
    const inView = visibleOnScreen(target);
    return {
      ok: true,
      action: "scroll",
      mode: "scroll_to_text",
      matches: matches.length,
      occurrence: index + 1,
      match_texts: matchTexts,
      found_text: clean(target.innerText || target.textContent).slice(0, 120),
      in_view: inView,
      scroll_changed: moved,
      scroll_attempted: true,
    };
  } catch (error) {
    return { ok: false, action: "scroll", mode: "scroll_to_text", failure_reason: "scroll_execution_failed", error: String(error) };
  }
}

function scrollSelectorIntoViewInPage(selector, expected) {
  // Bring the one element a remembered selector names into view (nearest, every scrolling ancestor). Nothing
  // is clicked or typed. The element must be unique and keep the structural identity it was seen with.
  try {
    const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const exactSelector = String(selector || "").trim();
    const fail = (reason, extra = {}) => ({ ok: false, action: "scroll", mode: "scroll_into_view", failure_reason: reason, scroll_attempted: false, ...extra });
    if (!exactSelector) return fail("control_selector_required");
    const queryAll = typeof globalThis.__lumenDeepQueryAll === "function"
      ? globalThis.__lumenDeepQueryAll
      : (value) => Array.from(document.querySelectorAll(value));
    let matches = [];
    try { matches = Array.from(queryAll(exactSelector)); } catch (_) { matches = []; }
    if (matches.length === 0) return fail("control_not_found", { matches: 0 });
    if (matches.length > 1) return fail("control_selector_ambiguous", { matches: matches.length });
    const node = matches[0];
    const want = expected && typeof expected === "object" ? expected : {};
    const live = {
      tag: String(node.tagName || "").toLowerCase(),
      role: clean(node.getAttribute && node.getAttribute("role")).toLowerCase(),
      type: clean(node.getAttribute && node.getAttribute("type")).toLowerCase(),
    };
    for (const field of ["tag", "role", "type"]) {
      if (clean(want[field]).toLowerCase() !== live[field]) return fail("control_identity_changed", { identity_mismatch_field: field });
    }
    const before = [window.scrollX, window.scrollY];
    node.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "instant" });
    const rect = node.getBoundingClientRect();
    const inView = rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0
      && rect.top < window.innerHeight && rect.left < window.innerWidth;
    return {
      ok: true,
      action: "scroll",
      mode: "scroll_into_view",
      scroll_attempted: true,
      in_view: inView,
      scroll_changed: window.scrollX !== before[0] || window.scrollY !== before[1],
      scrolled_into_view: true,
    };
  } catch (error) {
    return { ok: false, action: "scroll", mode: "scroll_into_view", failure_reason: "scroll_execution_failed", error: String(error), scroll_attempted: false };
  }
}

async function runVisionScrollIntoView(tabId, selector, expected) {
  if (!tabId) return { ok: false, action: "scroll", mode: "scroll_into_view", failure_reason: "scroll_execution_failed", scroll_attempted: false };
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: scrollSelectorIntoViewInPage, args: [String(selector || ""), expected || {}] }),
      3000,
      "scroll into view timed out",
    );
    const result = (rows && rows[0] && rows[0].result) || { ok: false, action: "scroll", mode: "scroll_into_view", failure_reason: "scroll_execution_failed", scroll_attempted: false };
    if (result.ok) {
      try {
        await withTimeout(
          chrome.scripting.executeScript({ target: { tabId }, func: domQuietInPage, args: [150, 600] }),
          900,
          "post-scroll settle timed out",
        );
      } catch (_) {}
    }
    return result;
  } catch (error) {
    return { ok: false, action: "scroll", mode: "scroll_into_view", failure_reason: "scroll_execution_failed", error: errorMessage(error), scroll_attempted: false };
  }
}

async function runVisionScrollToText(tabId, text, occurrence) {
  if (!tabId) return { ok: false, action: "scroll", mode: "scroll_to_text", failure_reason: "scroll_execution_failed" };
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: scrollToTextInPage, args: [String(text || ""), Number(occurrence || 1)] }),
      4000,
      "scroll to text timed out",
    );
    const result = (rows && rows[0] && rows[0].result) || { ok: false, action: "scroll", mode: "scroll_to_text", failure_reason: "scroll_execution_failed" };
    try {
      await withTimeout(
        chrome.scripting.executeScript({ target: { tabId }, func: domQuietInPage, args: [150, 600] }),
        900,
        "post-find settle timed out",
      );
    } catch (_) {}
    return result;
  } catch (error) {
    return { ok: false, action: "scroll", mode: "scroll_to_text", failure_reason: "scroll_execution_failed", error: errorMessage(error) };
  }
}

async function runNavigateHistory(tabId, direction) {
  // The browser's own Back / Forward buttons - real history navigation, not a key chord.
  if (!tabId) return { ok: false, action: direction, mode: "history_navigation", error: "No application tab." };
  const before = await tabPageEvidence(tabId);
  try {
    if (direction === "forward") await chrome.tabs.goForward(tabId);
    else await chrome.tabs.goBack(tabId);
  } catch (error) {
    return {
      ok: false,
      action: direction,
      mode: "history_navigation",
      failure_reason: "history_unavailable",
      error: errorMessage(error),
      before_url: before.url || "",
    };
  }
  try { await waitForLoad(tabId, 8000); } catch (_) {}
  await new Promise((resolve) => setTimeout(resolve, 200));
  const after = await tabPageEvidence(tabId);
  clearTransientSensitiveScreenshotBinding();
  return {
    ok: true,
    action: direction,
    mode: "history_navigation",
    before_url: before.url || "",
    after_url: after.url || "",
    url: after.url || "",
    title: after.title || "",
    url_changed: Boolean(before.url && after.url && before.url !== after.url),
    title_changed: Boolean(before.title && after.title && before.title !== after.title),
  };
}

// A click, key, or drag whose address differs only by its #fragment stayed on the same document (an href="#" link):
// url_changed above ignores it, so it is a local change, never a navigation.
function navigableUrl(raw) {
  // Only the web: an absolute http(s) URL without embedded credentials.
  try {
    const url = new URL(String(raw || "").trim());
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return "";
    return url.href;
  } catch (_) {
    return "";
  }
}

async function runVisionNavigate(tabId, rawUrl, newTab = false) {
  // The browser's address bar: open a URL in this task's tab, or in a new tab this task owns
  // (the current tab, and anything typed into it, stays as it is).
  const url = navigableUrl(rawUrl);
  if (!url) {
    return { ok: false, action: "navigate", mode: "address_bar", failure_reason: "navigate_url_invalid", error: "Only an absolute http(s) URL can be opened." };
  }
  if (!tabId) return { ok: false, action: "navigate", mode: "address_bar", failure_reason: "navigate_failed", error: "No application tab." };
  const before = await tabPageEvidence(tabId);
  let navigatedTabId = tabId;
  try {
    if (newTab) {
      const source = await chrome.tabs.get(tabId);
      const tab = await chrome.tabs.create({ url, active: true, openerTabId: tabId, windowId: source && source.windowId });
      navigatedTabId = Number(tab.id);
      if (!runTabIds.includes(Number(tabId))) runTabIds.push(Number(tabId));
      if (!runTabIds.includes(navigatedTabId)) runTabIds.push(navigatedTabId);
      persistRunTabIds();
      if (controlIndicatorState.active) await applyControlIndicatorToTab(tabId, { active: false }).catch(() => {});
      targetTabId = navigatedTabId;
      await chrome.storage.local.set({ lumenTargetTabId: targetTabId, lumenTargetOwned: true });
    } else {
      await chrome.tabs.update(tabId, { url });
    }
  } catch (error) {
    return { ok: false, action: "navigate", mode: "address_bar", failure_reason: "navigate_failed", error: errorMessage(error), before_url: before.url || "" };
  }
  try { await waitForLoad(navigatedTabId, 20000); } catch (_) {}
  if (newTab && controlIndicatorState.active) await applyControlIndicatorToTab(navigatedTabId).catch(() => {});
  await new Promise((resolve) => setTimeout(resolve, 200));
  const after = await tabPageEvidence(navigatedTabId);
  clearTransientSensitiveScreenshotBinding();
  return {
    ok: true,
    action: "navigate",
    mode: "address_bar",
    requested_url: url,
    before_url: before.url || "",
    after_url: after.url || "",
    url: after.url || "",
    title: after.title || "",
    opened_new_tab: Boolean(newTab),
    tab_id: String(navigatedTabId),
    url_changed: Boolean(newTab || (before.url && after.url && before.url !== after.url)),
    title_changed: Boolean(before.title && after.title && before.title !== after.title),
  };
}

async function runVisionReload(tabId) {
  // The browser's Reload button.
  if (!tabId) return { ok: false, action: "reload", mode: "address_bar", failure_reason: "reload_failed", error: "No application tab." };
  const before = await tabPageEvidence(tabId);
  try {
    await chrome.tabs.reload(tabId);
  } catch (error) {
    return { ok: false, action: "reload", mode: "address_bar", failure_reason: "reload_failed", error: errorMessage(error) };
  }
  try { await waitForLoad(tabId, 20000); } catch (_) {}
  await new Promise((resolve) => setTimeout(resolve, 200));
  const after = await tabPageEvidence(tabId);
  clearTransientSensitiveScreenshotBinding();
  return {
    ok: true,
    action: "reload",
    mode: "address_bar",
    reloaded: true,
    url: after.url || "",
    title: after.title || "",
    url_changed: Boolean(before.url && after.url && before.url !== after.url),
    surface_changed: Boolean(
      before.surface_signature && after.surface_signature && before.surface_signature !== after.surface_signature,
    ),
  };
}

function armClipboardEvidenceInPage() {
  // Record the page's own clipboard events around one key press: copy/cut/paste leave no visible
  // trace, so this is the only evidence a shortcut took. Never reads clipboard contents.
  try {
    if (typeof globalThis.__lumenClipboardDisarm === "function") globalThis.__lumenClipboardDisarm();
    const state = { events: [] };
    const record = (event) => {
      let selectionLength = 0;
      let selectedText = "";
      let privateSource = false;
      try {
        const active = document.activeElement;
        if (active && typeof active.selectionStart === "number" && typeof active.selectionEnd === "number") {
          selectionLength = Math.abs(active.selectionEnd - active.selectionStart);
          if (selectionLength && selectionLength <= 4000) {
            selectedText = String(active.value || "").slice(
              Math.min(active.selectionStart, active.selectionEnd),
              Math.max(active.selectionStart, active.selectionEnd),
            );
          }
          const inType = String(active.getAttribute("type") || "").toLowerCase();
          const tokens = String(active.getAttribute("autocomplete") || "").toLowerCase().split(/\s+/).filter(Boolean);
          privateSource = inType === "password" || tokens.some((token) => (
            token.includes("password") || token === "one-time-code" || token.startsWith("cc-")
          ));
        } else if (window.getSelection) {
          const selected = String(window.getSelection() || "");
          selectionLength = selected.length;
          if (selected.length <= 4000) selectedText = selected;
        }
      } catch (_) { privateSource = true; }
      const entry = { type: event.type, selection_length: selectionLength };
      // What a copy or cut puts on the clipboard (the page's own handler may set something else, read after
      // the handlers ran below). It goes to the run server only, which uses it to gate a later paste.
      if (event.type !== "paste") {
        if (privateSource) entry.private = true;
        else if (selectedText) entry.text = selectedText;
        state.pending = entry;
      }
      state.events.push(entry);
    };
    const settle = (event) => {
      const entry = state.pending;
      state.pending = null;
      if (!entry || entry.private) return;
      try {
        const set = event.clipboardData && typeof event.clipboardData.getData === "function"
          ? String(event.clipboardData.getData("text/plain") || "")
          : "";
        if (set) entry.text = set.length <= 4000 ? set : "";
        if (event.defaultPrevented && !set) delete entry.text;
      } catch (_) { delete entry.text; }
    };
    for (const type of ["copy", "cut", "paste"]) document.addEventListener(type, record, true);
    for (const type of ["copy", "cut"]) window.addEventListener(type, settle, false);
    globalThis.__lumenClipboardDisarm = () => {
      for (const type of ["copy", "cut", "paste"]) document.removeEventListener(type, record, true);
      for (const type of ["copy", "cut"]) window.removeEventListener(type, settle, false);
    };
    globalThis.__lumenClipboardState = state;
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}

function readClipboardEvidenceInPage() {
  try {
    const state = globalThis.__lumenClipboardState || { events: [] };
    if (typeof globalThis.__lumenClipboardDisarm === "function") globalThis.__lumenClipboardDisarm();
    globalThis.__lumenClipboardDisarm = null;
    globalThis.__lumenClipboardState = null;
    return { ok: true, events: (state.events || []).slice(0, 5) };
  } catch (error) {
    return { ok: false, events: [] };
  }
}

async function runVisionKey(tabId, key, options = null) {
  if (!tabId) return { ok: false, action: "key", error: "No application tab." };
  if (nativeDialogOpenFor(tabId)) return answerNativeDialog(tabId, key);
  // repeat presses the key again at human pace (Backspace x25); hold_ms keeps each press down
  // before its release (a held arrow in a game). A held key is one press: no auto-repeat.
  const presses = Math.max(1, Math.min(200, Math.round(Number((options && options.repeat) || 1))));
  const holdMs = Math.max(0, Math.min(5000, Math.round(Number((options && options.hold_ms) || 0))));
  let pressesSent = 0;
  const beforeState = await tabPageEvidence(tabId);
  let beforeActive = {};
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: activeElementEvidenceInPage }),
      2500,
      "pre-key active element evidence timed out",
    );
    beforeActive = (rows && rows[0] && rows[0].result) || {};
  } catch (_) {
    beforeActive = {};
  }
  // Dispatch-time re-check: the page may have moved focus (autofocus, a script) to a sensitive field after
  // the server observed the screen, so a key that types or pastes text never lands in one. Fails closed:
  // with no evidence of where focus is, or focus inside a frame whose inner field is unknown (a
  // cross-origin iframe reports as the frame itself), a key that types or pastes is refused too.
  {
    const probe = keyDispatchDescription(key);
    const lowerKey = String(probe.key || "").toLowerCase();
    const modBits = Number(probe.modifiers || 0);
    const typesText = (Boolean(probe.text) && probe.key !== "Enter") || (String(key || "").length > 0 && !String(key).trim());
    const pastes = ((modBits & (2 | 4)) && lowerKey === "v") || ((modBits & 8) && probe.key === "Insert");
    if (typesText || pastes) {
      const evidenceKnown = beforeActive && beforeActive.ok !== false && typeof beforeActive.active_tag === "string";
      if (evidenceKnown && beforeActive.active_sensitive === true) {
        return {
          ok: false,
          action: "key",
          failure_reason: "keyed_text_sensitive_refused",
          error: "The focused field is sensitive (password, one-time code, payment); no key was pressed. Use the type action or use_credential.",
          before_active_typable: true,
        };
      }
      if (!evidenceKnown) {
        return {
          ok: false,
          action: "key",
          failure_reason: "keyed_text_focus_unverified",
          error: "Where keyboard focus is could not be verified just before this key, so a key that types or pastes was not pressed. Re-observe, then use the type action on the exact field.",
        };
      }
      if (["iframe", "frame", "object", "embed"].includes(String(beforeActive.active_tag || ""))) {
        return {
          ok: false,
          action: "key",
          failure_reason: "keyed_text_frame_focus_refused",
          error: "Focus is inside a frame whose field is not visible to the page; no key was pressed. Click the field and use the type action.",
        };
      }
    }
  }
  let editedValueBefore = "";
  if (["enter", "tab"].includes(String(key || "").trim().toLowerCase()) && beforeActive.active_typable) {
    try {
      const rows = await withTimeout(
        chrome.scripting.executeScript({ target: { tabId }, func: focusedEditorValueInPage }),
        1500,
        "pre-key editor value read timed out",
      );
      editedValueBefore = String(((rows && rows[0] && rows[0].result) || {}).value || "");
    } catch (_) {
      editedValueBefore = "";
    }
  }
  const target = { tabId };
  let attached = false;
  let trustedKey = false;
  let heldKeyUp = null;
  // Set before the first keyDown reaches Chrome: from then on a failed ack cannot prove the key did
  // not land, so the synthetic fallback below would press it a second time (a second Enter submits twice).
  let keyDispatchStarted = false;
  const dispatch = keyDispatchDescription(key);
  try {
    await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: armClipboardEvidenceInPage }),
      1500,
      "clipboard evidence arm timed out",
    );
  } catch (_) {}
  try {
    await acquireDebugger(target);
    attached = true;
    // A key that types carries its text on keyDown (which also produces keypress/input); a
    // non-text key or a shortcut chord is a rawKeyDown the page and browser interpret.
    const { text: keyText, unmodifiedText: _unmodifiedText, ...keyUpDispatch } = dispatch;
    for (let press = 0; press < presses; press += 1) {
      // Repeated presses go one at a time at human pace, so the page sees each keystroke.
      if (press) await new Promise((resolve) => setTimeout(resolve, 30));
      keyDispatchStarted = true;
      // A chord is pressed the way a person presses it: each modifier goes down as its own key
      // (a page that tracks held keys sees Control, then Shift, then the key), and they come up in
      // reverse order after the key.
      const chordDowns = chordModifierKeys(dispatch.modifiers);
      let chordBits = 0;
      let chordDialog = false;
      for (const mod of chordDowns) {
        chordBits |= mod.bit;
        const modDown = await dispatchInputUnlessDialog(target, "Input.dispatchKeyEvent", {
          type: "rawKeyDown", key: mod.key, code: mod.code, windowsVirtualKeyCode: mod.vk, nativeVirtualKeyCode: mod.vk, modifiers: chordBits,
        });
        if (modDown === "dialog") { chordDialog = true; break; }
        await new Promise((resolve) => setTimeout(resolve, 25));
      }
      const releaseChord = async () => {
        let bits = chordBits;
        for (const mod of chordDowns.slice().reverse()) {
          if (!(bits & mod.bit)) continue;
          bits &= ~mod.bit;
          try {
            await _cdp(target, "Input.dispatchKeyEvent", {
              type: "keyUp", key: mod.key, code: mod.code, windowsVirtualKeyCode: mod.vk, nativeVirtualKeyCode: mod.vk, modifiers: bits,
            });
          } catch (_) { /* detached */ }
        }
        chordBits = 0;
      };
      if (chordDialog) { await releaseChord(); break; }
      const down = await dispatchInputUnlessDialog(
        target,
        "Input.dispatchKeyEvent",
        { type: keyText ? "keyDown" : "rawKeyDown", ...dispatch },
      );
      if (down === "dialog") { await releaseChord(); break; }
      heldKeyUp = keyUpDispatch;
      if (holdMs > 0) await new Promise((resolve) => setTimeout(resolve, holdMs));
      const up = await dispatchInputUnlessDialog(target, "Input.dispatchKeyEvent", { type: "keyUp", ...keyUpDispatch });
      heldKeyUp = null;
      await releaseChord();
      pressesSent += 1;
      if (up === "dialog" || nativeDialogOpenFor(tabId)) break;
    }
    trustedKey = true;
    if (nativeDialogOpenFor(tabId)) {
      return {
        ok: true,
        action: "key",
        mode: "cdp_key",
        key: dispatch.key,
        presses: pressesSent,
        trusted_key_attempted: true,
        trusted_key_ok: true,
        surface_changed: true,
        native_dialog: publicNativeDialog(nativeDialogOpenFor(tabId)),
      };
    }
  } catch (error) {
    if (pressesSent > 0 || keyDispatchStarted) {
      // A press may already have landed: a fallback press would add one the model never asked for.
      return {
        ok: false,
        action: "key",
        mode: "cdp_key",
        key: dispatch.key,
        presses: pressesSent,
        failure_reason: pressesSent > 0 ? "key_execution_partial" : "key_execution_outcome_unknown",
        execution_outcome_unknown: true,
        error: "Key presses stopped part way: " + errorMessage(error),
      };
    }
    try {
      const rows = await withTimeout(
        chrome.scripting.executeScript({ target: { tabId }, func: visionKeyInPage, args: [dispatch.key] }),
        2500,
        "dom key fallback timed out",
      );
      const fallback = (rows && rows[0] && rows[0].result) || {};
      if (!fallback.ok) throw error;
    } catch (fallbackError) {
      return { ok: false, action: "key", mode: "native_key", key: dispatch.key, error: "Native key failed: " + errorMessage(fallbackError) };
    }
  } finally {
    if (attached && heldKeyUp) {
      // Never leave a key held down in the page after a failed press.
      try { await _cdp(target, "Input.dispatchKeyEvent", { type: "keyUp", ...heldKeyUp }); } catch (_) {}
    }
    if (attached) {
      try {
        releaseDebugger(target);
      } catch (_) {
        /* already detached */
      }
    }
  }
  await new Promise((resolve) => setTimeout(resolve, 120));
  const afterState = await tabPageEvidence(tabId);
  let afterActive = {};
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: activeElementEvidenceInPage }),
      2500,
      "post-key active element evidence timed out",
    );
    afterActive = (rows && rows[0] && rows[0].result) || {};
  } catch (_) {
    afterActive = {};
  }
  let clipboardEvents = [];
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: readClipboardEvidenceInPage }),
      1500,
      "clipboard evidence read timed out",
    );
    const clipboard = (rows && rows[0] && rows[0].result) || {};
    clipboardEvents = Array.isArray(clipboard.events) ? clipboard.events : [];
  } catch (_) {
    clipboardEvents = [];
  }
  const beforeSelector = String(beforeActive.active_selector || "");
  const afterSelector = String(afterActive.active_selector || "");
  // A key that commits an edit (Enter, Tab) pressed in a text editor: read back where the edited
  // text went. Still focused with the same text = nothing committed; focus left it and the text is
  // visible on the page = it landed; otherwise the receipt says it is not visible (never assumed).
  let editCommit = null;
  const editedValue = editedValueBefore;
  if (editedValue && pressesSent > 0) {
    if (afterSelector && afterSelector === beforeSelector) {
      let valueAfter = "";
      try {
        const rows = await withTimeout(
          chrome.scripting.executeScript({ target: { tabId }, func: focusedEditorValueInPage }),
          1500,
          "post-key editor value read timed out",
        );
        valueAfter = String(((rows && rows[0] && rows[0].result) || {}).value || "");
      } catch (_) {
        valueAfter = "";
      }
      editCommit = {
        editor_hint: String(beforeActive.active_hint || ""),
        edit_value: editedValue,
        editor_still_focused: true,
        editor_value_after: valueAfter,
      };
    } else {
      let visible = null;
      try {
        const rows = await withTimeout(
          chrome.scripting.executeScript({
            target: { tabId },
            func: (needle) => {
              const norm = (value) => String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
              const wanted = norm(needle);
              if (!wanted) return false;
              const fieldHolds = Array.from(document.querySelectorAll("input,textarea")).some((node) => norm(node.value) === wanted);
              return fieldHolds || norm(document.body ? document.body.innerText : "").includes(wanted);
            },
            args: [editedValue],
          }),
          1500,
          "edited value visibility read timed out",
        );
        visible = Boolean(rows && rows[0] && rows[0].result);
      } catch (_) {
        visible = null;
      }
      editCommit = {
        editor_hint: String(beforeActive.active_hint || ""),
        edit_value: editedValue,
        editor_still_focused: false,
        edit_value_visible_after: visible,
      };
    }
  }
  const selectionChanged = Boolean(
    beforeSelector === afterSelector
    && (
      Number(beforeActive.selection_length || 0) !== Number(afterActive.selection_length || 0)
      || beforeActive.selection_start !== afterActive.selection_start
      || beforeActive.selection_end !== afterActive.selection_end
    )
  );
  return {
    ok: true,
    action: "key",
    selection_changed: selectionChanged,
    selection_length: Number(afterActive.selection_length || 0),
    mode: trustedKey ? "cdp_key" : "dom_key_fallback",
    clipboard_events: clipboardEvents,
    key: dispatch.key,
    presses: trustedKey ? pressesSent : 1,
    hold_ms: trustedKey ? holdMs : 0,
    trusted_key_attempted: true,
    trusted_key_ok: trustedKey,
    url_changed: Boolean(beforeState.url && afterState.url && beforeState.url !== afterState.url && String(beforeState.url).split("#")[0] !== String(afterState.url).split("#")[0]),
    title_changed: Boolean(beforeState.title && afterState.title && beforeState.title !== afterState.title),
    surface_changed: Boolean(beforeState.surface_signature && afterState.surface_signature && beforeState.surface_signature !== afterState.surface_signature),
    scroll_changed: beforeState.scroll_y != null && afterState.scroll_y != null && beforeState.scroll_y !== afterState.scroll_y,
    focus_changed: Boolean((beforeSelector || afterSelector) && beforeSelector !== afterSelector),
    ...(editCommit ? { edit_commit: editCommit } : {}),
    before_active_selector: beforeSelector,
    after_active_selector: afterSelector,
    // What held focus when the key went down (structure only, never a value): the server gates printable
    // keys by the focused field it observed, and this lets it see when that was not the field the key hit.
    before_active_typable: Boolean(beforeActive.active_typable),
    before_active_tag: String(beforeActive.active_tag || ""),
    before_active_input_type: String(beforeActive.active_input_type || ""),
    before_surface_signature: beforeState.surface_signature || "",
    after_surface_signature: afterState.surface_signature || "",
  };
}

async function runVisionUpload(tabId, imgX, imgY, documentInfo) {
  if (!tabId) return { ok: false, action: "upload", error: "No application tab." };
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: resolveUploadTargetInPage, args: [{ mode: "point", imgX, imgY }] }),
      2500,
      "file input point probe timed out",
    );
    const target = (rows && rows[0] && rows[0].result) || { ok: false, error: "file input point probe returned nothing" };
    if (!target.ok || !target.selector) return target;
    const result = await uploadFile(tabId, { ...(documentInfo || {}), action: "upload", selector: target.selector, target_label: target.target_label || target.label || "" });
    return { ...result, action: "upload", upload_target_selector: target.selector, upload_target_label: target.target_label || target.label || "", target_label: target.target_label || target.label || result.target_label || "" };
  } catch (error) {
    return { ok: false, action: "upload", mode: "vision_upload", error: "Vision upload failed: " + errorMessage(error) };
  }
}

async function runVisionUploadControl(
  tabId,
  selector,
  documentInfo,
  expectedTarget = null,
  expectedPageContext = null,
) {
  if (!tabId) return { ok: false, action: "upload", error: "No application tab." };
  if (!clickTargetIdentityComplete(expectedTarget)) {
    return { ok: false, action: "upload", mode: "vision_upload_control", failure_reason: "control_identity_required", upload_attempted: false };
  }
  const pageVerdict = await observedPageContextVerdict(tabId, expectedPageContext);
  if (!pageVerdict.ok) {
    return {
      ok: false,
      action: "upload",
      mode: "vision_upload_control",
      failure_reason: pageVerdict.reason,
      page_context_mismatch_field: pageVerdict.mismatch_field || "",
      upload_attempted: false,
    };
  }
  try {
    const identityRows = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId },
        func: visionControlCenterInPage,
        args: [String(selector || ""), false, false],
      }),
      2500,
      "upload control identity probe timed out",
    );
    const liveIdentity = (identityRows && identityRows[0] && identityRows[0].result) || {};
    const identityVerdict = clickTargetIdentityVerdict(expectedTarget, liveIdentity);
    if (!liveIdentity.ok || !identityVerdict.ok) {
      return {
        ok: false,
        action: "upload",
        mode: "vision_upload_control",
        failure_reason: liveIdentity.failure_reason || identityVerdict.reason || "control_target_drift",
        target_identity_mismatch_field: identityVerdict.mismatch_field || "",
        upload_attempted: false,
      };
    }
    const rows = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId },
        func: resolveUploadTargetInPage,
        args: [{ mode: "selector", selector: String(selector || ""), expectedTarget }],
      }),
      2500,
      "file input control probe timed out",
    );
    const target = (rows && rows[0] && rows[0].result) || { ok: false, error: "file input control probe returned nothing" };
    if (!target.ok || !target.selector) {
      return target;
    }
    const result = await uploadFile(tabId, { ...(documentInfo || {}), action: "upload", selector: target.selector, target_label: target.target_label || target.label || "" });
    return { ...result, action: "upload", mode: result.mode || "vision_upload_control", upload_target_selector: target.selector, upload_target_label: target.target_label || target.label || "", target_label: target.target_label || target.label || result.target_label || "" };
  } catch (error) {
    return { ok: false, action: "upload", mode: "vision_upload_control", error: "Vision upload failed: " + errorMessage(error) };
  }
}

// A CAPTCHA asks whether a person is present: it is the user's to answer, never the agent's. Any pointer, typing
// or key command that would land in one (by point, by selector, or by focus) is refused before it runs (run 55
// clicked "I'm not a robot" and started on the image grid).
const CAPTCHA_GUARDED_METHODS = new Set([
  "vision_click",
  "vision_click_control",
  "vision_drag",
  "vision_hover",
  "vision_select_text",
  "vision_type",
  "vision_type_control",
  "vision_key",
]);

function captchaAtTargetInPage(points, selector, checkFocus) {
  try {
    const queryAll = typeof globalThis.__lumenDeepQueryAll === "function"
      ? globalThis.__lumenDeepQueryAll
      : (value) => Array.from(document.querySelectorAll(value));
    const provider = (frame) => {
      const src = String((frame.getAttribute && frame.getAttribute("src")) || frame.src || "");
      const title = String((frame.getAttribute && frame.getAttribute("title")) || "");
      // An invisible widget's badge ("protected by reCAPTCHA") asks nothing of anyone: not a CAPTCHA to hand off.
      if (/[?&]size=invisible\b/i.test(src) || /frame=checkbox-invisible/i.test(src)) return "";
      if (/recaptcha/i.test(src) || /recaptcha/i.test(title)) return "recaptcha";
      if (/hcaptcha\.com/i.test(src) || /hcaptcha/i.test(title)) return "hcaptcha";
      if (/challenges\.cloudflare\.com/i.test(src) || /cloudflare security challenge/i.test(title)) return "turnstile";
      if (/arkoselabs\.com|funcaptcha\.com/i.test(src)) return "arkose";
      if (/captcha-delivery\.com/i.test(src)) return "datadome";
      if (/captcha/i.test(title)) return "captcha";
      return "";
    };
    // Only a CAPTCHA the user could see counts: an invisible widget's frame (Lever's hCaptcha) sits on the page
    // with nothing drawn, and run 65's click on the cookie banner's Dismiss above it was refused as a CAPTCHA.
    const shown = (el) => {
      for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
        const style = window.getComputedStyle(node);
        const opacity = style.opacity === "" ? 1 : Number(style.opacity);
        if (style.display === "none" || style.visibility === "hidden" || opacity <= 0.05) return false;
      }
      const box = el.getBoundingClientRect();
      return box.width > 0 && box.height > 0;
    };
    const frames = Array.from(queryAll("iframe"))
      .map((frame) => ({ frame, provider: provider(frame) }))
      .filter((entry) => entry.provider && shown(entry.frame));
    if (!frames.length) return { captcha: false };
    // The element a click at the point lands on: the CAPTCHA only when its frame is on top there.
    const topAt = (x, y) => {
      let el = document.elementFromPoint ? document.elementFromPoint(x, y) : null;
      while (el && el.shadowRoot && typeof el.shadowRoot.elementFromPoint === "function") {
        const inner = el.shadowRoot.elementFromPoint(x, y);
        if (!inner || inner === el) break;
        el = inner;
      }
      return el;
    };
    const dpr = window.devicePixelRatio || 1;
    for (const point of Array.isArray(points) ? points : []) {
      const imgX = Number(point && point[0]) || 0;
      const imgY = Number(point && point[1]) || 0;
      if (!imgX && !imgY) continue;
      const x = imgX / dpr;
      const y = imgY / dpr;
      const top = topAt(x, y);
      const hit = frames.find((entry) => entry.frame === top);
      if (hit) return { captcha: true, provider: hit.provider, via: "point" };
    }
    if (selector) {
      let matches = [];
      try {
        matches = Array.from(queryAll(String(selector)));
      } catch (_) {
        matches = [];
      }
      const target = matches.length === 1 ? matches[0] : null;
      const hit = target ? frames.find((entry) => entry.frame === target) : null;
      if (hit) return { captcha: true, provider: hit.provider, via: "selector" };
    }
    if (checkFocus) {
      const active = typeof globalThis.__lumenDeepActiveElement === "function"
        ? globalThis.__lumenDeepActiveElement()
        : document.activeElement;
      const hit = frames.find((entry) => entry.frame === active);
      if (hit) {
        // The user ticks the checkbox and replies; focus stays in its frame. A widget that already holds its
        // response token is solved: the keys would land in it, but there is nothing for the user to do again.
        const tokens = Array.from(queryAll('textarea[name^="g-recaptcha-response"], textarea[name="h-captcha-response"], input[name="cf-turnstile-response"]'));
        const solved = tokens.some((field) => String(field.value || "").trim());
        return { captcha: true, provider: hit.provider, via: "focus", ...(solved ? { solved: true } : {}) };
      }
    }
    return { captcha: false };
  } catch (error) {
    return { captcha: false, error: String(error) };
  }
}

async function captchaRefusalFor(method, params) {
  if (!CAPTCHA_GUARDED_METHODS.has(method)) return null;
  const p = params && typeof params === "object" ? params : {};
  const points = [];
  if (["vision_click", "vision_drag", "vision_hover", "vision_select_text"].includes(method)) points.push([p.x, p.y]);
  if (method === "vision_drag") points.push([p.to_x, p.to_y]);
  if (method === "vision_click_control") points.push([p.fallback_x, p.fallback_y]);
  const selector = method === "vision_click_control" || method === "vision_type_control" ? String(p.selector || "") : "";
  // A selector-bound type focuses its own field first, so only keys and caret typing go where focus already is.
  const checkFocus = method === "vision_key" || method === "vision_type";
  const tabId = await activeTabId();
  if (!tabId) return null;
  let verdict = {};
  try {
    const rows = await withTimeout(
      chrome.scripting.executeScript({ target: { tabId }, func: captchaAtTargetInPage, args: [points, selector, checkFocus] }),
      1500,
      "captcha check timed out",
    );
    verdict = (rows && rows[0] && rows[0].result) || {};
  } catch (_) {
    verdict = {};
  }
  if (!verdict.captcha) return null;
  if (verdict.solved) {
    return {
      ok: false,
      action: String(method).replace(/^vision_/, ""),
      failure_reason: "captcha_frame_focused",
      captcha_provider: verdict.provider || "captcha",
      activation_attempted: false,
      activation_dispatched: false,
      activation_count: 0,
      error: "Focus is inside the completed CAPTCHA's frame: click the field you mean before typing or pressing keys.",
    };
  }
  return {
    ok: false,
    action: String(method).replace(/^vision_/, ""),
    failure_reason: "captcha_requires_user",
    captcha_provider: verdict.provider || "captcha",
    captcha_detected_by: verdict.via || "",
    activation_attempted: false,
    activation_dispatched: false,
    activation_count: 0,
    error: "This is a CAPTCHA (a check that a person is present): the user completes it, never the agent.",
  };
}

const NATIVE_DIALOG_ANSWERING_METHODS = new Set(["vision_key", "vision_type", "vision_type_control"]);
// A browser dialog is tab-modal: the other tabs stay usable.
const NATIVE_DIALOG_TAB_INDEPENDENT_METHODS = new Set(["vision_switch_tab"]);

async function handleCommand(method, params) {
  const commandParams = params && typeof params === "object" ? params : {};
  if (String(method || "").startsWith("vision_") && !NATIVE_DIALOG_TAB_INDEPENDENT_METHODS.has(method)) {
    const dialogTabId = await activeTabId();
    const dialog = dialogTabId ? nativeDialogOpenFor(dialogTabId) : null;
    if (dialog) {
      if (method === "vision_key") return runVisionKey(dialogTabId, commandParams.key || "");
      if (method === "vision_type" || method === "vision_type_control") {
        return runVisionType(dialogTabId, commandParams.text || "");
      }
      return {
        ok: false,
        action: String(method).replace(/^vision_/, ""),
        failure_reason: "native_dialog_open",
        native_dialog: publicNativeDialog(dialog),
        error: "A browser dialog is open over the page; answer it first (type into a prompt, Enter = OK, Escape = Cancel).",
      };
    }
  }
  const captchaRefusal = await captchaRefusalFor(method, commandParams);
  if (captchaRefusal) return captchaRefusal;
  if (
    String(method || "").startsWith("vision_")
    && Object.prototype.hasOwnProperty.call(
      commandParams,
      "required_sensitive_mask_binding",
    )
  ) {
    const unguardedParams = { ...commandParams };
    delete unguardedParams.required_sensitive_mask_binding;
    return runVisionCommandWithSensitiveMaskRebind(
      commandParams,
      method,
      () => handleCommand(method, unguardedParams),
    );
  }
  if (method === "set_control_indicator") return setControlIndicator(params || {});
  if (method === "reload_extension") {
    // Let the backend hot-reload this unpacked body after a code change; the service worker
    // reconnects to the hub on startup with the same stable device id.
    setTimeout(() => chrome.runtime.reload(), 150);
    return { ok: true, action: "reload_extension" };
  }
  if (method === "close_bench_tabs") return closeBenchTabs();
  if (method === "read_run_tab_text") return runReadRunTabText(params || {});
  if (method === "navigate") return doNavigate(params || {});
  if (method === "navigate_back") return runNavigateHistory(await activeTabId(), "back");
  if (method === "navigate_forward") return runNavigateHistory(await activeTabId(), "forward");
  if (method === "vision_navigate") return runVisionNavigate(await activeTabId(), commandParams.url || "", commandParams.new_tab === true);
  if (method === "vision_reload") return runVisionReload(await activeTabId());
  if (method === "vision_switch_tab") return runVisionSwitchTab((params || {}).target);
  if (method === "vision_select_text") {
    const p = params || {};
    return runVisionSelectText(await activeTabId(), String(p.text || ""), Number(p.x || 0), Number(p.y || 0));
  }
  if (method === "vision_scroll_into_view") {
    return runVisionScrollIntoView(await activeTabId(), (params || {}).selector || "", (params || {}).expected_target_identity || {});
  }
  if (method === "vision_scroll_to_text") {
    return runVisionScrollToText(await activeTabId(), (params || {}).text || "", (params || {}).occurrence || 1);
  }
  if (method === "vision_drag") {
    const p = params || {};
    return runVisionDrag(
      await activeTabId(),
      Number(p.x || 0),
      Number(p.y || 0),
      Number(p.to_x || 0),
      Number(p.to_y || 0),
      String(p.button || "left"),
      Array.isArray(p.path) ? p.path : null,
      { modifiers: Array.isArray(p.modifiers) ? p.modifiers : [], hold_ms: Number(p.hold_ms || 0) },
    );
  }
  if (method === "vision_hover") {
    const p = params || {};
    return runVisionHover(await activeTabId(), Number(p.x || 0), Number(p.y || 0), Number(p.dwell_ms || 0));
  }
  if (method === "vision_wheel") {
    const p = params || {};
    return runVisionWheel(
      await activeTabId(),
      Number(p.x || 0),
      Number(p.y || 0),
      Number(p.dx || 0),
      Number(p.dy || 0),
      Array.isArray(p.modifiers) ? p.modifiers : null,
    );
  }
  if (method === "observe") return doObserve();
  if (method === "execute") return doExecute(params || {});
  if (method === "screenshot") return runScreenshotWithSensitiveMaskRebind(params || {});
  if (method === "mark_sensitive_screenshot_control") return runMarkSensitiveScreenshotControl(
    await activeTabId(),
    (params || {}).selectors || (params || {}).selector || "",
    (params || {}).expected_page_context || null,
  );
  if (method === "ensure_sensitive_screenshot_control") return runEnsureSensitiveScreenshotControl(
    await activeTabId(),
    (params || {}).selectors || (params || {}).selector || "",
    (params || {}).expected_page_context || null,
  );
  if (method === "vision_click") return runVisionClick(
    await activeTabId(),
    (params || {}).x || 0,
    (params || {}).y || 0,
    Boolean((params || {}).allow_final_submit),
    Boolean((params || {}).allow_guarded_intermediate),
    (params || {}).expected_target_identity || null,
    (params || {}).expected_page_context || null,
    {
      button: (params || {}).button || "left",
      clicks: (params || {}).clicks || 1,
      hold_ms: (params || {}).hold_ms || 0,
      modifiers: Array.isArray((params || {}).modifiers) ? (params || {}).modifiers : [],
      follow: (params || {}).follow && typeof (params || {}).follow === "object" ? (params || {}).follow : null,
      target_text: String((params || {}).target_text || "").slice(0, 120),
      target_wait_ms: Math.max(0, Math.min(30000, Number((params || {}).target_wait_ms) || 0)),
      glide_ms: (params || {}).glide_ms === undefined ? null : (params || {}).glide_ms,
      settle: (params || {}).settle !== false,
      refuse_value_control: Boolean((params || {}).refuse_value_control),
    },
  );
  if (method === "vision_click_control") return runVisionClickControl(
    await activeTabId(),
    (params || {}).selector || "",
    (params || {}).fallback_x || 0,
    (params || {}).fallback_y || 0,
    Boolean((params || {}).allow_final_submit),
    Boolean((params || {}).allow_guarded_intermediate),
    (params || {}).expected_target_identity || null,
    (params || {}).expected_page_context || null,
    {
      button: (params || {}).button || "left",
      clicks: (params || {}).clicks || 1,
      hold_ms: (params || {}).hold_ms || 0,
      modifiers: Array.isArray((params || {}).modifiers) ? (params || {}).modifiers : [],
      surface_offset: Array.isArray((params || {}).point) ? (params || {}).point : null,
      follow: (params || {}).follow && typeof (params || {}).follow === "object" ? (params || {}).follow : null,
      glide_ms: (params || {}).glide_ms === undefined ? null : (params || {}).glide_ms,
      settle: (params || {}).settle !== false,
      rebind_on_drift: (params || {}).allow_rebind_on_drift === true,
    },
  );
  if (method === "vision_type") return runVisionType(await activeTabId(), (params || {}).text || "");
  if (method === "vision_type_control") return runVisionTypeControl(
    await activeTabId(),
    (params || {}).selector || "",
    (params || {}).text || "",
    (params || {}).expected_target_identity || null,
    (params || {}).expected_page_context || null,
    Array.isArray((params || {}).point) ? (params || {}).point : null,
    (params || {}).at_caret === true,
  );
  if (method === "vision_type_credential_control") return runVisionCredentialTypeControl(
    await activeTabId(),
    (params || {}).selector || "",
    (params || {}).text || "",
    (params || {}).expected_target_identity || null,
    (params || {}).expected_page_context || null,
  );
  if (method === "vision_scroll") return runVisionScroll(
    await activeTabId(),
    (params || {}).dy || 0,
    (params || {}).selector || "",
    (params || {}).expected_target_identity || null,
    (params || {}).expected_page_context || null,
    (params || {}).dx || 0,
  );
  if (method === "vision_watch") return runVisionWatch(
    await activeTabId(),
    (params || {}).selector || "",
    Array.isArray((params || {}).point) ? (params || {}).point : null,
    (params || {}).duration_ms || 0,
    params || {},
  );
  if (method === "vision_read_print_view") return runVisionReadPrintView(await activeTabId(), params || {});
  if (method === "vision_read_form") return runVisionReadForm(await activeTabId(), params || {});
  if (method === "vision_wait") return runVisionWait(
    await activeTabId(),
    (params || {}).wait_ms || 750,
    (params || {}).text || "",
  );
  if (method === "vision_key") return runVisionKey(await activeTabId(), (params || {}).key || "", {
    repeat: Number((params || {}).repeat || 1),
    hold_ms: Number((params || {}).hold_ms || 0),
  });
  if (method === "vision_upload") return runVisionUpload(await activeTabId(), (params || {}).x || 0, (params || {}).y || 0, (params || {}).document || {});
  if (method === "vision_upload_control") return runVisionUploadControl(
    await activeTabId(),
    (params || {}).selector || "",
    (params || {}).document || {},
    (params || {}).expected_target_identity || null,
    (params || {}).expected_page_context || null,
  );
  return { ok: false, error: `Unknown command: ${method}` };
}

function commandTimeoutMs(method, params = null) {
  if (method === "vision_click" && params && Number(params.target_wait_ms) > 0) {
    return 19000 + Math.min(30000, Number(params.target_wait_ms));
  }
  if (method === "set_control_indicator") return 8000;
  if (method === "read_run_tab_text") return 8000;
  if (method === "navigate") return 45000;
  if (method === "navigate_back" || method === "navigate_forward") return 30000;
  if (method === "vision_navigate") return 45000;
  if (method === "vision_reload") return 30000;
  if (method === "vision_wheel") return 20000;
  const gestureHoldMs = Math.max(0, Math.min(5000, Number((params && params.hold_ms) || 0)));
  if (method === "vision_drag") return 20000 + gestureHoldMs;
  if (method === "vision_hover") return 15000 + Math.max(0, Math.min(5000, Number((params && params.dwell_ms) || 0)));
  if (method === "vision_key") {
    // Every press takes a few tens of milliseconds plus its hold.
    const presses = Math.max(1, Math.min(200, Number((params && params.repeat) || 1)));
    return 19000 + presses * (50 + gestureHoldMs);
  }
  if (method === "observe" || method === "execute") return 30000;
  if (method === "screenshot") return 25000;
  if (method === "mark_sensitive_screenshot_control") return 12000;
  if (method === "ensure_sensitive_screenshot_control") return 12000;
  if (method === "vision_upload") return 35000;
  if (method === "vision_upload_control") return 35000;
  // A wait lasts up to 30 s and a watch up to 15 s, plus their probes.
  if (method === "vision_wait") return 36000;
  if (method === "vision_watch") return 21000;
  if (String(method || "").startsWith("vision_")) return 19000;
  return 30000;
}

// --- channel -------------------------------------------------------------------------------

let connecting = false;
let reconnectTimer = null;
let reconnectAttempt = 0;
let keepaliveTimer = null;

function clearReconnectTimer() {
  if (reconnectTimer === null) return;
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
}

function scheduleReconnect() {
  if (reconnectTimer !== null) return;
  const delayMs = Math.min(1000 * (2 ** Math.min(reconnectAttempt, 4)), 15000);
  reconnectAttempt += 1;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delayMs);
}

async function connect() {
  if (connecting) return;
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  connecting = true;
  const { backend, deviceId } = await getConfig();
  let ws;
  try {
    ws = new WebSocket(wsUrl(backend, deviceId));
  } catch (error) {
    connecting = false;
    await setStatus(false);
    scheduleReconnect();
    return;
  }
  socket = ws;
  // Each handler references THIS socket (ws), never the shared `socket` var which may be
  // reassigned by a reconnect — and every send is guarded on OPEN. That's what prevents the
  // "Failed to execute 'send' on 'WebSocket': Still in CONNECTING state" race.
  const safeSend = (obj) => {
    if (ws.readyState !== WebSocket.OPEN) return false;
    try { ws.send(JSON.stringify(obj)); return true; } catch (_) { return false; }
  };
  ws.onopen = () => {
    connecting = false;
    reconnectAttempt = 0;
    clearReconnectTimer();
    setStatus(true);
    // Chrome 116+ extends the MV3 service worker's 30s idle deadline on every websocket
    // MESSAGE — an idle-but-open socket does not. Without this heartbeat the worker is
    // evicted every ~30s, the socket dies with it, and a task starting in a dead window sees
    // "extension disconnected". Traffic every 20s keeps the hands awake indefinitely.
    if (keepaliveTimer != null) clearInterval(keepaliveTimer);
    keepaliveTimer = setInterval(() => {
      if (!safeSend({ type: "ping", at: Date.now() }) && keepaliveTimer != null) {
        clearInterval(keepaliveTimer);
        keepaliveTimer = null;
      }
    }, 20000);
    safeSend({
      type: "hello",
      device_id: deviceId,
      build: EXTENSION_BUILD,
      capabilities: {
        active_control_indicator: true,
        atomic_credential_replay: true,
        atomic_targetable_scroll: true,
        exact_observation_target_identity: true,
        exact_final_action_page_binding: true,
        sensitive_page_evidence_redaction: true,
        sensitive_screenshot_region_masking: true,
        targetable_scroll_container: true,
        targetable_scroll_outcome_unknown: true,
        transient_semantic_screenshot_masking: true,
        transient_semantic_screenshot_mask_rebind: true,
        credential_replay_result_redaction: true,
        atomic_screenshot_observation: true,
      },
    });
  };
  ws.onclose = () => {
    connecting = false;
    if (keepaliveTimer != null) {
      clearInterval(keepaliveTimer);
      keepaliveTimer = null;
    }
    if (socket === ws) {
      socket = null;
      commandSocketEpoch += 1;
      void setControlIndicator({ active: false });
      scheduleReconnect();
    }
    setStatus(false);
  };
  ws.onerror = () => {
    connecting = false;
    if (keepaliveTimer != null) {
      clearInterval(keepaliveTimer);
      keepaliveTimer = null;
    }
    if (socket === ws) scheduleReconnect();
    setStatus(false);
  };
  ws.onmessage = async (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    if (message.type === "hud") {
      const payload = message.payload || {};
      await Promise.allSettled([rememberPanelPayload(payload), pushAttentionNotification(payload)]);
      return;
    }
    if (message.type !== "command") return;
    let result;
    try {
      if (message.method !== "set_control_indicator") {
        await refreshControlIndicatorLease();
      }
      result = await withTimeout(
        handleCommand(message.method, message.params),
        commandTimeoutMs(message.method, message.params),
        `${message.method || "command"} timed out`,
      );
    } catch (error) {
      // The bare message alone has proven undiagnosable ("error"); carry the error name
      // and the first stack frame so a failing command names its throw site.
      const stackHead = error && error.stack ? String(error.stack).split("\n").slice(0, 2).join(" | ").slice(0, 300) : "";
      const detail = `${error && error.name ? error.name + ": " : ""}${errorMessage(error)}${stackHead ? ` [${stackHead}]` : ""}`;
      safeSend({ type: "response", id: message.id, error: detail });
      return;
    }
    safeSend({ type: "response", id: message.id, result });
  };
}

chrome.runtime.onInstalled.addListener(() => { configureSidePanel(); connect(); });
chrome.runtime.onStartup.addListener(() => { configureSidePanel(); connect(); });
try {
  if (chrome.sidePanel && chrome.sidePanel.onOpened) {
    chrome.sidePanel.onOpened.addListener((info) => {
      rememberPanelWindowState(info && info.windowId, true);
    });
  }
  if (chrome.sidePanel && chrome.sidePanel.onClosed) {
    chrome.sidePanel.onClosed.addListener((info) => {
      rememberPanelWindowState(info && info.windowId, false);
    });
  }
} catch (_) { /* Side-panel lifecycle events require newer Chrome builds. */ }
// Chrome enforces a 30-second minimum for repeating MV3 alarms. The short in-worker timer
// handles normal disconnects; this valid alarm remains the recovery floor after worker eviction.
chrome.alarms.create("lumen-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === "lumen-keepalive") connect(); });
chrome.tabs.onRemoved.addListener(async (removedTabId) => {
  let storedId = null;
  try {
    const stored = await chrome.storage.local.get(["lumenTargetTabId"]);
    storedId = Number(stored && stored.lumenTargetTabId);
  } catch (_) {
    storedId = null;
  }
  if (Number(removedTabId) !== Number(targetTabId) && Number(removedTabId) !== storedId) return;
  targetTabId = null;
  clearTransientSensitiveScreenshotBinding();
  await chrome.storage.local.remove(TARGET_TAB_STORAGE_KEYS).catch(() => {});
});
chrome.tabs.onUpdated.addListener((updatedTabId, changeInfo) => {
  const binding = transientSensitiveScreenshotBinding;
  if (!binding || Number(updatedTabId) !== Number(binding.tab_id)) return;
  if (changeInfo && (changeInfo.status === "loading" || Object.prototype.hasOwnProperty.call(changeInfo, "url"))) {
    clearTransientSensitiveScreenshotBinding();
  }
});

// Messages from the side panel and the control-indicator content script.
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "LUMEN_CONTROL_INDICATOR_READY") {
    const senderTabId = Number(sender && sender.tab && sender.tab.id);
    activeTabId()
      .then((ownedTabId) => {
        const active = Boolean(
          controlIndicatorState.active
          && Number(ownedTabId) === senderTabId,
        );
        sendResponse(controlIndicatorPayload({ active }));
      })
      .catch(() => sendResponse(controlIndicatorPayload({ active: false })));
    return true;
  }
  if (message && message.type === "LUMEN_PANEL_STATE") {
    panelState()
      .then((result) => sendResponse(result))
      .catch((error) => sendResponse({ ok: false, error: errorMessage(error) }));
    return true;
  }
  if (message && message.type === "LUMEN_RECONNECT") {
    try { socket && socket.close(); } catch (_) {}
    socket = null;
    clearReconnectTimer();
    connect().then(() => sendResponse({ ok: true }));
    return true;
  }
  if (message && message.type === "LUMEN_HUD_CHAT_SEND") {
    postHudChatMessage(message)
      .then((result) => sendResponse(result))
      .catch((error) => sendResponse({ ok: false, error: errorMessage(error) }));
    return true;
  }
  if (message && message.type === "LUMEN_HUD_CHAT_SYNC") {
    fetchHudChatMessages(message)
      .then((result) => sendResponse(result))
      .catch((error) => sendResponse({ ok: false, error: errorMessage(error) }));
    return true;
  }
  return false;
});

configureSidePanel();
connect();
