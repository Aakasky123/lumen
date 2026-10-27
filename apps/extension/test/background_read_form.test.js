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

const ZERO = { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0 };

// The read_form enumerator (_visionPageEvidence with form_outline) over a jsdom page. There is no layout in jsdom:
// every element sits in the viewport except one marked data-offscreen, and an element under display:none has no box.
// options.selectors: the model-classified sensitive selectors; options.setup(document) runs before the read.
function outlineOf(html, options = {}) {
  const dom = new JSDOM(`<!doctype html><body>${html}</body>`, { url: "https://example.test/apply", runScripts: "outside-only" });
  Object.defineProperty(dom.window, "innerHeight", { value: 900, configurable: true });
  Object.defineProperty(dom.window, "innerWidth", { value: 1200, configurable: true });
  dom.window.Element.prototype.getBoundingClientRect = function () {
    for (let el = this; el && el.nodeType === 1; el = el.parentElement) {
      if (dom.window.getComputedStyle(el).display === "none") return ZERO;
    }
    if (this.hasAttribute("data-offscreen")) {
      return { left: 0, top: 3000, right: 200, bottom: 3020, width: 200, height: 20, x: 0, y: 3000 };
    }
    // A zero-size control: a styled custom radio or checkbox whose real input is opacity 0 and has no box.
    if (this.hasAttribute("data-zero")) return ZERO;
    return { left: 0, top: 0, right: 200, bottom: 20, width: 200, height: 20, x: 0, y: 0 };
  };
  if (typeof options.setup === "function") options.setup(dom.window.document);
  const visionPageEvidence = dom.window.eval(`(${fnSource("_visionPageEvidence")})`);
  // JSON round trip: the page realm's arrays and objects compare equal to the test realm's.
  const selectors = options.selectors || [];
  const readOptions = { form_outline: true, ...(options.target ? { form_target_selector: options.target } : {}) };
  return { dom, out: JSON.parse(JSON.stringify(visionPageEvidence(selectors, readOptions))) };
}

const FORM = `
  <form>
    <fieldset>
      <legend>Are you legally authorized to work in the US?</legend>
      <label><input type="radio" name="auth" value="y"> Yes</label>
      <label><input type="radio" name="auth" value="n" checked> No</label>
    </fieldset>
    <label>First name <input type="text" name="first" required></label>
    <label>Country <select name="country" required>
      <option>Choose one</option><option selected>United States</option><option>Canada</option>
    </select></label>
    <label>Resume <input type="file" name="resume"></label>
    <label><input type="checkbox" name="agree"> I agree to the terms</label>
    <label>Start date <input type="date" name="start" data-offscreen></label>
    <label>Account PIN <input type="password" name="pin" value="hunter22secret"></label>
  </form>`;

function row(out, label) {
  const found = out.form_outline.find((item) => item.label === label);
  assert.ok(found, `no row labelled ${label}: ${JSON.stringify(out.form_outline.map((r) => r.label))}`);
  return found;
}

test("every control is listed, on screen or not, with its kind, label and required flag", () => {
  const { out } = outlineOf(FORM);
  assert.equal(out.error, undefined);
  assert.equal(row(out, "First name").kind, "text");
  assert.equal(row(out, "First name").required, true);
  assert.equal(row(out, "First name").in_view, true);
  assert.equal(row(out, "First name").value, undefined);
  assert.equal(row(out, "Start date").kind, "date");
  assert.equal(row(out, "Start date").in_view, false);
  assert.equal(row(out, "Resume").kind, "file");
  assert.deepEqual(row(out, "Resume").files, []);
  assert.equal(row(out, "I agree to the terms").kind, "checkbox");
  assert.equal(row(out, "I agree to the terms").checked, false);
});

test("a radio group is one row: its question, its options, and the chosen one", () => {
  const { out } = outlineOf(FORM);
  const group = out.form_outline.filter((item) => item.kind === "radio_group");
  assert.equal(group.length, 1, "two radios sharing a name are one group");
  assert.equal(group[0].label, "Are you legally authorized to work in the US?");
  assert.deepEqual(group[0].options.map((option) => option.label), ["Yes", "No"]);
  assert.deepEqual(group[0].selected, ["No"]);
  assert.equal(group[0].required, false);
});

