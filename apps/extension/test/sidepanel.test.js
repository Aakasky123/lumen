const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const { JSDOM } = require("jsdom");

function wait(ms = 0) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function loadPanel(messages, page = "sidepanel.html", pendingQuestion = null) {
  const html = fs.readFileSync(path.join(__dirname, `../src/${page}`), "utf8").replace(/<script src="sidepanel\.js"><\/script>/, "");
  const script = fs.readFileSync(path.join(__dirname, "../src/sidepanel.js"), "utf8");
  const dom = new JSDOM(html, {
    url: `chrome-extension://lumen/src/${page}`,
    runScripts: "dangerously",
    pretendToBeVisual: true,
  });
  const intervals = [];
  const realSetInterval = dom.window.setInterval.bind(dom.window);
  const realClearInterval = dom.window.clearInterval.bind(dom.window);
  dom.window.setInterval = (fn, delay, ...args) => {
    const id = realSetInterval(fn, delay, ...args);
    intervals.push(id);
    return id;
  };
  dom.window.clearInterval = (id) => {
    const index = intervals.indexOf(id);
    if (index >= 0) intervals.splice(index, 1);
    return realClearInterval(id);
  };
  let runtimeListener = null;
  const sent = [];
  const payload = {
    run_id: 707,
    thread_id: 808,
    phase: "Thinking",
    title: "Choosing the next visual browser action.",
    detail: "5s elapsed",
    severity: "info",
    context_usage: { used_tokens: 2400, model: "xiaomi/mimo-v2.5" },
  };
  dom.window.chrome = {
    runtime: {
      onMessage: { addListener(fn) { runtimeListener = fn; } },
      sendMessage: async (message) => {
        sent.push(message);
        if (message.type === "LUMEN_PANEL_STATE") return { ok: true, connected: true, backend: "http://127.0.0.1:8001", payload };
        if (message.type === "LUMEN_HUD_CHAT_SYNC") {
          return { ok: true, messages, pending_question: pendingQuestion, pending_question_loaded: true };
        }
        if (message.type === "LUMEN_HUD_CHAT_SEND") {
          messages.push({
            id: messages.length + 1,
            role: "user",
            content: message.content,
            created_at: new Date().toISOString(),
          });
          return { ok: true };
        }
        if (message.type === "LUMEN_RECONNECT") return { ok: true };
        return { ok: false, error: "unexpected message" };
      },
    },
  };
  dom.window.eval(script);
  dom.window.document.dispatchEvent(new dom.window.Event("DOMContentLoaded"));
  const cleanup = () => {
    for (const id of intervals.splice(0)) realClearInterval(id);
    dom.window.close();
  };
  return { dom, sent, pushRuntimeMessage: (message) => runtimeListener && runtimeListener(message), cleanup };
}

test("side panel is a simple chat surface and sends through the canonical bridge", async (t) => {
  const messages = [
    { id: 1, role: "user", content: "apply https://ats.rippling.com/job", created_at: "2026-07-02T22:44:00.000" },
    { id: 2, role: "assistant", content: "Got it. I started the run.", created_at: "2026-07-02T22:44:05.000" },
  ];
  const { dom, sent, cleanup } = loadPanel(messages);
  t.after(cleanup);

  await wait(20);

  assert.equal(dom.window.document.getElementById("statusLine").textContent, "Connected");
  assert.equal(dom.window.document.getElementById("activitySection"), null);
  assert.equal(dom.window.document.getElementById("backend"), null);
  assert.equal(dom.window.document.getElementById("runStatus"), null);
  assert.match(dom.window.document.getElementById("messages").textContent, /Thinking/);
  assert.match(dom.window.document.getElementById("messages").textContent, /Choosing the next visual browser action/);
  assert.match(dom.window.document.getElementById("messages").textContent, /5s elapsed/);
  assert.match(dom.window.document.getElementById("messages").textContent, /Got it\. I started the run/);
  assert.equal(dom.window.document.getElementById("threadLine").textContent, "Run 707");
  const transcript = dom.window.document.getElementById("messages").textContent;
  assert(transcript.indexOf("apply https://ats.rippling.com/job") < transcript.indexOf("Got it. I started the run."));
  assert(transcript.indexOf("Got it. I started the run.") < transcript.indexOf("Thinking"));

  const input = dom.window.document.getElementById("chatInput");
  input.value = "Use the saved answer.";
  input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  dom.window.document.getElementById("send").click();
  await wait(20);

  assert(sent.some((message) =>
    message.type === "LUMEN_HUD_CHAT_SEND"
    && message.thread_id === 808
    && message.run_id === 707
    && message.source === "side_panel"
    && message.content === "Use the saved answer."
  ));
});

