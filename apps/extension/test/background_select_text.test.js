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

// jsdom has no layout: lay every character of the body's text out on one line, 10 px wide, 20 px tall.
function pageWithFakeLayout(html, { dpr = 1 } = {}) {
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, { runScripts: "outside-only" });
  const { window } = dom;
  const bases = new Map();
  let total = 0;
  const walker = window.document.createTreeWalker(window.document.body, window.NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    bases.set(node, total);
    total += node.nodeValue.length;
  }
  window.Range.prototype.getClientRects = function getClientRects() {
    const base = bases.get(this.startContainer);
    if (base === undefined || this.endOffset <= this.startOffset) return [];
    const left = (base + this.startOffset) * 10;
    return [{ left, right: left + 10, top: 100, bottom: 120, width: 10, height: 20 }];
  };
  Object.defineProperty(window, "devicePixelRatio", { value: dpr });
  Object.defineProperty(window, "innerWidth", { value: 4000 });
  Object.defineProperty(window, "innerHeight", { value: 800 });
  return window;
}

test("select-text points sit just inside the exact phrase, across element boundaries", () => {
  const html = "<p>The backup generator failed because the fuel <b>filter</b> was clogged with sediment.</p>";
  const window = pageWithFakeLayout(html);
  const locate = window.eval(`(${extractFunctionSource("selectTextPointsInPage")})`);
  const text = window.document.body.textContent;
  const at = text.indexOf("fuel filter was clogged");
  const result = locate("fuel filter was clogged", (at + 5) * 10, 110);
  assert.equal(result.ok, true);
  assert.equal(result.occurrences, 1);
  // A quarter of a character in from the first character's left edge and the last one's right edge.
  assert.equal(result.from.x, at * 10 + 2.5);
  assert.equal(result.to.x, (at + "fuel filter was clogged".length) * 10 - 2.5);
  assert.equal(result.from.y, 110);
});

test("the occurrence nearest the model's point is chosen; whitespace runs match any whitespace", () => {
  const html = "<p>Open the log.</p><p>Pick it up and open   the\nlog again.</p>";
  const window = pageWithFakeLayout(html);
  const locate = window.eval(`(${extractFunctionSource("selectTextPointsInPage")})`);
  const text = window.document.body.textContent;
  const second = text.indexOf("open   the");
  const near = locate("open the log", second * 10, 110);
  assert.equal(near.ok, true);
  assert.equal(near.occurrences, 1, "case-sensitive: 'Open the log' is a different text");
  assert.equal(near.from.x, second * 10 + 2.5);
});

test("screenshot pixels are divided by the device pixel ratio; missing text is reported", () => {
  const html = "<p>alpha beta alpha</p>";
  const window = pageWithFakeLayout(html, { dpr: 2 });
  const locate = window.eval(`(${extractFunctionSource("selectTextPointsInPage")})`);
  // The second "alpha" starts at CSS x=110; a point at screenshot x=240 is CSS x=120.
  const result = locate("alpha", 240, 220);
  assert.equal(result.ok, true);
  assert.equal(result.occurrences, 2);
  assert.equal(result.from.x, 110 + 2.5);
  assert.equal(JSON.stringify(locate("gamma", 0, 0)), JSON.stringify({ ok: false, failure_reason: "select_text_not_found", occurrences: 0 }));
  assert.equal(locate("   ", 0, 0).failure_reason, "select_text_empty");
});

test("text inside a textarea is selected in the field on the line nearest the point", () => {
  const dom = new JSDOM("<!doctype html><html><body><textarea id=t></textarea></body></html>", { runScripts: "outside-only" });
  const { window } = dom;
  Object.defineProperty(window, "devicePixelRatio", { value: 1 });
  Object.defineProperty(window, "innerWidth", { value: 1200 });
  Object.defineProperty(window, "innerHeight", { value: 800 });
  const area = window.document.getElementById("t");
  area.value = "R-2207 hold\nR-2208 ready\nR-2209 hold\nR-2210 ready";
  area.style.lineHeight = "20px";
  area.getBoundingClientRect = () => ({ left: 100, right: 500, top: 100, bottom: 300, width: 400, height: 200 });
  const select = window.eval(`(${extractFunctionSource("selectTextInFieldInPage")})`);
  // Line 3 (index 2) sits at y = 100 + 2 * 20 + 10.
  const result = select("hold", 300, 150);
  assert.equal(result.ok, true);
  assert.equal(result.selected_text, "hold");
  assert.equal(area.selectionStart, area.value.indexOf("R-2209 hold") + "R-2209 ".length);
  assert.equal(area.selectionEnd - area.selectionStart, 4);
  assert.equal(select("missing", 300, 150).failure_reason, "select_text_not_found");
});

test("a control click with the model's point lands there inside any element, not at its center", () => {
  const dom = new JSDOM("<!doctype html><html><body><div id=lb tabindex=0><div>A</div><div>B</div></div></body></html>", { runScripts: "outside-only" });
  const { window } = dom;
  Object.defineProperty(window, "devicePixelRatio", { value: 2 });
  const lb = window.document.getElementById("lb");
  lb.getBoundingClientRect = () => ({ left: 100, right: 400, top: 200, bottom: 440, width: 300, height: 240 });
  const probe = window.eval(`(${extractFunctionSource("visionControlCenterInPage")})`);
  const result = probe("#lb", false, false, [100, 260]);
  assert.equal(result.ok, true);
  assert.equal(result.x, 150);
  assert.equal(result.y, 330);
  const centered = probe("#lb", false, false, null);
  assert.equal(centered.x, 250);
});

test("a field dry run measures the nearest occurrence's line without selecting it", () => {
  const dom = new JSDOM("<!doctype html><html><body><textarea id=t></textarea></body></html>", { runScripts: "outside-only" });
  const { window } = dom;
  Object.defineProperty(window, "devicePixelRatio", { value: 1 });
  Object.defineProperty(window, "innerWidth", { value: 1200 });
  Object.defineProperty(window, "innerHeight", { value: 800 });
  const area = window.document.getElementById("t");
  area.value = "R-2207 hold\nR-2208 ready\nR-2209 hold";
  area.style.lineHeight = "20px";
  area.style.padding = "0";
  area.style.border = "0";
  area.getBoundingClientRect = () => ({ left: 100, right: 500, top: 100, bottom: 300, width: 400, height: 200 });
  area.setSelectionRange(0, 0);
  const select = window.eval(`(${extractFunctionSource("selectTextInFieldInPage")})`);
  // Line 3 is centred at y = 100 + 2.5 * 20 = 150.
  const probe = select("hold", 300, 150, false);
  assert.equal(probe.ok, true);
  assert.equal(probe.distance, 0);
  assert.equal(area.selectionEnd, 0, "a dry run selects nothing");
  // From below the field, the nearest occurrence's line (y = 150) is 250 px away.
  assert.equal(select("hold", 300, 400, false).distance, 250);
});