test("a select lists its chosen option and at most 30 options, with the total", () => {
  const { out } = outlineOf(FORM);
  const country = row(out, "Country");
  assert.equal(country.kind, "select");
  assert.equal(country.required, true);
  assert.deepEqual(country.selected, ["United States"]);
  assert.deepEqual(country.options.map((option) => option.label), ["Choose one", "United States", "Canada"]);
  const many = Array.from({ length: 40 }, (_, i) => `<option>Option ${i}</option>`).join("");
  const big = outlineOf(`<label>Pick <select name="pick">${many}</select></label>`).out;
  const pick = row(big, "Pick");
  assert.equal(pick.options.length, 30);
  assert.equal(pick.options_total, 40);
});

test("a sensitive value is masked, and no raw value or option of it leaves the page reader", () => {
  const { out } = outlineOf(FORM + `
    <label>One-time code <input type="text" name="otp" autocomplete="one-time-code" value="492817"></label>
    <label>Card expiry <select name="exp" autocomplete="cc-exp-month"><option>01</option><option selected>12</option></select></label>`);
  // A masked control keeps the inventory's label order (placeholder, aria-label, name): its name, never its label text.
  const pin = row(out, "pin");
  assert.equal(pin.value, "[REDACTED]");
  assert.equal(pin.value_redacted, true);
  const otp = row(out, "otp");
  assert.equal(otp.value, "[REDACTED]");
  const exp = row(out, "exp");
  assert.equal(exp.value, "[REDACTED]");
  assert.equal(exp.options, undefined, "a masked select's options are withheld");
  const text = JSON.stringify(out);
  assert.doesNotMatch(text, /hunter22secret/);
  assert.doesNotMatch(text, /492817/);
  assert.equal(exp.selected, undefined, "a masked select's chosen option is withheld");
  assert.doesNotMatch(JSON.stringify(exp), /"12"|"01"/, "a masked select's option labels never reach the row");
});

test("a hidden control is listed as not rendered, and a hidden input is skipped", () => {
  const { out } = outlineOf(`<label>Old field <input type="text" name="old" style="display:none"></label>
    <input type="hidden" name="token" value="abc">`);
  const old = row(out, "Old field");
  assert.equal(old.hidden, true);
  assert.equal(out.form_outline.length, 1, "the hidden input is not a form control");
});

test("each row names its element by selector only, and the page's own text is never an id", () => {
  const { out } = outlineOf(FORM);
  const first = row(out, "First name");
  assert.ok(first.selector, "a row carries its element's selector for the server to match");
  assert.equal(first.id, undefined, "the page reader never issues a control id");
});

test("controls in a same-origin frame are listed", () => {
  const dom = new JSDOM(`<!doctype html><body><iframe></iframe></body>`, { url: "https://example.test/apply", runScripts: "outside-only" });
  const frame = dom.window.document.querySelector("iframe");
  frame.contentDocument.body.innerHTML = `<label>Inner question <input type="text" name="inner" required></label>`;
  Object.defineProperty(dom.window, "innerHeight", { value: 900, configurable: true });
  Object.defineProperty(dom.window, "innerWidth", { value: 1200, configurable: true });
  const visionPageEvidence = dom.window.eval(`(${fnSource("_visionPageEvidence")})`);
  const out = JSON.parse(JSON.stringify(visionPageEvidence([], { form_outline: true })));
  const inner = row(out, "Inner question");
  assert.equal(inner.required, true);
  assert.match(inner.selector, / >>> /, "a frame's control is named through its frame element");
});

test("the outline is capped in total and says so", () => {
  const html = Array.from({ length: 200 }, (_, i) =>
    `<label>Field number ${i} with a long question text <input type="text" name="f${i}" value="${"v".repeat(190)}"></label>`,
  ).join("");
  const { out } = outlineOf(html);
  assert.equal(out.form_outline_truncated, true);
  assert.ok(out.form_outline.length < 200);
});

// (a) A control the page does not render for the user never has its value read into the outline; an on-screen or
// scrolled-away control keeps its value through the same redaction as the document text.
test("a display:none card number is listed without its value, marked value_withheld", () => {
  const { out } = outlineOf(`<label>Card number <input type="text" name="card" autocomplete="off"
    value="4111111111111111" style="display:none"></label>`);
  const card = row(out, "Card number");
  assert.equal(card.hidden, true);
  assert.equal(card.value, undefined, "a hidden control's value is never sent");
  assert.equal(card.value_withheld, "hidden", "the value is present but not read");
  assert.doesNotMatch(JSON.stringify(out), /4111111111111111/);
});

