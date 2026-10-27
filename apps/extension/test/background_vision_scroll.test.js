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

function makeWindow(html) {
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    url: "https://example.test/apply",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  Object.defineProperty(dom.window, "scrollY", { value: 400, writable: true, configurable: true });
  return dom;
}

function expectedIdentity(overrides = {}) {
  return {
    target_selector: "#menu-option",
    target_text: "New Jersey",
    target_tag: "button",
    target_type: "",
    target_role: "option",
    dispatch_path: "dom_activation",
    page_url: "https://example.test/apply",
    ...overrides,
  };
}

function expectedPageContext(overrides = {}) {
  return {
    url: "https://example.test/apply",
    document_lifecycle_token: "document-one",
    document_state_signature: "state-one",
    ...overrides,
  };
}

function installStructuralContext(dom, overrides = {}) {
  const lifecycle = overrides.lifecycle || "document-one";
  const state = overrides.state || "state-one";
  Object.defineProperty(dom.window, "__lumenDocumentLifecycleToken", {
    value: lifecycle,
    writable: true,
    configurable: true,
  });
  Object.defineProperty(dom.window, "__lumenDocumentStateSignature", {
    value: () => state,
    configurable: true,
  });
  Object.defineProperty(dom.window, "__lumenStableDeepSelector", {
    value: (node) => (node && node.id ? `#${node.id}` : ""),
    configurable: true,
  });
}

test("selector-bound scroll moves the nearest nested scroll container, not the page", () => {
  const dom = makeWindow(`
    <div id="outer">
      <div id="menu" style="overflow-y:auto">
        <button id="menu-option" role="option">New Jersey</button>
      </div>
    </div>
  `);
  try {
    installStructuralContext(dom);
    const menu = dom.window.document.querySelector("#menu");
    Object.defineProperty(menu, "scrollHeight", { value: 600, configurable: true });
    Object.defineProperty(menu, "clientHeight", { value: 100, configurable: true });
    Object.defineProperty(menu, "scrollTop", { value: 0, writable: true, configurable: true });
    const scrollTarget = dom.window.eval(`(${extractFunctionSource("scrollTargetInPage")})`);

    const result = scrollTarget(
      "#menu-option",
      180,
      expectedIdentity(),
      expectedPageContext(),
    );

    assert.equal(result.ok, true);
    assert.equal(result.mode, "element_scroll");
    assert.equal(result.scroll_target_kind, "container");
    assert.match(result.scroll_container_identity_hash, /^[a-f0-9]{32}$/);
    assert.equal(Object.hasOwn(result, "scroll_target_selector"), false);
    assert.equal(Object.hasOwn(result, "scroll_container_selector"), false);
    assert.equal(Object.hasOwn(result, "scroll_container_tag"), false);
    assert.equal(JSON.stringify(result).includes("#menu"), false);
    assert.equal(JSON.stringify(result).includes("New Jersey"), false);
    assert.equal(result.before_scroll_position, 0);
    assert.equal(result.scroll_position, 180);
    assert.equal(result.scroll_changed, true);
    assert.equal(result.before_scroll_y, 400);
    assert.equal(result.scroll_y, 400);
    assert.equal(menu.scrollTop, 180);

    Object.defineProperty(dom.window, "__lumenStableDeepSelector", {
      value: () => "",
      configurable: true,
    });
    menu.scrollTop = 0;
    const selectorlessContainer = scrollTarget(
      "#menu-option",
      180,
      expectedIdentity(),
      expectedPageContext(),
    );
    assert.equal(selectorlessContainer.ok, true);
    assert.equal(selectorlessContainer.scroll_container_identity_hash, "");
  } finally {
    dom.window.close();
  }
});

