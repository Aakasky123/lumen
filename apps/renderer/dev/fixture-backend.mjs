// Fixture backend for cockpit UI work.
//
// Serves the slice of the Lumen API the renderer uses, from in-memory scenarios, so every cockpit state
// (a working run, each question kind, final-action approval, sensitive entry, system blocks, outcomes,
// first run) can be designed and checked without a model, a browser body, or the real database. A sent
// task plays a scripted run: narration, a question, an approval, and an outcome.
//
//   node apps/renderer/dev/fixture-backend.mjs            (port 8790, scenario "home")
//   FIXTURE_PORT=8791 FIXTURE_SCENARIO=working node ...
//
// Point the renderer at it with API_BACKEND_URL=http://127.0.0.1:8790 and NEXT_PUBLIC_LUMEN_FIXTURES=1
// (the second also stands in for the Electron desktop bridge). Switch scenarios from the browser:
// GET /api/backend/__fixture/scenario/<name>. GET /api/backend/__fixture lists them.
//
// This is a test double, not a spec: the real contract lives in apps/api.

import { createServer } from "node:http";

const PORT = Number(process.env.FIXTURE_PORT || 8790);

// ---------------------------------------------------------------------------------------------------
// Time helpers. The backend serializes naive UTC timestamps ("2026-10-02T14:03:11.123456").

