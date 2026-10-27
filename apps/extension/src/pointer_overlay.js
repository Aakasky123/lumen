// Lumen virtual pointer overlay.
//
// Purely visual: shows the user where Lumen is about to click, type, or scroll on the
// task page. Injected on demand by the background worker right before an action executes.
// It never intercepts input (pointer-events: none), never reads page content, and is hidden
// while any screenshot is captured so the brain's observations and the coordinate pixel
// fingerprints stay byte-identical to what a user-free viewport would produce.
(() => {
  const ROOT_ID = "lumen-pointer-overlay";
  const INSTALL_KEY = "__lumenPointerOverlayInstalled";
  const COMMAND_KEY = "__lumenPointerOverlay";
  const IDLE_FADE_MS = 4000;
  const FADE_OUT_MS = 480;
  const MOVE_MS = 280;
  const RIPPLE_MS = 560;
  const LABEL_LIMIT = 90;

  if (globalThis[INSTALL_KEY] && typeof globalThis[COMMAND_KEY] === "function") return;

  let refs = null; // { host, shadow, cursor, labelChip, labelDot, labelText, scrollHint }
  let lastX = null;
  let lastY = null;
  let captureHidden = false;
  let shown = false;
  let idleTimer = null;
  let fadeTimer = null;
  // The pill's persistent narration line (what Lumen is doing right now); transient
  // gesture hints ("typing…") briefly take its place and then it returns.
  let statusText = "";
  let transientTimer = null;

  const reducedMotion = (() => {
    try {
      return typeof globalThis.matchMedia === "function"
        && globalThis.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (_) {
      return false;
    }
  })();

  function setImportantStyle(node, property, value) {
    node.style.setProperty(property, value, "important");
  }

  function clampToViewport(value, max) {
    const limit = Number(max || 0);
    if (!Number.isFinite(value)) return 0;
    if (!Number.isFinite(limit) || limit <= 4) return Math.max(2, value);
    return Math.min(Math.max(2, value), limit - 2);
  }

  function clearTimers() {
    if (idleTimer != null) {
      clearTimeout(idleTimer);
      idleTimer = null;
    }
    if (fadeTimer != null) {
      clearTimeout(fadeTimer);
      fadeTimer = null;
    }
    if (transientTimer != null) {
      clearTimeout(transientTimer);
      transientTimer = null;
    }
  }

  function removeOverlay() {
    clearTimers();
    const existing = document.getElementById(ROOT_ID);
    if (existing) existing.remove();
    refs = null;
    shown = false;
  }

  function scheduleIdleFade() {
    if (idleTimer != null) {
      clearTimeout(idleTimer);
      idleTimer = null;
    }
    if (fadeTimer != null) {
      clearTimeout(fadeTimer);
      fadeTimer = null;
    }
    if (refs) refs.cursor.classList.remove("parked");
    idleTimer = setTimeout(() => {
      idleTimer = null;
      if (!refs) return;
      if (statusText) {
        // While a run narrates through the pill, the worker stays visibly present:
        // park (dim) at the last position instead of disappearing between actions.
        refs.cursor.classList.add("parked");
        return;
      }
      refs.cursor.classList.add("faded");
      fadeTimer = setTimeout(() => {
        fadeTimer = null;
        removeOverlay();
      }, FADE_OUT_MS + 40);
    }, IDLE_FADE_MS);
  }

  function createOverlay() {
    const host = document.createElement("div");
    host.id = ROOT_ID;
    host.setAttribute("data-lumen-pointer-overlay", "true");
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
    setImportantStyle(host, "overflow", "hidden");
    setImportantStyle(host, "display", captureHidden ? "none" : "block");

    const shadow = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = `
      :host {
        color-scheme: dark;
        font-family: "Segoe UI Variable Text", "Segoe UI", Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, sans-serif;
      }
      .cursor {
        position: absolute;
        left: 0;
        top: 0;
        width: 0;
        height: 0;
        opacity: 0;
        transition:
          transform ${reducedMotion ? 0 : MOVE_MS}ms cubic-bezier(0.22, 0.72, 0.18, 1),
          opacity 220ms ease;
        will-change: transform;
      }
      .cursor.shown { opacity: 1; }
      .cursor.faded { opacity: 0; transition: opacity ${FADE_OUT_MS}ms ease; }
      .cursor.parked { opacity: 0.6; }
      .arrow {
        position: absolute;
        left: -2px;
        top: -2px;
        filter:
          drop-shadow(0 1px 2px rgba(4, 22, 16, 0.6))
          drop-shadow(0 0 9px rgba(94, 230, 189, 0.6));
      }
      .cursor.pressed .arrow { transform: scale(0.86); transform-origin: 4px 4px; }
      .label {
        position: absolute;
        left: 16px;
        top: 22px;
        display: inline-flex;
        align-items: center;
        gap: 6px;
        max-width: min(320px, 60vw);
        padding: 4px 10px 4px 8px;
        border: 1px solid rgba(94, 230, 189, 0.45);
        border-radius: 999px;
        background: rgba(9, 13, 11, 0.94);
        box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.3), 0 6px 18px rgba(0, 0, 0, 0.3);
        color: #eef6f2;
        font-size: 11px;
        font-weight: 500;
        letter-spacing: 0.01em;
        line-height: 1.2;
        white-space: nowrap;
      }
      .label:empty { display: none; }
      .label .text { overflow: hidden; text-overflow: ellipsis; }
      .label .dot {
        width: 6px;
        height: 6px;
        flex: 0 0 6px;
        border-radius: 999px;
        background: #5ee6bd;
        box-shadow: 0 0 8px rgba(94, 230, 189, 0.9);
        animation: lumen-dot-pulse 1.4s ease-in-out infinite;
      }
      @keyframes lumen-dot-pulse {
        0%, 100% { transform: scale(1); opacity: 1; }
        50% { transform: scale(0.62); opacity: 0.55; }
      }
      /* Near the right viewport edge the pill swings to the cursor's left instead of clipping. */
      .cursor.flip .label {
        left: auto;
        right: 16px;
      }
      .ripple {
        position: absolute;
        width: 44px;
        height: 44px;
        margin: -22px 0 0 -22px;
        border: 2.5px solid rgba(94, 230, 189, 0.95);
        border-radius: 999px;
        box-shadow: 0 0 0 1px rgba(4, 22, 16, 0.5), 0 0 18px rgba(94, 230, 189, 0.55);
        transform: scale(0.2);
        opacity: 0.95;
        animation: lumen-ripple ${RIPPLE_MS}ms ease-out forwards;
      }
      @keyframes lumen-ripple {
        0% { transform: scale(0.2); opacity: 0.95; }
        70% { opacity: 0.45; }
        100% { transform: scale(1.25); opacity: 0; }
      }
      .scroll-hint {
        position: absolute;
        left: -9px;
        display: none;
        color: #5ee6bd;
        font-size: 14px;
        font-weight: 700;
        text-shadow: 0 0 2px rgba(4, 22, 16, 0.9), 0 0 8px rgba(94, 230, 189, 0.7);
        animation: lumen-scroll-nudge 640ms ease-in-out 2;
      }
      .scroll-hint.up { top: -30px; display: block; }
      .scroll-hint.down { top: 16px; display: block; }
      .scroll-hint.up::before { content: "▲"; }
      .scroll-hint.down::before { content: "▼"; }
      @keyframes lumen-scroll-nudge {
        0%, 100% { transform: translateY(0); }
        50% { transform: translateY(var(--lumen-nudge, 6px)); }
      }
      @media (prefers-reduced-motion: reduce) {
        .cursor { transition: opacity 220ms ease; }
        .ripple { animation-duration: 200ms; }
        .scroll-hint { animation: none; }
        .label .dot { animation: none; }
      }
    `;

    const cursor = document.createElement("div");
    cursor.className = "cursor";
    const arrow = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    arrow.setAttribute("class", "arrow");
    arrow.setAttribute("width", "22");
    arrow.setAttribute("height", "22");
    arrow.setAttribute("viewBox", "0 0 24 24");
    const arrowPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
    arrowPath.setAttribute("d", "M5 2.5 L5 18.5 L9.2 14.9 L11.8 20.8 L14.6 19.5 L12 13.8 L17.5 13.4 Z");
    arrowPath.setAttribute("fill", "#0a120f");
    arrowPath.setAttribute("stroke", "#f2fbf7");
    arrowPath.setAttribute("stroke-width", "1.4");
    arrowPath.setAttribute("stroke-linejoin", "round");
    arrow.appendChild(arrowPath);
    const labelChip = document.createElement("span");
    labelChip.className = "label";
    const labelDot = document.createElement("span");
    labelDot.className = "dot";
    const labelText = document.createElement("span");
    labelText.className = "text";
    const scrollHint = document.createElement("span");
    scrollHint.className = "scroll-hint";
    cursor.append(arrow, labelChip, scrollHint);
    shadow.append(style, cursor);
    (document.documentElement || document.body)?.appendChild(host);
    return { host, shadow, cursor, labelChip, labelDot, labelText, scrollHint };
  }

  function ensureOverlay() {
    const existing = document.getElementById(ROOT_ID);
    if (!refs || !existing || existing.getAttribute("data-lumen-pointer-overlay") !== "true") {
      if (existing) existing.remove();
      clearTimers();
      refs = createOverlay();
      shown = false;
    }
    return refs;
  }

  function applyPosition(x, y, instant) {
    const view = refs.host.ownerDocument.defaultView || globalThis;
    const clampedX = clampToViewport(Number(x), view.innerWidth);
    const clampedY = clampToViewport(Number(y), view.innerHeight);
    refs.cursor.classList.toggle("flip", clampedX > (Number(view.innerWidth) || 800) - 360);
    const firstShow = !shown || lastX == null || lastY == null;
    if (firstShow || instant || reducedMotion) {
      // Appear at (or jump to) the target instead of gliding across the screen
      // from an unrelated stale position.
      refs.cursor.style.setProperty("transition-property", "opacity");
      refs.cursor.style.setProperty("transform", `translate(${clampedX}px, ${clampedY}px)`);
      void refs.cursor.offsetWidth;
      refs.cursor.style.removeProperty("transition-property");
    } else {
      refs.cursor.style.setProperty("transform", `translate(${clampedX}px, ${clampedY}px)`);
    }
    refs.cursor.classList.remove("faded");
    refs.cursor.classList.add("shown");
    shown = true;
    lastX = clampedX;
    lastY = clampedY;
    return { x: clampedX, y: clampedY };
  }

  function setLabel(text) {
    const next = String(text || "").slice(0, LABEL_LIMIT);
    refs.labelChip.replaceChildren();
    if (next) {
      refs.labelText.textContent = next;
      refs.labelChip.append(refs.labelDot, refs.labelText);
    }
  }

  function setStatus(text) {
    // Stored ONLY as the "a run is narrating" signal (it keeps the pointer parked
    // between actions). The narration text itself renders on the desktop-level pill —
    // one voice, never two; the cursor shows only momentary gesture hints.
    statusText = String(text || "").slice(0, LABEL_LIMIT).trim();
  }

  function showTransient(text) {
    if (transientTimer != null) clearTimeout(transientTimer);
    setLabel(text);
    transientTimer = setTimeout(() => {
      transientTimer = null;
      if (refs) setLabel("");
    }, 1600);
  }

  function spawnRipple(x, y) {
    const ripple = document.createElement("span");
    ripple.className = "ripple";
    ripple.style.setProperty("left", `${x}px`);
    ripple.style.setProperty("top", `${y}px`);
    refs.shadow.appendChild(ripple);
    // animationend does not fire in every embedding; a timeout keeps the shadow tree bounded.
    setTimeout(() => ripple.remove(), RIPPLE_MS + 120);
  }

  function pressCursor() {
    refs.cursor.classList.add("pressed");
    setTimeout(() => {
      if (refs) refs.cursor.classList.remove("pressed");
    }, 160);
  }

  function showScrollHint(dy) {
    const down = Number(dy || 0) >= 0;
    refs.scrollHint.classList.remove("up", "down");
    refs.scrollHint.style.setProperty("--lumen-nudge", down ? "6px" : "-6px");
    void refs.scrollHint.offsetWidth;
    refs.scrollHint.classList.add(down ? "down" : "up");
    setTimeout(() => {
      if (refs) refs.scrollHint.classList.remove("up", "down");
    }, 1400);
  }

  function receipt(extra = {}) {
    return {
      ok: true,
      visible: Boolean(shown && !captureHidden),
      x: lastX,
      y: lastY,
      ...extra,
    };
  }

  function command(rawCommand = {}) {
    try {
      const op = String(rawCommand.op || "");
      if (op === "set_capture_hidden") {
        captureHidden = rawCommand.hidden === true;
        const existing = document.getElementById(ROOT_ID);
        if (existing) setImportantStyle(existing, "display", captureHidden ? "none" : "block");
        return receipt();
      }
      if (op === "hide") {
        statusText = "";
        removeOverlay();
        return { ok: true, visible: false, x: lastX, y: lastY };
      }
      if (op === "move" || op === "click" || op === "type" || op === "scroll") {
        ensureOverlay();
        // The persistent narration line rides on every gesture so the pill keeps
        // describing what Lumen is doing wherever it is doing it.
        setStatus(rawCommand.status);
        const hasPoint = Number.isFinite(Number(rawCommand.x)) && Number.isFinite(Number(rawCommand.y));
        let position = { x: lastX, y: lastY };
        if (hasPoint) {
          position = applyPosition(Number(rawCommand.x), Number(rawCommand.y), op === "click" && shown === false);
        } else if (op === "scroll" && lastX == null) {
          const view = refs.host.ownerDocument.defaultView || globalThis;
          position = applyPosition((view.innerWidth || 800) * 0.5, (view.innerHeight || 600) * 0.5, true);
        } else if (!shown) {
          return receipt({ skipped: "no_position" });
        }
        if (op === "click") {
          pressCursor();
          spawnRipple(position.x, position.y);
        } else if (op === "type") {
          showTransient("typing…");
        } else if (op === "scroll") {
          showScrollHint(rawCommand.dy);
        }
        scheduleIdleFade();
        return receipt();
      }
      return { ok: false, visible: Boolean(shown && !captureHidden), reason: "unknown_pointer_op" };
    } catch (error) {
      return { ok: false, visible: false, reason: String(error && error.message || error) };
    }
  }

  Object.defineProperty(globalThis, INSTALL_KEY, { configurable: true, value: true });
  Object.defineProperty(globalThis, COMMAND_KEY, { configurable: true, value: command });
})();
