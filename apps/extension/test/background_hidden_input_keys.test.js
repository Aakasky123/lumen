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

function harness(html, { onKey = null, onAcquire = null } = {}) {
  const dom = new JSDOM(`<!doctype html><body>${html}</body>`, { url: "https://example.test/", runScripts: "outside-only" });
  Object.defineProperty(dom.window, "innerHeight", { value: 900, configurable: true });
  dom.window.Element.prototype.getBoundingClientRect = function () {
    return { left: 0, top: 0, right: 200, bottom: 20, width: 200, height: 20 };
  };
  const keys = [];
  const chrome = {
    scripting: {
      executeScript: async ({ func, args }) => [{ result: dom.window.eval(`(${func.toString()})`)(...(args || [])) }],
    },
  };
  const withTimeout = async (p) => p;
  const errorMessage = (e) => String((e && e.message) || e);
  const showPointerTypingAtActiveControl = async () => ({});
  const acquireDebugger = async () => { if (onAcquire) onAcquire(); };
  const releaseDebugger = () => {};
  const dispatchInputUnlessDialog = async (_t, method, params) => {
    if (method !== "Input.dispatchKeyEvent") return;
    if (onKey) onKey(params, keys.length);
    keys.push(params);
    const el = dom.window.document.activeElement;
    if (params.type === "keyDown" || params.type === "rawKeyDown") {
      el.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: params.key, bubbles: true }));
    }
  };
  const keyDispatchDescription = (k) => ({ key: k === "space" ? " " : k, text: k === "space" ? " " : k });
  const selectActiveTextInPage = eval(`(${fnSource("selectActiveTextInPage")})`);
  const activeValueSetInPage = eval(`(${fnSource("activeValueSetInPage")})`);
  const activeValueEvidenceInPage = eval(`(${fnSource("activeValueEvidenceInPage")})`);
  const hiddenKeyTargetFocusedInPage = eval(`(${fnSource("hiddenKeyTargetFocusedInPage")})`);
  const hiddenKeyTargetInPage = eval(`(${fnSource("hiddenKeyTargetInPage")})`);
  const printableCharacterKeyDescription = eval(`(${fnSource("printableCharacterKeyDescription")})`);
  void printableCharacterKeyDescription;
  const dispatchTrustedCharacterKeys = eval(`(${fnSource("dispatchTrustedCharacterKeys")})`);
  const runVisionType = eval(`(${fnSource("runVisionType")})`);
  const typeIntoNativeDialog = () => null;
  void [chrome, withTimeout, errorMessage, showPointerTypingAtActiveControl, acquireDebugger, releaseDebugger,
    dispatchInputUnlessDialog, keyDispatchDescription, selectActiveTextInPage, activeValueSetInPage,
    activeValueEvidenceInPage, hiddenKeyTargetInPage, hiddenKeyTargetFocusedInPage, dispatchTrustedCharacterKeys, typeIntoNativeDialog];
  return { dom, keys, run: (text) => runVisionType(1, text) };
}

test("a hidden opacity-0 input with a keydown listener receives the text as key events", async () => {
  const h = harness('<input id="t" style="opacity:0">');
  const el = h.dom.window.document.getElementById("t");
  const seen = [];
  el.addEventListener("keydown", (e) => seen.push(e.key));
  el.focus();
  const result = await h.run("ls -l");
  assert.deepEqual(seen, ["l", "s", " ", "-", "l"]);
  assert.equal(result.mode, "hidden_input_keystrokes");
  assert.equal(result.keys_sent, 5);
  assert.equal(result.active_hidden, true);
});

test("a visible textarea, date input, or long value keeps the direct value path", async () => {
  for (const [html, text] of [['<textarea id="t"></textarea>', "hello"], ['<input id="t" type="date">', "2026-10-09"], ['<input id="t">', "x".repeat(81)]]) {
    const h = harness(html);
    const el = h.dom.window.document.getElementById("t");
    const seen = [];
    el.addEventListener("keydown", (e) => seen.push(e.key));
    el.focus();
    const result = await h.run(text);
    assert.deepEqual(seen, [], html);
    assert.equal(el.value, text, html);
    assert.equal(result.mode, "dom_value_replace", html);
  }
});

test("a visible search-as-you-type input receives real key events", async () => {
  // Lever's location box (run 1792) looks suggestions up on key events; a value set never opened its list.
  for (const attrs of ['', 'type="text"', 'autocomplete="off"', 'role="combobox"', 'type="search"']) {
    const h = harness(`<input id="t" ${attrs}>`);
    const el = h.dom.window.document.getElementById("t");
    const seen = [];
    el.addEventListener("keydown", (e) => seen.push(e.key));
    el.focus();
    const result = await h.run("Jersey");
    assert.deepEqual(seen, ["J", "e", "r", "s", "e", "y"], attrs);
    assert.equal(result.mode, "typeahead_keystrokes", attrs);
    assert.equal(result.active_hidden, undefined, attrs);
  }
});