test("side panel renders a grounded proposal with explicit approval controls", async (t) => {
  const proposal = "I built a document intelligence platform with semantic search and contextual chat.";
  const pendingQuestion = {
    id: 432,
    status: "pending",
    question_type: "field_approval",
    prompt: "How have you applied AI in your engineering work and what impact did it have?",
    expected_answer_schema: {
      task_ids: [1300],
      fields: [{
        id: 1300,
        label: "How have you applied AI in your engineering work and what impact did it have?",
        display_reason: "Lumen drafted a resume-grounded answer for your approval.",
        required: true,
        options: [],
        proposed_answer: proposal,
      }],
    },
  };
  const { dom, sent, cleanup } = loadPanel([], "sidepanel.html", pendingQuestion);
  t.after(cleanup);

  await wait(20);

  const transcript = dom.window.document.getElementById("messages").textContent;
  assert.match(transcript, /I built a document intelligence platform/);
  assert.match(transcript, /Keep/);
  assert.match(transcript, /Keep & remember/);
  assert.match(transcript, /Change/);
  assert.match(transcript, /Skip/);

  dom.window.document.querySelector('[data-pending-action="keep-remember"]').click();
  await wait(20);

  assert(sent.some((message) =>
    message.type === "LUMEN_HUD_CHAT_SEND"
    && message.thread_id === 808
    && message.run_id === 707
    && message.content === proposal
    && message.keep_proposed === true
    && message.save_scope === "permanent"
    && JSON.stringify(message.task_ids) === JSON.stringify([1300])
  ));
});

test("side panel lets the user edit a proposal before sending it", async (t) => {
  const pendingQuestion = {
    id: 433,
    status: "pending",
    question_type: "field_approval",
    prompt: "Describe a developer tool you built.",
    expected_answer_schema: {
      task_ids: [1301],
      fields: [{ id: 1301, label: "Describe a developer tool you built.", proposed_answer: "Initial draft", options: [] }],
    },
  };
  const { dom, sent, cleanup } = loadPanel([], "sidepanel.html", pendingQuestion);
  t.after(cleanup);

  await wait(20);
  dom.window.document.querySelector('[data-pending-action="change"]').click();
  const input = dom.window.document.getElementById("pendingChangeInput");
  assert.equal(input.value, "Initial draft");
  input.value = "My approved revised answer";
  input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  dom.window.document.querySelector('[data-pending-action="send-change"]').click();
  await wait(20);

  assert(sent.some((message) =>
    message.type === "LUMEN_HUD_CHAT_SEND"
    && message.content === "My approved revised answer"
    && JSON.stringify(message.task_ids) === JSON.stringify([1301])
    && message.keep_proposed === undefined
  ));
});

test("side panel renders backend choices and sends the selected option structurally", async (t) => {
  const pendingQuestion = {
    id: 434,
    status: "pending",
    question_type: "field_approval",
    prompt: "How many years of experience do you have?",
    expected_answer_schema: {
      task_ids: [1302],
      fields: [{
        id: 1302,
        label: "How many years of experience do you have?",
        required: true,
        proposed_option: "5+",
        options: ["3", "4", "5+"],
      }],
    },
  };
  const { dom, sent, cleanup } = loadPanel([], "sidepanel.html", pendingQuestion);
  t.after(cleanup);

  await wait(20);
  const optionButtons = [...dom.window.document.querySelectorAll("[data-pending-option]")];
  assert.deepEqual(optionButtons.map((button) => button.textContent.trim()), ["13", "24", "35+"]);
  optionButtons[2].click();
  await wait(20);

  assert(sent.some((message) =>
    message.type === "LUMEN_HUD_CHAT_SEND"
    && message.content === "5+"
    && message.selected_option === "5+"
    && JSON.stringify(message.task_ids) === JSON.stringify([1302])
  ));
});

test("side panel renders a multi-select field as checkboxes and sends every pick", async (t) => {
  const pendingQuestion = {
    id: 435,
    status: "pending",
    question_type: "field_approval",
    prompt: "Which factors are most important to you? (Select 3)",
    expected_answer_schema: {
      task_ids: [1303],
      fields: [{
        id: 1303,
        label: "Which factors are most important to you? (Select 3)",
        required: true,
        multiple: true,
        options: ["Compensation", "Remote work", "Growth", "Team", "Mission"],
      }],
    },
  };
  const { dom, sent, cleanup } = loadPanel([], "sidepanel.html", pendingQuestion);
  t.after(cleanup);

  await wait(20);
  const doc = dom.window.document;
  const toggles = () => [...doc.querySelectorAll("[data-pending-toggle]")];
  assert.deepEqual(toggles().map((button) => button.getAttribute("role")), Array(5).fill("checkbox"));
  assert.deepEqual(toggles().map((button) => button.getAttribute("aria-checked")), Array(5).fill("false"));
  assert.equal(doc.querySelectorAll("[data-pending-option]").length, 0);
  const submit = () => doc.querySelector('[data-pending-action="submit-multi"]');
  assert.equal(submit().disabled, true);
  assert(doc.querySelector('[data-pending-action="skip"]'));

  // Picking toggles without sending; order of clicks does not matter, display order does.
  toggles()[4].click();
  toggles()[0].click();
  toggles()[2].click();
  toggles()[1].click();
  toggles()[1].click();
  assert.deepEqual(toggles().map((button) => button.getAttribute("aria-checked")), ["true", "false", "true", "false", "true"]);
  assert.equal(sent.filter((message) => message.type === "LUMEN_HUD_CHAT_SEND").length, 0);
  assert.equal(submit().disabled, false);
  assert.equal(submit().textContent.trim(), "Continue with 3");

  submit().click();
  await wait(20);

  const sends = sent.filter((message) => message.type === "LUMEN_HUD_CHAT_SEND");
  assert.equal(sends.length, 1);
  assert.equal(sends[0].content, "Compensation, Growth, Mission");
  assert.equal(JSON.stringify(sends[0].selected_options), JSON.stringify(["Compensation", "Growth", "Mission"]));
  assert.equal(sends[0].selected_option, undefined);
  assert.equal(JSON.stringify(sends[0].task_ids), JSON.stringify([1303]));
});

