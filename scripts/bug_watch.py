"""Watch real Lumen runs and note bug signatures to fix.

Polls the local API for runs on real sites (bench pages on localhost are skipped), reads each run's
new events, and appends one finding per (run, signature, subject) to storage/bug-watch/bugs.md and
bugs.jsonl. Each new finding is also printed as one line, so a monitor can stream it.

    python scripts/bug_watch.py            # poll forever
    python scripts/bug_watch.py --once     # one pass (testing)

Stdlib only, like scripts/lumen.py.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

API = os.environ.get("LUMEN_API", "http://127.0.0.1:8001").rstrip("/")
ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "storage" / "bug-watch"
STATE_PATH = OUT_DIR / "state.json"
MD_PATH = OUT_DIR / "bugs.md"
JSONL_PATH = OUT_DIR / "bugs.jsonl"

# How many times a refusal/failure on the same target counts as a loop worth fixing.
REPEAT_THRESHOLD = 3
SLOW_DECISION_MS = 45_000
TERMINAL_STATES = {"COMPLETED", "READY_FOR_REVIEW", "BLOCKED", "FAILED", "CANCELLED"}


def _get(path: str, timeout: float = 15.0):
    with urllib.request.urlopen(f"{API}{path}", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _real_site(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return bool(host) and host not in {"127.0.0.1", "localhost", "::1"} and not host.endswith(".localhost")


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"runs": {}, "seen": []}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")


def _short(value, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _findings(run: dict, events: list[dict], memo: dict) -> list[dict]:
    """Bug signatures in this batch of events. memo carries per-run counters across polls."""
    out: list[dict] = []
    refusals: Counter = Counter(memo.get("refusals") or {})
    asked: Counter = Counter(memo.get("asked") or {})

    def add(signature: str, subject: str, detail: str, at: str) -> None:
        out.append({"signature": signature, "subject": _short(subject, 120), "detail": _short(detail, 400), "at": at})

    for event in events:
        kind = event.get("event_type") or ""
        payload = event.get("event_payload") or {}
        at = str(event.get("timestamp") or "")[:19]
        if kind == "VISION_TARGET_BINDING_VERDICT" and payload.get("matches") is False:
            key = f"binding:{payload.get('selected_control')}"
            refusals[key] += 1
            if refusals[key] == REPEAT_THRESHOLD:
                add(
                    "target_binding_refused_repeatedly",
                    str(payload.get("question") or ""),
                    f"selected {payload.get('selected_control')!r}, expected {payload.get('expected_control')!r}: {payload.get('reason')}",
                    at,
                )
        elif kind == "ACTION_RECEIPT":
            verification = payload.get("verification") or {}
            reason = verification.get("failure_reason")
            if (payload.get("execution") or {}).get("status") == "failed" and reason:
                label = ((payload.get("target") or {}).get("label")) or ""
                key = f"exec:{reason}"
                refusals[key] += 1
                if refusals[key] == REPEAT_THRESHOLD:
                    add(
                        "action_failed_repeatedly",
                        reason,
                        f"{verification.get('body_error') or ''} mismatch={verification.get('target_identity_mismatch_field') or ''} last: {label}",
                        at,
                    )
        elif kind == "VISION_PAGE_STATE_CYCLE" and int(payload.get("visits") or 0) == REPEAT_THRESHOLD:
            # One per run: each loop gets its own fingerprint, and the first says enough to look.
            add("page_state_cycle", "", f"the page returned to the same state without progress ({payload.get('state_fingerprint')})", at)
        elif kind == "VISION_MODEL_REPLY_CUT_OFF":
            add(
                "brain_reply_cut_off",
                str(payload.get("upstream_provider") or payload.get("model") or ""),
                f"{payload.get('completion_tokens')} tokens, {payload.get('latency_ms')} ms, retry={payload.get('retry_outcome')}",
                at,
            )
        elif kind == "VISION_BRAIN_ESCALATED":
            add("brain_escalated", str(payload.get("reason") or ""), f"{payload.get('from')} -> {payload.get('to')}", at)
        elif kind == "STEP_TIMING" and int(payload.get("decide_ms") or 0) >= SLOW_DECISION_MS:
            add("slow_decision", "", f"first: step {payload.get('step')} decide {payload.get('decide_ms')} ms, outcome {payload.get('outcome')}", at)
        elif kind == "VISION_ASK_PARK_ENDED" and payload.get("outcome") == "expired":
            add("ask_wait_expired", "", f"waited {payload.get('waited_seconds')} s; the loop ended and a reply must resume it", at)
        elif kind == "OPERATOR_ANSWER_APPLIED":
            applied = payload.get("applied") or {}
            if applied.get("can_resume") is False:
                add("answer_did_not_resume", str(applied.get("target") or ""), f"question {payload.get('question_id')} applied without resuming", at)
        elif kind == "ANSWER_MODES":
            label = str(payload.get("label") or "")
            asked[label] += 1
        elif kind == "OPERATOR_QUESTION_ASKED":
            reason = str(payload.get("stuck_reason") or "")
            if payload.get("question_type") == "blocked_reason":
                add("generic_stuck_card", reason, "fell back to a blocked_reason card instead of a field question", at)

    for label, count in asked.items():
        previous = int((memo.get("asked") or {}).get(label) or 0)
        # ANSWER_MODES fires twice per ask (publish + answer); 3+ is the same question asked again.
        if previous < 3 <= count:
            add("question_reasked", label, f"asked {count // 2 + count % 2} times in this run", "")

    state = str(run.get("state") or "")
    if state in {"BLOCKED", "FAILED"} and memo.get("final_state") != state:
        add("run_stopped", state, str(run.get("status") or ""), "")
        if "internal/provider failure" in str(run.get("status") or "").lower():
            add("misleading_stop_message", state, "stop message blames a provider failure; check whether it was a harness refusal", "")

    memo["refusals"] = dict(refusals)
    memo["asked"] = dict(asked)
    memo["final_state"] = state if state in TERMINAL_STATES else ""
    return out


def _append(run: dict, findings: list[dict], state: dict) -> list[dict]:
    seen = set(state.get("seen") or [])
    fresh = []
    for finding in findings:
        key = f"{run['id']}|{finding['signature']}|{finding['subject']}"
        if key in seen:
            continue
        seen.add(key)
        fresh.append({"run_id": run["id"], "url": run.get("url"), **finding})
    state["seen"] = sorted(seen)[-5000:]
    if not fresh:
        return []
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not MD_PATH.exists():
        MD_PATH.write_text("# Lumen bug watch\n\nOne line per bug signature seen on a real run. Triage notes go under each entry.\n\n", encoding="utf-8")
    with MD_PATH.open("a", encoding="utf-8") as md, JSONL_PATH.open("a", encoding="utf-8") as jl:
        for item in fresh:
            noted = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            md.write(
                f"- [ ] **{item['signature']}** run {item['run_id']} ({urlparse(item['url'] or '').hostname}) "
                f"{item['at'] or noted}: {item['subject']} — {item['detail']}\n"
            )
            jl.write(json.dumps({**item, "noted_at": noted}) + "\n")
    return fresh


def poll_once(state: dict) -> list[dict]:
    runs = _get("/operator/runs")
    runs = runs.get("items", runs) if isinstance(runs, dict) else runs
    new: list[dict] = []
    for raw in runs[:15]:
        run = {
            "id": raw.get("id"),
            "state": raw.get("current_state") or raw.get("state"),
            "status": raw.get("status_message") or raw.get("status"),
            "url": raw.get("target_url") or raw.get("url"),
        }
        if not run["id"] or not _real_site(run["url"] or ""):
            continue
        memo = state["runs"].setdefault(str(run["id"]), {"last_event_id": 0})
        if memo.get("done"):
            continue
        events = _get(f"/operator/runs/{run['id']}/events", timeout=30)
        events = events if isinstance(events, list) else events.get("events", [])
        last = int(memo.get("last_event_id") or 0)
        batch = [event for event in events if int(event.get("id") or 0) > last]
        if batch:
            memo["last_event_id"] = max(int(event.get("id") or 0) for event in batch)
        new.extend(_append(run, _findings(run, batch, memo), state))
        if run["state"] in TERMINAL_STATES:
            memo["done"] = True
    return new


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=20.0)
    parser.add_argument("--since-run", type=int, default=0, help="ignore runs with a lower id on first start")
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    state = _load_state()
    if args.since_run:
        for run_id in range(1, args.since_run):
            state["runs"].setdefault(str(run_id), {"done": True})
    while True:
        try:
            for item in poll_once(state):
                print(
                    f"BUG {item['signature']} run {item['run_id']}: {item['subject']} — {item['detail']}",
                    flush=True,
                )
            _save_state(state)
        except Exception as exc:  # noqa: BLE001 - the API restarting must not end the watch
            print(f"WATCH_ERROR {type(exc).__name__}: {_short(exc, 200)}", flush=True)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
