/* Interaction Gauntlet generations 3-5 — shared page runtime.
 *
 * The harness (v3_harness.py) is the judge. This script only reports what happened on the page
 * and renders the harness's verdict; it never holds an answer or a pass token.
 *
 * V3.trusted(e) / V3.untrusted(key): a person's pointer and keyboard always produce trusted events;
 * pages ignore script-dispatched ones and report them as ineffective.
 * V3.beacon(kind, data): a report that still arrives when the page is closing (tab closed, navigated away).
 *
 * ?focus=<challenge> runs one challenge alone: the harness judges only it, and every other challenge's
 * section (a section.ch holding a [data-step-mark] for another step) is hidden. ?tag=<name> marks one of
 * several concurrent runs of the page (live_run --repeat), so they never replace one another.
 *
 * Markup contract:
 *   <body data-page="v3_x">            the mission this page belongs to
 *   <body data-aux="name">             a helper window of that mission (sid comes from ?sid=)
 *   #bench-progress / #bench-verdict   progress line and verdict (same text shape as gens 1-2)
 *   [data-step-mark=step]              shows "✓ complete" once the harness marks the step done
 *   [data-unlock=step]                 carries class "locked" until that step is done
 *   [data-public=key]                  text from the session's public values
 *   [data-msg=key]                     the harness's latest message for that key
 */
