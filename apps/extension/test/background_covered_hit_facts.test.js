const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { JSDOM } = require("jsdom");

const BACKGROUND_SRC = fs.readFileSync(path.join(__dirname, "..", "src", "background.js"), "utf8");

function extractFunctionSource(name) {
  const start = BACKGROUND_SRC.indexOf(`function ${name}`);
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

function refusal(coverBox) {
  const dom = new JSDOM(
    `<!doctype html><html><body><button id="target">Go</button><div id="veil" class="modal-backdrop big extra">Please wait</div></body></html>`,
    { runScripts: "outside-only" },
  );
  const { window } = dom;
  Object.defineProperty(window, "innerWidth", { value: 1000, configurable: true });
  Object.defineProperty(window, "innerHeight", { value: 800, configurable: true });
  const target = window.document.getElementById("target");
  const veil = window.document.getElementById("veil");
  target.scrollIntoView = () => {};
  target.getBoundingClientRect = () => ({ left: 100, top: 100, right: 200, bottom: 140, width: 100, height: 40 });
  veil.getBoundingClientRect = () => coverBox;
  window.document.elementFromPoint = () => veil;
  const fn = window.eval(`(${extractFunctionSource("visionControlCenterInPage")})`);
  return fn("#target", false, true, null);
}

test("a refused point names what it hit and whether that covers the viewport", () => {
  const whole = refusal({ left: 0, top: 0, right: 1000, bottom: 800, width: 1000, height: 800 });
  assert.equal(whole.failure_reason, "control_not_at_resolved_point");
  assert.equal(whole.hit_tag, "div");
  assert.equal(whole.hit_id, "veil");
  assert.equal(whole.hit_class, "modal-backdrop.big");
  assert.equal(whole.hit_text, "Please wait");
  assert.equal(whole.hit_covers_viewport, true);
  const small = refusal({ left: 90, top: 90, right: 210, bottom: 150, width: 120, height: 60 });
  assert.equal(small.failure_reason, "control_not_at_resolved_point");
  assert.equal(small.hit_covers_viewport, false);
});
