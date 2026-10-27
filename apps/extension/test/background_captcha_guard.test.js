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

// No layout in jsdom: the topmost element at a point is the last listed box containing it.
function stubElementFromPoint(dom, boxes) {
  dom.window.document.elementFromPoint = (x, y) => {
    let hit = null;
    for (const [id, [left, top, right, bottom]] of Object.entries(boxes)) {
      if (x >= left && x <= right && y >= top && y <= bottom) hit = dom.window.document.getElementById(id) || hit;
    }
    return hit;
  };
}

// Run 55's JazzHR form: a reCAPTCHA checkbox frame below the fields, the image challenge frame over the page.
function page({ dpr = 2 } = {}) {
  const dom = new JSDOM(
    `<!doctype html><body>
      <input id="email" type="email">
      <iframe id="anchor" title="reCAPTCHA" src="https://www.google.com/recaptcha/api2/anchor?k=x"></iframe>
      <iframe id="challenge" name="c-5kdag2lbsyni" title="recaptcha challenge expires in two minutes" src="https://www.google.com/recaptcha/api2/bframe?k=x"></iframe>
      <iframe id="video" src="https://www.youtube.com/embed/abc"></iframe>
    </body>`,
    { url: "https://zealogicsllc.applytojob.com/apply/x", runScripts: "outside-only" },
  );
  Object.defineProperty(dom.window, "devicePixelRatio", { value: dpr, configurable: true });
  const boxes = {
    email: [100, 100, 400, 140],
    anchor: [100, 600, 404, 678],
    challenge: [390, 120, 790, 700],
    video: [800, 100, 1200, 400],
  };
  dom.window.Element.prototype.getBoundingClientRect = function () {
    const [left, top, right, bottom] = boxes[this.id] || [0, 0, 0, 0];
    return { left, top, right, bottom, width: right - left, height: bottom - top, x: left, y: top };
  };
  stubElementFromPoint(dom, boxes);
  const inPage = dom.window.eval(`(${fnSource("captchaAtTargetInPage")})`);
  // The verdict is built in the page's realm: compare it as plain data.
  const check = (...args) => JSON.parse(JSON.stringify(inPage(...args)));
  return { dom, check };
}

test("a click on the reCAPTCHA checkbox or its image challenge is a CAPTCHA, in image pixels", () => {
  const { check } = page({ dpr: 2 });
  assert.deepEqual(check([[400, 1280]], "", false), { captcha: true, provider: "recaptcha", via: "point" });
  assert.deepEqual(check([[1200, 600]], "", false), { captcha: true, provider: "recaptcha", via: "point" });
});

test("ordinary fields and other frames are not CAPTCHAs", () => {
  const { check } = page({ dpr: 2 });
  assert.deepEqual(check([[400, 240]], "", false), { captcha: false });
  assert.deepEqual(check([[2000, 400]], "", false), { captcha: false });
  assert.deepEqual(check([[0, 0]], "#email", true), { captcha: false });
});

test("a control bound by selector or a focused CAPTCHA frame is caught too", () => {
  const { dom, check } = page();
  assert.equal(check([], 'iframe[name="c-5kdag2lbsyni"]', false).via, "selector");
  dom.window.document.getElementById("anchor").focus();
  dom.window.eval(`globalThis.__lumenDeepActiveElement = () => document.getElementById("anchor")`);
  assert.deepEqual(check([], "", true), { captcha: true, provider: "recaptcha", via: "focus" });
});

test("hCaptcha and Cloudflare Turnstile frames are recognised by their source", () => {
  const dom = new JSDOM(
    `<!doctype html><body>
      <iframe id="h" src="https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html"></iframe>
      <iframe id="t" src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2"></iframe>
    </body>`,
    { url: "https://example.test/", runScripts: "outside-only" },
  );
  const boxes = { h: [0, 0, 300, 80], t: [0, 100, 300, 165] };
  dom.window.Element.prototype.getBoundingClientRect = function () {
    const [left, top, right, bottom] = boxes[this.id] || [0, 0, 0, 0];
    return { left, top, right, bottom, width: right - left, height: bottom - top };
  };
  stubElementFromPoint(dom, boxes);
  const check = dom.window.eval(`(${fnSource("captchaAtTargetInPage")})`);
  assert.equal(check([[10, 10]], "", false).provider, "hcaptcha");
  assert.equal(check([[10, 120]], "", false).provider, "turnstile");
});

