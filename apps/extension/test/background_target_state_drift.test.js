const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { JSDOM, VirtualConsole } = require("jsdom");

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

const URL_ = "https://login.example.test/sign-in";
const TOKEN = "doc-1-token";
const row = (selector, frame, digest) => ({ selector, frame, digest });

// The observed page: a username, a password, a submit button, and the title row (selector "").
function observedRows() {
  return [
    row("#username", 0, "u1"),
    row("#password", 0, "p1"),
    row("#submit", 0, "s1"),
    row("", 0, "title-1"),
  ];
}

function expectedContext(overrides = {}) {
  return {
    url: URL_,
    document_lifecycle_token: TOKEN,
    document_state_signature: "state-observed",
    document_row_digests: observedRows(),
    ...overrides,
  };
}

// Builds the verdict under test with a stubbed live probe, exactly as the background evaluates it.
function verdictFor(live) {
  const tabPageEvidence = async () => ({
    url: URL_,
    surface_probe_complete: true,
    document_lifecycle_token: TOKEN,
    document_state_signature: "state-live",
    document_row_digests: observedRows(),
    ...live,
  });
  const targetOnlyStateDriftConfined = eval(`(${extractFunctionSource("targetOnlyStateDriftConfined")})`);
  const observedPageContextVerdict = eval(`(${extractFunctionSource("observedPageContextVerdict")})`);
  return {
    targetOnlyStateDriftConfined,
    observedPageContextVerdict,
    run(expected, targetScope) {
      return observedPageContextVerdict(1, expected, true, false, false, targetScope);
    },
  };
}

const CREDENTIAL_SCOPE = { selector: "#username", topDocumentOnly: true };

test("a credential's own field may change while every other row, the title, the URL and the token hold", async () => {
  // Run 1794 (Quarterhill login): the username row changed between observation and typing; nothing else did.
  const live = verdictFor({ document_row_digests: [
    row("#username", 0, "u2"), row("#password", 0, "p1"), row("#submit", 0, "s1"), row("", 0, "title-1"),
  ] });
  const verdict = await live.run(expectedContext(), CREDENTIAL_SCOPE);
  assert.equal(verdict.ok, true);
  assert.equal(verdict.target_state_drift, true);
  assert.equal(verdict.page_drifted, false);
});

test("without a target scope the same drift stays refused (strict whole-page check)", async () => {
  const live = verdictFor({ document_row_digests: [
    row("#username", 0, "u2"), row("#password", 0, "p1"), row("#submit", 0, "s1"), row("", 0, "title-1"),
  ] });
  const verdict = await live.run(expectedContext(), null);
  assert.equal(verdict.ok, false);
  assert.equal(verdict.mismatch_field, "document_state_signature");
});

test("a change to any other control refuses even when the target's row also changed", async () => {
  const live = verdictFor({ document_row_digests: [
    row("#username", 0, "u2"), row("#password", 0, "p2"), row("#submit", 0, "s1"), row("", 0, "title-1"),
  ] });
  const verdict = await live.run(expectedContext(), CREDENTIAL_SCOPE);
  assert.equal(verdict.ok, false);
  assert.equal(verdict.mismatch_field, "document_state_signature");
});

test("a changed title refuses", async () => {
  const live = verdictFor({ document_row_digests: [
    row("#username", 0, "u2"), row("#password", 0, "p1"), row("#submit", 0, "s1"), row("", 0, "title-2"),
  ] });
  const verdict = await live.run(expectedContext(), CREDENTIAL_SCOPE);
  assert.equal(verdict.ok, false);
});

test("a row added or removed refuses", async () => {
  const removed = verdictFor({ document_row_digests: [
    row("#username", 0, "u2"), row("#password", 0, "p1"), row("", 0, "title-1"),
  ] });
  assert.equal((await removed.run(expectedContext(), CREDENTIAL_SCOPE)).ok, false);
  const added = verdictFor({ document_row_digests: [
    row("#username", 0, "u2"), row("#password", 0, "p1"), row("#submit", 0, "s1"), row("#error", 0, "e1"), row("", 0, "title-1"),
  ] });
  assert.equal((await added.run(expectedContext(), CREDENTIAL_SCOPE)).ok, false);
});

test("a credential target inside a frame keeps the strict check; an ordinary target may follow its own row", async () => {
  const frameExpected = expectedContext({ document_row_digests: [
    row("#username", 1, "u1"), row("#password", 0, "p1"), row("", 0, "title-1"),
  ] });
  const frameLive = verdictFor({ document_row_digests: [
    row("#username", 1, "u2"), row("#password", 0, "p1"), row("", 0, "title-1"),
  ] });
  const credential = await frameLive.run(frameExpected, { selector: "#username", topDocumentOnly: true });
  assert.equal(credential.ok, false, "a frame-hosted credential is not confined to the top document");
  const ordinary = await frameLive.run(frameExpected, { selector: "#username", topDocumentOnly: false });
  assert.equal(ordinary.ok, true, "an ordinary target in a frame may follow its own row");
});

test("a target whose frame flag flips refuses", async () => {
  const live = verdictFor({ document_row_digests: [
    row("#username", 1, "u1"), row("#password", 0, "p1"), row("#submit", 0, "s1"), row("", 0, "title-1"),
  ] });
  const verdict = await live.run(expectedContext(), CREDENTIAL_SCOPE);
  assert.equal(verdict.ok, false);
});