(function () {
  "use strict";
  const body = document.body;
  const PAGE = body.dataset.page;
  const AUX = body.dataset.aux || "";
  const params = new URLSearchParams(location.search);
  const FOCUS = AUX ? "" : params.get("focus") || "";
  const TAG = AUX ? "" : params.get("tag") || "";
  // ?perturb=a,b&seed=N (generation 6 robustness lanes): the harness records them; v6_kit.js applies them.
  const PERTURB = AUX ? "" : params.get("perturb") || "";
  const SEED = AUX ? "" : params.get("seed") || "";
  const STORE = "v3-sid-" + PAGE + (FOCUS ? "#" + FOCUS : "") + (TAG ? "@" + TAG : "") + (PERTURB ? "~" + PERTURB : "");
  let sid = params.get("sid") || (AUX ? "" : sessionStorage.getItem(STORE) || "");
  let state = null;
  const listeners = [];
  let readyResolve;
  const ready = new Promise((resolve) => (readyResolve = resolve));

  async function api(method, path, payload) {
    const res = await fetch(path, {
      method,
      cache: "no-store",
      headers: payload ? { "Content-Type": "application/json" } : {},
      body: payload ? JSON.stringify(payload) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "harness HTTP " + res.status);
    return data;
  }

  function setText(el, text) {
    if (el && el.textContent !== text) el.textContent = text;
  }

  let focusShown = false;
  function showFocus(st) {
    if (focusShown || !st.focus) return;
    focusShown = true;
    let title = st.focus;
    document.querySelectorAll("section.ch").forEach((section) => {
      const marks = [...section.querySelectorAll("[data-step-mark]")].map((m) => m.dataset.stepMark);
      if (!marks.length) return;
      if (marks.includes(st.focus)) {
        const h = section.querySelector("h2");
        if (h) title = h.textContent.replace(/\s*✓ complete\s*$/, "").trim();
      } else {
        section.hidden = true;
      }
    });
    const banner = document.createElement("div");
    banner.className = "focus-banner";
    banner.style.cssText = "background:#eef4fb;border:1px solid #9cc0e6;border-radius:8px;padding:8px 12px;margin:10px 0;max-width:920px";
    banner.textContent = "Focus run: only the challenge \u201c" + title + "\u201d is part of this run; the page's other challenges are hidden.";
    const verdict = document.getElementById("bench-verdict");
    if (verdict && verdict.parentNode) verdict.parentNode.insertBefore(banner, verdict.nextSibling);
  }

  function render(st) {
    showFocus(st);
    setText(
      document.getElementById("bench-progress"),
      "Challenges complete: " + st.done.length + " / " + st.total +
        " | Ineffective interactions: " + st.futile + " | Surface hops: " + st.hops
    );
    const verdict = document.getElementById("bench-verdict");
    if (verdict) {
      let text = "Benchmark in progress.";
      let cls = "verdict";
      if (st.failed) {
        text = "BENCH_FAIL " + st.slug + ": " + st.failed;
        cls = "verdict fail";
      } else if (st.token) {
        text = st.token + " — all challenges complete";
        cls = "verdict pass";
      } else if (st.superseded) {
        text = "This session was replaced by a newer load of this page — continue in the newest tab.";
      }
      setText(verdict, text);
      if (verdict.className !== cls) verdict.className = cls;
    }
    document.querySelectorAll("[data-step-mark]").forEach((el) =>
      setText(el, st.done.includes(el.dataset.stepMark) ? "✓ complete" : "")
    );
    document.querySelectorAll("[data-unlock]").forEach((el) =>
      el.classList.toggle("locked", !st.done.includes(el.dataset.unlock))
    );
    document.querySelectorAll("[data-public]").forEach((el) => {
      const value = st.public[el.dataset.public];
      setText(el, value === undefined || value === null ? "" : String(value));
    });
    document.querySelectorAll("[data-msg]").forEach((el) => setText(el, st.messages[el.dataset.msg] || ""));
    if (st.failed) {
      try {
        sessionStorage.setItem("bench-fail-" + st.slug, st.failed);
      } catch (e) {
        /* storage unavailable */
      }
    }
  }

  function apply(st) {
    if (!st || !st.sid) return;
    if (state && st.sid === state.sid && st.rev === state.rev) return;
    state = st;
    render(st);
    listeners.forEach((fn) => {
      try {
        fn(st);
      } catch (e) {
        console.error(e);
      }
    });
  }

  async function start() {
    try {
      const st = await api("POST", "/v3/api/session", {
        page: PAGE, sid: sid || null, aux: !!AUX, focus: FOCUS || null, tag: TAG || null,
        perturb: PERTURB || null, seed: SEED ? Number(SEED) || 0 : null,
      });
      sid = st.sid;
      if (!AUX) sessionStorage.setItem(STORE, sid);
      apply(st);
      readyResolve(st);
      setInterval(async () => {
        try {
          apply(await api("GET", "/v3/api/state?sid=" + encodeURIComponent(sid)));
        } catch (e) {
          /* harness briefly unreachable; the next poll retries */
        }
      }, 700);
    } catch (e) {
      const verdict = document.getElementById("bench-verdict") || document.getElementById("aux-status");
      setText(verdict, "Harness unreachable: " + e.message);
    }
  }

  async function act(kind, data) {
    await ready;
    const out = await api("POST", "/v3/api/act", Object.assign({ sid, kind }, data || {}));
    apply(out.state);
    return out.result || {};
  }

  function beacon(kind, data) {
    if (!sid) return;
    try {
      navigator.sendBeacon("/v3/api/act", JSON.stringify(Object.assign({ sid, kind }, data || {})));
    } catch (e) {
      /* the page is going away; nothing else to do */
    }
  }

  function trusted(e) {
    return !!(e && e.isTrusted);
  }

  function untrusted(key) {
    return act("untrusted", { key: key || "" });
  }

  // Enter in a text field presses the button named by its data-enter attribute.
  document.addEventListener("keydown", (e) => {
    const target = e.target;
    if (e.key === "Enter" && target && target.dataset && target.dataset.enter) {
      const btn = document.getElementById(target.dataset.enter);
      if (btn) {
        e.preventDefault();
        btn.click();
      }
    }
  });

  window.V3 = {
    act,
    beacon,
    trusted,
    untrusted,
    ready,
    api,
    sid: () => sid,
    state: () => state,
    onState: (fn) => {
      listeners.push(fn);
      if (state) fn(state);
    },
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