test("visibility:hidden, aria-hidden (on the control or an ancestor) and the hidden attribute withhold values too", () => {
  const { out } = outlineOf(`
    <label>Invisible code <input type="text" name="vis" style="visibility:hidden" value="code-bbb-222"></label>
    <label>Aria hidden token <input type="text" name="tok" aria-hidden="true" value="tok-aaa-111"></label>
    <div aria-hidden="true"><label>Inside hidden panel <input type="text" name="inner" value="inner-ccc-333"></label></div>
    <label>Hidden attribute <input type="text" name="ha" hidden value="attr-ddd-444"></label>`);
  for (const label of ["Invisible code", "Aria hidden token", "Inside hidden panel", "Hidden attribute"]) {
    const found = row(out, label);
    assert.equal(found.value, undefined, `${label}: value withheld`);
    assert.equal(found.value_withheld, "hidden", `${label}: reported as withheld`);
  }
  assert.doesNotMatch(JSON.stringify(out), /code-bbb-222|tok-aaa-111|inner-ccc-333|attr-ddd-444/);
});

// A custom radio or checkbox: the real input is zero-size or visually hidden behind a styled label. Its checked state is
// not a secret, so the outline reports it; a hidden text value, file name or select stays withheld.
test("a zero-size radio group reports which option is selected, and a zero-size checkbox its state", () => {
  const { out } = outlineOf(`
    <fieldset><legend>Do you need visa sponsorship?</legend>
      <label><input type="radio" name="spons" value="y" data-zero> Yes</label>
      <label><input type="radio" name="spons" value="n" data-zero checked> No</label>
    </fieldset>
    <label><input type="checkbox" name="consent" data-zero checked> I consent</label>`);
  const group = out.form_outline.find((item) => item.kind === "radio_group");
  assert.ok(group, "the zero-size radios are one group");
  assert.equal(group.label, "Do you need visa sponsorship?");
  assert.deepEqual(group.selected, ["No"], "the chosen option is reported");
  assert.deepEqual(group.options.map((option) => [option.label, option.checked]), [["Yes", false], ["No", true]]);
  assert.equal(group.value_withheld, undefined);
  assert.equal(group.hidden, true, "the group is still marked not rendered");
  const consent = row(out, "I consent");
  assert.equal(consent.kind, "checkbox");
  assert.equal(consent.checked, true, "a zero-size checkbox reports its checked state");
  assert.equal(consent.value_withheld, undefined);
  assert.equal(consent.value, undefined);
});

test("a hidden text input still withholds its value even when it sits beside a custom checkbox", () => {
  const { out } = outlineOf(`
    <label>Zero text <input type="text" name="zt" data-zero value="secret-zz-777"></label>
    <label><input type="checkbox" name="agree" data-zero checked> I agree</label>`);
  const text = row(out, "Zero text");
  assert.equal(text.value, undefined);
  assert.equal(text.value_withheld, "hidden");
  assert.equal(row(out, "I agree").checked, true);
  assert.doesNotMatch(JSON.stringify(out), /secret-zz-777/);
});

test("a masked hidden checkbox reports [REDACTED], not its state", () => {
  const { out } = outlineOf(`<label><input type="checkbox" name="tb" data-zero checked> Token box</label>`,
    { selectors: ["input[name='tb']"] });
  const box = row(out, "tb"); // a masked control keeps the attribute order: its name, never its label text
  assert.equal(box.value, "[REDACTED]");
  assert.equal(box.value_redacted, true);
  assert.equal(box.checked, undefined, "a masked control's state is not sent");
});

test("a hidden select's chosen option and its options are withheld", () => {
  const { out } = outlineOf(`<label>Hidden country <select name="hc" style="display:none">
    <option>Freedonia Republic</option><option selected>Sylvania Republic</option></select></label>`);
  const country = row(out, "Hidden country");
  assert.equal(country.selected, undefined);
  assert.equal(country.options, undefined);
  assert.equal(country.value_withheld, "hidden");
  assert.doesNotMatch(JSON.stringify(out), /Sylvania|Freedonia/);
});

test("an off-screen but rendered control keeps its value, and a value that repeats a masked one is redacted", () => {
  const { out } = outlineOf(`
    <label>Start note <input type="text" name="note" data-offscreen value="Jane Q"></label>
    <label>Account PIN <input type="password" name="pin" value="hunter22secret"></label>
    <label>Confirm PIN <input type="text" name="confirm" value="hunter22secret"></label>`);
  const note = row(out, "Start note");
  assert.equal(note.in_view, false);
  assert.equal(note.value, "Jane Q", "an off-screen control's value is kept, as on screen");
  assert.equal(note.value_withheld, undefined);
  assert.equal(row(out, "Confirm PIN").value, "[REDACTED]");
  assert.doesNotMatch(JSON.stringify(out), /hunter22secret/);
});

