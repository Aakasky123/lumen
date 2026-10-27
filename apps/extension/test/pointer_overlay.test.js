const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { JSDOM } = require("jsdom");

const SOURCE = fs.readFileSync(
  path.join(__dirname, "..", "src", "pointer_overlay.js"),
  "utf8",
);
const BACKGROUND_SRC = fs.readFileSync(
  path.join(__dirname, "..", "src", "background.js"),
  "utf8",
);

function createPage() {
  const dom = new JSDOM("<!doctype html><html><body><main>Task page</main></body></html>", {
    runScripts: "dangerously",
    url: "https://example.test/task",
  });
  dom.window.eval(SOURCE);
  return dom;
}

test("pointer move mounts a click-through cursor overlay on the task page", () => {
  const dom = createPage();
  const result = dom.window.__lumenPointerOverlay({ op: "move", x: 120, y: 240 });

  assert.equal(result.ok, true);
  assert.equal(result.visible, true);
  assert.equal(result.x, 120);
  assert.equal(result.y, 240);

  const host = dom.window.document.getElementById("lumen-pointer-overlay");
  assert.ok(host);
  assert.equal(host.getAttribute("aria-hidden"), "true");
  assert.equal(host.style.getPropertyValue("pointer-events"), "none");
  assert.equal(host.style.getPropertyValue("display"), "block");
  assert.equal(host.shadowRoot, null, "the page cannot inspect the closed pointer surface");

  dom.window.close();
});

test("pointer coordinates clamp to the viewport", () => {
  const dom = createPage();
  const result = dom.window.__lumenPointerOverlay({ op: "move", x: -50, y: 99999 });
  assert.equal(result.ok, true);
  assert.equal(result.x, 2);
  assert.equal(result.y, dom.window.innerHeight - 2);
  dom.window.close();
});

test("capture suppression hides the pointer without losing its position", () => {
  const dom = createPage();
  dom.window.__lumenPointerOverlay({ op: "move", x: 200, y: 100 });

  const hidden = dom.window.__lumenPointerOverlay({ op: "set_capture_hidden", hidden: true });
  const host = dom.window.document.getElementById("lumen-pointer-overlay");
  assert.equal(hidden.ok, true);
  assert.equal(hidden.visible, false);
  assert.equal(hidden.x, 200);
  assert.equal(hidden.y, 100);
  assert.equal(host.style.getPropertyValue("display"), "none");

  const restored = dom.window.__lumenPointerOverlay({ op: "set_capture_hidden", hidden: false });
  assert.equal(restored.visible, true);
  assert.equal(host.style.getPropertyValue("display"), "block");

  dom.window.close();
});

test("click and scroll gestures acknowledge with the pointer kept visible", () => {
  const dom = createPage();
  const clicked = dom.window.__lumenPointerOverlay({ op: "click", x: 90, y: 60 });
  assert.equal(clicked.ok, true);
  assert.equal(clicked.visible, true);

  const scrolled = dom.window.__lumenPointerOverlay({ op: "scroll", dy: 400 });
  assert.equal(scrolled.ok, true);
  assert.equal(scrolled.x, 90, "scroll without a target keeps the cursor where it was");

  const typed = dom.window.__lumenPointerOverlay({ op: "type", x: 300, y: 200 });
  assert.equal(typed.ok, true);
  assert.equal(typed.x, 300);

  dom.window.close();
});

test("hide removes the overlay and unknown commands are rejected", () => {
  const dom = createPage();
  dom.window.__lumenPointerOverlay({ op: "move", x: 10, y: 10 });
  const hiddenResult = dom.window.__lumenPointerOverlay({ op: "hide" });
  assert.equal(hiddenResult.ok, true);
  assert.equal(hiddenResult.visible, false);
  assert.equal(dom.window.document.getElementById("lumen-pointer-overlay"), null);

  const unknown = dom.window.__lumenPointerOverlay({ op: "explode" });
  assert.equal(unknown.ok, false);
  assert.equal(unknown.reason, "unknown_pointer_op");

  dom.window.close();
});

test("pointer visuals ship a click ripple, scroll hint, and reduced-motion support", () => {
  assert.match(SOURCE, /@keyframes lumen-ripple/);
  assert.match(SOURCE, /scroll-hint/);
  assert.match(SOURCE, /prefers-reduced-motion/);
  assert.match(SOURCE, /pointer-events/);
});

test("background action executors drive the pointer and hide it for every capture", () => {
  // The glide finishes before the adjacent identity gate; the pulse fires only
  // after a real activation; every capture path hides the overlay first.
  // A 240 ms glide by default; a timed reaction (a click right after its wait saw the text) skips it.
  assert.match(BACKGROUND_SRC, /: 240;/);
  assert.match(BACKGROUND_SRC, /if \(glideMs > 0\) await animatePointerToPoint\(tabId, point\.x, point\.y, glideMs\)/);
  assert.match(BACKGROUND_SRC, /await showPointerTypingAtActiveControl\(tabId\)/);
  assert.match(BACKGROUND_SRC, /showPointerScrollHint\(tabId, dy, exactSelector\)/);
  assert.match(BACKGROUND_SRC, /setPointerOverlayCaptureHidden\(pointerTabId, true\)/);
  assert.match(BACKGROUND_SRC, /setPointerOverlayCaptureHidden\(pointerTabId, false\)/);
  const pulseIndex = BACKGROUND_SRC.indexOf("showPointerClickPulse(tabId, point.x, point.y)");
  const dispatchIndex = BACKGROUND_SRC.indexOf("activationDispatched = true;");
  assert.ok(pulseIndex > dispatchIndex && dispatchIndex > 0, "click pulse only after activation dispatch");
});

test("the moving pill carries a persistent narration line beside the cursor", () => {
  const dom = createPage();
  const moved = dom.window.__lumenPointerOverlay({ op: "move", x: 40, y: 40, status: "Searching for showtimes" });
  assert.equal(moved.ok, true);
  assert.equal(moved.visible, true);
  const clicked = dom.window.__lumenPointerOverlay({ op: "click", x: 60, y: 80, status: "Picking the 7:30 PM show" });
  assert.equal(clicked.ok, true);
  const cleared = dom.window.__lumenPointerOverlay({ op: "hide" });
  assert.equal(cleared.visible, false);
  dom.window.close();

  // Contract pins (the shadow root is closed, so behavior rides on source shape):
  // a persistent status line distinct from transient gesture hints, parking instead of
  // vanishing while a run narrates, an edge flip so the pill never clips off-screen,
  // and the background attaching the live line to every gesture.
  assert.match(SOURCE, /statusText/);
  assert.match(SOURCE, /showTransient/);
  assert.match(SOURCE, /classList\.add\("parked"\)/);
  assert.match(SOURCE, /"flip"/);
  assert.match(BACKGROUND_SRC, /pointerOverlayStatusText/);
  assert.match(BACKGROUND_SRC, /status: pointerOverlayStatusText\(\)/);
});
