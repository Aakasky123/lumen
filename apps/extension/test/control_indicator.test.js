const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { JSDOM } = require("jsdom");

const SOURCE = fs.readFileSync(
  path.join(__dirname, "..", "src", "control_indicator.js"),
  "utf8",
);

function createPage(initialState) {
  const dom = new JSDOM("<!doctype html><html><body><main>Task page</main></body></html>", {
    runScripts: "dangerously",
    url: "https://example.test/task",
  });
  dom.window.chrome = {
    runtime: {
      lastError: null,
      sendMessage(message, callback) {
        assert.equal(message.type, "LUMEN_CONTROL_INDICATOR_READY");
        callback(initialState);
      },
    },
  };
  dom.window.eval(SOURCE);
  return dom;
}

test("active browser control mounts an invisible lease anchor and renders nothing in-page", async () => {
  const dom = createPage({
    active: true,
    capture_hidden: false,
    expires_at: Date.now() + 60_000,
  });
  await Promise.resolve();

  const host = dom.window.document.getElementById("lumen-active-control-indicator");
  assert.ok(host);
  assert.equal(host.getAttribute("aria-hidden"), "true");
  assert.equal(host.style.getPropertyValue("pointer-events"), "none");
  assert.equal(host.style.getPropertyValue("display"), "block");
  assert.equal(host.shadowRoot, null, "the page cannot inspect the closed status surface");
  // ONE pill, desktop level only: the screen overlay owns the visible "in control"
  // pill and glow; the page mounts no frame, no status pill, no visible content.
  assert.doesNotMatch(SOURCE, /\.frame/);
  assert.doesNotMatch(SOURCE, /\.status/);
  assert.doesNotMatch(SOURCE, /createElement\("style"\)/);

  dom.window.close();
});

test("capture suppression hides the indicator without ending its active lease", async () => {
  const expiresAt = Date.now() + 60_000;
  const dom = createPage({
    active: true,
    capture_hidden: false,
    expires_at: expiresAt,
  });
  await Promise.resolve();

  const hidden = dom.window.__lumenSetControlIndicator({
    active: true,
    capture_hidden: true,
    expires_at: expiresAt,
  });
  const host = dom.window.document.getElementById("lumen-active-control-indicator");
  assert.equal(hidden.visible, false);
  assert.equal(host.style.getPropertyValue("display"), "none");

  const restored = dom.window.__lumenSetControlIndicator({
    active: true,
    capture_hidden: false,
    expires_at: expiresAt,
  });
  assert.equal(restored.visible, true);
  assert.equal(host.style.getPropertyValue("display"), "block");

  dom.window.__lumenSetControlIndicator({ active: false });
  assert.equal(
    dom.window.document.getElementById("lumen-active-control-indicator"),
    null,
  );
  dom.window.close();
});
