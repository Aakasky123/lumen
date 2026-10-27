const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "src", "background.js"), "utf8");

function extract(name) {
  const start = SRC.indexOf(`function ${name}`);
  const bodyStart = SRC.indexOf("{", start);
  let depth = 0;
  for (let i = bodyStart; i < SRC.length; i += 1) {
    if (SRC[i] === "{") depth += 1;
    if (SRC[i] === "}") depth -= 1;
    if (depth === 0) return SRC.slice(start, i + 1);
  }
  throw new Error("unclosed");
}

function read(html) {
  const window = new JSDOM(`<!doctype html><body>${html}</body>`, { runScripts: "outside-only" }).window;
  window.document.querySelector("input").focus();
  return window.eval(`(${extract("focusedEditorValueInPage")})`)().value;
}

test("edit-commit read-back withholds secret-looking fields but reads ordinary cells", () => {
  assert.equal(read('<input value="Q3 budget" aria-label="Cell B2">'), "Q3 budget");
  for (const attrs of ['name="cvv"', 'id="ssn"', 'placeholder="Security code"', 'aria-label="Card number"', 'type="password"', 'autocomplete="cc-number"']) {
    assert.equal(read(`<input value="123456" ${attrs}>`), "", attrs);
  }
});
