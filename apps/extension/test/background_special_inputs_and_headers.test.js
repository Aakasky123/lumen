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

function page(html, { lang = "" } = {}) {
  const dom = new JSDOM(`<!doctype html><html${lang ? ` lang="${lang}"` : ""}><body>${html}</body></html>`, {
    url: "https://example.test/form",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  Object.defineProperty(dom.window, "devicePixelRatio", { value: 1, configurable: true });
  Object.defineProperty(dom.window, "innerHeight", { value: 900, configurable: true });
  dom.window.Element.prototype.getBoundingClientRect = function () {
    const y = Number(this.getAttribute("data-y") || 0);
    const width = Number(this.getAttribute("data-width") || 480);
    const height = Number(this.getAttribute("data-height") || 28);
    return { left: 20, top: y, right: 20 + width, bottom: y + height, width, height, x: 20, y };
  };
  return dom;
}

function typeInto(dom, selector, text) {
  const el = dom.window.document.querySelector(selector);
  el.focus();
  const set = dom.window.eval(`(${extractFunctionSource("activeValueSetInPage")})`);
  return { el, result: set(text) };
}

test("a date input takes an unambiguous typed date in its canonical format and reads it back", () => {
  const dom = page('<input id="d" type="date" data-y="10">');
  try {
    const events = [];
    const input = dom.window.document.querySelector("#d");
    input.addEventListener("input", () => events.push("input"));
    input.addEventListener("change", () => events.push("change"));
    // 25 cannot be a month, so 25/12/2012 can only be day/month.
    const { result } = typeInto(dom, "#d", "25/12/2012");
    assert.equal(result.ok, true);
    assert.equal(input.value, "2012-12-25");
    assert.equal(result.normalized_expected_value, "2012-12-25");
    assert.equal(result.active_value_matches_expected, true);
    assert.deepEqual(events, ["input", "change"]);
  } finally {
    dom.window.close();
  }
});

test("a numeric date with no page locale follows the browser locale order, and digits-only forms parse", () => {
  const dom = page('<input id="d" type="date" data-y="10">');
  try {
    assert.equal(typeInto(dom, "#d", "09/10/2012").el.value, "2012-09-10");
    assert.equal(typeInto(dom, "#d", "09102012").el.value, "2012-09-10");
    assert.equal(typeInto(dom, "#d", "20121225").el.value, "2012-12-25");
  } finally {
    dom.window.close();
  }
});

test("an ambiguous MM/DD vs DD/MM date is refused unless the page or browser names a locale", () => {
  const dom = page('<input id="d" type="date" data-y="10">');
  try {
    Object.defineProperty(dom.window.navigator, "language", { value: "", configurable: true });
    const { el, result } = typeInto(dom, "#d", "09/10/2012");
    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "ambiguous_date_format");
    assert.match(result.error, /ambiguous date format/);
    assert.equal(el.value, "");
  } finally {
    dom.window.close();
  }
  const us = page('<input id="d" type="date" data-y="10">', { lang: "en-US" });
  try {
    const { el, result } = typeInto(us, "#d", "09/10/2012");
    assert.equal(result.ok, true);
    assert.equal(el.value, "2012-09-10");
  } finally {
    us.window.close();
  }
  const gb = page('<input id="d" type="date" data-y="10">', { lang: "en-GB" });
  try {
    const { el, result } = typeInto(gb, "#d", "09/10/2012");
    assert.equal(result.ok, true);
    assert.equal(el.value, "2012-10-09");
  } finally {
    gb.window.close();
  }
});

test("other special input types are converted, set through the native setter and read back", () => {
  const dom = page(`
    <input id="t" type="time" data-y="10">
    <input id="dt" type="datetime-local" data-y="50">
    <input id="m" type="month" data-y="90">
    <input id="r" type="range" min="0" max="10" data-y="130">
    <input id="c" type="color" data-y="170">
    <input id="d" type="date" data-y="210">
  `);
  try {
    assert.equal(typeInto(dom, "#t", "9:05 pm").el.value, "21:05");
    assert.equal(typeInto(dom, "#dt", "2012-12-25 08:30").el.value, "2012-12-25T08:30");
    assert.equal(typeInto(dom, "#m", "September 2012").el.value, "2012-09");
    assert.equal(typeInto(dom, "#c", "#FA0").el.value, "#ffaa00");
    assert.equal(typeInto(dom, "#d", "Oct 9, 2012").el.value, "2012-10-09");
    const range = typeInto(dom, "#r", "7");
    assert.equal(range.result.ok, true);
    assert.equal(range.el.value, "7");
    // The slider clamps out-of-range values: that is a mismatch, not a success.
    const clamped = typeInto(dom, "#r", "70");
    assert.equal(clamped.result.ok, false);
    assert.equal(clamped.result.failure_reason, "value_not_accepted");
    assert.equal(clamped.result.active_value_matches_expected, false);
    // Impossible dates are refused, not rolled over.
    const bad = typeInto(dom, "#d", "2012-02-30");
    assert.equal(bad.result.ok, false);
    assert.equal(bad.result.failure_reason, "special_input_value_invalid");
    const unknown = typeInto(dom, "#d", "next friday");
    assert.equal(unknown.result.failure_reason, "special_input_format_unrecognized");
  } finally {
    dom.window.close();
  }
});

test("a collapsed accordion header (role=tab, aria-expanded=false) stays in the inventory", () => {
  const dom = page(`
    <div id="area">
      <h3 id="h1" role="tab" aria-expanded="false" aria-selected="false" data-y="10">Section one</h3>
      <div role="tabpanel" style="display:none" data-y="40">hidden panel text</div>
      <h3 id="h2" role="tab" aria-expanded="false" aria-selected="false" data-y="60">Section two</h3>
    </div>
  `);
  try {
    const evidence = dom.window.eval(`(${extractFunctionSource("_visionPageEvidence")})`)();
    const ids = evidence.controls.map((control) => control.id);
    assert.ok(ids.includes("h1"), JSON.stringify(ids));
    assert.ok(ids.includes("h2"), JSON.stringify(ids));
  } finally {
    dom.window.close();
  }
});

test("a menu item wrapped in a role-less li is listed once, as the role-bearing element", () => {
  const dom = page(`
    <ul id="menu" role="menu" data-y="10">
      <li id="wrap" role="presentation" data-y="10"><div id="item" role="menuitem" tabindex="0" data-y="10">Alice</div></li>
      <li id="wrap2" role="presentation" data-y="50"><div id="item2" role="menuitem" tabindex="0" data-y="50">Bob</div></li>
    </ul>
  `);
  try {
    const evidence = dom.window.eval(`(${extractFunctionSource("_visionPageEvidence")})`)();
    const ids = evidence.controls.map((control) => control.id);
    assert.ok(ids.includes("item") && ids.includes("item2"), JSON.stringify(ids));
    assert.ok(!ids.includes("wrap") && !ids.includes("wrap2"), JSON.stringify(ids));
  } finally {
    dom.window.close();
  }
});

test("clicking an accordion header reports its expanded state, not a committed choice", () => {
  const dom = page('<h3 id="h" role="tab" aria-expanded="false" aria-selected="false" data-y="10">Section one</h3>');
  try {
    const header = dom.window.document.querySelector("#h");
    const probe = {
      target_selector: "#h",
      target_text: "Section one",
      target_state_before: "aria-pressed=|aria-expanded=false|aria-current=|aria-checked=|aria-selected=false|data-state=|data-selected=|data-checked=|data-active=|class=",
      target_selected_before: false,
      target_option_like: false,
    };
    header.setAttribute("aria-expanded", "true");
    header.setAttribute("aria-selected", "true");
    const outcome = dom.window.eval(`(${extractFunctionSource("clickOutcomeEvidenceInPage")})`)(probe);
    assert.equal(outcome.selection_committed, false);
    assert.equal(outcome.selected_value, "");
    assert.equal(outcome.target_disclosure, true);
    assert.equal(outcome.expansion_changed, true);
    assert.equal(outcome.target_expanded_before, "false");
    assert.equal(outcome.target_expanded_after, "true");
    assert.equal(outcome.target_state_changed, true);
  } finally {
    dom.window.close();
  }
});

test("autocomplete popup items, slider handles and unlabeled class-only buttons are enumerated with derived facts", () => {
  const dom = page(`
    <input id="tags" data-y="5">
    <ul id="ac" class="ui-autocomplete ui-menu" data-y="40">
      <li id="o1" class="ui-menu-item" data-y="40"><div>Python</div></li>
      <li id="o2" class="ui-menu-item" data-y="70"><div>Perl</div></li>
    </ul>
    <div id="track" class="ui-slider" data-y="120" data-width="200" data-height="10"><span id="handle" class="ui-slider-handle" tabindex="0" data-y="118" data-width="20" data-height="14"></span></div>
    <a id="up" class="ui-spinner-button ui-spinner-up ui-corner-tr" tabindex="-1" data-y="200"></a>
  `);
  try {
    const evidence = dom.window.eval(`(${extractFunctionSource("_visionPageEvidence")})`)();
    const byId = Object.fromEntries(evidence.controls.map((control) => [control.id, control]));
    assert.ok(byId.o1 && byId.o2, JSON.stringify(Object.keys(byId)));
    assert.ok(byId.handle, JSON.stringify(Object.keys(byId)));
    assert.match(byId.handle.derived_value, /% along the track/);
    if (byId.up) assert.equal(byId.up.derived_label, "spinner up");
  } finally {
    dom.window.close();
  }
});

test("an open datepicker's icon-only prev/next arrows are enumerated with labels", () => {
  const dom = page(`
    <button id="submit" data-y="300">Submit</button>
    <div id="dp" class="ui-datepicker" data-y="40" data-height="200">
      <a id="prev" class="ui-datepicker-prev" title="Prev" data-y="42" data-width="20"></a>
      <a id="next" class="ui-datepicker-next" title="Next" data-y="42" data-width="20"></a>
      <a id="plain" data-y="100"></a>
    </div>
  `);
  try {
    const evidence = dom.window.eval(`(${extractFunctionSource("_visionPageEvidence")})`)();
    const byId = Object.fromEntries(evidence.controls.map((control) => [control.id, control]));
    assert.ok(byId.prev && byId.next, JSON.stringify(Object.keys(byId)));
    assert.equal(byId.prev.derived_label, "Prev");
    assert.equal(byId.next.derived_label, "Next");
    assert.equal(byId.plain, undefined);
    assert.ok(byId.submit);
  } finally {
    dom.window.close();
  }
});
