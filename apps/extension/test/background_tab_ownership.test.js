const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const BACKGROUND_SRC = fs.readFileSync(path.join(__dirname, "..", "src", "background.js"), "utf8");

function extractFunctionSource(name) {
  const asyncStart = BACKGROUND_SRC.indexOf(`async function ${name}`);
  const start = asyncStart >= 0 ? asyncStart : BACKGROUND_SRC.indexOf(`function ${name}`);
  assert.notEqual(start, -1, `${name} not found`);
  const bodyStart = BACKGROUND_SRC.indexOf("{", BACKGROUND_SRC.indexOf(")", start));
  let depth = 0;
  for (let i = bodyStart; i < BACKGROUND_SRC.length; i += 1) {
    const ch = BACKGROUND_SRC[i];
    if (ch === "{") depth += 1;
    if (ch === "}") depth -= 1;
    if (depth === 0) return BACKGROUND_SRC.slice(start, i + 1);
  }
  throw new Error(`${name} source did not close`);
}

test("new navigation creates a dedicated tab instead of replacing the tracked task tab", async () => {
  let targetTabId = 10;
  const TARGET_TAB_STORAGE_KEYS = ["lumenTargetTabId", "lumenTargetOwned"];
  const calls = [];
  const chrome = {
    tabs: {
      get: async (id) => {
        calls.push(["get", id]);
        return { id, url: "https://example.test/previous-task" };
      },
      update: async (id, options) => calls.push(["update", id, options]),
      create: async (options) => {
        calls.push(["create", options]);
        return { id: 77, url: options.url };
      },
    },
    storage: {
      local: {
        set: async (value) => calls.push(["set", value]),
        remove: async (keys) => calls.push(["remove", keys]),
      },
    },
  };
  const ensureTab = eval(`(${extractFunctionSource("ensureTab")})`);

  const result = await ensureTab("https://example.test/task", { newTab: true });

  assert.equal(result, 77);
  assert.equal(targetTabId, 77);
  assert.deepEqual(calls, [
    ["create", { url: "https://example.test/task", active: true }],
    ["set", { lumenTargetTabId: 77, lumenTargetOwned: true }],
  ]);
});

test("a windowless browser (profile picker) gets a window created instead of dying", async () => {
  // With several Chrome profiles the browser can open at the profile picker with ZERO normal
  // windows, where tabs.create throws "No current window".
  let targetTabId = null;
  const TARGET_TAB_STORAGE_KEYS = ["lumenTargetTabId", "lumenTargetOwned"];
  const calls = [];
  const chrome = {
    windows: {
      getAll: async (options) => {
        calls.push(["windows.getAll", options]);
        return [];
      },
      create: async (options) => {
        calls.push(["windows.create", options]);
        return { id: 5, tabs: [{ id: 88, url: options.url }] };
      },
    },
    tabs: {
      create: async () => {
        throw new Error("No current window");
      },
    },
    storage: {
      local: {
        set: async (value) => calls.push(["set", value]),
        remove: async (keys) => calls.push(["remove", keys]),
      },
    },
  };
  const ensureTab = eval(`(${extractFunctionSource("ensureTab")})`);

  const result = await ensureTab("https://www.atomtickets.com/", { newTab: true });

  assert.equal(result, 88);
  assert.equal(targetTabId, 88);
  assert.deepEqual(calls, [
    ["windows.getAll", { windowTypes: ["normal"] }],
    ["windows.create", { url: "https://www.atomtickets.com/", focused: true }],
    ["set", { lumenTargetTabId: 88, lumenTargetOwned: true }],
  ]);
});


test("restored targeting fails closed when storage has no ownership marker", async () => {
  let targetTabId = null;
  const TARGET_TAB_STORAGE_KEYS = ["lumenTargetTabId", "lumenTargetOwned"];
  const calls = [];
  const chrome = {
    tabs: {
      get: async (id) => {
        calls.push(["get", id]);
        return { id };
      },
    },
    storage: {
      local: {
        get: async () => ({ lumenTargetTabId: 10 }),
        remove: async (keys) => calls.push(["remove", keys]),
      },
    },
  };
  const clearTransientSensitiveScreenshotBinding = () => {};
  const activeTabId = eval(`(${extractFunctionSource("activeTabId")})`);

  const result = await activeTabId();

  assert.equal(result, null);
  assert.equal(targetTabId, null);
  assert.deepEqual(calls, [["remove", TARGET_TAB_STORAGE_KEYS]]);
});

test("post-click adoption follows only a newly created tab, never a pre-existing active tab", async () => {
  let targetTabId = 10;
  let runTabIds = [10];
  let rows = [
    { id: 10, active: false, url: "https://example.test/task" },
    { id: 20, active: true, url: "https://example.test/other-task" },
  ];
  const stored = [];
  const chrome = {
    tabs: { query: async () => rows },
    storage: { local: { set: async (value) => stored.push(value) } },
  };
  const clearTransientSensitiveScreenshotBinding = () => {};
  const persistRunTabIds = () => {};
  const controlIndicatorState = { active: false };
  const captureTabLineage = eval(`(${extractFunctionSource("captureTabLineage")})`);
  const adoptNewTabAfterClick = eval(`(${extractFunctionSource("adoptNewTabAfterClick")})`);
  const lineage = await captureTabLineage(10);

  const unchanged = await adoptNewTabAfterClick(10, lineage);
  assert.deepEqual(unchanged, { tabId: 10, adopted: false });
  assert.equal(targetTabId, 10);
  assert.deepEqual(stored, []);

  rows = [
    ...rows,
    { id: 30, active: true, openerTabId: 10, url: "https://example.test/next", title: "Next" },
  ];
  const adopted = await adoptNewTabAfterClick(10, lineage);

  assert.deepEqual(adopted, {
    tabId: 30,
    adopted: true,
    adopted_url: "https://example.test/next",
    adopted_title: "Next",
  });
  assert.equal(targetTabId, 30);
  assert.deepEqual(stored, [
    { lumenTargetTabId: 30, lumenTargetOwned: true },
  ]);
});


test("post-click adoption never claims a new tab that another tab opened", async () => {
  // The only new tab during the click was opened by the user's tab 88, not by the task tab.
  let targetTabId = 10;
  let runTabIds = [10];
  let rows = [
    { id: 10, active: true, url: "https://example.test/task" },
    { id: 88, active: false, url: "https://example.test/user-tab" },
  ];
  const stored = [];
  const chrome = {
    tabs: { query: async () => rows },
    storage: { local: { set: async (value) => stored.push(value) } },
  };
  const clearTransientSensitiveScreenshotBinding = () => {};
  const persistRunTabIds = () => {};
  const controlIndicatorState = { active: false };
  const captureTabLineage = eval(`(${extractFunctionSource("captureTabLineage")})`);
  const adoptNewTabAfterClick = eval(`(${extractFunctionSource("adoptNewTabAfterClick")})`);
  const lineage = await captureTabLineage(10);
  rows = [...rows, { id: 90, active: true, openerTabId: 88, url: "https://example.test/user-popup" }];

  const result = await adoptNewTabAfterClick(10, lineage);
  assert.deepEqual(result, { tabId: 10, adopted: false });
  assert.equal(targetTabId, 10);
  assert.deepEqual(runTabIds, [10]);

  rows = [...rows.filter((tab) => tab.id !== 90), { id: 91, active: true, url: "https://example.test/noopener-popup", title: "Popup" }];
  const openerless = await adoptNewTabAfterClick(10, lineage);
  assert.equal(openerless.adopted, true, "an opener-less popup still counts as this click's tab");
  assert.equal(openerless.tabId, 91);
});
