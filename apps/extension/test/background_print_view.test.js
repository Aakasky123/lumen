const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "src", "background.js"), "utf8");

function fnSource(name) {
  const asyncStart = SRC.indexOf(`async function ${name}`);
  const start = asyncStart >= 0 ? asyncStart : SRC.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `${name} not found`);
  const bodyStart = SRC.indexOf("{", start);
  let depth = 0;
  for (let i = bodyStart; i < SRC.length; i += 1) {
    if (SRC[i] === "{") depth += 1;
    if (SRC[i] === "}") depth -= 1;
    if (depth === 0) return SRC.slice(start, i + 1);
  }
  throw new Error("unclosed");
}

const BASE_HTML = `
  <h1>Ticket</h1>
  <p>Shared line</p>
  <label>Account PIN <input id="pin" type="password" value="hunter22secret"></label>
  <p id="echo">hunter22secret</p>
  <div id="slip" class="print-only">Reference number: 48213</div>
  <style>.print-only { display: none; }</style>`;

// A page whose text differs by media. The CDP stub flips a class on <html> the page's CSS keys off.
function harness({ html = BASE_HTML, failPrintRead = false, failEmulate = false, maskContext = null, tokenAfterPrint = null } = {}) {
  const dom = new JSDOM(`<!doctype html><body>${html}</body>`, { url: "https://example.test/p", runScripts: "outside-only" });
  Object.defineProperty(dom.window, "innerHeight", { value: 900, configurable: true });
  Object.defineProperty(dom.window, "innerWidth", { value: 1200, configurable: true });
  dom.window.Element.prototype.getBoundingClientRect = function () {
    // No layout in jsdom: an element under a display:none ancestor has no box.
    for (let el = this; el && el.nodeType === 1; el = el.parentElement) {
      if (dom.window.getComputedStyle(el).display === "none") return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0 };
    }
    return { left: 0, top: 0, right: 200, bottom: 20, width: 200, height: 20, x: 0, y: 0 };
  };
  const calls = [];
  const state = { media: "" };
  const visionPageEvidence = dom.window.eval(`(${fnSource("_visionPageEvidence")})`);
  const chrome = {
    scripting: {
      executeScript: async ({ func, args }) => {
        if (state.media === "print" && failPrintRead) throw new Error("read blew up");
        void func;
        const out = visionPageEvidence(...(args || []));
        if (state.media === "print" && tokenAfterPrint) out.document_lifecycle_token = tokenAfterPrint;
        return [{ result: out }];
      },
    },
  };
  const withTimeout = async (p) => p;
  const errorMessage = (e) => String((e && e.message) || e);
  const nativeDialogOpenFor = () => false;
  const acquireDebugger = async () => { calls.push("attach"); };
  const releaseDebugger = () => { calls.push("release"); };
  const _cdp = async (_t, method, params) => {
    calls.push(`${method}:${params.media}`);
    state.media = params.media;
    // Print media: the page's print stylesheet shows the slip.
    dom.window.document.querySelector("style").textContent =
      params.media === "print" ? ".print-only { display: block !important; }" : ".print-only { display: none; }";
    if (failEmulate && params.media === "print") throw new Error("emulate failed");
  };
  const sensitiveScreenshotMaskContextForTab = () => maskContext;
  const normalizeSensitiveScreenshotSelectors = (value) => (Array.isArray(value) ? value : value ? [String(value)] : []);
  const PRINT_VIEW_TEXT_CAP = 4000;
  const PRINT_VIEW_READ_CHARS = 20000;
  const _visionPageEvidence = (...args) => visionPageEvidence(...args);
  const printOnlyLines = eval(`(${fnSource("printOnlyLines")})`);
  const documentReadMaskSelectors = eval(`(${fnSource("documentReadMaskSelectors")})`);
  const runVisionReadPrintView = eval(`(${fnSource("runVisionReadPrintView")})`);
  void [chrome, withTimeout, errorMessage, nativeDialogOpenFor, acquireDebugger, releaseDebugger, _cdp,
    sensitiveScreenshotMaskContextForTab, normalizeSensitiveScreenshotSelectors, PRINT_VIEW_TEXT_CAP,
    PRINT_VIEW_READ_CHARS, _visionPageEvidence, printOnlyLines, documentReadMaskSelectors];
  return { dom, calls, state, run: (params = null) => runVisionReadPrintView(7, params) };
}

test("print_only_text holds the lines print adds; the rendering is restored afterwards", async () => {
  const h = harness();
  const res = await h.run();
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.match(res.print_only_text, /Reference number: 48213/);
  assert.doesNotMatch(res.print_only_text, /Shared line/);
  assert.match(res.print_text, /Shared line/);
  assert.equal(res.url, "https://example.test/p");
  assert.ok(res.document_lifecycle_token);
  assert.deepEqual(h.calls, ["attach", "Emulation.setEmulatedMedia:print", "Emulation.setEmulatedMedia:", "release"]);
});

test("a sensitive control's value is masked in the print text", async () => {
  const h = harness();
  const res = await h.run();
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.match(res.print_text, /REDACTED/);
  assert.doesNotMatch(res.print_text, /hunter22secret/);
  assert.doesNotMatch(res.print_only_text, /hunter22secret/);
});

test("a lost required sensitive-mask binding fails closed before anything is read", async () => {
  const h = harness({ maskContext: { required: true, selectors: [] } });
  const res = await h.run();
  assert.equal(res.ok, false);
  assert.equal(res.failure_reason, "sensitive_mask_binding_lost");
  assert.deepEqual(h.calls, []);
  const h2 = harness();
  const res2 = await h2.run({ required_sensitive_mask_binding: null });
  assert.equal(res2.failure_reason, "sensitive_mask_page_context_required");
  assert.deepEqual(h2.calls, []);
});

test("media is restored when the print read throws", async () => {
  const h = harness({ failPrintRead: true });
  const res = await h.run();
  assert.equal(res.ok, false);
  assert.equal(res.failure_reason, "print_view_unavailable");
  assert.equal(h.state.media, "");
  assert.ok(h.calls.includes("Emulation.setEmulatedMedia:"));
});

test("media is restored when emulation itself fails after applying", async () => {
  const h = harness({ failEmulate: true });
  const res = await h.run();
  assert.equal(res.ok, false);
  assert.equal(h.state.media, "");
});

test("a document that changed between the two reads is refused", async () => {
  const h = harness({ tokenAfterPrint: "another-document" });
  const res = await h.run();
  assert.equal(res.ok, false);
  assert.equal(h.state.media, "");
});

test("printOnlyLines keeps order, drops screen lines and repeats", () => {
  const printOnlyLines = eval(`(${fnSource("printOnlyLines")})`);
  assert.equal(printOnlyLines("A\n  B \nC", "A\nX\nB\nY\nX"), "X\nY");
});

test("each text is capped at 4000 characters and says so", async () => {
  const h = harness({ html: `<p>x</p><div class="print-only">${Array.from({ length: 800 }, (_, i) => `<p>line number ${i}</p>`).join("")}</div><style>.print-only { display: none; }</style>` });
  const res = await h.run();
  assert.equal(res.ok, true, JSON.stringify(res));
  assert.equal(res.print_text.length, 4000);
  assert.equal(res.print_only_text.length, 4000);
  assert.equal(res.print_text_truncated, true);
  assert.equal(res.print_only_text_truncated, true);
});