test("selector-bound sideways scroll moves the nearest horizontally scrollable ancestor", () => {
  const dom = makeWindow(`
    <div id="page-col" style="overflow-y:auto">
      <div id="lane" style="overflow-x:auto">
        <button id="menu-option" role="option">New Jersey</button>
      </div>
    </div>
  `);
  try {
    installStructuralContext(dom);
    const column = dom.window.document.querySelector("#page-col");
    Object.defineProperty(column, "scrollHeight", { value: 900, configurable: true });
    Object.defineProperty(column, "clientHeight", { value: 300, configurable: true });
    Object.defineProperty(column, "scrollTop", { value: 0, writable: true, configurable: true });
    const lane = dom.window.document.querySelector("#lane");
    Object.defineProperty(lane, "scrollWidth", { value: 2400, configurable: true });
    Object.defineProperty(lane, "clientWidth", { value: 600, configurable: true });
    Object.defineProperty(lane, "scrollLeft", { value: 0, writable: true, configurable: true });
    const scrollTarget = dom.window.eval(`(${extractFunctionSource("scrollTargetInPage")})`);

    const result = scrollTarget("#menu-option", 0, expectedIdentity(), expectedPageContext(), 350);

    assert.equal(result.ok, true);
    assert.equal(result.scroll_axis, "horizontal");
    assert.equal(result.before_scroll_position, 0);
    assert.equal(result.scroll_position, 350);
    assert.equal(result.scroll_changed, true);
    assert.equal(result.scroll_container_scroll_width, 2400);
    assert.equal(result.scroll_container_client_width, 600);
    assert.equal(lane.scrollLeft, 350);
    assert.equal(column.scrollTop, 0);

    const vertical = scrollTarget("#menu-option", 120, expectedIdentity(), expectedPageContext());
    assert.equal(vertical.ok, true);
    assert.equal(vertical.scroll_axis, "vertical");
    assert.equal(column.scrollTop, 120);
    assert.equal(lane.scrollLeft, 350);
  } finally {
    dom.window.close();
  }
});

test("selector-bound scroll accepts any observed name of the exact control", () => {
  const dom = makeWindow(`
    <div id="menu" style="overflow-y:auto">
      <label>Amount for LX-437 <input id="menu-option" placeholder="0.00"></label>
    </div>
  `);
  try {
    installStructuralContext(dom);
    const menu = dom.window.document.querySelector("#menu");
    Object.defineProperty(menu, "scrollHeight", { value: 600, configurable: true });
    Object.defineProperty(menu, "clientHeight", { value: 100, configurable: true });
    Object.defineProperty(menu, "scrollTop", { value: 0, writable: true, configurable: true });
    const scrollTarget = dom.window.eval(`(${extractFunctionSource("scrollTargetInPage")})`);
    const identity = expectedIdentity({ target_text: "Amount for LX-437", target_tag: "input", target_role: "" });

    assert.equal(scrollTarget("#menu-option", 90, identity, expectedPageContext()).ok, false);
    const named = scrollTarget(
      "#menu-option",
      90,
      { ...identity, target_names: ["0.00", "Amount for LX-437"] },
      expectedPageContext(),
    );
    assert.equal(named.ok, true);
    assert.equal(menu.scrollTop, 90);
    const stranger = scrollTarget(
      "#menu-option",
      90,
      { ...identity, target_names: ["Some other field"] },
      expectedPageContext(),
    );
    assert.equal(stranger.ok, false);
    assert.equal(stranger.failure_reason, "scroll_target_drift");
  } finally {
    dom.window.close();
  }
});

test("selector-bound scroll follows one exact accessibility-controlled sibling region", () => {
  const dom = makeWindow(`
    <button id="country-trigger" aria-controls="country-popup">Country/Region</button>
    <div id="country-popup">
      <div id="country-list" style="overflow-y:auto">
        <div role="option">Alabama</div>
        <div role="option">New Jersey</div>
      </div>
    </div>
  `);
  try {
    installStructuralContext(dom);
    const list = dom.window.document.querySelector("#country-list");
    Object.defineProperty(list, "scrollHeight", { value: 900, configurable: true });
    Object.defineProperty(list, "clientHeight", { value: 160, configurable: true });
    Object.defineProperty(list, "scrollTop", { value: 40, writable: true, configurable: true });
    const scrollTarget = dom.window.eval(`(${extractFunctionSource("scrollTargetInPage")})`);

    const result = scrollTarget(
      "#country-trigger",
      180,
      expectedIdentity({
        target_selector: "#country-trigger",
        target_text: "Country/Region",
        target_tag: "button",
        target_role: "",
      }),
      expectedPageContext(),
    );

    assert.equal(result.ok, true);
    assert.equal(result.mode, "element_scroll");
    assert.equal(result.scroll_target_kind, "container");
    assert.equal(result.target_identity_matched, true);
    assert.equal(result.before_scroll_position, 40);
    assert.equal(result.scroll_position, 220);
    assert.equal(result.scroll_changed, true);
    assert.equal(list.scrollTop, 220);
    assert.equal(dom.window.scrollY, 400);
    assert.match(result.scroll_container_identity_hash, /^[a-f0-9]{32}$/);
    assert.equal(JSON.stringify(result).includes("country-popup"), false);
    assert.equal(JSON.stringify(result).includes("country-list"), false);
    assert.equal(JSON.stringify(result).includes("Country/Region"), false);
  } finally {
    dom.window.close();
  }
});

