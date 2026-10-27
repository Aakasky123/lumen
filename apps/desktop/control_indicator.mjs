// Lumen's floating activity pill + screen edge glow.
//
// Rendered in the same protected, click-through, always-on-top windows as the pointer
// overlay, one per display. The pill carries the LUMEN slab mark (its slabs shift while Lumen
// works) and mirrors the active run's live status line: the host polls
// /operator/desktop/status and forwards the current message to window.__lumenControlStatus,
// so the user always sees what Lumen is doing without opening the app. A "Keeping screen on"
// tag shows while the run holds the machine awake. The edge glow is mint with a dark inner
// hairline so it reads over light and dark apps alike. Content protection keeps the pill out
// of the body's own screenshots.

export function controlIndicatorHtml() {
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
    .frame {
      position: fixed;
      inset: 0;
      border: 3px solid rgba(52, 214, 168, 0.96);
      box-shadow:
        inset 0 0 0 1px rgba(4, 22, 16, 0.5),
        inset 0 0 28px 5px rgba(52, 214, 168, 0.5),
        inset 0 0 80px 4px rgba(52, 214, 168, 0.18);
    }
    .status {
      position: fixed;
      top: 14px;
      left: 50%;
      display: inline-flex;
      align-items: center;
      gap: 10px;
      max-width: min(62vw, 720px);
      transform: translateX(-50%);
      padding: 8px 16px 8px 12px;
      border: 1px solid rgba(94, 230, 189, 0.42);
      border-radius: 999px;
      background: rgba(9, 13, 11, 0.94);
      box-shadow:
        0 0 0 1px rgba(0, 0, 0, 0.35),
        0 12px 32px rgba(0, 0, 0, 0.38),
        0 0 22px rgba(94, 230, 189, 0.22);
      color: #eef6f2;
      font-size: 13px;
      font-weight: 500;
      line-height: 1.2;
      white-space: nowrap;
      animation: lumen-breathe 2.8s ease-in-out infinite;
    }
    .mark {
      display: flex;
      align-items: flex-end;
      gap: 2px;
      flex: 0 0 auto;
      height: 15px;
    }
    .mark span {
      width: 3px;
      height: 15px;
      border-radius: 1px;
      background: #5ee6bd;
      animation: lumen-pulse 1.35s cubic-bezier(0.45, 0, 0.25, 1) infinite;
    }
    .mark span:nth-child(2),
    .mark span:nth-child(3) {
      height: 14px;
      background: #3fbf99;
    }
    .mark span:nth-child(2) { animation-delay: 110ms; }
    .mark span:nth-child(3) { animation-delay: 220ms; }
    .mark span:nth-child(4) { animation-delay: 330ms; }
    .brand {
      flex: 0 0 auto;
      color: #5ee6bd;
      font: 600 11px/1 "Cascadia Mono", Consolas, ui-monospace, monospace;
      letter-spacing: 0.24em;
      text-transform: uppercase;
    }
    .divider {
      flex: 0 0 auto;
      width: 1px;
      height: 14px;
      background: rgba(238, 246, 242, 0.16);
    }
    #message {
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .awake {
      flex: 0 0 auto;
      padding: 3px 8px;
      border-radius: 999px;
      background: rgba(94, 230, 189, 0.12);
      color: #a8f0d8;
      font-size: 11px;
      font-weight: 500;
    }
    @keyframes lumen-pulse {
      0%, 100% { transform: translateY(0); opacity: 0.6; }
      30% { transform: translateY(-3px); opacity: 1; }
      60% { transform: translateY(1px); opacity: 0.85; }
    }
    @keyframes lumen-breathe {
      0%, 100% { box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.35), 0 12px 32px rgba(0, 0, 0, 0.38), 0 0 18px rgba(94, 230, 189, 0.18); }
      50% { box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.35), 0 12px 32px rgba(0, 0, 0, 0.38), 0 0 32px rgba(94, 230, 189, 0.4); }
    }
    @media (prefers-reduced-motion: reduce) {
      .status, .mark span { animation: none; }
    }
  </style>
</head>
<body aria-hidden="true">
  <div class="frame"></div>
  <div class="status"><span class="mark"><span></span><span></span><span></span><span></span></span><span class="brand">Lumen</span><span class="divider"></span><span id="message">Lumen is using your computer</span><span id="awake" class="awake" hidden>Keeping screen on</span></div>
  <script>
    (() => {
      const message = document.getElementById("message");
      const awake = document.getElementById("awake");
      // Live status line from the desktop host. textContent keeps arbitrary run
      // narration inert; the pill only ever renders it as plain text. keepAwake says
      // whether the run holds the power request that stops sleep and screensavers.
      window.__lumenControlStatus = (text, keepAwake) => {
        try {
          const next = String(text || "").slice(0, 120).trim();
          if (next && message.textContent !== next) message.textContent = next;
          awake.hidden = !keepAwake;
          return { ok: true };
        } catch (error) {
          return { ok: false, reason: String(error && error.message || error) };
        }
      };
    })();
  </script>
</body>
</html>`;
}

export function controlIndicatorDataUrl() {
  return `data:text/html;charset=utf-8,${encodeURIComponent(controlIndicatorHtml())}`;
}
