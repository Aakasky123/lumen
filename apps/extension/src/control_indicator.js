(() => {
  const ROOT_ID = "lumen-active-control-indicator";
  const INSTALL_KEY = "__lumenControlIndicatorInstalled";
  const SETTER_KEY = "__lumenSetControlIndicator";

  if (globalThis[INSTALL_KEY] && typeof globalThis[SETTER_KEY] === "function") {
    try {
      chrome.runtime.sendMessage({ type: "LUMEN_CONTROL_INDICATOR_READY" }, (state) => {
        if (!chrome.runtime.lastError && state) globalThis[SETTER_KEY](state);
      });
    } catch (_) {
      // The background worker can still inject an immediate state update.
    }
    return;
  }

  let leaseTimer = null;
  let currentState = { active: false, capture_hidden: false, expires_at: 0 };

  function clearLeaseTimer() {
    if (leaseTimer == null) return;
    clearTimeout(leaseTimer);
    leaseTimer = null;
  }

  function removeIndicator() {
    const existing = document.getElementById(ROOT_ID);
    if (existing) existing.remove();
  }

  function setImportantStyle(node, property, value) {
    node.style.setProperty(property, value, "important");
  }

  function createIndicator() {
    const host = document.createElement("div");
    host.id = ROOT_ID;
    host.setAttribute("data-lumen-control-indicator", "true");
    host.setAttribute("aria-hidden", "true");
    setImportantStyle(host, "all", "initial");
    setImportantStyle(host, "position", "fixed");
    setImportantStyle(host, "inset", "0");
    setImportantStyle(host, "width", "100vw");
    setImportantStyle(host, "height", "100vh");
    setImportantStyle(host, "z-index", "2147483647");
    setImportantStyle(host, "pointer-events", "none");
    setImportantStyle(host, "user-select", "none");
    setImportantStyle(host, "contain", "strict");
    setImportantStyle(host, "display", "block");

    // The in-page indicator renders NOTHING: the desktop-level overlay owns the one
    // visible "Lumen is in control" pill and edge glow, and the cursor-attached chip
    // narrates at the action. This host stays mounted as the invisible lease anchor
    // so the tab-ownership/capture-hidden machinery keeps its exact contract.
    host.attachShadow({ mode: "closed" });
    return host;
  }

  function mountIndicator(captureHidden) {
    let host = document.getElementById(ROOT_ID);
    if (!host || host.getAttribute("data-lumen-control-indicator") !== "true") {
      if (host) host.remove();
      host = createIndicator();
      (document.documentElement || document.body)?.appendChild(host);
    }
    if (!host) return { ok: false, visible: false, reason: "document_root_unavailable" };
    setImportantStyle(host, "display", captureHidden ? "none" : "block");
    return { ok: true, visible: !captureHidden };
  }

  function scheduleLeaseExpiry(expiresAt) {
    clearLeaseTimer();
    const remaining = Number(expiresAt || 0) - Date.now();
    if (remaining <= 0) {
      currentState = { active: false, capture_hidden: false, expires_at: 0 };
      removeIndicator();
      return;
    }
    leaseTimer = setTimeout(() => {
      currentState = { active: false, capture_hidden: false, expires_at: 0 };
      removeIndicator();
    }, Math.min(remaining + 25, 2_147_000_000));
  }

  function setIndicator(nextState = {}) {
    const active = nextState.active === true;
    const expiresAt = Number(nextState.expires_at || 0);
    const captureHidden = nextState.capture_hidden === true;
    if (!active || !Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
      currentState = { active: false, capture_hidden: false, expires_at: 0 };
      clearLeaseTimer();
      removeIndicator();
      return { ok: true, visible: false };
    }

    currentState = {
      active: true,
      capture_hidden: captureHidden,
      expires_at: expiresAt,
    };
    const result = mountIndicator(captureHidden);
    scheduleLeaseExpiry(expiresAt);
    return result;
  }

  Object.defineProperty(globalThis, INSTALL_KEY, {
    configurable: true,
    value: true,
  });
  Object.defineProperty(globalThis, SETTER_KEY, {
    configurable: true,
    value: setIndicator,
  });

  const observer = new MutationObserver(() => {
    if (
      currentState.active
      && currentState.expires_at > Date.now()
      && !document.getElementById(ROOT_ID)
    ) {
      mountIndicator(currentState.capture_hidden);
    }
  });
  if (document.documentElement) {
    observer.observe(document.documentElement, { childList: true });
  }

  try {
    chrome.runtime.sendMessage({ type: "LUMEN_CONTROL_INDICATOR_READY" }, (state) => {
      if (chrome.runtime.lastError || !state) return;
      setIndicator(state);
    });
  } catch (_) {
    // Programmatic injection from the worker will immediately provide state.
  }
})();