test("typed keys keep each character's case and symbol", async () => {
  // cabc2d08 typed "Jersey City, NJ" as "jersey city, nj": the chord parser lowercases a bare letter.
  const h = harness('<input id="t">');
  h.dom.window.document.getElementById("t").focus();
  await h.run("Jersey City, NJ!");
  const typed = h.keys.filter((k) => k.type === "keyDown").map((k) => k.text).join("");
  assert.equal(typed, "Jersey City, NJ!");
  const capitalJ = h.keys.find((k) => k.type === "keyDown" && k.text === "J");
  assert.equal(capitalJ.modifiers & 8, 8);
});

test("a hidden password input is never keyed", async () => {
  const h = harness('<input id="t" type="password" style="opacity:0">');
  h.dom.window.document.getElementById("t").focus();
  const result = await h.run("secret");
  assert.equal(h.keys.length, 0);
  assert.equal(result.mode, "dom_value_replace");
});

test("focus moved mid-burst aborts with fewer keys than the text and no value set", async () => {
  let h;
  h = harness('<input id="t" style="opacity:0"><button id="b">Delete</button>', {
    onKey: (_p, n) => { if (n === 6) h.dom.window.document.getElementById("b").focus(); },
  });
  const el = h.dom.window.document.getElementById("t");
  el.focus();
  const result = await h.run("abcdefghijkl");
  assert.equal(result.ok, false);
  assert.equal(result.failure_reason, "hidden_input_focus_moved");
  assert.ok(result.keys_sent > 0 && result.keys_sent < 12);
  assert.equal(el.value, "");
});

test("focus moved before the first key aborts with zero keys", async () => {
  let h;
  h = harness('<input id="t" style="opacity:0"><button id="b">Delete</button>', {
    onAcquire: () => h.dom.window.document.getElementById("b").focus(),
  });
  h.dom.window.document.getElementById("t").focus();
  const result = await h.run("abc");
  assert.equal(result.failure_reason, "hidden_input_focus_moved");
  assert.equal(result.keys_sent, 0);
  assert.equal(h.keys.length, 0);
});

test("a CDP failure after some keys is a failure, not a value set", async () => {
  const h = harness('<input id="t" style="opacity:0">', {
    onKey: (_p, n) => { if (n === 5) throw new Error("cdp gone"); },
  });
  const el = h.dom.window.document.getElementById("t");
  el.focus();
  const result = await h.run("abcdef");
  assert.equal(result.ok, false);
  assert.equal(result.failure_reason, "hidden_input_keystroke_failed");
  assert.equal(result.keys_sent, 2);
  assert.equal(el.value, "");
});

test("focus moved right before a Space aborts with no Space sent", async () => {
  let h;
  h = harness('<input id="t" style="opacity:0"><button id="b">Delete</button>', {
    onKey: (p, n) => { if (n === 3) h.dom.window.document.getElementById("b").focus(); },
  });
  let clicked = 0;
  h.dom.window.document.getElementById("b").addEventListener("click", () => { clicked += 1; });
  h.dom.window.document.getElementById("t").focus();
  const result = await h.run("ab c");
  assert.equal(result.failure_reason, "hidden_input_focus_moved");
  assert.equal(result.keys_sent, 2);
  assert.ok(!h.keys.some((k) => k.key === " "));
  assert.equal(clicked, 0);
});

test("the real key mapper types capitals and symbols exactly", async () => {
  const keyDispatchDescription = eval(`(${fnSource("keyDispatchDescription")})`);
  const sent = [];
  // eslint-disable-next-line no-unused-vars -- resolved by the evaluated dispatcher below
  const dispatchInputUnlessDialog = async (_t, _method, params) => { sent.push(params); };
  const printableCharacterKeyDescription = eval(`(${fnSource("printableCharacterKeyDescription")})`);
  void [keyDispatchDescription, printableCharacterKeyDescription];
  const dispatchTrustedCharacterKeys = eval(`(${fnSource("dispatchTrustedCharacterKeys")})`);
  await dispatchTrustedCharacterKeys({ tabId: 1 }, "Aa+Z9 !@");
  const typed = sent.filter((k) => k.type === "keyDown" || k.type === "rawKeyDown").map((k) => k.text || "").join("");
  assert.equal(typed, "Aa+Z9 !@");
  assert.equal(sent.find((k) => k.text === "A").modifiers & 8, 8);
  assert.equal(sent.find((k) => k.text === "a").modifiers & 8, 0);
  // Symbols carry their US-layout key, never a navigation key's code ("$" was VK 36, Home).
  const dollar = [];
  // eslint-disable-next-line no-unused-vars
  const sentBefore = sent.length;
  await dispatchTrustedCharacterKeys({ tabId: 1 }, "$!(");
  for (const k of sent.slice(sentBefore).filter((k) => k.type === "keyDown")) dollar.push([k.text, k.windowsVirtualKeyCode, k.modifiers & 8]);
  assert.deepEqual(dollar, [["$", 52, 8], ["!", 49, 8], ["(", 57, 8]]);
});