test("a missing, capped or repeated row list proves nothing and refuses", async () => {
  const withoutObserved = verdictFor({ document_row_digests: [
    row("#username", 0, "u2"), row("#password", 0, "p1"), row("#submit", 0, "s1"), row("", 0, "title-1"),
  ] });
  assert.equal((await withoutObserved.run(expectedContext({ document_row_digests: [] }), CREDENTIAL_SCOPE)).ok, false);
  const withoutLive = verdictFor({ document_row_digests: [] });
  assert.equal((await withoutLive.run(expectedContext(), CREDENTIAL_SCOPE)).ok, false);
  const repeated = verdictFor({ document_row_digests: [
    row("#username", 0, "u2"), row("#username", 0, "u2"), row("#submit", 0, "s1"), row("", 0, "title-1"),
  ] });
  assert.equal((await repeated.run(expectedContext(), CREDENTIAL_SCOPE)).ok, false);
});

test("a changed URL or lifecycle token refuses, and the token verdict reports both leading prefixes", async () => {
  const navigated = verdictFor({ url: "https://login.example.test/other" });
  const nav = await navigated.run(expectedContext(), CREDENTIAL_SCOPE);
  assert.equal(nav.ok, false);
  assert.equal(nav.mismatch_field, "url");

  const replaced = verdictFor({ document_lifecycle_token: "doc-2-token" });
  const token = await replaced.run(expectedContext(), CREDENTIAL_SCOPE);
  assert.equal(token.ok, false);
  assert.equal(token.mismatch_field, "document_lifecycle_token");
  assert.equal(token.lifecycle_expected_prefix, "doc-1-to");
  assert.equal(token.lifecycle_live_prefix, "doc-2-to");
});

test("the target-only rule itself: the named row is the only one allowed to differ", () => {
  const { targetOnlyStateDriftConfined } = verdictFor({});
  const expected = observedRows();
  const sameTargetChanged = expected.map((item) => (item.selector === "#username" ? row("#username", 0, "x") : item));
  assert.equal(targetOnlyStateDriftConfined(expected, sameTargetChanged, CREDENTIAL_SCOPE), true);
  assert.equal(targetOnlyStateDriftConfined(expected, expected, { selector: "", topDocumentOnly: true }), false);
  assert.equal(targetOnlyStateDriftConfined(expected, expected, { selector: "#absent", topDocumentOnly: true }), false);
});

test("page evidence digests one row per control plus the title, and a change moves only its own digest", () => {
  const html = (value) => `<!doctype html><html><head><title>Sign in</title></head><body>
    <div data-x="0" data-y="0" data-width="600" data-height="400">
      <input id="username" type="text" value="${value}" data-x="10" data-y="10" data-width="200" data-height="24">
      <input id="password" type="password" data-x="10" data-y="50" data-width="200" data-height="24">
      <button id="submit" data-x="10" data-y="90" data-width="120" data-height="30">Sign in</button>
    </div></body></html>`;
  const evidenceFor = (value) => {
    // jsdom has no getComputedStyle pseudo-element support; the page probe reads it, so its notices are muted.
    const virtualConsole = new VirtualConsole();
    const dom = new JSDOM(html(value), {
      url: URL_,
      runScripts: "outside-only",
      pretendToBeVisual: true,
      virtualConsole,
    });
    Object.defineProperty(dom.window, "devicePixelRatio", { value: 1, configurable: true });
    Object.defineProperty(dom.window, "innerHeight", { value: 900, configurable: true });
    Object.defineProperty(dom.window, "innerWidth", { value: 1200, configurable: true });
    dom.window.Element.prototype.getBoundingClientRect = function () {
      const left = Number(this.getAttribute("data-x") || 0);
      const top = Number(this.getAttribute("data-y") || 0);
      const width = Number(this.getAttribute("data-width") || 100);
      const height = Number(this.getAttribute("data-height") || 24);
      return { left, top, right: left + width, bottom: top + height, width, height, x: left, y: top };
    };
    try {
      const fn = dom.window.eval(`(${extractFunctionSource("_visionPageEvidence")})`);
      // Round-trip through JSON: the rows come from the page's own realm, not this one.
      return JSON.parse(JSON.stringify(fn([]).document_row_digests));
    } finally {
      dom.window.close();
    }
  };
  const before = evidenceFor("alice");
  const again = evidenceFor("alice");
  const edited = evidenceFor("bob");
  assert.ok(Array.isArray(before) && before.length >= 4, "one row per visible control plus the title");
  assert.deepEqual(before, again, "the same page digests the same");
  assert.equal(before[before.length - 1].selector, "", "the title is the last row");
  const byKey = (rows) => new Map(rows.map((item) => [item.selector, item.digest]));
  const a = byKey(before);
  const b = byKey(edited);
  const changed = [...a.keys()].filter((key) => a.get(key) !== b.get(key));
  assert.deepEqual(changed, ["#username"], "only the edited control's digest moves");
  assert.ok(before.every((item) => item.frame === 0), "top-document rows carry frame 0");
});