test("side panel keeps a multi-select proposal that names only real options", async (t) => {
  const pendingQuestion = {
    id: 436,
    status: "pending",
    question_type: "field_approval",
    prompt: "Which factors are most important to you?",
    expected_answer_schema: {
      fields: [{
        id: 1304,
        label: "Which factors are most important to you?",
        multiple: true,
        proposed_answer: "Growth, Team",
        options: ["Compensation", "Growth", "Team"],
      }],
    },
  };
  const { dom, sent, cleanup } = loadPanel([], "sidepanel.html", pendingQuestion);
  t.after(cleanup);

  await wait(20);
  const keep = dom.window.document.querySelector('[data-pending-action="keep"]');
  assert.equal(keep.textContent.trim(), "Keep Growth, Team");
  keep.click();
  await wait(20);

  assert(sent.some((message) =>
    message.type === "LUMEN_HUD_CHAT_SEND"
    && message.content === "Growth, Team"
    && message.keep_proposed === true
    && JSON.stringify(message.task_ids) === JSON.stringify([1304])
  ));
});

test("side panel replaces its connecting placeholder once the first state arrives", async (t) => {
  const html = fs.readFileSync(path.join(__dirname, "../src/sidepanel.html"), "utf8").replace(/<script src="sidepanel\.js"><\/script>/, "");
  const script = fs.readFileSync(path.join(__dirname, "../src/sidepanel.js"), "utf8");
  const dom = new JSDOM(html, { url: "chrome-extension://lumen/src/sidepanel.html", runScripts: "dangerously", pretendToBeVisual: true });
  const intervals = [];
  const realSetInterval = dom.window.setInterval.bind(dom.window);
  dom.window.setInterval = (fn, delay, ...args) => {
    const id = realSetInterval(fn, delay, ...args);
    intervals.push(id);
    return id;
  };
  dom.window.chrome = {
    runtime: {
      onMessage: { addListener() {} },
      sendMessage: async (message) => (message.type === "LUMEN_PANEL_STATE" ? { ok: true, connected: true, payload: {} } : { ok: false })
    }
  };
  t.after(() => {
    for (const id of intervals) dom.window.clearInterval(id);
    dom.window.close();
  });
  dom.window.eval(script);
  dom.window.document.dispatchEvent(new dom.window.Event("DOMContentLoaded"));
  await wait(20);

  const transcript = dom.window.document.getElementById("messages").textContent;
  assert.doesNotMatch(transcript, /Connecting/);
  assert.match(transcript, /Nothing running in Chrome/);
  assert.equal(dom.window.document.getElementById("statusLine").textContent, "Connected");
});

test("side panel says how to reconnect when Lumen is unreachable", async (t) => {
  const html = fs.readFileSync(path.join(__dirname, "../src/sidepanel.html"), "utf8").replace(/<script src="sidepanel\.js"><\/script>/, "");
  const script = fs.readFileSync(path.join(__dirname, "../src/sidepanel.js"), "utf8");
  const dom = new JSDOM(html, { url: "chrome-extension://lumen/src/sidepanel.html", runScripts: "dangerously", pretendToBeVisual: true });
  const intervals = [];
  const realSetInterval = dom.window.setInterval.bind(dom.window);
  dom.window.setInterval = (fn, delay, ...args) => {
    const id = realSetInterval(fn, delay, ...args);
    intervals.push(id);
    return id;
  };
  dom.window.chrome = {
    runtime: {
      onMessage: { addListener() {} },
      sendMessage: async (message) => (message.type === "LUMEN_PANEL_STATE" ? { ok: true, connected: false, payload: {} } : { ok: false })
    }
  };
  t.after(() => {
    for (const id of intervals) dom.window.clearInterval(id);
    dom.window.close();
  });
  dom.window.eval(script);
  dom.window.document.dispatchEvent(new dom.window.Event("DOMContentLoaded"));
  await wait(20);

  assert.equal(dom.window.document.getElementById("statusLine").textContent, "Disconnected");
  assert.match(dom.window.document.getElementById("messages").textContent, /Not connected to Lumen/);
});