function iso(offsetSeconds = 0) {
  return new Date(Date.now() + offsetSeconds * 1000).toISOString().replace("Z", "");
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---------------------------------------------------------------------------------------------------
// State

let state;
let seq = 1000;
const nextId = () => ++seq;
const timers = new Set();

function later(ms, fn) {
  const timer = setTimeout(() => {
    timers.delete(timer);
    fn();
  }, ms);
  timers.add(timer);
}

function clearTimers() {
  for (const timer of timers) clearTimeout(timer);
  timers.clear();
}

function baseState() {
  return {
    user: { id: 1, email: "maya.chen@example.com", name: "Maya Chen" },
    profile: {
      phone: "+1 415 555 0142",
      location: "Oakland, CA",
      linkedin: "https://www.linkedin.com/in/maya-chen-example",
      github: "https://github.com/maya-example",
      portfolio: "",
      work_authorization: "US citizen",
      sponsorship_status: "No",
      salary_expectation: "",
      relocation_preference: "Open to the Bay Area",
      profile_json: {}
    },
    answers: [
      { id: 71, question_pattern: "start_date: When can you start?", answer: "Two weeks after an offer", confidence: 0.92, last_used_at: iso(-86400 * 3) },
      { id: 72, question_pattern: "heard_about: How did you hear about us?", answer: "Company careers page", confidence: 0.88, last_used_at: iso(-86400 * 9) },
      { id: 73, question_pattern: "pronouns: Pronouns", answer: "she/her", confidence: 0.97, last_used_at: null }
    ],
    attestations: [],
    memoryFacts: [
      { key: "name", label: "Name", value: "Alex Rivera", section: "about", learned_on: "2026-09-12", source: "from Resume.pdf" },
      { key: "preferred_name", label: "Preferred name", value: "Alex", section: "about", learned_on: new Date().toISOString().slice(0, 10), source: "you, in chat" },
      { key: "email", label: "Email", value: "alex@example.com", section: "contact", learned_on: "2026-09-12", source: "from Resume.pdf" },
      { key: "phone", label: "Phone", value: "+1 (555) 010-2030", section: "contact", learned_on: "2026-09-12", source: "from Resume.pdf" },
      { key: "location", label: "Location", value: "Hoboken, NJ", section: "contact", learned_on: new Date().toISOString().slice(0, 10), source: "you, in chat" },
      { key: "current_role", label: "Current role", value: "Senior Backend Engineer at Ledgerly", section: "work", learned_on: "2026-09-12", source: "from Resume.pdf" },
      { key: "professional_python_experience", label: "Professional python experience", value: "7+ years", section: "work", learned_on: "2026-09-28", source: "you, answering a question" },
      { key: "work_authorization", label: "Work authorization", value: "US citizen; no sponsorship needed", section: "work", learned_on: "2026-09-20", source: "added by you" },
      { key: "education", label: "Education", value: "MS Computer Science, Northfield University (2024)", section: "education", learned_on: "2026-09-12", source: "from Resume.pdf" },
      { key: "salary_expectation", label: "Salary expectation", value: "$150,000 base", section: "preferences", learned_on: "2026-09-28", source: "you, answering a question" },
      { key: "remote_preference", label: "Remote preference", value: "Hybrid in the NYC area, 2 days on site at most", section: "preferences", learned_on: new Date().toISOString().slice(0, 10), source: "you, in chat" },
      { key: "veteran_status", label: "Veteran status", value: "I am not a protected veteran", section: "sensitive", learned_on: "2026-09-28", source: "you, answering a question" }
    ],
    devices: [
      { id: 1, name: "Pixel 10 Pro", platform: "android", has_push: true, last_seen_at: new Date(Date.now() - 4 * 60_000).toISOString(), created_at: new Date(Date.now() - 3 * 86_400_000).toISOString() }
    ],
    providers: [
      provider(12, "GLM 5.3 Flash", "openrouter", "z-ai/glm-5.3-flash", "healthy"),
      provider(8, "OpenAI · Luna", "openai", "gpt-6-luna", "healthy"),
      provider(11, "Qwen 3.8 Flash", "openrouter", "qwen/qwen3.8-flash", "healthy")
    ],
    // The three seats: the Brain (12, also the active row), the Checker (11) and Escalation (8).
    activeProviderId: 12,
    settings: {
      vision_provider_id: 12,
      vision_escalation_provider_id: 8,
      judge_provider_id: 11,
      strict_model_provider_allowlist: false,
      final_action_mode: "approval",
      autonomous_attestation_consent: false,
      autonomous_relay_consent: false
    },
    artifacts: [
      artifact(301, "Maya Chen — Résumé 2026", "resume", "maya-chen-resume-2026.pdf", 182_400, "Senior backend engineer with 7 years building payments and data platforms. Led the ledger rewrite at Affirm…"),
      artifact(302, "Passport (scan)", "passport", "passport-scan.jpg", 1_204_000, "")
    ],
    vault: [
      vaultItem(41, "Personal", "https://www.opentable.com", ["email", "password"], true, iso(-86400 * 2)),
      vaultItem(42, "Work account", "https://app.greenhouse.io", ["email", "password"], false, null)
    ],
    threads: [],
    messages: [],
    runs: [],
    questions: [],
    desktop: {
      backendHealthy: true,
      modelConfigured: true,
      modelReady: true,
      modelDisplayName: "GLM 5.3 Flash",
      modelError: "",
      extensionConnected: true,
      liveActionsReady: true,
      computerUseAvailable: true,
      computerUseEnabled: true
    }
  };
}

function provider(id, display, type, model, health) {
  return {
    id,
    display_name: display,
    provider_type: type,
    model_name: model,
    base_url: "",
    api_key_masked: "sk-…" + String(id).padStart(4, "0"),
    has_api_key: true,
    temperature: 0.2,
    max_tokens: 2048,
    timeout_seconds: 30,
    enabled: true,
    is_active: false,
    source: "database",
    health_status: health,
    last_error: null,
    last_tested_at: iso(-3600),
    created_at: iso(-86400 * 20),
    updated_at: iso(-3600)
  };
}

function artifact(id, display, kind, filename, size, preview) {
  return {
    id,
    user_id: 1,
    run_id: null,
    application_id: null,
    job_posting_id: null,
    artifact_kind: kind,
    source: "uploaded",
    filename,
    mime_type: filename.endsWith(".pdf") ? "application/pdf" : "image/jpeg",
    storage_path: `storage/artifacts/${filename}`,
    text_content: preview,
    text_preview: preview,
    status: "active",
    metadata_json: { display_name: display, artifact_role: kind },
    file_size: size,
    created_at: iso(-86400 * 14),
    updated_at: iso(-86400 * 14),
    deleted_at: null
  };
}

function vaultItem(id, label, origin, slots, autoUse, lastUsed) {
  return {
    id,
    account_label: label,
    origin,
    available_slots: slots,
    auto_use: autoUse,
    created_at: iso(-86400 * 30),
    updated_at: iso(-86400 * 30),
    last_used_at: lastUsed
  };
}

function addThread({ title, runId = null, ageSeconds = 0 }) {
  const thread = {
    id: nextId(),
    user_id: 1,
    run_id: runId,
    title,
    mode: runId ? "run" : "global",
    status: "active",
    created_at: iso(-ageSeconds - 60),
    updated_at: iso(-ageSeconds)
  };
  state.threads.push(thread);
  return thread;
}

/** A thread as the API returns it: the model spend of its run rides along (null without a run). */
function threadOut(thread) {
  const run = thread.run_id ? state.runs.find((item) => item.id === thread.run_id) : null;
  return { ...thread, cost_usd: thread.run_id ? run?.cost_estimate ?? 0 : null };
}

function addMessage(thread, role, content, payload = {}, ageSeconds = 0) {
  const message = {
    id: nextId(),
    thread_id: thread.id,
    role,
    content,
    payload_json: payload,
    linked_event_id: null,
    linked_task_id: null,
    created_at: iso(-ageSeconds)
  };
  state.messages.push(message);
  thread.updated_at = message.created_at;
  return message;
}

function addRun({ goal, url, runState, status, confidence = {}, ageSeconds = 0, cost = null }) {
  const id = nextId();
  const run = {
    id,
    application_id: null,
    user_id: 1,
    goal,
    target_url: url,
    current_state: runState,
    active_plan: [],
    confidence_state: confidence,
    risk_level: "low",
    // Model spend in USD; a spread of plausible figures unless a scenario sets one.
    cost_estimate: cost ?? Number((0.03 + ((id * 37) % 41) / 100).toFixed(4)),
    status_message: status,
    created_at: iso(-ageSeconds - 120),
    updated_at: iso(-ageSeconds)
  };
  state.runs.push(run);
  return run;
}

function addQuestion(run, thread, { type = "field_approval", prompt, schema, phase = "FILLING", reason = "" }) {
  const question = {
    id: nextId(),
    run_id: run.id,
    thread_id: thread.id,
    question_type: type,
    stuck_phase: phase,
    stuck_reason: reason,
    prompt,
    expected_answer_schema: schema,
    status: "pending",
    answer_payload: {},
    applied_at: null,
    resumed_at: null,
    created_at: iso(),
    updated_at: iso()
  };
  state.questions.push(question);
  return question;
}

function outcome(thread, run, content, summaryState, extra = {}) {
  const verified = extra.verified ?? 6;
  return addMessage(thread, "assistant", content, {
    source: "in_browser_outcome",
    summary: {
      state: summaryState,
      reason: extra.reason || (summaryState === "COMPLETED" ? "done" : "ready_for_review"),
      actions: extra.actions ?? verified + 2,
      receipts: Array.from({ length: verified }, (_, index) => ({
        operation_id: `op-${index}`,
        type: "browser_action",
        outcome: "verified_succeeded"
      })).concat(
        Array.from({ length: extra.unconfirmed ?? 0 }, (_, index) => ({
          operation_id: `op-u-${index}`,
          type: "field_filled",
          outcome: "verification_inconclusive"
        })),
        Array.from({ length: extra.failed ?? 0 }, (_, index) => ({
          operation_id: `op-f-${index}`,
          type: "browser_action",
          outcome: "execution_failed"
        }))
      ),
      field_census: (extra.requiredOpen || []).map((label) => ({ label, status: "required_unfilled" })),
      ...(extra.completion ? { completion_check: { verdict: extra.completion, verified_summary: "", missing: "" } } : {})
    }
  }, extra.ageSeconds ?? 0);
}

// ---------------------------------------------------------------------------------------------------
// Scenario building blocks

function seedHistory() {
  const apt = addRun({ goal: "Find 2-bedroom apartments in Fremont under $3,200 and list the five best", url: "https://www.zillow.com/fremont-ca/rentals/", runState: "COMPLETED", status: "Done.", ageSeconds: 86400 * 2 });
  const aptThread = addThread({ title: "Browser task: zillow.com", runId: apt.id, ageSeconds: 86400 * 2 });
  addMessage(aptThread, "user", "Find 2-bedroom apartments in Fremont under $3,200 and list the five best", {}, 86400 * 2 + 300);
  addMessage(aptThread, "assistant", "On it. I'll work in a new Chrome tab on zillow.com and tell you what I find.", { source: "action" }, 86400 * 2 + 290);
  outcome(
    aptThread,
    apt,
    "Here are the five best matches I found on Zillow, sorted by price:\n\n1. **Mission Blvd, 2 bd / 2 ba** — $2,950/mo, in-unit laundry, 0.4 mi to BART\n2. **Walnut Ave, 2 bd / 1 ba** — $3,050/mo, updated kitchen, covered parking\n3. **Paseo Padre Pkwy, 2 bd / 2 ba** — $3,100/mo, pool, pets allowed\n4. **Fremont Blvd, 2 bd / 2 ba** — $3,150/mo, new construction\n5. **Stevenson Blvd, 2 bd / 1.5 ba** — $3,195/mo, townhouse with patio\n\nNo consequential final action was executed.",
    "COMPLETED",
    { verified: 14, ageSeconds: 86400 * 2 }
  );

  const files = addRun({ goal: "Rename the screenshots in Pictures\\Screenshots by date taken", url: "desktop://windows", runState: "COMPLETED", status: "Done.", ageSeconds: 86400 * 5, cost: 0.004 });
  const filesThread = addThread({ title: "Computer task: Windows", runId: files.id, ageSeconds: 86400 * 5 });
  addMessage(filesThread, "user", "Rename the screenshots in Pictures\\Screenshots by date taken", {}, 86400 * 5 + 400);
  addMessage(filesThread, "assistant", "Starting on your desktop. I'll keep to File Explorer.", { source: "action" }, 86400 * 5 + 390);
  outcome(filesThread, files, "I renamed 23 screenshots to the `YYYY-MM-DD HH.MM` pattern in File Explorer and checked each new name in the folder view.", "COMPLETED", { verified: 23, ageSeconds: 86400 * 5 });

  const chat = addThread({ title: "What can you do?", ageSeconds: 86400 * 9 });
  addMessage(chat, "user", "What can you do?", {}, 86400 * 9 + 60);
  addMessage(
    chat,
    "assistant",
    "I can do real work on your computer, one verified step at a time:\n\n- **In Chrome** — research, compare, fill forms, apply to jobs, book things. I work in my own tab and never touch yours.\n- **On Windows** — use desktop apps through the screen, like a person would.\n- **With your context** — your profile, documents, and saved answers, only when a task needs them.\n\nI ask before anything consequential (submit, send, book, pay) unless you switch to full autonomy, and I never type a fact I can't ground in what you gave me.",
    {},
    86400 * 9
  );
}

function liveRun({ goal, url, title, narration, ageSeconds = 140 }) {
  const run = addRun({ goal, url, runState: "RUNNING", status: narration[0], ageSeconds: 0, confidence: { context_usage: { used_tokens: 18_400, limit_tokens: 200_000, model: "z-ai/glm-5.3-flash", provider_display_name: "GLM 5.3 Flash", context_window_source: "provider" } } });
  run.created_at = iso(-ageSeconds);
  const thread = addThread({ title, runId: run.id });
  addMessage(thread, "user", goal, {}, ageSeconds);
  addMessage(thread, "assistant", `On it. I'll work in a new Chrome tab on ${hostOf(url)} and check each step before moving on.`, { source: "action" }, ageSeconds - 4);
  return { run, thread, narration };
}

function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function narrate(run, lines, every = 2600) {
  let index = 0;
  const tick = () => {
    if (!state.runs.includes(run) || run.current_state !== "RUNNING" || pausedRun(run)) return;
    index = (index + 1) % lines.length;
    run.status_message = lines[index];
    run.updated_at = iso();
    const usage = run.confidence_state.context_usage;
    if (usage) usage.used_tokens = Math.min(usage.limit_tokens - 1000, usage.used_tokens + 2100);
    run.cost_estimate = Number((run.cost_estimate + 0.0031).toFixed(4));
    later(every, tick);
  };
  later(every, tick);
}

function pausedRun(run) {
  return Boolean(run.confidence_state?.user_pause?.active);
}

const APPLY_NARRATION = [
  "Reading the application form",
  "Filling First name from your profile",
  "Verified Email",
  "Uploading your résumé to the Resume/CV field",
  "Checking the upload took",
  "Scrolling to the work authorization questions",
  "Choosing the next step"
];

// ---------------------------------------------------------------------------------------------------
// Scenarios

const SCENARIOS = {
  home: {
    note: "History, configured models, nothing running.",
    build() {
      seedHistory();
    }
  },
  "first-run": {
    note: "No model provider yet: onboarding.",
    build() {
      state.providers = [];
      state.activeProviderId = null;
      state.settings.vision_provider_id = null;
      state.settings.judge_provider_id = null;
      state.settings.vision_escalation_provider_id = null;
      state.desktop.modelConfigured = false;
      state.desktop.modelReady = false;
      state.desktop.modelDisplayName = "";
      state.artifacts = [];
      state.vault = [];
      state.answers = [];
    }
  },
  working: {
    note: "A browser run is working and narrating.",
    build() {
      seedHistory();
      const live = liveRun({
        goal: "Apply to the Backend Engineer, Payments role at Stripe: https://boards.greenhouse.io/stripe/jobs/7120543",
        url: "https://boards.greenhouse.io/stripe/jobs/7120543",
        title: "Browser task: boards.greenhouse.io",
        narration: APPLY_NARRATION
      });
      narrate(live.run, live.narration);
      return { focusThreadId: live.thread.id };
    }
  },
  "question-choice": {
    note: "A required choice field without a proposal.",
    build() {
      seedHistory();
      const { run, thread } = waitingRun();
      addQuestion(run, thread, {
        prompt: "The form asks how you heard about this role.",
        schema: {
          fields: [{
            id: 5101,
            label: "How did you hear about this job?",
            display_title: "How did you hear about this job?",
            display_reason: "Nothing in your profile or saved answers says how you found this role.",
            is_choice: true,
            question_kind: "choice",
            required: true,
            options: ["LinkedIn", "Company website", "Referral from an employee", "Job board", "Other"]
          }]
        }
      });
      addMessage(thread, "assistant", "The form asks how you heard about this role.", { question_type: "field_approval" });
      return { focusThreadId: thread.id };
    }
  },
  "question-multi-choice": {
    note: "A required checkbox group: pick one or more.",
    build() {
      seedHistory();
      const { run, thread } = waitingRun();
      addQuestion(run, thread, {
        prompt: "The form asks which factors matter most to you in a new role.",
        schema: {
          fields: [{
            id: 5108,
            label: "Which factors are most important to you? (Select 3)",
            display_title: "Which factors are most important to you? (Select 3)",
            display_reason: "This is about your preferences, and nothing in your profile says which ones you'd pick.",
            is_choice: true,
            multiple: true,
            question_kind: "choice",
            control_kind: "checkbox",
            required: true,
            options: ["Compensation", "Remote flexibility", "Growth and learning", "Team and culture", "Mission", "Work-life balance"]
          }]
        }
      });
      addMessage(thread, "assistant", "The form asks which factors matter most to you in a new role.", { question_type: "field_approval" });
      return { focusThreadId: thread.id };
    }
  },
  "question-proposal": {
    note: "A drafted free-text answer that needs approval.",
    build() {
      seedHistory();
      const { run, thread } = waitingRun();
      addQuestion(run, thread, {
        prompt: "I drafted an answer from your résumé. Approve it, edit it, or write your own.",
        schema: {
          fields: [{
            id: 5102,
            label: "Why do you want to work at Stripe?",
            display_title: "Why do you want to work at Stripe?",
            display_subtitle: "Long answer",
            field_kind: "textarea",
            required: true,
            proposed_answer: "I've spent seven years building payment systems, most recently leading the ledger rewrite at Affirm. Stripe's work on global money movement is the problem I most want to keep working on, and the Payments team's focus on reliability at scale matches what I've done best.",
            confirmation_required: true,
            evidence_coverage: {
              explanation: "Drafted from your résumé (Affirm ledger rewrite, 7 years in payments). Your profile has no stated motivation for Stripe specifically.",
              searched_scopes: ["profile", "resume", "saved_answers"],
              matching_fact_found: false,
              bounded: true
            }
          }]
        }
      });
      return { focusThreadId: thread.id };
    }
  },
  "question-proposal-choice": {
    note: "A proposed option on a choice field.",
    build() {
      seedHistory();
      const { run, thread } = waitingRun();
      addQuestion(run, thread, {
        prompt: "I think the answer is “Yes”, but I need you to confirm it.",
        schema: {
          fields: [{
            id: 5103,
            label: "Are you legally authorized to work in the United States?",
            display_title: "Are you legally authorized to work in the United States?",
            is_choice: true,
            question_kind: "choice",
            required: true,
            options: ["Yes", "No"],
            proposed_option: "Yes",
            confirmation_required: true,
            evidence_coverage: {
              explanation: "Your profile says “US citizen”.",
              searched_scopes: ["profile"],
              matching_fact_found: true,
              bounded: false
            }
          }]
        }
      });
      return { focusThreadId: thread.id };
    }
  },
  "question-document": {
    note: "A cover-letter field: upload, generate, or choose.",
    build() {
      seedHistory();
      state.artifacts.push(artifact(303, "Cover letter — Plaid", "cover_letter", "cover-letter-plaid.docx", 24_100, "Dear Plaid team, I'm writing to apply for the Senior Backend Engineer role…"));
      const { run, thread } = waitingRun();
      addQuestion(run, thread, {
        prompt: "This form wants a cover letter.",
        schema: {
          fields: [{
            id: 5104,
            label: "Cover letter",
            display_title: "Cover letter",
            display_reason: "Optional, but most applicants include one.",
            required: false,
            answer_modes: ["upload_file", "generate_artifact", "choose_artifact"],
            document_capability: { document_kind: "cover_letter", accepted_file_extensions: ["pdf", "docx", "txt"] },
            artifact_options: [
              { id: 303, display_name: "Cover letter — Plaid", filename: "cover-letter-plaid.docx", text_preview: "Dear Plaid team, I'm writing to apply for the Senior Backend Engineer role…" }
            ]
          }]
        }
      });
      return { focusThreadId: thread.id };
    }
  },
  "multi-field": {
    note: "Several fields at once; reply in chat.",
    build() {
      seedHistory();
      const { run, thread } = waitingRun();
      addQuestion(run, thread, {
        prompt: "A few things I couldn't find in your profile:",
        schema: {
          fields: [
            { id: 5105, label: "Desired salary", required: true, stored_answer: "" },
            { id: 5106, label: "Earliest start date", required: true, interpreted_answer_preview: { resolved_value: "Two weeks after an offer" } },
            { id: 5107, label: "Preferred office", required: true, options: ["San Francisco", "Seattle", "New York", "Remote (US)"] }
          ]
        }
      });
      return { focusThreadId: thread.id };
    }
  },
  "final-approval": {
    note: "Approval mode: Lumen waits before the final submit.",
    build() {
      seedHistory();
      const { run, thread } = waitingRun({ status: "Waiting for your approval to submit" });
      addMessage(thread, "assistant", "Everything is filled in and checked. I'm stopping before the final submit.", { source: "action" }, 20);
      addQuestion(run, thread, {
        type: "final_action_approval",
        phase: "FINAL_ACTION",
        prompt: "All 18 fields on the Stripe application are filled and verified, including your résumé upload. The submit button is “Submit application”.\n\nShould I click Submit application now?",
        schema: {
          question_type: "final_action_approval",
          options: ["Submit", "Leave it for my review"],
          final_action: {
            mode: "approval",
            payment_floor: false,
            summary: "",
            submit_label: "Submit application",
            approval_token: "fixture-token-1",
            choices: [
              { label: "Submit", decision: "approve" },
              { label: "Leave it for my review", decision: "decline" }
            ]
          }
        }
      });
      return { focusThreadId: thread.id };
    }
  },
  "payment-approval": {
    note: "The payment floor: confirm before paying.",
    build() {
      seedHistory();
      const run = addRun({ goal: "Book two tickets for Dune: Part Three at AMC Metreon on Friday around 7:30", url: "https://www.amctheatres.com/", runState: "WAITING_FOR_USER", status: "Waiting for your payment confirmation" });
      const thread = addThread({ title: "Browser task: amctheatres.com", runId: run.id });
      addMessage(thread, "user", run.goal, {}, 400);
      addMessage(thread, "assistant", "On it. I'll work in a new Chrome tab on amctheatres.com.", { source: "action" }, 395);
      addQuestion(run, thread, {
        type: "final_action_approval",
        phase: "FINAL_ACTION",
        prompt: "Two seats (H7, H8) for Dune: Part Three, Friday Oct 9 at 7:30 PM, AMC Metreon 16. Total $41.90 on the card saved in your AMC account.\n\nShould I complete the purchase?",
        schema: {
          question_type: "final_action_approval",
          options: ["Confirm and submit", "Leave it for my review"],
          final_action: {
            mode: "approval",
            payment_floor: true,
            summary: "2 tickets · Fri Oct 9, 7:30 PM · AMC Metreon 16 · $41.90",
            submit_label: "Purchase",
            approval_token: "fixture-token-2",
            choices: [
              { label: "Confirm and submit", decision: "approve" },
              { label: "Leave it for my review", decision: "decline" }
            ]
          }
        }
      });
      return { focusThreadId: thread.id };
    }
  },
  "system-block": {
    note: "An engineering block with diagnostics.",
    build() {
      seedHistory();
      const run = addRun({
        goal: "Download my October statement from Chase",
        url: "https://secure.chase.com/",
        runState: "BLOCKED",
        status: "I stopped because the browser agent hit an internal/provider failure (vision_model_timeout). This is an engineering block, not a missing answer from you. The page is left for review.",
        confidence: {
          system_block: { reason: "vision_model_timeout", needs_user: false, block_kind: "awaiting_engineering", model_failure_reason: "provider_timeout" },
          end_to_end: { system_block: true, system_block_reason: "vision_model_timeout", block_kind: "awaiting_engineering" }
        }
      });
      const thread = addThread({ title: "Browser task: secure.chase.com", runId: run.id });
      addMessage(thread, "user", run.goal, {}, 300);
      addMessage(thread, "assistant", run.status_message, { source: "in_browser_outcome", summary: { state: "BLOCKED", reason: "vision_model_timeout", system_block: true, actions: 4, receipts: [] } }, 10);
      return { focusThreadId: thread.id };
    }
  },
  "restart-block": {
    note: "A run the runtime restarted under: resumable.",
    build() {
      seedHistory();
      const run = addRun({
        goal: "Fill out the volunteer sign-up form for the October food drive",
        url: "https://forms.gle/example",
        runState: "BLOCKED",
        status: "The Lumen runtime restarted while this run was executing. Nothing was replayed. Use Resume to reattach with a completely fresh observation.",
        confidence: { system_block: { reason: "runtime_restart", needs_user: false, block_kind: "runtime_restart" } }
      });
      const thread = addThread({ title: "Browser task: forms.gle", runId: run.id });
      addMessage(thread, "user", run.goal, {}, 900);
      addMessage(thread, "assistant", "On it. I'll work in a new Chrome tab on forms.gle.", { source: "action" }, 895);
      return { focusThreadId: thread.id };
    }
  },
  "screen-unavailable": {
    note: "A desktop run paused because the screen could not be captured: resumable.",
    build() {
      seedHistory();
      const run = addRun({
        goal: "Reconcile this week's invoices in the Invoice Viewer",
        url: "desktop://windows/hwnd:7",
        runState: "BLOCKED",
        status: "I paused because I can't see the screen: the computer looks locked, asleep, or covered by a screensaver. Wake or unlock it, then resume and I'll pick up from a fresh look.",
        confidence: { system_block: { reason: "desktop_view_unavailable", needs_user: true, block_kind: "screen_unavailable" } }
      });
      const thread = addThread({ title: "Desktop task: Invoice Viewer", runId: run.id });
      addMessage(thread, "user", run.goal, {}, 900);
      addMessage(thread, "assistant", "On it. I'll work in the Invoice Viewer on your desktop.", { source: "action" }, 895);
      return { focusThreadId: thread.id };
    }
  },
  "sensitive-entry": {
    note: "Chat locks while you type a secret into Chrome.",
    build() {
      seedHistory();
      const run = addRun({
        goal: "Sign in to my utility account and download the latest bill",
        url: "https://myaccount.pge.com/",
        runState: "WAITING_FOR_USER",
        status: "Waiting for you to enter the one-time code in Chrome",
        confidence: { sensitive_browser_takeover: { active: true, handoff_id: "handoff-fixture-1", status: "waiting", kind: "direct_entry", entry_surface: "browser" } }
      });
      const thread = addThread({ title: "Browser task: myaccount.pge.com", runId: run.id });
      addMessage(thread, "user", run.goal, {}, 200);
      addMessage(thread, "assistant", "The site sent a one-time code. Codes never go through chat or a model, so please type it into the Chrome tab yourself.", { source: "action" }, 5);
      return { focusThreadId: thread.id };
    }
  },
  "sensitive-entry-desktop": {
    note: "Chat locks while you type a secret into a Windows app.",
    build() {
      seedHistory();
      const run = addRun({
        goal: "Unlock my password manager and export the vault summary",
        url: "desktop://windows",
        runState: "WAITING_FOR_USER",
        status: "Waiting for you to enter the master password in the app window",
        confidence: { sensitive_browser_takeover: { active: true, handoff_id: "handoff-fixture-3", status: "waiting", kind: "direct_entry", entry_surface: "desktop" } }
      });
      const thread = addThread({ title: "Computer task: Windows", runId: run.id });
      addMessage(thread, "user", run.goal, {}, 200);
      addMessage(thread, "assistant", "The app wants its master password. Passwords never go through chat or a model, so please type it into the window yourself.", { source: "action" }, 5);
      return { focusThreadId: thread.id };
    }
  },
  "credential-handoff": {
    note: "Lumen needs a password saved to the vault.",
    build() {
      seedHistory();
      const run = addRun({
        goal: "Check whether my library books are due this week",
        url: "https://sfpl.bibliocommons.com/",
        runState: "WAITING_FOR_USER",
        status: "Waiting for sign-in details",
        confidence: { sensitive_browser_takeover: { active: true, handoff_id: "handoff-fixture-2", status: "waiting", kind: "credential_vault", credential_origin: "https://sfpl.bibliocommons.com", credential_slot: "password" } }
      });
      const thread = addThread({ title: "Browser task: sfpl.bibliocommons.com", runId: run.id });
      addMessage(thread, "user", run.goal, {}, 200);
      addMessage(thread, "assistant", "The library site wants a password I don't have saved.", { source: "action" }, 5);
      return { focusThreadId: thread.id };
    }
  },
  outcomes: {
    note: "Completed, ready-for-review, and stopped outcomes in one thread.",
    build() {
      seedHistory();
      const run = addRun({ goal: "Fill out the Plaid application", url: "https://jobs.lever.co/plaid/abc", runState: "READY_FOR_REVIEW", status: "Ready for review" });
      const thread = addThread({ title: "Application: jobs.lever.co", runId: run.id });
      addMessage(thread, "user", "Fill out the Plaid application at https://jobs.lever.co/plaid/abc but don't submit", {}, 600);
      addMessage(thread, "assistant", "On it. I'll work in a new Chrome tab on jobs.lever.co and stop before submitting.", { source: "action" }, 595);
      outcome(thread, run, "I filled 16 of 17 fields and checked each one. (1 I couldn't confirm — please double-check it.) The browser work is ready for your review. No consequential final action was executed.", "READY_FOR_REVIEW", { verified: 16, unconfirmed: 1, ageSeconds: 120 });
      addMessage(thread, "user", "Looks good. Also check the LinkedIn field", {}, 90);
      outcome(thread, run, "I stopped after 3 step(s) (no_progress). The page is left for your review.", "BLOCKED", { verified: 1, failed: 2, reason: "no_progress", ageSeconds: 60 });
      addMessage(thread, "user", "Try the last page once more", {}, 45);
      outcome(
        thread,
        run,
        "Filled 14 field(s) in your browser, verified by reading them back. These required field(s) still need you: Desired salary, Earliest start date. Answer here and I'll fill them, or complete them in the form. No final action was executed.",
        "READY_FOR_REVIEW",
        { verified: 14, requiredOpen: ["Desired salary", "Earliest start date"], ageSeconds: 20 }
      );
      addMessage(thread, "user", "Now save the Q3 report as a PDF", {}, 15);
      outcome(thread, run, "Saved the report as Q3.pdf. Completed 5 browser action(s) with verified outcomes. No consequential final action was executed.", "READY_FOR_REVIEW", { verified: 5, completion: "complete", ageSeconds: 10 });
      addMessage(thread, "user", "And export the March one too", {}, 8);
      outcome(thread, run, "The March report is open. I couldn't confirm from the browser that the task is finished: no saved file is shown. Completed 3 browser action(s) with verified outcomes. No consequential final action was executed.", "READY_FOR_REVIEW", { verified: 3, completion: "incomplete", ageSeconds: 4 });
      return { focusThreadId: thread.id };
    }
  },
  paused: {
    note: "A run you paused.",
    build() {
      seedHistory();
      const live = liveRun({
        goal: "Compare the three cheapest flights from SFO to JFK on Oct 18",
        url: "https://www.google.com/travel/flights",
        title: "Browser task: google.com",
        narration: ["Reading flight results"]
      });
      live.run.current_state = "WAITING_FOR_USER";
      live.run.status_message = "Paused. Resume when you're ready.";
      live.run.confidence_state.user_pause = { active: true };
      return { focusThreadId: live.thread.id };
    }
  },
  "runtime-offline": {
    note: "Desktop runtime offline.",
    build() {
      seedHistory();
      state.desktop.backendHealthy = false;
    }
  },
  "extension-stale": {
    note: "Chrome extension connected but needs a reload.",
    build() {
      seedHistory();
      state.desktop.liveActionsReady = false;
    }
  }
};

function waitingRun({ status = "Waiting for your answer" } = {}) {
  const run = addRun({
    goal: "Apply to the Backend Engineer, Payments role at Stripe: https://boards.greenhouse.io/stripe/jobs/7120543",
    url: "https://boards.greenhouse.io/stripe/jobs/7120543",
    runState: "WAITING_FOR_USER",
    status,
    confidence: { context_usage: { used_tokens: 61_200, limit_tokens: 200_000, model: "z-ai/glm-5.3-flash", provider_display_name: "GLM 5.3 Flash", context_window_source: "provider" } }
  });
  const thread = addThread({ title: "Browser task: boards.greenhouse.io", runId: run.id });
  addMessage(thread, "user", run.goal, {}, 420);
  addMessage(thread, "assistant", "On it. I'll work in a new Chrome tab on boards.greenhouse.io and check each step before moving on.", { source: "action" }, 415);
  return { run, thread };
}

let currentScenario = process.env.FIXTURE_SCENARIO || "home";

function loadScenario(name) {
  clearTimers();
  seq = 1000;
  state = baseState();
  const scenario = SCENARIOS[name] || SCENARIOS.home;
  currentScenario = SCENARIOS[name] ? name : "home";
  const result = scenario.build() || {};
  state.focusThreadId = result.focusThreadId ?? null;
  syncProviderFlags();
}

function syncProviderFlags() {
  for (const item of state.providers) item.is_active = item.id === state.activeProviderId;
}

// Provider types that can carry a screenshot; every seat needs one.
const IMAGE_TYPES = new Set(["openai", "anthropic", "gemini", "openrouter", "custom_openai_compatible"]);

function providerModelKey(id) {
  const row = state.providers.find((item) => item.id === id);
  return row ? [row.provider_type, (row.base_url || "").replace(/\/+$/, ""), row.model_name].join("|").toLowerCase() : null;
}

// The seat invariants the API keeps: the Checker is never the Brain's model, and Escalation is neither of them.
function enforceSeatInvariants() {
  const seats = state.settings;
  const same = (a, b) => a != null && b != null && (a === b || (providerModelKey(a) != null && providerModelKey(a) === providerModelKey(b)));
  if (same(seats.judge_provider_id, seats.vision_provider_id)) seats.judge_provider_id = null;
  if (same(seats.vision_escalation_provider_id, seats.vision_provider_id) || same(seats.vision_escalation_provider_id, seats.judge_provider_id)) {
    seats.vision_escalation_provider_id = null;
  }
}

// ---------------------------------------------------------------------------------------------------
// Scripted run for a newly sent task

const TASK_HINT = /\b(apply|book|buy|find|fill|search|compare|order|open|download|rename|organi[sz]e|sign|check|get|go to|look up|reserve|schedule|send|move|install|update|https?:\/\/)/i;

function startScriptedRun(thread, content) {
  const urlMatch = content.match(/https?:\/\/\S+/);
  const desktop = /\b(desktop|windows|file explorer|folder|downloads|excel|word|notepad|rename)\b/i.test(content) && !urlMatch;
  const url = urlMatch ? urlMatch[0] : desktop ? "desktop://windows" : "https://www.google.com/";
  const run = addRun({
    goal: content,
    url,
    runState: "RUNNING",
    status: desktop ? "Looking at your screen" : `Opening ${hostOf(url)}`,
    confidence: { context_usage: { used_tokens: 4_200, limit_tokens: 200_000, model: "z-ai/glm-5.3-flash", provider_display_name: "GLM 5.3 Flash", context_window_source: "provider" } }
  });
  thread.run_id = run.id;
  thread.mode = "run";
  thread.title = desktop ? "Computer task: Windows" : `Browser task: ${hostOf(url)}`;
  const lines = desktop
    ? ["Looking at your screen", "Opening File Explorer", "Reading the folder contents", "Choosing the next step"]
    : [`Opening ${hostOf(url)}`, "Reading the page", "Scrolling to the results", "Comparing the top options", "Choosing the next step"];
  narrate(run, lines, 2200);
  later(13_000, () => {
    if (run.current_state !== "RUNNING") return;
    run.current_state = "WAITING_FOR_USER";
    run.status_message = "Waiting for your answer";
    addQuestion(run, thread, {
      prompt: "Quick check before I keep going.",
      schema: {
        fields: [{
          id: nextId(),
          label: "Which matters more for this task?",
          display_title: "Which matters more for this task?",
          display_reason: "Your request could be read either way, and I don't want to guess.",
          is_choice: true,
          question_kind: "choice",
          required: true,
          options: ["Lowest price", "Best reviews", "Closest to me"]
        }]
      }
    });
  });
  return run;
}

function resumeAfterAnswer(run, thread, answer) {
  run.current_state = "RUNNING";
  run.status_message = `Using “${answer}”`;
  narrate(run, [`Using “${answer}”`, "Re-reading the page", "Checking the result", "Choosing the next step"], 2000);
  later(9_000, () => {
    if (run.current_state !== "RUNNING") return;
    if (state.settings.final_action_mode === "autonomous") {
      finishRun(run, thread, "COMPLETED", "Done. I finished the task and verified the result on the page.");
      return;
    }
    run.current_state = "WAITING_FOR_USER";
    run.status_message = "Waiting for your approval";
    addQuestion(run, thread, {
      type: "final_action_approval",
      phase: "FINAL_ACTION",
      prompt: `Everything for “${run.goal.slice(0, 80)}” is ready. The next click is “Confirm”.\n\nShould I click Confirm now?`,
      schema: {
        question_type: "final_action_approval",
        options: ["Approve exact action", "Leave it for my review"],
        final_action: {
          mode: "approval",
          payment_floor: false,
          summary: "",
          submit_label: "Confirm",
          approval_token: `fixture-token-${run.id}`,
          choices: [
            { label: "Approve exact action", decision: "approve" },
            { label: "Leave it for my review", decision: "decline" }
          ]
        }
      }
    });
  });
}

function finishRun(run, thread, runState, content) {
  run.current_state = runState;
  run.status_message = content;
  run.updated_at = iso();
  outcome(thread, run, content, runState, { verified: 9 });
}

// ---------------------------------------------------------------------------------------------------
// HTTP plumbing

function send(res, status, body) {
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "no-store"
  });
  res.end(JSON.stringify(body));
}

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const raw = Buffer.concat(chunks);
  const type = String(req.headers["content-type"] || "");
  if (type.includes("application/json")) {
    try {
      return JSON.parse(raw.toString("utf8") || "{}");
    } catch {
      return {};
    }
  }
  if (type.includes("multipart/form-data")) {
    // Enough multipart parsing for a fixture: field values and the uploaded file's name.
    const text = raw.toString("latin1");
    const fields = {};
    for (const part of text.split(/--[-\w]+/)) {
      const name = part.match(/name="([^"]+)"/)?.[1];
      if (!name) continue;
      const filename = part.match(/filename="([^"]*)"/)?.[1];
      const value = part.split(/\r\n\r\n/).slice(1).join("\r\n\r\n").replace(/\r\n$/, "");
      fields[name] = filename != null ? { filename, size: value.length } : value;
    }
    return fields;
  }
  return {};
}