// (b) A file name is document text: it goes through the same redaction as a value.
test("a file name is redacted like the document text, and a hidden file input's names are withheld", () => {
  const { out } = outlineOf(`
    <label>One-time code <input type="text" name="otp" autocomplete="one-time-code" value="492817"></label>
    <label>Resume <input type="file" name="resume"></label>
    <label>Old upload <input type="file" name="old" style="display:none"></label>`, {
    setup: (doc) => {
      const resume = doc.querySelector("input[name=resume]");
      Object.defineProperty(resume, "files", { value: [{ name: "resume-492817.pdf" }], configurable: true });
      const old = doc.querySelector("input[name=old]");
      Object.defineProperty(old, "files", { value: [{ name: "cv-final.pdf" }], configurable: true });
    },
  });
  const resume = row(out, "Resume");
  assert.deepEqual(resume.files, ["resume- [REDACTED] .pdf"]);
  const old = row(out, "Old upload");
  assert.equal(old.files, undefined, "a hidden file input's file names are not sent");
  assert.equal(old.value_withheld, "hidden");
  assert.doesNotMatch(JSON.stringify(out), /492817|cv-final/);
});

// (c) A masked control of any tag keeps its option text out of a neighbour's question text.
test("a classifier-marked select without autocomplete leaks no option label through a neighbour's question", () => {
  const { out } = outlineOf(`
    <div class="q">Which office is nearest <select name="office"><option>Newark Office</option><option selected>Trenton Office</option></select>
      <span><input type="radio" name="near" value="1"> Yes <input type="radio" name="near" value="2"> No</span>
    </div>`, { selectors: ["select[name='office']"] });
  const office = row(out, "office");
  assert.equal(office.value, "[REDACTED]");
  assert.equal(office.options, undefined, "the masked select's options are withheld");
  const group = out.form_outline.find((item) => item.kind === "radio_group");
  assert.ok(group, "the neighbouring radio group is listed");
  assert.match(group.label, /Which office is nearest/);
  assert.doesNotMatch(JSON.stringify(out), /Newark|Trenton/);
});

test("a model-marked control of any tag leaks no option text through a neighbour's question", () => {
  const { out } = outlineOf(`
    <div class="q">Pick a plan <div id="plan" role="combobox" aria-label="Plan">Gold Platinum Tier</div>
      <span><input type="radio" name="agree" value="1"> Yes <input type="radio" name="agree" value="2"> No</span>
    </div>`, { selectors: ["#plan"] });
  assert.match(JSON.stringify(out), /Pick a plan/);
  assert.doesNotMatch(JSON.stringify(out), /Gold Platinum Tier/);
});

test("a hidden rich-text control's own text is its value: withheld, and never its label", () => {
  const { out } = outlineOf(`<div contenteditable="true" aria-label="Notes" style="display:none">Secret note text</div>`);
  const notes = out.form_outline.find((item) => item.label === "Notes");
  assert.ok(notes, `labelled by its aria-label: ${JSON.stringify(out.form_outline)}`);
  assert.equal(notes.hidden, true);
  assert.equal(notes.value_withheld, "hidden");
  assert.doesNotMatch(JSON.stringify(out), /Secret note text/);
});

test("rows carry their owning form, the browser's value-missing verdict, and the submit target's form", () => {
  const html = `
    <form id="search"><label>Search jobs <input type="search" name="q" required></label><button id="go">Go</button></form>
    <form id="apply">
      <label>First name <input type="text" name="first" required></label>
      <label>Country <select name="country" required><option value="">Choose one</option><option>United States</option></select></label>
      <label>City <input type="text" name="city" role="combobox" aria-autocomplete="list" required></label>
      <label>Location <input type="search" name="loc" role="combobox" required></label>
      <button id="submit" type="submit">Submit application</button>
    </form>`;
  const { out } = outlineOf(html, { target: "#submit" });
  const search = row(out, "Search jobs");
  const first = row(out, "First name");
  assert.notEqual(search.form, first.form, "the search box belongs to another form");
  assert.equal(out.form_target, first.form, "Submit application belongs to the application form");
  assert.equal(first.value_missing, true);
  assert.equal(row(out, "Country").value_missing, true, "a select left on its empty placeholder is missing");
  assert.equal(row(out, "City").kind, "combobox");
  assert.equal(row(out, "Location").kind, "combobox", "a search-type input acting as a combobox is a combobox");
});

test("a target selector that names no one element leaves the target form unknown", () => {
  const { out } = outlineOf(FORM, { target: "#nope" });
  assert.equal(out.form_target, null);
});
