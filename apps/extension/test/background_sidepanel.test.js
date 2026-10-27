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

test("side panel opens synchronously against the notified task window", async () => {
  const calls = [];
  const chrome = {
    sidePanel: {
      open: (options) => {
        calls.push(["open", options]);
        return Promise.resolve();
      },
    },
    windows: { WINDOW_ID_CURRENT: -2 },
    tabs: {
      query: async () => {
        calls.push(["query"]);
        return [{ windowId: 99 }];
      },
    },
  };
  const errorMessage = (error) => String((error && error.message) || error || "");
  const openSidePanel = eval(`(${extractFunctionSource("openSidePanel")})`);

  const result = await openSidePanel({ windowId: 17 });

  assert.deepEqual(result, { ok: true });
  assert.deepEqual(calls, [["open", { windowId: 17 }]]);
});

test("side panel uses Chrome's current-window sentinel without an async lookup", async () => {
  const calls = [];
  const chrome = {
    sidePanel: {
      open: (options) => {
        calls.push(["open", options]);
        return Promise.resolve();
      },
    },
    windows: { WINDOW_ID_CURRENT: -2 },
    tabs: {
      query: async () => {
        calls.push(["query"]);
        return [{ windowId: 99 }];
      },
    },
  };
  const errorMessage = (error) => String((error && error.message) || error || "");
  const openSidePanel = eval(`(${extractFunctionSource("openSidePanel")})`);

  const result = await openSidePanel({});

  assert.deepEqual(result, { ok: true });
  assert.deepEqual(calls, [["open", { windowId: -2 }]]);
});

test("new task notification binds the Chrome window and tab for one-click panel activation", async () => {
  const TASK_PANEL_NOTIFICATION_PREFIX = "lumen-open-panel";
  const taskPanelNotificationId = eval(`(${extractFunctionSource("taskPanelNotificationId")})`);
  const parseTaskPanelNotificationId = eval(`(${extractFunctionSource("parseTaskPanelNotificationId")})`);

  const notificationId = taskPanelNotificationId(17, 42);

  assert.match(notificationId, /^lumen-open-panel-17-42-\d+$/);
  assert.deepEqual(parseTaskPanelNotificationId(notificationId), { windowId: 17, tabId: 42 });
  assert.equal(parseTaskPanelNotificationId("lumen-attention-42"), null);
});

test("new task tab requests panel attention only when the global panel is closed", async () => {
  const calls = [];
  const chrome = {
    notifications: {
      create: async (id, options) => calls.push([id, options]),
    },
  };
  const panelIsOpenForWindow = async () => false;
  const taskPanelNotificationId = () => "lumen-open-panel-17-42-123";
  const notificationIconUrl = () => "chrome-extension://lumen/icons/icon128.png";
  const errorMessage = (error) => String((error && error.message) || error || "");
  const pushTaskPanelNotification = eval(`(${extractFunctionSource("pushTaskPanelNotification")})`);

  const result = await pushTaskPanelNotification({ id: 42, windowId: 17 });

  assert.deepEqual(result, {
    notified: true,
    notification_id: "lumen-open-panel-17-42-123",
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].title, "Lumen task started in Chrome");
  assert.equal(calls[0][1].requireInteraction, true);
});

test("toolbar action opens the side panel directly instead of an intermediate popup", () => {
  const manifest = JSON.parse(
    fs.readFileSync(path.join(__dirname, "..", "manifest.json"), "utf8"),
  );

  assert.equal(manifest.action.default_title, "Open Lumen side panel");
  assert.equal(Object.hasOwn(manifest.action, "default_popup"), false);
  assert.match(BACKGROUND_SRC, /setPanelBehavior\(\{ openPanelOnActionClick: true \}\)/);
  assert.match(BACKGROUND_SRC, /const panelPromise = openSidePanel\(\{ windowId: taskPanelTarget\.windowId \}\)/);
  assert.match(BACKGROUND_SRC, /const panelNotice = openInNewTab\s*\?\s*await pushTaskPanelNotification\(tab\)/);
});

test("socket close schedules one bounded reconnect and a successful replacement resets backoff", async () => {
  const EXTENSION_BUILD = "2026-07-26-automatic-task-context-panel-v50";
  let socket = null;
  let commandSocketEpoch = 0;
  let connecting = false;
  let reconnectTimer = null;
  let reconnectAttempt = 0;
  let keepaliveTimer = null;
  let timerCallback = null;
  const delays = [];
  const statuses = [];
  const sockets = [];
  const intervals = [];
  const setTimeout = (callback, delay) => {
    timerCallback = callback;
    delays.push(delay);
    return 17;
  };
  const clearTimeout = () => { timerCallback = null; };
  const setInterval = (callback, delay) => {
    intervals.push(delay);
    return 23;
  };
  const clearInterval = () => {};
  const getConfig = async () => ({ backend: "http://127.0.0.1:8001", deviceId: "dev-stable" });
  const wsUrl = () => "ws://127.0.0.1:8001/extension/ws?device_id=dev-stable";
  const setStatus = async (connected) => { statuses.push(connected); };
  const setControlIndicator = async () => ({ ok: true });
  class WebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSED = 3;

    constructor(url) {
      this.url = url;
      this.readyState = WebSocket.CONNECTING;
      this.sent = [];
      sockets.push(this);
    }

    send(value) { this.sent.push(value); }
  }
  const clearReconnectTimer = eval(`(${extractFunctionSource("clearReconnectTimer")})`);
  const scheduleReconnect = eval(`(${extractFunctionSource("scheduleReconnect")})`);
  const connect = eval(`(${extractFunctionSource("connect")})`);

  await connect();
  const stale = sockets[0];
  stale.readyState = WebSocket.OPEN;
  stale.onopen();
  assert.equal(reconnectAttempt, 0);

  stale.readyState = WebSocket.CLOSED;
  stale.onclose();
  stale.onerror();
  assert.deepEqual(delays, [1000]);
  assert.equal(reconnectAttempt, 1);
  assert.equal(typeof timerCallback, "function");

  timerCallback();
  await Promise.resolve();
  const replacement = sockets[1];
  replacement.readyState = WebSocket.OPEN;
  replacement.onopen();

  assert.equal(socket, replacement);
  assert.equal(reconnectAttempt, 0);
  assert.equal(reconnectTimer, null);
  assert.deepEqual(statuses, [true, false, false, true]);
  const hello = JSON.parse(replacement.sent[0]);
  assert.equal(hello.type, "hello");
  assert.equal(hello.build, EXTENSION_BUILD);
  assert.match(BACKGROUND_SRC, /chrome\.alarms\.create\("lumen-keepalive", \{ periodInMinutes: 0\.5 \}\)/);
  // The socket heartbeat is what keeps the MV3 service worker (and so the hands) awake:
  // sub-30s traffic on every open socket, cleared with it.
  assert.deepEqual(intervals, [20000, 20000]);
  assert.match(BACKGROUND_SRC, /safeSend\(\{ type: "ping", at: Date\.now\(\) \}\)/);
});

