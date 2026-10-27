// Unit harness for observe.js.
//
// observe.js runs in the page and exposes globalThis.__lumenObserveFullPage() plus the typed
// executors. Here we load it into a jsdom window and run it against fixture HTML. jsdom limits:
// getBoundingClientRect is 0 (so geometry label fallback and option visibility aren't exercised) and
// CSS ::after pseudo content isn't computed — fixtures avoid depending on those and use
// DOM-structural markers instead.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const OBSERVE_SRC = fs.readFileSync(path.join(__dirname, "..", "src", "observe.js"), "utf8");

// Load observe.js into a jsdom window and return the LIVE window (caller closes it).
function loadWindow(bodyHtml) {
  const dom = new JSDOM(
    `<!DOCTYPE html><html><head><title>Apply</title></head><body>${bodyHtml}</body></html>`,
    { url: "https://ats.rippling.com/x/apply?step=application", runScripts: "outside-only", pretendToBeVisual: true }
  );
  // jsdom doesn't implement CSS.escape (observe.js uses it to build label[for=...] selectors). Polyfill
  // the ID-safe subset so the perception code runs unchanged.
  if (!dom.window.CSS) dom.window.CSS = {};
  if (!dom.window.CSS.escape) dom.window.CSS.escape = (value) => String(value).replace(/[^a-zA-Z0-9_-]/g, (ch) => "\\" + ch);
  // jsdom reports every element as 0x0, so observe.js's isVisible() filter would drop them all. Give a
  // non-zero rect so elements count as visible (we test structure/labels/required, not real geometry).
  dom.window.Element.prototype.getBoundingClientRect = function () {
    return { x: 0, y: 0, top: 0, left: 0, right: 100, bottom: 20, width: 100, height: 20, toJSON() {} };
  };
  // jsdom doesn't implement scrollIntoView (the execute paths call it). No-op it.
  dom.window.Element.prototype.scrollIntoView = function () {};
  dom.window.scrollTo = function (_x, y) {
    Object.defineProperty(dom.window, "scrollY", { value: Number(y || 0), configurable: true });
  };
  // jsdom throws "Not implemented" on getComputedStyle(el, pseudoElt). observe.js's pseudoHasStar reads
  // ::after/::before content (a common required-marker style); make it pseudo-safe here so the rest of
  // detectRequired runs.
  const realGetComputedStyle = dom.window.getComputedStyle.bind(dom.window);
  dom.window.getComputedStyle = (element, pseudo) => (pseudo ? { content: "", getPropertyValue: () => "" } : realGetComputedStyle(element));
  dom.window.eval(OBSERVE_SRC);
  return dom.window;
}

async function observe(bodyHtml) {
  const win = loadWindow(bodyHtml);
  const observation = await win.__lumenObserveFullPage();
  win.close();
  // The observation's objects/arrays are created in the jsdom realm (different prototypes), which breaks
  // deepStrictEqual. Round-trip through JSON to bring it fully into the Node realm.
  return JSON.parse(JSON.stringify(observation));
}

const field = (nodes, substr) => nodes.find((node) => String(node.label || "").includes(substr));

test("observe structurally redacts sensitive values and preserves ordinary text", async () => {
  const observation = await observe(`
    <label for="password">Password</label>
    <input id="password" type="password" value="password-secret">
    <label for="otp">Verification code</label>
    <input id="otp" type="text" autocomplete="one-time-code" value="123456">
    <label for="card">Card number</label>
    <input id="card" type="text" autocomplete="section-checkout billing cc-number" value="4111111111111111">
    <div id="secret-editor" contenteditable="true" autocomplete="new-password" aria-label="Passphrase editor">contenteditable-secret</div>
    <textarea id="neighbor" aria-label="Neighboring notes"></textarea>
    <label for="display-name">Display name</label>
    <input id="display-name" type="text" autocomplete="name" value="Visible ordinary value">
  `);
  const password = field(observation.nodes, "Password");
  const otp = field(observation.nodes, "Verification code");
  const card = field(observation.nodes, "Card number");
  const secretEditor = field(observation.nodes, "Passphrase editor");
  const ordinary = field(observation.nodes, "Display name");

  for (const node of [password, otp, card, secretEditor]) {
    assert.equal(node.value, "[REDACTED]");
    assert.equal(node.value_redacted, true);
    assert.equal(node.value_redaction_reason, "sensitive_input_structure");
  }
  assert.equal(ordinary.value, "Visible ordinary value");
  assert.equal(ordinary.value_redacted, undefined);
  const serialized = JSON.stringify(observation);
  assert.doesNotMatch(serialized, /password-secret|123456|4111111111111111|contenteditable-secret/);
});

