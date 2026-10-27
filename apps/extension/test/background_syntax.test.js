const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

// The service worker is an ES module: a syntax error there means Chromium loads no extension at all
// (a bench lane then never connects), so every source file must at least parse.
for (const name of ["background.js", "sidepanel.js", "control_indicator.js"]) {
  const file = path.join(__dirname, "..", "src", name);
  if (!fs.existsSync(file)) continue;
  test(`${name} parses`, () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "lumen-syntax-"));
    const copy = path.join(dir, name.replace(/\.js$/, ".mjs"));
    fs.copyFileSync(file, copy);
    const result = spawnSync(process.execPath, ["--check", copy], { encoding: "utf8" });
    fs.rmSync(dir, { recursive: true, force: true });
    assert.equal(result.status, 0, result.stderr);
  });
}
