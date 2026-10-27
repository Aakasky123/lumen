#!/usr/bin/env python3
"""Lumen CLI - drive the Lumen backend from a terminal or an agent.

Thin wrapper over the local HTTP API (default http://127.0.0.1:8001, override
with LUMEN_API). Stdlib only, JSON-friendly output. Exit code 0 on success.

Common flows:
  lumen status                          # backend + extension body + model health
  lumen apply <job-url> --auto          # set autonomous mode and start an apply run
  lumen watch <run-id> --auto-resume 4  # follow a run, auto-resume transient blocks
  lumen questions <run-id>              # pending question cards
  lumen answer <question-id> "text"     # answer and auto-resume (--remember keeps it for later runs)
  lumen chat "message" --thread <id>    # any chat turn (streams the reply)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("LUMEN_API", "http://127.0.0.1:8001").rstrip("/")

TRANSIENT_BLOCK_REASONS = ("vision_model_timeout",)


def _request(method: str, path: str, body: dict | None = None, timeout: float = 15.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code} {method} {path}: {detail[:400]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach Lumen backend at {API}: {exc.reason}") from exc


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _run_brief(run: dict) -> dict:
    return {
        "id": run.get("id"),
        "state": run.get("current_state"),
        "status": run.get("status_message"),
        "url": run.get("target_url"),
    }


def cmd_status(args) -> None:
    health = _request("GET", "/health")
    extension = _request("GET", "/operator/extension/status")
    settings = _request("GET", "/agent-settings")
    mode = health.get("mode", {})
    out = {
        "backend": health.get("status"),
        "model_mode": mode.get("model_mode"),
        "final_action_mode": settings.get("settings", {}).get("final_action_mode"),
        "extension_connected": extension.get("connected"),
        "live_actions_ready": extension.get("live_actions_ready"),
        "devices": extension.get("devices"),
    }
    _print(out)


def cmd_settings(args) -> None:
    if args.final_action_mode:
        result = _request("PATCH", "/agent-settings", {"final_action_mode": args.final_action_mode})
    else:
        result = _request("GET", "/agent-settings")
    _print(result.get("settings", result))


def _stream_chat(thread_id: int, content: str, quiet_tokens: bool = False) -> dict:
    """POST a chat message and consume the SSE stream. Returns collected metadata."""
    req = urllib.request.Request(
        f"{API}/operator/chat/threads/{thread_id}/messages",
        data=json.dumps({"content": content}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    collected: dict = {"thread_id": thread_id, "assistant_text": "", "run_id": None}
    with urllib.request.urlopen(req, timeout=180) as resp:
        event = None
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if event == "meta":
                    app_req = data.get("application_request") or {}
                    if app_req.get("run_id"):
                        collected["run_id"] = app_req["run_id"]
                elif event == "token":
                    text = data.get("text") or data.get("token") or ""
                    collected["assistant_text"] += text
                    if not quiet_tokens:
                        sys.stdout.write(text)
                        sys.stdout.flush()
                elif event == "done":
                    message = data.get("assistant_message") or {}
                    if message.get("content"):
                        collected["assistant_text"] = message["content"]
                    run_id = (data.get("application_request") or {}).get("run_id")
                    if run_id:
                        collected["run_id"] = run_id
    if not quiet_tokens:
        sys.stdout.write("\n")
    if collected["run_id"] is None:
        # Deferred launches register the run just after the stream closes.
        time.sleep(2)
        runs = _request("GET", "/operator/runs")
        if runs and content.lower().startswith("apply"):
            latest = runs[0]
            if latest.get("target_url") and latest["target_url"] in content:
                collected["run_id"] = latest["id"]
    return collected


def cmd_apply(args) -> None:
    if args.auto:
        _request("PATCH", "/agent-settings", {"final_action_mode": "autonomous"})
    elif args.approval:
        _request("PATCH", "/agent-settings", {"final_action_mode": "approval"})
    thread = _request("POST", "/operator/chat/threads", {"title": f"CLI apply {args.url[:60]}"})
    result = _stream_chat(thread["id"], f"apply {args.url}", quiet_tokens=args.json)
    out = {"thread_id": thread["id"], "run_id": result["run_id"]}
    if args.json:
        out["assistant_text"] = result["assistant_text"]
        _print(out)
    else:
        print(f"thread={thread['id']} run={result['run_id']}")
    if args.watch and result["run_id"]:
        args.run_id = result["run_id"]
        args.auto_resume = args.auto_resume if hasattr(args, "auto_resume") else 4
        args.interval = 15
        args.timeout = 60
        cmd_watch(args)


def cmd_chat(args) -> None:
    thread_id = args.thread
    if thread_id is None:
        thread = _request("POST", "/operator/chat/threads", {"title": "CLI chat"})
        thread_id = thread["id"]
    result = _stream_chat(thread_id, args.message, quiet_tokens=args.json)
    if args.json:
        _print(result)


def cmd_runs(args) -> None:
    runs = _request("GET", "/operator/runs")
    _print([_run_brief(r) for r in runs[: args.limit]])


def cmd_run(args) -> None:
    run = _request("GET", f"/operator/runs/{args.run_id}")
    _print(_run_brief(run) if not args.full else run)


def cmd_events(args) -> None:
    events = _request("GET", f"/operator/runs/{args.run_id}/events")
    for event in events[-args.limit:]:
        payload = json.dumps(event.get("event_payload", {}), default=str)
        print(f"{event.get('timestamp', '')} {event['event_type']} {payload[:200]}")


def cmd_watch(args) -> None:
    """Poll a run until it reaches a state needing outside input, printing transitions.

    With --auto-resume N, transient engineering blocks (vision model timeouts)
    are resumed automatically up to N times.
    """
    resumes = 0
    last_line = ""
    deadline = time.time() + args.timeout * 60
    while time.time() < deadline:
        run = _request("GET", f"/operator/runs/{args.run_id}")
        state = run.get("current_state")
        msg = run.get("status_message") or ""
        line = f"{state} | {msg}"
        if line != last_line:
            print(line, flush=True)
            last_line = line
        if state == "BLOCKED" and any(reason in msg for reason in TRANSIENT_BLOCK_REASONS) and resumes < args.auto_resume:
            resumes += 1
            print(f"[auto-resume {resumes}/{args.auto_resume}]", flush=True)
            _request("POST", f"/operator/runs/{args.run_id}/resume")
            time.sleep(args.interval)
            continue
        if state not in ("RUNNING", "CREATED"):
            questions = _request("GET", f"/operator/runs/{args.run_id}/questions")
            pending = [q for q in questions if str(q.get("status", "")).upper() in ("PENDING", "OPEN", "ASKED")]
            if pending:
                print("pending questions:")
                for q in pending:
                    print(f"  id={q['id']} {q.get('question_text') or q.get('prompt') or ''}")
            raise SystemExit(0 if state in ("COMPLETED", "READY_FOR_REVIEW", "WAITING_FOR_USER") else 1)
        time.sleep(args.interval)
    print("watch window elapsed; run still active", flush=True)


def cmd_questions(args) -> None:
    questions = _request("GET", f"/operator/runs/{args.run_id}/questions")
    _print(questions)


def cmd_answer(args) -> None:
    result = _request(
        "POST",
        f"/operator/questions/{args.question_id}/answer",
        {"answer": args.answer, "auto_resume": not args.no_resume, **({"save_scope": "permanent"} if args.remember else {})},
        timeout=60,
    )
    _print(
        {
            "applied": result.get("applied"),
            "resolved_value": result.get("resolved_value"),
            "can_resume": result.get("can_resume"),
            "run": _run_brief(result.get("run", {})),
        }
    )


def _simple_run_action(action: str):
    def handler(args) -> None:
        run = _request("POST", f"/operator/runs/{args.run_id}/{action}")
        _print(_run_brief(run))

    return handler


def main() -> None:
    parser = argparse.ArgumentParser(prog="lumen", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="backend + extension + mode summary").set_defaults(func=cmd_status)

    p = sub.add_parser("settings", help="show or change agent settings")
    p.add_argument("--final-action-mode", choices=["approval", "autonomous"])
    p.set_defaults(func=cmd_settings)

    p = sub.add_parser("apply", help="start an application run for a job URL via chat")
    p.add_argument("url")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--auto", action="store_true", help="set final-action mode to autonomous first")
    mode.add_argument("--approval", action="store_true", help="set final-action mode to approval first")
    p.add_argument("--watch", action="store_true", help="follow the run after starting it")
    p.add_argument("--auto-resume", type=int, default=4)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("chat", help="send a chat message (streams the reply)")
    p.add_argument("message")
    p.add_argument("--thread", type=int, help="existing thread id (default: new thread)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("runs", help="list recent runs")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("run", help="show one run")
    p.add_argument("run_id", type=int)
    p.add_argument("--full", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("events", help="tail run events")
    p.add_argument("run_id", type=int)
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("watch", help="follow a run until it needs input or finishes")
    p.add_argument("run_id", type=int)
    p.add_argument("--auto-resume", type=int, default=0, help="auto-resume transient blocks up to N times")
    p.add_argument("--interval", type=int, default=15, help="poll seconds")
    p.add_argument("--timeout", type=int, default=60, help="watch window in minutes")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("questions", help="list a run's question cards")
    p.add_argument("run_id", type=int)
    p.set_defaults(func=cmd_questions)

    p = sub.add_parser("answer", help="answer a pending question")
    p.add_argument("question_id", type=int)
    p.add_argument("answer")
    p.add_argument("--no-resume", action="store_true", help="do not auto-resume the run")
    p.add_argument("--remember", action="store_true", help="keep and remember the answer (like the card's Keep & remember)")
    p.set_defaults(func=cmd_answer)

    for action, help_text in (("resume", "resume a paused/blocked run"), ("pause", "pause a run"), ("cancel", "cancel a run")):
        p = sub.add_parser(action, help=help_text)
        p.add_argument("run_id", type=int)
        p.set_defaults(func=_simple_run_action(action))

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