test("full-page sweep merges every viewport's controls and restores the scroll position", async () => {
  const controls = Array.from({ length: 12 }, (_, i) => `<label for="f${i}">Question ${i + 1}</label><input id="f${i}" />`).join("");
  const win = loadWindow(`<main>${controls}<button type="button">Review application</button></main>`);
  try {
    Object.defineProperty(win, "innerHeight", { value: 600, configurable: true });
    Object.defineProperty(win.document.documentElement, "clientHeight", { value: 600, configurable: true });
    Object.defineProperty(win.document.documentElement, "scrollHeight", { value: 2800, configurable: true });
    const scrolledTo = [];
    win.scrollTo = function (_x, y) {
      scrolledTo.push(Number(y || 0));
      Object.defineProperty(win, "scrollY", { value: Number(y || 0), configurable: true });
      Object.defineProperty(win.document.documentElement, "scrollTop", { value: Number(y || 0), configurable: true });
    };

    const observation = JSON.parse(JSON.stringify(await win.__lumenObserveFullPage()));

    assert.equal(observation.nodes.filter((node) => node.role === "textbox").length, 12);
    assert.ok(field(observation.nodes, "Review application"));
    assert.ok(scrolledTo.length > 2, "the sweep scrolls through the page");
    assert.equal(win.scrollY, 0, "the original scroll position is restored");
  } finally {
    win.close();
  }
});

test("react-select required marker in the question REGION (asterisk a sibling of the label, control below) is detected", async () => {
  // The captured label excludes the asterisk (it's a sibling span) and the control's tight wrapper
  // text is only "Select"; detectRequired searches the field's question region and finds it.
  const observation = await observe(`
    <div class="application-question">
      <label for="llm">Have you built integrations with LLMs such as Claude, GPT, or similar in a production environment?</label>
      <span style="color:red">*</span>
      <div class="select-control-wrapper">
        <div id="llm" role="combobox" aria-haspopup="listbox" tabindex="0"><span class="placeholder">Select</span></div>
      </div>
    </div>
  `);
  const node = field(observation.nodes, "Have you built integrations");
  assert.ok(node, "the LLM dropdown must be observed");
  assert.equal(node.role, "combobox");
  assert.equal(node.required, true);
  assert.equal(node.value, ""); // "Select" is a placeholder, not a value
});

test("asterisk INSIDE the captured label is detected", async () => {
  const observation = await observe(`<div class="field"><label for="a">How have you applied AI?*</label><textarea id="a"></textarea></div>`);
  const node = field(observation.nodes, "applied AI");
  assert.equal(node.required, true);
});

test("a dedicated required-marker element (sup) is detected", async () => {
  const observation = await observe(`
    <div class="field">
      <label for="b">Phone number</label><sup class="required-indicator">*</sup>
      <input id="b" type="text" />
    </div>
  `);
  const node = field(observation.nodes, "Phone number");
  assert.equal(node.required, true);
});

test("a field with NO required marker reads not-required", async () => {
  const observation = await observe(`
    <div class="application-question">
      <label for="x">What is your favorite color?</label>
      <input id="x" type="text" />
    </div>
  `);
  const node = field(observation.nodes, "favorite color");
  assert.equal(node.required, false);
});

test("a sibling field's asterisk does NOT leak onto an unmarked field", async () => {
  // Two questions in one section; only the first is required. The unmarked one must stay not-required.
  const observation = await observe(`
    <form>
      <div class="application-question"><label for="q1">First question</label><span>*</span><input id="q1" /></div>
      <div class="application-question"><label for="q2">Second question</label><input id="q2" /></div>
    </form>
  `);
  assert.equal(field(observation.nodes, "First question").required, true);
  assert.equal(field(observation.nodes, "Second question").required, false);
});

test("native DOM required attribute is detected", async () => {
  const observation = await observe(`<label for="e">Email</label><input id="e" type="email" required />`);
  const node = field(observation.nodes, "Email");
  assert.equal(node.required, true);
});

