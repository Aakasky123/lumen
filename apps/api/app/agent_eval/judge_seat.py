"""Work the external-judge mailbox (app/external_judge.py) from the judge's chair.

Run from apps/api, with JUDGE_DIR set to the judge mailbox root (default storage/external_judge). The judge
seat must be a different subagent from the brain: never read the brain mailbox, its notes, or its history.

  python -m app.agent_eval.judge_seat wait [--timeout 1500]   # block until a request is pending, then print ALL pending
  python -m app.agent_eval.judge_seat list                    # pending request ids with purpose and seconds left
  python -m app.agent_eval.judge_seat show 12/0003            # print one request again (system in full with --system)
  python -m app.agent_eval.judge_seat answer 12/0003 '{...}'  # write the verdict ('-' reads stdin, @file a file)
  python -m app.agent_eval.judge_seat schema 12/0003          # the JSON schema the harness requested

A request id is <run>/<step> for a run mailbox (<root>/run-<run>/step-<step>) or just <step> for the root.
Several requests can be pending at once (overlapped gates, parallel runs): `wait` prints every one of them and
any may be answered in any order. Each request carries its own deadline_seconds; a late or malformed reply is
treated by Lumen as "no verdict" (fail closed), so when unsure answer the safe verdict the system text names, or
do not answer. Long system texts and prompts go to files next to the request (read them once with Read); the
system text is printed in full only the first time a hash is seen. Polls every 0.1 s.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
ROOT = Path(os.environ.get("JUDGE_DIR") or REPO / "storage" / "external_judge")
SEEN = ROOT / ".seen_system_hashes"
POLL_SECONDS = 0.1
PRINT_LIMIT = 6000


def write_atomic(path: Path, text: str, *, skip_if_same_size: bool = False) -> None:
    """Write via a per-process tmp file + os.replace, so a concurrent reader never sees a partial file.

    skip_if_same_size is for content-addressed files (system-<sha>.txt): one that already exists with the same
    byte size is left alone, never rewritten."""
    data = text.encode("utf-8")
    if skip_if_same_size:
        try:
            if path.stat().st_size == len(data):
                return
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    # Windows: a reader holding the target makes os.replace raise PermissionError for a moment.
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(0.05)


def _boxes() -> list[tuple[str, Path]]:
    boxes = [("", ROOT)] if ROOT.exists() else []
    boxes += [(path.name[4:], path) for path in sorted(ROOT.glob("run-*")) if path.is_dir()]
    return boxes


def _locate(ident: str) -> tuple[Path, int]:
    run, _, step = ident.rpartition("/")
    return (ROOT / f"run-{run}" if run else ROOT), int(step)


def _request_path(box: Path, n: int) -> Path:
    return box / f"step-{n:04d}.request.json"


def pending() -> list[tuple[str, Path, int]]:
    found = []
    for run, box in _boxes():
        for path in sorted(box.glob("step-*.request.json")):
            n = int(path.name[5:9])
            if not (box / f"step-{n:04d}.response.json").exists():
                found.append((f"{run}/{n:04d}" if run else f"{n:04d}", box, n))
    return found


def _clip(text: str, box: Path, name: str) -> str:
    if len(text) <= PRINT_LIMIT:
        return text
    path = box / name
    write_atomic(path, text, skip_if_same_size=name.startswith("system-"))
    return f"{text[:1500]} ... [{len(text)} chars; full text: read {path.resolve()}]"


def print_request(ident: str, box: Path, n: int, force_system: bool = False) -> None:
    req = json.loads(_request_path(box, n).read_text(encoding="utf-8"))
    system = req.get("system") or ""
    digest = hashlib.sha1(system.encode()).hexdigest()[:10]
    seen = set(SEEN.read_text().split()) if SEEN.exists() else set()
    left = float(req.get("deadline_seconds") or 0) - (time.time() - _request_path(box, n).stat().st_mtime)
    print(f"== REQUEST {ident}  purpose={req.get('purpose')}  deadline={req.get('deadline_seconds')}s ({max(left, 0):.0f}s left)")
    if force_system or digest not in seen:
        print(f"== SYSTEM (sha {digest}): {_clip(system, box, f'system-{digest}.txt')}")
        SEEN.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(SEEN, " ".join(sorted(seen | {digest})))
    else:
        print(f"== SYSTEM unchanged (sha {digest}, {len(system)} chars; shown earlier, or read {box.resolve() / f'system-{digest}.txt'})")
        path = box / f"system-{digest}.txt"
        write_atomic(path, system, skip_if_same_size=True)
    prompt_json = json.dumps(req.get("prompt"), ensure_ascii=True, separators=(",", ":"))
    print(f"== PROMPT: {_clip(prompt_json, box, f'step-{n:04d}.prompt.json')}")
    schema_json = json.dumps(req.get("schema"), ensure_ascii=True, separators=(",", ":"))
    print(f"== SCHEMA: {_clip(schema_json, box, f'step-{n:04d}.schema.json')}")
    for name in req.get("images") or []:
        print(f"== IMAGE: {(box / name).resolve()}")
    print(f"== Reply with: python -m app.agent_eval.judge_seat answer {ident} '<json>'")


def wait(timeout: float) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        todo = pending()
        if todo:
            print(f"== {len(todo)} pending: {', '.join(i for i, _, _ in todo)}")
            for ident, box, n in todo:
                print_request(ident, box, n)
            return 0
        time.sleep(POLL_SECONDS)
    print(f"== no request for {timeout:.0f}s (the run has probably ended)")
    return 3


def answer(ident: str, payload: str) -> None:
    box, n = _locate(ident)
    if payload == "-":
        payload = sys.stdin.buffer.read().decode("utf-8")
    elif payload.startswith("@"):
        payload = Path(payload[1:]).read_text(encoding="utf-8")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise SystemExit("a verdict is one JSON object")
    path = box / f"step-{n:04d}.response.json"
    write_atomic(path, payload)
    print(f"answered {ident}")


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="ascii", errors="backslashreplace")
    cmd = argv[0] if argv else "wait"
    if cmd == "wait":
        return wait(float(argv[argv.index("--timeout") + 1]) if "--timeout" in argv else 1500.0)
    if cmd == "list":
        for ident, box, n in pending():
            req = json.loads(_request_path(box, n).read_text(encoding="utf-8"))
            print(f"{ident}  {req.get('purpose')}  deadline={req.get('deadline_seconds')}s")
        return 0
    ident = argv[1]
    box, n = _locate(ident)
    if cmd == "show":
        print_request(ident, box, n, "--system" in argv)
    elif cmd == "answer":
        answer(ident, argv[2])
    elif cmd == "schema":
        print(json.dumps(json.loads(_request_path(box, n).read_text(encoding="utf-8"))["schema"], indent=1))
    else:
        raise SystemExit(f"unknown command {cmd!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