test("chat sync returns the canonical pending run question with the transcript", async () => {
  const calls = [];
  const question = { id: 432, status: "pending", question_type: "field_approval" };
  const getConfig = async () => ({ backend: "http://127.0.0.1:8001" });
  const fetch = async (url, options) => {
    calls.push([url, options]);
    if (url.endsWith("/messages")) {
      return { ok: true, status: 200, json: async () => [{ id: 1, role: "assistant", content: "Ready" }] };
    }
    if (url.endsWith("/questions")) {
      return { ok: true, status: 200, json: async () => [{ id: 431, status: "answered" }, question] };
    }
    throw new Error(`unexpected URL: ${url}`);
  };
  const fetchHudChatMessages = eval(`(${extractFunctionSource("fetchHudChatMessages")})`);

  const result = await fetchHudChatMessages({ thread_id: 808, run_id: 707 });

  assert.equal(result.ok, true);
  assert.equal(result.pending_question_loaded, true);
  assert.deepEqual(result.pending_question, question);
  assert.deepEqual(result.messages, [{ id: 1, role: "assistant", content: "Ready" }]);
  assert.deepEqual(calls.map(([url]) => url), [
    "http://127.0.0.1:8001/operator/chat/threads/808/messages",
    "http://127.0.0.1:8001/operator/runs/707/questions",
  ]);
});

test("chat send forwards only validated structured answer fields", async () => {
  let request = null;
  const getConfig = async () => ({ backend: "http://127.0.0.1:8001" });
  const fetch = async (url, options) => {
    request = { url, options };
    return { ok: true, status: 200, text: async () => "" };
  };
  const postHudChatMessage = eval(`(${extractFunctionSource("postHudChatMessage")})`);

  const result = await postHudChatMessage({
    thread_id: 808,
    run_id: 707,
    content: "5+",
    source: "side_panel",
    selected_option: "5+",
    task_ids: [1300, "1300", 0, -1, "invalid"],
    save_scope: "permanent",
    ignored_field: "do-not-forward",
  });

  assert.equal(result.ok, true);
  assert.equal(request.url, "http://127.0.0.1:8001/operator/chat/threads/808/messages");
  const body = JSON.parse(request.options.body);
  assert.deepEqual(body, {
    content: "5+",
    payload: {
      source: "side_panel",
      run_id: 707,
      selected_option: "5+",
      task_ids: [1300],
      save_scope: "permanent",
    },
  });
});

test("chat send forwards a multi-select answer as a trimmed, capped string list", async () => {
  let request = null;
  const getConfig = async () => ({ backend: "http://127.0.0.1:8001" });
  const fetch = async (url, options) => {
    request = { url, options };
    return { ok: true, status: 200, text: async () => "" };
  };
  const postHudChatMessage = eval(`(${extractFunctionSource("postHudChatMessage")})`);
  const many = Array.from({ length: 25 }, (_, index) => `Option ${index + 1}`);

  const result = await postHudChatMessage({
    thread_id: 808,
    run_id: 707,
    content: "Compensation, Growth, Mission",
    source: "side_panel",
    selected_options: [" Compensation ", "Growth", "", "   ", 7, null, { x: 1 }, "Mission", "Growth"],
    task_ids: [1303],
  });

  assert.equal(result.ok, true);
  assert.deepEqual(JSON.parse(request.options.body), {
    content: "Compensation, Growth, Mission",
    payload: {
      source: "side_panel",
      run_id: 707,
      selected_options: ["Compensation", "Growth", "Mission"],
      task_ids: [1303],
    },
  });

  await postHudChatMessage({ thread_id: 808, run_id: 707, content: many.join(", "), source: "side_panel", selected_options: many });
  assert.deepEqual(JSON.parse(request.options.body).payload.selected_options, many.slice(0, 20));

  await postHudChatMessage({ thread_id: 808, run_id: 707, content: "x", source: "side_panel", selected_options: "Growth" });
  assert.equal("selected_options" in JSON.parse(request.options.body).payload, false);
  await postHudChatMessage({ thread_id: 808, run_id: 707, content: "x", source: "side_panel", selected_options: ["", "  "] });
  assert.equal("selected_options" in JSON.parse(request.options.body).payload, false);
});
