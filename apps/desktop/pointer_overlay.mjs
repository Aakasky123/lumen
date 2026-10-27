// Desktop-level Lumen pointer overlay.
//
// Rendered inside the same protected, click-through, always-on-top windows as the control
// indicator, one per display. The Windows body publishes pointer gestures (click / type /
// scroll / key) with absolute physical screen coordinates; the host converts them to
// display-local DIP points and forwards them to window.__lumenDesktopPointer in the overlay
// page. Content protection keeps the pointer out of the body's own screenshots, so the
// brain's visual evidence never contains Lumen's own chrome. Rings are LUMEN mint with a dark
// hairline so they stay visible over light and dark apps.

const IDLE_FADE_MS = 4000;
const MOVE_MS = 280;
const RIPPLE_MS = 560;

export function pointerOverlayHtml() {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    * { box-sizing: border-box; }
    html, body {
      width: 100%;
      height: 100%;
      margin: 0;
      overflow: hidden;
      background: transparent;
      pointer-events: none;
      user-select: none;
      font-family: "Segoe UI Variable Text", "Segoe UI", Inter, ui-sans-serif, system-ui, sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    #cursor {
      position: fixed;
      left: 0;
      top: 0;
      opacity: 0;
      transition:
        transform ${MOVE_MS}ms cubic-bezier(0.22, 0.72, 0.18, 1),
        opacity 220ms ease;
      will-change: transform;
    }
    #cursor.shown { opacity: 1; }
    /* Lumen's cursor never disappears while it is working: idle parks it at the last
       position as a smaller, dimmed, gently pulsing halo until the next gesture. */
    #cursor.shown.parked { opacity: 0.6; }
    .halo {
      position: absolute;
      left: -17px;
      top: -17px;
      width: 34px;
      height: 34px;
      border: 2px solid rgba(94, 230, 189, 0.95);
      border-radius: 999px;
      background: rgba(94, 230, 189, 0.08);
      box-shadow:
        0 0 0 1px rgba(4, 22, 16, 0.6),
        inset 0 0 0 1px rgba(4, 22, 16, 0.35),
        0 0 16px rgba(94, 230, 189, 0.55);
      transition: transform 420ms ease;
    }
    #cursor.parked .halo {
      transform: scale(0.68);
      animation: lumen-park-pulse 2.6s ease-in-out infinite;
    }
    @keyframes lumen-park-pulse {
      0%, 100% { box-shadow: 0 0 0 1px rgba(4, 22, 16, 0.6), 0 0 8px rgba(94, 230, 189, 0.4); }
      50% { box-shadow: 0 0 0 1px rgba(4, 22, 16, 0.6), 0 0 20px rgba(94, 230, 189, 0.7); }
    }
    #label {
      position: absolute;
      left: 22px;
      top: 22px;
      display: inline-flex;
      align-items: center;
      gap: 7px;
      max-width: 340px;
      padding: 5px 11px 5px 9px;
      border: 1px solid rgba(94, 230, 189, 0.45);
      border-radius: 999px;
      background: rgba(9, 13, 11, 0.94);
      box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.3), 0 8px 20px rgba(0, 0, 0, 0.32);
      color: #eef6f2;
      font-size: 11.5px;
      font-weight: 500;
      line-height: 1.2;
      white-space: nowrap;
    }
    /* Near the right screen edge the pill swings to the cursor's left instead of clipping. */
    #cursor.flip #label {
      left: auto;
      right: 22px;
    }
    #label.hidden { display: none; }
    #label .dot {
      width: 6px;
      height: 6px;
      flex: 0 0 6px;
      border-radius: 999px;
      background: #5ee6bd;
      box-shadow: 0 0 8px rgba(94, 230, 189, 0.9);
      animation: lumen-dot-pulse 1.4s ease-in-out infinite;
    }
    #label #label-text {
      overflow: hidden;
      text-overflow: ellipsis;
    }
    #cursor.parked #label { opacity: 0.82; }
    @keyframes lumen-dot-pulse {
      0%, 100% { transform: scale(1); opacity: 1; }
      50% { transform: scale(0.62); opacity: 0.55; }
    }
    .ripple {
      position: fixed;
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
    .chevron {
      position: absolute;
      left: -8px;
      color: #5ee6bd;
      font-size: 14px;
      font-weight: 700;
      text-shadow: 0 0 2px rgba(4, 22, 16, 0.9), 0 0 8px rgba(94, 230, 189, 0.7);
      animation: lumen-nudge 640ms ease-in-out 2;
    }
    @keyframes lumen-nudge {
      0%, 100% { transform: translateY(0); }
      50% { transform: translateY(var(--lumen-nudge, 6px)); }
    }
    @media (prefers-reduced-motion: reduce) {
      #cursor { transition: opacity 220ms ease; }
      .ripple { animation-duration: 200ms; }
      .chevron { animation: none; }
      #cursor.parked .halo { animation: none; }
      #label .dot { animation: none; }
    }
  </style>
</head>
<body aria-hidden="true">
  <div id="cursor"><div class="halo"></div><span id="label" class="hidden"><span class="dot"></span><span id="label-text"></span></span></div>
  <script>
    (() => {
      const cursor = document.getElementById("cursor");
      const label = document.getElementById("label");
      const labelText = document.getElementById("label-text");
      let shown = false;
      let lastX = null;
      let lastY = null;
      let idleTimer = null;
      let transientTimer = null;

      function renderLabel(text) {
        const next = String(text || "").slice(0, 90).trim();
        labelText.textContent = next;
        label.classList.toggle("hidden", !next);
      }

      function showTransient(text) {
        if (transientTimer != null) clearTimeout(transientTimer);
        renderLabel(text);
        transientTimer = setTimeout(() => {
          transientTimer = null;
          renderLabel("");
        }, 1600);
      }

      function scheduleFade() {
        if (idleTimer != null) clearTimeout(idleTimer);
        cursor.classList.remove("parked");
        idleTimer = setTimeout(() => {
          // Park instead of hiding: Lumen stays visibly present at its last position
          // for the whole run; the overlay window closing is what removes the cursor.
          cursor.classList.add("parked");
        }, ${IDLE_FADE_MS});
      }

      function moveTo(x, y) {
        const clampedX = Math.min(Math.max(2, x), window.innerWidth - 2);
        const clampedY = Math.min(Math.max(2, y), window.innerHeight - 2);
        cursor.style.transform = "translate(" + clampedX + "px, " + clampedY + "px)";
        cursor.classList.toggle("flip", clampedX > window.innerWidth - 380);
        cursor.classList.add("shown");
        cursor.classList.remove("parked");
        shown = true;
        lastX = clampedX;
        lastY = clampedY;
        return { x: clampedX, y: clampedY };
      }

      function ripple(x, y) {
        const node = document.createElement("span");
        node.className = "ripple";
        node.style.left = x + "px";
        node.style.top = y + "px";
        document.body.appendChild(node);
        setTimeout(() => node.remove(), ${RIPPLE_MS} + 120);
      }

      function chevrons(down) {
        const node = document.createElement("span");
        node.className = "chevron";
        node.style.top = down ? "20px" : "-34px";
        node.style.setProperty("--lumen-nudge", down ? "6px" : "-6px");
        node.textContent = down ? "\\u25BC" : "\\u25B2";
        cursor.appendChild(node);
        setTimeout(() => node.remove(), 1400);
      }

      window.__lumenDesktopPointer = (event) => {
        try {
          const kind = String((event && event.kind) || "");
          const hasPoint = Number.isFinite(event && event.x) && Number.isFinite(event && event.y);
          let point = { x: lastX, y: lastY };
          if (hasPoint) point = moveTo(Number(event.x), Number(event.y));
          else if (!shown) return { ok: true, skipped: "no_position" };
          if (kind === "click") ripple(point.x, point.y);
          else if (kind === "type") showTransient("typing…");
          else if (kind === "scroll") chevrons(Number(event && event.dy || 0) >= 0);
          else if (kind === "key") showTransient(String((event && event.label) || "key").slice(0, 32));
          scheduleFade();
          return { ok: true, x: point.x, y: point.y };
        } catch (error) {
          return { ok: false, reason: String(error && error.message || error) };
        }
      };
    })();
  </script>
</body>
</html>`;
}

export function pointerOverlayDataUrl() {
  return `data:text/html;charset=utf-8,${encodeURIComponent(pointerOverlayHtml())}`;
}

// Route an absolute DIP point to the overlay entry whose display contains it. Falls back to
// the first entry (the primary display) so an event just outside every display still renders.
export function displayForPoint(entries, point) {
  const list = Array.isArray(entries) ? entries.filter((entry) => entry && entry.bounds) : [];
  if (!list.length) return null;
  const x = Number(point && point.x);
  const y = Number(point && point.y);
  const hit = list.find(({ bounds }) => (
    Number.isFinite(x)
    && Number.isFinite(y)
    && x >= bounds.x
    && x < bounds.x + bounds.width
    && y >= bounds.y
    && y < bounds.y + bounds.height
  ));
  return hit || list[0];
}