function threadById(id) {
  return state.threads.find((thread) => thread.id === Number(id));
}

function runById(id) {
  return state.runs.find((run) => run.id === Number(id));
}

function pendingQuestionFor(runId) {
  return [...state.questions].reverse().find((question) => question.run_id === runId && question.status === "pending") || null;
}

function threadMessages(threadId) {
  return state.messages.filter((message) => message.thread_id === Number(threadId));
}

function modelProvidersResponse() {
  syncProviderFlags();
  const active = state.providers.find((item) => item.is_active) || {
    ...provider(null, "Not configured", "openai", "", "unknown"),
    id: null,
    has_api_key: false,
    api_key_masked: "",
    source: "environment"
  };
  return {
    items: state.providers,
    active,
    env_fallback: { ...active, id: null, source: "environment" },
    supported_provider_types: ["openai", "anthropic", "gemini", "openrouter", "deepseek", "custom_openai_compatible"]
  };
}

async function streamChat(req, res, thread, body) {
  const content = String(body.content || "").trim();
  const payload = body.payload || {};
  res.writeHead(200, {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-store",
    Connection: "keep-alive",
    "Access-Control-Allow-Origin": "*"
  });
  const frame = (event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);

  if (!threadMessages(thread.id).length && thread.title === "Lumen chat") {
    thread.title = content.split("\n")[0].slice(0, 60);
  }
  const userMessage = addMessage(thread, "user", content, { source: "user_mode_chat", view_mode: "user" });
  const run = thread.run_id ? runById(thread.run_id) : null;
  const pending = run ? pendingQuestionFor(run.id) : null;

  let reply;
  let taskRequest = null;
  if (!state.providers.length) {
    reply = "I need a model before I can do anything. Add one in **Settings → Models**, then ask me again.";
  } else if (pending && run) {
    const selectedOptions = Array.isArray(payload.selected_options)
      ? payload.selected_options.map((option) => String(option).trim()).filter(Boolean)
      : [];
    const answer = selectedOptions.length ? selectedOptions.join(", ") : payload.selected_option || content;
    pending.status = "answered";
    pending.answer_payload = { answer, ...payload };
    pending.updated_at = iso();
    reply = payload.skip
      ? "Okay — I'll leave that one for you and keep going."
      : payload.keep_proposed
        ? `Thanks — I'll use that answer${payload.save_scope === "permanent" ? " and remember it for next time" : ""}.`
        : `Got it — “${answer.slice(0, 80)}”. Continuing.`;
    resumeAfterAnswer(run, thread, answer.slice(0, 40));
  } else if (TASK_HINT.test(content)) {
    const started = startScriptedRun(thread, content);
    taskRequest = { run_id: started.id };
    reply = started.target_url.startsWith("desktop://")
      ? "On it. I'll work on your desktop and narrate each step. You can pause or stop me anytime."
      : `On it. I'll work in a new Chrome tab on ${hostOf(started.target_url)} and check each step before moving on.`;
  } else {
    reply = "I'm a fixture backend, so I can only pretend. Ask me to *find*, *book*, *apply*, or *fill* something to watch a scripted run play out.";
  }

  await sleep(500);
  frame("meta", {
    user_message: userMessage,
    chat_scope: null,
    pending_question: run ? pendingQuestionFor(run.id) : null,
    task_request: taskRequest,
    application_request: null,
    run_control: null,
    runtime_correction: null,
    question_answer: null
  });
  await sleep(700);
  for (const word of reply.split(/(?<=\s)/)) {
    frame("token", { text: word });
    await sleep(28);
  }
  const assistant = addMessage(thread, "assistant", reply, { source: taskRequest ? "action" : "chat" });
  frame("done", { assistant_message: assistant, context: {}, chat_response: taskRequest ? { active_run_id: taskRequest.run_id } : null });
  res.end();
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url || "/", "http://fixture");
  const path = url.pathname.replace(/\/+$/, "") || "/";
  const method = req.method || "GET";

  if (method === "OPTIONS") {
    res.writeHead(204, {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Accept"
    });
    res.end();
    return;
  }

  try {
    // ----- fixture controls
    if (path === "/__fixture") {
      return send(res, 200, {
        current: currentScenario,
        scenarios: Object.fromEntries(Object.entries(SCENARIOS).map(([name, scenario]) => [name, scenario.note]))
      });
    }
    let match = path.match(/^\/__fixture\/scenario\/([\w-]+)$/);
    if (match) {
      loadScenario(match[1]);
      return send(res, 200, { scenario: currentScenario, focus_thread_id: state.focusThreadId });
    }
    if (path === "/__fixture/desktop-status") return send(res, 200, state.desktop);
    match = path.match(/^\/__fixture\/desktop\/(start|restart|computer-use)$/);
    if (match && method === "POST") {
      const body = await readBody(req);
      if (match[1] === "computer-use") state.desktop.computerUseEnabled = Boolean(body.enabled);
      else state.desktop.backendHealthy = true;
      return send(res, 200, state.desktop);
    }

    // ----- health
    if (path === "/health") return send(res, 200, { status: "ok", service: "fixture" });

    // ----- models and settings
    if (path === "/model-providers" && method === "GET") return send(res, 200, modelProvidersResponse());
    if (path === "/model-providers" && method === "POST") {
      const body = await readBody(req);
      const created = provider(nextId(), body.display_name, body.provider_type, body.model_name, "healthy");
      state.providers.push(created);
      state.desktop.modelConfigured = true;
      state.desktop.modelReady = true;
      return send(res, 200, { provider: created, active: modelProvidersResponse().active });
    }
    match = path.match(/^\/model-providers\/(\d+)\/activate$/);
    if (match) {
      state.activeProviderId = Number(match[1]);
      syncProviderFlags();
      const active = state.providers.find((item) => item.id === state.activeProviderId);
      // The active row is the Brain when it can read a screenshot.
      if (active && IMAGE_TYPES.has(active.provider_type)) {
        state.settings.vision_provider_id = active.id;
        enforceSeatInvariants();
      }
      state.desktop.modelDisplayName = active?.display_name || "";
      return send(res, 200, { provider: active, active });
    }
    if (path === "/model-providers/available-models") {
      const body = await readBody(req);
      await sleep(600);
      if (!body.key_from_provider_id && String(body.api_key || "").length < 6) return send(res, 400, { detail: "That key was rejected by the provider." });
      const catalog = {
        openai: ["gpt-6-luna", "gpt-6-sol", "gpt-6-mini"],
        anthropic: ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001"],
        gemini: ["gemini-3.5-pro", "gemini-3.5-flash"],
        openrouter: ["z-ai/glm-5.3-flash", "qwen/qwen3.8-flash", "moonshotai/kimi-k3"],
        deepseek: ["deepseek-v4", "deepseek-v4-reasoner"],
        custom_openai_compatible: ["local-model"]
      };
      return send(res, 200, { models: catalog[body.provider_type] || ["model-a", "model-b"] });
    }
    if (path === "/agent-settings" && method === "GET") return send(res, 200, { settings: state.settings });
    if (path === "/agent-settings" && method === "PATCH") {
      const body = await readBody(req);
      await sleep(250);
      if (body.vision_provider_id != null) {
        // Setting the Brain activates its row.
        const brain = state.providers.find((item) => item.id === body.vision_provider_id);
        if (!brain || !brain.enabled) return send(res, 400, { detail: "Cannot activate a disabled model provider." });
        state.activeProviderId = brain.id;
        syncProviderFlags();
        state.desktop.modelDisplayName = brain.display_name;
      }
      Object.assign(state.settings, body);
      enforceSeatInvariants();
      return send(res, 200, { settings: state.settings });
    }

    // ----- paired phones
    if (path === "/devices/remote-status" && method === "GET") {
      return send(res, 200, {
        ready: true,
        problem: null,
        public_url: "https://lumen-pc.example.ts.net",
        listener: { enabled: true, port: 8002, running: true, error: null },
        tailscale: { installed: true, logged_in: true, dns_name: "lumen-pc.example.ts.net", url: "https://lumen-pc.example.ts.net", serve_proxy: "http://127.0.0.1:8002" },
        serve_command: "tailscale serve --bg --https=443 http://127.0.0.1:8002",
        push: { configured: true, project_id: "fixture" }
      });
    }
    // The Android remote's routes (apps/mobile). Tokens and step-up signatures are accepted as-is.
    if (path === "/devices/pair" && method === "POST") {
      const body = await readBody(req);
      const device = { id: 100 + state.devices.length, name: body.name || "Phone", platform: "android", has_push: false, last_seen_at: new Date().toISOString(), created_at: new Date().toISOString() };
      state.devices.push(device);
      return send(res, 200, { device, token: `fixture-token-${device.id}` });
    }
    if (path === "/devices/me" && method === "GET") return send(res, 200, { device: state.devices[state.devices.length - 1] });
    if (path === "/devices/me" && method === "DELETE") return send(res, 200, { revoked: true });
    if (path === "/devices/me/push-token" && method === "POST") return send(res, 200, { device: { ...state.devices[state.devices.length - 1], has_push: true } });
    if (path === "/devices/me/challenges" && method === "POST") {
      return send(res, 200, { nonce: `fixture-${Date.now().toString(36)}`, expires_in_seconds: 120, format: "lumen-step-up/v1" });
    }
    if (path === "/devices" && method === "GET") return send(res, 200, { devices: state.devices });
    if (path === "/devices/pairing-codes" && method === "POST") {
      const code = `fixture-${Date.now().toString(36)}`;
      return send(res, 200, {
        code,
        expires_at: new Date(Date.now() + 300_000).toISOString(),
        expires_in_seconds: 300,
        public_url: "https://lumen-pc.example.ts.net",
        pairing_uri: `lumen://pair?v=1&url=https%3A%2F%2Flumen-pc.example.ts.net&code=${code}`,
        ready: true,
        problem: null
      });
    }
    if (path.startsWith("/devices/") && method === "DELETE") {
      const id = Number(path.split("/")[2]);
      state.devices = state.devices.filter((device) => device.id !== id);
      return send(res, 200, { revoked: true, id });
    }

    // ----- what Lumen knows (the profile memory)
    const memoryPayload = () => ({
      sections: [["about", "About you"], ["contact", "Contact"], ["work", "Work"], ["education", "Education"], ["preferences", "Preferences"], ["sensitive", "Sensitive"], ["other", "Other"]].map(([key, title]) => ({ key, title })),
      facts: state.memoryFacts
    });
    const memoryKey = (label) => String(label || "").toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 80);
    if (path === "/memory" && method === "GET") return send(res, 200, memoryPayload());
    if (path === "/memory" && method === "POST") {
      const body = await readBody(req);
      const key = memoryKey(body.label);
      if (state.memoryFacts.some((fact) => fact.key === key)) return send(res, 409, { detail: "You already have a fact with that name. Edit it instead." });
      state.memoryFacts.push({ key, label: String(body.label), value: String(body.value), section: body.section || "other", learned_on: new Date().toISOString().slice(0, 10), source: "added by you" });
      return send(res, 200, memoryPayload());
    }
    match = path.match(/^\/memory\/([a-z0-9_]+)$/);
    if (match && method === "PATCH") {
      const body = await readBody(req);
      const fact = state.memoryFacts.find((item) => item.key === match[1]);
      if (!fact) return send(res, 404, { detail: "That fact is gone." });
      if (body.label) Object.assign(fact, { key: memoryKey(body.label), label: body.label });
      if (body.value) fact.value = body.value;
      if (body.section) fact.section = body.section;
      Object.assign(fact, { learned_on: new Date().toISOString().slice(0, 10), source: "edited by you" });
      return send(res, 200, memoryPayload());
    }
    if (match && method === "DELETE") {
      state.memoryFacts = state.memoryFacts.filter((item) => item.key !== match[1]);
      return send(res, 200, memoryPayload());
    }

    // ----- profile
    if (path === "/profiles/me") {
      return send(res, 200, { user: state.user, profile: state.profile, common_answers: state.answers });
    }
    if (path === "/profiles" && method === "POST") {
      const body = await readBody(req);
      await sleep(350);
      state.user = { ...state.user, name: body.name, email: body.email };
      state.profile = { ...state.profile, ...body };
      return send(res, 200, { user_id: 1, profile_id: 1, status: "saved" });
    }
    if (path === "/profiles/me/closed-world-attestations" && method === "GET") return send(res, 200, { items: state.attestations });
    if (path === "/profiles/me/closed-world-attestations" && method === "POST") {
      const body = await readBody(req);
      const now = iso();
      for (const item of state.attestations) if (item.status === "active") item.status = "revoked";
      const attestation = {
        id: `att-${nextId()}`,
        scope: body.scope,
        set_key: body.set_key,
        members: body.members,
        exhaustive: true,
        status: "active",
        attested_by: "user",
        source: "profile_attestation_api",
        version: state.attestations.length + 1,
        created_at: now,
        updated_at: now,
        revoked_at: null
      };
      state.attestations.push(attestation);
      return send(res, 200, { attestation });
    }
    match = path.match(/^\/profiles\/me\/closed-world-attestations\/([\w-]+)\/revoke$/);
    if (match) {
      const attestation = state.attestations.find((item) => item.id === match[1]);
      if (attestation) {
        attestation.status = "revoked";
        attestation.revoked_at = iso();
        attestation.updated_at = iso();
      }
      return send(res, 200, { attestation });
    }
    match = path.match(/^\/operator\/answers\/(\d+)$/);
    if (match && method === "PATCH") {
      const body = await readBody(req);
      const answer = state.answers.find((item) => item.id === Number(match[1]));
      if (answer) answer.answer = body.answer;
      return send(res, 200, { id: Number(match[1]), answer: body.answer });
    }
    if (match && method === "DELETE") {
      state.answers = state.answers.filter((item) => item.id !== Number(match[1]));
      return send(res, 200, { deleted: true, id: Number(match[1]) });
    }

    // ----- credential vault
    if (path === "/credential-vault" && method === "GET") {
      const origin = url.searchParams.get("origin");
      return send(res, 200, { items: origin ? state.vault.filter((item) => item.origin === origin) : state.vault });
    }
    if (path === "/credential-vault" && method === "POST") {
      const body = await readBody(req);
      const item = vaultItem(nextId(), body.account_label, body.origin, Object.keys(body.fields || {}), body.auto_use, null);
      state.vault.push(item);
      return send(res, 200, item);
    }
    match = path.match(/^\/credential-vault\/(\d+)$/);
    if (match && method === "PATCH") {
      const body = await readBody(req);
      const item = state.vault.find((entry) => entry.id === Number(match[1]));
      if (!item) return send(res, 404, { detail: "Not found" });
      if (body.account_label) item.account_label = body.account_label;
      if (body.origin) item.origin = body.origin;
      if (typeof body.auto_use === "boolean") item.auto_use = body.auto_use;
      if (body.fields) item.available_slots = [...new Set([...item.available_slots, ...Object.keys(body.fields)])];
      item.updated_at = iso();
      return send(res, 200, item);
    }
    if (match && method === "DELETE") {
      state.vault = state.vault.filter((entry) => entry.id !== Number(match[1]));
      return send(res, 200, { deleted: true, id: Number(match[1]) });
    }

    // ----- artifacts
    if (path === "/artifacts" && method === "GET") return send(res, 200, state.artifacts);
    if (path === "/artifacts/preview") {
      const body = await readBody(req);
      await sleep(1200);
      const filename = body.file?.filename || "document.pdf";
      return send(res, 200, {
        draft_id: `draft-${nextId()}`,
        filename,
        mime_type: "application/pdf",
        file_size: body.file?.size || 120_000,
        text_preview: "",
        extracted_text_preview: "MAYA CHEN · Senior Backend Engineer · Oakland, CA · maya.chen@example.com · Experience: Affirm (2021–2026), Square (2018–2021)…",
        suggested_artifact_kind: /resume|cv/i.test(filename) ? "resume" : "reference_letter",
        suggested_display_name: filename.replace(/\.[^.]+$/, "").replace(/[-_]+/g, " "),
        summary: "A one-page résumé for a senior backend engineer focused on payments infrastructure.",
        key_facts: [
          { label: "Name", value: "Maya Chen" },
          { label: "Most recent role", value: "Senior Backend Engineer, Affirm" },
          { label: "Years of experience", value: "7" }
        ],
        confidence: 0.91,
        warnings: [],
        extraction_status: "extracted",
        provider: "fixture",
        model: "fixture"
      });
    }
    if (path === "/artifacts" && method === "POST") {
      const body = await readBody(req);
      await sleep(400);
      const created = artifact(nextId(), body.display_name || "New document", body.selected_type || "document", "uploaded.pdf", 98_000, "");
      state.artifacts.unshift(created);
      return send(res, 200, created);
    }
    match = path.match(/^\/artifacts\/(\d+)$/);
    if (match && method === "PATCH") {
      const body = await readBody(req);
      const item = state.artifacts.find((entry) => entry.id === Number(match[1]));
      if (item) {
        item.text_content = body.text_content;
        item.text_preview = String(body.text_content || "").slice(0, 200);
      }
      return send(res, 200, item);
    }
    if (match && method === "DELETE") {
      state.artifacts = state.artifacts.filter((entry) => entry.id !== Number(match[1]));
      return send(res, 200, { deleted: true, id: Number(match[1]), deleted_files: [] });
    }
    match = path.match(/^\/artifacts\/(\d+)\/download$/);
    if (match) {
      res.writeHead(200, { "Content-Type": "text/plain", "Access-Control-Allow-Origin": "*" });
      res.end("fixture document");
      return;
    }

    // ----- questions and runs
    match = path.match(/^\/operator\/questions\/(\d+)\/answer$/);
    if (match) {
      const body = await readBody(req);
      const question = state.questions.find((item) => item.id === Number(match[1]));
      if (!question) return send(res, 404, { detail: "Question not found" });
      const run = runById(question.run_id);
      const thread = state.threads.find((item) => item.run_id === run.id);
      question.status = "answered";
      question.answer_payload = body;
      await sleep(400);
      if (body.final_action_decision === "approve") {
        run.current_state = "RUNNING";
        run.status_message = `Clicking “${question.expected_answer_schema.final_action?.submit_label || "the approved button"}”`;
        addMessage(thread, "user", body.answer);
        later(3500, () => {
          run.status_message = "Checking the page for a confirmation";
          later(3000, () => finishRun(run, thread, "COMPLETED", "Done. I clicked the exact button you approved and the page now shows a confirmation."));
        });
      } else {
        addMessage(thread, "user", body.answer);
        run.current_state = "READY_FOR_REVIEW";
        run.status_message = "Left for your review";
        outcome(thread, run, "Okay — I left it for your review. Nothing was submitted.", "READY_FOR_REVIEW", { verified: 0 });
      }
      return send(res, 200, { question, run, applied: null, can_resume: true });
    }
    match = path.match(/^\/operator\/questions\/(\d+)\/artifacts\/(upload|generate)$/);
    if (match) {
      const body = await readBody(req);
      await sleep(match[2] === "generate" ? 1600 : 600);
      const question = state.questions.find((item) => item.id === Number(match[1]));
      const created = artifact(
        nextId(),
        match[2] === "generate" ? "Cover letter — Stripe (draft)" : body.file?.filename || "Upload",
        "cover_letter",
        match[2] === "generate" ? "cover-letter-stripe.txt" : body.file?.filename || "upload.pdf",
        6_400,
        match[2] === "generate"
          ? "Dear Stripe Payments team,\n\nI've spent seven years building payment systems, most recently leading the ledger rewrite at Affirm, which moved $40B a year onto a double-entry core without downtime…\n\nBest,\nMaya Chen"
          : ""
      );
      created.text_content = created.text_preview;
      state.artifacts.unshift(created);
      if (match[2] === "upload" && question) {
        question.status = "answered";
        const run = runById(question.run_id);
        resumeAfterAnswer(run, threadById(question.thread_id), created.metadata_json.display_name);
      }
      return send(res, 200, { artifact: created, question, run: question ? runById(question.run_id) : null, applied: null });
    }
    match = path.match(/^\/operator\/questions\/(\d+)\/artifacts\/(\d+)\/apply$/);
    if (match) {
      const question = state.questions.find((item) => item.id === Number(match[1]));
      if (question) {
        question.status = "answered";
        resumeAfterAnswer(runById(question.run_id), threadById(question.thread_id), "the cover letter");
      }
      return send(res, 200, { artifact: state.artifacts.find((item) => item.id === Number(match[2])) || null, question, run: question ? runById(question.run_id) : null, applied: {} });
    }
    if (path === "/operator/runs") return send(res, 200, [...state.runs].sort((a, b) => b.id - a.id).slice(0, 20));
    match = path.match(/^\/operator\/runs\/(\d+)$/);
    if (match) {
      const run = runById(match[1]);
      return run ? send(res, 200, run) : send(res, 404, { detail: "Run not found" });
    }
    match = path.match(/^\/operator\/runs\/(\d+)\/questions$/);
    if (match) return send(res, 200, state.questions.filter((item) => item.run_id === Number(match[1])));
    match = path.match(/^\/operator\/runs\/(\d+)\/(pause|resume|cancel|sensitive-browser-continue)$/);
    if (match) {
      const run = runById(match[1]);
      if (!run) return send(res, 404, { detail: "Run not found" });
      await sleep(300);
      const thread = state.threads.find((item) => item.run_id === run.id);
      if (match[2] === "pause") {
        run.confidence_state = { ...run.confidence_state, user_pause: { active: true } };
        run.current_state = "WAITING_FOR_USER";
        run.status_message = "Paused. Resume when you're ready.";
      } else if (match[2] === "resume") {
        run.confidence_state = { ...run.confidence_state, user_pause: { active: false }, system_block: undefined, end_to_end: undefined };
        run.current_state = "RUNNING";
        run.status_message = "Picking up where I left off";
        narrate(run, ["Picking up where I left off", ...APPLY_NARRATION.slice(2)]);
      } else if (match[2] === "cancel") {
        run.current_state = "CANCELLED";
        run.status_message = "Stopped by you.";
        run.confidence_state = { ...run.confidence_state, sensitive_browser_takeover: { active: false } };
        for (const question of state.questions) if (question.run_id === run.id && question.status === "pending") question.status = "superseded";
        if (thread) addMessage(thread, "assistant", "Stopped. I left the page as it is; nothing else will happen.", { source: "runtime_control", reason: "user_cancelled", run_state: "CANCELLED" });
      } else {
        run.confidence_state = { ...run.confidence_state, sensitive_browser_takeover: { active: false } };
        run.current_state = "RUNNING";
        run.status_message = "Re-reading the page after your entry";
        narrate(run, ["Re-reading the page after your entry", "Signed in. Opening Billing", "Downloading the latest bill"]);
      }
      run.updated_at = iso();
      return send(res, 200, run);
    }

    // ----- chat
    if (path === "/operator/chat/threads" && method === "GET") {
      const runId = Number(url.searchParams.get("run_id") || 0);
      const items = state.threads
        .filter((thread) => thread.status === "active" && (!runId || thread.run_id === runId))
        .sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1))
        .slice(0, 30)
        .map(threadOut);
      return send(res, 200, items);
    }
    if (path === "/operator/chat/threads" && method === "POST") return send(res, 200, threadOut(addThread({ title: "Lumen chat" })));
    match = path.match(/^\/operator\/chat\/threads\/(\d+)$/);
    if (match && method === "DELETE") {
      const thread = threadById(match[1]);
      if (thread) thread.status = "deleted";
      return send(res, 200, thread);
    }
    match = path.match(/^\/operator\/chat\/threads\/(\d+)\/messages$/);
    if (match && method === "GET") return send(res, 200, threadMessages(match[1]));
    if (match && method === "POST") {
      const thread = threadById(match[1]);
      if (!thread) return send(res, 404, { detail: "Chat thread not found" });
      const run = thread.run_id ? runById(thread.run_id) : null;
      if (run?.confidence_state?.sensitive_browser_takeover?.active) return send(res, 409, { detail: "Chat is locked during sensitive entry." });
      return streamChat(req, res, thread, await readBody(req));
    }

    return send(res, 404, { detail: `Fixture has no route for ${method} ${path}` });
  } catch (error) {
    return send(res, 500, { detail: String(error?.stack || error) });
  }
});

loadScenario(currentScenario);
server.listen(PORT, "127.0.0.1", () => {
  console.log(`Lumen fixture backend on http://127.0.0.1:${PORT} (scenario: ${currentScenario})`);
});
