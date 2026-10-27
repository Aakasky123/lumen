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

function boxes() {
  return function () {
    return { left: 10, top: 10, right: 210, bottom: 40, width: 200, height: 30, x: 10, y: 10 };
  };
}

function loadWindow(html, frameHtml = "", frameOrigin = "https://shop.example.test") {
  const nativeDialogOpenFor = () => null;
  const typeIntoNativeDialog = () => null;
  const publicNativeDialog = (dialog) => dialog;
  const nativeDialogObservation = () => null;
  const dispatchInputUnlessDialog = async () => "dispatched";
  const acquireDebugger = async () => ({ attached_now: false });
  const releaseDebugger = () => {};
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    url: "https://shop.example.test/login",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  Object.defineProperty(dom.window, "devicePixelRatio", { value: 1, configurable: true });
  Object.defineProperty(dom.window, "innerHeight", { value: 900, configurable: true });
  Object.defineProperty(dom.window, "innerWidth", { value: 1200, configurable: true });
  dom.window.Element.prototype.getBoundingClientRect = boxes();
  for (const frame of Array.from(dom.window.document.querySelectorAll("iframe"))) {
    try {
      // jsdom does not load srcdoc: the frame's document (about:blank, origin inherited) is filled in directly.
      if (frameHtml) frame.contentDocument.body.innerHTML = frameHtml;
      // What the browser reports as the frame document's origin: the embedder's for a plain srcdoc/blank frame,
      // "null" for a sandboxed one. jsdom reports "null" for about:blank, so the browser's answer is stated here.
      Object.defineProperty(frame.contentWindow, "origin", { value: frameOrigin, configurable: true });
      frame.contentWindow.Element.prototype.getBoundingClientRect = boxes();
    } catch (_) {}
  }
  const fn = dom.window.eval(`(${extractFunctionSource("_visionPageEvidence")})`);
  return { dom, fn };
}

test("a control in the top document carries no frame fields", () => {
  const { dom, fn } = loadWindow(`<input id="otp" type="text" aria-label="Verification code">`);
  try {
    const controls = fn([]).controls || [];
    const otp = controls.find((row) => row.id === "otp");
    assert.ok(otp, "the top-document field is observed");
    assert.equal(otp.frame_origin, undefined);
    assert.equal(otp.frame_depth, undefined);
  } finally {
    dom.window.close();
  }
});

test("a same-origin frame's control reports the frame document's own origin, from the browser", () => {
  const { dom, fn } = loadWindow(
    `<iframe id="f"></iframe>`,
    `<input id="inner" type="text" aria-label="Verification code">`,
  );
  try {
    const controls = fn([]).controls || [];
    const inner = controls.find((row) => row.id === "inner");
    assert.ok(inner, "the frame's field is observed (same-origin frames join the composed tree)");
    // An about:srcdoc document inherits its embedder's origin; the page cannot name another one.
    assert.equal(inner.frame_origin, "https://shop.example.test");
    assert.equal(inner.frame_depth, 1);
  } finally {
    dom.window.close();
  }
});

test("page content cannot set a frame's reported origin", () => {
  const { dom, fn } = loadWindow(
    `<iframe id="f" data-frame-origin="https://evil.example.test" name="https://evil.example.test"></iframe>`,
    `<input id="inner" type="text" data-origin="https://evil.example.test" aria-label="code">`,
  );
  try {
    const inner = (fn([]).controls || []).find((row) => row.id === "inner");
    assert.ok(inner);
    assert.equal(inner.frame_origin, "https://shop.example.test");
  } finally {
    dom.window.close();
  }
});

test("a frame whose document the page cannot read (cross-origin) contributes no controls", () => {
  const { dom, fn } = loadWindow(`<iframe id="f" src="https://evil.example.test/embed"></iframe><input id="top" type="text" aria-label="Name">`);
  try {
    const iframe = dom.window.document.getElementById("f");
    Object.defineProperty(iframe, "contentDocument", { get: () => null, configurable: true });
    const controls = fn([]).controls || [];
    assert.ok(controls.find((row) => row.id === "top"));
    assert.equal(controls.filter((row) => row.frame_origin).length, 0);
  } finally {
    dom.window.close();
  }
});

test("a sandboxed about:srcdoc/about:blank frame (origin \"null\") stays opaque and never inherits the embedder's origin", () => {
  const { dom, fn } = loadWindow(
    `<iframe id="f" sandbox="allow-scripts"></iframe>`,
    `<input id="inner" type="text" aria-label="Verification code">`,
    "null",
  );
  try {
    const inner = (fn([]).controls || []).find((row) => row.id === "inner");
    assert.ok(inner);
    assert.equal(inner.frame_origin, "null");
    assert.equal(inner.frame_depth, 1);
  } finally {
    dom.window.close();
  }
});

test("an unreadable or non-http frame origin is reported as null", () => {
  for (const reported of ["", "about:blank", "file://", "data:"]) {
    const { dom, fn } = loadWindow(`<iframe id="f"></iframe>`, `<input id="inner" type="text" aria-label="code">`, reported);
    try {
      const inner = (fn([]).controls || []).find((row) => row.id === "inner");
      assert.ok(inner);
      assert.equal(inner.frame_origin, "null", `reported ${JSON.stringify(reported)}`);
    } finally {
      dom.window.close();
    }
  }
});
