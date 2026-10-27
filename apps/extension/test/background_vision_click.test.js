const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { JSDOM } = require("jsdom");

const BACKGROUND_SRC = fs.readFileSync(path.join(__dirname, "..", "src", "background.js"), "utf8");

function extractFunctionSource(name) {
  const asyncStart = BACKGROUND_SRC.indexOf(`async function ${name}`);
  const start = asyncStart >= 0 ? asyncStart : BACKGROUND_SRC.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `${name} not found`);
  const bodyStart = BACKGROUND_SRC.indexOf("{", start);
  let depth = 0;
  for (let i = bodyStart; i < BACKGROUND_SRC.length; i += 1) {
    const ch = BACKGROUND_SRC[i];
    if (ch === "{") depth += 1;
    if (ch === "}") depth -= 1;
    if (depth === 0) return BACKGROUND_SRC.slice(start, i + 1);
  }
  throw new Error(`${name} source did not close`);
}

function loadVisionEvidenceWindow(html) {
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const acquireDebugger = async () => ({ attached_now: false });
  const releaseDebugger = () => {};
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    url: "https://example.test/apply",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  Object.defineProperty(dom.window, "devicePixelRatio", { value: 1, configurable: true });
  Object.defineProperty(dom.window, "innerHeight", { value: 900, configurable: true });
  dom.window.Element.prototype.getBoundingClientRect = function () {
    const y = Number(this.getAttribute("data-y") || 0);
    const width = Number(this.getAttribute("data-width") || (this.matches("textarea,input,button") ? 500 : 480));
    const height = Number(this.getAttribute("data-height") || (this.matches("textarea") ? 90 : 28));
    return { left: 20, top: y, right: 20 + width, bottom: y + height, width, height, x: 20, y };
  };
  const fn = dom.window.eval(`(${extractFunctionSource("_visionPageEvidence")})`);
  return { dom, fn };
}

function loadSensitiveMaskWindow(html) {
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const acquireDebugger = async () => ({ attached_now: false });
  const releaseDebugger = () => {};
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    url: "https://example.test/recovery",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  Object.defineProperty(dom.window, "innerWidth", { value: 900, configurable: true });
  Object.defineProperty(dom.window, "innerHeight", { value: 700, configurable: true });
  dom.window.Element.prototype.getBoundingClientRect = function () {
    const y = Number(this.getAttribute("data-y") || 0);
    const width = Number(this.getAttribute("data-width") || 480);
    const height = Number(this.getAttribute("data-height") || 36);
    return { left: 20, top: y, right: 20 + width, bottom: y + height, width, height, x: 20, y };
  };
  return {
    dom,
    bind: dom.window.eval(`(${extractFunctionSource("bindSensitiveScreenshotControlsInPage")})`),
    mask: dom.window.eval(`(${extractFunctionSource("maskSensitiveScreenshotRegionsInPage")})`),
    paintBarrier: dom.window.eval(
      `(${extractFunctionSource("sensitiveScreenshotMaskPaintBarrierInPage")})`,
    ),
  };
}

function loadSensitiveMaskPaintProofRuntime({ reject = false } = {}) {
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const acquireDebugger = async () => ({ attached_now: false });
  const releaseDebugger = () => {};
  const calls = [];
  const chrome = {
    scripting: {
      executeScript: async ({ target }) => {
        calls.push(target);
        if (reject) throw new Error("paint probe timed out");
        return (target.frameIds || []).map((frameId) => ({
          frameId,
          result: { ok: true, mask_count: frameId === 7 ? 2 : 1 },
        }));
      },
    },
  };
  const withTimeout = async (promise) => promise;
  const sensitiveScreenshotMaskPaintBarrierInPage = () => {};
  const prove = eval(`(${extractFunctionSource("proveSensitiveScreenshotMasksPainted")})`);
  return { calls, prove };
}

function loadSensitiveMarkRuntime(html, { pageContextOk = true } = {}) {
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const acquireDebugger = async () => ({ attached_now: false });
  const releaseDebugger = () => {};
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    url: "https://example.test/recovery",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  let transientSensitiveScreenshotBinding = null;
  const injectedCalls = [];
  const clearTransientSensitiveScreenshotBinding = () => {
    transientSensitiveScreenshotBinding = null;
  };
  const observedPageContextVerdict = async (_tabId, expected) => (
    pageContextOk
      ? { ok: true, expected, live: { url: dom.window.location.href } }
      : { ok: false, reason: "page_context_drift", mismatch_field: "surface_signature" }
  );
  const observedSensitiveMaskPageContextVerdict = async (_tabId, _selector, expected) => (
    pageContextOk
      ? {
        ok: true,
        expected,
        live: {
          url: dom.window.location.href,
          mask_surface_signature: "value-free-mask-surface",
        },
      }
      : {
        ok: false,
        reason: "sensitive_mask_page_context_drift",
        mismatch_field: "mask_surface_signature",
      }
  );
  const tabPageEvidence = async () => ({
    url: dom.window.location.href,
    mask_surface_signature: "value-free-mask-surface",
  });
  const bindSensitiveScreenshotControlsInPage = dom.window.eval(
    `(${extractFunctionSource("bindSensitiveScreenshotControlsInPage")})`,
  );
  const normalizeSensitiveScreenshotSelectors = eval(
    `(${extractFunctionSource("normalizeSensitiveScreenshotSelectors")})`,
  );
  const chrome = {
    scripting: {
      executeScript: async ({ func, args, target }) => {
        injectedCalls.push({ args, target });
        return [{ frameId: 0, result: func(...args) }];
      },
    },
  };
  const withTimeout = async (promise) => promise;
  const runMarkSensitiveScreenshotControl = eval(
    `(${extractFunctionSource("runMarkSensitiveScreenshotControl")})`,
  );
  const runEnsureSensitiveScreenshotControl = eval(
    `(${extractFunctionSource("runEnsureSensitiveScreenshotControl")})`,
  );
  return {
    dom,
    injectedCalls,
    runMarkSensitiveScreenshotControl,
    runEnsureSensitiveScreenshotControl,
    binding: () => transientSensitiveScreenshotBinding,
    restartWorker: () => {
      transientSensitiveScreenshotBinding = null;
    },
  };
}

function loadSensitiveRebindScreenshotRuntime({ ensureOk = true } = {}) {
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const acquireDebugger = async () => ({ attached_now: false });
  const releaseDebugger = () => {};
  const calls = [];
  const activeTabId = async () => 44;
  const runEnsureSensitiveScreenshotControl = async (tabId, selector, expectedPageContext) => {
    calls.push({
      kind: "ensure",
      tabId,
      selector,
      expectedPageContext,
    });
    return ensureOk
      ? {
        ok: true,
        selector_bound: true,
        page_context_bound: true,
        mask_surface_signature: expectedPageContext.mask_surface_signature,
      }
      : {
        ok: false,
        selector_bound: false,
        page_context_bound: false,
        failure_reason: "sensitive_mask_page_context_drift",
      };
  };
  const doScreenshot = async (requiredSensitiveCapture = null) => {
    calls.push({ kind: "screenshot", requiredSensitiveCapture });
    return { ok: true, image: "masked-image" };
  };
  const withScreenshotCaptureLock = async (operation) => operation();
  const normalizeSensitiveScreenshotSelectors = eval(
    `(${extractFunctionSource("normalizeSensitiveScreenshotSelectors")})`,
  );
  const runScreenshotWithSensitiveMaskRebind = eval(
    `(${extractFunctionSource("runScreenshotWithSensitiveMaskRebind")})`,
  );
  return { calls, runScreenshotWithSensitiveMaskRebind };
}

function loadSensitiveRebindVisionRuntime({
  ensureOk = true,
  postContextOk = true,
} = {}) {
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const acquireDebugger = async () => ({ attached_now: false });
  const releaseDebugger = () => {};
  const calls = [];
  const activeTabId = async () => 44;
  const runEnsureSensitiveScreenshotControl = async (tabId, selector, expectedPageContext) => {
    calls.push({ kind: "ensure", tabId, selector, expectedPageContext });
    return ensureOk
      ? {
        ok: true,
        selector_bound: true,
        page_context_bound: true,
        mask_surface_signature: expectedPageContext.mask_surface_signature,
      }
      : {
        ok: false,
        failure_reason: "sensitive_mask_page_context_drift",
        page_context_mismatch_field: "mask_surface_signature",
      };
  };
  const observedSensitiveMaskPageContextVerdict = async (
    tabId,
    selector,
    expectedPageContext,
  ) => {
    calls.push({ kind: "post", tabId, selector, expectedPageContext });
    return postContextOk
      ? { ok: true }
      : {
        ok: false,
        reason: "sensitive_mask_page_context_drift",
        mismatch_field: "mask_surface_signature",
      };
  };
  const clearTransientSensitiveScreenshotBinding = () => {
    calls.push({ kind: "clear" });
  };
  const normalizeSensitiveScreenshotSelectors = eval(
    `(${extractFunctionSource("normalizeSensitiveScreenshotSelectors")})`,
  );
  const runVisionCommandWithSensitiveMaskRebind = eval(
    `(${extractFunctionSource("runVisionCommandWithSensitiveMaskRebind")})`,
  );
  return { calls, runVisionCommandWithSensitiveMaskRebind };
}

function loadScreenshotCaptureLock() {
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const acquireDebugger = async () => ({ attached_now: false });
  const releaseDebugger = () => {};
  let screenshotCaptureTail = Promise.resolve();
  const controlIndicatorState = { active: false };
  const activeTabId = async () => null;
  const applyControlIndicatorToTab = async () => ({ ok: true, visible: false });
  const setPointerOverlayCaptureHidden = async () => ({ ok: true, visible: false });
  return eval(`(${extractFunctionSource("withScreenshotCaptureLock")})`);
}

function loadTargetSurfaceRuntime({
  tabActive = true,
  windowFocused = false,
  loseFocusDuringPaint = false,
  paintOk = true,
} = {}) {
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const acquireDebugger = async () => ({ attached_now: false });
  const releaseDebugger = () => {};
  globalThis.__lumenPaintStalledAt = 0; // paint-stall memory is per-worker state; start each fixture fresh
  const calls = [];
  let active = tabActive;
  let focused = windowFocused;
  const chrome = {
    tabs: {
      get: async (tabId) => ({ id: tabId, windowId: 7, active }),
      update: async (tabId, update) => {
        calls.push({ kind: "tab_update", tabId, update });
        if (update && update.active) active = true;
        return { id: tabId, windowId: 7, active };
      },
    },
    windows: {
      get: async (windowId) => ({ id: windowId, focused }),
      update: async (windowId, update) => {
        calls.push({ kind: "window_update", windowId, update });
        if (update && update.focused) focused = true;
        return { id: windowId, focused };
      },
    },
    scripting: {
      executeScript: async ({ target, func }) => {
        calls.push({ kind: "paint", target, func: func.name });
        if (loseFocusDuringPaint) focused = false;
        return [{ result: { ok: paintOk, mask_count: 0 } }];
      },
    },
  };
  const withTimeout = async (promise) => promise;
  const errorMessage = (error) => (error && error.message ? String(error.message) : String(error || ""));
  const sensitiveScreenshotMaskPaintBarrierInPage = function sensitiveScreenshotMaskPaintBarrierInPage() {};
  const targetSurfaceForegroundVerdict = eval(
    `(${extractFunctionSource("targetSurfaceForegroundVerdict")})`,
  );
  const acquireTargetSurfaceAndWaitForPaint = eval(
    `(${extractFunctionSource("acquireTargetSurfaceAndWaitForPaint")})`,
  );
  return { calls, acquireTargetSurfaceAndWaitForPaint };
}

function loadScreenshotRuntime({
  primaryImage = "",
  debuggerImage = "fallback-image",
  driftAfterCapture = false,
  geometryDriftAfterCapture = false,
  scrollMovesAfterCapture = false,
  semanticMaskContext = null,
  initialEvidenceFailure = false,
  preCaptureProbeIncomplete = false,
  foregroundAfterCapture = true,
} = {}) {
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  globalThis.__lumenPaintStalledAt = 0; // paint-stall memory is per-worker state; start each fixture fresh
  let attachCalls = 0;
  let detachCalls = 0;
  let pageEvidenceReads = 0;
  const commands = [];
  const scriptTargets = [];
  const chrome = {
    runtime: { lastError: null },
    tabs: {
      get: async () => ({ id: 44, windowId: 7, active: true }),
      update: async () => ({}),
      captureVisibleTab: (_windowId, _options, callback) => {
        if (primaryImage) {
          chrome.runtime.lastError = null;
          callback(`data:image/jpeg;base64,${primaryImage}`);
          return;
        }
        chrome.runtime.lastError = { message: "primary capture failed" };
        callback("");
        chrome.runtime.lastError = null;
      },
    },
    windows: { update: async () => ({}) },
    scripting: {
      executeScript: async ({ func, target, args }) => {
        if (func.name === "_pageState") {
          return [{ result: { url: "https://example.test/apply", title: "Apply", inner_w: 900, inner_h: 700, dpr: 1, scroll_y: 0 } }];
        }
        if (func.name === "_visionPageEvidence" && initialEvidenceFailure) {
          throw new Error("vision page evidence timed out");
        }
        if (func.name === "maskSensitiveScreenshotRegionsInPage") {
          scriptTargets.push({ kind: "mask", target, args });
          return [{ frameId: 0, result: { ok: true, count: 0, rectangles: [] } }];
        }
        if (func.name === "clearSensitiveScreenshotRegionsInPage") {
          scriptTargets.push({ kind: "clear", target, args });
          return [{ frameId: 0, result: { ok: true, removed: 0 } }];
        }
        return [{ result: { viewport_text: "Application", controls: [] } }];
      },
    },
    debugger: {
      attach: (_target, _version, callback) => {
        attachCalls += 1;
        chrome.runtime.lastError = null;
        callback();
      },
      sendCommand: (_target, method, params, callback) => {
        commands.push({ method, params });
        chrome.runtime.lastError = null;
        callback({ data: debuggerImage });
      },
      detach: (_target, callback) => {
        detachCalls += 1;
        chrome.runtime.lastError = null;
        callback();
      },
    },
  };
  const withTimeout = async (promise) => promise;
  const settlePageMotion = async () => ({});
  // Stand-ins for the tab debugger session: one acquire/release per primitive is observable
  // as one attach/detach on the stubbed chrome.debugger.
  const acquireDebugger = (target) => new Promise((resolve, reject) => chrome.debugger.attach(target, "1.3", () => (
    chrome.runtime.lastError ? reject(new Error(chrome.runtime.lastError.message)) : resolve({ attached_now: true })
  )));
  const releaseDebugger = (target) => { chrome.debugger.detach(target, () => {}); };
  const errorMessage = (error) => (error && error.message ? String(error.message) : String(error || ""));
  const activeTabId = async () => 44;
  const sensitiveScreenshotMaskContextForTab = () => semanticMaskContext;
  const normalizeSensitiveScreenshotSelectors = eval(
    `(${extractFunctionSource("normalizeSensitiveScreenshotSelectors")})`,
  );
  const _pageState = function _pageState() {};
  const _visionPageEvidence = function _visionPageEvidence() {};
  const maskSensitiveScreenshotRegionsInPage = function maskSensitiveScreenshotRegionsInPage() {};
  const clearSensitiveScreenshotRegionsInPage = function clearSensitiveScreenshotRegionsInPage() {};
  const proveSensitiveScreenshotMasksPainted = async () => true;
  const acquireTargetSurfaceAndWaitForPaint = async () => ({
    ok: true,
    window_id: 7,
    tab_active: true,
    window_focused: true,
    paint_barrier_verified: true,
  });
  const targetSurfaceForegroundVerdict = async () => ({
    ok: foregroundAfterCapture,
    tab_active: true,
    window_focused: foregroundAfterCapture,
    window_id: 7,
  });
  const screenshotDataSha256 = async () => "fixture-screenshot-sha256";
  const surfaceSignatureFromEvidence = eval(`(${extractFunctionSource("surfaceSignatureFromEvidence")})`);
  const stableSurfaceSignatureFromEvidence = eval(
    `(${extractFunctionSource("stableSurfaceSignatureFromEvidence")})`,
  );
  const screenshotObservationContextMatches = eval(
    `(${extractFunctionSource("screenshotObservationContextMatches")})`,
  );
  const pageGeometryMoved = eval(`(${extractFunctionSource("pageGeometryMoved")})`);
  const fixtureEvidence = { viewport_text: "Application", controls: [] };
  const stableSurface = surfaceSignatureFromEvidence(fixtureEvidence);
  const stableStructuralSurface = stableSurfaceSignatureFromEvidence(fixtureEvidence);
  const tabPageEvidence = async () => {
    pageEvidenceReads += 1;
    return {
      url: "https://example.test/apply",
      tab_metadata_probe_complete: true,
      page_state_probe_complete: true,
      scroll_y: scrollMovesAfterCapture && pageEvidenceReads > 1 ? 40 : 0,
      surface_probe_complete: !(preCaptureProbeIncomplete && pageEvidenceReads === 1),
      surface_signature: (
        (driftAfterCapture || geometryDriftAfterCapture) && pageEvidenceReads > 1
          ? "changed-surface"
          : stableSurface
      ),
      stable_surface_signature: (
        driftAfterCapture && pageEvidenceReads > 1
          ? "changed-stable-surface"
          : stableStructuralSurface
      ),
      mask_surface_signature: String(
        (semanticMaskContext && semanticMaskContext.expected_mask_surface_signature)
        || "",
      ),
    };
  };
  const captureVisibleViewportWithDebugger = eval(`(${extractFunctionSource("captureVisibleViewportWithDebugger")})`);
  let screenshotDriftStreak = 0;
  const screenshotDriftTolerable = eval(`(${extractFunctionSource("screenshotDriftTolerable")})`);
  const doScreenshot = eval(`(${extractFunctionSource("doScreenshot")})`);
  return {
    doScreenshot,
    attachCalls: () => attachCalls,
    detachCalls: () => detachCalls,
    commands,
    scriptTargets,
  };
}

function loadCoordinateFingerprintRuntime({
  liveHashes = ["fixture-sha256"],
  captureErrors = [],
  priorRegionVerdict = null,
} = {}) {
  const pixelsMatchOutsideRegions = async () => priorRegionVerdict || { ok: false, why: "observed_image_unavailable" };
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const acquireDebugger = async () => ({ attached_now: false });
  const releaseDebugger = () => {};
  const maskContext = {
    required: true,
    document_token: "bound-page-token",
    expected_url: "https://example.test/apply",
    expected_mask_surface_signature: "mask-surface-17",
    selectors: ["#recovery-answer"],
    top_frame_only: true,
  };
  const scriptCalls = [];
  const timeline = [];
  let captureCount = 0;
  let hashCount = 0;
  const chrome = {
    runtime: { lastError: null },
    tabs: {
      get: async () => ({ id: 44, windowId: 7, active: true }),
      update: async () => ({}),
      captureVisibleTab: (_windowId, _options, callback) => {
        captureCount += 1;
        timeline.push(`capture:${captureCount}`);
        const captureError = String(captureErrors[captureCount - 1] || "");
        if (captureError) {
          chrome.runtime.lastError = { message: captureError };
          timeline.push(`capture-error:${captureError}`);
          callback("");
          chrome.runtime.lastError = null;
          return;
        }
        callback(`data:image/jpeg;base64,fixture-${captureCount}`);
      },
    },
    windows: { update: async () => ({}) },
    scripting: {
      executeScript: async (request) => {
        scriptCalls.push(request);
        if (request.func.name === "maskSensitiveScreenshotRegionsInPage") {
          timeline.push("mask");
          return [
            { frameId: 0, result: { ok: true, count: 1, rectangles: [] } },
            { frameId: 3, result: { ok: true, count: 0, rectangles: [] } },
          ];
        }
        timeline.push("clear");
        return [{ frameId: 0, result: { ok: true, removed: 1 } }];
      },
    },
  };
  const withTimeout = async (promise) => promise;
  const errorMessage = (error) => (error && error.message ? String(error.message) : String(error || ""));
  const sensitiveScreenshotMaskContextForTab = () => maskContext;
  const maskSensitiveScreenshotRegionsInPage = function maskSensitiveScreenshotRegionsInPage() {};
  const clearSensitiveScreenshotRegionsInPage = function clearSensitiveScreenshotRegionsInPage() {};
  const proveSensitiveScreenshotMasksPainted = async () => true;
  const acquireTargetSurfaceAndWaitForPaint = async () => ({
    ok: true,
    window_id: 7,
    tab_active: true,
    window_focused: true,
    paint_barrier_verified: true,
  });
  const targetSurfaceForegroundVerdict = async () => ({
    ok: true,
    tab_active: true,
    window_focused: true,
    window_id: 7,
  });
  const captureVisibleViewportWithDebugger = async () => ({ ok: false });
  const screenshotDataSha256 = async () => {
    const index = Math.min(hashCount, Math.max(0, liveHashes.length - 1));
    const hash = String(liveHashes[index] || "");
    hashCount += 1;
    timeline.push(`hash:${hash}`);
    return hash;
  };
  const setTimeout = (callback) => {
    timeline.push("retry-wait");
    callback();
    return 0;
  };
  const captureMaskedViewportFingerprint = eval(`(${extractFunctionSource("captureMaskedViewportFingerprint")})`);
  return {
    captureMaskedViewportFingerprint,
    maskContext,
    scriptCalls,
    timeline,
    captureCount: () => captureCount,
  };
}

test("viewport text keeps inline emphasis in reading order", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <p data-y="40">Operator note: your settings PIN is <strong>5239</strong>. Enter it in Settings.</p>
    <p data-y="90">Spec code: <code>QX7M</code> (see <a href="#more">details</a>).</p>
    <div data-y="140"><div>Block child stays separate</div></div>`);
  const evidence = fn([]);
  const text = String(evidence.viewport_text || "");
  assert.match(text, /settings PIN is 5239 ?\. Enter it in Settings\./);
  assert.match(text, /Spec code: QX7M \(see details ?\)\./);
  assert.match(text, /Block child stays separate/);
  assert.equal((text.match(/5239/g) || []).length, 1);
  dom.window.close();
});

test("a canvas is listed as a control before anything focuses it", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <p data-y="40">The intake form below exists only as pixels.</p>
    <canvas id="pf-canvas" width="400" height="200" data-y="100" data-height="200"></canvas>`);
  const controls = fn([]).controls || [];
  assert.ok(controls.some((control) => control.id === "pf-canvas" && control.tag === "canvas"));
  dom.window.close();
});

test("viewport text leaves out lines a scrolled panel clips out of view", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <p data-y="40">Find berth 214 in the manifest.</p>
    <div id="manifest" style="overflow: auto" data-y="100" data-height="120">
      <div data-y="-60">Berth 149 occupied</div>
      <div data-y="130">Berth 214 Claim</div>
      <div data-y="400">Berth 260 occupied</div>
    </div>`);
  const panel = dom.window.document.querySelector("#manifest");
  Object.defineProperty(panel, "scrollHeight", { value: 2000, configurable: true });
  Object.defineProperty(panel, "clientHeight", { value: 120, configurable: true });
  const text = String(fn([]).viewport_text || "");
  assert.match(text, /Find berth 214/);
  assert.match(text, /Berth 214 Claim/);
  assert.doesNotMatch(text, /Berth 149/);
  assert.doesNotMatch(text, /Berth 260/);
  dom.window.close();
});

test("target surface acquisition focuses an already-active tab and waits for paint", async () => {
  const runtime = loadTargetSurfaceRuntime({ tabActive: true, windowFocused: false });
  const result = await runtime.acquireTargetSurfaceAndWaitForPaint(44);

  assert.equal(result.ok, true);
  assert.equal(result.tab_active, true);
  assert.equal(result.window_focused, true);
  assert.equal(result.paint_barrier_verified, true);
  assert.deepEqual(runtime.calls.map((call) => call.kind), [
    "tab_update",
    "window_update",
    "paint",
  ]);
});

test("target surface acquisition fails closed when focus is lost during paint", async () => {
  const runtime = loadTargetSurfaceRuntime({ loseFocusDuringPaint: true });
  const result = await runtime.acquireTargetSurfaceAndWaitForPaint(44);

  assert.equal(result.ok, false);
  assert.equal(result.reason, "target_surface_lost_during_paint");
  assert.equal(result.window_focused, false);
});

test("screenshot falls back to the debugger viewport when captureVisibleTab fails", async () => {
  const runtime = loadScreenshotRuntime();
  const result = await runtime.doScreenshot();

  assert.equal(result.ok, true);
  assert.equal(result.image, "fallback-image");
  assert.equal(result.screenshot_mode, "debugger_capture");
  assert.equal(runtime.attachCalls(), 1);
  assert.equal(runtime.detachCalls(), 1);
  assert.deepEqual(runtime.commands.map(({ method }) => method), ["Page.captureScreenshot"]);
  assert.equal(runtime.commands[0].params.captureBeyondViewport, false);
});

test("screenshot capture lock serializes mask injection through cleanup", async () => {
  const withScreenshotCaptureLock = loadScreenshotCaptureLock();
  const calls = [];
  let releaseFirst = () => {};
  const firstMayFinish = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  const first = withScreenshotCaptureLock(async () => {
    calls.push("first:start");
    await firstMayFinish;
    calls.push("first:end");
  });
  const second = withScreenshotCaptureLock(async () => {
    calls.push("second:start");
    calls.push("second:end");
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, ["first:start"]);
  releaseFirst();
  await Promise.all([first, second]);
  assert.deepEqual(calls, [
    "first:start",
    "first:end",
    "second:start",
    "second:end",
  ]);
});

test("successful primary screenshot does not attach the debugger fallback", async () => {
  const runtime = loadScreenshotRuntime({ primaryImage: "primary-image" });
  const result = await runtime.doScreenshot();

  assert.equal(result.ok, true);
  assert.equal(result.image, "primary-image");
  assert.equal(result.screenshot_mode, "capture_visible_tab");
  assert.equal(runtime.attachCalls(), 0);
  assert.equal(runtime.detachCalls(), 0);
  assert.deepEqual(runtime.commands, []);
  assert.ok(runtime.scriptTargets.length >= 2);
  assert.ok(runtime.scriptTargets.every(({ target }) => target && target.allFrames === true));
});

test("screenshot classifies a timed-out evidence probe as unavailable, not semantic drift", async () => {
  const runtime = loadScreenshotRuntime({
    primaryImage: "must-not-be-captured",
    initialEvidenceFailure: true,
  });
  const result = await runtime.doScreenshot();

  assert.equal(result.ok, false);
  assert.equal(result.failure_reason, "screenshot_observation_evidence_unavailable");
  assert.equal(result.evidence_stage, "initial_observation");
  assert.equal(result.surface_probe_complete, false);
  assert.equal(result.image_discarded, true);
  assert.equal(runtime.scriptTargets.length, 0);
});

test("screenshot discards captured pixels when the target window loses focus", async () => {
  const runtime = loadScreenshotRuntime({
    primaryImage: "stale-window-image",
    foregroundAfterCapture: false,
  });
  const result = await runtime.doScreenshot();

  assert.equal(result.ok, false);
  assert.equal(result.failure_reason, "screenshot_target_surface_lost");
  assert.equal(result.image_discarded, true);
  assert.equal(result.window_focused, false);
});

test("screenshot refuses a partial pre-capture context instead of comparing empty evidence", async () => {
  const runtime = loadScreenshotRuntime({
    primaryImage: "must-not-be-captured",
    preCaptureProbeIncomplete: true,
  });
  const result = await runtime.doScreenshot();

  assert.equal(result.ok, false);
  assert.equal(result.failure_reason, "screenshot_observation_evidence_unavailable");
  assert.equal(result.evidence_stage, "pre_capture_context");
  assert.equal(result.surface_probe_complete, false);
  assert.equal(result.image_discarded, true);
});

test("primary screenshot passes transient semantic selectors to all-frame masking", async () => {
  const semanticMaskContext = {
    required: true,
    document_token: "bound-page-token",
    expected_url: "https://example.test/apply",
    expected_mask_surface_signature: "mask-surface-17",
    selectors: ["#recovery-answer"],
    top_frame_only: true,
  };
  const runtime = loadScreenshotRuntime({
    primaryImage: "primary-image",
    semanticMaskContext,
  });
  const result = await runtime.doScreenshot();

  assert.equal(result.ok, true);
  const maskCall = runtime.scriptTargets.find(({ kind }) => kind === "mask");
  assert.ok(maskCall);
  assert.equal(maskCall.target.allFrames, true);
  assert.deepEqual(maskCall.args, [semanticMaskContext]);
});

test("coordinate screenshot recapture applies the same transient masks in every frame", async () => {
  const runtime = loadCoordinateFingerprintRuntime();
  const result = await runtime.captureMaskedViewportFingerprint(44, {
    screenshot_sha256: "fixture-sha256",
    screenshot_mode: "capture_visible_tab",
    screenshot_quality: 60,
  });

  assert.equal(result.ok, true);
  assert.equal(result.capture_attempts, 1);
  assert.equal(result.temporal_exact_hash_retry_attempted, false);
  assert.equal(result.temporal_exact_hash_recovered, false);
  const maskCall = runtime.scriptCalls.find(
    (request) => request.func.name === "maskSensitiveScreenshotRegionsInPage",
  );
  assert.ok(maskCall);
  assert.equal(maskCall.target.allFrames, true);
  assert.deepEqual(maskCall.args, [runtime.maskContext]);
  const clearCall = runtime.scriptCalls.find(
    (request) => request.func.name === "clearSensitiveScreenshotRegionsInPage",
  );
  assert.ok(clearCall);
  assert.equal(clearCall.target.allFrames, true);
});

test("coordinate screenshot recapture can recover only from a later exact temporal hash", async () => {
  const runtime = loadCoordinateFingerprintRuntime({
    liveHashes: ["caret-opposite-phase", "fixture-sha256"],
  });
  const result = await runtime.captureMaskedViewportFingerprint(44, {
    screenshot_sha256: "fixture-sha256",
    screenshot_mode: "capture_visible_tab",
    screenshot_quality: 60,
  });

  assert.equal(result.ok, true);
  assert.equal(result.live_screenshot_sha256, "fixture-sha256");
  assert.equal(result.capture_attempts, 2);
  assert.equal(result.temporal_exact_hash_retry_attempted, true);
  assert.equal(result.temporal_exact_hash_recovered, true);
  assert.equal(runtime.captureCount(), 2);
  assert.equal(
    runtime.scriptCalls.filter(
      (request) => request.func.name === "maskSensitiveScreenshotRegionsInPage",
    ).length,
    2,
  );
  assert.equal(
    runtime.scriptCalls.filter(
      (request) => request.func.name === "clearSensitiveScreenshotRegionsInPage",
    ).length,
    1,
  );
  assert.deepEqual(runtime.timeline, [
    "mask",
    "capture:1",
    "hash:caret-opposite-phase",
    "retry-wait",
    "mask",
    "capture:2",
    "hash:fixture-sha256",
    "clear",
  ]);
});

test("coordinate screenshot recapture retries a transient Chrome capture quota failure", async () => {
  const quotaError = "This request exceeds the MAX_CAPTURE_VISIBLE_TAB_CALLS_PER_SECOND quota.";
  const runtime = loadCoordinateFingerprintRuntime({
    captureErrors: [quotaError],
    liveHashes: ["fixture-sha256"],
  });
  const result = await runtime.captureMaskedViewportFingerprint(44, {
    screenshot_sha256: "fixture-sha256",
    screenshot_mode: "capture_visible_tab",
    screenshot_quality: 60,
  });

  assert.equal(result.ok, true);
  assert.equal(result.live_screenshot_sha256, "fixture-sha256");
  assert.equal(result.capture_attempts, 2);
  assert.equal(result.capture_error_retry_attempted, true);
  assert.equal(result.capture_error_recovered, true);
  assert.equal(runtime.captureCount(), 2);
  assert.deepEqual(runtime.timeline, [
    "mask",
    "capture:1",
    `capture-error:${quotaError}`,
    "retry-wait",
    "mask",
    "capture:2",
    "hash:fixture-sha256",
    "clear",
  ]);
});

test("coordinate screenshot recapture exhausts transient capture failures without activating", async () => {
  const quotaError = "This request exceeds the MAX_CAPTURE_VISIBLE_TAB_CALLS_PER_SECOND quota.";
  const runtime = loadCoordinateFingerprintRuntime({
    captureErrors: [quotaError, quotaError, quotaError],
  });
  const result = await runtime.captureMaskedViewportFingerprint(44, {
    screenshot_sha256: "fixture-sha256",
    screenshot_mode: "capture_visible_tab",
    screenshot_quality: 60,
  });

  assert.equal(result.ok, false);
  assert.equal(result.reason, "coordinate_pixel_recapture_failed");
  assert.equal(result.error, quotaError);
  assert.equal(result.capture_attempts, 3);
  assert.equal(result.capture_error_retry_attempted, true);
  assert.equal(result.capture_error_recovered, false);
  assert.equal(runtime.captureCount(), 3);
  assert.equal(runtime.timeline.at(-1), "clear");
});

test("coordinate screenshot temporal recapture remains bounded and fails closed without an exact hash", async () => {
  const runtime = loadCoordinateFingerprintRuntime({
    liveHashes: ["changed-1", "changed-2", "changed-3", "would-match-too-late"],
  });
  const result = await runtime.captureMaskedViewportFingerprint(44, {
    screenshot_sha256: "would-match-too-late",
    screenshot_mode: "capture_visible_tab",
    screenshot_quality: 60,
  });

  assert.equal(result.ok, false);
  assert.equal(result.reason, "coordinate_pixel_drift");
  assert.equal(result.live_screenshot_sha256, "changed-3");
  assert.equal(result.capture_attempts, 3);
  assert.equal(result.temporal_exact_hash_retry_attempted, true);
  assert.equal(result.temporal_exact_hash_recovered, false);
  assert.equal(runtime.captureCount(), 3);
  assert.equal(
    runtime.scriptCalls.filter(
      (request) => request.func.name === "maskSensitiveScreenshotRegionsInPage",
    ).length,
    3,
  );
  assert.equal(
    runtime.scriptCalls.filter(
      (request) => request.func.name === "clearSensitiveScreenshotRegionsInPage",
    ).length,
    1,
  );
  assert.equal(runtime.timeline.at(-1), "clear");
});

test("screenshot discards pixels when the bracketed page surface drifts", async () => {
  const runtime = loadScreenshotRuntime({ primaryImage: "primary-image", driftAfterCapture: true });
  const result = await runtime.doScreenshot();

  assert.equal(result.ok, false);
  assert.equal(result.failure_reason, "screenshot_observation_drift");
  assert.equal(result.image_discarded, true);
  assert.equal(result.after_surface_signature, "changed-surface");
});

test("screenshot accepts a volatile same-document capture after repeated surface drift", async () => {
  const runtime = loadScreenshotRuntime({ primaryImage: "primary-image", driftAfterCapture: true });
  const first = await runtime.doScreenshot();
  const second = await runtime.doScreenshot();
  const third = await runtime.doScreenshot();

  assert.equal(first.failure_reason, "screenshot_observation_drift");
  assert.equal(second.failure_reason, "screenshot_observation_drift");
  assert.equal(third.ok, true);
  assert.equal(third.observation_volatile, true);
  assert.equal(third.image, "primary-image");
});

test("screenshot never tolerates drift across a document replacement", async () => {
  const screenshotDriftStreak = 0;
  const screenshotDriftTolerable = eval(`(${extractFunctionSource("screenshotDriftTolerable")})`);
  const before = { url: "https://example.test/apply", document_lifecycle_token: "doc-1" };
  const replaced = { url: "https://example.test/apply", document_lifecycle_token: "doc-2" };
  const moved = { url: "https://example.test/done", document_lifecycle_token: "doc-1" };

  for (let i = 0; i < 5; i += 1) {
    assert.equal(screenshotDriftTolerable(before, replaced, ""), false);
    assert.equal(screenshotDriftTolerable(before, moved, ""), false);
    assert.equal(screenshotDriftTolerable(before, before, "mask-surface"), false);
  }
});

test("target surface acquisition degrades to an unverified paint instead of failing the capture", async () => {
  const runtime = loadTargetSurfaceRuntime({ paintOk: false });
  const result = await runtime.acquireTargetSurfaceAndWaitForPaint(44);

  assert.equal(result.ok, true);
  assert.equal(result.paint_barrier_verified, false);
  assert.equal(result.window_id, 7);
});

test("target surface acquisition still fails closed for sensitive-mask captures without a verified paint", async () => {
  const runtime = loadTargetSurfaceRuntime({ paintOk: false });
  const result = await runtime.acquireTargetSurfaceAndWaitForPaint(44, true);

  assert.equal(result.ok, false);
  assert.equal(result.reason, "target_surface_paint_unverified");
});

test("click identity verdict follows a re-rendered control by semantics unless strict", () => {
  const clickTargetIdentityVerdict = eval(`(${extractFunctionSource("clickTargetIdentityVerdict")})`);
  const clickTargetIdentityFromPoint = eval(`(${extractFunctionSource("clickTargetIdentityFromPoint")})`);
  const expected = {
    target_selector: "#m-abc123",
    target_text: "Verify identity",
    target_tag: "button",
    target_type: "",
    target_role: "",
    dispatch_path: "dom_activation",
    page_url: "https://example.test/deck",
  };
  const rerendered = { ...expected, target_selector: "#m-xyz789" };
  const swapped = { ...rerendered, target_text: "Delete records" };

  const relaxed = clickTargetIdentityVerdict(expected, rerendered);
  assert.equal(relaxed.ok, true);
  assert.equal(relaxed.selector_rebound, true);
  assert.equal(clickTargetIdentityVerdict(expected, swapped).ok, false);
  assert.equal(clickTargetIdentityVerdict(expected, swapped).mismatch_field, "target_text");
  assert.equal(clickTargetIdentityVerdict(expected, rerendered, true).ok, false);
  assert.equal(clickTargetIdentityVerdict(expected, rerendered, true).mismatch_field, "target_selector");
  const iconOnly = { ...expected, target_text: "" };
  assert.equal(clickTargetIdentityVerdict(iconOnly, { ...iconOnly, target_selector: "#m-other" }).ok, false);
  assert.equal(typeof clickTargetIdentityFromPoint, "function");
});

test("click identity accepts a label whose live widget tail changed, only on the same element", () => {
  const sameLeadingIdentityText = eval(`(${extractFunctionSource("sameLeadingIdentityText")})`);
  const clickTargetIdentityVerdict = eval(`(${extractFunctionSource("clickTargetIdentityVerdict")})`);
  const clickTargetIdentityFromPoint = eval(`(${extractFunctionSource("clickTargetIdentityFromPoint")})`);
  assert.equal(typeof clickTargetIdentityFromPoint, "function");
  // Lever's location typeahead (run 1792): the wrapping label carries the dropdown's status text.
  const expected = {
    target_selector: "#location-input",
    target_text: "Current location ✱ No location found. Try entering a different location Loading",
    target_tag: "input",
    target_type: "text",
    target_role: "",
    dispatch_path: "dom_activation",
    page_url: "https://jobs.lever.co/acme/1/apply",
  };
  const live = { ...expected, target_text: "Current location ✱" };
  assert.equal(sameLeadingIdentityText(expected.target_text, live.target_text), true);
  assert.equal(clickTargetIdentityVerdict(expected, live).ok, true);
  // Not across elements, not mid-word, not for strict identity.
  assert.equal(clickTargetIdentityVerdict(expected, { ...live, target_selector: "#other" }).ok, false);
  assert.equal(sameLeadingIdentityText("Current location", "Current locations"), false);
  // Greenhouse (run 1795): the observer read "First Name *", the live label is "First Name*".
  assert.equal(sameLeadingIdentityText("First Name *", "First Name*"), true);
  assert.equal(
    clickTargetIdentityVerdict(
      { ...expected, target_selector: "#first_name", target_text: "First Name *", target_type: "text" },
      { ...live, target_selector: "#first_name", target_text: "First Name*", target_type: "text" },
    ).ok,
    true,
  );
  assert.equal(sameLeadingIdentityText("Email *", "Email2*"), false);
  assert.equal(sameLeadingIdentityText("Cur", "Cur x"), false);
  assert.equal(clickTargetIdentityVerdict(expected, live, true).ok, false);
});

test("click identity treats the observer's tag-name type fallback as no explicit type", () => {
  const clickTargetIdentityVerdict = eval(`(${extractFunctionSource("clickTargetIdentityVerdict")})`);
  const clickTargetIdentityFromPoint = eval(`(${extractFunctionSource("clickTargetIdentityFromPoint")})`);
  const observed = {
    target_selector: "#rr-old",
    target_text: "Verify identity",
    target_tag: "button",
    target_type: "button", // observe.js input_type fallback for <button> without a type attribute
    target_role: "",
    dispatch_path: "dom_activation",
    page_url: "https://example.test/deck",
  };
  const probed = { ...observed, target_selector: "#rr-new", target_type: "" };
  assert.equal(clickTargetIdentityFromPoint(observed).target_type, "");
  assert.equal(clickTargetIdentityFromPoint({ ...observed, target_type: "submit" }).target_type, "submit");
  assert.equal(clickTargetIdentityVerdict(observed, probed).ok, true);
  assert.equal(clickTargetIdentityVerdict({ ...observed, target_type: "submit" }, probed).ok, false);
});

test("click identity accepts any observed name of a labelled input, never a stranger's", () => {
  // eslint-disable-next-line no-unused-vars -- resolved by the evaluated verdict below
  const sameLeadingIdentityText = eval(`(${extractFunctionSource("sameLeadingIdentityText")})`);
  const clickTargetIdentityVerdict = eval(`(${extractFunctionSource("clickTargetIdentityVerdict")})`);
  // eslint-disable-next-line no-unused-vars -- resolved by the evaluated verdict above
  const clickTargetIdentityFromPoint = eval(`(${extractFunctionSource("clickTargetIdentityFromPoint")})`);
  const observed = {
    target_selector: "#amount-input",
    target_text: "0.00", // the observer's own-text fallback reached the placeholder
    target_names: ["0.00", "Amount for LX-437"],
    target_tag: "input",
    target_type: "",
    target_role: "",
    dispatch_path: "dom_activation",
    page_url: "https://example.test/ledger",
  };
  const probed = { ...observed, target_names: undefined, target_text: "Amount for LX-437" };
  assert.equal(clickTargetIdentityVerdict(observed, probed).ok, true);
  assert.equal(clickTargetIdentityVerdict(observed, { ...probed, target_text: "Card number" }).ok, false);
  assert.equal(clickTargetIdentityVerdict(observed, probed, true).ok, false);
});

test("a non-strict click accepts a different same-tag control whose text is an observed alias (existing twin behaviour)", () => {
  // Documents the current identity logic, unchanged: a non-strict verdict matches the live target_text against
  // every observed name (target_names) without requiring the same selector. So a different button of the same tag,
  // type and role, whose text equals an observed alias, passes a non-strict check. A strict (final-submit) check
  // still binds the one exact element, and a different tag or text is refused either way.
  const clickTargetIdentityVerdict = eval(`(${extractFunctionSource("clickTargetIdentityVerdict")})`);
  // eslint-disable-next-line no-unused-vars -- resolved by the evaluated verdict above
  const clickTargetIdentityFromPoint = eval(`(${extractFunctionSource("clickTargetIdentityFromPoint")})`);
  const observed = {
    target_selector: "#next-step",
    target_text: "Continue",
    target_names: ["Continue", "Next step"],
    target_tag: "button",
    target_type: "button",
    target_role: "",
    dispatch_path: "dom_activation",
    page_url: "https://example.test/apply",
  };
  const twin = { ...observed, target_selector: "#save-draft", target_text: "Next step", target_names: undefined };
  const alias = clickTargetIdentityVerdict(observed, twin);
  assert.equal(alias.ok, true, "the alias text matches the observed names, whatever the selector");
  assert.equal(alias.selector_rebound, true);
  const strict = clickTargetIdentityVerdict(observed, twin, true);
  assert.equal(strict.ok, false);
  assert.equal(strict.mismatch_field, "target_selector");
  assert.equal(clickTargetIdentityVerdict(observed, { ...twin, target_tag: "a" }).ok, false);
  assert.equal(clickTargetIdentityVerdict(observed, { ...twin, target_text: "Cancel" }).ok, false);
});

test("page-context verdict reports content drift without blocking ordinary clicks", async () => {
  const tabPageEvidence = async () => ({
    url: "https://example.test/deck",
    stable_surface_signature: "re-rendered-panel",
    document_lifecycle_token: "doc-1",
  });
  const observedPageContextVerdict = eval(`(${extractFunctionSource("observedPageContextVerdict")})`);
  const expected = {
    url: "https://example.test/deck",
    stable_surface_signature: "observed-panel",
    document_lifecycle_token: "doc-1",
  };

  const relaxed = await observedPageContextVerdict(1, expected, false);
  assert.equal(relaxed.ok, true);
  assert.equal(relaxed.page_drifted, true);
  assert.equal(relaxed.drift_field, "stable_surface_signature");
  const strict = await observedPageContextVerdict(1, expected, true);
  assert.equal(strict.ok, false);
  assert.equal(strict.mismatch_field, "stable_surface_signature");
});

test("page-context verdict reports an unfinished probe as unavailable, not as a new document", async () => {
  // Run 1794: the evidence probe timed out on iCIMS, its blank lifecycle token read as a document change,
  // and every credential replay on that page was refused as document_lifecycle_token drift.
  // eslint-disable-next-line no-unused-vars -- resolved by the evaluated verdict below
  const tabPageEvidence = async () => ({ url: "https://example.test/apply", surface_probe_complete: false });
  const observedPageContextVerdict = eval(`(${extractFunctionSource("observedPageContextVerdict")})`);
  const verdict = await observedPageContextVerdict(1, { url: "https://example.test/apply", document_lifecycle_token: "doc-1" });
  assert.equal(verdict.ok, false);
  assert.equal(verdict.reason, "page_context_unavailable");
  assert.equal(verdict.mismatch_field, "surface_probe");
});

test("a window the OS is not painting is captured from its surface without waiting on captureVisibleTab", async () => {
  const runtime = loadScreenshotRuntime({ primaryImage: "primary-image", debuggerImage: "surface-image" });
  globalThis.__lumenPaintStalledAt = Date.now();
  const stalled = await runtime.doScreenshot();
  assert.equal(stalled.ok, true);
  assert.equal(stalled.image, "surface-image");
  assert.ok(runtime.commands.some((command) => command.method === "Page.captureScreenshot"));
  assert.equal(typeof stalled.capture_timing_ms.total, "number");

  const painting = loadScreenshotRuntime({ primaryImage: "primary-image", debuggerImage: "surface-image" });
  const normal = await painting.doScreenshot();
  assert.equal(normal.image, "primary-image");
  assert.equal(painting.commands.length, 0);
});

test("screenshot keeps pixels when only control geometry drifts", async () => {
  const runtime = loadScreenshotRuntime({
    primaryImage: "primary-image",
    geometryDriftAfterCapture: true,
  });
  const result = await runtime.doScreenshot();

  assert.equal(result.ok, true);
  assert.equal(result.image, "primary-image");
  assert.equal(result.stable_surface_signature.length > 0, true);
});

test("screenshot is observed again when the page scrolled between the boxes and the capture", async () => {
  const runtime = loadScreenshotRuntime({ primaryImage: "primary-image", scrollMovesAfterCapture: true });
  const first = await runtime.doScreenshot();
  assert.equal(first.ok, false);
  assert.equal(first.failure_reason, "screenshot_observation_drift");
  assert.equal(first.drift_kind, "geometry_moved_during_capture");
  assert.equal(first.retryable, true);
  const second = await runtime.doScreenshot();
  assert.equal(second.ok, false);
  // A page that never settles is not retried forever: the third capture is accepted as volatile.
  const third = await runtime.doScreenshot();
  assert.equal(third.ok, true);
  assert.equal(third.observation_volatile, true);
});

test("sensitive-control command binds only selector and page context, never the control value", async () => {
  const rawSecret = "do-not-cross-the-extension-channel";
  const runtime = loadSensitiveMarkRuntime(
    `<div id="recovery-answer" contenteditable="true">${rawSecret}</div>`,
  );
  const result = await runtime.runMarkSensitiveScreenshotControl(
    44,
    "#recovery-answer",
    {
      url: runtime.dom.window.location.href,
      surface_signature: "observed-surface",
    },
  );

  assert.equal(result.ok, true);
  assert.equal(result.selector_bound, true);
  assert.equal(result.page_context_bound, true);
  assert.equal(result.mask_surface_signature, "value-free-mask-surface");
  assert.equal(runtime.injectedCalls.length, 1);
  assert.equal(runtime.injectedCalls[0].target.tabId, 44);
  assert.deepEqual(runtime.injectedCalls[0].args[0], ["#recovery-answer"]);
  assert.equal(runtime.injectedCalls[0].args.length, 2);
  assert.equal(JSON.stringify(runtime.injectedCalls).includes(rawSecret), false);
  const binding = runtime.binding();
  assert.deepEqual(Object.keys(binding).sort(), [
    "document_token",
    "expected_mask_surface_signature",
    "expected_url",
    "selectors",
    "tab_id",
  ]);
  assert.equal(binding.expected_mask_surface_signature, "value-free-mask-surface");
  assert.deepEqual(Array.from(binding.selectors), ["#recovery-answer"]);
  assert.equal(JSON.stringify({
    ...binding,
    selectors: Array.from(binding.selectors),
  }).includes(rawSecret), false);
});

test("sensitive mask surface identity is independent of the entered value", async () => {
  const signature = eval(
    `(${extractFunctionSource("sensitiveMaskSurfaceSignatureFromEvidence")})`,
  );
  const before = await signature({
    viewport_text: "Recovery phrase [REDACTED]",
    controls: [
      {
        selector: "#recovery-answer",
        tag: "input",
        role: "textbox",
        type: "text",
        id: "recovery-answer",
        name: "recovery",
        text: "Recovery phrase",
        value: "",
        value_redacted: true,
        disabled: true,
        x: 20,
        y: 100,
      },
      {
        selector: "#ordinary-answer",
        tag: "input",
        role: "textbox",
        type: "text",
        id: "ordinary-answer",
        name: "ordinary",
        value: "ordinary value before",
      },
    ],
  });
  const after = await signature({
    viewport_text: "Recovery phrase [REDACTED]",
    controls: [
      {
        selector: "#recovery-answer",
        tag: "input",
        role: "textbox",
        type: "text",
        id: "recovery-answer",
        name: "recovery",
        text: "Recovery phrase",
        value: "redacted direct-entry value",
        value_redacted: true,
        disabled: false,
        x: 20,
        y: 140,
      },
      {
        selector: "#ordinary-answer",
        tag: "input",
        role: "textbox",
        type: "text",
        id: "ordinary-answer",
        name: "ordinary",
        value: "ordinary value after",
      },
    ],
  });
  const changedSurface = await signature({
    viewport_text: "Different recovery prompt [REDACTED]",
    controls: [{
      selector: "#recovery-answer",
      tag: "input",
      role: "textbox",
      type: "text",
      id: "recovery-answer",
      name: "recovery",
      text: "Different recovery prompt",
      value: "redacted direct-entry value",
      value_redacted: true,
    }],
  });

  assert.equal(before, after);
  assert.match(before, /^sha256:[0-9a-f]{64}$/);
  assert.notEqual(before, changedSurface);
  assert.equal(before.includes("redacted direct-entry value"), false);
});

test("sensitive-control ensure command rebinds after worker restart before observation", async () => {
  const rawSecret = "value-entered-directly-in-chrome";
  const runtime = loadSensitiveMarkRuntime(
    `<div id="recovery-answer" contenteditable="true">${rawSecret}</div>`,
  );
  const initial = await runtime.runMarkSensitiveScreenshotControl(
    44,
    "#recovery-answer",
    {
      url: runtime.dom.window.location.href,
      surface_signature: "observed-surface",
    },
  );
  assert.equal(initial.ok, true);

  runtime.restartWorker();
  assert.equal(runtime.binding(), null);
  const rebound = await runtime.runEnsureSensitiveScreenshotControl(
    44,
    "#recovery-answer",
    {
      url: runtime.dom.window.location.href,
      mask_surface_signature: initial.mask_surface_signature,
    },
  );

  assert.equal(rebound.ok, true);
  assert.equal(rebound.action, "ensure_sensitive_screenshot_control");
  assert.equal(rebound.binding_rebound, true);
  assert.equal(rebound.mask_surface_signature, initial.mask_surface_signature);
  assert.deepEqual(Array.from(runtime.binding().selectors), ["#recovery-answer"]);
  assert.equal(JSON.stringify(runtime.injectedCalls).includes(rawSecret), false);
  assert.equal(JSON.stringify(rebound).includes(rawSecret), false);
});

test("worker restart rebind preserves every previously marked semantic selector", async () => {
  const runtime = loadSensitiveMarkRuntime(`
    <input id="first-secret" type="text" value="first-direct-value">
    <input id="second-secret" type="text" value="second-direct-value">
  `);
  const first = await runtime.runMarkSensitiveScreenshotControl(
    44,
    "#first-secret",
    {
      url: runtime.dom.window.location.href,
      surface_signature: "observed-surface",
    },
  );
  const second = await runtime.runMarkSensitiveScreenshotControl(
    44,
    "#second-secret",
    {
      url: runtime.dom.window.location.href,
      surface_signature: "observed-surface",
    },
  );

  assert.deepEqual(second.bound_selectors, ["#first-secret", "#second-secret"]);
  runtime.restartWorker();
  const rebound = await runtime.runEnsureSensitiveScreenshotControl(
    44,
    second.bound_selectors,
    {
      url: runtime.dom.window.location.href,
      mask_surface_signature: second.mask_surface_signature,
    },
  );

  assert.equal(first.ok, true);
  assert.equal(rebound.ok, true);
  assert.deepEqual(rebound.bound_selectors, ["#first-secret", "#second-secret"]);
  assert.deepEqual(
    Array.from(runtime.binding().selectors),
    ["#first-secret", "#second-secret"],
  );
  assert.equal(
    JSON.stringify({ rebound, calls: runtime.injectedCalls })
      .includes("first-direct-value"),
    false,
  );
  assert.equal(
    JSON.stringify({ rebound, calls: runtime.injectedCalls })
      .includes("second-direct-value"),
    false,
  );
});

test("post-continue screenshot atomically ensures the value-free mask binding first", async () => {
  const runtime = loadSensitiveRebindScreenshotRuntime();
  const result = await runtime.runScreenshotWithSensitiveMaskRebind({
    required_sensitive_mask_binding: {
      selector: "#recovery-answer",
      expected_page_context: {
        url: "https://example.test/recovery",
        mask_surface_signature: "value-free-mask-surface",
      },
    },
  });

  assert.equal(result.ok, true);
  assert.deepEqual(runtime.calls.map((call) => call.kind), ["ensure", "screenshot"]);
  assert.deepEqual(runtime.calls[0].selector, ["#recovery-answer"]);
  assert.deepEqual(runtime.calls[1].requiredSensitiveCapture, {
    tab_id: 44,
    selectors: ["#recovery-answer"],
    expected_page_context: {
      url: "https://example.test/recovery",
      mask_surface_signature: "value-free-mask-surface",
    },
  });
});

test("post-continue screenshot returns no pixels when mask rebind fails", async () => {
  const runtime = loadSensitiveRebindScreenshotRuntime({ ensureOk: false });
  const result = await runtime.runScreenshotWithSensitiveMaskRebind({
    required_sensitive_mask_binding: {
      selector: "#recovery-answer",
      expected_page_context: {
        url: "https://example.test/recovery",
        mask_surface_signature: "value-free-mask-surface",
      },
    },
  });

  assert.equal(result.ok, false);
  assert.equal(result.failure_reason, "sensitive_mask_page_context_drift");
  assert.equal(result.image_discarded, true);
  assert.deepEqual(runtime.calls.map((call) => call.kind), ["ensure"]);
});

test("screenshot returns no pixels when the rebound context disappears before capture", async () => {
  const runtime = loadScreenshotRuntime({
    primaryImage: "must-not-be-captured",
    semanticMaskContext: null,
  });
  const result = await runtime.doScreenshot({
    tab_id: 44,
    selector: "#recovery-answer",
    expected_page_context: {
      url: "https://example.test/apply",
      mask_surface_signature: "value-free-mask-surface",
    },
  });

  assert.equal(result.ok, false);
  assert.equal(result.failure_reason, "sensitive_mask_rebind_lost_before_capture");
  assert.equal(result.image_discarded, true);
  assert.deepEqual(runtime.scriptTargets, []);
  assert.deepEqual(runtime.commands, []);
});

test("post-continue vision commands rebind before returning verification evidence", async () => {
  const runtime = loadSensitiveRebindVisionRuntime();
  const result = await runtime.runVisionCommandWithSensitiveMaskRebind(
    {
      required_sensitive_mask_binding: {
        selector: "#recovery-answer",
        expected_page_context: {
          url: "https://example.test/recovery",
          mask_surface_signature: "value-free-mask-surface",
        },
      },
    },
    "vision_key",
    async () => {
      runtime.calls.push({ kind: "operation" });
      return { ok: true, action: "key", surface_changed: false };
    },
  );

  assert.equal(result.ok, true);
  assert.deepEqual(
    runtime.calls.map((call) => call.kind),
    ["ensure", "operation", "post"],
  );
});

test("post-continue vision commands execute nothing when mask rebind fails", async () => {
  const runtime = loadSensitiveRebindVisionRuntime({ ensureOk: false });
  let operationCalls = 0;
  const result = await runtime.runVisionCommandWithSensitiveMaskRebind(
    {
      required_sensitive_mask_binding: {
        selector: "#recovery-answer",
        expected_page_context: {
          url: "https://example.test/recovery",
          mask_surface_signature: "value-free-mask-surface",
        },
      },
    },
    "vision_click",
    async () => {
      operationCalls += 1;
      return { ok: true };
    },
  );

  assert.equal(result.ok, false);
  assert.equal(result.browser_mutation_attempted, false);
  assert.equal(operationCalls, 0);
  assert.deepEqual(runtime.calls.map((call) => call.kind), ["ensure"]);
});

test("post-continue vision command discards evidence when the bound surface drifts", async () => {
  const runtime = loadSensitiveRebindVisionRuntime({ postContextOk: false });
  const rawSecret = "must-not-return-from-action-verification";
  const result = await runtime.runVisionCommandWithSensitiveMaskRebind(
    {
      required_sensitive_mask_binding: {
        selector: "#recovery-answer",
        expected_page_context: {
          url: "https://example.test/recovery",
          mask_surface_signature: "value-free-mask-surface",
        },
      },
    },
    "vision_key",
    async () => {
      runtime.calls.push({ kind: "operation" });
      return { ok: true, action: "key", unsafe_evidence: rawSecret };
    },
  );

  assert.equal(result.ok, false);
  assert.equal(result.operation_result_discarded, true);
  assert.equal(result.operation_attempted, true);
  assert.equal(result.browser_mutation_attempted, true);
  assert.equal(result.execution_outcome_unknown, true);
  assert.equal(JSON.stringify(result).includes(rawSecret), false);
  assert.deepEqual(
    runtime.calls.map((call) => call.kind),
    ["ensure", "operation", "post", "clear"],
  );
});

test("model-classified ordinary controls are painted, masked, and redacted without serializing their values", async () => {
  const typedSecret = "ordinary-text-control-secret";
  const contentEditableSecret = "ordinary-contenteditable-secret";
  const html = `
    <input id="recovery-answer" type="text" value="${typedSecret}" data-y="60">
    <div id="recovery-note" contenteditable="true" data-y="120">${contentEditableSecret}</div>
  `;
  const { dom, bind, mask, paintBarrier } = loadSensitiveMaskWindow(html);
  const token = "page-token-fixture";

  const binding = bind(["#recovery-answer", "#recovery-note"], token);
  assert.equal(binding.ok, true);
  assert.equal(binding.selector_bound, true);
  assert.equal(binding.match_count, 2);
  assert.equal(JSON.stringify(binding).includes(typedSecret), false);
  assert.equal(JSON.stringify(binding).includes(contentEditableSecret), false);

  const result = mask({
    required: true,
    document_token: token,
    expected_url: dom.window.location.href,
    expected_mask_surface_signature: "mask-surface-17",
    selectors: ["#recovery-answer", "#recovery-note"],
    top_frame_only: true,
  });
  assert.equal(result.ok, true);
  assert.equal(result.count, 2);
  assert.equal(dom.window.document.querySelectorAll("[data-lumen-sensitive-screenshot-mask]").length, 2);
  const painted = await paintBarrier();
  assert.equal(painted.ok, true);
  assert.equal(painted.mask_count, 2);
  const serializedMaskResult = JSON.stringify(result);
  assert.equal(serializedMaskResult.includes(typedSecret), false);
  assert.equal(serializedMaskResult.includes(contentEditableSecret), false);

  const evidenceWindow = loadVisionEvidenceWindow(html);
  const evidence = evidenceWindow.fn(["#recovery-answer", "#recovery-note"]);
  const serializedEvidence = JSON.stringify(evidence);
  assert.equal(serializedEvidence.includes(typedSecret), false);
  assert.equal(serializedEvidence.includes(contentEditableSecret), false);
  assert.equal(evidence.controls.find((control) => control.selector === "#recovery-answer").value, "[REDACTED]");
  assert.equal(evidence.controls.find((control) => control.selector === "#recovery-note").value, "[REDACTED]");
  assert.ok(evidence.controls
    .filter((control) => ["#recovery-answer", "#recovery-note"].includes(control.selector))
    .every((control) => control.value_redaction_reason === "model_classified_sensitive_control"));
});

test("screenshot paint proof probes only frames that contain an injected mask", async () => {
  const runtime = loadSensitiveMaskPaintProofRuntime();
  const result = await runtime.prove(41, [
    { frame_id: 0, result: { ok: true, count: 1 } },
    { frame_id: 3, result: { ok: true, count: 0 } },
    { frame_id: 7, result: { ok: true, count: 2 } },
  ]);

  assert.equal(result, true);
  assert.deepEqual(runtime.calls, [{ tabId: 41, frameIds: [0, 7] }]);
});

test("screenshot paint proof succeeds without a frame barrier when no masks exist", async () => {
  const runtime = loadSensitiveMaskPaintProofRuntime();
  const result = await runtime.prove(42, [
    { frame_id: 0, result: { ok: true, count: 0 } },
    { frame_id: 5, result: { ok: true, count: 0 } },
  ]);

  assert.equal(result, true);
  assert.deepEqual(runtime.calls, []);
});

test("screenshot paint proof remains fail closed when a masked frame cannot prove paint", async () => {
  const runtime = loadSensitiveMaskPaintProofRuntime({ reject: true });
  const result = await runtime.prove(43, [
    { frame_id: 0, result: { ok: true, count: 1 } },
    { frame_id: 9, result: { ok: true, count: 0 } },
  ]);

  assert.equal(result, false);
  assert.deepEqual(runtime.calls, [{ tabId: 43, frameIds: [0] }]);
});

test("semantic screenshot masking fails closed when its page binding drifts", () => {
  const { bind, mask } = loadSensitiveMaskWindow(
    '<div id="recovery-note" contenteditable="true">secret</div>',
  );
  assert.equal(bind(["#recovery-note"], "current-page-token").ok, true);

  const result = mask({
    required: true,
    document_token: "stale-page-token",
    expected_url: "https://example.test/recovery",
    expected_mask_surface_signature: "mask-surface-17",
    selectors: ["#recovery-note"],
    top_frame_only: true,
  });
  assert.equal(result.ok, false);
  assert.equal(result.error, "sensitive_mask_page_context_drift");
  assert.equal(result.count, 0);
});

test("vision key uses trusted CDP dispatch and reports observed browser effects", async () => {
  let attachCalls = 0;
  let detachCalls = 0;
  let activeReads = 0;
  const commands = [];
  const chrome = {
    runtime: { lastError: null },
    debugger: {
      attach: (_target, _version, callback) => {
        attachCalls += 1;
        callback();
      },
      detach: (_target, callback) => {
        detachCalls += 1;
        callback();
      },
    },
    scripting: {
      executeScript: async () => {
        activeReads += 1;
        return [{ result: { active_selector: activeReads === 1 ? "#choice-owner" : "#next-field" } }];
      },
    },
  };
  const tabPageEvidence = async () => (
    activeReads === 0
      ? { url: "https://example.test/apply", title: "Apply", scroll_y: 0, surface_signature: "menu-open" }
      : { url: "https://example.test/apply", title: "Apply", scroll_y: 0, surface_signature: "menu-closed" }
  );
  const _cdp = async (_target, method, params) => {
    commands.push({ method, params });
    return {};
  };
  const withTimeout = async (promise) => promise;
  // Stand-ins for the tab debugger session: one acquire/release per primitive is observable
  // as one attach/detach on the stubbed chrome.debugger.
  const acquireDebugger = (target) => new Promise((resolve, reject) => chrome.debugger.attach(target, "1.3", () => (
    chrome.runtime.lastError ? reject(new Error(chrome.runtime.lastError.message)) : resolve({ attached_now: true })
  )));
  const releaseDebugger = (target) => { chrome.debugger.detach(target, () => {}); };
  const errorMessage = (error) => (error && error.message ? String(error.message) : String(error || ""));
  const activeElementEvidenceInPage = function activeElementEvidenceInPage() {};
  const visionKeyInPage = function visionKeyInPage() {};
  const setTimeout = (callback) => {
    callback();
    return 0;
  };
  const nativeDialogOpenFor = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const keyDispatchDescription = eval(`(${extractFunctionSource("keyDispatchDescription")})`);
  const chordModifierKeys = eval(`(${extractFunctionSource("chordModifierKeys")})`);
  const runVisionKey = eval(`(${extractFunctionSource("runVisionKey")})`);

  const result = await runVisionKey(44, "Escape");

  assert.equal(result.ok, true);
  assert.equal(result.mode, "cdp_key");
  assert.equal(result.trusted_key_ok, true);
  assert.equal(result.surface_changed, true);
  assert.equal(result.focus_changed, true);
  assert.equal(result.before_active_selector, "#choice-owner");
  assert.equal(result.after_active_selector, "#next-field");
  assert.equal(attachCalls, 1);
  assert.equal(detachCalls, 1);
  assert.deepEqual(commands.map(({ method }) => method), ["Input.dispatchKeyEvent", "Input.dispatchKeyEvent"]);
  assert.equal(commands[0].params.type, "rawKeyDown");
  assert.equal(commands[0].params.key, "Escape");
  assert.equal(commands[0].params.windowsVirtualKeyCode, 27);
  assert.equal(commands[1].params.type, "keyUp");
});

function loadVisionKeyRuntime({ failAfterDowns = 0, failFirstKeyUp = false, active = null, evidenceThrows = false } = {}) {
  const commands = [];
  const delays = [];
  const injected = [];
  let downs = 0;
  const chrome = {
    runtime: { lastError: null },
    debugger: {
      attach: (_target, _version, callback) => callback(),
      detach: (_target, callback) => callback(),
    },
    scripting: {
      executeScript: async ({ func }) => {
        injected.push(func && func.name);
        if (evidenceThrows && func && func.name === "activeElementEvidenceInPage") throw new Error("pre-key active element evidence timed out");
        return [{ result: { ok: true, active_tag: "body", active_selector: "#field", ...(active || {}) } }];
      },
    },
  };
  const tabPageEvidence = async () => ({ url: "https://example.test/apply", title: "Apply", scroll_y: 0, surface_signature: "same" });
  const _cdp = async (_target, method, params) => {
    commands.push({ method, params });
    if (failFirstKeyUp && method === "Input.dispatchKeyEvent" && params.type === "keyUp") {
      throw new Error("Input.dispatchKeyEvent timed out");
    }
    if (method === "Input.dispatchKeyEvent" && params.type !== "keyUp") {
      downs += 1;
      if (failAfterDowns && downs > failAfterDowns) throw new Error("debugger detached");
    }
    return {};
  };
  const withTimeout = async (promise) => promise;
  const acquireDebugger = async () => ({ attached_now: true });
  const releaseDebugger = () => {};
  const errorMessage = (error) => (error && error.message ? String(error.message) : String(error || ""));
  const activeElementEvidenceInPage = function activeElementEvidenceInPage() {};
  const visionKeyInPage = function visionKeyInPage() {};
  const armClipboardEvidenceInPage = function armClipboardEvidenceInPage() {};
  const readClipboardEvidenceInPage = function readClipboardEvidenceInPage() {};
  const setTimeout = (callback, ms) => {
    delays.push(ms);
    callback();
    return 0;
  };
  const nativeDialogOpenFor = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const keyDispatchDescription = eval(`(${extractFunctionSource("keyDispatchDescription")})`);
  const chordModifierKeys = eval(`(${extractFunctionSource("chordModifierKeys")})`);
  const runVisionKey = eval(`(${extractFunctionSource("runVisionKey")})`);
  return { commands, delays, injected, runVisionKey };
}

test("a key whose first press may have landed is never pressed again by the fallback", async () => {
  // The keyDown reached the page but its keyUp ack failed; pressesSent was still 0, so the
  // synthetic fallback pressed Enter a second time.
  const runtime = loadVisionKeyRuntime({ failFirstKeyUp: true });
  const result = await runtime.runVisionKey(44, "Enter");

  assert.equal(result.ok, false);
  assert.equal(result.execution_outcome_unknown, true);
  assert.equal(result.failure_reason, "key_execution_outcome_unknown");
  assert.ok(!runtime.injected.includes("visionKeyInPage"), "no synthetic fallback press");
});

test("vision key repeats a press the model asked for, one keystroke at a time", async () => {
  const runtime = loadVisionKeyRuntime();
  const result = await runtime.runVisionKey(44, "Backspace", { repeat: 3 });
  const types = runtime.commands.filter(({ method }) => method === "Input.dispatchKeyEvent").map(({ params }) => params.type);

  assert.equal(result.ok, true);
  assert.equal(result.presses, 3);
  assert.deepEqual(types, ["rawKeyDown", "keyUp", "rawKeyDown", "keyUp", "rawKeyDown", "keyUp"]);
});

test("vision key holds one press down for hold_ms without auto-repeating it", async () => {
  const runtime = loadVisionKeyRuntime();
  const result = await runtime.runVisionKey(44, "ArrowRight", { hold_ms: 1500 });
  const types = runtime.commands.filter(({ method }) => method === "Input.dispatchKeyEvent").map(({ params }) => params.type);

  assert.equal(result.ok, true);
  assert.equal(result.presses, 1);
  assert.equal(result.hold_ms, 1500);
  assert.deepEqual(types, ["rawKeyDown", "keyUp"], "a held key is one keystroke, never a burst");
  assert.ok(runtime.delays.includes(1500));
});

test("vision key never adds a fallback press after some presses already landed", async () => {
  const runtime = loadVisionKeyRuntime({ failAfterDowns: 2 });
  const result = await runtime.runVisionKey(44, "Backspace", { repeat: 5 });

  assert.equal(result.ok, false);
  assert.equal(result.failure_reason, "key_execution_partial");
  assert.equal(result.presses, 2);
  assert.equal(result.execution_outcome_unknown, true);
});

test("the address bar opens only absolute http(s) URLs without embedded credentials", () => {
  const navigableUrl = eval(`(${extractFunctionSource("navigableUrl")})`);
  assert.equal(navigableUrl("https://example.com/jobs?q=1"), "https://example.com/jobs?q=1");
  assert.equal(navigableUrl("http://example.com"), "http://example.com/");
  assert.equal(navigableUrl("javascript:alert(1)"), "");
  assert.equal(navigableUrl("file:///C:/secrets.txt"), "");
  assert.equal(navigableUrl("chrome://settings"), "");
  assert.equal(navigableUrl("https://user:pw@example.com/"), "");
  assert.equal(navigableUrl("example.com"), "");
});

test("vision evidence keeps neighboring unlabeled textareas structurally distinct", () => {
  const { fn } = loadVisionEvidenceWindow(`
    <main data-y="20">
      <div data-y="100">How have you applied AI in your engineering work and what impact did it have?</div>
      <div data-y="135"><textarea id="ai-answer" data-y="140"></textarea></div>
      <div data-y="260">Describe a developer tool or internal platform you built.</div>
      <div data-y="295"><textarea id="tool-answer" data-y="300"></textarea></div>
    </main>
  `);

  const evidence = fn();
  const ai = evidence.controls.find((control) => control.selector === "#ai-answer");
  const tool = evidence.controls.find((control) => control.selector === "#tool-answer");

  assert.equal(ai.dom_context[0].text, "How have you applied AI in your engineering work and what impact did it have?");
  assert.equal(tool.dom_context[0].text, "Describe a developer tool or internal platform you built.");
  assert.match(ai.context_text, /^How have you applied AI/);
  assert.match(tool.context_text, /^Describe a developer tool/);
});

test("vision evidence emits document-unique selectors for repeated controls", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <main>
      <section data-y="40">
        <div><button data-y="60">Toggle flyout</button></div>
      </section>
      <section data-y="140">
        <div><button data-y="160">Toggle flyout</button></div>
      </section>
    </main>
  `);

  try {
    const evidence = fn();
    const toggles = evidence.controls.filter((control) => control.text === "Toggle flyout");

    assert.equal(toggles.length, 2);
    assert.notEqual(toggles[0].selector, toggles[1].selector);
    for (const toggle of toggles) {
      const matches = dom.window.document.querySelectorAll(toggle.selector);
      assert.equal(matches.length, 1);
      assert.equal(matches[0].textContent.trim(), "Toggle flyout");
    }
  } finally {
    dom.window.close();
  }
});

test("vision evidence keeps walking repeated deep component trees until controls are uniquely addressable", () => {
  const repeatedBranch = (y) => `
    <section data-y="${y}">
      <div><div><div><div><div><div><div><div>
        <button data-y="${y + 10}">Toggle flyout</button>
      </div></div></div></div></div></div></div></div>
    </section>
  `;
  const { dom, fn } = loadVisionEvidenceWindow(`
    <main>
      ${repeatedBranch(40)}
      ${repeatedBranch(140)}
    </main>
  `);

  try {
    const evidence = fn();
    const toggles = evidence.controls.filter((control) => control.text === "Toggle flyout");

    assert.equal(toggles.length, 2);
    assert.notEqual(toggles[0].selector, toggles[1].selector);
    for (const toggle of toggles) {
      assert.ok(toggle.selector);
      assert.ok(toggle.selector.length <= 200);
      const matches = dom.window.document.querySelectorAll(toggle.selector);
      assert.equal(matches.length, 1);
      assert.equal(matches[0].textContent.trim(), "Toggle flyout");
    }
  } finally {
    dom.window.close();
  }
});

test("vision evidence and execution traverse open shadow roots with one stable composed selector", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <script>window.__fixtureNoise = "do-not-send-this-config-to-the-model";</script>
    <candidate-field id="first-name-field" data-y="80"></candidate-field>
  `);

  try {
    const host = dom.window.document.querySelector("#first-name-field");
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <label for="first-name" data-y="90">First name</label>
      <input id="first-name" type="text" aria-label="First name" data-y="120">
    `;
    const input = root.querySelector("#first-name");
    // Chrome retargets document.elementFromPoint() to the outer custom-element host and does not
    // expose elementFromPoint() on ShadowRoot. Exact selector binding must still recover the inner
    // control without weakening the coordinate or target-identity gate.
    dom.window.document.elementFromPoint = () => host;

    const evidence = fn();
    const control = evidence.controls.find((candidate) => candidate.id === "first-name");

    assert.ok(control);
    assert.match(control.selector, /^#first-name-field >>> #first-name$/);
    assert.deepEqual(Array.from(dom.window.__lumenDeepQueryAll(control.selector)), [input]);
    assert.match(evidence.viewport_text, /First name/);
    assert.doesNotMatch(evidence.viewport_text, /do-not-send-this-config-to-the-model/);

    const center = dom.window.eval(`(${extractFunctionSource("visionControlCenterInPage")})`)(
      control.selector,
      false,
      true,
    );
    assert.equal(center.ok, true);
    assert.equal(center.target_selector, control.selector);

    const pointInfo = dom.window.eval(`(${extractFunctionSource("visionPointInfoInPage")})`)(
      center.img_x,
      center.img_y,
      false,
      false,
      center,
    );
    assert.equal(pointInfo.ok, true);
    assert.equal(pointInfo.target_selector, control.selector);
    assert.equal(pointInfo.target_tag, "input");

    const click = dom.window.eval(`(${extractFunctionSource("visionClickAtPointInPage")})`)(
      center.img_x,
      center.img_y,
      false,
      false,
      center,
    );
    assert.equal(click.ok, true);
    assert.equal(click.target_selector, control.selector);

    const focus = dom.window.eval(`(${extractFunctionSource("focusControlForTypeInPage")})`)(
      control.selector,
      {
        target_selector: control.selector,
        target_text: "First name",
        target_tag: "input",
        target_type: "text",
        target_role: "",
        dispatch_path: "dom_activation",
        page_url: dom.window.location.href,
      },
    );
    assert.equal(focus.ok, true);
    assert.equal(dom.window.__lumenDeepActiveElement(), input);

    const setValue = dom.window.eval(`(${extractFunctionSource("activeValueSetInPage")})`);
    const readValue = dom.window.eval(`(${extractFunctionSource("activeValueEvidenceInPage")})`);
    assert.equal(setValue("Jordan").ok, true);
    const readback = readValue("Jordan");
    assert.equal(readback.active_value, "Jordan");
    assert.equal(readback.active_selector, control.selector);
  } finally {
    dom.window.close();
  }
});

test("a point that hits a wrapper rebinds the observed control by one of its recorded names", () => {
  // The observed control's own text is "Search", its accessible name "Search location" (run 1792's
  // typeahead options carry several names). The point lands on the wrapper; the one visible button
  // with the same tag, type, role, and an exact recorded name is the control, and is pressed there.
  const { dom } = loadVisionEvidenceWindow(`
    <div id="wrap" data-y="100" data-height="60"><button id="go" data-y="100" data-height="28">Search location</button></div>
  `);
  try {
    const wrap = dom.window.document.querySelector("#wrap");
    const button = dom.window.document.querySelector("#go");
    dom.window.document.elementFromPoint = (x, y) => (x === 260 && y === 110 ? wrap : button);
    const preflight = dom.window.eval(`(${extractFunctionSource("visionClickAtPointInPage")})`);
    const result = preflight(260, 110, false, false, {
      target_selector: "#gone-after-rerender",
      target_text: "Search",
      target_tag: "button",
      target_type: "",
      target_role: "",
      dispatch_path: "dom_activation",
      page_url: dom.window.location.href,
      target_names: ["Search", "Search location"],
    });
    assert.equal(result.ok, true);
    assert.equal(result.target_selector, "#go");

    // A different control under the same wrapper (another name, another tag) is still refused.
    const other = dom.window.eval(`(${extractFunctionSource("visionClickAtPointInPage")})`)(260, 110, false, false, {
      target_selector: "#gone-after-rerender",
      target_text: "Remote",
      target_tag: "button",
      target_type: "",
      target_role: "",
      dispatch_path: "dom_activation",
      page_url: dom.window.location.href,
      target_names: ["Remote"],
    });
    assert.equal(other.ok, false);
    assert.equal(other.failure_reason, "click_target_drift");
  } finally {
    dom.window.close();
  }
});

test("vision evidence and execution enter same-origin frames with one composed selector in top-viewport pixels", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <p data-y="40">The supplier form below is embedded in a frame.</p>
    <iframe id="supplier-frame" data-y="200" data-width="420" data-height="130"></iframe>
  `);

  try {
    const frame = dom.window.document.querySelector("#supplier-frame");
    const frameDoc = frame.contentDocument;
    frameDoc.body.innerHTML = `
      <label data-y="10">Frame passcode <input id="fp" data-y="12"></label>
      <button id="fs" data-y="60">Submit inside frame</button>
    `;
    frame.contentWindow.Element.prototype.getBoundingClientRect = dom.window.Element.prototype.getBoundingClientRect;
    const input = frameDoc.querySelector("#fp");
    // Chrome hit-tests and focus stop at the frame element in the parent document.
    dom.window.document.elementFromPoint = () => frame;
    frameDoc.elementFromPoint = (x, y) => (y >= 12 && y < 40 ? input : frameDoc.body);

    const evidence = fn();
    const control = evidence.controls.find((candidate) => candidate.id === "fp");
    assert.ok(control);
    assert.equal(control.selector, "#supplier-frame >>> #fp");
    assert.deepEqual(Array.from(dom.window.__lumenDeepQueryAll(control.selector)), [input]);
    // Frame-local y=12 sits 200px down the top viewport.
    assert.equal(dom.window.__lumenTopRect(input).top, 212);
    assert.match(evidence.viewport_text, /Frame passcode/);
    assert.ok(evidence.controls.some((candidate) => candidate.id === "fs"));

    const center = dom.window.eval(`(${extractFunctionSource("visionControlCenterInPage")})`)(
      control.selector,
      false,
      true,
    );
    assert.equal(center.ok, true);
    assert.equal(center.target_selector, control.selector);
    assert.equal(dom.window.__lumenDeepElementFromPoint(center.x, center.y), input);

    input.focus();
    Object.defineProperty(dom.window.document, "activeElement", { get: () => frame, configurable: true });
    assert.equal(dom.window.__lumenDeepActiveElement(), input);
    const setValue = dom.window.eval(`(${extractFunctionSource("activeValueSetInPage")})`);
    const readValue = dom.window.eval(`(${extractFunctionSource("activeValueEvidenceInPage")})`);
    assert.equal(setValue("relay-qxzt").ok, true);
    assert.equal(input.value, "relay-qxzt");
    assert.equal(readValue("relay-qxzt").active_value, "relay-qxzt");
  } finally {
    dom.window.close();
  }
});

test("final authority binds to the exact approved control even when it is not keyword-shaped", () => {
  const { dom } = loadVisionEvidenceWindow(`
    <button id="dispatch" data-y="100">Dispatch shipment</button>
    <button id="other" data-y="200">Refresh</button>
  `);
  try {
    const dispatch = dom.window.document.querySelector("#dispatch");
    dom.window.document.elementFromPoint = (x, y) => (y >= 100 && y < 128 ? dispatch : dom.window.document.body);
    const pointInfo = dom.window.eval(`(${extractFunctionSource("visionPointInfoInPage")})`);
    const identity = {
      target_selector: "#dispatch",
      target_text: "Dispatch shipment",
      target_tag: "button",
      target_type: "",
      target_role: "",
      dispatch_path: "dom_activation",
      page_url: dom.window.location.href,
    };
    const bound = pointInfo(60, 110, true, false, identity);
    assert.equal(bound.ok, true);
    assert.equal(bound.target_selector, "#dispatch");
    // Final authority granted for another element never applies to this one.
    const unbound = pointInfo(60, 110, true, false, { ...identity, target_selector: "#other", target_text: "Refresh" });
    assert.equal(unbound.ok, false);
  } finally {
    dom.window.close();
  }
});

test("vision evidence does not list a control parked off the left edge as visible", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <button id="hp-trap" data-y="100">Proceed to final check</button>
    <button id="hp-real" data-y="140">Proceed to final check</button>
  `);
  try {
    const base = dom.window.Element.prototype.getBoundingClientRect;
    dom.window.Element.prototype.getBoundingClientRect = function () {
      const rect = base.call(this);
      if (this.id !== "hp-trap") return rect;
      return { ...rect, left: -9979, right: -9479, x: -9979 };
    };
    const ids = fn().controls.map((control) => control.id);
    assert.ok(ids.includes("hp-real"));
    assert.equal(ids.includes("hp-trap"), false);
  } finally {
    dom.window.close();
  }
});

test("semantic page signature ignores fresh ids and reshuffled buttons, never a changed label or value", () => {
  const evidence = (html) => {
    const { dom, fn } = loadVisionEvidenceWindow(html);
    try {
      return fn();
    } finally {
      dom.window.close();
    }
  };
  const before = evidence(`
    <input id="a1" name="count" value="7" data-y="20">
    <button id="m-x1" data-y="60">+1</button>
    <button id="m-x2" data-y="100">Finalize alignment</button>
    <button id="m-x3" data-y="140">Decommission deck</button>
  `);
  const rerendered = evidence(`
    <input id="b9" name="count" value="7" data-y="20">
    <button id="m-q7" data-y="60">Decommission deck</button>
    <button id="m-q8" data-y="100">+1</button>
    <button id="m-q9" data-y="140">Finalize alignment</button>
  `);
  assert.match(before.document_semantic_signature, /^sv2:/);
  assert.notEqual(before.document_state_signature, rerendered.document_state_signature);
  assert.equal(before.document_semantic_signature, rerendered.document_semantic_signature);
  const relabeled = evidence(`
    <input id="a1" name="count" value="7" data-y="20">
    <button id="m-x1" data-y="60">+1</button>
    <button id="m-x2" data-y="100">Finalize everything</button>
    <button id="m-x3" data-y="140">Decommission deck</button>
  `);
  assert.notEqual(before.document_semantic_signature, relabeled.document_semantic_signature);
  const revalued = evidence(`
    <input id="a1" name="count" value="8" data-y="20">
    <button id="m-x1" data-y="60">+1</button>
    <button id="m-x2" data-y="100">Finalize alignment</button>
    <button id="m-x3" data-y="140">Decommission deck</button>
  `);
  assert.notEqual(before.document_semantic_signature, revalued.document_semantic_signature);
  // Submitted values keep their order: two fields that swap values are a different page.
  const pair = evidence(`<input name="first" value="Ada" data-y="20"><input name="last" value="Lovelace" data-y="60">`);
  const swapped = evidence(`<input name="last" value="Lovelace" data-y="20"><input name="first" value="Ada" data-y="60">`);
  assert.notEqual(pair.document_semantic_signature, swapped.document_semantic_signature);
});

test("final authority follows a re-rendered target only under semantic proof and only to its one exact twin", () => {
  const { dom, pointFn } = loadInjectedClickWindow(`
    <button data-hit id="m-new1">Decommission deck</button>
    <button data-hit id="m-new2">Finalize alignment</button>`);
  stubRects(dom, {
    "m-new1": { left: 10, top: 10, width: 120, height: 30 },
    "m-new2": { left: 200, top: 10, width: 120, height: 30 },
  });
  // Approved while "Finalize alignment" sat in the first slot with an id the re-render discarded.
  const approved = {
    target_selector: "#m-old",
    target_text: "Finalize alignment",
    target_tag: "button",
    target_type: "button",
    target_role: "",
    dispatch_path: "dom_activation",
    page_url: dom.window.location.href,
  };
  const unproven = pointFn(140, 50, true, false, approved, false);
  assert.equal(unproven.ok, false);
  const followed = pointFn(140, 50, true, false, approved, true);
  assert.equal(followed.ok, true);
  assert.equal(followed.strict_twin_rebound, true);
  assert.equal(followed.target_text, "Finalize alignment");
  assert.equal(followed.target_selector, "#m-new2");
  assert.equal(followed.x, 260);
  // The live probe is that one element, so the strict identity verdict accepts its fresh selector.
  const clickTargetIdentityFromPoint = eval(`(${extractFunctionSource("clickTargetIdentityFromPoint")})`);
  const clickTargetIdentityVerdict = eval(`(${extractFunctionSource("clickTargetIdentityVerdict")})`);
  assert.equal(clickTargetIdentityVerdict(approved, followed, true).ok, true);
  assert.equal(clickTargetIdentityVerdict(approved, { ...followed, strict_twin_rebound: false }, true).ok, false);
  // Strict authority never follows an alternate name.
  const byAlias = pointFn(140, 50, true, false, { ...approved, target_text: "Finalize", target_names: ["Finalize alignment"] }, true);
  assert.equal(byAlias.ok, false);
  // A second rendered twin anywhere in the page (even off-screen) means no rebind.
  const offscreen = dom.window.document.createElement("button");
  offscreen.id = "m-new3";
  offscreen.setAttribute("data-hit", "");
  offscreen.textContent = "Finalize alignment";
  dom.window.document.body.appendChild(offscreen);
  offscreen.getBoundingClientRect = () => ({ left: 10, top: 5000, right: 130, bottom: 5030, width: 120, height: 30, x: 10, y: 5000 });
  const ambiguous = pointFn(140, 50, true, false, approved, true);
  assert.equal(ambiguous.ok, false);
});

test("an approved final click follows a re-rendered button with a fresh id when the page's semantic state is unchanged", async () => {
  const runtime = loadPrivilegedFinalClickRuntime(
    `<button id="m-old">Finalize alignment</button><button id="m-other">Decommission deck</button>`,
    {
      driftAfterFirstTrustedProbeHtml: '<button id="m-fresh">Finalize alignment</button>',
      liveEvidence: { document_state_signature: "v1:rerendered", document_semantic_signature: "sv2:same" },
    },
  );
  try {
    const identity = {
      target_selector: "#m-old",
      target_text: "Finalize alignment",
      target_tag: "button",
      target_type: "button",
      target_role: "",
      dispatch_path: "dom_activation",
      page_url: runtime.dom.window.location.href,
    };
    const context = {
      url: runtime.dom.window.location.href,
      document_state_signature: "v1:approved",
      document_semantic_signature: "sv2:same",
    };
    const result = await runtime.runVisionClickControl(7, "#m-old", 0, 0, true, false, identity, context);
    assert.equal(result.ok, true);
    assert.equal(result.strict_twin_rebound, true);
    assert.equal(runtime.clickInvocations(), 1);
    assert.deepEqual(runtime.dispatches.map((item) => item.params.type), ["mousePressed", "mouseReleased"]);
  } finally {
    runtime.dom.window.close();
  }
});

test("an approved final click refuses a re-render that changed any control's label, value, or state", async () => {
  const runtime = loadPrivilegedFinalClickRuntime(
    `<button id="m-old">Finalize alignment</button>`,
    {
      driftAfterFirstTrustedProbeHtml: '<button id="m-fresh">Finalize alignment</button>',
      liveEvidence: { document_state_signature: "v1:rerendered", document_semantic_signature: "sv2:changed" },
    },
  );
  try {
    const identity = {
      target_selector: "#m-old",
      target_text: "Finalize alignment",
      target_tag: "button",
      target_type: "button",
      target_role: "",
      dispatch_path: "dom_activation",
      page_url: runtime.dom.window.location.href,
    };
    const result = await runtime.runVisionClickControl(7, "#m-old", 0, 0, true, false, identity, {
      url: runtime.dom.window.location.href,
      document_state_signature: "v1:approved",
      document_semantic_signature: "sv2:same",
    });
    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "final_submit_page_context_drift");
    assert.equal(runtime.clickInvocations(), 0);
    assert.equal(runtime.dispatches.length, 0);
  } finally {
    runtime.dom.window.close();
  }
});

test("a final click's dispatch guard keeps a press from a control swapped in after the adjacent probe", async () => {
  const runtime = loadPrivilegedFinalClickRuntime(
    `<form><input><input><input><button type="submit" id="submit">Submit application</button></form>`,
    { driftAfterImmediateProbeHtml: '<button type="submit" id="submit">Delete application</button>' },
  );
  try {
    const result = await runtime.runVisionClickControl(7, "#submit", 0, 0, true);
    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "click_target_drift_at_dispatch");
    assert.equal(result.dispatch_guard_blocked, true);
    assert.equal(result.activation_count, 0);
    // The press reached the page, but no page handler ran for the swapped-in control.
    assert.equal(runtime.clickInvocations(), 0);
    assert.equal(runtime.dom.window.__lumenDispatchGuard, null);
  } finally {
    runtime.dom.window.close();
  }
});

test("an ordinary click whose first pointer event lands on something else is stopped and names what intercepts", async () => {
  const runtime = loadPrivilegedFinalClickRuntime(
    `<button id="go">Save draft</button>`,
    { driftAfterImmediateProbeHtml: '<button id="go" class="shifted banner">Delete draft</button>' },
  );
  try {
    const result = await runtime.runVisionClickControl(7, "#go", 0, 0, false);
    assert.equal(result.ok, false, JSON.stringify(result) + JSON.stringify(runtime.dispatches));
    assert.equal(result.failure_reason, "click_intercepted", JSON.stringify(result));
    assert.match(result.failure_detail, /^intercepted: button\.shifted\.banner \/ "Delete draft" intercepts pointer events/);
    assert.equal(result.dispatch_intercepted, true);
    assert.equal(result.activation_attempted, false);
    assert.equal(result.activation_count, 0);
    // The swapped-in control's own handler never ran, and the guard is gone.
    assert.equal(runtime.clickInvocations(), 0);
    assert.equal(runtime.dom.window.__lumenDispatchGuard, null);
  } finally {
    runtime.dom.window.close();
  }
});

test("an ordinary click that reaches its target passes the guard and disarms on the first event", async () => {
  const runtime = loadPrivilegedFinalClickRuntime(`<button id="go">Save draft</button>`);
  try {
    const result = await runtime.runVisionClickControl(7, "#go", 0, 0, false);
    assert.equal(result.ok, true);
    assert.equal(runtime.clickInvocations(), 1, JSON.stringify(result) + JSON.stringify(runtime.dispatches));
    assert.equal(runtime.dom.window.__lumenDispatchGuard, null);
  } finally {
    runtime.dom.window.close();
  }
});

test("a double-click whose second press lands on a cover is stopped and reported as an unknown outcome", async () => {
  const runtime = loadPrivilegedFinalClickRuntime(
    `<button id="go">Save draft</button><div id="cover">Promo</div>`,
    { coverPressNumber: 2 },
  );
  try {
    const result = await runtime.runVisionClickControl(7, "#go", 0, 0, false, false, null, null, { clicks: 2 });
    assert.equal(result.ok, false, JSON.stringify(result));
    assert.equal(result.failure_reason, "click_execution_outcome_unknown", JSON.stringify(result));
    assert.equal(result.dispatch_guard_blocked, true);
    assert.equal(result.dispatch_guard_partial, true);
    assert.equal(runtime.clickInvocations(), 1);
    assert.equal(runtime.dom.window.__lumenDispatchGuard, null);
  } finally {
    runtime.dom.window.close();
  }
});

test("a right-click whose first press lands on a cover is refused as click_intercepted", async () => {
  const runtime = loadPrivilegedFinalClickRuntime(
    `<button id="go">Save draft</button><div id="cover">Promo</div>`,
    { coverPressNumber: 1 },
  );
  try {
    const result = await runtime.runVisionClickControl(7, "#go", 0, 0, false, false, null, null, { button: "right" });
    assert.equal(result.ok, false, JSON.stringify(result));
    assert.equal(result.failure_reason, "click_intercepted", JSON.stringify(result));
    assert.equal(result.dispatch_intercepted, true);
    assert.equal(result.activation_count, 0);
    assert.equal(runtime.dom.window.__lumenDispatchGuard, null);
  } finally {
    runtime.dom.window.close();
  }
});

test("a refused press puts keyboard focus back where it was", async () => {
  const runtime = loadPrivilegedFinalClickRuntime(
    `<input id="name"><button id="go">Save draft</button><div id="cover" tabindex="0">Promo</div>`,
    { coverPressNumber: 1 },
  );
  try {
    const doc = runtime.dom.window.document;
    doc.getElementById("name").focus();
    // The page's own default action would have moved focus to the cover.
    doc.getElementById("cover").addEventListener("mousedown", () => doc.getElementById("cover").focus());
    const result = await runtime.runVisionClickControl(7, "#go", 0, 0, false);
    assert.equal(result.failure_reason, "click_intercepted", JSON.stringify(result));
    assert.equal(doc.activeElement && doc.activeElement.id, "name");
  } finally {
    runtime.dom.window.close();
  }
});

test("a disabled target is refused before any press, with its own code", async () => {
  const runtime = loadPrivilegedFinalClickRuntime(`<button id="go" disabled>Save draft</button>`);
  try {
    const result = await runtime.runVisionClickControl(7, "#go", 0, 0, false);
    assert.equal(result.ok, false, JSON.stringify(result));
    assert.equal(result.failure_reason, "click_disabled", JSON.stringify(result));
    assert.equal(result.actionability_code, "disabled");
    assert.equal(result.activation_attempted, false);
    assert.equal(runtime.dispatches.length, 0);
  } finally {
    runtime.dom.window.close();
  }
});

function loadActionabilityWindow(html, { rects = {}, hitSequence = null } = {}) {
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    url: "https://example.test/task",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  Object.defineProperty(dom.window, "devicePixelRatio", { value: 2, configurable: true });
  Object.defineProperty(dom.window, "innerWidth", { value: 1000, configurable: true });
  Object.defineProperty(dom.window, "innerHeight", { value: 800, configurable: true });
  const scrolls = [];
  dom.window.Element.prototype.scrollIntoView = function (options) { scrolls.push(options && options.block); };
  dom.window.Element.prototype.getBoundingClientRect = function () {
    const r = rects[this.id] || { left: 10, top: 10, width: 100, height: 40 };
    return { ...r, x: r.left, y: r.top, right: r.left + r.width, bottom: r.top + r.height };
  };
  const state = { hits: 0 };
  dom.window.document.elementFromPoint = (x, y) => {
    state.hits += 1;
    return hitSequence ? hitSequence(state.hits, dom.window.document) : dom.window.document.getElementById("go");
  };
  const fn = dom.window.eval(`(${extractFunctionSource("clickActionabilityInPage")})`);
  return { dom, fn, scrolls };
}

test("actionability: a target that holds still and is reachable passes without scrolling", async () => {
  const { dom, fn, scrolls } = loadActionabilityWindow(`<button id="go">Go</button>`);
  try {
    const result = await fn("#go", 50, 30);
    assert.equal(result.ok, true);
    assert.equal(result.checked, true);
    assert.equal(result.moved, false);
    assert.equal(result.actionability_stable, true);
    assert.deepEqual(scrolls, []);
  } finally {
    dom.window.close();
  }
});

test("actionability: a covered target is scrolled center, end, start with backoff and then refused naming the cover", async () => {
  const { dom, fn, scrolls } = loadActionabilityWindow(
    `<button id="go">Go</button><div id="veil" class="backdrop open" role="dialog" aria-label="Subscribe"></div>`,
    { hitSequence: (_n, doc) => doc.getElementById("veil") },
  );
  try {
    const started = Date.now();
    const result = await fn("#go", 50, 30);
    assert.equal(result.ok, false);
    assert.equal(result.code, "covered");
    assert.equal(result.failure_reason, "click_covered");
    assert.match(result.covering_element, /^div\.backdrop\.open \/ dialog "Subscribe"$/);
    assert.deepEqual(scrolls, ["center", "end", "start", "center", "end"]);
    assert.ok(Date.now() - started >= 700, "the bounded backoff ran (0/20/100/100/500 ms)");
  } finally {
    dom.window.close();
  }
});

test("actionability: scrolling that uncovers the target moves the press point to its centre", async () => {
  const { dom, fn, scrolls } = loadActionabilityWindow(
    `<button id="go">Go</button><div id="veil"></div>`,
    {
      rects: { go: { left: 200, top: 300, width: 100, height: 40 } },
      hitSequence: (_n, doc) => (_n <= 1 ? doc.getElementById("veil") : doc.getElementById("go")),
    },
  );
  try {
    const result = await fn("#go", 50, 30);
    assert.equal(result.ok, true);
    assert.equal(result.moved, true);
    assert.equal(result.actionability_scrolled, 1);
    assert.deepEqual(scrolls, ["center"]);
    assert.deepEqual([result.x, result.y], [250, 320]);
    assert.deepEqual([result.img_x, result.img_y], [500, 640]);
  } finally {
    dom.window.close();
  }
});

test("actionability: disabled, pointer-events none (ancestor), not visible and detached each get their own code", async () => {
  const cases = [
    { html: `<button id="go" disabled>Go</button>`, code: "disabled" },
    { html: `<div style="pointer-events:none" class="lock"><button id="go">Go</button></div>`, code: "pointer_events_none", detail: /^div\.lock .*sets pointer-events: none/ },
    { html: `<button id="go" style="visibility:hidden">Go</button>`, code: "not_visible" },
  ];
  for (const item of cases) {
    const { dom, fn } = loadActionabilityWindow(item.html);
    try {
      const result = await fn("#go", 50, 30);
      assert.equal(result.ok, false, item.code);
      assert.equal(result.code, item.code);
      if (item.detail) assert.match(result.detail, item.detail);
    } finally {
      dom.window.close();
    }
  }
  const zero = loadActionabilityWindow(`<button id="go">Go</button>`, { rects: { go: { left: 0, top: 0, width: 0, height: 0 } } });
  try {
    assert.equal((await zero.fn("#go", 50, 30)).code, "not_visible");
  } finally {
    zero.dom.window.close();
  }
  const gone = loadActionabilityWindow(`<button id="go">Go</button>`);
  try {
    const pending = gone.fn("#go", 50, 30);
    gone.dom.window.document.getElementById("go").remove();
    assert.equal((await pending).code, "detached");
  } finally {
    gone.dom.window.close();
  }
});

test("actionability: a readonly target is reported as a fact, not refused", async () => {
  const { dom, fn } = loadActionabilityWindow(`<input id="go" readonly>`);
  try {
    const result = await fn("#go", 50, 30);
    assert.equal(result.ok, true);
    assert.equal(result.target_readonly, true);
  } finally {
    dom.window.close();
  }
});

test("a click's effect evidence reads a value changed beside the clicked control (a stepper's quantity)", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <div class="stepper"><button data-hit id="dec">-</button><span id="qty">1</span><button data-hit id="inc">+</button></div>
  `);
  try {
    stubRects(dom, { dec: { left: 10, top: 10, width: 30, height: 30 }, inc: { left: 100, top: 10, width: 30, height: 30 } });
    const doc = dom.window.document;
    doc.querySelector("#inc").addEventListener("click", () => { doc.querySelector("#qty").textContent = "2"; });
    const probe = fn(230, 50);
    assert.equal(probe.ok, true);
    assert.deepEqual(Array.from(probe.linked_before), ["1"]);
    const outcomeFn = dom.window.eval(`(${extractFunctionSource("clickOutcomeEvidenceInPage")})`);
    const outcome = outcomeFn(probe);
    assert.equal(outcome.linked_value_changed, true);
    assert.equal(outcome.linked_value_before, "1");
    assert.equal(outcome.linked_value_after, "2");
    assert.equal(outcome.linked_value_basis, "sibling");
    // Nothing changed since the snapshot: no claim.
    const again = fn(230, 50);
    assert.equal(outcomeFn(again).linked_value_changed, false);
  } finally {
    dom.window.close();
  }
});

test("linked value: a sibling that changes without the click (a live counter) is not counted", () => {
  const { dom, fn, pointFn } = loadInjectedClickWindow(`
    <div class="row"><button data-hit id="inc">+</button><span id="live">7</span><span id="qty">1</span></div>
  `);
  try {
    stubRects(dom, { inc: { left: 100, top: 10, width: 30, height: 30 } });
    const doc = dom.window.document;
    const probe = fn(230, 50);
    assert.equal(probe.ok, true);
    // The page ticks the counter between the preflight and the immediate probe.
    doc.querySelector("#live").textContent = "8";
    pointFn(230, 50, false, false, null, false, true);
    // The click itself changes nothing; the counter ticks again afterwards.
    doc.querySelector("#live").textContent = "9";
    const outcomeFn = dom.window.eval(`(${extractFunctionSource("clickOutcomeEvidenceInPage")})`);
    assert.equal(outcomeFn(probe).linked_value_changed, false);
    // A real change on a non-spontaneous sibling still counts.
    doc.querySelector("#qty").textContent = "2";
    const real = outcomeFn(probe);
    assert.equal(real.linked_value_changed, true);
    assert.equal(real.linked_value_after, "2");
  } finally {
    dom.window.close();
  }
});

test("linked value: siblings shifting position are compared by identity, and a clock-like sibling is skipped", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <div class="row"><span id="clock">12:30:01</span><button data-hit id="inc">+</button><span id="a">1</span><span id="b">2</span></div>
  `);
  try {
    stubRects(dom, { inc: { left: 100, top: 10, width: 30, height: 30 } });
    const doc = dom.window.document;
    const probe = fn(230, 50);
    assert.deepEqual(Array.from(probe.linked_before), ["1", "2"]);
    // A sibling is removed ahead of the others: index 0 now holds b's text, but nothing changed.
    doc.querySelector("#a").remove();
    doc.querySelector("#clock").textContent = "12:30:02";
    const outcomeFn = dom.window.eval(`(${extractFunctionSource("clickOutcomeEvidenceInPage")})`);
    assert.equal(outcomeFn(probe).linked_value_changed, false);
  } finally {
    dom.window.close();
  }
});

test("linked value: a value named by aria-controls outranks a positional sibling", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <div class="row"><button data-hit id="inc" aria-controls="total">+</button><span id="qty">1</span></div><output id="total">10</output>
  `);
  try {
    stubRects(dom, { inc: { left: 100, top: 10, width: 30, height: 30 } });
    const doc = dom.window.document;
    const probe = fn(230, 50);
    doc.querySelector("#qty").textContent = "2";
    doc.querySelector("#total").textContent = "11";
    const outcome = dom.window.eval(`(${extractFunctionSource("clickOutcomeEvidenceInPage")})`)(probe);
    assert.equal(outcome.linked_value_basis, "linked");
    assert.equal(outcome.linked_value_after, "11");
  } finally {
    dom.window.close();
  }
});

test("vision evidence splits occluded into covered, behind_modal, clipped and pointer_events_none facts", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <button id="under-banner" data-y="40">Pay</button>
    <div id="banner" class="sticky bar" data-y="30"></div>
    <button id="under-modal" data-y="200">Other</button>
    <div id="modal" role="dialog" aria-modal="true" data-y="190"><button id="in-modal" data-y="200">Close</button></div>
    <div id="pane" data-y="400"><button id="cut" data-y="410">Hidden row</button></div>
    <button id="locked" style="pointer-events:none" data-y="500">Locked</button>
    <button id="free" data-y="600">Free</button>
  `);
  try {
    const doc = dom.window.document;
    const byId = (id) => doc.getElementById(id);
    doc.elementFromPoint = (x, y) => {
      if (y >= 40 && y < 68) return byId("banner");
      if (y >= 200 && y < 228) return byId("in-modal");
      if (y >= 410 && y < 438) return byId("pane");
      if (y >= 500 && y < 528) return doc.body;
      return null;
    };
    const controls = fn().controls;
    const row = (id) => controls.find((control) => control.id === id);
    assert.equal(row("under-banner").covered, true);
    assert.equal(row("under-banner").covered_by, "div.sticky.bar");
    assert.equal(row("under-banner").occluded, true);
    assert.equal(row("under-modal").behind_modal, true);
    assert.equal(row("under-modal").covered, false);
    assert.equal(row("cut").clipped, true);
    assert.equal(row("cut").covered, false);
    assert.equal(row("locked").pointer_events_none, true);
    assert.equal(row("locked").covered, false);
    assert.equal(row("free").occluded, false);
  } finally {
    dom.window.close();
  }
});

test("target text matching ignores decorative glyphs around the words but not the words", () => {
  const dom = new JSDOM(`<!doctype html><html><body><button id="a">Set status \u25be</button><button id="b">Set statuses</button><button id="c">\u00d7</button></body></html>`, {
    url: "https://example.test/", runScripts: "outside-only", pretendToBeVisual: true,
  });
  Object.defineProperty(dom.window, "innerWidth", { value: 1000, configurable: true });
  Object.defineProperty(dom.window, "innerHeight", { value: 800, configurable: true });
  dom.window.Element.prototype.getBoundingClientRect = function () {
    return { left: 10, top: 10, width: 100, height: 30, x: 10, y: 10, right: 110, bottom: 40 };
  };
  for (const id of ["a", "b", "c"]) {
    const node = dom.window.document.getElementById(id);
    Object.defineProperty(node, "innerText", { value: node.textContent, configurable: true });
  }
  try {
    const find = dom.window.eval(`(${extractFunctionSource("findLabeledClickableInPage")})`);
    // "Set status" names the first only once the caret is ignored; the second is a different word.
    dom.window.document.getElementById("c").remove();
    dom.window.document.getElementById("b").remove();
    assert.equal(find("Set status").ok, true);
    assert.equal(find("\u203a Set status \u2713").ok, true);
    assert.equal(find("Set").ok, false);
    assert.equal(find("Set statuses").ok, false);
  } finally {
    dom.window.close();
  }
});

test("semantic page signature catches a state that moves between controls, and withholds itself when it cannot vouch", () => {
  const evidence = (html) => {
    const { dom, fn } = loadVisionEvidenceWindow(html);
    try {
      return fn();
    } finally {
      dom.window.close();
    }
  };
  // Plan cards: "Selected" moves from Basic to Pro with nothing re-rendered or reordered.
  const basicChosen = evidence(`
    <div data-y="20"><h3 data-y="20">Basic $10/mo</h3><button id="b" data-y="50">Selected</button></div>
    <div data-y="100"><h3 data-y="100">Pro $20/mo</h3><button id="p" data-y="130">Select</button></div>
    <button id="go" data-y="200">Subscribe</button>
  `);
  const proChosen = evidence(`
    <div data-y="20"><h3 data-y="20">Basic $10/mo</h3><button id="b" data-y="50">Select</button></div>
    <div data-y="100"><h3 data-y="100">Pro $20/mo</h3><button id="p" data-y="130">Selected</button></div>
    <button id="go" data-y="200">Subscribe</button>
  `);
  assert.match(basicChosen.document_semantic_signature, /^sv2:/);
  assert.notEqual(basicChosen.document_semantic_signature, proChosen.document_semantic_signature);
  // The same cards shuffled with fresh ids keep it.
  const shuffled = evidence(`
    <div data-y="20"><h3 data-y="20">Pro $20/mo</h3><button id="p2" data-y="50">Select</button></div>
    <button id="go2" data-y="90">Subscribe</button>
    <div data-y="120"><h3 data-y="120">Basic $10/mo</h3><button id="b2" data-y="150">Selected</button></div>
  `);
  assert.equal(basicChosen.document_semantic_signature, shuffled.document_semantic_signature);
  // A detail outside every control (the record an Approve button acts on) is part of it.
  const recordA = evidence(`<p data-y="20">Invoice 1041 from Acme, $500</p><button data-y="60">Approve</button>`);
  const recordB = evidence(`<p data-y="20">Invoice 1042 from Zenith, $9000</p><button data-y="60">Approve</button>`);
  assert.notEqual(recordA.document_semantic_signature, recordB.document_semantic_signature);
  // Two action controls nothing tells apart: a state moving between them would hide, so no proof.
  const twins = evidence(`<div data-y="20"><button data-y="20" aria-pressed="true">Like</button><button data-y="60" aria-pressed="false">Like</button></div>`);
  assert.equal(twins.document_semantic_signature, "");
  // A value it can only see redacted cannot be compared: no proof.
  const redacted = evidence(`<input autocomplete="cc-number" value="4111111111111111" data-y="20"><button data-y="60">Pay</button>`);
  assert.equal(redacted.document_semantic_signature, "");
  assert.notEqual(redacted.document_state_signature, "");
  // Cards flattened into one container: each button's own preceding text still pins its state.
  const flatBasic = evidence(`<div data-y="20"><h3 data-y="20">Basic $10</h3><button data-y="40">Selected</button><h3 data-y="60">Pro $20</h3><button data-y="80">Select</button></div><button data-y="120">Subscribe</button>`);
  const flatPro = evidence(`<div data-y="20"><h3 data-y="20">Basic $10</h3><button data-y="40">Select</button><h3 data-y="60">Pro $20</h3><button data-y="80">Selected</button></div><button data-y="120">Subscribe</button>`);
  assert.notEqual(flatBasic.document_semantic_signature, flatPro.document_semantic_signature);
  // Text it cannot walk (a frame) means no proof.
  const framed = evidence(`<iframe data-y="20" src="about:blank"></iframe><button data-y="160">Approve</button>`);
  assert.equal(framed.document_semantic_signature, "");
});

test("semantic page signature withholds itself when record text lives in an open shadow root", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`<div id="host" data-y="20"></div><button data-y="120">Approve</button>`);
  try {
    const root = dom.window.document.querySelector("#host").attachShadow({ mode: "open" });
    root.innerHTML = `<p data-y="20">Invoice 1041 from Acme, $500</p>`;
    const result = fn();
    assert.equal(result.document_semantic_signature, "");
    assert.notEqual(result.document_state_signature, "");
  } finally {
    dom.window.close();
  }
});

test("a strict twin is never chosen while the approved node is still in the document", () => {
  const { dom, pointFn } = loadInjectedClickWindow(`
    <button data-hit id="slideA-buy" style="display:none">Buy now</button>
    <button data-hit id="slideB-buy">Buy now</button>`);
  stubRects(dom, {
    "slideA-buy": { left: 0, top: 0, width: 0, height: 0 },
    "slideB-buy": { left: 200, top: 10, width: 120, height: 30 },
  });
  const approved = {
    target_selector: "#slideA-buy",
    target_text: "Buy now",
    target_tag: "button",
    target_type: "button",
    target_role: "",
    dispatch_path: "dom_activation",
    page_url: dom.window.location.href,
  };
  const probe = pointFn(520, 50, true, false, approved, true);
  assert.equal(probe.ok, false);
  assert.notEqual(probe.strict_twin_rebound, true);
  const findTwin = dom.window.eval(`(${extractFunctionSource("findIdentityTwinInPage")})`);
  assert.equal(findTwin(approved, true).reason, "approved_target_still_present");
  dom.window.document.querySelector("#slideA-buy").remove();
  assert.equal(findTwin(approved, true).ok, true);
});

test("an approved final click whose selector vanished in a re-render binds its one exact twin", async () => {
  const runtime = loadPrivilegedFinalClickRuntime(
    `<button id="m-fresh">Finalize alignment</button>`,
    { liveEvidence: { document_state_signature: "v1:rerendered", document_semantic_signature: "sv2:same" } },
  );
  try {
    const identity = {
      target_selector: "#m-old",
      target_text: "Finalize alignment",
      target_tag: "button",
      target_type: "button",
      target_role: "",
      dispatch_path: "dom_activation",
      page_url: runtime.dom.window.location.href,
    };
    const context = {
      url: runtime.dom.window.location.href,
      document_state_signature: "v1:approved",
      document_semantic_signature: "sv2:same",
    };
    const result = await runtime.runVisionClickControl(7, "#m-old", 0, 0, true, false, identity, context);
    assert.equal(result.ok, true);
    assert.equal(runtime.clickInvocations(), 1);
    assert.deepEqual(runtime.dispatches.map((item) => item.params.type), ["mousePressed", "mouseReleased"]);
    // Without the semantic proof the vanished selector fails closed.
    const unproven = await runtime.runVisionClickControl(7, "#m-old", 0, 0, true, false, identity, {
      ...context,
      document_semantic_signature: "sv2:other",
    });
    assert.equal(unproven.ok, false);
    assert.equal(runtime.clickInvocations(), 1);
  } finally {
    runtime.dom.window.close();
  }
});

test("an ordinary click follows a roving control to its one exact identity twin", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <button id="bay-1" data-y="100">Empty bay</button>
    <button id="bay-2" data-y="140">Empty bay</button>
    <button id="bay-3" data-y="180">Capture token</button>
  `);
  try {
    fn(); // installs the composed-DOM helpers the twin lookup uses
    const findTwin = dom.window.eval(`(${extractFunctionSource("findIdentityTwinInPage")})`);
    const observed = { target_selector: "#bay-2", target_text: "Capture token", target_tag: "button", target_type: "button", target_role: "" };
    const twin = findTwin(observed);
    assert.equal(twin.ok, true);
    assert.equal(twin.selector, "#bay-3");
    dom.window.document.querySelector("#bay-1").textContent = "Capture token";
    assert.equal(findTwin(observed).reason, "identity_twin_ambiguous");
    assert.equal(findTwin({ ...observed, target_text: "Release token" }).reason, "identity_twin_absent");
  } finally {
    dom.window.close();
  }
});

test("vision evidence lists an icon-only pointer span by its id words and splits side-by-side pointer children", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <div id="bar" data-y="40"><span id="send-reply" data-y="40" data-width="14" data-height="14" style="cursor:pointer"></span></div>
    <div id="actions" data-y="200" style="cursor:pointer"><span class="reply" data-y="200" data-left="20" data-width="60">Reply</span><span class="forward" data-y="200" data-left="120" data-width="60">Forward</span></div>
  `);
  const baseRect = dom.window.Element.prototype.getBoundingClientRect;
  dom.window.Element.prototype.getBoundingClientRect = function () {
    const rect = baseRect.call(this);
    const left = this.getAttribute("data-left");
    return left === null ? rect : { ...rect, left: Number(left), x: Number(left), right: Number(left) + rect.width };
  };
  try {
    const controls = fn().controls;
    const send = controls.find((control) => control.id === "send-reply");
    assert.ok(send, "an icon-only clickable span with an id is enumerated");
    assert.equal(send.derived_label, "send reply");
    assert.ok(controls.find((control) => control.text === "Reply"), "Reply is its own row");
    assert.ok(controls.find((control) => control.text === "Forward"), "Forward is its own row");
  } finally {
    dom.window.close();
  }
});

test("vision evidence lists pointer-cursor custom options and marks the control painted over", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <select id="province" data-y="120"><option>Select a country first</option></select>
    <div id="menu" data-y="110"><div id="opt-canada" data-y="118" style="cursor:pointer">Canada</div></div>
    <div id="decor" data-y="300" style="cursor:pointer"><button id="inner" data-y="300">Go</button></div>
  `);
  try {
    const option = dom.window.document.querySelector("#opt-canada");
    // The open menu is painted over the select's center.
    dom.window.document.elementFromPoint = (x, y) => (y >= 110 && y < 150 ? option : dom.window.document.body);
    const controls = fn().controls;
    const canada = controls.find((control) => control.id === "opt-canada");
    const province = controls.find((control) => control.id === "province");
    assert.ok(canada, "pointer-cursor option is enumerated");
    assert.equal(canada.text, "Canada");
    assert.equal(province.occluded, true);
    assert.equal(canada.occluded, false);
    // A pointer container around an already-listed control is not listed twice.
    assert.equal(controls.some((control) => control.id === "decor"), false);
  } finally {
    dom.window.close();
  }
});

test("an option clipped out of view inside a scrolled list is marked occluded", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <div id="list" data-y="100">
      <div id="opt-shown" data-y="110" style="cursor:pointer">Scandium</div>
      <div id="opt-clipped" data-y="260" style="cursor:pointer">Cobalt</div>
    </div>
  `);
  try {
    const list = dom.window.document.querySelector("#list");
    const shown = dom.window.document.querySelector("#opt-shown");
    // The list shows 100-250px; below that the point hits the list container around the option.
    dom.window.document.elementFromPoint = (x, y) => (y < 250 && y >= 110 && y < 140 ? shown : list);
    const controls = fn().controls;
    assert.equal(controls.find((control) => control.id === "opt-shown").occluded, false);
    assert.equal(controls.find((control) => control.id === "opt-clipped").occluded, true);
  } finally {
    dom.window.close();
  }
});

test("vision evidence reads slotted accessibility labels across open shadow components", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <privacy-field id="privacy-field" data-y="80">
      <span slot="policy-label">
        Please be informed that submitting this application will process your personal data.
      </span>
    </privacy-field>
  `);

  try {
    const host = dom.window.document.querySelector("#privacy-field");
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <label id="policy-label" for="policy-checkbox" data-y="90">
        <slot name="policy-label"></slot><span>*</span>
      </label>
      <input id="policy-checkbox" type="checkbox" aria-labelledby="policy-label" data-y="120">
    `;

    const evidence = fn();
    const control = evidence.controls.find(
      (candidate) => candidate.id === "policy-checkbox",
    );
    const readableLabel = (
      "Please be informed that submitting this application will process your personal data."
    );

    assert.ok(control);
    assert.match(control.selector, /^#privacy-field >>> #policy-checkbox$/);
    assert.match(control.nearby_text, new RegExp(readableLabel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    assert.ok(
      control.accessibility_context.some(
        (item) => (
          ["label", "aria-labelledby"].includes(item.relation)
          && item.text.includes(readableLabel)
        ),
      ),
    );
  } finally {
    dom.window.close();
  }
});

test("upload resolver binds a visible shadow control to its shadow-root file input", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <candidate-upload id="resume-upload" data-y="80"></candidate-upload>
  `);

  try {
    const host = dom.window.document.querySelector("#resume-upload");
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <div data-y="90">
        <label for="resume-file" data-y="95">Resume</label>
        <input id="resume-file" type="file" data-y="105">
        <button id="choose-resume" type="button" data-y="140">Choose resume</button>
      </div>
    `;
    fn();
    const button = root.querySelector("#choose-resume");
    const input = root.querySelector("#resume-file");
    const buttonSelector = dom.window.__lumenStableDeepSelector(button);
    const resolver = dom.window.eval(`(${extractFunctionSource("resolveUploadTargetInPage")})`);
    const result = resolver({
      mode: "selector",
      selector: buttonSelector,
      expectedTarget: {
        target_selector: buttonSelector,
        target_text: "Choose resume",
        target_tag: "button",
        target_type: "button",
        target_role: "",
        dispatch_path: "dom_activation",
        page_url: dom.window.location.href,
      },
    });

    assert.equal(result.ok, true);
    assert.equal(result.target_label, "Resume");
    assert.deepEqual(Array.from(dom.window.__lumenDeepQueryAll(result.selector)), [input]);
    assert.match(result.selector, /^#resume-upload >>> #resume-file$/);
    // The same button observed with different spacing in its text still resolves (run 1795's labels).
    const spaced = resolver({
      mode: "selector",
      selector: buttonSelector,
      expectedTarget: {
        target_selector: buttonSelector,
        target_text: "Choose  resume *",
        target_names: [],
        target_tag: "button",
        target_type: "button",
        target_role: "",
        dispatch_path: "dom_activation",
        page_url: dom.window.location.href,
      },
    });
    assert.equal(spaced.ok, true, "same element, same leading label with a live tail");
    const respaced = resolver({
      mode: "selector",
      selector: buttonSelector,
      expectedTarget: {
        target_selector: buttonSelector,
        target_text: "Choose a resume",
        target_tag: "button",
        target_type: "button",
        target_role: "",
        dispatch_path: "dom_activation",
        page_url: dom.window.location.href,
      },
    });
    assert.equal(respaced.ok, false, "a different label on the same selector is still refused");
  } finally {
    dom.window.close();
  }
});

test("vision evidence exposes raw combobox and popup-option relationships", () => {
  const { fn } = loadVisionEvidenceWindow(`
    <label id="field-24-label" for="field-24" data-y="80">Pronouns</label>
    <div id="field-24" role="combobox" aria-controls="field-24-list" aria-labelledby="field-24-label" data-y="110">Search</div>
    <ul id="field-24-list" role="listbox" aria-labelledby="field-24-label" data-y="150">
      <li id="field-24-list-option-0" role="option" data-y="160">She/her/hers</li>
      <li id="field-24-list-option-1" role="option" data-y="200">He/him/his</li>
    </ul>
  `);

  const evidence = fn();
  const field = evidence.controls.find((control) => control.id === "field-24");
  const option = evidence.controls.find((control) => control.id === "field-24-list-option-1");

  assert.equal(field.aria_controls, "field-24-list");
  assert.equal(field.aria_labelledby, "field-24-label");
  assert.equal(option.text, "He/him/his");
  assert.deepEqual(
    { id: option.ancestor_chain[0].id, role: option.ancestor_chain[0].role, aria_labelledby: option.ancestor_chain[0].aria_labelledby },
    { id: "field-24-list", role: "listbox", aria_labelledby: "field-24-label" }
  );
});

test("vision evidence preserves raw rendered state for custom choice controls", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <button
      id="yes"
      class="choice selected"
      aria-pressed="true"
      data-state="on"
      data-y="80"
      style="background-color: rgb(20, 20, 20); color: rgb(255, 255, 255); border-color: rgb(20, 20, 20)"
    >Yes</button>
    <button
      id="no"
      class="choice"
      aria-pressed="false"
      data-state="off"
      data-y="120"
      style="background-color: rgb(255, 255, 255); color: rgb(20, 20, 20); border-color: rgb(180, 180, 180)"
    >No</button>
  `);
  try {
    const evidence = fn();
    const yes = evidence.controls.find((control) => control.id === "yes");
    const no = evidence.controls.find((control) => control.id === "no");

    assert.equal(yes.class_name, "choice selected");
    assert.equal(yes.aria_pressed, "true");
    assert.equal(yes.data_state, "on");
    assert.equal(yes.visual_state.background_color, "rgb(20, 20, 20)");
    assert.equal(no.class_name, "choice");
    assert.equal(no.aria_pressed, "false");
    assert.equal(no.data_state, "off");
    assert.equal(no.visual_state.background_color, "rgb(255, 255, 255)");
  } finally {
    dom.window.close();
  }
});

test("vision page evidence structurally redacts sensitive values and preserves ordinary text", () => {
  const { fn } = loadVisionEvidenceWindow(`
    <label for="password">Password</label>
    <input id="password" type="password" value="password-secret" data-y="40">
    <label for="otp">Verification code</label>
    <input id="otp" type="text" autocomplete="one-time-code" value="123456" data-y="90">
    <label for="card">Card number</label>
    <input id="card" type="text" autocomplete="section-checkout billing cc-number" value="4111111111111111" data-y="140">
    <div id="secret-editor" contenteditable="true" autocomplete="new-password" aria-label="Passphrase editor" data-y="170">contenteditable-secret</div>
    <textarea id="neighbor" aria-label="Neighboring notes" data-y="185"></textarea>
    <label for="display-name">Display name</label>
    <input id="display-name" type="text" autocomplete="name" value="Visible ordinary value" data-y="230">
  `);

  const evidence = fn();
  const password = evidence.controls.find((control) => control.id === "password");
  const otp = evidence.controls.find((control) => control.id === "otp");
  const card = evidence.controls.find((control) => control.id === "card");
  const secretEditor = evidence.controls.find((control) => control.id === "secret-editor");
  const ordinary = evidence.controls.find((control) => control.id === "display-name");

  for (const control of [password, otp, card, secretEditor]) {
    assert.equal(control.value, "[REDACTED]");
    assert.equal(control.value_redacted, true);
    assert.equal(control.value_redaction_reason, "sensitive_input_structure");
    assert.equal(control.width, control.id === "secret-editor" ? 480 : 500);
    assert.equal(control.height, 28);
  }
  assert.equal(ordinary.value, "Visible ordinary value");
  assert.equal(ordinary.value_redacted, undefined);
  assert.equal(ordinary.autocomplete, "name");
  const serialized = JSON.stringify(evidence);
  assert.doesNotMatch(serialized, /password-secret|123456|4111111111111111|contenteditable-secret/);
});

// Values built inside a jsdom window carry that realm's prototypes; compare their plain data.
const plainJson = (value) => JSON.parse(JSON.stringify(value));

test("a focused multi-line textarea is listed line by line with its caret line", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <p data-y="10">Fill the manifest.</p>
    <textarea id="manifest" aria-label="Manifest" data-y="40" data-height="300"></textarea>`);
  try {
    const area = dom.window.document.querySelector("#manifest");
    area.value = "Shipping manifest\nContainer ID:  ________________\nSeal number: __\n";
    area.focus();
    area.setSelectionRange(20, 20);
    const evidence = fn([]);
    const row = evidence.controls.find((control) => control.id === "manifest");
    assert.equal(row.focused, true);
    assert.deepEqual(plainJson(evidence.focused_text_lines), {
      control: row.index,
      line_count: 4,
      lines: ["1|Shipping manifest", "2|Container ID:  ________________", "3|Seal number: __", "4|"],
      caret_line: 2,
    });
    // A single line has nothing to number; an unfocused textarea is not read out.
    area.value = "One line";
    assert.equal(fn([]).focused_text_lines, undefined);
    area.value = "a\nb";
    area.blur();
    assert.equal(fn([]).focused_text_lines, undefined);
  } finally {
    dom.window.close();
  }
});

test("a long focused text shows the lines around the caret, bounded", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`<textarea id="log" data-y="40" data-height="300"></textarea>`);
  try {
    const area = dom.window.document.querySelector("#log");
    const lines = Array.from({ length: 200 }, (_, index) => `Line ${index + 1}`);
    area.value = lines.join("\n");
    area.focus();
    const caret = lines.slice(0, 119).join("\n").length + 1; // the start of line 120
    area.setSelectionRange(caret, caret);
    const view = plainJson(fn([]).focused_text_lines);
    const numbers = view.lines.map((line) => Number(line.split("|")[0]));
    assert.equal(view.line_count, 200);
    assert.equal(view.caret_line, 120);
    assert.equal(numbers.length, 40);
    assert.ok(numbers.includes(120));
    assert.deepEqual(numbers, Array.from({ length: 40 }, (_, index) => numbers[0] + index));
    assert.ok(Math.abs((numbers[0] + numbers[39]) / 2 - 120) <= 1);
    area.value = `${"x".repeat(5000)}\nshort`;
    area.setSelectionRange(0, 0);
    assert.deepEqual(plainJson(fn([]).focused_text_lines.lines), [`1|${"x".repeat(400)}...[+4600 chars]`, "2|short"]);
  } finally {
    dom.window.close();
  }
});

test("a focused editable region is listed line by line without claiming an exact caret line", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <div id="editor" contenteditable="true" tabindex="0" aria-label="Notes" data-y="40" data-height="200">Agenda
Item one: ____
Item two: ____</div>`);
  try {
    dom.window.document.querySelector("#editor").focus();
    const view = plainJson(fn([]).focused_text_lines);
    assert.deepEqual(view.lines, ["1|Agenda", "2|Item one: ____", "3|Item two: ____"]);
    assert.equal(view.caret_line, undefined);
  } finally {
    dom.window.close();
  }
});

test("a sensitive focused text control is never listed line by line", () => {
  // Structurally sensitive (a one-time-code field) or classified sensitive by the model.
  for (const [html, selectors] of [
    [`<textarea id="phrase" autocomplete="one-time-code" data-y="40"></textarea>`, []],
    [`<textarea id="phrase" aria-label="Recovery phrase" data-y="40"></textarea>`, ["#phrase"]],
  ]) {
    const { dom, fn } = loadVisionEvidenceWindow(html);
    try {
      const area = dom.window.document.querySelector("#phrase");
      area.value = "orbit maple canyon\nsecond-secret-line";
      area.focus();
      const evidence = fn(selectors);
      assert.equal(evidence.controls.find((control) => control.id === "phrase").focused, true);
      assert.equal(evidence.focused_text_lines, undefined);
      assert.doesNotMatch(JSON.stringify(evidence), /orbit maple canyon|second-secret-line/);
    } finally {
      dom.window.close();
    }
  }
  // Another control's secret written into the focused text reads redacted on its line.
  const { dom, fn } = loadVisionEvidenceWindow(`
    <input id="password" type="password" value="hunter2-secret" data-y="10">
    <textarea id="notes" data-y="60"></textarea>`);
  try {
    const area = dom.window.document.querySelector("#notes");
    area.value = "Account notes\nPassword is hunter2-secret today\nEnd";
    area.focus();
    const view = fn([]).focused_text_lines;
    assert.equal(view.lines[0], "1|Account notes");
    assert.match(view.lines[1], /^2\|Password is \[REDACTED\] today$/);
    assert.doesNotMatch(JSON.stringify(view), /hunter2-secret/);
  } finally {
    dom.window.close();
  }
});

test("the focused field's caret and selection are reported like a native control's", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <input id="rename" data-y="10" value="q3-report.txt">
    <textarea id="notes" data-y="60"></textarea>
    <input id="pin" type="password" data-y="120" value="hunter2-secret">`);
  try {
    const doc = dom.window.document;
    // A rename editor that pre-selects the name before its extension: the selection is what a
    // keystroke replaces, and the line shows what stays.
    const rename = doc.querySelector("#rename");
    rename.focus();
    rename.setSelectionRange(0, 9);
    assert.deepEqual(plainJson(fn([]).caret), {
      line_text: "q3-report.txt",
      column: 0,
      selection_length: 9,
      selected_text: "q3-report",
    });
    // In a multi-line field the caret's own line, and its column within that line.
    const notes = doc.querySelector("#notes");
    notes.value = "Agenda\nItem one: ____\nEnd";
    notes.focus();
    notes.setSelectionRange(17, 21);
    assert.deepEqual(plainJson(fn([]).caret), {
      line_text: "Item one: ____",
      column: 10,
      selection_length: 4,
      selected_text: "____",
    });
    notes.setSelectionRange(0, 0);
    assert.equal(fn([]).caret.line_text, "Agenda");
    // Nothing focused, or a secret field focused: no caret.
    notes.blur();
    assert.equal(fn([]).caret, undefined);
    doc.querySelector("#pin").focus();
    const evidence = fn([]);
    assert.equal(evidence.caret, undefined);
    assert.doesNotMatch(JSON.stringify(evidence), /hunter2-secret/);
  } finally {
    dom.window.close();
  }
});

test("document-text mode returns the whole rendered document, redacted like the viewport text", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <h1 data-y="10">Paperwork 0 / 4</h1>
    <p data-y="60">1. Rename the invoice</p>
    <p data-y="2400">4. Label the customs form as customs-CF-88.pdf</p>
    <div id="panel" style="overflow: auto" data-y="100" data-height="120">
      <div data-y="-400">3. Label the passport scan as passport-ID-2291.pdf</div>
    </div>
    <p style="display: none" data-y="80">Hidden draft note</p>
    <div id="secret-editor" contenteditable="true" autocomplete="new-password" data-y="140">contenteditable-secret</div>
    <textarea id="note" data-y="180">recovery words here</textarea>`);
  try {
    // The document scrolls to 3,000 px; the panel is scrolled 520 px down its 2,000 px of content.
    Object.defineProperty(dom.window.document.documentElement, "scrollHeight", { value: 3000, configurable: true });
    const panel = dom.window.document.querySelector("#panel");
    Object.defineProperty(panel, "scrollHeight", { value: 2000, configurable: true });
    Object.defineProperty(panel, "clientHeight", { value: 120, configurable: true });
    Object.defineProperty(panel, "scrollTop", { value: 520, configurable: true });
    const whole = fn([], { document_text: true, max_chars: 6000 });
    assert.equal(whole.url, "https://example.test/apply");
    assert.match(whole.document_text, /Paperwork 0 \/ 4/);
    assert.match(whole.document_text, /customs-CF-88\.pdf/, "text below the viewport is read");
    assert.match(whole.document_text, /passport-ID-2291\.pdf/, "text scrolled out of a panel is read");
    assert.doesNotMatch(whole.document_text, /Hidden draft note/);
    assert.doesNotMatch(whole.document_text, /contenteditable-secret/);
    assert.equal(whole.controls, undefined, "only the text, not the screenshot's evidence");
    // The viewport text of the same page leaves the far-below line out.
    assert.doesNotMatch(String(fn([]).viewport_text || ""), /customs-CF-88/);
    // A model-classified sensitive control is redacted; a selector that no longer names exactly one
    // element returns no text at all.
    assert.match(fn([], { document_text: true }).document_text, /recovery words here/);
    const classified = fn(["#note"], { document_text: true });
    assert.doesNotMatch(classified.document_text, /recovery words here/);
    const drifted = fn(["#gone"], { document_text: true });
    assert.equal(drifted.error, "sensitive_selector_drift");
    assert.equal(drifted.document_text, "");
    const bounded = fn([], { document_text: true, max_chars: 500 });
    assert.ok(bounded.document_text.length <= 500);
  } finally {
    dom.window.close();
  }
});

test("document-text mode leaves out text positioned off canvas", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <p data-y="10">Paperwork 0 / 4</p>
    <p data-y="2400">4. Label the customs form as customs-CF-88.pdf</p>
    <p data-x="-9999" data-y="40">Off the left edge 4417</p>
    <p data-x="5000" data-y="60">Past the right edge 5521</p>
    <p data-y="-5000">Above the document 6630</p>
    <p data-y="9000">Below the document 7741</p>
    <div id="offstage" style="overflow: auto" data-x="-9999" data-y="80">
      <div data-x="-9990" data-y="90">Inside an off-canvas panel 8852</div>
    </div>
    <div id="panel" style="overflow: auto" data-y="100" data-height="120">
      <div data-y="-400">3. Label the passport scan as passport-ID-2291.pdf</div>
      <div data-y="-3000">Above the panel's scroll origin 9963</div>
    </div>`);
  try {
    dom.window.Element.prototype.getBoundingClientRect = function () {
      const x = Number(this.getAttribute("data-x") || 20);
      const y = Number(this.getAttribute("data-y") || 0);
      const height = Number(this.getAttribute("data-height") || 28);
      return { left: x, top: y, right: x + 480, bottom: y + height, width: 480, height, x, y };
    };
    Object.defineProperty(dom.window.document.documentElement, "scrollHeight", { value: 3000, configurable: true });
    Object.defineProperty(dom.window.document.documentElement, "scrollWidth", { value: 1024, configurable: true });
    const panel = dom.window.document.querySelector("#panel");
    Object.defineProperty(panel, "scrollHeight", { value: 2000, configurable: true });
    Object.defineProperty(panel, "clientHeight", { value: 120, configurable: true });
    Object.defineProperty(panel, "scrollTop", { value: 520, configurable: true });
    const text = fn([], { document_text: true, max_chars: 6000 }).document_text;
    assert.match(text, /Paperwork 0 \/ 4/);
    assert.match(text, /customs-CF-88\.pdf/, "below the fold but inside the document");
    assert.match(text, /passport-ID-2291\.pdf/, "scrolled out of its panel but inside the panel's scroll area");
    for (const hidden of ["4417", "5521", "6630", "7741", "8852", "9963"]) {
      assert.doesNotMatch(text, new RegExp(hidden), `off-canvas text ${hidden} is left out`);
    }
  } finally {
    dom.window.close();
  }
});

function loadReadRunTabTextRuntime(html, {
  tabId = 44,
  dialog = null,
  maskContext = null,
} = {}) {
  const dom = new JSDOM(`<!doctype html><html><head><title>Paperwork</title></head><body>${html}</body></html>`, {
    url: "https://example.test/paperwork",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  Object.defineProperty(dom.window, "innerHeight", { value: 900, configurable: true });
  Object.defineProperty(dom.window.document.documentElement, "scrollHeight", { value: 4000, configurable: true });
  dom.window.Element.prototype.getBoundingClientRect = function () {
    const y = Number(this.getAttribute("data-y") || 0);
    return { left: 20, top: y, right: 500, bottom: y + 28, width: 480, height: 28, x: 20, y };
  };
  const calls = [];
  const _visionPageEvidence = dom.window.eval(`(${extractFunctionSource("_visionPageEvidence")})`);
  const activeTabId = async () => tabId;
  const nativeDialogOpenFor = () => dialog;
  const sensitiveScreenshotMaskContextForTab = () => maskContext;
  const withTimeout = async (promise) => promise;
  const errorMessage = (error) => String((error && error.message) || error || "");
  const normalizeSensitiveScreenshotSelectors = eval(
    `(${extractFunctionSource("normalizeSensitiveScreenshotSelectors")})`,
  );
  // Only script injection exists: any tab activation, window focus, or capture would throw.
  const chrome = {
    scripting: {
      executeScript: async ({ target, func, args }) => {
        calls.push({ target, args });
        return [{ frameId: 0, result: func(...args) }];
      },
    },
  };
  const runReadRunTabText = eval(`(${extractFunctionSource("runReadRunTabText")})`);
  return { dom, calls, runReadRunTabText };
}

test("read_run_tab_text reads only the run's tab in place and returns its whole text", async () => {
  const runtime = loadReadRunTabTextRuntime(`
    <p data-y="10">Paperwork 1 / 4</p>
    <p data-y="3000">4. Label the customs form as customs-CF-88.pdf</p>`);
  try {
    const result = await runtime.runReadRunTabText({ max_chars: 6000 });
    assert.equal(result.ok, true);
    assert.equal(result.action, "read_run_tab_text");
    assert.equal(result.tab_id, 44);
    assert.equal(result.url, "https://example.test/paperwork");
    assert.equal(result.title, "Paperwork");
    assert.match(result.text, /Paperwork 1 \/ 4/);
    assert.match(result.text, /customs-CF-88\.pdf/);
    assert.equal(result.truncated, false);
    assert.deepEqual(runtime.calls.map((call) => call.target), [{ tabId: 44 }]);
    assert.deepEqual(runtime.calls[0].args, [[], { document_text: true, max_chars: 6000 }]);
  } finally {
    runtime.dom.window.close();
  }
});

test("read_run_tab_text refuses without a run tab, under a dialog, or without its sensitive binding", async () => {
  const html = `<p data-y="10">Paperwork</p><textarea id="note" data-y="60">recovery words here</textarea>`;
  const cases = [
    [{ tabId: null }, {}, "run_tab_unavailable"],
    [{ dialog: { type: "alert", message: "Saved" } }, {}, "native_dialog_open"],
    [{}, { required_sensitive_mask_binding: null }, "sensitive_mask_page_context_required"],
    [{ maskContext: { required: true, selectors: [] } }, {}, "sensitive_mask_binding_lost"],
    [{}, { required_sensitive_mask_binding: { selector: "#gone", expected_page_context: {} } }, "sensitive_selector_drift"],
  ];
  for (const [options, params, reason] of cases) {
    const runtime = loadReadRunTabTextRuntime(html, options);
    try {
      const result = await runtime.runReadRunTabText(params);
      assert.equal(result.ok, false, reason);
      assert.equal(result.failure_reason, reason);
      assert.equal(result.text, undefined);
      assert.equal(result.sensitive_mask_rebind_required, undefined, "a refused read never asks for a rebind");
    } finally {
      runtime.dom.window.close();
    }
  }
  // The tab's own binding and the backend's required binding both redact.
  const runtime = loadReadRunTabTextRuntime(html, { maskContext: { required: true, selectors: ["#note"] } });
  try {
    const result = await runtime.runReadRunTabText({
      required_sensitive_mask_binding: { selector: "#note", expected_page_context: {} },
    });
    assert.equal(result.ok, true);
    assert.deepEqual(runtime.calls[0].args[0], ["#note"]);
    assert.doesNotMatch(result.text, /recovery words here/);
  } finally {
    runtime.dom.window.close();
  }
});

function loadInjectedClickWindow(html) {
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const acquireDebugger = async () => ({ attached_now: false });
  const releaseDebugger = () => {};
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    url: "https://ats.rippling.com/capacity/jobs/example",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  Object.defineProperty(dom.window, "devicePixelRatio", { value: 2, configurable: true });
  dom.window.PointerEvent = undefined;
  dom.window.document.elementFromPoint = (x, y) => {
    for (const el of dom.window.document.querySelectorAll("[data-hit]")) {
      const rect = el.getBoundingClientRect();
      if (x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) return el;
    }
    return null;
  };
  const preflight = dom.window.eval(`(${extractFunctionSource("visionClickAtPointInPage")})`);
  // Production dispatches one trusted CDP click at the preflighted point; the harness stands in
  // with a DOM click on whatever element is hit there.
  const fn = (...args) => {
    const probe = preflight(...args);
    if (probe.ok) {
      const hit = dom.window.document.elementFromPoint(probe.x, probe.y);
      if (hit) hit.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true, cancelable: true }));
    }
    return probe;
  };
  const pointFn = dom.window.eval(`(${extractFunctionSource("visionPointInfoInPage")})`);
  return { dom, fn, pointFn };
}

function stubRects(dom, rects) {
  for (const node of dom.window.document.querySelectorAll("[data-hit]")) {
    const r = rects[node.id];
    node.getBoundingClientRect = () => ({
      ...r, x: r.left, y: r.top, right: r.left + r.width, bottom: r.top + r.height,
    });
  }
}

test("point probe follows a re-rendered control by semantics when a different control now sits under the point", () => {
  const { dom, pointFn } = loadInjectedClickWindow(`
    <button data-hit id="m-new1">Refresh data</button>
    <button data-hit id="m-new2">Verify identity</button>`);
  stubRects(dom, {
    "m-new1": { left: 10, top: 10, width: 120, height: 30 },
    "m-new2": { left: 200, top: 10, width: 120, height: 30 },
  });
  // The model chose "Verify identity" while it occupied the first slot; the panel re-rendered with
  // fresh ids and shuffled the slots. Image px (140, 50) at dpr 2 = CSS (70, 25) = "Refresh data".
  const expected = {
    target_selector: "#m-old",
    target_text: "Verify identity",
    target_tag: "button",
    target_type: "button", // observer fallback for a typeless <button>
    target_role: "",
    dispatch_path: "dom_activation",
    page_url: dom.window.location.href,
  };
  const result = pointFn(140, 50, false, false, expected);
  assert.equal(result.ok, true);
  assert.equal(result.rebound, true);
  assert.equal(result.target_text, "Verify identity");
  assert.equal(result.x, 260);
  assert.equal(result.y, 25);
  assert.equal(result.rebound_from.x, 70);
  assert.equal(result.rebound_from.y, 25);

  const strict = pointFn(140, 50, true, false, expected);
  assert.equal(strict.ok, false);
  assert.notEqual(strict.rebound, true);
});

test("DOM-activation click probe follows a re-rendered control by semantics too", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <button data-hit id="m-new1">Refresh data</button>
    <button data-hit id="m-new2">Verify identity</button>`);
  stubRects(dom, {
    "m-new1": { left: 10, top: 10, width: 120, height: 30 },
    "m-new2": { left: 200, top: 10, width: 120, height: 30 },
  });
  const expected = {
    target_selector: "#m-old",
    target_text: "Verify identity",
    target_tag: "button",
    target_type: "button",
    target_role: "",
    dispatch_path: "dom_activation",
    page_url: dom.window.location.href,
  };
  const probed = fn(140, 50, false, false, expected);
  assert.equal(probed.ok, true);
  assert.equal(probed.rebound, true);
  assert.equal(probed.target_text, "Verify identity");
  assert.equal(probed.x, 260);
});

test("point probe names a checkbox by its associated label, matching the observer's identity", () => {
  const { dom, pointFn } = loadInjectedClickWindow(`
    <label for="telemetry" data-hit id="lbl">Telemetry off</label>
    <input type="checkbox" id="telemetry" data-hit>`);
  stubRects(dom, {
    lbl: { left: 30, top: 10, width: 120, height: 20 },
    telemetry: { left: 10, top: 12, width: 14, height: 14 },
  });
  const expected = {
    target_selector: "#telemetry",
    target_text: "Telemetry off", // observer identity from the label relation, never the on/off value
    target_tag: "input",
    target_type: "checkbox",
    target_role: "",
    dispatch_path: "dom_activation",
    page_url: dom.window.location.href,
  };
  const result = pointFn(34, 38, false, false, expected); // CSS (17, 19) at dpr 2 = the checkbox
  assert.equal(result.ok, true);
  assert.equal(result.target_text, "Telemetry off");
  assert.equal(result.target_selector, "#telemetry");
});

test("point probe refuses to rebind when the semantic identity is ambiguous", () => {
  const { dom, pointFn } = loadInjectedClickWindow(`
    <button data-hit id="m-new1">Refresh data</button>
    <button data-hit id="m-new2">Verify identity</button>
    <button data-hit id="m-new3">Verify identity</button>`);
  stubRects(dom, {
    "m-new1": { left: 10, top: 10, width: 120, height: 30 },
    "m-new2": { left: 200, top: 10, width: 120, height: 30 },
    "m-new3": { left: 400, top: 10, width: 120, height: 30 },
  });
  const expected = {
    target_selector: "#m-old",
    target_text: "Verify identity",
    target_tag: "button",
    target_type: "",
    target_role: "",
    dispatch_path: "dom_activation",
    page_url: dom.window.location.href,
  };
  const result = pointFn(140, 50, false, false, expected);
  assert.equal(result.ok, false);
  assert.equal(result.failure_reason, "click_target_drift");
  assert.equal(result.activation_attempted, false);
});

function loadInjectedControlClickWindow(
  html,
  {
    livePageUrl = "",
    liveSurfaceSignature = null,
  } = {},
) {
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const acquireDebugger = async () => ({ attached_now: false });
  const releaseDebugger = () => {};
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    url: "https://ats.rippling.com/capacity/jobs/example",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  Object.defineProperty(dom.window, "devicePixelRatio", { value: 2, configurable: true });
  dom.window.PointerEvent = undefined;
  const scrolled = [];
  dom.window.Element.prototype.scrollIntoView = function (options) {
    scrolled.push({ id: this.id, options });
  };
  dom.window.document.elementFromPoint = (x, y) => {
    for (const el of dom.window.document.querySelectorAll("[data-hit]")) {
      const rect = el.getBoundingClientRect();
      if (x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) return el;
    }
    return null;
  };
  const visionControlCenterInPage = eval(`(${extractFunctionSource("visionControlCenterInPage")})`);
  const clickAtPoint = dom.window.eval(`(${extractFunctionSource("visionClickAtPointInPage")})`);
  const calls = [];
  const pageContexts = [];
  const chrome = {
    scripting: {
      executeScript: async ({ func, args }) => {
        const pageFunc = dom.window.eval(`(${func.toString()})`);
        return [{ result: pageFunc(...(args || [])) }];
      },
    },
  };
  const withTimeout = async (promise) => promise;
  const errorMessage = (error) => (error && error.message ? String(error.message) : String(error || ""));
  const runVisionClick = async (
    _tabId,
    imgX,
    imgY,
    allowFinalSubmit = false,
    allowGuardedIntermediate = false,
    expectedTarget = null,
    expectedPageContext = null,
  ) => {
    calls.push([
      imgX,
      imgY,
      Boolean(allowFinalSubmit),
      ...(allowGuardedIntermediate ? [true] : []),
    ]);
    pageContexts.push(expectedPageContext);
    const probe = clickAtPoint(imgX, imgY, allowFinalSubmit, allowGuardedIntermediate, expectedTarget);
    if (!probe.ok) return probe;
    const hit = dom.window.document.elementFromPoint(probe.x, probe.y);
    if (hit) hit.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true, cancelable: true }));
    return { ...probe, mode: "cdp_trusted_click", activation_count: 1 };
  };
  const clickTargetIdentityFromPoint = eval(`(${extractFunctionSource("clickTargetIdentityFromPoint")})`);
  const clickTargetIdentityVerdict = eval(`(${extractFunctionSource("clickTargetIdentityVerdict")})`);
  const clickTargetIdentityComplete = eval(`(${extractFunctionSource("clickTargetIdentityComplete")})`);
  const observedPageContextVerdict = async (_tabId, expected) => {
    const liveUrl = livePageUrl || dom.window.location.href;
    const liveSurface = liveSurfaceSignature == null
      ? String(expected && expected.surface_signature || "")
      : String(liveSurfaceSignature);
    const live = { url: liveUrl, surface_signature: liveSurface };
    if (!expected || expected.url !== liveUrl) {
      return { ok: false, reason: "page_context_drift", mismatch_field: "url", expected, live };
    }
    if (
      expected.surface_signature
      && String(expected.surface_signature) !== liveSurface
    ) {
      return {
        ok: false,
        reason: "page_context_drift",
        mismatch_field: "surface_signature",
        expected,
        live,
      };
    }
    return { ok: true, expected, live };
  };
  const rawFn = eval(`(${extractFunctionSource("runVisionClickControl")})`);
  const fn = (
    tabId,
    selector,
    fallbackX,
    fallbackY,
    allowFinalSubmit = false,
    allowGuardedIntermediate = false,
    expectedTarget = null,
    expectedPageContext = null,
  ) => {
    const fallbackNode = dom.window.document.elementFromPoint(
      Math.round(Number(fallbackX || 0) / 2),
      Math.round(Number(fallbackY || 0) / 2),
    );
    const target = dom.window.document.querySelector(selector) || fallbackNode;
    const targetText = target
      ? String(
        target.innerText
        || target.textContent
        || (() => {
          // The observer names a form control by its accessible label before hints such as
          // placeholder/name; the in-page probes do the same.
          const explicit = target.id ? dom.window.document.querySelector(`label[for="${target.id}"]`) : null;
          const wrapper = target.closest ? target.closest("label") : null;
          const node = explicit || wrapper;
          return node ? String(node.textContent || "").replace(/\s+/g, " ").trim() : "";
        })()
        || target.getAttribute("aria-label")
        || target.getAttribute("placeholder")
        || target.getAttribute("name")
        || "",
      ).replace(/\s+/g, " ").trim()
      : "";
    const identity = expectedTarget || {
      target_selector: selector,
      target_text: targetText,
      target_tag: target ? target.tagName.toLowerCase() : "",
      target_type: target ? String(target.getAttribute("type") || "").toLowerCase() : "",
      target_role: target ? String(target.getAttribute("role") || "").toLowerCase() : "",
      dispatch_path: target && target.closest("canvas,svg") ? "cdp_trusted_click" : "dom_activation",
      page_url: dom.window.location.href,
    };
    return rawFn(
      tabId,
      selector,
      fallbackX,
      fallbackY,
      allowFinalSubmit,
      allowGuardedIntermediate,
      identity,
      expectedPageContext || { url: dom.window.location.href },
    );
  };
  return { dom, fn, calls, pageContexts, scrolled };
}

function loadPrivilegedFinalClickRuntime(
  html,
  { driftAfterFirstTrustedProbeHtml = "", driftAfterImmediateProbeHtml = "", liveEvidence = {}, coverPressNumber = 0, moveAfterFirstTrustedProbeTo = null } = {},
) {
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    url: "https://ats.rippling.com/capacity/jobs/example",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  Object.defineProperty(dom.window, "devicePixelRatio", { value: 2, configurable: true });
  dom.window.PointerEvent = undefined;
  dom.window.Element.prototype.scrollIntoView = function () {};
  for (const button of dom.window.document.querySelectorAll("button")) {
    Object.defineProperty(button, "innerText", { value: button.textContent || "", configurable: true });
  }
  const dispatches = [];
  let pixelRecaptures = 0;
  let clickInvocations = 0;
  let liveTarget = dom.window.document.querySelector("button");
  let currentRect = { left: 40, top: 20, right: 160, bottom: 70, width: 120, height: 50 };
  const targetRect = () => currentRect;
  const prepareTarget = (node) => {
    node.getBoundingClientRect = targetRect;
    node.addEventListener("click", (event) => {
      event.preventDefault();
      clickInvocations += 1;
    });
  };
  prepareTarget(liveTarget);
  dom.window.document.elementFromPoint = (x, y) => {
    const rect = liveTarget && liveTarget.getBoundingClientRect();
    return rect && x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom ? liveTarget : null;
  };
  let trustedProbeCount = 0;
  let pressesSeen = 0;
  const chrome = {
    runtime: { lastError: null },
    scripting: {
      executeScript: async ({ func, args }) => {
        const pageFunc = dom.window.eval(`(${func.toString()})`);
        const result = await pageFunc(...(args || []));
        if (func.name === "visionPointInfoInPage") {
          trustedProbeCount += 1;
          const driftHtml = trustedProbeCount === 1
            ? driftAfterFirstTrustedProbeHtml
            : (trustedProbeCount === 2 ? driftAfterImmediateProbeHtml : "");
          if (trustedProbeCount === 2 && moveAfterFirstTrustedProbeTo) currentRect = moveAfterFirstTrustedProbeTo;
          if (driftHtml) {
            const template = dom.window.document.createElement("template");
            template.innerHTML = String(driftHtml).trim();
            const replacement = template.content.firstElementChild;
            assert.ok(replacement, "final-click drift fixture must produce a replacement target");
            liveTarget.replaceWith(replacement);
            liveTarget = replacement;
            prepareTarget(liveTarget);
          }
        }
        return [{ result }];
      },
    },
    debugger: {
      attach: (_target, _version, callback) => callback(),
      detach: (_target, callback) => callback(),
    },
  };
  const withTimeout = async (promise) => promise;
  // Stand-ins for the tab debugger session: one acquire/release per primitive is observable
  // as one attach/detach on the stubbed chrome.debugger.
  const acquireDebugger = (target) => new Promise((resolve, reject) => chrome.debugger.attach(target, "1.3", () => (
    chrome.runtime.lastError ? reject(new Error(chrome.runtime.lastError.message)) : resolve({ attached_now: true })
  )));
  const releaseDebugger = (target) => { chrome.debugger.detach(target, () => {}); };
  const errorMessage = (error) => (error && error.message ? String(error.message) : String(error || ""));
  const waitForLoad = async () => {};
  const tabPageEvidence = async () => ({
    url: dom.window.location.href,
    title: "Application",
    surface_signature: "stable",
    ...liveEvidence,
  });
  const captureTabLineage = async () => ({});
  const adoptNewTabAfterClick = async (tabId) => ({ tabId, adopted: false, adopted_url: "" });
  dom.window.__lumenDispatchGuardCountsSyntheticEvents = true;
  const _cdp = async (_target, method, params) => {
    dispatches.push({ method, params });
    if (method === "Input.dispatchMouseEvent" && params.type === "mousePressed") {
      pressesSeen += 1;
      // A page that puts something over the control from the Nth press on.
      const cover = coverPressNumber && pressesSeen >= coverPressNumber ? dom.window.document.getElementById("cover") : null;
      // A moving target is hit where the press actually lands, not where the test knows it is.
      const hit = moveAfterFirstTrustedProbeTo ? (dom.window.document.elementFromPoint(params.x, params.y) || dom.window.document.body) : liveTarget;
      (cover || hit).dispatchEvent(new dom.window.MouseEvent("mousedown", { bubbles: true, cancelable: true }));
    }
    if (method === "Input.dispatchMouseEvent" && params.type === "mouseReleased") {
      const cover = coverPressNumber && pressesSeen >= coverPressNumber ? dom.window.document.getElementById("cover") : null;
      const hit = moveAfterFirstTrustedProbeTo ? (dom.window.document.elementFromPoint(params.x, params.y) || dom.window.document.body) : liveTarget;
      (cover || hit).dispatchEvent(new dom.window.MouseEvent(params.button === "right" ? "contextmenu" : "click", { bubbles: true, cancelable: true }));
      if (!cover && params.clickCount > 1 && params.button !== "right") liveTarget.dispatchEvent(new dom.window.MouseEvent("dblclick", { bubbles: true, cancelable: true }));
    }
    return {};
  };
  const visionPointInfoInPage = dom.window.eval(`(${extractFunctionSource("visionPointInfoInPage")})`);
  const visionControlCenterInPage = dom.window.eval(`(${extractFunctionSource("visionControlCenterInPage")})`);
  const activeElementEvidenceInPage = dom.window.eval(`(${extractFunctionSource("activeElementEvidenceInPage")})`);
  const clickOutcomeEvidenceInPage = dom.window.eval(`(${extractFunctionSource("clickOutcomeEvidenceInPage")})`);
  const clickActionabilityInPage = dom.window.eval(`(${extractFunctionSource("clickActionabilityInPage")})`);
  const clickPointIdentityComplete = eval(`(${extractFunctionSource("clickPointIdentityComplete")})`);
  const visionClickAtPointInPage = dom.window.eval(`(${extractFunctionSource("visionClickAtPointInPage")})`);
  const clickTargetIdentityFromPoint = eval(`(${extractFunctionSource("clickTargetIdentityFromPoint")})`);
  const clickTargetIdentityVerdict = eval(`(${extractFunctionSource("clickTargetIdentityVerdict")})`);
  const clickTargetIdentityComplete = eval(`(${extractFunctionSource("clickTargetIdentityComplete")})`);
  const observedPageContextVerdict = eval(`(${extractFunctionSource("observedPageContextVerdict")})`);
  const captureMaskedViewportFingerprint = async () => {
    pixelRecaptures += 1;
    return { ok: false, reason: "coordinate_pixel_drift" };
  };
  const withScreenshotCaptureLock = async (operation) => operation();
  const animatePointerToPoint = async () => ({ ok: true, visible: false });
  const showPointerClickPulse = () => {};
  const dispatchGuardReportInPage = dom.window.eval(`(${extractFunctionSource("dispatchGuardReportInPage")})`);
  const readDispatchGuard = eval(`(${extractFunctionSource("readDispatchGuard")})`);
  const runTrustedVisionClick = eval(`(${extractFunctionSource("runTrustedVisionClick")})`);
  const runVisionClickAt = eval(`(${extractFunctionSource("runVisionClickAt")})`);
  const runVisionClick = eval(`(${extractFunctionSource("runVisionClick")})`);
  const findIdentityTwinInPage = dom.window.eval(`(${extractFunctionSource("findIdentityTwinInPage")})`);
  const rawRunVisionClickControl = eval(`(${extractFunctionSource("runVisionClickControl")})`);
  const runVisionClickControl = (
    tabId,
    selector,
    fallbackX,
    fallbackY,
    allowFinalSubmit = false,
    allowGuardedIntermediate = false,
    expectedTarget = null,
    expectedPageContext = null,
    pointer = null,
  ) => {
    const center = visionControlCenterInPage(selector, false, false);
    return rawRunVisionClickControl(
      tabId,
      selector,
      fallbackX,
      fallbackY,
      allowFinalSubmit,
      allowGuardedIntermediate,
      expectedTarget || center,
      expectedPageContext || { url: dom.window.location.href, surface_signature: "stable" },
      pointer,
    );
  };
  return {
    dom,
    dispatches,
    runVisionClick,
    runVisionClickControl,
    clickInvocations: () => clickInvocations,
    pixelRecaptures: () => pixelRecaptures,
  };
}

function loadSingleActivationClickRuntime(
  html,
  {
    mutateSurface = false,
    driftAfterPreflightHtml = "",
    pixelDrift = false,
    pixelVerdict = null,
    pressAckTimesOut = false,
  } = {},
) {
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    url: "https://example.test/task",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  Object.defineProperty(dom.window, "devicePixelRatio", { value: 1, configurable: true });
  Object.defineProperty(dom.window, "innerWidth", { value: 1000, configurable: true });
  Object.defineProperty(dom.window, "innerHeight", { value: 800, configurable: true });
  dom.window.PointerEvent = undefined;
  let hit = dom.window.document.querySelector("[data-hit]");
  const prepareHit = (node) => {
    node.getBoundingClientRect = () => ({
      left: 20,
      top: 20,
      right: 220,
      bottom: 120,
      width: 200,
      height: 100,
      x: 20,
      y: 20,
    });
    node.addEventListener("click", (event) => {
      event.preventDefault();
      clickInvocations += 1;
      if (mutateSurface) node.setAttribute("data-state", "activated");
    });
  };
  dom.window.document.elementFromPoint = (x, y) => (
    x >= 20 && x <= 220 && y >= 20 && y <= 120 ? hit : null
  );

  let clickInvocations = 0;
  prepareHit(hit);
  const dispatches = [];
  let attachCalls = 0;
  let detachCalls = 0;
  let drifted = false;
  const chrome = {
    runtime: { lastError: null },
    scripting: {
      executeScript: async ({ func, args }) => {
        const pageFunc = dom.window.eval(`(${func.toString()})`);
        const result = pageFunc(...(args || []));
        if (
          !drifted
          && driftAfterPreflightHtml
          && func.name === "visionClickAtPointInPage"
        ) {
          const template = dom.window.document.createElement("template");
          template.innerHTML = String(driftAfterPreflightHtml).trim();
          const replacement = template.content.firstElementChild;
          if (!replacement) throw new Error("drift fixture did not produce a replacement target");
          hit.replaceWith(replacement);
          hit = replacement;
          prepareHit(hit);
          drifted = true;
        }
        return [{ result }];
      },
    },
    debugger: {
      attach: (_target, _version, callback) => {
        attachCalls += 1;
        callback();
      },
      detach: (_target, callback) => {
        detachCalls += 1;
        callback();
      },
    },
  };
  const withTimeout = async (promise) => promise;
  // Stand-ins for the tab debugger session: one acquire/release per primitive is observable
  // as one attach/detach on the stubbed chrome.debugger.
  const acquireDebugger = (target) => new Promise((resolve, reject) => chrome.debugger.attach(target, "1.3", () => (
    chrome.runtime.lastError ? reject(new Error(chrome.runtime.lastError.message)) : resolve({ attached_now: true })
  )));
  const releaseDebugger = (target) => { chrome.debugger.detach(target, () => {}); };
  const errorMessage = (error) => (error && error.message ? String(error.message) : String(error || ""));
  let settleWaits = 0;
  const waitForLoad = async () => { settleWaits += 1; };
  const tabPageEvidence = async () => ({
    url: dom.window.location.href,
    title: "Task",
    surface_signature: `surface:${hit.getAttribute("data-state") || "initial"}`,
  });
  const captureTabLineage = async () => ({ source_tab_id: 7, before_tab_ids: [7] });
  const adoptNewTabAfterClick = async (tabId) => ({ tabId, adopted: false, adopted_url: "" });
  const _cdp = async (_target, method, params) => {
    dispatches.push({ method, params });
    if (pressAckTimesOut && method === "Input.dispatchMouseEvent" && params.type === "mousePressed") {
      throw new Error("Input.dispatchMouseEvent timed out");
    }
    if (method === "Input.dispatchMouseEvent" && params.type === "mouseReleased") {
      const target = dom.window.document.elementFromPoint(params.x, params.y);
      if (target) target.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true, cancelable: true }));
    }
    return {};
  };
  const setTimeout = (callback) => {
    callback();
    return 0;
  };
  const visionPointInfoInPage = dom.window.eval(`(${extractFunctionSource("visionPointInfoInPage")})`);
  const visionClickAtPointInPage = dom.window.eval(`(${extractFunctionSource("visionClickAtPointInPage")})`);
  const activeElementEvidenceInPage = dom.window.eval(`(${extractFunctionSource("activeElementEvidenceInPage")})`);
  const clickOutcomeEvidenceInPage = dom.window.eval(`(${extractFunctionSource("clickOutcomeEvidenceInPage")})`);
  const clickTargetIdentityFromPoint = eval(`(${extractFunctionSource("clickTargetIdentityFromPoint")})`);
  const clickTargetIdentityVerdict = eval(`(${extractFunctionSource("clickTargetIdentityVerdict")})`);
  const clickPointIdentityComplete = eval(`(${extractFunctionSource("clickPointIdentityComplete")})`);
  const clickTargetIdentityComplete = eval(`(${extractFunctionSource("clickTargetIdentityComplete")})`);
  const observedPageContextVerdict = async (_tabId, expected) => (
    expected && expected.url === dom.window.location.href
      ? { ok: true, expected, live: { url: dom.window.location.href } }
      : { ok: false, reason: "page_context_drift", mismatch_field: "url" }
  );
  const captureMaskedViewportFingerprint = async () => (
    pixelVerdict && typeof pixelVerdict === "object"
      ? pixelVerdict
      : pixelDrift
      ? {
        ok: false,
        reason: "coordinate_pixel_drift",
        expected_screenshot_sha256: "observed-pixels",
        live_screenshot_sha256: "changed-pixels",
        capture_attempts: 3,
        temporal_exact_hash_retry_attempted: true,
        temporal_exact_hash_recovered: false,
      }
      : { ok: true }
  );
  const withScreenshotCaptureLock = async (operation) => operation();
  const animatePointerToPoint = async () => ({ ok: true, visible: false });
  const showPointerClickPulse = () => {};
  const dispatchGuardReportInPage = dom.window.eval(`(${extractFunctionSource("dispatchGuardReportInPage")})`);
  const readDispatchGuard = eval(`(${extractFunctionSource("readDispatchGuard")})`);
  const runTrustedVisionClick = eval(`(${extractFunctionSource("runTrustedVisionClick")})`);
  const runVisionClickAt = eval(`(${extractFunctionSource("runVisionClickAt")})`);
  const rawRunVisionClick = eval(`(${extractFunctionSource("runVisionClick")})`);
  const runVisionClick = (
    tabId,
    x,
    y,
    allowFinalSubmit = false,
    allowGuardedIntermediate = false,
    expectedTarget = null,
    expectedPageContext = null,
  ) => rawRunVisionClick(
    tabId,
    x,
    y,
    allowFinalSubmit,
    allowGuardedIntermediate,
    expectedTarget,
    expectedPageContext || {
      url: dom.window.location.href,
      surface_signature: `surface:${hit.getAttribute("data-state") || "initial"}`,
      screenshot_sha256: "fixture",
      screenshot_mode: "capture_visible_tab",
      screenshot_quality: 60,
    },
  );
  return {
    dom,
    runVisionClick,
    runTrustedVisionClick,
    settleWaits: () => settleWaits,
    dispatches,
    clickInvocations: () => clickInvocations,
    attachCalls: () => attachCalls,
    detachCalls: () => detachCalls,
  };
}

function loadInjectedUploadResolverWindow(html) {
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const acquireDebugger = async () => ({ attached_now: false });
  const releaseDebugger = () => {};
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    url: "https://ats.rippling.com/capacity/jobs/example",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  Object.defineProperty(dom.window, "devicePixelRatio", { value: 2, configurable: true });
  dom.window.document.elementFromPoint = (x, y) => {
    for (const el of dom.window.document.querySelectorAll("[data-hit]")) {
      const rect = el.getBoundingClientRect();
      if (x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) return el;
    }
    return null;
  };
  const fn = dom.window.eval(`(${extractFunctionSource("resolveUploadTargetInPage")})`);
  return { dom, fn };
}

function loadInjectedTypeControlWindow(html) {
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const acquireDebugger = async () => ({ attached_now: false });
  const releaseDebugger = () => {};
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    url: "https://ats.rippling.com/capacity/jobs/example",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  Object.defineProperty(dom.window, "devicePixelRatio", { value: 2, configurable: true });
  const scrolled = [];
  dom.window.Element.prototype.scrollIntoView = function (options) {
    scrolled.push({ id: this.id, options });
  };
  const chrome = {
    scripting: {
      executeScript: async ({ func, args }) => {
        const pageFunc = dom.window.eval(`(${func.toString()})`);
        return [{ result: pageFunc(...(args || [])) }];
      },
    },
  };
  const withTimeout = async (promise) => promise;
  const errorMessage = (error) => (error && error.message ? String(error.message) : String(error || ""));
  const selectActiveTextInPage = eval(`(${extractFunctionSource("selectActiveTextInPage")})`);
  const activeValueSetInPage = eval(`(${extractFunctionSource("activeValueSetInPage")})`);
  const activeValueEvidenceInPage = eval(`(${extractFunctionSource("activeValueEvidenceInPage")})`);
  const focusControlForTypeInPage = eval(`(${extractFunctionSource("focusControlForTypeInPage")})`);
  const replayCredentialControlInPage = eval(
    `(${extractFunctionSource("replayCredentialControlInPage")})`,
  );
  const showPointerTypingAtActiveControl = async () => ({ ok: true, visible: false });
  const runVisionType = eval(`(${extractFunctionSource("runVisionType")})`);
  const clickTargetIdentityFromPoint = eval(`(${extractFunctionSource("clickTargetIdentityFromPoint")})`);
  const clickTargetIdentityComplete = eval(`(${extractFunctionSource("clickTargetIdentityComplete")})`);
  const observedPageContextVerdict = async (_tabId, expected) => (
    expected && expected.url === dom.window.location.href
      ? { ok: true, expected, live: { url: dom.window.location.href } }
      : { ok: false, reason: "page_context_drift", mismatch_field: "url" }
  );
  const tabPageEvidence = async () => ({
    url: dom.window.location.href,
    document_lifecycle_token: String(
      dom.window.__lumenDocumentLifecycleToken || "",
    ),
  });
  const rawFn = eval(`(${extractFunctionSource("runVisionTypeControl")})`);
  const credentialReplayCommandReceipt = eval(
    `(${extractFunctionSource("credentialReplayCommandReceipt")})`,
  );
  // Keystroke credential replay: the in-page binding helpers run in the page; the harness stands in
  // for CDP by inserting each key's text into the focused field.
  const credentialKeyBindingStillBoundInPage = eval(`(${extractFunctionSource("credentialKeyBindingStillBoundInPage")})`);
  const credentialKeyBindingClearInPage = eval(`(${extractFunctionSource("credentialKeyBindingClearInPage")})`);
  const credentialKeyBindingFinishInPage = eval(`(${extractFunctionSource("credentialKeyBindingFinishInPage")})`);
  void [credentialKeyBindingStillBoundInPage, credentialKeyBindingClearInPage, credentialKeyBindingFinishInPage];
  const _cdp = async (_target, method, params) => {
    if (method !== "Input.dispatchKeyEvent" || params.type !== "keyDown" || !params.text) return;
    if (typeof dom.window.__testBeforeKey === "function") dom.window.__testBeforeKey(params.text);
    let el = dom.window.document.activeElement;
    while (el && el.tagName === "IFRAME" && el.contentDocument) el = el.contentDocument.activeElement;
    if (el && "value" in el) {
      el.value = String(el.value || "") + params.text;
      el.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    }
  };
  const keyDispatchDescription = eval(`(${extractFunctionSource("keyDispatchDescription")})`);
  const printableCharacterKeyDescription = eval(`(${extractFunctionSource("printableCharacterKeyDescription")})`);
  const dispatchTrustedCharacterKeys = eval(`(${extractFunctionSource("dispatchTrustedCharacterKeys")})`);
  const credentialKeystrokeDisabledTabs = new Set();
  const CREDENTIAL_KEYSTROKE_DEADLINE_MS = 12000;
  void [printableCharacterKeyDescription, credentialKeystrokeDisabledTabs, CREDENTIAL_KEYSTROKE_DEADLINE_MS];
  const credentialKeystrokeReplay = eval(`(${extractFunctionSource("credentialKeystrokeReplay")})`);
  void [_cdp, keyDispatchDescription, dispatchTrustedCharacterKeys, credentialKeystrokeReplay];
  const rawCredentialFn = eval(
    `(${extractFunctionSource("runVisionCredentialTypeControl")})`,
  );
  const fn = (
    tabId,
    selector,
    text,
    expectedTarget = null,
    expectedPageContext = null,
    forceRedact = false,
  ) => {
    const target = dom.window.document.querySelector(selector);
    const targetText = target
      ? String(
        target.innerText
        || target.textContent
        || (() => {
          // The observer names a form control by its accessible label before hints such as
          // placeholder/name; the in-page probes do the same.
          const explicit = target.id ? dom.window.document.querySelector(`label[for="${target.id}"]`) : null;
          const wrapper = target.closest ? target.closest("label") : null;
          const node = explicit || wrapper;
          return node ? String(node.textContent || "").replace(/\s+/g, " ").trim() : "";
        })()
        || target.getAttribute("aria-label")
        || target.getAttribute("placeholder")
        || target.getAttribute("name")
        || "",
      ).replace(/\s+/g, " ").trim()
      : "";
    const identity = expectedTarget || {
      target_selector: selector,
      target_text: targetText,
      target_tag: target ? target.tagName.toLowerCase() : "",
      target_type: target ? String(target.getAttribute("type") || "").toLowerCase() : "",
      target_role: target ? String(target.getAttribute("role") || "").toLowerCase() : "",
      dispatch_path: "dom_activation",
      page_url: dom.window.location.href,
    };
    const runner = forceRedact ? rawCredentialFn : rawFn;
    return runner(
      tabId,
      selector,
      text,
      identity,
      expectedPageContext || { url: dom.window.location.href },
    );
  };
  return { dom, fn, scrolled };
}

test("vision click injected function is self-contained", () => {
  const { dom, fn } = loadInjectedClickWindow(`<button data-hit id="apply">Apply now</button>`);
  try {
    const button = dom.window.document.querySelector("#apply");
    button.innerText = "Apply now";
    button.getBoundingClientRect = () => ({ left: 40, top: 20, right: 140, bottom: 60, width: 100, height: 40 });
    let clicked = 0;
    button.addEventListener("click", (event) => { event.preventDefault(); clicked += 1; });

    const result = fn(160, 80);

    assert.equal(result.ok, true);
    assert.equal(result.action, "click");
    assert.equal(result.tag, "button");
    assert.equal(clicked, 1);
  } finally {
    dom.window.close();
  }
});

test("vision click refuses upload labels before a native file dialog can open", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <label data-hit id="resume-drop" for="resume-file">Upload resume</label>
    <input id="resume-file" type="file">
  `);
  try {
    const label = dom.window.document.querySelector("#resume-drop");
    label.innerText = "Upload resume";
    label.getBoundingClientRect = () => ({ left: 40, top: 20, right: 220, bottom: 80, width: 180, height: 60 });
    let clicked = 0;
    label.addEventListener("click", () => { clicked += 1; });

    const result = fn(160, 80);

    assert.equal(result.ok, false);
    assert.equal(result.blocked_file_dialog, true);
    assert.equal(result.failure_reason, "click_would_open_file_dialog");
    assert.equal(result.target_label, "Upload resume");
    assert.equal(clicked, 0);
  } finally {
    dom.window.close();
  }
});

test("vision click injected function still blocks final submit", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <form>
      <input><input><input>
      <button data-hit type="submit" id="submit">Submit</button>
    </form>
  `);
  try {
    dom.window.HTMLFormElement.prototype.requestSubmit = function () {};
    const button = dom.window.document.querySelector("#submit");
    button.innerText = "Submit";
    button.getBoundingClientRect = () => ({ left: 40, top: 20, right: 140, bottom: 60, width: 100, height: 40 });

    const result = fn(160, 80);

    assert.equal(result.ok, false);
    assert.equal(result.blocked_submit, true);
    assert.equal(result.selector, "#submit");
    assert.equal(result.label, "Submit");
  } finally {
    dom.window.close();
  }
});

test("contextual submit guard reports raw structure and permits one revalidated intermediate click", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <input><input><input>
    <button data-hit type="button" id="start" aria-label="Apply">Apply</button>
  `);
  try {
    const button = dom.window.document.querySelector("#start");
    button.innerText = "Apply";
    button.getBoundingClientRect = () => ({ left: 40, top: 20, right: 140, bottom: 60, width: 100, height: 40 });
    let clicked = 0;
    button.addEventListener("click", (event) => { event.preventDefault(); clicked += 1; });

    const blocked = fn(160, 80);
    assert.equal(blocked.ok, false);
    assert.equal(blocked.blocked_submit, true);
    assert.equal(blocked.submit_detection_basis, "contextual");
    assert.equal(blocked.submit_detection.tag, "button");
    assert.equal(blocked.submit_detection.resolved_type, "button");
    assert.equal(blocked.submit_detection.inside_form, false);
    assert.equal(clicked, 0);

    const executed = fn(160, 80, false, true);
    assert.equal(executed.ok, true);
    assert.equal(executed.guarded_intermediate_revalidated, true);
    assert.equal(executed.submit_detection_basis, "contextual");
    assert.equal(clicked, 1);
  } finally {
    dom.window.close();
  }
});

test("guarded intermediate capability never bypasses a native submit control", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <form>
      <input><input><input>
      <button data-hit type="submit" id="submit">Submit</button>
    </form>
  `);
  try {
    dom.window.HTMLFormElement.prototype.requestSubmit = function () {};
    const button = dom.window.document.querySelector("#submit");
    button.innerText = "Submit";
    button.getBoundingClientRect = () => ({ left: 40, top: 20, right: 140, bottom: 60, width: 100, height: 40 });
    let clicked = 0;
    button.addEventListener("click", (event) => { event.preventDefault(); clicked += 1; });

    const result = fn(160, 80, false, true);

    assert.equal(result.ok, false);
    assert.equal(result.blocked_submit, true);
    assert.equal(result.submit_detection_basis, "native_submit");
    assert.equal(clicked, 0);
  } finally {
    dom.window.close();
  }
});

test("explicit submit controls remain blocked even when they are not form-associated", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <button data-hit type="submit" id="commit">Continue</button>
  `);
  try {
    const button = dom.window.document.querySelector("#commit");
    button.innerText = "Continue";
    button.getBoundingClientRect = () => ({ left: 40, top: 20, right: 140, bottom: 60, width: 100, height: 40 });
    let clicked = 0;
    button.addEventListener("click", (event) => { event.preventDefault(); clicked += 1; });

    const result = fn(160, 80, false, true);

    assert.equal(result.ok, false);
    assert.equal(result.blocked_submit, true);
    assert.equal(result.submit_detection_basis, "native_submit");
    assert.equal(result.submit_detection.inside_form, false);
    assert.equal(clicked, 0);
  } finally {
    dom.window.close();
  }
});

test("guarded intermediate capability does not execute a contextual control inside a form", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <form id="application">
      <input><input><input>
      <button data-hit type="button" id="apply">Apply</button>
    </form>
  `);
  try {
    const button = dom.window.document.querySelector("#apply");
    button.innerText = "Apply";
    button.getBoundingClientRect = () => ({ left: 40, top: 20, right: 140, bottom: 60, width: 100, height: 40 });
    let clicked = 0;
    button.addEventListener("click", (event) => { event.preventDefault(); clicked += 1; });

    const result = fn(160, 80, false, true);

    assert.equal(result.ok, false);
    assert.equal(result.blocked_submit, true);
    assert.equal(result.submit_detection_basis, "contextual");
    assert.equal(result.submit_detection.inside_form, true);
    assert.equal(result.submit_detection.form_owner, "application");
    assert.equal(clicked, 0);
  } finally {
    dom.window.close();
  }
});

test("blocked final submit captures the unique enclosing button instead of its inner span", () => {
  const { dom, fn, pointFn } = loadInjectedClickWindow(`
    <form>
      <input><input><input>
      <div>
        <button type="submit" name="final-action"><span><span><span><span><span><span data-hit>Submit</span></span></span></span></span></span></button>
        <button type="submit" name="final-action">Submit another</button>
      </div>
    </form>
  `);
  try {
    const inner = dom.window.document.querySelector("[data-hit]");
    const wrapper = inner.parentElement;
    const button = inner.closest("button");
    Object.defineProperty(inner, "innerText", { value: "Submit", configurable: true });
    Object.defineProperty(wrapper, "innerText", { value: "Submit", configurable: true });
    Object.defineProperty(button, "innerText", { value: "Submit", configurable: true });
    inner.getBoundingClientRect = () => ({ left: 40, top: 20, right: 140, bottom: 60, width: 100, height: 40 });

    for (const result of [fn(160, 80), pointFn(160, 80)]) {
      assert.equal(result.ok, false);
      assert.equal(result.blocked_submit, true);
      assert.equal(result.label, "Submit");
      assert.equal(dom.window.document.querySelector(result.selector), button);
      assert.equal(dom.window.document.querySelectorAll(result.selector).length, 1);
      assert.notEqual(result.selector, 'button[name="final-action"]');
    }
  } finally {
    dom.window.close();
  }
});

test("vision click injected function allows one explicit final submit bypass", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <form>
      <input><input><input>
      <button data-hit type="submit" id="submit">Submit</button>
    </form>
  `);
  try {
    dom.window.HTMLFormElement.prototype.requestSubmit = function () {};
    const button = dom.window.document.querySelector("#submit");
    button.innerText = "Submit";
    button.getBoundingClientRect = () => ({ left: 40, top: 20, right: 140, bottom: 60, width: 100, height: 40 });
    let clicked = 0;
    button.addEventListener("click", (event) => { event.preventDefault(); clicked += 1; });

    const result = fn(160, 80, true);

    assert.equal(result.ok, true);
    assert.equal(result.action, "click");
    assert.equal(result.final_submit_revalidated, true);
    assert.equal(result.final_submit_selector, "#submit");
    assert.equal(clicked, 1);
  } finally {
    dom.window.close();
  }
});

test("vision click refuses an explicit final-submit bypass when the live target is no longer final", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <form>
      <input><input><input>
      <button data-hit type="button" id="continue">Continue</button>
    </form>
  `);
  try {
    const button = dom.window.document.querySelector("#continue");
    button.innerText = "Continue";
    button.getBoundingClientRect = () => ({ left: 40, top: 20, right: 140, bottom: 60, width: 100, height: 40 });
    let clicked = 0;
    button.addEventListener("click", () => { clicked += 1; });

    const result = fn(160, 80, true);

    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "final_submit_target_mismatch");
    assert.equal(result.selector, "#continue");
    assert.equal(clicked, 0);
  } finally {
    dom.window.close();
  }
});

test("raw selected-state transition commits without an option role classifier", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <div data-hit id="choice" role="radio" aria-checked="false">Approved answer</div>
  `);
  try {
    const choice = dom.window.document.querySelector("#choice");
    choice.getBoundingClientRect = () => ({ left: 20, top: 10, right: 260, bottom: 50, width: 240, height: 40 });
    choice.addEventListener("click", () => choice.setAttribute("aria-checked", "true"));

    const clickResult = fn(200, 60);
    const outcomeFn = dom.window.eval(`(${extractFunctionSource("clickOutcomeEvidenceInPage")})`);
    const outcome = outcomeFn(clickResult);

    assert.equal(clickResult.target_option_like, false);
    assert.equal(clickResult.target_selected_before, false);
    assert.equal(outcome.target_selected_after, true);
    assert.equal(outcome.selection_evidence, "selected_state_changed");
    assert.equal(outcome.selection_committed, true);
    assert.equal(outcome.selected_value, "Approved answer");
  } finally {
    dom.window.close();
  }
});

test("option click reports committed autocomplete value from generic read-back", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <input id="city" role="combobox" aria-controls="city-options" aria-invalid="true" value="Jersey City, New Jersey, USA">
    <ul id="city-options" role="listbox"><li data-hit id="city-option" role="option">Jersey City, New Jersey, USA</li></ul>
  `);
  try {
    const input = dom.window.document.querySelector("#city");
    const menu = dom.window.document.querySelector("#city-options");
    const option = dom.window.document.querySelector("#city-option");
    input.focus();
    input.getBoundingClientRect = () => ({ left: 20, top: 10, right: 260, bottom: 50, width: 240, height: 40 });
    menu.getBoundingClientRect = () => ({ left: 20, top: 55, right: 300, bottom: 120, width: 280, height: 65 });
    option.getBoundingClientRect = () => ({ left: 30, top: 65, right: 290, bottom: 105, width: 260, height: 40 });
    option.addEventListener("click", () => {
      option.setAttribute("aria-selected", "true");
      input.setAttribute("aria-invalid", "false");
      menu.style.display = "none";
    });

    const clickResult = fn(200, 160);
    const outcomeFn = dom.window.eval(`(${extractFunctionSource("clickOutcomeEvidenceInPage")})`);
    const outcome = outcomeFn(clickResult);

    assert.equal(clickResult.target_option_like, true);
    assert.equal(outcome.selection_committed, true);
    assert.equal(outcome.selected_value, "Jersey City, New Jersey, USA");
    assert.ok(["selected_state_changed", "menu_closed", "validity_improved"].includes(outcome.selection_evidence));
  } finally {
    dom.window.close();
  }
});

test("option click verifies a non-input combobox through its accessibility-linked owner", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <div data-hit id="status" role="combobox" tabindex="0" aria-controls="status-options">Select...</div>
    <ul id="status-options" role="listbox"><li data-hit id="status-no" role="option">No, I don't have a disability</li></ul>
  `);
  try {
    const owner = dom.window.document.querySelector("#status");
    const menu = dom.window.document.querySelector("#status-options");
    const option = dom.window.document.querySelector("#status-no");
    owner.focus();
    owner.getBoundingClientRect = () => ({ left: 20, top: 10, right: 320, bottom: 50, width: 300, height: 40 });
    menu.getBoundingClientRect = () => ({ left: 20, top: 55, right: 340, bottom: 120, width: 320, height: 65 });
    option.getBoundingClientRect = () => ({ left: 30, top: 65, right: 330, bottom: 105, width: 300, height: 40 });
    option.addEventListener("click", () => {
      owner.textContent = option.textContent;
      menu.style.display = "none";
    });

    const clickResult = fn(200, 160);
    const outcomeFn = dom.window.eval(`(${extractFunctionSource("clickOutcomeEvidenceInPage")})`);
    const outcome = outcomeFn(clickResult);

    assert.equal(clickResult.owner_selector, "#status");
    assert.equal(clickResult.owner_value_before, "Select...");
    assert.equal(outcome.owner_value_after, "No, I don't have a disability");
    assert.equal(outcome.owner_readback_changed, true);
    assert.equal(outcome.owner_matches_target_after, true);
    assert.equal(outcome.target_selected_after, true);
    assert.equal(outcome.selection_committed, true);
    assert.equal(outcome.selected_value, "No, I don't have a disability");
  } finally {
    dom.window.close();
  }
});

test("closing an option surface without owner read-back remains inconclusive", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <div data-hit id="status" role="combobox" tabindex="0" aria-controls="status-options">Select...</div>
    <ul id="status-options" role="listbox"><li data-hit id="status-no" role="option">No</li></ul>
  `);
  try {
    const owner = dom.window.document.querySelector("#status");
    const menu = dom.window.document.querySelector("#status-options");
    const option = dom.window.document.querySelector("#status-no");
    owner.focus();
    owner.getBoundingClientRect = () => ({ left: 20, top: 10, right: 320, bottom: 50, width: 300, height: 40 });
    menu.getBoundingClientRect = () => ({ left: 20, top: 55, right: 340, bottom: 120, width: 320, height: 65 });
    option.getBoundingClientRect = () => ({ left: 30, top: 65, right: 330, bottom: 105, width: 300, height: 40 });
    option.addEventListener("click", () => { menu.style.display = "none"; });

    const clickResult = fn(200, 160);
    const outcomeFn = dom.window.eval(`(${extractFunctionSource("clickOutcomeEvidenceInPage")})`);
    const outcome = outcomeFn(clickResult);

    assert.equal(outcome.option_menu_closed, true);
    assert.equal(outcome.owner_value_after, "Select...");
    assert.equal(outcome.owner_matches_target_after, false);
    assert.equal(outcome.selection_committed, false);
  } finally {
    dom.window.close();
  }
});

test("option click cannot borrow an unrelated focused input value as choice read-back", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <input id="unrelated-value" type="tel" value="5550100142">
    <ul id="choices" role="listbox"><li data-hit id="choice" role="option">Desired choice</li></ul>
  `);
  try {
    const unrelated = dom.window.document.querySelector("#unrelated-value");
    const menu = dom.window.document.querySelector("#choices");
    const option = dom.window.document.querySelector("#choice");
    unrelated.focus();
    unrelated.getBoundingClientRect = () => ({ left: 320, top: 10, right: 560, bottom: 50, width: 240, height: 40 });
    menu.getBoundingClientRect = () => ({ left: 20, top: 55, right: 300, bottom: 120, width: 280, height: 65 });
    option.getBoundingClientRect = () => ({ left: 30, top: 65, right: 290, bottom: 105, width: 260, height: 40 });
    option.addEventListener("click", () => { menu.style.display = "none"; });

    const clickResult = fn(200, 160);
    const outcomeFn = dom.window.eval(`(${extractFunctionSource("clickOutcomeEvidenceInPage")})`);
    const outcome = outcomeFn(clickResult);

    assert.equal(clickResult.target_option_like, true);
    assert.equal(clickResult.owner_selector, "");
    assert.equal(outcome.active_value, "5550100142");
    assert.equal(outcome.option_menu_closed, true);
    assert.equal(outcome.selection_committed, false);
    assert.equal(outcome.selected_value, "");
  } finally {
    dom.window.close();
  }
});

test("vision click still refuses file dialog targets with final submit bypass", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <label data-hit id="resume-drop" for="resume-file">Upload resume</label>
    <input id="resume-file" type="file">
  `);
  try {
    const label = dom.window.document.querySelector("#resume-drop");
    label.innerText = "Upload resume";
    label.getBoundingClientRect = () => ({ left: 40, top: 20, right: 220, bottom: 80, width: 180, height: 60 });
    let clicked = 0;
    label.addEventListener("click", () => { clicked += 1; });

    const result = fn(160, 80, true);

    assert.equal(result.ok, false);
    assert.equal(result.blocked_file_dialog, true);
    assert.equal(clicked, 0);
  } finally {
    dom.window.close();
  }
});

test("upload resolver with control selector uses associated input only", () => {
  const { dom, fn } = loadInjectedUploadResolverWindow(`
    <div id="resume-drop">
      <label for="resume-file">Resume</label>
      <input id="resume-file" type="file">
    </div>
    <div id="cover-drop">Cover letter <input id="cover-file" type="file"></div>
  `);
  try {
    const result = fn({
      mode: "selector",
      selector: "#cover-drop",
      expectedTarget: {
        target_selector: "#cover-drop",
        target_text: "Cover letter",
        target_tag: "div",
        target_type: "",
        target_role: "",
        dispatch_path: "dom_activation",
        page_url: "https://ats.rippling.com/capacity/jobs/example",
      },
    });
    const target = dom.window.document.querySelector(result.selector);

    assert.equal(result.ok, true);
    assert.equal(target.id, "cover-file");
    assert.equal(result.target_label, "Cover letter");
    assert.equal(dom.window.document.querySelector("#resume-file").hasAttribute("data-lumen-vision-upload-target"), false);
  } finally {
    dom.window.close();
  }
});

test("coordinate upload without associated input is ambiguous and does not choose sibling input", () => {
  const { dom, fn } = loadInjectedUploadResolverWindow(`
    <div id="resume-drop">
      <label for="resume-file">Resume</label>
      <input id="resume-file" type="file">
    </div>
    <div data-hit id="cover-drop"><span>Cover letter</span></div>
  `);
  try {
    const cover = dom.window.document.querySelector("#cover-drop");
    cover.getBoundingClientRect = () => ({ left: 40, top: 20, right: 220, bottom: 90, width: 180, height: 70 });

    const result = fn({ mode: "point", imgX: 160, imgY: 80 });

    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "upload_target_ambiguous");
    assert.equal(dom.window.document.querySelector("#resume-file").hasAttribute("data-lumen-vision-upload-target"), false);
  } finally {
    dom.window.close();
  }
});

test("vision_type_control focuses selector, types, and reports active identity", async () => {
  const { dom, fn, scrolled } = loadInjectedTypeControlWindow(`
    <label for="phone">Phone number</label>
    <input id="phone" name="phone" placeholder="Phone" value="prefilled-old-value">
    <input id="location" name="location" value="">
  `);
  try {
    const phone = dom.window.document.querySelector("#phone");
    phone.getBoundingClientRect = () => ({ left: 40, top: 20, right: 240, bottom: 60, width: 200, height: 40 });

    const result = await fn(1, "#phone", "555-123-4567");

    assert.equal(result.ok, true);
    assert.equal(result.mode, "control_selector");
    assert.equal(result.control_selector_resolved, true);
    assert.equal(result.active_selector, "#phone");
    assert.equal(result.active_label, "Phone number");
    assert.equal(result.active_name, "phone");
    assert.equal(phone.value, "555-123-4567");
    // Fully visible already: focusing it must not move the page under the model's other points.
    assert.equal(scrolled.length, 0);
  } finally {
    dom.window.close();
  }
});

test("vision_type_control sets a native select to the option whose visible text is typed, and reads it back", async () => {
  const { dom, fn } = loadInjectedTypeControlWindow(`
    <label for="region">Region</label>
    <select id="region"><option>Select a region</option><option value="ab">Alberta</option><option value="on">Ontario</option></select>
  `);
  try {
    const select = dom.window.document.querySelector("#region");
    const changes = [];
    select.addEventListener("change", () => changes.push(select.value));

    const result = await fn(1, "#region", "Ontario");

    assert.equal(result.ok, true);
    assert.equal(result.active_tag, "select");
    assert.equal(result.active_value, "Ontario");
    assert.equal(result.active_value_matches_expected, true);
    assert.equal(select.value, "on");
    assert.deepEqual(changes, ["on"]);
  } finally {
    dom.window.close();
  }
});

test("vision_type_control names the identity field that refused it and types nothing", async () => {
  const { dom, fn } = loadInjectedTypeControlWindow(`
    <label for="first_name">First Name *</label>
    <input id="first_name" type="text" aria-label="First Name" autocomplete="given-name">
  `);
  try {
    const input = dom.window.document.querySelector("#first_name");
    const result = await fn(1, "#first_name", "Jordan", {
      target_selector: "#first_name",
      target_text: "Last Name *",
      target_tag: "input",
      target_type: "text",
      target_role: "",
      dispatch_path: "dom_activation",
      page_url: dom.window.location.href,
      target_names: ["Last Name *"],
    }, { url: dom.window.location.href });

    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "control_target_drift");
    assert.equal(result.target_identity_mismatch_field, "target_text");
    assert.equal(input.value, "");
  } finally {
    dom.window.close();
  }
});

test("vision_type_control types into a field that already holds focus when its observed identity matches", async () => {
  // Run 1795: the observation listed "First Name *" focused; the label's text carries the asterisk
  // without a space ("First Name*") in the live DOM, and the observer's aliases name the same field.
  const { dom, fn } = loadInjectedTypeControlWindow(`
    <label for="first_name">First Name<span>*</span></label>
    <input id="first_name" type="text" aria-label="First Name" autocomplete="given-name">
  `);
  try {
    const input = dom.window.document.querySelector("#first_name");
    input.focus();
    const result = await fn(1, "#first_name", "Jordan", {
      target_selector: "#first_name",
      target_text: "First Name *",
      target_tag: "input",
      target_type: "text",
      target_role: "",
      dispatch_path: "dom_activation",
      page_url: dom.window.location.href,
      target_names: ["First Name *", "First Name"],
    }, { url: dom.window.location.href });

    assert.equal(result.ok, true);
    assert.equal(result.active_typable, true);
    assert.equal(input.value, "Jordan");
  } finally {
    dom.window.close();
  }
});

test("vision_type_control never guesses a native select option and returns the real options", async () => {
  const { dom, fn } = loadInjectedTypeControlWindow(`
    <label for="region">Region</label>
    <select id="region"><option>Select a region</option><option>Ontario</option><option>Quebec</option></select>
  `);
  try {
    const select = dom.window.document.querySelector("#region");

    const result = await fn(1, "#region", "Ont");

    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "select_option_not_found");
    assert.deepEqual(Array.from(result.select_options), ["Select a region", "Ontario", "Quebec"]);
    assert.equal(select.selectedIndex, 0);
  } finally {
    dom.window.close();
  }
});

test("page evidence lists a native select's option texts in order, and only a multi-select's selected values", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <label for="sponsor">Do you require sponsorship?</label>
    <select id="sponsor"><option>— Make a Selection —</option><option> Yes </option><option>No</option></select>
    <label for="langs">Languages</label>
    <select id="langs" multiple><option selected>Python</option><option>Go</option></select>
  `);
  try {
    const rows = fn().controls;
    const sponsor = rows.find((row) => row.tag === "select" && row.id === "sponsor");
    const langs = rows.find((row) => row.tag === "select" && row.id === "langs");
    assert.ok(sponsor, "single select row present");
    assert.deepEqual(Array.from(sponsor.options), ["— Make a Selection —", "Yes", "No"]);
    assert.equal("selected_values" in sponsor, false);
    assert.deepEqual(Array.from(langs.selected_values), ["Python"]);
    assert.deepEqual(Array.from(langs.options), ["Python", "Go"]);
  } finally {
    dom.window.close();
  }
});

test("vision_type_control refuses a read-only field instead of writing a value no keyboard could", async () => {
  const { dom, fn } = loadInjectedTypeControlWindow(`
    <label for="start">Start date</label>
    <input id="start" readonly placeholder="Pick a date" value="">
  `);
  try {
    const input = dom.window.document.querySelector("#start");
    let clicks = 0;
    input.addEventListener("click", () => { clicks += 1; });

    const result = await fn(1, "#start", "2026-09-14");

    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "control_readonly");
    assert.equal(input.value, "");
    assert.equal(clicks, 0);
  } finally {
    dom.window.close();
  }
});

test("credential replay redacts ordinary identifier values from browser receipts", async () => {
  const before = "old-account@example.test";
  const after = "new-account@example.test";
  const { dom, fn } = loadInjectedTypeControlWindow(
    `<label for="account">Account email</label><input id="account" type="email" value="${before}">`,
  );
  try {
    const result = await fn(3, "#account", after, null, null, true);
    const serialized = JSON.stringify(result);

    assert.equal(result.ok, true);
    assert.equal(result.active_value_matches_expected, true);
    assert.equal(result.active_value_redacted, true);
    assert.equal(result.active_value_redaction_reason, "credential_replay");
    assert.equal(result.active_value, undefined);
    assert.equal(result.before_value, undefined);
    assert.doesNotMatch(serialized, new RegExp(`${before}|${after}`));
    assert.equal(dom.window.document.querySelector("#account").value, after);
  } finally {
    dom.window.close();
  }
});

test("credential replay command allowlist excludes page-controlled secret metadata", async () => {
  const credential = "mirrored-secret-value";
  const escaped = credential.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const { dom, fn } = loadInjectedTypeControlWindow(
    `<input
      id="account"
      type="text"
      name="${credential}"
      aria-label="${credential}"
      aria-invalid="${credential}"
      value=""
    >`,
  );
  try {
    const result = await fn(5, "#account", credential, null, null, true);
    const serialized = JSON.stringify(result);

    assert.equal(result.ok, true);
    assert.equal(result.active_value_matches_expected, true);
    assert.equal(result.active_value_length, credential.length);
    assert.equal(result.active_aria_invalid, false);
    assert.equal(result.active_name, undefined);
    assert.equal(result.active_label, undefined);
    assert.equal(result.active_selector, undefined);
    assert.equal(result.page_url, undefined);
    assert.equal(result.document_lifecycle_token, undefined);
    assert.doesNotMatch(serialized, new RegExp(escaped));
  } finally {
    dom.window.close();
  }
});

test("credential replay never writes after an onfocus cross-origin navigation", async () => {
  const before = "existing-account@example.test";
  const credential = "vault-account@example.test";
  const { dom, fn } = loadInjectedTypeControlWindow(
    `<input id="account" type="email" aria-label="Account email" value="${before}">`,
  );
  try {
    const account = dom.window.document.querySelector("#account");
    account.addEventListener("focus", () => {
      dom.reconfigure({ url: "https://attacker.example/sign-in" });
    }, { once: true });

    const result = await fn(9, "#account", credential, null, null, true);
    const serialized = JSON.stringify(result);

    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "credential_replay_page_context_drift");
    assert.equal(result.page_context_mismatch_field, "url");
    assert.equal(result.url_changed, true);
    assert.equal(result.focus_attempted, true);
    assert.equal(result.value_write_attempted, false);
    assert.equal(account.value, before);
    assert.doesNotMatch(serialized, new RegExp(`${before}|${credential}`));
  } finally {
    dom.window.close();
  }
});

test("credential replay never writes after an onfocus same-selector document target swap", async () => {
  const credential = "vault-password-never-written";
  const { dom, fn } = loadInjectedTypeControlWindow(
    '<input id="password" type="password" aria-label="Password" value="">',
  );
  try {
    const original = dom.window.document.querySelector("#password");
    let replacement = null;
    original.addEventListener("focus", () => {
      replacement = original.cloneNode(true);
      original.replaceWith(replacement);
      replacement.focus();
    }, { once: true });

    const result = await fn(11, "#password", credential, null, null, true);
    const serialized = JSON.stringify(result);

    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "credential_replay_page_context_drift");
    assert.equal(result.page_context_mismatch_field, "document_lifecycle_token");
    assert.equal(result.document_lifecycle_changed, true);
    assert.equal(result.focus_attempted, true);
    assert.equal(result.value_write_attempted, false);
    assert.equal(original.value, "");
    assert.equal(replacement.value, "");
    assert.doesNotMatch(serialized, new RegExp(credential));
  } finally {
    dom.window.close();
  }
});

for (const fixture of [
  { name: "password", attributes: 'type="password"', before: "old-password-secret", after: "new-password-secret" },
  { name: "one-time code", attributes: 'type="text" autocomplete="one-time-code"', before: "123456", after: "654321" },
  { name: "card", attributes: 'type="text" autocomplete="section-checkout billing cc-number"', before: "4111111111111111", after: "5555555555554444" },
  { name: "WebAuthn", attributes: 'type="text" autocomplete="username webauthn"', before: "old-passkey-secret", after: "new-passkey-secret" },
]) {
  test(`vision_type_control verifies a ${fixture.name} locally without serializing its value`, async () => {
    const { dom, fn } = loadInjectedTypeControlWindow(
      `<input id="sensitive" ${fixture.attributes} value="${fixture.before}">`,
    );
    try {
      const result = await fn(7, "#sensitive", fixture.after);
      const serialized = JSON.stringify(result);

      assert.equal(result.ok, true);
      assert.equal(result.active_typable, true);
      assert.equal(result.active_has_value, true);
      assert.equal(result.active_value_matches_expected, true);
      assert.equal(result.active_value_redacted, true);
      assert.equal(result.active_value_redaction_reason, "sensitive_input_structure");
      assert.equal(result.active_value, undefined);
      assert.equal(result.active_value_ledger, undefined);
      assert.doesNotMatch(serialized, new RegExp(`${fixture.before}|${fixture.after}`));
    } finally {
      dom.window.close();
    }
  });
}

test("selectorless coordinate click refuses changed captured pixels before activation", async () => {
  const runtime = loadSingleActivationClickRuntime(
    '<canvas data-hit id="canvas-target"></canvas>',
    { pixelDrift: true },
  );
  try {
    const result = await runtime.runVisionClick(7, 100, 60);

    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "coordinate_pixel_drift");
    assert.equal(result.coordinate_pixel_capture_attempts, 3);
    assert.equal(result.coordinate_pixel_temporal_retry_attempted, true);
    assert.equal(result.coordinate_pixel_temporal_exact_hash_recovered, false);
    assert.equal(result.activation_attempted, false);
    assert.equal(result.activation_count, 0);
    assert.equal(runtime.clickInvocations(), 0);
    assert.equal(runtime.attachCalls(), 0);
    assert.equal(runtime.dispatches.length, 0);
  } finally {
    runtime.dom.window.close();
  }
});

test("selectorless coordinate click dispatches exactly once after a later frame exactly recovers its pixel gate", async () => {
  const runtime = loadSingleActivationClickRuntime(
    '<canvas data-hit id="canvas-target"></canvas>',
    {
      pixelVerdict: {
        ok: true,
        expected_screenshot_sha256: "observed-pixels",
        live_screenshot_sha256: "observed-pixels",
        capture_attempts: 2,
        temporal_exact_hash_retry_attempted: true,
        temporal_exact_hash_recovered: true,
      },
    },
  );
  try {
    const result = await runtime.runVisionClick(7, 100, 60);

    assert.equal(result.ok, true);
    assert.equal(result.mode, "cdp_trusted_click");
    assert.equal(result.coordinate_pixel_capture_attempts, 2);
    assert.equal(result.coordinate_pixel_temporal_retry_attempted, true);
    assert.equal(result.coordinate_pixel_temporal_exact_hash_recovered, true);
    assert.equal(result.activation_attempted, true);
    assert.equal(result.activation_dispatched, true);
    assert.equal(result.activation_count, 1);
    assert.equal(runtime.clickInvocations(), 1);
    assert.equal(runtime.attachCalls(), 1);
    assert.equal(runtime.detachCalls(), 1);
    assert.deepEqual(
      runtime.dispatches
        .filter((item) => item.method === "Input.dispatchMouseEvent")
        .map((item) => item.params.type),
      ["mousePressed", "mouseReleased"],
    );
    assert.equal(
      runtime.dispatches.filter((item) => item.params.type === "mouseReleased").length,
      1,
    );
  } finally {
    runtime.dom.window.close();
  }
});

test("click evidence keeps sensitive active and linked-owner values local", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <input id="credential-owner" role="combobox" autocomplete="username webauthn" aria-controls="credential-menu" value="credential-before-secret">
    <ul id="credential-menu" role="listbox">
      <li id="credential-option" role="option" data-hit>Use saved credential</li>
    </ul>
  `);
  try {
    const owner = dom.window.document.querySelector("#credential-owner");
    const option = dom.window.document.querySelector("#credential-option");
    Object.defineProperty(option, "innerText", { value: "Use saved credential", configurable: true });
    option.getBoundingClientRect = () => ({ left: 40, top: 20, right: 240, bottom: 70, width: 200, height: 50 });
    owner.focus();
    option.addEventListener("click", () => {
      owner.value = "Use saved credential";
      option.setAttribute("aria-selected", "true");
    });

    const clickResult = fn(200, 80);
    const outcomeFn = dom.window.eval(`(${extractFunctionSource("clickOutcomeEvidenceInPage")})`);
    const outcome = outcomeFn(clickResult);
    const serialized = JSON.stringify({ clickResult, outcome });

    assert.equal(clickResult.ok, true);
    assert.equal(clickResult.active_value_before, "[REDACTED]");
    assert.equal(clickResult.active_value_redacted, true);
    assert.equal(clickResult.owner_value_before, "[REDACTED]");
    assert.equal(clickResult.owner_value_redacted, true);
    assert.equal(outcome.active_has_value, true);
    assert.equal(outcome.active_value, "[REDACTED]");
    assert.equal(outcome.owner_value_before, "[REDACTED]");
    assert.equal(outcome.owner_value_after, "[REDACTED]");
    assert.equal(outcome.owner_matches_target_after, true);
    assert.equal(outcome.selection_committed, true);
    assert.equal(outcome.selected_value, "[REDACTED]");
    assert.equal(outcome.selected_value_redacted, true);
    assert.doesNotMatch(serialized, /credential-before-secret/);
  } finally {
    dom.window.close();
  }
});

test("vision_click_control resolves selector and clicks element center without scrolling a visible control", async () => {
  const { dom, fn, calls, scrolled } = loadInjectedControlClickWindow(`<button data-hit id="next">Next</button>`);
  try {
    const button = dom.window.document.querySelector("#next");
    button.innerText = "Next";
    button.getBoundingClientRect = () => ({ left: 40, top: 20, right: 140, bottom: 60, width: 100, height: 40 });
    let clicked = 0;
    button.addEventListener("click", () => { clicked += 1; });

    const result = await fn(1, "#next", 0, 0);

    assert.equal(result.ok, true);
    assert.equal(result.mode, "control_selector");
    assert.equal(result.click_mode, "cdp_trusted_click");
    assert.equal(clicked, 1);
    assert.deepEqual(calls, [[180, 80, false]]);
    assert.equal(scrolled.length, 0, "a fully visible control is clicked where it is");
    assert.equal(result.scrolled_into_view, false);
    assert.deepEqual(Array.from(result.scroll_delta_img), [0, 0]);
  } finally {
    dom.window.close();
  }
});

test("vision_click_control keeps a selector-backed button on DOM activation when hit-testing lands on its nested SVG", async () => {
  const runtime = loadInjectedControlClickWindow(`
    <button id="country-toggle" type="button" aria-label="Toggle flyout">
      <svg aria-hidden="true">
        <path data-hit d="M0 0h10v10z"></path>
      </svg>
    </button>
  `);
  try {
    const button = runtime.dom.window.document.querySelector("#country-toggle");
    const path = runtime.dom.window.document.querySelector("path");
    Object.defineProperty(button, "innerText", {
      value: "Toggle flyout",
      configurable: true,
    });
    button.getBoundingClientRect = () => ({
      left: 40,
      top: 20,
      right: 180,
      bottom: 60,
      width: 140,
      height: 40,
    });
    path.getBoundingClientRect = () => ({
      left: 40,
      top: 20,
      right: 180,
      bottom: 60,
      width: 140,
      height: 40,
    });
    let clicked = 0;
    button.addEventListener("click", (event) => {
      event.preventDefault();
      clicked += 1;
    });

    const result = await runtime.fn(1, "#country-toggle", 0, 0);

    assert.equal(result.ok, true);
    assert.equal(result.mode, "control_selector");
    assert.equal(result.dispatch_path, "dom_activation");
    assert.equal(result.target_selector, "#country-toggle");
    assert.equal(result.activation_count, 1);
    assert.equal(clicked, 1);
    assert.deepEqual(runtime.calls, [[220, 80, false]]);
  } finally {
    runtime.dom.window.close();
  }
});

test("ordinary selector click tolerates unrelated surface drift after exact URL and target revalidation", async () => {
  const runtime = loadInjectedControlClickWindow(
    `<button data-hit id="country">Choose country</button>`,
    { liveSurfaceSignature: "live-surface" },
  );
  try {
    const button = runtime.dom.window.document.querySelector("#country");
    button.innerText = "Choose country";
    button.getBoundingClientRect = () => ({
      left: 40,
      top: 20,
      right: 180,
      bottom: 60,
      width: 140,
      height: 40,
    });
    let clicked = 0;
    button.addEventListener("click", () => { clicked += 1; });

    const result = await runtime.fn(
      1,
      "#country",
      0,
      0,
      false,
      false,
      null,
      {
        url: runtime.dom.window.location.href,
        surface_signature: "observed-surface",
      },
    );

    assert.equal(result.ok, true);
    assert.equal(result.reversible_surface_drift_tolerated, true);
    assert.equal(result.reversible_page_context_basis, "exact_url_and_live_target_identity");
    assert.equal(result.observed_surface_signature, "observed-surface");
    assert.equal(result.live_surface_signature, "live-surface");
    assert.equal(runtime.pageContexts.length, 1);
    assert.equal(runtime.pageContexts[0].url, runtime.dom.window.location.href);
    assert.equal(runtime.pageContexts[0].surface_signature, "");
    assert.equal(clicked, 1);
  } finally {
    runtime.dom.window.close();
  }
});

test("ordinary selector click keeps URL drift fail-closed", async () => {
  const runtime = loadInjectedControlClickWindow(
    `<button data-hit id="country">Choose country</button>`,
    { livePageUrl: "https://example.test/replaced" },
  );
  try {
    const button = runtime.dom.window.document.querySelector("#country");
    button.innerText = "Choose country";
    button.getBoundingClientRect = () => ({
      left: 40,
      top: 20,
      right: 180,
      bottom: 60,
      width: 140,
      height: 40,
    });
    let clicked = 0;
    button.addEventListener("click", () => { clicked += 1; });

    const result = await runtime.fn(
      1,
      "#country",
      0,
      0,
      false,
      false,
      null,
      {
        url: runtime.dom.window.location.href,
        surface_signature: "observed-surface",
      },
    );

    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "page_context_drift");
    assert.equal(result.page_context_mismatch_field, "url");
    assert.equal(result.activation_attempted, false);
    assert.equal(clicked, 0);
    assert.deepEqual(runtime.calls, []);
  } finally {
    runtime.dom.window.close();
  }
});

test("stable surface signature ignores geometry but tracks rendered control state", () => {
  const stableSurfaceSignatureFromEvidence = eval(
    `(${extractFunctionSource("stableSurfaceSignatureFromEvidence")})`,
  );
  const base = {
    viewport_text: "Professional answer Submit application",
    controls: [{
      selector: "#submit",
      tag: "button",
      role: "button",
      type: "submit",
      text: "Submit application",
      value: "",
      checked: false,
      x: 100,
      y: 420,
      width: 300,
      height: 48,
    }],
  };
  const geometryOnly = {
    ...base,
    controls: [{ ...base.controls[0], x: 112, y: 408, width: 305 }],
  };
  const changed = {
    ...base,
    controls: [{ ...base.controls[0], text: "Submit purchase" }],
  };

  assert.equal(
    stableSurfaceSignatureFromEvidence(base),
    stableSurfaceSignatureFromEvidence(geometryOnly),
  );
  assert.notEqual(
    stableSurfaceSignatureFromEvidence(base),
    stableSurfaceSignatureFromEvidence(changed),
  );
});

test("document state signature survives viewport changes but tracks raw control state", () => {
  const runtime = loadVisionEvidenceWindow(`
    <button id="answer" class="choice selected" data-y="100">Yes</button>
    <a href="/privacy" data-y="780">Privacy</a>
  `);
  Object.defineProperty(runtime.dom.window, "innerHeight", { value: 500, configurable: true });
  const compactViewport = runtime.fn();
  Object.defineProperty(runtime.dom.window, "innerHeight", { value: 900, configurable: true });
  const tallViewport = runtime.fn();

  assert.notEqual(compactViewport.controls.length, tallViewport.controls.length);
  assert.equal(
    compactViewport.document_state_signature,
    tallViewport.document_state_signature,
  );

  runtime.dom.window.document.querySelector("#answer").className = "choice";
  const changedControlState = runtime.fn();
  assert.notEqual(
    tallViewport.document_state_signature,
    changedControlState.document_state_signature,
  );
});

test("vision evidence keeps one lifecycle token per document and changes it for a replacement document", () => {
  const firstDocument = loadVisionEvidenceWindow(`<input id="name" value="Jordan">`);
  const secondDocument = loadVisionEvidenceWindow(`<input id="name" value="Jordan">`);
  try {
    const firstObservation = firstDocument.fn();
    const repeatedObservation = firstDocument.fn();
    const replacementObservation = secondDocument.fn();

    assert.ok(firstObservation.document_lifecycle_token);
    assert.equal(
      firstObservation.document_lifecycle_token,
      repeatedObservation.document_lifecycle_token,
    );
    assert.notEqual(
      firstObservation.document_lifecycle_token,
      replacementObservation.document_lifecycle_token,
    );
  } finally {
    firstDocument.dom.window.close();
    secondDocument.dom.window.close();
  }
});

test("page-context verdict accepts geometry drift when stable structure matches", async () => {
  const tabPageEvidence = async () => ({
    url: "https://example.test/apply",
    surface_signature: "geometry-shifted",
    stable_surface_signature: "stable-structure",
  });
  const observedPageContextVerdict = eval(`(${extractFunctionSource("observedPageContextVerdict")})`);

  const result = await observedPageContextVerdict(1, {
    url: "https://example.test/apply",
    surface_signature: "approval-geometry",
    stable_surface_signature: "stable-structure",
  });

  assert.equal(result.ok, true);
});

test("page-context verdict rejects a replacement document even when URL and control state match", async () => {
  const tabPageEvidence = async () => ({
    url: "https://example.test/apply",
    stable_surface_signature: "stable-structure",
    document_state_signature: "v1:stable-document-state",
    document_lifecycle_token: "replacement-document",
  });
  const observedPageContextVerdict = eval(`(${extractFunctionSource("observedPageContextVerdict")})`);

  const result = await observedPageContextVerdict(1, {
    url: "https://example.test/apply",
    stable_surface_signature: "stable-structure",
    document_state_signature: "v1:stable-document-state",
    document_lifecycle_token: "approved-document",
  });

  assert.equal(result.ok, false);
  assert.equal(result.mismatch_field, "document_lifecycle_token");
});

test("page-context verdict prefers document state over viewport surface drift", async () => {
  const tabPageEvidence = async () => ({
    url: "https://example.test/apply",
    surface_signature: "resized-geometry",
    stable_surface_signature: "extra-footer-controls",
    document_state_signature: "v1:stable-document-state",
  });
  const observedPageContextVerdict = eval(`(${extractFunctionSource("observedPageContextVerdict")})`);

  const result = await observedPageContextVerdict(1, {
    url: "https://example.test/apply",
    surface_signature: "approval-geometry",
    stable_surface_signature: "approval-visible-controls",
    document_state_signature: "v1:stable-document-state",
  });

  assert.equal(result.ok, true);
});

test("final and guarded selector capabilities keep surface drift fail-closed", async () => {
  for (const authority of [
    {
      allowFinalSubmit: true,
      allowGuardedIntermediate: false,
      failureReason: "final_submit_page_context_drift",
    },
    {
      allowFinalSubmit: false,
      allowGuardedIntermediate: true,
      failureReason: "page_context_drift",
    },
  ]) {
    const runtime = loadInjectedControlClickWindow(
      `<button data-hit id="continue">Continue</button>`,
      { liveSurfaceSignature: "live-surface" },
    );
    try {
      const button = runtime.dom.window.document.querySelector("#continue");
      button.innerText = "Continue";
      button.getBoundingClientRect = () => ({
        left: 40,
        top: 20,
        right: 180,
        bottom: 60,
        width: 140,
        height: 40,
      });
      let clicked = 0;
      button.addEventListener("click", () => { clicked += 1; });

      const result = await runtime.fn(
        1,
        "#continue",
        0,
        0,
        authority.allowFinalSubmit,
        authority.allowGuardedIntermediate,
        null,
        {
          url: runtime.dom.window.location.href,
          surface_signature: "observed-surface",
        },
      );

      assert.equal(result.ok, false);
      assert.equal(result.failure_reason, authority.failureReason);
      assert.equal(result.page_context_mismatch_field, "surface_signature");
      assert.equal(result.activation_attempted, false);
      assert.equal(clicked, 0);
      assert.deepEqual(runtime.calls, []);
    } finally {
      runtime.dom.window.close();
    }
  }
});

test("vision_click_control re-resolves a moved control after scroll settles", async () => {
  const { dom, fn, calls } = loadInjectedControlClickWindow(`<button data-hit id="choice">Desired choice</button>`);
  try {
    const button = dom.window.document.querySelector("#choice");
    button.innerText = "Desired choice";
    let settled = false;
    button.scrollIntoView = () => { settled = true; };
    button.getBoundingClientRect = () => settled
      ? ({ left: 40, top: 120, right: 180, bottom: 160, width: 140, height: 40 })
      : ({ left: 40, top: 900, right: 180, bottom: 940, width: 140, height: 40 });
    let clicked = 0;
    button.addEventListener("click", () => { clicked += 1; });

    const result = await fn(1, "#choice", 220, 1840);

    assert.equal(result.ok, true);
    assert.equal(result.mode, "control_selector");
    assert.equal(clicked, 1);
    assert.deepEqual(calls, [[220, 280, false]]);
  } finally {
    dom.window.close();
  }
});

test("vision_click_control never falls back to stale coordinates after selector resolution", async () => {
  const { dom, fn, calls } = loadInjectedControlClickWindow(`<button data-hit id="choice">Desired choice</button>`);
  try {
    const button = dom.window.document.querySelector("#choice");
    button.innerText = "Desired choice";
    button.getBoundingClientRect = () => ({ left: 40, top: 900, right: 180, bottom: 940, width: 140, height: 40 });
    let clicked = 0;
    button.addEventListener("click", () => { clicked += 1; });

    const result = await fn(1, "#choice", 220, 1840);

    assert.equal(result.ok, false);
    assert.equal(result.mode, "control_selector");
    assert.equal(result.failure_reason, "control_outside_viewport");
    assert.equal(result.selector_was_resolved, true);
    assert.equal(clicked, 0);
    assert.deepEqual(calls, []);
  } finally {
    dom.window.close();
  }
});

test("vision_click_control refuses coordinate fallback when its observed selector is missing", async () => {
  const { dom, fn, calls } = loadInjectedControlClickWindow(`<button data-hit id="fallback">Continue</button>`);
  try {
    const button = dom.window.document.querySelector("#fallback");
    button.innerText = "Continue";
    button.getBoundingClientRect = () => ({ left: 20, top: 10, right: 80, bottom: 50, width: 60, height: 40 });
    let clicked = 0;
    button.addEventListener("click", () => { clicked += 1; });

    // The model cited a stale selector; the control it observed there read "Save draft". What now
    // sits under its coordinates is a different control, so the fallback must refuse.
    const observed = {
      target_selector: "#missing",
      target_text: "Save draft",
      target_tag: "button",
      target_type: "button",
      target_role: "",
      dispatch_path: "dom_activation",
      page_url: dom.window.location.href,
    };
    const result = await fn(1, "#missing", 100, 60, false, false, observed);

    assert.equal(result.ok, false);
    assert.equal(result.mode, "coordinate_fallback");
    assert.equal(result.selector_failure_reason, "control_not_found");
    assert.equal(result.failure_reason, "click_target_drift");
    assert.equal(result.activation_attempted, false);
    assert.equal(clicked, 0);
    assert.deepEqual(calls, [[100, 60, false]]);
  } finally {
    dom.window.close();
  }
});

test("vision_click_control coordinate fallback follows the observed control when only its selector went stale", async () => {
  const { dom, fn } = loadInjectedControlClickWindow(`<button data-hit id="fallback">Continue</button>`);
  try {
    const button = dom.window.document.querySelector("#fallback");
    button.innerText = "Continue";
    button.getBoundingClientRect = () => ({ left: 20, top: 10, right: 80, bottom: 50, width: 60, height: 40 });
    let clicked = 0;
    button.addEventListener("click", () => { clicked += 1; });

    const observed = {
      target_selector: "#missing",
      target_text: "Continue",
      target_tag: "button",
      target_type: "button",
      target_role: "",
      dispatch_path: "dom_activation",
      page_url: dom.window.location.href,
    };
    const result = await fn(1, "#missing", 100, 60, false, false, observed);

    assert.equal(result.ok, true);
    assert.equal(result.mode, "coordinate_fallback");
    assert.equal(result.target_text, "Continue");
    assert.equal(clicked, 1);
  } finally {
    dom.window.close();
  }
});

test("vision_click_control still blocks submit-like targets", async () => {
  const { dom, fn } = loadInjectedControlClickWindow(`
    <form>
      <input><input><input>
      <button data-hit type="submit" id="submit">Submit</button>
    </form>
  `);
  try {
    const button = dom.window.document.querySelector("#submit");
    button.innerText = "Submit";
    button.getBoundingClientRect = () => ({ left: 40, top: 20, right: 140, bottom: 60, width: 100, height: 40 });

    const result = await fn(1, "#submit", 0, 0);

    assert.equal(result.ok, false);
    assert.equal(result.mode, "control_selector");
    assert.equal(result.blocked_submit, true);
    assert.equal(result.selector, "#submit");
  } finally {
    dom.window.close();
  }
});

test("vision_click_control passes explicit final submit bypass to one click", async () => {
  const { dom, fn, calls } = loadInjectedControlClickWindow(`
    <form>
      <input><input><input>
      <button data-hit type="submit" id="submit">Submit</button>
    </form>
  `);
  try {
    dom.window.HTMLFormElement.prototype.requestSubmit = function () {};
    const button = dom.window.document.querySelector("#submit");
    button.innerText = "Submit";
    button.getBoundingClientRect = () => ({ left: 40, top: 20, right: 140, bottom: 60, width: 100, height: 40 });
    let clicked = 0;
    button.addEventListener("click", (event) => { event.preventDefault(); clicked += 1; });

    const result = await fn(1, "#submit", 0, 0, true);

    assert.equal(result.ok, true);
    assert.equal(result.mode, "control_selector");
    assert.equal(clicked, 1);
    assert.deepEqual(calls, [[180, 80, true]]);
  } finally {
    dom.window.close();
  }
});

test("vision_click_control does not coordinate-fallback when an approved final selector is stale", async () => {
  const { dom, fn, calls } = loadInjectedControlClickWindow(`
    <form>
      <input><input><input>
      <button data-hit type="submit" id="replacement-submit">Submit</button>
    </form>
  `);
  try {
    const button = dom.window.document.querySelector("#replacement-submit");
    button.innerText = "Submit";
    button.getBoundingClientRect = () => ({ left: 40, top: 20, right: 140, bottom: 60, width: 100, height: 40 });
    let clicked = 0;
    button.addEventListener("click", () => { clicked += 1; });

    const result = await fn(1, "#stale-submit", 160, 80, true);

    assert.equal(result.ok, false);
    assert.equal(result.mode, "control_selector");
    assert.equal(result.failure_reason, "final_submit_target_mismatch");
    assert.equal(result.control_selector_resolved, false);
    assert.equal(clicked, 0);
    assert.deepEqual(calls, []);
  } finally {
    dom.window.close();
  }
});

test("vision_click_control executes an exact contextual control with guarded intermediate authority", async () => {
  const { dom, fn, calls } = loadInjectedControlClickWindow(`
    <input><input><input>
    <button data-hit type="button" id="start" aria-label="Apply">Apply</button>
  `);
  try {
    const button = dom.window.document.querySelector("#start");
    button.innerText = "Apply";
    button.getBoundingClientRect = () => ({ left: 40, top: 20, right: 140, bottom: 60, width: 100, height: 40 });
    let clicked = 0;
    button.addEventListener("click", (event) => { event.preventDefault(); clicked += 1; });

    const result = await fn(1, "#start", 0, 0, false, true);

    assert.equal(result.ok, true);
    assert.equal(result.mode, "control_selector");
    assert.equal(result.guarded_intermediate_revalidated, true);
    assert.equal(clicked, 1);
    assert.deepEqual(calls, [[180, 80, false, true]]);
  } finally {
    dom.window.close();
  }
});

test("guarded intermediate selector mismatch never falls back to coordinates", async () => {
  const { dom, fn, calls } = loadInjectedControlClickWindow(`
    <input><input><input>
    <button data-hit type="button" id="replacement" aria-label="Apply">Apply</button>
  `);
  try {
    const button = dom.window.document.querySelector("#replacement");
    button.innerText = "Apply";
    button.getBoundingClientRect = () => ({ left: 40, top: 20, right: 140, bottom: 60, width: 100, height: 40 });
    let clicked = 0;
    button.addEventListener("click", () => { clicked += 1; });

    const result = await fn(1, "#stale-start", 160, 80, false, true);

    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "guarded_intermediate_target_mismatch");
    assert.equal(result.control_selector_resolved, false);
    assert.equal(clicked, 0);
    assert.deepEqual(calls, []);
  } finally {
    dom.window.close();
  }
});

test("approved selector execution uses exactly one trusted activation", async () => {
  const runtime = loadPrivilegedFinalClickRuntime(`
    <form>
      <input><input><input>
      <button type="submit" id="submit">Submit application</button>
    </form>
  `);
  try {
    const button = runtime.dom.window.document.querySelector("#submit");
    button.getBoundingClientRect = () => ({ left: 40, top: 20, right: 160, bottom: 70, width: 120, height: 50 });

    const result = await runtime.runVisionClickControl(7, "#submit", 0, 0, true);

    assert.equal(result.ok, true);
    assert.equal(result.mode, "control_selector");
    assert.equal(result.click_mode, "cdp_trusted_click");
    assert.equal(result.final_submit_revalidated, true);
    assert.equal(runtime.pixelRecaptures(), 0);
    assert.equal(runtime.clickInvocations(), 1);
    assert.equal(runtime.dispatches.filter((item) => item.params.type === "mouseReleased").length, 1);
    assert.deepEqual(
      runtime.dispatches.map((item) => item.params.type),
      ["mousePressed", "mouseReleased"],
    );
  } finally {
    runtime.dom.window.close();
  }
});

test("approved final coordinate click without an exact selector identity fails closed", async () => {
  const runtime = loadPrivilegedFinalClickRuntime(`
    <form>
      <input><input><input>
      <button type="submit" id="submit">Submit application</button>
    </form>
  `);
  try {
    const result = await runtime.runVisionClick(7, 100, 45, true);

    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "final_submit_identity_required");
    assert.equal(result.activation_attempted, false);
    assert.equal(result.activation_count, 0);
    assert.equal(runtime.clickInvocations(), 0);
    assert.equal(runtime.dispatches.length, 0);
  } finally {
    runtime.dom.window.close();
  }
});

for (const drift of [
  {
    name: "selector",
    replacement: '<button type="submit" id="replacement-submit">Submit application</button>',
    field: "target_selector",
  },
  {
    name: "rendered text",
    replacement: '<button type="submit" id="submit">Delete application</button>',
    field: "target_text",
  },
  {
    name: "role",
    replacement: '<button type="submit" id="submit" role="switch">Submit application</button>',
    field: "target_role",
  },
  {
    name: "tag",
    replacement: '<input type="submit" id="submit" aria-label="Submit application" value="Submit application">',
    field: "target_tag",
  },
]) {
  test(`approved final click refuses ${drift.name} drift at the immediate activation gate`, async () => {
    const runtime = loadPrivilegedFinalClickRuntime(
      `
        <form>
          <input><input><input>
          <button type="submit" id="submit">Submit application</button>
        </form>
      `,
      { driftAfterFirstTrustedProbeHtml: drift.replacement },
    );
    try {
      const result = await runtime.runVisionClickControl(7, "#submit", 0, 0, true);

      assert.equal(result.ok, false);
      assert.equal(result.failure_reason, "click_target_drift");
      assert.equal(result.target_identity_matched, false);
      assert.equal(result.target_identity_mismatch_field, drift.field);
      assert.equal(result.activation_attempted, false);
      assert.equal(result.activation_dispatched, false);
      assert.equal(result.activation_count, 0);
      assert.equal(runtime.clickInvocations(), 0);
      assert.equal(runtime.dispatches.length, 0);
    } finally {
      runtime.dom.window.close();
    }
  });
}

for (const fixture of [
  {
    name: "canvas coordinate surface",
    html: '<canvas data-hit id="canvas-target"></canvas>',
    basis: "coordinate_rendered_surface",
  },
  {
    name: "SVG coordinate surface",
    html: '<svg data-hit id="svg-target" role="button"><path d="M0 0h10v10z"></path></svg>',
    basis: "coordinate_rendered_surface",
  },
  {
    name: "custom coordinate target",
    html: '<div data-hit id="custom-target"></div>',
    basis: "coordinate_target",
  },
]) {
  test(`${fixture.name} receives one trusted activation and reports inconclusive read-back honestly`, async () => {
    const runtime = loadSingleActivationClickRuntime(fixture.html);
    try {
      const result = await runtime.runVisionClick(7, 100, 60);

      assert.equal(result.ok, true);
      assert.equal(result.mode, "cdp_trusted_click");
      assert.equal(result.dispatch_path_selected, "cdp_trusted_click");
      assert.equal(result.dispatch_basis, fixture.basis);
      assert.equal(result.activation_dispatched, true);
      assert.equal(result.activation_count, 1);
      assert.equal(result.trusted_click_attempted, true);
      assert.equal(result.verification_observed, false);
      assert.equal(result.verification_state, "inconclusive");
      assert.equal(result.visual_verification_required, true);
      assert.equal(runtime.clickInvocations(), 1);
      assert.equal(runtime.attachCalls(), 1);
      assert.equal(runtime.detachCalls(), 1);
      assert.equal(runtime.dispatches.filter((item) => item.params.type === "mouseReleased").length, 1);
    } finally {
      runtime.dom.window.close();
    }
  });
}

test("a click whose press acknowledgement times out reports an unknown outcome, not a plain failure", async () => {
  // The press reached Chrome; a busy page can ack it after the timeout. A plain failure would
  // invite the model to click "Add" or "Next" a second time.
  const runtime = loadSingleActivationClickRuntime(
    '<button data-hit id="add-row">Add</button>',
    { pressAckTimesOut: true },
  );
  try {
    const button = runtime.dom.window.document.querySelector("#add-row");
    Object.defineProperty(button, "innerText", { value: "Add", configurable: true });

    const result = await runtime.runVisionClick(7, 100, 60);

    assert.equal(result.ok, false);
    assert.equal(result.execution_outcome_unknown, true);
    assert.equal(result.failure_reason, "click_execution_outcome_unknown");
    assert.equal(result.activation_dispatched, true);
    assert.equal(result.activation_count, 1);
  } finally {
    runtime.dom.window.close();
  }
});

test("ordinary DOM control receives one trusted user-level activation", async () => {
  const runtime = loadSingleActivationClickRuntime(
    '<button data-hit id="ordinary-control">Continue</button>',
    { mutateSurface: true },
  );
  try {
    const button = runtime.dom.window.document.querySelector("#ordinary-control");
    Object.defineProperty(button, "innerText", { value: "Continue", configurable: true });

    const result = await runtime.runVisionClick(7, 100, 60);

    assert.equal(result.ok, true);
    assert.equal(result.mode, "cdp_trusted_click");
    assert.equal(result.dispatch_path_selected, "cdp_trusted_click");
    assert.equal(result.dispatch_basis, "dom_actionable_target");
    assert.equal(result.activation_dispatched, true);
    assert.equal(result.activation_count, 1);
    assert.equal(result.trusted_click_attempted, true);
    assert.equal(result.surface_changed, true);
    assert.equal(result.verification_state, "observed_effect");
    assert.equal(result.visual_verification_required, false);
    assert.equal(runtime.clickInvocations(), 1);
    assert.equal(runtime.attachCalls(), 1);
    assert.equal(runtime.detachCalls(), 1);
    assert.deepEqual(
      runtime.dispatches
        .filter((item) => item.method === "Input.dispatchMouseEvent")
        .map((item) => item.params.type),
      ["mousePressed", "mouseReleased"],
    );
    assert.equal(
      runtime.dispatches.filter((item) => item.params.type === "mouseReleased").length,
      1,
    );
  } finally {
    runtime.dom.window.close();
  }
});

test("inconclusive ordinary DOM control still receives exactly one trusted activation", async () => {
  const runtime = loadSingleActivationClickRuntime(
    '<button data-hit id="ordinary-control">Continue</button>',
  );
  try {
    const button = runtime.dom.window.document.querySelector("#ordinary-control");
    Object.defineProperty(button, "innerText", { value: "Continue", configurable: true });

    const result = await runtime.runVisionClick(7, 100, 60);

    assert.equal(result.ok, true);
    assert.equal(result.mode, "cdp_trusted_click");
    assert.equal(result.dispatch_path_selected, "cdp_trusted_click");
    assert.equal(result.target_identity_matched, true);
    assert.equal(result.activation_dispatched, true);
    assert.equal(result.activation_count, 1);
    assert.equal(result.trusted_click_attempted, true);
    assert.equal(result.verification_observed, false);
    assert.equal(result.verification_state, "inconclusive");
    assert.equal(result.visual_verification_required, true);
    assert.equal(runtime.clickInvocations(), 1);
    assert.equal(runtime.attachCalls(), 1);
    assert.equal(runtime.detachCalls(), 1);
    assert.equal(
      runtime.dispatches.filter((item) => item.params.type === "mouseReleased").length,
      1,
    );
  } finally {
    runtime.dom.window.close();
  }
});

test("trusted ordinary activation refuses a target that drifted after dispatch preflight", async () => {
  const runtime = loadSingleActivationClickRuntime(
    '<button data-hit id="ordinary-control">Continue</button>',
    {
      driftAfterPreflightHtml: '<button data-hit id="replacement-control">Delete</button>',
    },
  );
  try {
    const result = await runtime.runVisionClick(7, 100, 60);

    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "click_target_drift");
    assert.equal(result.target_identity_matched, false);
    assert.equal(result.preflight_target_selector, "#ordinary-control");
    assert.equal(result.live_target_selector, "#replacement-control");
    assert.equal(result.activation_attempted, false);
    assert.equal(result.activation_dispatched, false);
    assert.equal(result.activation_count, 0);
    assert.equal(runtime.clickInvocations(), 0);
    assert.equal(runtime.attachCalls(), 0);
    assert.equal(runtime.detachCalls(), 0);
    assert.equal(runtime.dispatches.length, 0);
  } finally {
    runtime.dom.window.close();
  }
});

test("trusted coordinate activation refuses a target that drifted after dispatch preflight", async () => {
  const runtime = loadSingleActivationClickRuntime(
    '<canvas data-hit id="canvas-target"></canvas>',
    {
      driftAfterPreflightHtml: '<canvas data-hit id="replacement-canvas"></canvas>',
    },
  );
  try {
    const result = await runtime.runVisionClick(7, 100, 60);

    assert.equal(result.ok, false);
    assert.equal(result.mode, "cdp_trusted_click");
    assert.equal(result.failure_reason, "click_target_drift");
    assert.equal(result.target_identity_matched, false);
    assert.equal(result.preflight_target_selector, "#canvas-target");
    assert.equal(result.live_target_selector, "#replacement-canvas");
    assert.equal(result.activation_attempted, false);
    assert.equal(result.activation_dispatched, false);
    assert.equal(result.activation_count, 0);
    assert.equal(runtime.clickInvocations(), 0);
    assert.equal(runtime.attachCalls(), 0);
    assert.equal(runtime.detachCalls(), 0);
    assert.equal(runtime.dispatches.length, 0);
  } finally {
    runtime.dom.window.close();
  }
});

test("vision click runtime selects one dispatch path before activation and retains surface verification", () => {
  assert.match(BACKGROUND_SRC, /function runTrustedVisionClick/);
  assert.match(BACKGROUND_SRC, /function adoptNewTabAfterClick/);
  assert.match(BACKGROUND_SRC, /Input\.dispatchMouseEvent/);
  assert.match(BACKGROUND_SRC, /surface_changed/);
  assert.match(BACKGROUND_SRC, /dispatch_path_selected/);
  assert.match(BACKGROUND_SRC, /activation_count/);
  assert.match(BACKGROUND_SRC, /adopted_new_tab/);
  assert.match(BACKGROUND_SRC, /active_value_redaction_reason = "sensitive_input_structure"/);
  assert.match(BACKGROUND_SRC, /active_value_matches_expected/);
  assert.doesNotMatch(BACKGROUND_SRC, /active_value_ledger:\s*value\.slice\(0,\s*4000\)/);
  assert.doesNotMatch(BACKGROUND_SRC, /vision_retry_final_submit|resolveFinalSubmitRetryInPage|runVisionRetryFinalSubmit/);
});

test("post-click settle waits for a load only when a navigation actually starts", async () => {
  const listeners = [];
  let tabStatus = "complete";
  const chrome = {
    runtime: { lastError: null },
    tabs: {
      onUpdated: {
        addListener: (fn) => listeners.push(fn),
        removeListener: (fn) => listeners.splice(listeners.indexOf(fn), 1),
      },
      get: (_id, callback) => callback({ status: tabStatus }),
    },
  };
  const waitForLoad = eval(`(${extractFunctionSource("waitForLoad")})`);

  let started = Date.now();
  const inPage = await waitForLoad(7, 1500, { startWindowMs: 60 });
  assert.equal(inPage.navigation_started, false);
  assert.ok(Date.now() - started < 600, "an in-page click does not wait out the load timeout");

  started = Date.now();
  const navigating = waitForLoad(7, 1500, { startWindowMs: 60 });
  listeners.forEach((fn) => fn(7, { status: "loading" }));
  setTimeout(() => listeners.forEach((fn) => fn(7, { status: "complete" })), 150);
  const loaded = await navigating;
  assert.equal(loaded.navigation_started, true);
  assert.ok(Date.now() - started >= 140, "a started navigation is waited for until it loads");
  assert.equal(listeners.length, 0);
});

test("post-click settle returns once the page stops changing, ignoring Lumen's own overlay", async () => {
  const dom = new JSDOM(`<!doctype html><html><body><div id="panel"></div><div data-lumen-pointer-overlay="true" id="lumen-pointer-overlay"></div></body></html>`, {
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  try {
    const domQuiet = dom.window.eval(`(${extractFunctionSource("domQuietInPage")})`);
    const panel = dom.window.document.querySelector("#panel");
    const overlay = dom.window.document.querySelector("#lumen-pointer-overlay");

    const settling = domQuiet(80, 1000);
    setTimeout(() => { panel.textContent = "Menu open"; }, 30);
    const ticker = setInterval(() => { overlay.setAttribute("data-x", String(Date.now())); }, 10);
    const settled = await settling;
    clearInterval(ticker);
    assert.equal(settled.settled, "quiet");
    assert.ok(settled.waited_ms >= 100 && settled.waited_ms < 600);

    const busy = setInterval(() => { panel.textContent = String(Date.now()); }, 20);
    const capped = await domQuiet(80, 300);
    clearInterval(busy);
    assert.equal(capped.settled, "max_wait");
  } finally {
    dom.window.close();
  }
});

function loadVisionDragRuntime({ interceptAfterMoves = 0, probe = null } = {}) {
  // The click path's dry-run point probe (blocked_submit for a final control), stubbed per test.
  const visionClickAtPointInPage = probe || (() => ({ ok: true }));
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async (target, method, params) => { await _cdp(target, method, params); return "dispatched"; };
  const dom = new JSDOM(`<!doctype html><html><body><div id="thumb"></div></body></html>`, {
    url: "https://example.test/drag",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  Object.defineProperty(dom.window, "devicePixelRatio", { value: 2, configurable: true });
  dom.window.document.elementFromPoint = () => dom.window.document.querySelector("#thumb");
  const dispatches = [];
  const listeners = [];
  let moves = 0;
  let surface = "before";
  const chrome = {
    runtime: { lastError: null },
    scripting: {
      executeScript: async ({ func, args }) => {
        if (func === visionClickAtPointInPage) return [{ result: func(...(args || [])) }];
        const pageFunc = dom.window.eval(`(${func.toString()})`);
        return [{ result: pageFunc(...(args || [])) }];
      },
    },
    debugger: {
      attach: (_target, _version, callback) => callback(),
      detach: (_target, callback) => callback(),
      onEvent: {
        addListener: (fn) => listeners.push(fn),
        removeListener: (fn) => { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); },
      },
    },
  };
  const _cdp = async (_target, method, params) => {
    dispatches.push({ method, params });
    if (method === "Input.dispatchMouseEvent" && params.type === "mouseMoved" && params.buttons) {
      moves += 1;
      if (interceptAfterMoves && moves === interceptAfterMoves) {
        listeners.forEach((fn) => fn({ tabId: 7 }, "Input.dragIntercepted", { data: { items: [{ mimeType: "text/plain", data: "chip" }], dragOperationsMask: 1 } }));
      }
    }
    if (method === "Input.dispatchMouseEvent" && params.type === "mouseReleased") surface = "after";
    return {};
  };
  const withTimeout = async (promise) => promise;
  // Stand-ins for the tab debugger session: one acquire/release per primitive is observable
  // as one attach/detach on the stubbed chrome.debugger.
  const acquireDebugger = (target) => new Promise((resolve, reject) => chrome.debugger.attach(target, "1.3", () => (
    chrome.runtime.lastError ? reject(new Error(chrome.runtime.lastError.message)) : resolve({ attached_now: true })
  )));
  const releaseDebugger = (target) => { chrome.debugger.detach(target, () => {}); };
  const errorMessage = (error) => (error && error.message ? String(error.message) : String(error || ""));
  const tabPageEvidence = async () => ({ url: "https://example.test/drag", surface_signature: `surface:${surface}` });
  const animatePointerToPoint = async () => ({ ok: true });
  const domQuietInPage = () => ({ ok: true });
  const pointerPointsInPage = eval(`(${extractFunctionSource("pointerPointsInPage")})`);
  const gestureModifierBits = eval(`(${extractFunctionSource("gestureModifierBits")})`);
  const runVisionDrag = eval(`(${extractFunctionSource("runVisionDrag")})`);
  const runVisionHover = eval(`(${extractFunctionSource("runVisionHover")})`);
  return { dom, dispatches, runVisionDrag, runVisionHover, listeners };
}

test("a drag whose press or release lands on a final control is refused before any press", async () => {
  for (const [name, blockedX] of [["press", 200], ["release", 600]]) {
    const runtime = loadVisionDragRuntime({
      probe: (x) => (x === blockedX ? { ok: false, blocked_submit: true, label: "Send" } : { ok: true }),
    });
    try {
      const result = await runtime.runVisionDrag(7, 200, 100, 600, 100);
      assert.equal(result.ok, false);
      assert.equal(result.failure_reason, "drag_endpoint_final_control");
      assert.equal(result.drag_endpoint, name);
      assert.equal(result.activation_attempted, false);
      assert.equal(runtime.dispatches.filter((item) => item.method === "Input.dispatchMouseEvent").length, 0);
    } finally {
      runtime.dom.window.close();
    }
  }
});

test("vision drag presses, moves in steps, and releases at the destination in CSS pixels", async () => {
  const runtime = loadVisionDragRuntime();
  try {
    const result = await runtime.runVisionDrag(7, 200, 100, 600, 100);
    const mouse = runtime.dispatches.filter((item) => item.method === "Input.dispatchMouseEvent");
    const pressed = mouse.find((item) => item.params.type === "mousePressed");
    const released = mouse.filter((item) => item.params.type === "mouseReleased");
    const heldMoves = mouse.filter((item) => item.params.type === "mouseMoved" && item.params.buttons === 1);

    assert.equal(result.ok, true);
    assert.equal(result.mode, "pointer_drag");
    assert.equal(result.surface_changed, true);
    assert.deepEqual([pressed.params.x, pressed.params.y], [100, 50]);
    assert.ok(heldMoves.length >= 8, "a slider or canvas sees a stream of held mousemoves");
    assert.deepEqual([heldMoves.at(-1).params.x, heldMoves.at(-1).params.y], [300, 50]);
    assert.equal(released.length, 1);
    assert.deepEqual([released[0].params.x, released[0].params.y], [300, 50]);
    assert.equal(runtime.listeners.length, 0);
  } finally {
    runtime.dom.window.close();
  }
});

test("vision drag completes a native drag-and-drop with the drag's own data at the drop point", async () => {
  const runtime = loadVisionDragRuntime({ interceptAfterMoves: 2 });
  try {
    const result = await runtime.runVisionDrag(7, 200, 100, 600, 300);
    const dragEvents = runtime.dispatches.filter((item) => item.method === "Input.dispatchDragEvent");

    assert.equal(result.ok, true);
    assert.equal(result.mode, "native_drag_and_drop");
    assert.equal(result.native_drag_intercepted, true);
    assert.deepEqual(dragEvents.map((item) => item.params.type), ["dragEnter", "dragOver", "drop"]);
    assert.deepEqual([dragEvents[2].params.x, dragEvents[2].params.y], [300, 150]);
    assert.equal(dragEvents[2].params.data.items[0].data, "chip");
    const methods = runtime.dispatches.map((item) => `${item.method}:${(item.params || {}).type || (item.params || {}).enabled}`);
    assert.ok(methods.indexOf("Input.dispatchDragEvent:drop") < methods.indexOf("Input.dispatchMouseEvent:mouseReleased"));
    assert.equal(methods.at(-1), "Input.setInterceptDrags:false");
  } finally {
    runtime.dom.window.close();
  }
});

test("vision hover moves the pointer without pressing", async () => {
  const runtime = loadVisionDragRuntime();
  try {
    const result = await runtime.runVisionHover(7, 400, 200);
    const mouse = runtime.dispatches.filter((item) => item.method === "Input.dispatchMouseEvent");
    assert.equal(result.ok, true);
    assert.deepEqual(mouse.map((item) => item.params.type), ["mouseMoved"]);
    assert.deepEqual([mouse[0].params.x, mouse[0].params.y], [200, 100]);
    assert.equal(result.dwell_ms, 0);
  } finally {
    runtime.dom.window.close();
  }
});

test("vision hover rests the pointer for a delayed tooltip without pressing", async () => {
  const runtime = loadVisionDragRuntime();
  try {
    const started = Date.now();
    const result = await runtime.runVisionHover(7, 400, 200, 120);
    const mouse = runtime.dispatches.filter((item) => item.method === "Input.dispatchMouseEvent");
    assert.equal(result.ok, true);
    assert.equal(result.dwell_ms, 120);
    assert.ok(Date.now() - started >= 110, "the pointer rested before the settle read");
    assert.deepEqual(mouse.map((item) => item.params.type), ["mouseMoved"]);
  } finally {
    runtime.dom.window.close();
  }
});

test("a ctrl-drag holds the modifier on every event and a long-press waits before moving", async () => {
  const runtime = loadVisionDragRuntime();
  try {
    const started = Date.now();
    const result = await runtime.runVisionDrag(7, 200, 100, 600, 100, "left", null, { modifiers: ["ctrl", "shift"], hold_ms: 120 });
    const mouse = runtime.dispatches.filter((item) => item.method === "Input.dispatchMouseEvent");
    const pressedAt = mouse.findIndex((item) => item.params.type === "mousePressed");

    assert.equal(result.ok, true);
    assert.equal(result.drag_modifiers, 2 | 8);
    assert.equal(result.drag_hold_ms, 120);
    assert.ok(mouse.every((item) => item.params.modifiers === (2 | 8)), "ctrl+shift ride on the press, every move, and the release");
    assert.ok(pressedAt >= 0 && mouse[pressedAt + 1].params.type === "mouseMoved");
    assert.ok(Date.now() - started >= 110, "the button stayed pressed in place before the first move");
  } finally {
    runtime.dom.window.close();
  }
});

test("the tab debugger session stays attached across actions and detaches on idle or run end", async () => {
  let attaches = 0;
  let detaches = 0;
  const timers = [];
  const chrome = {
    runtime: { lastError: null },
    debugger: {
      attach: (_target, _version, callback) => { attaches += 1; callback(); },
      detach: (_target, callback) => { detaches += 1; callback(); },
    },
  };
  const setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  const clearTimeout = (id) => { if (timers[id - 1]) timers[id - 1].fn = null; };
  const DEBUGGER_IDLE_DETACH_MS = 45000;
  const debuggerSessions = new Map();
  const acquireDebugger = eval(`(${extractFunctionSource("acquireDebugger")})`);
  const releaseDebugger = eval(`(${extractFunctionSource("releaseDebugger")})`);
  const detachDebuggerNow = eval(`(${extractFunctionSource("detachDebuggerNow")})`);
  const detachAllDebuggerSessions = eval(`(${extractFunctionSource("detachAllDebuggerSessions")})`);

  assert.deepEqual(await acquireDebugger({ tabId: 7 }), { attached_now: true });
  releaseDebugger({ tabId: 7 });
  assert.deepEqual(await acquireDebugger({ tabId: 7 }), { attached_now: false });
  releaseDebugger({ tabId: 7 });
  assert.equal(attaches, 1, "consecutive actions share one attachment (no infobar flicker)");
  assert.equal(detaches, 0, "release defers the detach");
  const idle = timers.filter((timer) => timer.fn);
  assert.equal(idle.length, 1);
  assert.equal(idle[0].ms, 45000);

  await detachAllDebuggerSessions();
  assert.equal(detaches, 1, "the run releasing control ends the session");
  assert.equal(debuggerSessions.size, 0);
});

function loadNativeDialogRuntime() {
  const commands = [];
  let hangNextInput = false;
  const nativeDialogs = new Map();
  const lastObservationByTab = new Map();
  const chrome = { runtime: { lastError: null }, scripting: { executeScript: async () => [{ result: { ok: true } }] } };
  const _cdp = (_target, method, params) => {
    commands.push({ method, params });
    if (hangNextInput && method.startsWith("Input.")) {
      hangNextInput = false;
      // The page's handler opened confirm(): the dispatch never returns while it is open.
      setTimeout(() => nativeDialogs.set(7, { type: "confirm", message: "Archive 3 records?", default_prompt: "", prompt_text: null }), 30);
      return new Promise(() => {});
    }
    return Promise.resolve({});
  };
  const withTimeout = async (promise) => promise;
  const errorMessage = (error) => (error && error.message ? String(error.message) : String(error || ""));
  const acquireDebugger = async () => ({ attached_now: false });
  const releaseDebugger = () => {};
  const domQuietInPage = () => ({ ok: true });
  const publicNativeDialog = eval(`(${extractFunctionSource("publicNativeDialog")})`);
  const nativeDialogOpenFor = eval(`(${extractFunctionSource("nativeDialogOpenFor")})`);
  const dispatchInputUnlessDialog = eval(`(${extractFunctionSource("dispatchInputUnlessDialog")})`);
  const answerNativeDialog = eval(`(${extractFunctionSource("answerNativeDialog")})`);
  const typeIntoNativeDialog = eval(`(${extractFunctionSource("typeIntoNativeDialog")})`);
  const nativeDialogObservation = eval(`(${extractFunctionSource("nativeDialogObservation")})`);
  return {
    commands,
    nativeDialogs,
    lastObservationByTab,
    hangNext: () => { hangNextInput = true; },
    dispatchInputUnlessDialog,
    answerNativeDialog,
    typeIntoNativeDialog,
    nativeDialogObservation,
  };
}

test("an input whose handler opens a browser dialog returns as soon as the dialog opens", async () => {
  const runtime = loadNativeDialogRuntime();
  runtime.hangNext();
  const outcome = await runtime.dispatchInputUnlessDialog({ tabId: 7 }, "Input.dispatchMouseEvent", { type: "mouseReleased" });
  assert.equal(outcome, "dialog");
  assert.equal(await runtime.dispatchInputUnlessDialog({ tabId: 8 }, "Input.dispatchMouseEvent", { type: "mouseMoved" }), "dispatched");
});

test("a browser prompt is answered only by the model's typed text and Enter, or dismissed by Escape", async () => {
  const runtime = loadNativeDialogRuntime();
  runtime.nativeDialogs.set(7, { type: "prompt", message: "Enter the archive code:", default_prompt: "", prompt_text: null });

  const refused = await runtime.answerNativeDialog(7, "Tab");
  assert.equal(refused.ok, false);
  assert.equal(refused.failure_reason, "native_dialog_open");

  const typed = runtime.typeIntoNativeDialog(7, "PX-4821");
  assert.equal(typed.ok, true);
  assert.equal(typed.active_value, "PX-4821");

  const answered = await runtime.answerNativeDialog(7, "Enter");
  assert.equal(answered.ok, true);
  assert.equal(answered.native_dialog_answered.accepted, true);
  const handled = runtime.commands.find((item) => item.method === "Page.handleJavaScriptDialog");
  assert.deepEqual(handled.params, { accept: true, promptText: "PX-4821" });
  assert.equal(runtime.nativeDialogs.has(7), false);

  runtime.nativeDialogs.set(7, { type: "confirm", message: "Delete everything?", default_prompt: "", prompt_text: null });
  assert.equal(runtime.typeIntoNativeDialog(7, "yes").ok, false);
  const dismissed = await runtime.answerNativeDialog(7, "Escape");
  assert.equal(dismissed.native_dialog_answered.accepted, false);
  assert.deepEqual(runtime.commands.filter((item) => item.method === "Page.handleJavaScriptDialog").at(-1).params, { accept: false });
});

test("a page frozen behind a browser dialog is observed as its last observation plus the dialog", () => {
  const runtime = loadNativeDialogRuntime();
  assert.equal(runtime.nativeDialogObservation(7), null);
  runtime.nativeDialogs.set(7, { type: "confirm", message: "Archive 3 records?", default_prompt: "", prompt_text: null });
  assert.equal(runtime.nativeDialogObservation(7).failure_reason, "native_dialog_open_without_observation");
  runtime.lastObservationByTab.set(7, { ok: true, image: "frame", screenshot_sha256: "abc", page_evidence: { viewport_text: "Start archival job" } });
  const observed = runtime.nativeDialogObservation(7);
  assert.equal(observed.ok, true);
  assert.equal(observed.image, "frame");
  assert.equal(observed.native_dialog.message, "Archive 3 records?");
  assert.equal(observed.page_evidence.native_dialog.type, "confirm");
  assert.equal(observed.page_evidence.viewport_text, "Start archival job");
});

test("switch_tab brings only this run's own tabs to the front", async () => {
  const tabs = new Map([
    [11, { id: 11, windowId: 1, title: "Archive desk", url: "https://example.test/archive" }],
    [12, { id: 12, windowId: 1, title: "Relay console", url: "https://example.test/relay" }],
    [99, { id: 99, windowId: 1, title: "User's bank", url: "https://bank.example/" }],
  ]);
  const activated = [];
  const chrome = {
    runtime: { lastError: null },
    storage: { local: { set: async () => {} } },
    tabs: {
      get: async (id) => { if (!tabs.has(id)) throw new Error("No tab"); return tabs.get(id); },
      update: async (id) => { activated.push(id); return tabs.get(id); },
    },
    windows: { update: async () => ({}) },
  };
  let targetTabId = 12;
  let runTabIds = [11, 12];
  const controlIndicatorState = { active: false };
  const applyControlIndicatorToTab = async () => ({ ok: true });
  const clearTransientSensitiveScreenshotBinding = () => {};
  const persistRunTabIds = () => {};
  const errorMessage = (error) => String((error && error.message) || error || "");
  const runTabsEvidence = eval(`(${extractFunctionSource("runTabsEvidence")})`);
  const runVisionSwitchTab = eval(`(${extractFunctionSource("runVisionSwitchTab")})`);

  const listed = await runTabsEvidence();
  assert.deepEqual(listed.map((tab) => [tab.tab_id, tab.active]), [["11", false], ["12", true]]);
  assert.equal(listed.some((tab) => tab.tab_id === "99"), false, "the user's other tabs are never listed");

  const refused = await runVisionSwitchTab("99");
  assert.equal(refused.ok, false);
  assert.equal(refused.failure_reason, "tab_not_in_run");

  const switched = await runVisionSwitchTab("11");
  assert.equal(switched.ok, true);
  assert.equal(switched.switched, true);
  assert.equal(targetTabId, 11);
  assert.deepEqual(activated, [11]);
});

test("key chords dispatch as modifier flags on the main key; printable keys and Enter carry text", () => {
  const keyDispatchDescription = eval(`(${extractFunctionSource("keyDispatchDescription")})`);
  const chordModifierKeys = eval(`(${extractFunctionSource("chordModifierKeys")})`);
  const plain = (description) => JSON.parse(JSON.stringify(description));

  assert.deepEqual(plain(keyDispatchDescription("CTRL+A")), {
    key: "a", code: "KeyA", windowsVirtualKeyCode: 65, nativeVirtualKeyCode: 65, modifiers: 2,
  });
  assert.equal(keyDispatchDescription("Ctrl+V").modifiers, 2);
  assert.equal(keyDispatchDescription("Ctrl+V").text, undefined, "a shortcut chord never types its letter");
  const shiftTab = keyDispatchDescription("Shift+Tab");
  assert.equal(shiftTab.key, "Tab");
  assert.equal(shiftTab.windowsVirtualKeyCode, 9);
  assert.equal(shiftTab.modifiers, 8);
  assert.equal(keyDispatchDescription("Ctrl+Shift+End").modifiers, 10);
  assert.equal(keyDispatchDescription("Ctrl++").key, "+");
  assert.equal(keyDispatchDescription("o").text, "o");
  assert.equal(keyDispatchDescription("Shift+a").key, "A");
  assert.equal(keyDispatchDescription("Shift+a").text, "A");
  assert.equal(keyDispatchDescription("Enter").text, "\r");
  assert.equal(keyDispatchDescription("Escape").text, undefined);
  assert.equal(keyDispatchDescription("F5").windowsVirtualKeyCode, 116);
});

test("find-in-page brings the chosen occurrence of exact text into view through nested scrollers", () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <div id="outer"><div id="inner">
      <div class="row">Berth 124 <button>Claim</button></div>
      <div class="row" id="target">Berth 214 <button>Claim</button></div>
      <div class="row" id="second">Berth 214 <button>Claim</button></div>
    </div></div>
    <div style="display:none">Berth 214 hidden</div>
  </body></html>`, { runScripts: "outside-only", pretendToBeVisual: true });
  try {
    const scrolled = [];
    dom.window.Element.prototype.scrollIntoView = function () { scrolled.push(this.id || this.textContent.trim()); };
    dom.window.Element.prototype.getBoundingClientRect = function () {
      return { left: 10, top: 10, right: 200, bottom: 40, width: 190, height: 30, x: 10, y: 10 };
    };
    const find = dom.window.eval(`(${extractFunctionSource("scrollToTextInPage")})`);

    const first = find("berth  214", 1);
    assert.equal(first.ok, true);
    assert.equal(first.matches, 2, "the hidden copy is not a rendered match");
    assert.equal(first.occurrence, 1);
    assert.deepEqual(scrolled, ["target"]);

    const second = find("Berth 214", 2);
    assert.equal(second.occurrence, 2);
    assert.equal(scrolled.at(-1), "second");

    assert.equal(find("Berth 999", 1).failure_reason, "scroll_text_not_found");
  } finally {
    dom.window.close();
  }
});

test("find-in-page does not call an option clipped out of its list's window in view", () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <p id="sheet">Moderator element: <code>Terbium</code></p>
    <div id="menu" style="overflow-y:auto;height:150px"><div id="option">Terbium</div></div>
  </body></html>`, { runScripts: "outside-only", pretendToBeVisual: true });
  try {
    dom.window.Element.prototype.scrollIntoView = function () {};
    const boxes = {
      menu: { left: 300, top: 300, right: 500, bottom: 450 },
      option: { left: 300, top: 700, right: 500, bottom: 730 },
    };
    dom.window.Element.prototype.getBoundingClientRect = function () {
      const b = boxes[this.id] || { left: 10, top: 10, right: 200, bottom: 40 };
      return { ...b, width: b.right - b.left, height: b.bottom - b.top, x: b.left, y: b.top };
    };
    Object.defineProperty(dom.window, "innerHeight", { value: 900, configurable: true });
    Object.defineProperty(dom.window, "innerWidth", { value: 1200, configurable: true });
    const find = dom.window.eval(`(${extractFunctionSource("scrollToTextInPage")})`);

    const result = find("Terbium", 1);
    assert.equal(result.matches, 2);
    assert.equal(result.match_texts[0].in_view, true, "the sheet's text is on screen");
    assert.equal(result.match_texts[1].in_view, false, "the option sits below its list's visible window");
  } finally {
    dom.window.close();
  }
});

test("vision evidence lists scrollable regions with their raw scroll geometry", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <div id="hold" data-y="100" style="overflow-y:auto;height:120px"><div style="height:900px">Outer hold</div></div>
    <button id="plain" data-y="300">Engage</button>
  `);
  try {
    const hold = dom.window.document.querySelector("#hold");
    Object.defineProperty(hold, "scrollHeight", { value: 900, configurable: true });
    Object.defineProperty(hold, "clientHeight", { value: 120, configurable: true });
    hold.scrollTop = 0;
    const controls = fn().controls;
    const region = controls.find((control) => control.id === "hold");
    assert.ok(region, "the scrollable region is listed");
    assert.equal(region.scroll.height, 900);
    assert.equal(region.scroll.visible_height, 120);
    assert.equal(region.scroll.top, 0);
    assert.equal(controls.find((control) => control.id === "plain").scroll, null);
  } finally {
    dom.window.close();
  }
});

test("vision evidence reports the label CSS paints when it differs from the DOM text", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <button id="truth-a" data-y="100">Cancel order</button>
    <button id="plain" data-y="160">Continue</button>
  `);
  try {
    const realStyle = dom.window.getComputedStyle.bind(dom.window);
    dom.window.getComputedStyle = (node, pseudo) => {
      const style = realStyle(node, pseudo);
      if (node.id !== "truth-a") return style;
      if (pseudo === "::after") return { ...style, content: '"Confirm shipment"' };
      if (!pseudo) return new Proxy(style, { get: (target, key) => (key === "color" ? "rgba(0, 0, 0, 0)" : target[key]) });
      return style;
    };
    const controls = fn().controls;
    const truth = controls.find((control) => control.id === "truth-a");
    assert.equal(truth.text, "Cancel order");
    assert.deepEqual({ ...truth.painted }, { before: "", after: "Confirm shipment", own_text_hidden: true });
    assert.equal(controls.find((control) => control.id === "plain").painted, null);
  } finally {
    dom.window.close();
  }
});

test("vision drag follows its waypoints in order while held", async () => {
  const runtime = loadVisionDragRuntime();
  try {
    const result = await runtime.runVisionDrag(7, 200, 100, 600, 100, "left", [[400, 400]]);
    const held = runtime.dispatches
      .filter((item) => item.method === "Input.dispatchMouseEvent" && item.params.type === "mouseMoved" && item.params.buttons === 1)
      .map((item) => [item.params.x, item.params.y]);
    assert.equal(result.ok, true);
    assert.ok(held.some(([x, y]) => x === 200 && y === 200), "the pointer passes through the waypoint");
    assert.deepEqual(held.at(-1), [300, 50]);
    const waypointIndex = held.findIndex(([x, y]) => x === 200 && y === 200);
    assert.ok(held.slice(0, waypointIndex).every(([, y]) => y <= 200), "the first leg heads to the waypoint");
  } finally {
    runtime.dom.window.close();
  }
});

test("active element evidence names the focused element and the selection length, never the text", () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <textarea id="source">kvnq4-zweq</textarea><input id="register">
  </body></html>`, { runScripts: "outside-only", pretendToBeVisual: true });
  try {
    dom.window.eval("globalThis.__lumenStableDeepSelector = (el) => '#' + el.id;");
    const probe = dom.window.eval(`(${extractFunctionSource("activeElementEvidenceInPage")})`);
    const source = dom.window.document.querySelector("#source");
    source.focus();
    source.setSelectionRange(0, 0);
    const before = probe();
    source.select();
    const after = probe();
    assert.equal(after.active_selector, "#source");
    assert.equal(before.selection_length, 0);
    assert.equal(after.selection_length, 10);
    assert.equal(JSON.stringify(after).includes("kvnq4"), false, "the selected text itself is never reported");
    dom.window.document.querySelector("#register").focus();
    assert.equal(probe().active_selector, "#register");
  } finally {
    dom.window.close();
  }
});

test("watch reads an element's text continuously and reports each distinct value in order", async () => {
  const dom = new JSDOM(`<!doctype html><html><body><span id="gauge">-</span></body></html>`, { runScripts: "outside-only", pretendToBeVisual: true });
  try {
    const watch = dom.window.eval(`(${extractFunctionSource("watchElementTextInPage")})`);
    const gauge = dom.window.document.querySelector("#gauge");
    const readings = ["#1: 450", "#2: 812", "#3: 301"];
    let i = 0;
    const timer = dom.window.setInterval(() => { if (i < readings.length) gauge.textContent = readings[i++]; }, 60);
    const result = await watch("#gauge", null, 400);
    dom.window.clearInterval(timer);
    assert.equal(result.ok, true);
    const texts = Array.from(result.values).map((item) => item.text);
    assert.deepEqual(texts, ["-", ...readings]);
    assert.ok(Array.from(result.values).every((item, index, all) => index === 0 || item.t_ms >= all[index - 1].t_ms));
    const missing = await watch("#nothing", null, 200);
    assert.equal(missing.failure_reason, "watch_target_not_found");
  } finally {
    dom.window.close();
  }
});

test("watch never reads a sensitive control, nor an element inside or holding one", async () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <span id="gauge">-</span>
    <input id="pin" type="password" value="hunter2-secret">
    <input id="otp" autocomplete="one-time-code" value="481920">
    <input id="card" autocomplete="cc-number" value="4111111111111111">
    <div id="answer" contenteditable="true">recovery words here</div>
    <form id="login"><span id="status">Signed out</span><input id="password" type="password" value="pw-secret"></form>
  </body></html>`, { runScripts: "outside-only", pretendToBeVisual: true });
  try {
    const watch = dom.window.eval(`(${extractFunctionSource("watchElementTextInPage")})`);
    for (const target of ["#pin", "#otp", "#card", "#login"]) {
      const refused = await watch(target, null, 200);
      assert.equal(refused.ok, false, target);
      assert.equal(refused.failure_reason, "watch_target_sensitive", target);
      assert.equal(refused.values, undefined, `${target}: nothing is read`);
      assert.doesNotMatch(JSON.stringify(refused), /hunter2-secret|481920|4111111111111111|pw-secret/);
    }
    // A model-marked control (the tab's binding), and an element inside it.
    const marked = await watch("#answer", null, 200, ["#answer"]);
    assert.equal(marked.failure_reason, "watch_target_sensitive");
    assert.doesNotMatch(JSON.stringify(marked), /recovery words here/);
    // An unreadable marked selector refuses rather than guessing.
    assert.equal((await watch("#gauge", null, 200, ["#answer["])).failure_reason, "watch_target_sensitive");
    // The same page's ordinary element is still watched, marks and all.
    const gauge = await watch("#gauge", null, 200, ["#answer"]);
    assert.equal(gauge.ok, true);
    assert.deepEqual(Array.from(gauge.values).map((item) => item.text), ["-"]);
    // A watch by point that lands on the password field is refused the same way.
    dom.window.document.elementFromPoint = () => dom.window.document.querySelector("#pin");
    const byPoint = await watch("", [10, 10], 200);
    assert.equal(byPoint.failure_reason, "watch_target_sensitive");
  } finally {
    dom.window.close();
  }
});

test("watch stops and keeps nothing when its target turns sensitive", async () => {
  const dom = new JSDOM(`<!doctype html><html><body><input id="field" type="text" value="step 1"></body></html>`, {
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  try {
    const watch = dom.window.eval(`(${extractFunctionSource("watchElementTextInPage")})`);
    const field = dom.window.document.querySelector("#field");
    dom.window.setTimeout(() => {
      field.value = "s3cret-value";
      field.setAttribute("type", "password");
    }, 90);
    const result = await watch("#field", null, 600);
    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "watch_target_sensitive");
    assert.doesNotMatch(JSON.stringify(result), /step 1|s3cret-value/);
  } finally {
    dom.window.close();
  }
});

test("a followed target is found where its pixels are now, not among decoys", () => {
  const dom = new JSDOM(`<!doctype html><html><body></body></html>`, { runScripts: "outside-only" });
  try {
    const match = dom.window.eval(`(${extractFunctionSource("matchTemplateSad")})`);
    const image = (width, height, fill) => {
      const data = new Uint8ClampedArray(width * height * 4);
      for (let i = 0; i < width * height; i += 1) data.set([...fill, 255], i * 4);
      return { width, height, data };
    };
    const paint = (img, x0, y0, size, rgb) => {
      for (let y = y0; y < y0 + size; y += 1) for (let x = x0; x < x0 + size; x += 1) img.data.set([...rgb, 255], (y * img.width + x) * 4);
    };
    const template = image(20, 20, [16, 23, 36]);
    paint(template, 4, 4, 12, [250, 204, 21]); // a gold beacon on the dark background
    const frame = image(300, 200, [16, 23, 36]);
    paint(frame, 44, 64, 12, [107, 114, 128]); // grey decoy
    paint(frame, 210, 124, 12, [250, 204, 21]); // the beacon, moved
    const found = match(frame, template, 2);
    assert.equal(found.x, 206);
    assert.equal(found.y, 120);
    assert.ok(found.score < 5);
    const empty = image(300, 200, [16, 23, 36]);
    paint(empty, 44, 64, 12, [107, 114, 128]);
    assert.ok(match(empty, template, 2).score > 38, "no gold beacon: no confident match");
  } finally {
    dom.window.close();
  }
});

test("typing at the caret replaces only the selection and keeps the rest of the field", () => {
  const dom = new JSDOM(
    `<!doctype html><html><body><textarea id="doc">Container ID: ________\nSeal: ________</textarea><input id="pw" type="password"></body></html>`,
    { runScripts: "outside-only", pretendToBeVisual: true },
  );
  try {
    const insert = dom.window.eval(`(${extractFunctionSource("insertAtCaretInPage")})`);
    const doc = dom.window.document.querySelector("#doc");
    const inputs = [];
    doc.addEventListener("input", (event) => inputs.push(event.inputType));
    doc.focus();
    const blank = doc.value.indexOf("________");
    doc.setSelectionRange(blank, blank + 8);
    const result = insert("WDLQ-8");
    assert.equal(result.ok, true);
    assert.equal(doc.value, "Container ID: WDLQ-8\nSeal: ________");
    assert.equal(result.replaced_length, 8);
    assert.deepEqual(inputs, ["insertText"]);

    dom.window.document.querySelector("#pw").focus();
    assert.equal(insert("secret").failure_reason, "control_not_typable");
  } finally {
    dom.window.close();
  }
});

test("a noisy moved target is followed when it clearly beats every other place, and ambiguity is refused", () => {
  const dom = new JSDOM(`<!doctype html><html><body></body></html>`, { runScripts: "outside-only" });
  try {
    for (const name of ["matchTemplateSad", "poolImage2", "cropImage", "followMatch"]) {
      dom.window.eval(`globalThis.${name} = (${extractFunctionSource(name)})`);
    }
    const follow = dom.window.followMatch;
    const image = (width, height, fill) => {
      const data = new Uint8ClampedArray(width * height * 4);
      for (let i = 0; i < width * height; i += 1) data.set([...fill, 255], i * 4);
      return { width, height, data };
    };
    const ring = (img, cx, cy, radius, thickness, rgb) => {
      for (let y = 0; y < img.height; y += 1) for (let x = 0; x < img.width; x += 1) {
        const d = Math.hypot(x - cx, y - cy);
        if (Math.abs(d - radius) <= thickness / 2) img.data.set([...rgb, 255], (y * img.width + x) * 4);
      }
    };
    let seed = 7;
    const noise = (img, amount) => {
      for (let i = 0; i < img.data.length; i += 4) for (let c = 0; c < 3; c += 1) {
        seed = (seed * 1103515245 + 12345) % 2147483648;
        img.data[i + c] = Math.max(0, Math.min(255, img.data[i + c] + ((seed % (2 * amount + 1)) - amount)));
      }
    };
    const bg = [14, 23, 38];
    const template = image(44, 44, bg);
    ring(template, 22, 22, 16, 6, [250, 204, 21]);
    const frame = image(400, 240, bg);
    ring(frame, 80, 60, 16, 6, [107, 114, 128]);
    ring(frame, 320, 180, 16, 6, [107, 114, 128]);
    ring(frame, 250, 90, 16, 6, [250, 204, 21]); // the beacon, moved
    noise(frame, 60); // heavy compression-like noise: the strict threshold alone would miss it
    const found = follow(frame, template);
    assert.ok(found.accepted, `accepted (score ${found.score}, ratio ${found.ratio})`);
    assert.ok(Math.abs(found.x + 22 - 250) <= 2 && Math.abs(found.y + 22 - 90) <= 2);
    // Two identical beacons: no clear winner, so nothing is clicked.
    const twin = image(400, 240, bg);
    ring(twin, 100, 100, 16, 6, [250, 204, 21]);
    ring(twin, 300, 150, 16, 6, [250, 204, 21]);
    noise(twin, 60);
    assert.equal(follow(twin, template).accepted, false);
  } finally {
    dom.window.close();
  }
});

test("a small beacon among look-alike rings is judged on full-resolution scores", () => {
  const dom = new JSDOM(`<!doctype html><html><body></body></html>`, { runScripts: "outside-only" });
  try {
    for (const name of ["matchTemplateSad", "poolImage2", "cropImage", "followMatch"]) {
      dom.window.eval(`globalThis.${name} = (${extractFunctionSource(name)})`);
    }
    const follow = dom.window.followMatch;
    const image = (width, height, fill) => {
      const data = new Uint8ClampedArray(width * height * 4);
      for (let i = 0; i < width * height; i += 1) data.set([...fill, 255], i * 4);
      return { width, height, data };
    };
    const disc = (img, cx, cy, radius, rgb) => {
      for (let y = 0; y < img.height; y += 1) for (let x = 0; x < img.width; x += 1) {
        if (Math.hypot(x - cx, y - cy) <= radius) img.data.set([...rgb, 255], (y * img.width + x) * 4);
      }
    };
    const bg = [14, 23, 38];
    const yellow = [250, 204, 21];
    const grey = [107, 114, 128];
    // A 44x44 patch around a small beacon is mostly background.
    const template = image(44, 44, bg);
    disc(template, 22, 22, 6, yellow);
    const frame = image(480, 300, bg);
    for (const [x, y] of [[60, 60], [180, 60], [300, 60], [60, 220], [180, 220], [420, 220]]) disc(frame, x, y, 6, grey);
    disc(frame, 330, 170, 6, yellow);
    const found = follow(frame, template);
    assert.ok(found.accepted, `accepted (score ${found.score}, ratio ${found.ratio}, coarse ${found.coarse_ratio})`);
    assert.ok(Math.abs(found.x + 22 - 330) <= 1 && Math.abs(found.y + 22 - 170) <= 1);
    assert.ok(found.ratio <= found.coarse_ratio, "full resolution separates at least as well as pooling");
    // The same scene seen with 40 extra rows of chrome on top: the beacon is simply lower.
    const taller = image(480, 340, bg);
    for (const [x, y] of [[60, 100], [180, 100], [300, 100], [60, 260], [180, 260], [420, 260]]) disc(taller, x, y, 6, grey);
    disc(taller, 330, 210, 6, yellow);
    const moved = follow(taller, template);
    assert.ok(moved.accepted && Math.abs(moved.y + 22 - 210) <= 1);
    // Only grey look-alikes and a grey template: no single place wins.
    const greyTemplate = image(44, 44, bg);
    disc(greyTemplate, 22, 22, 6, grey);
    assert.equal(follow(frame, greyTemplate).accepted, false);
  } finally {
    dom.window.close();
  }
});

test("a control named by its label is found only once it is visible, and never among look-alikes", () => {
  const dom = new JSDOM(
    `<!doctype html><html><body>
      <button id="hold" data-y="100">Hold</button>
      <button id="commit" data-y="160" style="display:none">Commit</button>
      <button id="a" data-y="220">Engage</button><button id="b" data-y="260">Engage</button>
      <button id="off" data-y="300" disabled>Retry</button>
      <div role="listbox"><div role="option" id="tx" data-y="340"><span>Tex</span><span>as</span></div></div>
    </body></html>`,
    { url: "https://example.test/tempo", runScripts: "outside-only", pretendToBeVisual: true },
  );
  try {
    Object.defineProperty(dom.window, "devicePixelRatio", { value: 2, configurable: true });
    Object.defineProperty(dom.window, "innerHeight", { value: 900, configurable: true });
    Object.defineProperty(dom.window, "innerWidth", { value: 1200, configurable: true });
    dom.window.Element.prototype.getBoundingClientRect = function () {
      if (dom.window.getComputedStyle(this).display === "none") {
        return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0 };
      }
      const y = Number(this.getAttribute("data-y") || 0);
      return { left: 40, top: y, right: 140, bottom: y + 30, width: 100, height: 30, x: 40, y };
    };
    const find = dom.window.eval(`(${extractFunctionSource("findLabeledClickableInPage")})`);
    assert.equal(find("Commit").failure_reason, "target_text_not_found", "hidden until the window opens");
    dom.window.document.querySelector("#commit").style.display = "";
    const found = find("commit");
    assert.ok(found.ok);
    assert.deepEqual([found.img_x, found.img_y], [180, 350], "centre in screenshot pixels (CSS x2)");
    assert.equal(find("Engage").failure_reason, "target_text_ambiguous");
    assert.equal(find("Retry").failure_reason, "target_text_not_found", "a disabled control is not pressable");
    const option = find("Texas");
    assert.ok(option.ok, "an option of an open custom listbox is found by its label");
    assert.deepEqual([option.img_x, option.img_y], [180, 710]);
  } finally {
    dom.window.close();
  }
});

test("a fully visible control is clicked where it is; only an off-screen one is scrolled into view", () => {
  const { dom } = loadVisionEvidenceWindow(
    `<button id="sample" data-y="500">Sample</button><button id="far" data-y="1300">Far below</button>`,
  );
  try {
    let scrolls = 0;
    dom.window.Element.prototype.scrollIntoView = function () {
      scrolls += 1;
      Object.defineProperty(dom.window, "scrollY", { value: 300, configurable: true });
    };
    const center = dom.window.eval(`(${extractFunctionSource("visionControlCenterInPage")})`);
    const visible = center("#sample", true, false);
    assert.equal(visible.ok, true);
    assert.equal(scrolls, 0, "centring a visible control only moves the page under the model's other points");
    assert.equal(visible.scrolled_into_view, false);
    assert.deepEqual(Array.from(visible.scroll_delta_img), [0, 0]);
    const far = center("#far", true, false);
    assert.equal(scrolls, 1);
    assert.equal(far.scrolled_into_view, true);
    assert.deepEqual(Array.from(far.scroll_delta_img), [0, 300]);
  } finally {
    dom.window.close();
  }
});

test("a click by label waits for its control to appear, and gives up or refuses ambiguity", async () => {
  const build = (findResults) => {
    let calls = 0;
    const findLabeledClickableInPage = () => {};
    const visionPointInfoInPage = () => {};
    const chrome = {
      scripting: {
        executeScript: async ({ func }) => {
          if (func === findLabeledClickableInPage) {
            calls += 1;
            return [{ result: findResults(calls) }];
          }
          return [{ result: { ok: true, target_text: "Commit", target_tag: "button", target_selector: "#commit", dispatch_path: "dom_activation", page_url: "https://example.test/tempo" } }];
        },
      },
    };
    const withTimeout = (promise) => promise;
    const errorMessage = (error) => String(error);
    const resolve = new Function(
      "chrome", "withTimeout", "errorMessage", "findLabeledClickableInPage", "visionPointInfoInPage", "commandSocketEpoch",
      `return (${extractFunctionSource("resolveTargetTextClick")});`,
    )(chrome, withTimeout, errorMessage, findLabeledClickableInPage, visionPointInfoInPage, 0);
    return { resolve, calls: () => calls };
  };
  // Hidden for the first three looks, then shown.
  const appearing = build((call) => (call <= 3 ? { ok: false, failure_reason: "target_text_not_found" } : { ok: true, img_x: 180, img_y: 350 }));
  const found = await appearing.resolve(1, "Commit", 3000);
  assert.equal(found.ok, true);
  assert.equal(appearing.calls(), 4);
  assert.ok(found.waited_for_target_ms >= 150, `waited ${found.waited_for_target_ms} ms`);
  assert.equal(found.identity.target_selector, "#commit");
  const never = build(() => ({ ok: false, failure_reason: "target_text_not_found" }));
  const missing = await never.resolve(1, "Commit", 250);
  assert.equal(missing.failure_reason, "target_text_not_found");
  assert.ok(missing.waited_for_target_ms >= 250);
  const twins = build(() => ({ ok: false, failure_reason: "target_text_ambiguous", candidates: 2 }));
  const ambiguous = await twins.resolve(1, "Engage", 5000);
  assert.equal(ambiguous.failure_reason, "target_text_ambiguous");
  assert.equal(twins.calls(), 1, "two candidates is an answer, not a reason to keep polling");
  const now = build(() => ({ ok: true, img_x: 10, img_y: 10 }));
  assert.equal((await now.resolve(1, "Commit", 0)).ok, true);
});

test("a label-named click resolves a control whose live text carries edge glyphs, and still refuses a different word", async () => {
  const findLabeledClickableInPage = () => {};
  const visionPointInfoInPage = () => {};
  const run = async (liveText) => {
    const chrome = {
      scripting: {
        executeScript: async ({ func }) => (func === findLabeledClickableInPage
          ? [{ result: { ok: true, img_x: 10, img_y: 10 } }]
          : [{ result: { ok: true, target_text: liveText, target_selector: "#b" } }]),
      },
    };
    const resolve = new Function(
      "chrome", "withTimeout", "errorMessage", "findLabeledClickableInPage", "visionPointInfoInPage", "commandSocketEpoch",
      `return (${extractFunctionSource("resolveTargetTextClick")});`,
    )(chrome, (p) => p, String, findLabeledClickableInPage, visionPointInfoInPage, 0);
    return resolve(1, "Set status", 0);
  };
  assert.equal((await run("Set status ▾")).ok, true);
  assert.equal((await run("× Set status")).ok, true);
  assert.equal((await run("Cancel")).failure_reason, "target_text_covered");
});

test("a followed or label-named click can never carry final-submit or guarded authority", () => {
  const source = extractFunctionSource("runVisionClick");
  // Both dispatch-time retargeting paths are reached only for ordinary clicks.
  assert.match(source, /pointer && pointer\.target_text && !allowFinalSubmit && !allowGuardedIntermediate/);
  assert.match(source, /pointer && pointer\.follow && pointer\.follow\.template && !allowFinalSubmit && !allowGuardedIntermediate/);
  // A label-resolved click is dispatched with both authorities off.
  assert.match(source, /resolved\.img_y,\s*false,\s*false,\s*resolved\.identity/);
});

test("the value signature ignores rewritten labels but follows every field value", () => {
  const { dom, fn } = loadVisionEvidenceWindow(
    `<button id="shuffle" data-y="10">Alpha</button><input id="peak" data-y="60" value=""><button id="submit" data-y="100">Submit peak</button>`,
  );
  try {
    const first = fn();
    const doc = dom.window.document;
    doc.querySelector("#shuffle").textContent = "Omega"; // a live page rewrites a label
    const relabeled = fn();
    assert.notEqual(relabeled.document_state_signature, first.document_state_signature);
    assert.equal(relabeled.document_value_signature, first.document_value_signature);
    doc.querySelector("#peak").value = "947"; // a field value the submit carries
    const typed = fn();
    assert.notEqual(typed.document_value_signature, relabeled.document_value_signature);
  } finally {
    dom.window.close();
  }
});

test("the idle debugger detach waits while a run holds control, and detaches once it lets go", async () => {
  // A mid-run detach drops Chrome's debugging bar and grows the viewport between an observation
  // and the action chosen from it.
  const DEBUGGER_IDLE_DETACH_MS = 45000;
  const debuggerSessions = new Map([[7, { detachTimer: null }]]);
  const detached = [];
  const detachDebuggerNow = async (tabId) => { detached.push(tabId); debuggerSessions.delete(tabId); };
  const timers = [];
  const setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  let controlIndicatorState = { active: true, expires_at: Date.now() + 300000 };
  const idleDetachDebugger = eval(`(${extractFunctionSource("idleDetachDebugger")})`);

  await idleDetachDebugger(7);
  assert.deepEqual(detached, [], "a live lease keeps the session");
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, DEBUGGER_IDLE_DETACH_MS);
  assert.ok(debuggerSessions.get(7).detachTimer, "the idle timer is re-armed");

  controlIndicatorState = { active: true, expires_at: Date.now() - 1 };
  await idleDetachDebugger(7);
  assert.deepEqual(detached, [7], "a lapsed lease (backend gone) detaches as before");
});


test("a label-named click stops waiting and never clicks once its command socket closes", async () => {
  // The server abandoned the command with the socket; a control that appears afterwards must not
  // be pressed for it.
  let commandSocketEpoch = 0;
  let looks = 0;
  const findLabeledClickableInPage = () => {};
  const visionPointInfoInPage = () => {};
  const chrome = {
    scripting: {
      executeScript: async ({ func }) => {
        if (func === findLabeledClickableInPage) {
          looks += 1;
          if (looks === 2) commandSocketEpoch += 1;
          return [{ result: looks >= 3 ? { ok: true, img_x: 10, img_y: 10 } : { ok: false, failure_reason: "target_text_not_found" } }];
        }
        return [{ result: { ok: true, x: 10, y: 10, target_selector: "#go" } }];
      },
    },
  };
  const withTimeout = (promise) => promise;
  const errorMessage = (error) => String(error);
  const resolveTargetTextClick = eval(`(${extractFunctionSource("resolveTargetTextClick")})`);

  const result = await resolveTargetTextClick(1, "Go", 5000);
  assert.equal(result.ok, false);
  assert.equal(result.failure_reason, "command_connection_lost");
  assert.equal(looks, 2, "polling stopped as soon as the socket closed");
});

function loadVisionWaitRuntime(html, mutateOnProbe = {}) {
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    url: "https://example.test/tempo",
    runScripts: "outside-only",
  });
  // jsdom has no layout: innerText falls back to the text content.
  Object.defineProperty(dom.window.HTMLElement.prototype, "innerText", {
    configurable: true,
    get() { return this.textContent; },
  });
  let probes = 0;
  const chrome = {
    scripting: {
      executeScript: async ({ func, args }) => {
        probes += 1;
        if (typeof mutateOnProbe[probes] === "function") mutateOnProbe[probes](dom.window.document);
        const pageFunc = dom.window.eval(`(${func.toString()})`);
        return [{ result: pageFunc(...(args || [])) }];
      },
    },
  };
  const withTimeout = async (promise) => promise;
  const pageTextContainsInPage = eval(`(${extractFunctionSource("pageTextContainsInPage")})`);
  const runVisionWait = eval(`(${extractFunctionSource("runVisionWait")})`);
  return { dom, runVisionWait, probes: () => probes };
}

test("a wait for text fires when that text newly appears, never on a copy already shown", async () => {
  const runtime = loadVisionWaitRuntime(
    `<p>Fire once the gauge says READY TO FIRE (it says ready to fire only briefly).</p><p id="status">Charging</p>`,
    { 3: (doc) => { doc.querySelector("#status").textContent = "READY TO FIRE"; } },
  );
  try {
    const result = await runtime.runVisionWait(7, 3000, "READY TO FIRE");
    assert.equal(result.ok, true);
    assert.equal(result.text_matched, true);
    assert.equal(result.present_at_start, 2);
    assert.ok(runtime.probes() >= 3);
  } finally {
    runtime.dom.window.close();
  }
});

test("a wait for text already on the page, in any casing, does not fire", async () => {
  const runtime = loadVisionWaitRuntime(`<p>When it is ready to fire, press READY TO FIRE.</p><p id="status">Charging</p>`);
  try {
    const result = await runtime.runVisionWait(7, 300, "READY TO FIRE");
    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "wait_text_not_seen");
    assert.equal(result.present_at_start, 2);
  } finally {
    runtime.dom.window.close();
  }
});

test("a wait for text counts a status that disappears and comes back", async () => {
  const runtime = loadVisionWaitRuntime(`<p id="status">Armed</p>`, {
    2: (doc) => { doc.querySelector("#status").textContent = "Charging"; },
    4: (doc) => { doc.querySelector("#status").textContent = "Armed"; },
  });
  try {
    const result = await runtime.runVisionWait(7, 3000, "Armed");
    assert.equal(result.ok, true);
    assert.equal(result.present_at_start, 1);
  } finally {
    runtime.dom.window.close();
  }
});

test("vision evidence lists a textless drag surface and a slider with their raw drag facts", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <div id="dial" style="cursor: grab" data-y="100" data-width="80" data-height="80"><div id="dial-inner" style="cursor: grab" data-y="110" data-width="20" data-height="20"></div></div>
    <div id="thumb" role="slider" aria-valuenow="40" aria-valuemin="0" aria-valuemax="100" data-y="300" data-width="16" data-height="16"></div>
    <div id="card" draggable="true" data-y="400" data-width="200" data-height="60"></div>
    <div id="plain" data-y="500">Nothing to drag</div>
  `);
  try {
    const controls = fn().controls;
    const byId = Object.fromEntries(controls.map((row) => [row.id, row]));
    assert.ok(byId.dial, "the outermost grab surface is listed without any text");
    assert.equal(byId["dial-inner"], undefined);
    assert.equal(JSON.stringify(byId.dial.affordances), JSON.stringify(["cursor:grab"]));
    assert.equal(byId.thumb.role, "slider");
    assert.equal(byId.thumb.aria_valuenow, "40");
    assert.equal(JSON.stringify(byId.card.affordances), JSON.stringify(["draggable"]));
    assert.equal(byId.plain, undefined);
  } finally {
    dom.window.close();
  }
});

test("a click chained into a watch skips the post-click settle so the watch sees the first readings", async () => {
  const settled = loadSingleActivationClickRuntime('<button data-hit id="sample">Sample</button>');
  const chained = loadSingleActivationClickRuntime('<button data-hit id="sample">Sample</button>');
  try {
    const first = await settled.runTrustedVisionClick(7, 100, 60, false, false, null, { glide_ms: 0 });
    assert.equal(first.ok, true);
    assert.equal(settled.settleWaits(), 1);
    const second = await chained.runTrustedVisionClick(7, 100, 60, false, false, null, { glide_ms: 0, settle: false });
    assert.equal(second.ok, true);
    assert.equal(chained.settleWaits(), 0);
    assert.equal(chained.clickInvocations(), 1);
  } finally {
    settled.dom.window.close();
    chained.dom.window.close();
  }
});

test("a wait for text matches a status whatever letter case the page styles it in", async () => {
  const runtime = loadVisionWaitRuntime(`<p id="status">Charging</p>`, {
    3: (doc) => { doc.querySelector("#status").textContent = "Ready"; },
  });
  try {
    const result = await runtime.runVisionWait(7, 3000, "READY");
    assert.equal(result.ok, true);
    assert.equal(result.present_at_start, 0);
  } finally {
    runtime.dom.window.close();
  }
});

test("bench tab cleanup matches only loopback Interaction Gauntlet pages", () => {
  const isLocalBenchUrl = eval(`(${extractFunctionSource("isLocalBenchUrl")})`);
  assert.equal(isLocalBenchUrl("http://127.0.0.1:50373/eval/bench/v3_incident_portal?sid=a95891"), true);
  assert.equal(isLocalBenchUrl("http://localhost:8001/eval/bench/v2_mutation_storm"), true);
  assert.equal(isLocalBenchUrl("https://example.com/eval/bench/v3_incident"), false);
  assert.equal(isLocalBenchUrl("http://127.0.0.1:8001/operator/status"), false);
  assert.equal(isLocalBenchUrl("chrome://newtab/"), false);
  assert.equal(isLocalBenchUrl(""), false);
});

test("bench tab cleanup never closes the browser's last tab", async () => {
  const created = [];
  const removed = [];
  const chrome = {
    tabs: {
      query: async () => [
        { id: 1, url: "http://127.0.0.1:55989/eval/bench/v3_paperwork" },
        { id: 2, url: "http://127.0.0.1:55989/eval/bench/v3_incident_portal?sid=1" },
      ],
      create: async (props) => { created.push(props); return { id: 3 }; },
      remove: async (id) => { removed.push(id); },
    },
  };
  const errorMessage = (error) => String(error);
  const isLocalBenchUrl = eval(`(${extractFunctionSource("isLocalBenchUrl")})`);
  const closeBenchTabs = eval(`(${extractFunctionSource("closeBenchTabs")})`);
  const result = await closeBenchTabs();
  assert.equal(result.ok, true);
  assert.equal(result.closed, 2);
  assert.equal(created.length, 1);
  assert.deepEqual(removed, [1, 2]);
});

test("visionControlCenterInPage scrolls a clipped listbox option within its list (nearest) and says so", () => {
  const dom = new JSDOM(
    `<!doctype html><body><ul role="listbox" id="times"><li role="option" id="t1530">15:30</li></ul></body>`,
    { url: "https://example.test/cal", runScripts: "outside-only" },
  );
  const win = dom.window;
  Object.defineProperty(win, "devicePixelRatio", { value: 1, configurable: true });
  const listbox = win.document.getElementById("times");
  Object.defineProperty(listbox, "scrollHeight", { value: 600, configurable: true });
  Object.defineProperty(listbox, "clientHeight", { value: 100, configurable: true });
  win.Element.prototype.getBoundingClientRect = function () {
    const top = this.id === "t1530" ? 400 : 0;
    return { left: 10, top, right: 110, bottom: top + 20, width: 100, height: 20, x: 10, y: top };
  };
  let seen = null;
  win.document.getElementById("t1530").scrollIntoView = (arg) => { seen = arg; listbox.scrollTop = 300; };
  const center = win.eval(`(${extractFunctionSource("visionControlCenterInPage")})`)("#t1530", true, false);
  assert.equal(center.ok, true);
  assert.equal(seen.block, "nearest");
  assert.equal(center.scrolled_into_view, true);
  assert.equal(center.scrolled_within_list, true);
});

test("vision evidence numbers dialog containers so the newest open dialog's controls are known", () => {
  const { fn } = loadVisionEvidenceWindow(`
    <button id="open" data-y="40">Open</button>
    <div role="dialog" id="d1" data-y="80"><button id="ok1" data-y="90">OK</button></div>
    <div aria-modal="true" id="d2" data-y="200"><button id="save" data-y="210">Save</button></div>
  `);
  const evidence = fn();
  const byId = (id) => evidence.controls.find((control) => control.id === id);
  assert.equal(byId("open").in_dialog, undefined);
  assert.equal(byId("ok1").in_dialog, true);
  assert.equal(byId("save").in_dialog, true);
  assert.ok(byId("save").dialog_ordinal > byId("ok1").dialog_ordinal);
});

test("point preflight reports a value control at the point and a plain button as not one", () => {
  const { dom, fn } = loadInjectedClickWindow(`
    <input data-hit id="yes" type="radio" name="us" aria-label="Yes">
    <button data-hit id="save">Save deal</button>
    <div data-hit id="opt" role="option" aria-selected="false">Qualification</div>`);
  stubRects(dom, {
    yes: { left: 10, top: 10, width: 60, height: 30 },
    save: { left: 200, top: 10, width: 120, height: 30 },
    opt: { left: 400, top: 10, width: 120, height: 30 },
  });
  assert.equal(fn(60, 50, false, false, null).target_value_control, true);
  assert.equal(fn(1000, 50, false, false, null).target_value_control, true);
  assert.equal(fn(1000, 50, false, false, null).ok, true);
  assert.equal(fn(600, 50, false, false, null).target_value_control, false);

});

function labelProbeWindow(html) {
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    url: "https://example.test/tab", runScripts: "outside-only", pretendToBeVisual: true,
  });
  Object.defineProperty(dom.window, "devicePixelRatio", { value: 1, configurable: true });
  Object.defineProperty(dom.window, "innerHeight", { value: 600, configurable: true });
  Object.defineProperty(dom.window, "innerWidth", { value: 800, configurable: true });
  const offsets = new Map();
  dom.window.Element.prototype.getBoundingClientRect = function () {
    if (this.hasAttribute("data-list")) return { left: 0, top: 100, right: 300, bottom: 200, width: 300, height: 100, x: 0, y: 100 };
    const inList = Boolean(this.closest("[data-list]"));
    const y = Number(this.getAttribute("data-y") || 0) - (offsets.get("page") || 0) - (inList ? (offsets.get("list") || 0) : 0);
    return { left: 40, top: y, right: 140, bottom: y + 30, width: 100, height: 30, x: 40, y };
  };
  return { dom, offsets };
}

test("target_text: a unique match below the viewport is scrolled into view and used; its facts say so", () => {
  const { dom, offsets } = labelProbeWindow(`<button id="d" data-y="900">Discontinue</button>`);
  try {
    dom.window.Element.prototype.scrollIntoView = function () { offsets.set("page", 500); };
    const find = dom.window.eval(`(${extractFunctionSource("findLabeledClickableInPage")})`);
    const found = find("Discontinue");
    assert.equal(found.ok, true);
    assert.equal(found.scrolled_into_view, true);
    assert.deepEqual([found.img_x, found.img_y], [90, 415]);
  } finally { dom.window.close(); }
});

test("target_text: an off-screen match that stays off screen reports not_visible with side and distance", () => {
  const { dom } = labelProbeWindow(`<button id="d" data-y="900">Discontinue</button>`);
  try {
    dom.window.Element.prototype.scrollIntoView = function () {};
    const find = dom.window.eval(`(${extractFunctionSource("findLabeledClickableInPage")})`);
    const missing = find("Discontinue");
    assert.equal(missing.failure_reason, "target_text_not_visible");
    assert.equal(missing.off_screen_side, "below");
    assert.equal(missing.off_screen_px, 300);
    assert.equal(find("Nothing here").failure_reason, "target_text_not_found");
  } finally { dom.window.close(); }
});

test("target_text: several matches all off screen are ambiguous, never scrolled to", () => {
  const { dom } = labelProbeWindow(`<button data-y="900">Open</button><button data-y="1000">Open</button>`);
  try {
    let scrolls = 0;
    dom.window.Element.prototype.scrollIntoView = function () { scrolls += 1; };
    const find = dom.window.eval(`(${extractFunctionSource("findLabeledClickableInPage")})`);
    const result = find("Open");
    assert.equal(result.failure_reason, "target_text_ambiguous");
    assert.equal(result.candidates, 2);
    assert.equal(scrolls, 0);
  } finally { dom.window.close(); }
});

test("target_text: a unique option clipped inside its own scrolling list is scrolled within the list and found", () => {
  const { dom, offsets } = labelProbeWindow(
    `<div data-list style="overflow-y:auto"><div role="option" data-y="150">Alabama</div><div role="option" id="ma" data-y="400">Massachusetts</div></div>`,
  );
  try {
    dom.window.Element.prototype.scrollIntoView = function (arg) {
      assert.equal(arg.block, "nearest");
      offsets.set("list", 280);
    };
    const find = dom.window.eval(`(${extractFunctionSource("findLabeledClickableInPage")})`);
    const found = find("Massachusetts");
    assert.equal(found.ok, true, JSON.stringify(found));
    assert.equal(found.scrolled_into_view, true);
  } finally { dom.window.close(); }
});

test("scrollSelectorIntoViewInPage scrolls a unique element of the remembered identity, refuses a changed or ambiguous one", () => {
  const { dom } = labelProbeWindow(`<button id="a" data-y="900">Save</button><a id="b" href="#" data-y="950">Link</a><i class="x" data-y="1">1</i><i class="x" data-y="2">2</i>`);
  try {
    let scrolled = 0;
    dom.window.Element.prototype.scrollIntoView = function (arg) { scrolled += 1; assert.equal(arg.block, "nearest"); };
    const scroll = dom.window.eval(`(${extractFunctionSource("scrollSelectorIntoViewInPage")})`);
    assert.equal(scroll("#a", { tag: "button", role: "", type: "" }).scrolled_into_view, true);
    assert.equal(scrolled, 1);
    assert.equal(scroll("#a", { tag: "a", role: "", type: "" }).failure_reason, "control_identity_changed");
    assert.equal(scroll(".x", { tag: "i", role: "", type: "" }).failure_reason, "control_selector_ambiguous");
    assert.equal(scroll("#gone", { tag: "button", role: "", type: "" }).failure_reason, "control_not_found");
    assert.equal(scrolled, 1, "a refused identity never scrolls");
  } finally { dom.window.close(); }
});

test("a key chord presses each modifier as its own key, then the key, and releases in reverse", async () => {
  const runtime = loadVisionKeyRuntime();
  const result = await runtime.runVisionKey(44, "Ctrl+Shift+Y");
  const events = runtime.commands
    .filter(({ method }) => method === "Input.dispatchKeyEvent")
    .map(({ params }) => `${params.type}:${params.key}:${params.modifiers}`);

  assert.equal(result.ok, true);
  assert.deepEqual(events, [
    "rawKeyDown:Control:2",
    "rawKeyDown:Shift:10",
    "rawKeyDown:Y:10",
    "keyUp:Y:10",
    "keyUp:Shift:2",
    "keyUp:Control:0",
  ]);
});

test("typing into a field the page then removes reads back as replaced (unverified), not as a mismatch", () => {
  const { dom } = labelProbeWindow(`<input id="answer" type="text">`);
  try {
    const setIn = dom.window.eval(`(${extractFunctionSource("activeValueSetInPage")})`);
    const evidence = dom.window.eval(`(${extractFunctionSource("activeValueEvidenceInPage")})`);
    const input = dom.window.document.getElementById("answer");
    input.focus();
    assert.equal(setIn("hello").ok, true);
    input.remove();
    const read = evidence("hello");
    assert.equal(read.ok, true);
    assert.equal(read.typed_control_replaced, true);
    assert.equal("active_value" in read, false);
  } finally { dom.window.close(); }
});

test("a value read back from an invisible field is flagged hidden, a visible one is not", () => {
  const { dom } = labelProbeWindow(`<input id="shown" type="text"><input id="faded" type="text" style="opacity:0"><div style="opacity:0"><input id="inherited" type="text"></div><input id="gone" type="text" style="visibility:hidden">`);
  try {
    const setIn = dom.window.eval(`(${extractFunctionSource("activeValueSetInPage")})`);
    const evidence = dom.window.eval(`(${extractFunctionSource("activeValueEvidenceInPage")})`);
    const read = (id) => {
      const input = dom.window.document.getElementById(id);
      input.focus();
      assert.equal(setIn("ls").ok, true, id);
      return evidence("ls");
    };
    assert.equal(read("shown").active_hidden, undefined);
    assert.deepEqual([read("faded").active_hidden, read("faded").active_hidden_reason], [true, "opacity 0"]);
    assert.equal(read("inherited").active_hidden_reason, "opacity 0");
  } finally { dom.window.close(); }
});

test("a key that types or pastes is refused at dispatch when focus is on a sensitive field", async () => {
  for (const key of ["a", "Shift+A", " ", "space", "Ctrl+v", "Meta+v", "Ctrl+Shift+V", "Shift+Insert"]) {
    const runtime = loadVisionKeyRuntime({ active: { active_typable: true, active_sensitive: true } });
    const result = await runtime.runVisionKey(44, key);
    assert.equal(result.ok, false, "ok " + key);
    assert.equal(result.failure_reason, "keyed_text_sensitive_refused", key);
    assert.equal(runtime.commands.length, 0, key);
  }
  for (const key of ["Tab", "Enter", "Backspace", "Ctrl+a", "ArrowLeft"]) {
    const runtime = loadVisionKeyRuntime({ active: { active_typable: true, active_sensitive: true } });
    const result = await runtime.runVisionKey(44, key);
    assert.notEqual(result.failure_reason, "keyed_text_sensitive_refused", key);
  }
  const plain = loadVisionKeyRuntime({ active: { active_typable: true, active_sensitive: false } });
  assert.notEqual((await plain.runVisionKey(44, "a")).failure_reason, "keyed_text_sensitive_refused");
});

test("a key that types or pastes is refused when pre-key focus evidence is unavailable (fail closed)", async () => {
  for (const key of ["a", " ", "Ctrl+v", "Meta+v", "Ctrl+Shift+V", "Shift+Insert"]) {
    const runtime = loadVisionKeyRuntime({ evidenceThrows: true });
    const result = await runtime.runVisionKey(44, key);
    assert.equal(result.ok, false, key);
    assert.equal(result.failure_reason, "keyed_text_focus_unverified", key);
    assert.equal(runtime.commands.length, 0, key);
  }
  for (const key of ["Tab", "Enter", "ArrowLeft", "Backspace", "Ctrl+a"]) {
    const runtime = loadVisionKeyRuntime({ evidenceThrows: true });
    const result = await runtime.runVisionKey(44, key);
    assert.notEqual(result.failure_reason, "keyed_text_focus_unverified", key);
    assert.ok(runtime.commands.length > 0, key);
  }
});

test("a page copy event reports the copied text for the server to gate a later paste; a password copy is private", () => {
  const { dom } = labelProbeWindow(`<input id="plain" type="text" value="hello world"><input id="pw" type="password" value="hunter2">`);
  try {
    const arm = dom.window.eval(`(${extractFunctionSource("armClipboardEvidenceInPage")})`);
    const read = dom.window.eval(`(${extractFunctionSource("readClipboardEvidenceInPage")})`);
    const fire = (id) => {
      const input = dom.window.document.getElementById(id);
      input.focus();
      input.setSelectionRange(0, input.value.length);
      input.dispatchEvent(new dom.window.Event("copy", { bubbles: true, cancelable: true }));
    };
    assert.equal(arm().ok, true);
    fire("plain");
    let events = read().events;
    assert.equal(events[0].type, "copy");
    assert.equal(events[0].text, "hello world");
    assert.equal(arm().ok, true);
    fire("pw");
    events = read().events;
    assert.equal(events[0].private, true);
    assert.equal("text" in events[0], false);
  } finally { dom.window.close(); }
});

test("a key that types or pastes is refused while focus sits in a frame; navigation keys still go", async () => {
  for (const key of ["a", " ", "Ctrl+v", "Shift+Insert"]) {
    const runtime = loadVisionKeyRuntime({ active: { active_tag: "iframe" } });
    const result = await runtime.runVisionKey(44, key);
    assert.equal(result.ok, false, key);
    assert.equal(result.failure_reason, "keyed_text_frame_focus_refused", key);
    assert.equal(runtime.commands.length, 0, key);
  }
  const arrow = loadVisionKeyRuntime({ active: { active_tag: "iframe" } });
  assert.notEqual((await arrow.runVisionKey(44, "ArrowLeft")).failure_reason, "keyed_text_frame_focus_refused");
});


test("coordinate pixel drift confined to the chain's earlier click is tolerated, drift elsewhere is not", async () => {
  const context = {
    screenshot_sha256: "observed",
    screenshot_mode: "capture_visible_tab",
    screenshot_quality: 60,
    chain_prior_click_regions: [{ x: 10, y: 10, width: 40, height: 40 }],
    chain_click_point: { x: 300, y: 200 },
  };
  const tolerated = loadCoordinateFingerprintRuntime({
    liveHashes: ["changed"],
    priorRegionVerdict: { ok: true, regions: 1 },
  });
  const okResult = await tolerated.captureMaskedViewportFingerprint(44, context);
  assert.equal(okResult.ok, true);
  assert.equal(okResult.pixel_change_confined_to_prior_click, true);

  const refused = loadCoordinateFingerprintRuntime({
    liveHashes: ["changed"],
    priorRegionVerdict: { ok: false, why: "changed_outside_prior_click" },
  });
  const bad = await refused.captureMaskedViewportFingerprint(44, context);
  assert.equal(bad.ok, false);
  assert.equal(bad.reason, "coordinate_pixel_drift");
  assert.equal(bad.prior_click_region_check, "changed_outside_prior_click");

  const noRegions = loadCoordinateFingerprintRuntime({ liveHashes: ["changed"], priorRegionVerdict: { ok: true } });
  const plain = await noRegions.captureMaskedViewportFingerprint(44, { ...context, chain_prior_click_regions: [] });
  assert.equal(plain.ok, false);
  assert.equal(plain.reason, "coordinate_pixel_drift");
});

test("pixel comparison outside prior click regions ignores only those boxes and never the click target", async () => {
  const W = 100;
  const H = 100;
  const make = (paint) => {
    const data = new Uint8ClampedArray(W * H * 4).fill(200);
    for (const [x, y] of paint) {
      const i = (y * W + x) * 4;
      data[i] = 0;
    }
    return { width: W, height: H, data };
  };
  const images = new Map();
  const lastObservationByTab = new Map([[44, { image: "OBSERVED", screenshot_sha256: "observed" }]]);
  const fetch = async (url) => ({ blob: async () => ({ url }) });
  const createImageBitmap = async (blob) => ({ width: W, height: H, key: blob.url });
  class OffscreenCanvas {
    getContext() {
      let key = "";
      return {
        drawImage: (bitmap) => { key = bitmap.key; },
        getImageData: () => images.get(key),
      };
    }
  }
  const pixelsMatchOutsideRegions = eval(`(${extractFunctionSource("pixelsMatchOutsideRegions")})`);
  const regions = [{ x: 10, y: 10, width: 10, height: 10 }];
  images.set("data:image/jpeg;base64,OBSERVED", make([]));
  images.set("live-inside", make([[15, 15]]));
  images.set("live-outside", make([[80, 80]]));
  const point = { x: 70, y: 70 };
  assert.equal((await pixelsMatchOutsideRegions(44, "observed", "live-inside", regions, point)).ok, true);
  const outside = await pixelsMatchOutsideRegions(44, "observed", "live-outside", regions, point);
  assert.equal(outside.ok, false);
  assert.equal(outside.why, "changed_outside_prior_click");
  const onTarget = await pixelsMatchOutsideRegions(44, "observed", "live-inside", regions, { x: 12, y: 12 });
  assert.equal(onTarget.why, "target_inside_prior_click_region");
  assert.equal((await pixelsMatchOutsideRegions(44, "other-hash", "live-inside", regions, point)).why, "observed_image_unavailable");
});


test("a multi-select lists every selected option, not only the first", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`
    <select id="m" multiple data-y="40" data-height="80">
      <option>Miguelita</option><option selected>Lolita</option><option selected>Ana</option>
    </select>`);
  const control = (fn([]).controls || []).find((row) => row.id === "m");
  assert.ok(control);
  assert.deepEqual(Array.from(control.selected_values), ["Lolita", "Ana"]);
  dom.window.close();
});

test("SVG same-graphic binding is never granted to a final or guarded click", () => {
  const guard = /if \(allowFinalSubmit \|\| allowGuardedIntermediate\) return false;[^\n]*\n?\s*(?:\/\/[^\n]*\n\s*)?(?:const surface = exactTarget\.closest|if \(allowFinalSubmit)/;
  assert.equal((BACKGROUND_SRC.match(/const surface = exactTarget\.closest && exactTarget\.closest\("svg"\)/g) || []).length, 2);
  assert.equal((BACKGROUND_SRC.match(/if \(allowFinalSubmit \|\| allowGuardedIntermediate\) return false;/g) || []).length >= 2, true);
  assert.ok(guard);
});


const MOVED_RECT = { left: 400, top: 300, right: 520, bottom: 350, width: 120, height: 50 };

test("an ordinary stable-id click on a control that moved before dispatch rebinds to its current centre", async () => {
  const runtime = loadPrivilegedFinalClickRuntime(
    `<button id="go">Click me</button>`,
    { moveAfterFirstTrustedProbeTo: MOVED_RECT },
  );
  try {
    const result = await runtime.runVisionClickControl(7, "#go", 0, 0, false, false, null, null, { rebind_on_drift: true });

    assert.equal(result.ok, true);
    assert.equal(runtime.clickInvocations(), 1);
    assert.match(result.target_moved_rebound_at_dispatch, /^from \(\d+,\d+\) to \(\d+,\d+\)$/);
    assert.notDeepEqual(result.target_moved_rebound_from, result.target_moved_rebound_to);
    const press = runtime.dispatches.filter((d) => d.params && d.params.type === "mousePressed").pop();
    assert.ok(press.params.x >= 400 && press.params.x <= 520 && press.params.y >= 300 && press.params.y <= 350);
  } finally {
    runtime.dom.window.close();
  }
});

test("a moved control without the server's rebind permission is still refused as drift", async () => {
  const runtime = loadPrivilegedFinalClickRuntime(
    `<button id="go">Click me</button>`,
    { moveAfterFirstTrustedProbeTo: MOVED_RECT },
  );
  try {
    const result = await runtime.runVisionClickControl(7, "#go", 0, 0, false, false, null, null, null);

    assert.equal(result.ok, false);
    assert.equal(runtime.clickInvocations(), 0);
    assert.equal(result.target_moved_rebound_at_dispatch, undefined);
  } finally {
    runtime.dom.window.close();
  }
});

test("a moved final-submit click is never rebound even when the flag is present", async () => {
  const runtime = loadPrivilegedFinalClickRuntime(
    `<button id="go">Submit application</button>`,
    { moveAfterFirstTrustedProbeTo: MOVED_RECT },
  );
  try {
    const result = await runtime.runVisionClickControl(7, "#go", 0, 0, true, false, null, null, { rebind_on_drift: true });

    assert.equal(result.ok, false);
    assert.equal(runtime.clickInvocations(), 0);
    assert.equal(result.target_moved_rebound_at_dispatch, undefined);
  } finally {
    runtime.dom.window.close();
  }
});

test("a moved control whose accessible name also changed is not rebound", async () => {
  const runtime = loadPrivilegedFinalClickRuntime(
    `<button id="go">Click me</button>`,
    {
      moveAfterFirstTrustedProbeTo: MOVED_RECT,
      driftAfterFirstTrustedProbeHtml: '<button id="go">Delete everything</button>',
    },
  );
  try {
    const result = await runtime.runVisionClickControl(7, "#go", 0, 0, false, false, null, null, { rebind_on_drift: true });

    assert.equal(result.ok, false);
    assert.equal(runtime.clickInvocations(), 0);
    assert.equal(result.target_moved_rebound_at_dispatch, undefined);
  } finally {
    runtime.dom.window.close();
  }
});

test("vision evidence stamps dispatch_path with the click's own rule", () => {
  const { fn } = loadVisionEvidenceWindow(`
    <button id="plain" data-y="40">Go</button>
    <svg id="pie" width="200" height="200"><text id="slice" role="button" data-y="100">Slice</text></svg>
  `);

  const evidence = fn();
  const plain = evidence.controls.find((control) => control.id === "plain");
  const slice = evidence.controls.find((control) => control.id === "slice");

  assert.equal(plain.dispatch_path, "dom_activation");
  assert.equal(slice.tag, "text");
  // role=button makes the text its own actionable target, so the live click activates it by DOM.
  assert.equal(slice.dispatch_path, "dom_activation");
});

for (const [label, attrs, expectedPath] of [
  ["a plain SVG text", "", "cdp_trusted_click"],
  ["an SVG text with role=button", 'role="button"', "dom_activation"],
]) {
  test(`vision_click_control accepts the observer's dispatch_path for ${label} inside an svg`, async () => {
    const html = `<svg id="pie" width="200" height="200"><text id="slice" ${attrs} data-hit data-y="100">Slice</text></svg>`;
    const evidenceRuntime = loadVisionEvidenceWindow(html);
    const { dom, fn } = loadInjectedControlClickWindow(html);
    try {
      const observed = evidenceRuntime.fn().controls.find((control) => control.id === "slice");
      if (observed) assert.equal(observed.dispatch_path, expectedPath);
      const slice = dom.window.document.querySelector("#slice");
      slice.getBoundingClientRect = () => ({ left: 40, top: 20, right: 140, bottom: 60, width: 100, height: 40 });
      const result = await fn(1, "#slice", 0, 0, false, false, {
        target_selector: "#slice",
        target_text: "Slice",
        target_tag: "text",
        target_type: "",
        target_role: attrs ? "button" : "",
        dispatch_path: expectedPath,
        page_url: dom.window.location.href,
      });
      assert.notEqual(result.target_identity_mismatch_field, "dispatch_path", JSON.stringify(result));
      assert.equal(result.ok, true, JSON.stringify(result));

      const wrong = await fn(1, "#slice", 0, 0, false, false, {
        target_selector: "#slice",
        target_text: "Slice",
        target_tag: "text",
        target_type: "",
        target_role: attrs ? "button" : "",
        dispatch_path: expectedPath === "dom_activation" ? "cdp_trusted_click" : "dom_activation",
        page_url: dom.window.location.href,
      });
      assert.equal(wrong.ok, false);
      assert.equal(wrong.target_identity_mismatch_field, "dispatch_path");
    } finally {
      dom.window.close();
      evidenceRuntime.dom && evidenceRuntime.dom.window.close();
    }
  });
}

test("in-page click identity accepts the same input whose label lost its typeahead status tail", () => {
  // Run 1792: Lever's location label read "Current location ✱ No location found ... Loading" at
  // observation and "Current location ✱" at the click, so every focus click was refused.
  const { dom, fn } = loadInjectedClickWindow(`
    <label>Current location <span>✱</span><input data-hit id="location-input" type="text" autocomplete="off"></label>`);
  try {
    stubRects(dom, { "location-input": { left: 10, top: 10, width: 300, height: 30 } });
    const expected = {
      target_selector: "#location-input",
      target_text: "Current location ✱ No location found. Try entering a different location Loading",
      target_names: ["Current location ✱ No location found. Try entering a different location Loading", "location"],
      target_tag: "input",
      target_type: "text",
      target_role: "",
      dispatch_path: "dom_activation",
      page_url: dom.window.location.href,
    };
    assert.equal(fn(100, 50, false, false, expected).ok, true);
    // Not for strict authority, and not for another element.
    assert.equal(fn(100, 50, true, false, expected).ok, false);
    assert.equal(fn(100, 50, false, false, { ...expected, target_selector: "#other" }).ok, false);
  } finally {
    dom.window.close();
  }
});

test("a suggestion list inside a field's label is clicked as the observed list, not the label", () => {
  // Run 1792: Lever renders the location suggestions inside the field's <label>; the actionable
  // ancestor of the hit was the whole label, so every click on a suggestion was refused as drift.
  const { dom, fn, pointFn } = loadInjectedClickWindow(`
    <ul><li><label><div class="application-label">Current location</div><div class="application-field">
      <input id="location-input" type="text"><div class="dropdown-container">
        <div class="dropdown-results" id="results"><div data-hit class="dropdown-location" id="location-0">Jersey City, NJ, USA</div></div>
      </div></div></label></li></ul>`);
  try {
    stubRects(dom, { "location-0": { left: 10, top: 100, width: 300, height: 30 } });
    const expected = {
      target_selector: "label > div:nth-of-type(2) > div > div:nth-of-type(1)",
      target_text: "Jersey City, NJ, USA",
      target_tag: "div",
      target_type: "",
      target_role: "",
      dispatch_path: "dom_activation",
      page_url: dom.window.location.href,
    };
    const probe = fn(100, 230, false, false, expected);
    assert.equal(probe.ok, true, JSON.stringify(probe));
    assert.equal(pointFn(100, 230, false, false, expected).ok, true);
    // A selector naming something that does not contain the hit is still drift.
    const elsewhere = { ...expected, target_selector: "#location-input", target_tag: "input", target_text: "Current location" };
    assert.equal(fn(100, 230, false, false, elsewhere).ok, false);
  } finally {
    dom.window.close();
  }
});


test("a credential is typed as real keystrokes and stops, clearing the field, when focus leaves it", async () => {
  const secret = "Hunter2!pass";
  const { dom, fn } = loadInjectedTypeControlWindow(
    `<label for="pw">Password</label><input id="pw" type="password"><input id="other" type="text">`,
  );
  try {
    const keys = [];
    dom.window.__testBeforeKey = (text) => keys.push(text);
    const typed = await fn(7, "#pw", secret, null, null, true);
    assert.equal(typed.ok, true);
    assert.equal(keys.join(""), secret, "every character went out as a key, case kept");
    assert.equal(dom.window.document.querySelector("#pw").value, secret);
    assert.doesNotMatch(JSON.stringify(typed), /Hunter2/);

    // Focus is stolen after three keys: the burst stops before the next key and the field is cleared.
    dom.window.document.querySelector("#pw").value = "";
    let sent = 0;
    dom.window.__testBeforeKey = () => {
      sent += 1;
      if (sent === 3) dom.window.document.querySelector("#other").focus();
    };
    const stolen = await fn(7, "#pw", secret, null, null, true);
    assert.equal(stolen.ok, false);
    assert.equal(stolen.failure_reason, "credential_replay_target_drift");
    assert.equal(dom.window.document.querySelector("#pw").value, "");
    // Focus moved during the third key's own dispatch: that one character landed in the other field,
    // and the burst stopped right after it (the per-key check runs before and after every key).
    assert.equal(dom.window.document.querySelector("#other").value.length, 1);
    assert.equal(sent, 3);

    // One strike per tab: the next credential on this tab is written atomically, with no keys.
    dom.window.document.querySelector("#pw").focus();
    let keyedAfterStrike = 0;
    dom.window.__testBeforeKey = () => { keyedAfterStrike += 1; };
    const atomic = await fn(7, "#pw", secret, null, null, true);
    assert.equal(atomic.ok, true);
    assert.equal(keyedAfterStrike, 0);
    assert.equal(dom.window.document.querySelector("#pw").value, secret);
  } finally {
    dom.window.close();
  }
});

// Runs 68-69 on SmartRecruiters: each screening question is a shadow-DOM component; the open dropdown of the first
// covers the second question, whose label is then the nearest text above the "No" option.
function smartRecruitersQuestions(dom, { ownerLabel = true } = {}) {
  const doc = dom.window.document;
  const formRoot = doc.getElementById("form").attachShadow({ mode: "open" });
  const question = (id, label, y, options) => {
    const host = doc.createElement("spl-input");
    host.id = id;
    host.setAttribute("data-y", String(y));
    formRoot.appendChild(host);
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `<label id="${id}-label" for="${id}-input" data-y="${y + 8}">${label}</label>`
      + `<input id="${id}-input" role="combobox" aria-controls="menu-${id}"${ownerLabel ? ` aria-labelledby="${id}-label"` : ""} data-y="${y + 56}">`;
    if (!options) return;
    const list = doc.createElement("div");
    list.id = `menu-${id}`;
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-labelledby", "spl-270");
    list.setAttribute("data-y", String(y + 144));
    root.appendChild(list);
    options.forEach((text, index) => {
      const option = doc.createElement("spl-select-option");
      option.setAttribute("data-y", String(y + 144 + index * 74));
      option.textContent = text;
      list.appendChild(option);
      option.attachShadow({ mode: "open" }).innerHTML =
        `<div role="option" class="c-spl-dropdown-item" data-y="${y + 144 + index * 74}"><slot></slot></div>`;
    });
  };
  question("question_b92", "Have you ever worked at ServiceNow in any capacity? *", 290, ["Yes", "No"]);
  question("question_1b3", "If applicable, would you consider relocating for a role with ServiceNow?", 450, null);
}

test("a popup option is named after the field that owns its list, not the next question under the open list", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`<sr-form id="form" data-y="0"></sr-form>`);
  smartRecruitersQuestions(dom);
  const options = fn().controls.filter((control) => control.role === "option");
  const no = options.find((control) => control.text === "No");
  const yes = options.find((control) => control.text === "Yes");
  assert.ok(no && yes, `options read by their slotted text: ${JSON.stringify(options.map((o) => o.text))}`);
  assert.match(no.context_text, /^Have you ever worked at ServiceNow/);
  assert.doesNotMatch(no.context_text, /relocat/);
  assert.match(yes.context_text, /^Have you ever worked at ServiceNow/);
});

test("an option whose list has no named owner keeps the position-based context", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`<sr-form id="form" data-y="0"></sr-form>`);
  smartRecruitersQuestions(dom, { ownerLabel: false });
  const options = fn().controls.filter((control) => control.role === "option");
  assert.equal(options.length, 2);
  for (const option of options) assert.notEqual(option.context_text, undefined);
});

test("a light-DOM popup option takes its combobox's label as context", () => {
  const { fn } = loadVisionEvidenceWindow(`
    <label id="field-24-label" for="field-24" data-y="80">Pronouns</label>
    <div id="field-24" role="combobox" aria-controls="field-24-list" aria-labelledby="field-24-label" data-y="110">Search</div>
    <label data-y="140">Preferred name</label>
    <ul id="field-24-list" role="listbox" aria-labelledby="field-24-label" data-y="150">
      <li id="field-24-list-option-0" role="option" data-y="160">She/her/hers</li>
      <li id="field-24-list-option-1" role="option" data-y="200">He/him/his</li>
    </ul>
  `);
  const option = fn().controls.find((control) => control.id === "field-24-list-option-1");
  assert.equal(option.context_text, "Pronouns");
});

test("custom radios read their own shadow text and take their question from the group label", () => {
  // Run 73 on SmartRecruiters: spl-radio hosts carry their "Yes"/"No" inside a shadow root, so the second radio's
  // nearest text was the first one's "Yes" and the selected "No" read as "Yes".
  const { dom, fn } = loadVisionEvidenceWindow(`
    <span id="q-label" data-y="340">Is your current employer a customer of ServiceNow? *</span>
    <fieldset aria-labelledby="q-label" data-y="360">
      <spl-radio id="r-yes" role="radio" aria-checked="false" data-y="370"></spl-radio>
      <spl-radio id="r-no" role="radio" aria-checked="true" data-y="394"></spl-radio>
    </fieldset>
  `);
  const doc = dom.window.document;
  doc.getElementById("r-yes").attachShadow({ mode: "open" }).innerHTML = `<span data-y="370">Yes</span>`;
  doc.getElementById("r-no").attachShadow({ mode: "open" }).innerHTML = `<span data-y="394">No</span>`;
  const controls = fn().controls;
  const yes = controls.find((control) => control.id === "r-yes");
  const no = controls.find((control) => control.id === "r-no");
  // `text` stays what the click-time identity checks read (run 74); the visible label is shown as derived_label.
  assert.notEqual(no.text, "Yes");
  assert.equal(yes.derived_label, "Yes");
  assert.equal(no.derived_label, "No");
  assert.equal(no.context_text, "Is your current employer a customer of ServiceNow? *");
  assert.equal(yes.context_text, "Is your current employer a customer of ServiceNow? *");
});

// Run 75 on SmartRecruiters (ServiceNow): Spotlight components nest their option text through two slots inside
// templates full of whitespace, a field's <label> slots its name in beside a bare "*", and an icon's aria-label
// holds a double space. Each made the observer and the action probes disagree.
function serviceNowSelfIdField(dom) {
  const doc = dom.window.document;
  const formRoot = doc.getElementById("form").attachShadow({ mode: "open" });
  const host = doc.createElement("spl-select");
  host.id = "question_eth";
  host.setAttribute("data-y", "300");
  formRoot.appendChild(host);
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `<input id="question_eth-input" role="combobox" aria-controls="menu-eth" data-y="310">`
    + `<spl-icon aria-label="Clear  value" role="button" data-y="310" data-width="24"></spl-icon>`
    + `<div id="menu-eth" role="listbox" data-y="360"></div>`;
  root.querySelector("spl-icon").attachShadow({ mode: "open" }).innerHTML =
    `<svg data-y="312"><path id="clear-path" data-y="314"></path></svg>`;
  const list = root.getElementById("menu-eth");
  ["American Indian or Alaska Native", "Asian", "Black or African American"].forEach((text, index) => {
    const option = doc.createElement("spl-select-option");
    option.setAttribute("data-y", String(360 + index * 74));
    option.textContent = text;
    list.appendChild(option);
    option.attachShadow({ mode: "open" }).innerHTML =
      `\n  <spl-dropdown-item data-y="${360 + index * 74}">\n    <slot></slot>\n  </spl-dropdown-item>\n`;
    option.shadowRoot.querySelector("spl-dropdown-item").attachShadow({ mode: "open" }).innerHTML =
      `\n  <div role="option" class="c-spl-dropdown-item" data-y="${360 + index * 74}">\n    <slot></slot>\n  </div>\n`;
  });
  const sigHost = doc.createElement("spl-input");
  sigHost.id = "question_sig";
  sigHost.setAttribute("data-y", "600");
  sigHost.innerHTML = `<span slot="label">Name (Signature Field):</span>`;
  formRoot.appendChild(sigHost);
  sigHost.attachShadow({ mode: "open" }).innerHTML =
    `<label id="question_sig-label" for="question_sig" data-y="600">\n  <slot name="label"></slot>\n  <span>*</span>\n</label>`
    + `<input id="question_sig" type="text" data-y="630">`;
  return { formRoot, host, sigHost };
}

test("an option whose own text is template whitespace around nested slots is shown by its slotted words", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`<sr-form id="form" data-y="0"></sr-form>`);
  serviceNowSelfIdField(dom);
  const options = fn().controls.filter((control) => control.role === "option").sort((a, b) => a.y - b.y);
  assert.deepEqual(
    Array.from(options, (option) => option.derived_label),
    ["American Indian or Alaska Native", "Asian", "Black or African American"],
  );
  // `text` stays what click-time identity reads for these rows; the label is marked as the row's own words.
  for (const option of options) {
    assert.equal(option.text, "");
    assert.equal(option.derived_label_visible, true);
  }
});

test("typing into a shadow field whose label slots its name in keeps the observed name", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`<sr-form id="form" data-y="0"></sr-form>`);
  const { sigHost } = serviceNowSelfIdField(dom);
  const control = fn().controls.find((candidate) => candidate.id === "question_sig" && candidate.tag === "input");
  assert.ok(control);
  assert.equal(control.text, "Name (Signature Field): *");
  const focus = dom.window.eval(`(${extractFunctionSource("focusControlForTypeInPage")})`)(
    control.selector,
    {
      target_selector: control.selector,
      target_text: control.text,
      target_tag: "input",
      target_type: "text",
      target_role: "",
      dispatch_path: "dom_activation",
      page_url: dom.window.location.href,
    },
  );
  assert.equal(focus.ok, true, JSON.stringify(focus));
  assert.equal(dom.window.__lumenDeepActiveElement(), sigHost.shadowRoot.getElementById("question_sig"));
});

test("a selector whose attribute value holds a double space still binds its control at the point", () => {
  const { dom, fn } = loadVisionEvidenceWindow(`<sr-form id="form" data-y="0"></sr-form>`);
  const { host } = serviceNowSelfIdField(dom);
  const icon = host.shadowRoot.querySelector("spl-icon");
  const control = fn().controls.find((candidate) => candidate.tag === "spl-icon");
  assert.ok(control, "the clear icon is listed");
  assert.match(control.selector, /aria-label="Clear {2}value"/);
  // The point lands on the drawing inside the icon's own shadow root.
  dom.window.document.elementFromPoint = () => icon.shadowRoot.getElementById("clear-path");
  const center = dom.window.eval(`(${extractFunctionSource("visionControlCenterInPage")})`)(control.selector, false, true);
  assert.equal(center.ok, true, JSON.stringify(center));
  const info = dom.window.eval(`(${extractFunctionSource("visionPointInfoInPage")})`)(
    center.img_x,
    center.img_y,
    false,
    false,
    { ...center, target_selector: control.selector },
  );
  assert.equal(info.ok, true, JSON.stringify(info));
  assert.equal(info.target_selector, control.selector);
});