test("selector-bound scroll fails closed when an accessibility relation resolves to multiple scrollable regions", () => {
  const dom = makeWindow(`
    <button id="region-trigger" aria-controls="region-a region-b">Open region</button>
    <div id="region-a" style="overflow-y:auto"><div>First region</div></div>
    <div id="region-b" style="overflow-y:auto"><div>Second region</div></div>
  `);
  try {
    installStructuralContext(dom);
    const regions = [
      dom.window.document.querySelector("#region-a"),
      dom.window.document.querySelector("#region-b"),
    ];
    for (const region of regions) {
      Object.defineProperty(region, "scrollHeight", { value: 700, configurable: true });
      Object.defineProperty(region, "clientHeight", { value: 120, configurable: true });
      Object.defineProperty(region, "scrollTop", { value: 0, writable: true, configurable: true });
    }
    const scrollTarget = dom.window.eval(`(${extractFunctionSource("scrollTargetInPage")})`);

    const result = scrollTarget(
      "#region-trigger",
      180,
      expectedIdentity({
        target_selector: "#region-trigger",
        target_text: "Open region",
        target_tag: "button",
        target_role: "",
      }),
      expectedPageContext(),
    );

    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "scroll_container_unavailable");
    assert.equal(result.scroll_attempted, false);
    assert.equal(result.target_identity_matched, true);
    assert.deepEqual(regions.map((region) => region.scrollTop), [0, 0]);
    assert.equal(dom.window.scrollY, 400);
  } finally {
    dom.window.close();
  }
});

test("targeted scroll fails closed without an exact target or nested scroll container", () => {
  const dom = makeWindow(`
    <button class="duplicate" id="menu-option" role="option">New Jersey</button>
    <button class="duplicate" role="option">New Jersey</button>
  `);
  try {
    installStructuralContext(dom);
    const scrollTarget = dom.window.eval(`(${extractFunctionSource("scrollTargetInPage")})`);

    const missing = scrollTarget(
      "#missing",
      180,
      expectedIdentity({ target_selector: "#missing" }),
      expectedPageContext(),
    );
    assert.equal(missing.ok, false);
    assert.equal(missing.failure_reason, "control_not_found");
    assert.equal(missing.scroll_attempted, false);

    const ambiguous = scrollTarget(
      ".duplicate",
      180,
      expectedIdentity({ target_selector: ".duplicate" }),
      expectedPageContext(),
    );
    assert.equal(ambiguous.ok, false);
    assert.equal(ambiguous.failure_reason, "control_selector_ambiguous");
    assert.equal(ambiguous.scroll_attempted, false);

    const drifted = scrollTarget(
      "#menu-option",
      180,
      expectedIdentity({ target_text: "Arizona" }),
      expectedPageContext(),
    );
    assert.equal(drifted.ok, false);
    assert.equal(drifted.failure_reason, "scroll_target_drift");
    assert.equal(drifted.target_identity_mismatch_field, "target_text");
    assert.equal(drifted.scroll_attempted, false);

    const noContainer = scrollTarget(
      "#menu-option",
      180,
      expectedIdentity(),
      expectedPageContext(),
    );
    assert.equal(noContainer.ok, false);
    assert.equal(noContainer.failure_reason, "scroll_container_unavailable");
    assert.equal(noContainer.scroll_attempted, false);
    assert.equal(dom.window.scrollY, 400);
  } finally {
    dom.window.close();
  }
});

test("targeted scroll atomically rejects same-URL document replacement before mutation", () => {
  const dom = makeWindow(`
    <div id="menu" style="overflow-y:auto">
      <button id="menu-option" role="option">New Jersey</button>
    </div>
  `);
  try {
    const menu = dom.window.document.querySelector("#menu");
    Object.defineProperty(menu, "scrollHeight", { value: 600, configurable: true });
    Object.defineProperty(menu, "clientHeight", { value: 100, configurable: true });
    Object.defineProperty(menu, "scrollTop", { value: 0, writable: true, configurable: true });
    let lifecycleReads = 0;
    Object.defineProperty(dom.window, "__lumenDocumentLifecycleToken", {
      get() {
        lifecycleReads += 1;
        return lifecycleReads === 1 ? "document-one" : "same-url-replacement";
      },
      configurable: true,
    });
    Object.defineProperty(dom.window, "__lumenDocumentStateSignature", {
      value: () => "state-one",
      configurable: true,
    });
    Object.defineProperty(dom.window, "__lumenStableDeepSelector", {
      value: (node) => (node && node.id ? `#${node.id}` : ""),
      configurable: true,
    });
    const scrollTarget = dom.window.eval(`(${extractFunctionSource("scrollTargetInPage")})`);

    const result = scrollTarget(
      "#menu-option",
      180,
      expectedIdentity(),
      expectedPageContext(),
    );

    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "page_context_drift");
    assert.equal(result.page_context_mismatch_field, "document_lifecycle_token");
    assert.equal(result.scroll_attempted, false);
    assert.equal(menu.scrollTop, 0);
    assert.equal(dom.window.scrollY, 400);
  } finally {
    dom.window.close();
  }
});

