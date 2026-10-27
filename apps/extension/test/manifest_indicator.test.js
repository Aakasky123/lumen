const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

test("the safety indicator is injected into every task page at document start", () => {
  const srcDir = path.join(__dirname, "../src");
  const manifest = JSON.parse(
    fs.readFileSync(path.join(__dirname, "../manifest.json"), "utf8"),
  );

  assert.equal(fs.existsSync(path.join(srcDir, "control_indicator.js")), true);
  assert.ok(
    manifest.content_scripts.some((entry) =>
      entry.js.includes("src/control_indicator.js")
      && entry.run_at === "document_start"
      && entry.matches.includes("<all_urls>")
    ),
  );
});