test("the dispatcher refuses guarded commands before running them", () => {
  const dispatcher = fnSource("handleCommand");
  assert.ok(dispatcher.indexOf("captchaRefusalFor(method, commandParams)") > -1);
  assert.ok(dispatcher.indexOf("captchaRefusalFor") < dispatcher.indexOf('method === "vision_click"'));
  for (const method of ["vision_click", "vision_click_control", "vision_drag", "vision_hover", "vision_type_control", "vision_key"]) {
    assert.ok(SRC.includes(`"${method}",`), `${method} is guarded`);
  }
});

test("an invisible CAPTCHA frame under a cookie banner's Dismiss button is not a CAPTCHA click", () => {
  // Run 65 on Lever: the invisible hCaptcha frame lay under the privacy banner, and Dismiss was refused.
  const dom = new JSDOM(
    `<!doctype html><body>
      <iframe id="hc" style="opacity: 0" src="https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=challenge"></iframe>
      <iframe id="hc2" src="https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=checkbox"></iframe>
      <div id="banner"><button id="dismiss">Dismiss</button></div>
    </body>`,
    { url: "https://jobs.lever.co/ion/x/apply", runScripts: "outside-only" },
  );
  const boxes = { hc: [0, 0, 1120, 600], hc2: [800, 500, 1100, 590], banner: [0, 520, 1120, 600], dismiss: [1020, 545, 1093, 571] };
  dom.window.Element.prototype.getBoundingClientRect = function () {
    const [left, top, right, bottom] = boxes[this.id] || [0, 0, 0, 0];
    return { left, top, right, bottom, width: right - left, height: bottom - top };
  };
  stubElementFromPoint(dom, boxes);
  const inPage = dom.window.eval(`(${fnSource("captchaAtTargetInPage")})`);
  const check = (...args) => JSON.parse(JSON.stringify(inPage(...args)));
  // Dismiss is on top of both frames; the transparent challenge frame never counts.
  assert.deepEqual(check([[1056, 558]], "", false), { captcha: false });
  assert.deepEqual(check([[100, 100]], "", false), { captcha: false });
  // A visible frame that is the topmost element at the point still counts.
  assert.deepEqual(check([[900, 510]], "", false), { captcha: true, provider: "hcaptcha", via: "point" });
});

test("an invisible widget's badge is not a CAPTCHA, and keys in a solved widget's frame are told to click first", () => {
  const dom = new JSDOM(
    `<!doctype html><body>
      <iframe id="badge" title="reCAPTCHA" src="https://www.google.com/recaptcha/api2/anchor?ar=1&k=x&size=invisible"></iframe>
      <iframe id="anchor" title="reCAPTCHA" src="https://www.google.com/recaptcha/api2/anchor?k=x&size=normal"></iframe>
      <textarea name="g-recaptcha-response" style="display:none"></textarea>
      <input id="email" type="email">
    </body>`,
    { url: "https://zealogicsllc.applytojob.com/apply/x", runScripts: "outside-only" },
  );
  const boxes = { badge: [1100, 800, 1356, 860], anchor: [100, 600, 404, 678], email: [100, 100, 400, 140] };
  dom.window.Element.prototype.getBoundingClientRect = function () {
    const [left, top, right, bottom] = boxes[this.id] || [0, 0, 0, 0];
    return { left, top, right, bottom, width: right - left, height: bottom - top };
  };
  stubElementFromPoint(dom, boxes);
  const inPage = dom.window.eval(`(${fnSource("captchaAtTargetInPage")})`);
  const check = (...args) => JSON.parse(JSON.stringify(inPage(...args)));
  assert.deepEqual(check([[1200, 830]], "", false), { captcha: false });
  dom.window.eval(`globalThis.__lumenDeepActiveElement = () => document.getElementById("anchor")`);
  // Unsolved: still the user's to answer.
  assert.deepEqual(check([], "", true), { captcha: true, provider: "recaptcha", via: "focus" });
  dom.window.document.querySelector('textarea[name="g-recaptcha-response"]').value = "03AFcWeA-token";
  assert.deepEqual(check([], "", true), { captcha: true, provider: "recaptcha", via: "focus", solved: true });
});

test("a selector-bound type is not checked against focus: it focuses its own field", () => {
  const guard = fnSource("captchaRefusalFor");
  assert.ok(guard.includes('const checkFocus = method === "vision_key" || method === "vision_type";'));
  assert.ok(guard.includes('failure_reason: "captcha_frame_focused"'));
});