test("targeted scroll at a container boundary is attempted but verification-inconclusive", () => {
  const dom = makeWindow(`
    <div id="menu" style="overflow-y:auto">
      <button id="menu-option" role="option">New Jersey</button>
    </div>
  `);
  try {
    installStructuralContext(dom);
    const menu = dom.window.document.querySelector("#menu");
    Object.defineProperty(menu, "scrollHeight", { value: 600, configurable: true });
    Object.defineProperty(menu, "clientHeight", { value: 100, configurable: true });
    Object.defineProperty(menu, "scrollTop", { value: 500, writable: true, configurable: true });
    const scrollTarget = dom.window.eval(`(${extractFunctionSource("scrollTargetInPage")})`);

    const result = scrollTarget(
      "#menu-option",
      180,
      expectedIdentity(),
      expectedPageContext(),
    );

    assert.equal(result.ok, true);
    assert.equal(result.scroll_attempted, true);
    assert.equal(result.scroll_changed, false);
    assert.equal(result.before_scroll_position, 500);
    assert.equal(result.scroll_position, 500);
    assert.equal(menu.scrollTop, 500);
    assert.equal(dom.window.scrollY, 400);
  } finally {
    dom.window.close();
  }
});

test("targeted scroll reports unknown outcome when post-mutation receipt read-back throws", () => {
  const dom = makeWindow(`
    <div id="menu" style="overflow-y:auto">
      <button id="menu-option" role="option">New Jersey</button>
    </div>
  `);
  try {
    installStructuralContext(dom);
    const menu = dom.window.document.querySelector("#menu");
    let scrollHeightReads = 0;
    Object.defineProperty(menu, "scrollHeight", {
      get() {
        scrollHeightReads += 1;
        if (scrollHeightReads >= 3) throw new Error("post-mutation read-back failed");
        return 600;
      },
      configurable: true,
    });
    Object.defineProperty(menu, "clientHeight", { value: 100, configurable: true });
    Object.defineProperty(menu, "scrollTop", { value: 0, writable: true, configurable: true });
    const scrollTarget = dom.window.eval(`(${extractFunctionSource("scrollTargetInPage")})`);

    const result = scrollTarget(
      "#menu-option",
      180,
      expectedIdentity(),
      expectedPageContext(),
    );

    assert.equal(menu.scrollTop, 180);
    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "scroll_execution_outcome_unknown");
    assert.equal(result.scroll_attempted, true);
    assert.equal(result.browser_mutation_attempted, true);
    assert.equal(result.execution_outcome_unknown, true);
  } finally {
    dom.window.close();
  }
});

test("scroll command receipt drops every raw page-controlled container string", () => {
  const projectReceipt = eval(`(${extractFunctionSource("scrollCommandReceipt")})`);
  const result = projectReceipt({
    ok: true,
    mode: "element_scroll",
    scroll_target_kind: "container",
    scroll_target_selector: "#page-owned-target",
    scroll_container_selector: "#page-owned-container",
    scroll_container_tag: "page-owned-tag",
    scroll_container_identity_hash: "0123456789abcdef0123456789abcdef",
    scroll_attempted: true,
    target_identity_matched: true,
    before_scroll_position: 0,
    scroll_position: 100,
  }, "element_scroll");

  assert.equal(result.scroll_container_identity_hash, "0123456789abcdef0123456789abcdef");
  assert.equal(Object.hasOwn(result, "scroll_target_selector"), false);
  assert.equal(Object.hasOwn(result, "scroll_container_selector"), false);
  assert.equal(Object.hasOwn(result, "scroll_container_tag"), false);
  assert.equal(JSON.stringify(result).includes("page-owned"), false);
});

