"""Work the external-brain mailbox (app/external_brain.py) from the brain's chair.

Run from apps/api, with BRAIN_DIR set to the mailbox (default storage/external_brain):

  python -m app.agent_eval.brain_seat wait [--timeout 1500]  # block until the next unanswered request, print it
  python -m app.agent_eval.brain_seat show 3 [--system]      # print step 3 (system text in full with --system)
  python -m app.agent_eval.brain_seat answer 3 '{...}'       # write the reply ('-' reads stdin, @file reads a file)
  python -m app.agent_eval.brain_seat schema 3               # print the JSON schema the harness requested
  python -m app.agent_eval.brain_seat note 3 "[intent] ... [missing] ..."  # live feedback line for the supervisor

`wait` prints the system text in full the first time it sees it (and again whenever it changes), then only its
hash; the prompt as compact JSON; and the absolute image paths to Read. Exit code 3 from `wait` = the run ended
(no new request before --timeout). Notes go to BRAIN_NOTES (default <mailbox>/notes): live.log for a supervisor's
Monitor, plus one log per BRAIN_TAG (else the mailbox folder's name).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from app.agent_eval.judge_seat import write_atomic
from app.prompt_cache import split_system_cache_prefix

REPO = Path(__file__).resolve().parents[4]
MAILBOX = Path(os.environ.get("BRAIN_DIR") or REPO / "storage" / "external_brain")
NOTES = Path(os.environ.get("BRAIN_NOTES") or MAILBOX / "notes")
SEEN = MAILBOX / ".seen_system_hashes"
# Lumen polls for the reply every 50 ms (external_brain._POLL_SECONDS); the brain's side polls as briskly, so a
# step loses no more than a tenth of a second to the mailbox in either direction.
POLL_SECONDS = 0.1


def _request(n: int) -> dict:
    return json.loads((MAILBOX / f"step-{n:04d}.request.json").read_text(encoding="utf-8"))


def print_step(n: int, force_system: bool = False) -> None:
    req = _request(n)
    # The rules (stable) and the per-step directives (after the boundary) are hashed apart, as transports
    # send them apart: a changed directive reprints only itself, never the ~40 KB of unchanged rules.
    system, directives = split_system_cache_prefix(req.get("system") or "")
    digest = hashlib.sha1(system.encode()).hexdigest()[:10]
    seen = set(SEEN.read_text().split()) if SEEN.exists() else set()
    print(f"== STEP {n}  purpose={req.get('purpose')}")
    # A brain's shell cuts tool output near 30 KB, and the rules alone are ~44 KB: the full rules go to a file
    # the brain reads once, and only the action cheat sheet (the part a reply is built from) is printed.
    system_file = MAILBOX / f"system-{digest}.txt"
    write_atomic(system_file, system, skip_if_same_size=True)
    if force_system or digest not in seen:
        print(f"== SYSTEM (sha {digest}, {len(system)} chars): read the full rules ONCE with your Read tool: {system_file.resolve()}")
        start = system.find("ACTION CHEAT SHEET")
        if start >= 0:
            end = system.find("\n\n", start)
            print(system[start:end if end > start else start + 2000])
        write_atomic(SEEN, " ".join(sorted(seen | {digest})))
    else:
        print(f"== SYSTEM unchanged (sha {digest}, {len(system)} chars) - same rules as {system_file.resolve()}")
    if directives.strip():
        print("== THIS STEP'S DIRECTIVES (change per step):")
        print(directives.strip())
    # ascii JSON escapes (\uXXXX) stay valid JSON on the ascii stdout below; backslashreplace would not
    prompt_json = json.dumps(req.get("prompt"), ensure_ascii=True, separators=(",", ":"))
    if len(prompt_json) > 20000:
        prompt_file = MAILBOX / f"step-{n:04d}.prompt.json"
        write_atomic(prompt_file, prompt_json)
        print(f"== PROMPT ({len(prompt_json)} chars, too long to print whole): read it with your Read tool: {prompt_file.resolve()}")
        print(prompt_json[:4000] + " ...")
    else:
        print("== PROMPT (compact JSON):")
        print(prompt_json)
    for name in req.get("images") or []:
        print(f"== IMAGE: {(MAILBOX / name).resolve()}")
    print(f"== Reply with: python -m app.agent_eval.brain_seat answer {n} '<json>'")


def pending() -> list[int]:
    return [
        int(path.name[5:9]) for path in sorted(MAILBOX.glob("step-*.request.json"))
        if not (MAILBOX / f"step-{int(path.name[5:9]):04d}.response.json").exists()
    ]


def wait(timeout: float) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        todo = pending()
        if todo:
            print_step(todo[0])
            return 0
        time.sleep(POLL_SECONDS)
    print(f"== no new request for {timeout:.0f}s (the run has probably ended)")
    return 3


def note(n: int, text: str) -> None:
    tag = os.environ.get("BRAIN_TAG") or MAILBOX.name
    line = " ".join(text.split())
    NOTES.mkdir(parents=True, exist_ok=True)
    with open(NOTES / "live.log", "a", encoding="utf-8") as fh:
        fh.write(f"[{tag} step {n}] {line}\n")
    with open(NOTES / f"{tag}.log", "a", encoding="utf-8") as fh:
        fh.write(f"step {n}: {line}\n")
    print("noted")


def answer(n: int, payload: str) -> None:
    if payload == "-":  # a heredoc on stdin: a brain without a file-writing tool replies this way
        payload = sys.stdin.buffer.read().decode("utf-8")
    elif payload.startswith("@"):
        payload = Path(payload[1:]).read_text(encoding="utf-8")
    def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
        keys = [key for key, _ in pairs]
        repeated = sorted({key for key in keys if keys.count(key) > 1})
        if repeated:  # json keeps the last one silently: a typo like two "y" for "to_y" would drop a field
            raise ValueError(f"duplicate JSON key(s) {repeated}: each key once (did you mean another field?)")
        return dict(pairs)

    json.loads(payload, object_pairs_hook=_no_duplicate_keys)  # fail here, not in the loop
    path = MAILBOX / f"step-{n:04d}.response.json"
    write_atomic(path, payload)
    print(f"answered step {n}")


def main(argv: list[str]) -> int:
    # The Bash tool reads stdout as cp1252: UTF-8 would arrive as mojibake; backslash escapes arrive intact.
    sys.stdout.reconfigure(encoding="ascii", errors="backslashreplace")
    cmd = argv[0]
    if cmd == "wait":
        return wait(float(argv[argv.index("--timeout") + 1]) if "--timeout" in argv else 1500.0)
    n = int(argv[1])
    if cmd == "show":
        print_step(n, "--system" in argv)
    elif cmd == "answer":
        answer(n, argv[2])
    elif cmd == "note":
        note(n, " ".join(argv[2:]))
    elif cmd == "schema":
        print(json.dumps(_request(n)["schema"], indent=1))
    else:
        raise SystemExit(f"unknown command {cmd!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
