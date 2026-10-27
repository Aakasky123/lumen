(function () {
  const CHAT_SYNC_INTERVAL_MS = 1500;
  const STATE_SYNC_INTERVAL_MS = 1000;
  const MAX_ACTIVITY = 100;
  const STICK_THRESHOLD_PX = 64;

  const state = {
    connected: false,
    runId: null,
    threadId: null,
    activity: [],
    messages: [],
    stateLoaded: false,
    messagesLoaded: false,
    messageSignature: "",
    pendingQuestion: null,
    pendingQuestionSignature: "",
    editingQuestionId: null,
    questionDraft: "",
    // Multi-select (checkbox group) picks for the pending question, as exact option strings.
    multiPicksQuestionId: null,
    multiPicks: [],
    draft: "",
    sending: false,
    inputError: "",
  };

  const els = {};

  function byId(id) {
    return document.getElementById(id);
  }

  function clean(value, limit) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    if (!text) return "";
    return text.length > limit ? text.slice(0, Math.max(0, limit - 1)) + "..." : text;
  }

  function messageText(value, limit) {
    const text = String(value || "").trim();
    if (!text) return "";
    return text.length > limit ? text.slice(0, limit) : text;
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // Assistant text gets a safe sliver of markdown: the whole string is escaped first, then **bold** and
  // `code` spans are re-marked. Links stay plain text.
  function formatMessage(value) {
    return escapeHtml(value)
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n][^*]*?)\*\*/g, "<strong>$1</strong>");
  }

  const MARK_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.6" y="4.2" width="2.7" height="14.7" rx="0.75"/><rect class="shade" x="8.3" y="5.1" width="2.7" height="14.7" rx="0.75"/><rect class="shade" x="13" y="5.1" width="2.7" height="14.7" rx="0.75"/><rect x="17.7" y="4.2" width="2.7" height="14.7" rx="0.75"/></svg>';

  // Backend timestamps are naive UTC ("2026-07-11 05:50:14.123456" / ISO without offset). Parsing
  // them bare makes Date treat them as LOCAL wall-clock, so bubbles showed UTC times. Anything
  // without an explicit zone gets one; epoch numbers and Z/offset strings pass through untouched.
  function parseBackendTimestamp(value) {
    if (typeof value === "string") {
      const s = value.trim();
      if (s && !/(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(s)) return new Date(s.replace(" ", "T") + "Z");
    }
    return new Date(value);
  }

  function timeLabel(value) {
    try {
      if (!value) return "";
      return parseBackendTimestamp(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch (_) {
      return "";
    }
  }

  function visibleRole(role) {
    const normalized = String(role || "").toLowerCase();
    if (normalized === "user") return "user";
    if (normalized === "assistant") return "assistant";
    return "system";
  }

  function messageSignature(messages) {
    return messages.map((item) => [
      item.id,
      item.role,
      item.created_at,
      String(item.content || ""),
    ].join(":")).join("|");
  }

  function questionSignature(question) {
    if (!question || typeof question !== "object") return "";
    try {
      return JSON.stringify([
        question.id,
        question.status,
        question.updated_at,
        question.expected_answer_schema || {},
      ]);
    } catch (_) {
      return String(question.id || "");
    }
  }

  function pendingQuestionView(question) {
    if (!question || typeof question !== "object") return null;
    const schema = question.expected_answer_schema && typeof question.expected_answer_schema === "object"
      ? question.expected_answer_schema
      : {};
    const rawFields = Array.isArray(schema.fields) ? schema.fields : [];
    const field = rawFields.find((item) => item && typeof item === "object") || {};
    const rawOptions = Array.isArray(field.options)
      ? field.options
      : Array.isArray(schema.options)
        ? schema.options
        : [];
    const options = rawOptions.map((option) => String(option || "").trim()).filter(Boolean);
    const schemaTaskIds = Array.isArray(schema.task_ids) ? schema.task_ids : [];
    const taskIds = Array.from(new Set([
      field.id,
      ...schemaTaskIds,
    ].map((taskId) => Number(taskId)).filter((taskId) => Number.isFinite(taskId) && taskId > 0)));
    const proposed = String(field.proposed_answer || field.proposed_option || "").trim();
    const title = clean(field.display_title || field.observed_label || field.label || schema.display_title || question.prompt || "Lumen needs your input", 360);
    const reason = clean(field.display_reason || field.ask_reason || schema.display_reason || question.prompt || "", 360);
    const required = field.required !== false;
    return {
      id: Number(question.id) || null,
      type: String(question.question_type || ""),
      title,
      reason,
      proposed,
      options,
      multiple: field.multiple === true,
      taskIds,
      required,
    };
  }

  // The options a multi-select proposal ("A, B") names, in display order; [] unless every part is an option.
  function proposedMultiSelection(options, proposed) {
    const text = String(proposed || "").trim().toLowerCase();
    if (!text) return [];
    const whole = options.find((option) => option.toLowerCase() === text);
    if (whole) return [whole];
    const normalize = (value) => value.toLowerCase().replace(/\s*,\s*/g, ", ");
    let remaining = `, ${normalize(text)}, `;
    const matched = new Set();
    for (const option of [...options].sort((a, b) => b.length - a.length)) {
      const token = `, ${normalize(option)}, `;
      if (remaining.includes(token)) {
        remaining = remaining.replace(token, ", ");
        matched.add(option);
      }
    }
    if (remaining.replace(/[\s,]+/g, "")) return [];
    return options.filter((option) => matched.has(option));
  }

  function multiPicksFor(view) {
    if (!view || state.multiPicksQuestionId !== view.id) return [];
    return view.options.filter((option) => state.multiPicks.includes(option));
  }

  function activityTimeLabel(value) {
    try {
      return (value ? parseBackendTimestamp(value) : new Date()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch (_) {
      return "";
    }
  }

  function activitySignature(item) {
    return `${item.phase}|${item.title}`;
  }

  function severityClass(value) {
    const normalized = String(value || "").toLowerCase();
    if (normalized === "error" || normalized === "danger") return "error";
    if (normalized === "warn" || normalized === "warning") return "warn";
    if (normalized === "ok" || normalized === "success") return "ok";
    return "info";
  }

  function upsertActivity(data) {
    const title = clean(data.title || data.summary || data.phase || "", 360);
    const detail = clean(data.detail || data.reason || data.message || "", 360);
    if (!title && !detail && !data.phase) return false;
    const item = {
      id: `activity-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      role: "activity",
      phase: clean(data.phase || data.kind || "Activity", 80),
      title: title || detail || "Lumen is working.",
      detail,
      severity: clean(data.severity || "info", 24),
      created_at: new Date().toISOString(),
      messageAnchor: state.messagesLoaded ? state.messages.length : null,
    };
    const last = state.activity[state.activity.length - 1];
    if (last && activitySignature(last) === activitySignature(item)) {
      const changed = last.detail !== item.detail || last.severity !== item.severity;
      if (!changed) {
        if (last.messageAnchor == null && item.messageAnchor != null) last.messageAnchor = item.messageAnchor;
        return false;
      }
      last.detail = item.detail;
      last.severity = item.severity;
      last.created_at = item.created_at;
      if (last.messageAnchor == null && item.messageAnchor != null) last.messageAnchor = item.messageAnchor;
      return true;
    }
    state.activity.push(item);
    state.activity = state.activity.slice(-MAX_ACTIVITY);
    return true;
  }

  function updateFromPayload(payload) {
    const data = payload && typeof payload === "object" ? payload : {};
    const previousThreadId = state.threadId;
    if (data.run_id != null) state.runId = data.run_id;
    if (data.thread_id != null) state.threadId = data.thread_id;
    const changedActivity = upsertActivity(data);
    render();
    if (previousThreadId !== state.threadId) {
      state.messages = [];
      state.messagesLoaded = false;
      state.messageSignature = "";
      renderMessages(false);
    }
    if (changedActivity) renderMessages(true);
  }

  async function sendRuntimeMessage(message) {
    try {
      return await chrome.runtime.sendMessage(message);
    } catch (error) {
      return { ok: false, error: error && error.message ? error.message : String(error || "Extension message failed.") };
    }
  }

  async function refreshState() {
    const result = await sendRuntimeMessage({ type: "LUMEN_PANEL_STATE" });
    const firstLoad = !state.stateLoaded;
    const wasConnected = state.connected;
    state.stateLoaded = true;
    if (!result || result.ok === false) {
      state.connected = false;
      render();
      if (firstLoad || wasConnected) renderMessages(false);
      return;
    }
    state.connected = Boolean(result.connected);
    updateFromPayload(result.payload || {});
    // The empty state was drawn before the first state arrived, and it names the connection;
    // redraw it when either changes.
    if (firstLoad || wasConnected !== state.connected) renderMessages(false);
  }

  async function syncMessages() {
    if (!state.threadId) return;
    const result = await sendRuntimeMessage({
      type: "LUMEN_HUD_CHAT_SYNC",
      thread_id: state.threadId,
      run_id: state.runId,
    });
    if (!result || !result.ok || !Array.isArray(result.messages)) return;
    const signature = messageSignature(result.messages);
    const questionStateLoaded = result.pending_question_loaded !== false;
    const nextQuestion = questionStateLoaded
      ? result.pending_question && typeof result.pending_question === "object"
        ? result.pending_question
        : null
      : state.pendingQuestion;
    const nextQuestionSignature = questionSignature(nextQuestion);
    if (
      signature === state.messageSignature
      && nextQuestionSignature === state.pendingQuestionSignature
      && state.messagesLoaded
    ) return;
    const wasLoaded = state.messagesLoaded;
    state.messageSignature = signature;
    state.messages = result.messages;
    state.messagesLoaded = true;
    const previousQuestionId = Number(state.pendingQuestion && state.pendingQuestion.id) || null;
    const nextQuestionId = Number(nextQuestion && nextQuestion.id) || null;
    state.pendingQuestion = nextQuestion;
    state.pendingQuestionSignature = nextQuestionSignature;
    if (previousQuestionId !== nextQuestionId) {
      state.editingQuestionId = null;
      state.questionDraft = "";
      state.multiPicksQuestionId = null;
      state.multiPicks = [];
    }
    if (!wasLoaded) {
      for (const item of state.activity) {
        if (item.messageAnchor == null) item.messageAnchor = state.messages.length;
      }
    }
    renderMessages(true);
  }

  async function sendChatContent(content, extra, clearComposer) {
    content = String(content || "").trim();
    if (!content || state.sending || !state.threadId) return;
    state.sending = true;
    state.inputError = "";
    updateComposer();
    renderMessages(false);
    const result = await sendRuntimeMessage({
      type: "LUMEN_HUD_CHAT_SEND",
      thread_id: state.threadId,
      run_id: state.runId,
      content,
      source: "side_panel",
      ...(extra && typeof extra === "object" ? extra : {}),
    });
    state.sending = false;
    if (!result || !result.ok) {
      state.inputError = clean((result && result.error) || "Lumen could not send that message.", 220);
      updateComposer();
      renderMessages(false);
      return;
    }
    if (clearComposer) {
      state.draft = "";
      if (els.chatInput) els.chatInput.value = "";
      resizeInput();
    }
    updateComposer();
    renderMessages(false);
    await syncMessages();
  }

  async function sendMessage() {
    const content = String(state.draft || "").trim();
    return sendChatContent(content, {}, true);
  }

  function pendingQuestionHtml() {
    const view = pendingQuestionView(state.pendingQuestion);
    if (!view) return "";
    const disabled = state.sending ? " disabled" : "";
    const badge = view.required ? "required" : "optional";
    const reason = view.reason && view.reason !== view.title
      ? `<p class="pending-reason">${escapeHtml(view.reason)}</p>`
      : "";
    const proposal = view.proposed
      ? `<div class="pending-proposal"><span>Lumen suggests</span><p>${escapeHtml(view.proposed)}</p></div>`
      : "";
    let controls = "";
    if (view.options.length && view.multiple) {
      const matched = proposedMultiSelection(view.options, view.proposed);
      const keep = matched.length
        ? `<button type="button" class="pending-button primary" data-pending-action="keep"${disabled}>Keep ${escapeHtml(matched.join(", "))}</button>`
        : "";
      const picks = multiPicksFor(view);
      const options = view.options.map((option, index) => {
        const checked = picks.includes(option);
        return `<button type="button" class="pending-option multi${checked ? " checked" : ""}" role="checkbox" aria-checked="${checked ? "true" : "false"}" data-pending-toggle="${index}"${disabled}><span aria-hidden="true">${checked ? "&#10003;" : ""}</span>${escapeHtml(option)}</button>`;
      }).join("");
      const submitDisabled = state.sending || !picks.length ? " disabled" : "";
      const submit = `<button type="button" class="pending-button${keep ? "" : " primary"}" data-pending-action="submit-multi"${submitDisabled}>${picks.length ? `Continue with ${picks.length}` : "Continue"}</button>`;
      const skip = view.type === "field_approval"
        ? `<button type="button" class="pending-button muted" data-pending-action="skip"${disabled}>Skip</button>`
        : "";
      controls = `${keep ? `<div class="pending-actions">${keep}</div>` : ""}<p class="pending-hint">Pick one or more.</p><div class="pending-options" role="group" aria-label="${escapeHtml(view.title)}">${options}</div><div class="pending-actions">${submit}${skip}</div>`;
    } else if (view.options.length) {
      const matched = view.proposed
        ? view.options.find((option) => option.toLowerCase() === view.proposed.toLowerCase())
        : "";
      const keep = matched
        ? `<button type="button" class="pending-button primary" data-pending-action="keep"${disabled}>Keep ${escapeHtml(matched)}</button>`
        : "";
      const options = view.options.map((option, index) => (
        `<button type="button" class="pending-option" data-pending-option="${index}"${disabled}><span>${index + 1}</span>${escapeHtml(option)}</button>`
      )).join("");
      const skip = view.type === "field_approval"
        ? `<button type="button" class="pending-button muted" data-pending-action="skip"${disabled}>Skip</button>`
        : "";
      controls = `${keep ? `<div class="pending-actions">${keep}</div>` : ""}<div class="pending-options">${options}</div>${skip ? `<div class="pending-actions">${skip}</div>` : ""}`;
    } else if (view.proposed) {
      const editing = state.editingQuestionId === view.id;
      if (editing) {
        controls = `
          <textarea id="pendingChangeInput" class="pending-change-input" rows="4" aria-label="Change suggested answer"${disabled}>${escapeHtml(state.questionDraft)}</textarea>
          <div class="pending-actions">
            <button type="button" class="pending-button primary" data-pending-action="send-change"${disabled}>Use change</button>
            <button type="button" class="pending-button" data-pending-action="cancel-change"${disabled}>Cancel</button>
          </div>
        `;
      } else {
        controls = `
          <div class="pending-actions">
            <button type="button" class="pending-button primary" data-pending-action="keep"${disabled}>Keep</button>
            <button type="button" class="pending-button remember" data-pending-action="keep-remember"${disabled}>Keep &amp; remember</button>
            <button type="button" class="pending-button" data-pending-action="change"${disabled}>Change</button>
            <button type="button" class="pending-button muted" data-pending-action="skip"${disabled}>Skip</button>
          </div>
        `;
      }
    } else {
      controls = `<p class="pending-hint">Reply below to continue.</p>`;
    }
    return `
      <article class="pending-card" data-pending-question-id="${view.id || ""}">
        <span class="pending-kicker">Lumen needs your input</span>
        <div class="pending-body">
          <div class="pending-title-row">
            <p class="pending-title">${escapeHtml(view.title || "Pending question")}</p>
            <span class="pending-badge ${badge}">${badge}</span>
          </div>
          ${reason}
          ${proposal}
          ${controls}
        </div>
      </article>
    `;
  }

  function bindPendingQuestionActions() {
    if (!els.messages || !state.pendingQuestion) return;
    const card = els.messages.querySelector("[data-pending-question-id]");
    const view = pendingQuestionView(state.pendingQuestion);
    if (!card || !view) return;
    for (const button of card.querySelectorAll("[data-pending-action]")) {
      button.addEventListener("click", () => {
        const action = button.getAttribute("data-pending-action");
        if (action === "keep" && view.proposed) {
          void sendChatContent(view.proposed, { keep_proposed: true, task_ids: view.taskIds }, false);
        } else if (action === "keep-remember" && view.proposed) {
          void sendChatContent(view.proposed, { keep_proposed: true, task_ids: view.taskIds, save_scope: "permanent" }, false);
        } else if (action === "change") {
          state.editingQuestionId = view.id;
          state.questionDraft = view.proposed;
          renderMessages(false);
        } else if (action === "cancel-change") {
          state.editingQuestionId = null;
          state.questionDraft = "";
          renderMessages(false);
        } else if (action === "send-change") {
          const input = byId("pendingChangeInput");
          const changed = String((input && input.value) || state.questionDraft || "").trim();
          if (changed) void sendChatContent(changed, { task_ids: view.taskIds }, false);
        } else if (action === "skip") {
          void sendChatContent("Skip this field", { skip: true, task_ids: view.taskIds }, false);
        } else if (action === "submit-multi") {
          const picks = multiPicksFor(view);
          if (picks.length) void sendChatContent(picks.join(", "), { selected_options: picks, task_ids: view.taskIds }, false);
        }
      });
    }
    const changeInput = byId("pendingChangeInput");
    if (changeInput) {
      changeInput.addEventListener("input", () => {
        state.questionDraft = changeInput.value;
      });
    }
    for (const button of card.querySelectorAll("[data-pending-toggle]")) {
      button.addEventListener("click", () => {
        const index = Number(button.getAttribute("data-pending-toggle"));
        const option = view.options[index];
        if (!option || state.sending) return;
        const picks = multiPicksFor(view);
        state.multiPicksQuestionId = view.id;
        state.multiPicks = picks.includes(option) ? picks.filter((item) => item !== option) : [...picks, option];
        renderMessages(false);
        const again = els.messages && els.messages.querySelector(`[data-pending-toggle="${index}"]`);
        if (again) again.focus();
      });
    }
    for (const button of card.querySelectorAll("[data-pending-option]")) {
      button.addEventListener("click", () => {
        const index = Number(button.getAttribute("data-pending-option"));
        const option = view.options[index];
        if (option) void sendChatContent(option, { selected_option: option, task_ids: view.taskIds }, false);
      });
    }
  }

  function renderMessages(scrollToBottom) {
    if (!els.messages) return;
    const items = transcriptItems();
    const shouldStick = scrollToBottom && shouldStickToBottom();
    if (!items.length && !state.pendingQuestion) {
      els.messages.innerHTML = emptyStateHtml();
      if (shouldStick) els.messages.scrollTop = els.messages.scrollHeight;
      return;
    }
    const transcript = items.map((message) => {
      if (message.role === "activity") {
        const severity = severityClass(message.severity);
        const meta = `${escapeHtml(message.phase || "Activity")}${message.created_at ? ` · ${escapeHtml(activityTimeLabel(message.created_at))}` : ""}`;
        const detail = message.detail ? `<span class="activity-detail">${escapeHtml(message.detail)}</span>` : "";
        return `<article class="message activity ${severity}"><span class="severity ${severity}"></span><span class="activity-body"><span class="message-meta">${meta}</span><span class="activity-title">${escapeHtml(message.title || "Lumen is working.")}</span>${detail}</span></article>`;
      }
      const role = visibleRole(message && message.role);
      const content = messageText(message && message.content, 4000);
      const label = role === "user" ? "You" : role === "assistant" ? "Lumen" : "System";
      const meta = `${escapeHtml(label)}${message && message.created_at ? ` · ${escapeHtml(timeLabel(message.created_at))}` : ""}`;
      const body = role === "assistant" ? formatMessage(content) : escapeHtml(content);
      return `<article class="message ${role}"><span class="message-meta">${meta}</span><span class="message-content">${body}</span></article>`;
    }).join("");
    els.messages.innerHTML = transcript + pendingQuestionHtml();
    bindPendingQuestionActions();
    if (shouldStick) els.messages.scrollTop = els.messages.scrollHeight;
  }

  function emptyStateHtml() {
    let title = "Nothing running in Chrome";
    let detail = "Give Lumen a task from the Lumen app. Its work in Chrome shows up here.";
    let loading = false;
    if (state.threadId && !state.messagesLoaded) {
      title = "Loading the conversation";
      detail = "Syncing the latest from Lumen.";
      loading = true;
    } else if (state.threadId) {
      title = "No messages yet";
      detail = "What Lumen does in this tab will show up here.";
    } else if (!state.stateLoaded) {
      title = "Connecting";
      detail = "Checking what Lumen is doing in Chrome.";
      loading = true;
    } else if (!state.connected) {
      title = "Not connected to Lumen";
      detail = "Open the Lumen app, then press reconnect above.";
    }
    return `
      <div class="empty-state">
        <div class="empty-icon${loading ? " loading" : ""}" aria-hidden="true">${MARK_SVG}</div>
        <div class="empty-title">${escapeHtml(title)}</div>
        <p class="empty-detail">${escapeHtml(detail)}</p>
      </div>
    `;
  }

  function transcriptItems() {
    const groupedActivity = new Map();
    for (const item of state.activity) {
      const rawAnchor = item.messageAnchor == null ? state.messages.length : Number(item.messageAnchor);
      const anchor = Math.max(0, Math.min(state.messages.length, Number.isFinite(rawAnchor) ? rawAnchor : state.messages.length));
      const items = groupedActivity.get(anchor) || [];
      items.push(item);
      groupedActivity.set(anchor, items);
    }
    const out = [];
    out.push(...(groupedActivity.get(0) || []));
    state.messages.forEach((message, index) => {
      out.push(message);
      out.push(...(groupedActivity.get(index + 1) || []));
    });
    return out;
  }

  function shouldStickToBottom() {
    if (!els.messages) return true;
    return els.messages.scrollHeight - els.messages.scrollTop - els.messages.clientHeight <= STICK_THRESHOLD_PX;
  }

  function updateComposer() {
    const canSend = Boolean(state.threadId && String(state.draft || "").trim() && !state.sending);
    if (els.send) els.send.disabled = !canSend;
    if (els.threadLine) {
      els.threadLine.textContent = state.sending
        ? "Sending…"
        : state.threadId
          ? `Run ${state.runId || "active"}`
          : "No active task";
      els.threadLine.title = els.threadLine.textContent;
    }
    if (els.chatInput) {
      els.chatInput.placeholder = state.threadId ? "Message Lumen…" : "Start a task in the Lumen app to chat here";
    }
    if (els.inputError) {
      els.inputError.textContent = state.inputError || "";
      els.inputError.classList.toggle("visible", Boolean(state.inputError));
    }
  }

  function render() {
    const statusText = state.connected ? "Connected" : state.stateLoaded ? "Disconnected" : "Checking";
    if (els.statusLine) els.statusLine.textContent = statusText;
    if (els.connectionBadge) {
      els.connectionBadge.classList.toggle("connected", state.connected);
      els.connectionBadge.classList.toggle("disconnected", !state.connected && state.stateLoaded);
      els.connectionBadge.classList.toggle("checking", !state.stateLoaded);
      els.connectionBadge.title = statusText;
    }
    updateComposer();
  }

  function resizeInput() {
    if (!els.chatInput) return;
    els.chatInput.style.height = "auto";
    const computed = window.getComputedStyle ? window.getComputedStyle(els.chatInput) : null;
    const maxHeight = computed ? parseInt(computed.maxHeight, 10) : 138;
    const minHeight = computed ? parseInt(computed.minHeight, 10) : 44;
    const max = Number.isFinite(maxHeight) && maxHeight > 0 ? maxHeight : 138;
    const min = Number.isFinite(minHeight) && minHeight > 0 ? minHeight : 44;
    const nextHeight = Math.max(min, Math.min(els.chatInput.scrollHeight || min, max));
    els.chatInput.style.height = `${nextHeight}px`;
    els.chatInput.style.overflowY = (els.chatInput.scrollHeight || min) > max ? "auto" : "hidden";
  }

  function bindDom() {
    for (const id of ["statusLine", "connectionBadge", "reconnect", "messages", "chatInput", "inputError", "threadLine", "send"]) {
      els[id] = byId(id);
    }
    if (els.chatInput) {
      els.chatInput.addEventListener("input", () => {
        state.draft = els.chatInput.value;
        resizeInput();
        updateComposer();
      });
      els.chatInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.altKey && !event.isComposing) {
          event.preventDefault();
          sendMessage();
        }
      });
      resizeInput();
    }
    if (els.send) els.send.addEventListener("click", sendMessage);
    if (els.reconnect) {
      els.reconnect.addEventListener("click", async () => {
        state.connected = false;
        state.stateLoaded = false;
        render();
        if (els.statusLine) els.statusLine.textContent = "Reconnecting";
        if (els.connectionBadge) els.connectionBadge.title = "Reconnecting";
        await sendRuntimeMessage({ type: "LUMEN_RECONNECT" });
        setTimeout(refreshState, 400);
      });
    }
    if (chrome.runtime && chrome.runtime.onMessage) {
      chrome.runtime.onMessage.addListener((message) => {
        if (message && message.type === "LUMEN_PANEL_UPDATE") {
          updateFromPayload(message.payload || {});
          setTimeout(syncMessages, 50);
        }
      });
    }
  }

  function start() {
    bindDom();
    render();
    renderMessages(false);
    refreshState().then(syncMessages);
    setInterval(refreshState, STATE_SYNC_INTERVAL_MS);
    setInterval(syncMessages, CHAT_SYNC_INTERVAL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