test("targeted scroll never backfills missing observed document identity from preflight", async () => {
  const dom = makeWindow("<button id=\"menu-option\">Option</button>");
  try {
    let preflightCalls = 0;
    dom.window.clickTargetIdentityComplete = () => true;
    dom.window.scrollCommandReceipt = dom.window.eval(
      `(${extractFunctionSource("scrollCommandReceipt")})`,
    );
    dom.window.showPointerScrollHint = () => {};
    dom.window.observedPageContextVerdict = async () => {
      preflightCalls += 1;
      return {
        ok: true,
        live: {
          url: "https://example.test/apply",
          document_lifecycle_token: "unobserved-live-document",
          document_state_signature: "unobserved-live-state",
        },
      };
    };
    const runScroll = dom.window.eval(`(${extractFunctionSource("runVisionScroll")})`);

    const result = await runScroll(
      1,
      180,
      "#menu-option",
      expectedIdentity(),
      { url: "https://example.test/apply" },
    );

    assert.equal(result.ok, false);
    assert.equal(result.failure_reason, "page_context_required");
    assert.equal(result.scroll_attempted, false);
    assert.equal(preflightCalls, 0);
  } finally {
    dom.window.close();
  }
});

test("targeted scroll distinguishes definite pre-dispatch failure from unknown post-dispatch outcome", async () => {
  const dom = makeWindow("<button id=\"menu-option\">Option</button>");
  try {
    dom.window.clickTargetIdentityComplete = () => true;
    dom.window.scrollCommandReceipt = dom.window.eval(
      `(${extractFunctionSource("scrollCommandReceipt")})`,
    );
    dom.window.showPointerScrollHint = () => {};
    dom.window.observedPageContextVerdict = async () => ({ ok: true, live: {} });
    dom.window.withTimeout = async (promise) => promise;
    dom.window.scrollTargetInPage = () => ({});
    const makeRunScroll = () => dom.window.eval(`
      ((chrome, clickTargetIdentityComplete, scrollCommandReceipt, observedPageContextVerdict, withTimeout, scrollTargetInPage) =>
        (${extractFunctionSource("runVisionScroll")})
      )(
        globalThis.chrome,
        globalThis.clickTargetIdentityComplete,
        globalThis.scrollCommandReceipt,
        globalThis.observedPageContextVerdict,
        globalThis.withTimeout,
        globalThis.scrollTargetInPage
      )
    `);

    let dispatches = 0;
    Object.defineProperty(dom.window, "chrome", {
      value: {
        scripting: {
          executeScript() {
            dispatches += 1;
            return Promise.reject(new Error("post-dispatch channel rejection"));
          },
        },
      },
      configurable: true,
    });
    const runScroll = makeRunScroll();
    const unknown = await runScroll(
      1,
      180,
      "#menu-option",
      expectedIdentity(),
      expectedPageContext(),
    );

    assert.equal(dispatches, 1);
    assert.equal(unknown.ok, false);
    assert.equal(unknown.failure_reason, "scroll_execution_outcome_unknown");
    assert.equal(unknown.scroll_attempted, true);
    assert.equal(unknown.browser_mutation_attempted, true);
    assert.equal(unknown.execution_outcome_unknown, true);

    Object.defineProperty(dom.window, "chrome", {
      value: {
        scripting: {
          executeScript() {
            throw new Error("synchronous pre-dispatch rejection");
          },
        },
      },
      configurable: true,
    });
    const definitelyNotAttempted = await makeRunScroll()(
      1,
      180,
      "#menu-option",
      expectedIdentity(),
      expectedPageContext(),
    );

    assert.equal(definitelyNotAttempted.ok, false);
    assert.equal(definitelyNotAttempted.failure_reason, "scroll_execution_failed");
    assert.equal(definitelyNotAttempted.scroll_attempted, false);
    assert.equal(definitelyNotAttempted.browser_mutation_attempted, false);
    assert.equal(definitelyNotAttempted.execution_outcome_unknown, false);
  } finally {
    dom.window.close();
  }
});

test("unbound document scroll preserves the existing page-scroll contract", () => {
  const dom = makeWindow("<main>Document</main>");
  try {
    const scrolling = dom.window.document.documentElement;
    Object.defineProperty(dom.window.document, "scrollingElement", { value: scrolling, configurable: true });
    Object.defineProperty(scrolling, "scrollHeight", { value: 1600, configurable: true });
    Object.defineProperty(scrolling, "scrollTop", { value: 400, writable: true, configurable: true });
    scrolling.scrollBy = ({ top }) => {
      scrolling.scrollTop += Number(top || 0);
      dom.window.scrollY = scrolling.scrollTop;
    };
    const scrollPage = dom.window.eval(`(${extractFunctionSource("scrollByInPage")})`);

    const result = scrollPage(200);

    assert.equal(result.ok, true);
    assert.equal(result.mode, "document_scroll");
    assert.equal(result.scroll_target_kind, "page");
    assert.equal(result.before_scroll_position, 400);
    assert.equal(result.scroll_position, 600);
    assert.equal(result.scroll_changed, true);
  } finally {
    dom.window.close();
  }
});