test("roles + native options are read correctly", async () => {
  const observation = await observe(`
    <label for="s">Country</label><select id="s"><option>United States</option><option>Canada</option></select>
    <label for="t">Cover letter</label><textarea id="t"></textarea>
    <label for="f">Résumé</label><input id="f" type="file" />
    <label for="c">Pronouns</label><div id="c" role="combobox"><span>Search</span></div>
  `);
  assert.equal(field(observation.nodes, "Country").role, "select");
  assert.deepEqual(field(observation.nodes, "Country").options, ["United States", "Canada"]);
  assert.equal(field(observation.nodes, "Cover letter").role, "textarea");
  assert.equal(field(observation.nodes, "Résumé").role, "file_input");
  assert.equal(field(observation.nodes, "Pronouns").role, "combobox");
  assert.equal(field(observation.nodes, "Pronouns").value, ""); // "Search" is a placeholder
});

test("a filled value is perceived as present; a long answer does not break required-detection", async () => {
  const longAnswer = "In my recent project DocuMindAI I applied AI ".repeat(20); // > 400 chars
  const observation = await observe(`
    <div class="application-question">
      <label for="ai">How have you applied AI in your work?</label><span>*</span>
      <textarea id="ai">${longAnswer}</textarea>
    </div>
  `);
  const node = field(observation.nodes, "applied AI in your work");
  assert.match(node.value, /^In my recent project DocuMindAI/);
  // The long typed answer is subtracted from the block text, so the "*" is still found in the region.
  assert.equal(node.required, true);
});

// --- Execute paths: a control's transient menu must not leak onto the next field ------------------

test("fill: a stale open menu from a previous field does not bleed onto a free-text textarea", async () => {
  // A select left its Yes/No menu open; the next free-text fill must ignore PRE-EXISTING menus (only
  // newly-revealed, this-field options count) and accept the free text.
  const win = loadWindow(`
    <div role="listbox" class="select__menu"><div role="option">Yes</div><div role="option">No</div></div>
    <label for="why">What interests you about this role?</label>
    <textarea id="why"></textarea>
  `);
  try {
    const res = await win.__lumenExecuteFill("#why", "I am drawn to backend systems and distributed services.");
    assert.equal(res.ok, true, JSON.stringify(res));
    assert.ok(!res.needs_translation, "free-text fill must not be flagged needs_translation by a stale menu");
    assert.match(String(res.observed_value), /backend systems/);
  } finally {
    win.close();
  }
});

test("fill: a real typeahead (options revealed BY typing) still matches and picks", async () => {
  // Options that appear as a RESULT of typing are this field's and should be picked.
  const win = loadWindow(`<label for="loc">Location</label><input id="loc" /><div id="menu" style="display:none"></div>`);
  try {
    win.document.getElementById("loc").addEventListener("input", () => {
      const menu = win.document.getElementById("menu");
      menu.style.display = "block";
      menu.innerHTML = '<div role="listbox"><div role="option">New Jersey, USA</div><div role="option">New York, USA</div></div>';
    });
    const res = await win.__lumenExecuteFill("#loc", "New Jersey, USA");
    assert.equal(res.ok, true, JSON.stringify(res));
    assert.equal(res.typeahead_selected, true, "the revealed option should be picked");
  } finally {
    win.close();
  }
});

test("select: owned menu still visible fails with explicit receipt reason", async () => {
  const win = loadWindow(`
    <div id="auth" role="combobox" aria-haspopup="listbox" aria-controls="auth-list" tabindex="0">Select</div>
    <div id="auth-list" role="listbox" style="display:block"><div role="option">Yes</div><div role="option">No</div></div>
  `);
  try {
    const res = await win.__lumenExecuteSelect("#auth", "Yes");
    assert.equal(res.ok, false, JSON.stringify(res));
    assert.equal(res.failure_reason, "OWNED_MENU_STILL_OPEN");
    assert.equal(res.owned_menu_visible_after, true);
    assert.equal(res.observed_value_matches, true);
  } finally {
    win.close();
  }
});

test("select: short option labels do not substring-match diagnostic text", async () => {
  const win = loadWindow(`
    <select id="binary">
      <option value="">Select</option>
      <option value="yes">Yes</option>
      <option value="no">No</option>
    </select>
  `);
  try {
    const res = await win.__lumenExecuteSelect("#binary", "do-not-match");
    assert.equal(res.ok, false, JSON.stringify(res));
    assert.equal(res.needs_translation, true);
    assert.equal(win.document.getElementById("binary").value, "", "No must not be selected from the 'no' substring");
  } finally {
    win.close();
  }
});
