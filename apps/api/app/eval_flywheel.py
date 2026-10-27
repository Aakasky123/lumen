"""Failure -> eval-case flywheel: every blocked vision run becomes a reviewable candidate.

The eval harness (app/agent_eval) replays hand-curated capability snapshots; until now the
only way a real failure became a regression case was a human re-assembling artifacts from
the run database by hand. This module closes that gap:

  * capture_failure_candidate — called from the vision loop's terminal path for BLOCKED
    runs. Writes one JSON candidate per run under <artifact_root>/eval_candidates/, shaped
    around the same `snapshot` block that CapabilityRegressionSpec.snapshot_failures
    validates, so promoting a candidate is a review-and-sanitize step, not a rebuild.
  * export_correction_cases — snapshots the ANSWER_CORRECTION flywheel (Keep/Change on
    confirm-asks) into the same candidates directory as answer-quality case specs.
  * list_candidates / CLI — the review queue. Promotion into CAPABILITY_REGRESSIONS or
    ANSWER_CASES stays a human decision (runtime code never edits the test suite).

Candidates are local artifacts, not committed fixtures: they may retain user-derived
values (beyond OTP redaction) and must be sanitized by a human before entering the repo.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app import models
from app.answer_corrections import correction_to_case, load_corrections
from app.config import get_settings
from app.event_store import emit_event
from app.execution_logging import run_failure_evidence
from app.security import redact_otp_codes

CANDIDATE_SCHEMA_VERSION = "lumen_eval_candidate_v1"
_CANDIDATE_RECEIPT_LIMIT = 80


def candidates_dir() -> Path:
    directory = get_settings().artifact_root / "eval_candidates"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _host(url: str) -> str:
    try:
        host = urlparse(str(url or "")).hostname or ""
    except ValueError:
        host = ""
    return host.removeprefix("www.")


def _receipt_payloads(vision_action_receipts: Any) -> list[dict[str, Any]]:
    """Unwrap {id, event_type, payload} envelopes down to the receipt payloads the
    capability-spec validator matches on (type / outcome / verification)."""
    receipts: list[dict[str, Any]] = []
    for item in vision_action_receipts if isinstance(vision_action_receipts, list) else []:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
        receipts.append(payload)
        if len(receipts) >= _CANDIDATE_RECEIPT_LIMIT:
            break
    return receipts


def capture_failure_candidate(
    db: Any,
    run_id: int,
    summary: dict[str, Any],
) -> dict[str, Any] | None:
    """Record one blocked run as an eval-candidate file; never raises into the loop."""
    run = db.get(models.OperatorRun, int(run_id))
    if run is None:
        return None
    summary = dict(summary or {})
    export = run_failure_evidence(db, run)
    observed_at = datetime.now(timezone.utc).isoformat()
    reason = str(summary.get("reason") or "")
    candidate = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "candidate_kind": "blocked_run_failure",
        "provenance": {
            "run_id": int(run_id),
            "surface": "vision_loop",
            "host": _host(run.target_url),
            "target_url": str(run.target_url or ""),
            "observed_at": observed_at,
            "captured_from_local_run_database": True,
            "promotion_requires_human_review": True,
        },
        "privacy": {
            "sanitized": False,
            "otp_codes_redacted": True,
            "may_contain_user_derived_values": True,
        },
        # Mirrors the `snapshot` block CapabilityRegressionSpec.snapshot_failures reads.
        # spec_id stays empty until a human writes the spec this candidate should satisfy
        # (or refute); verified_success is False by construction for a blocked run.
        "snapshot": {
            "spec_id": "",
            "body_mode": "real_chrome",
            "final_state": str(summary.get("state") or ""),
            "verified_success": False,
            "verified_success_criteria": [],
            "forbidden_side_effects_observed": False,
            "receipts": _receipt_payloads(export.get("vision_action_receipts")),
            "failure_artifacts": [item for item in (reason,) if item],
        },
        "failure": {
            "reason": reason,
            "block_kind": str(summary.get("block_kind") or ""),
            "model_failure_reason": str(summary.get("model_failure_reason") or ""),
            "error": str(summary.get("error") or "")[:2000],
            "actions": summary.get("actions"),
            "latest_observation_path": export.get("latest_observation_path"),
            "status_message": export.get("status_message"),
            "run_state": export.get("run_state"),
        },
        "loop_evidence": {
            "vision_loop_event_counts": export.get("vision_loop_event_counts"),
            "vision_timings": export.get("vision_timings"),
            "timing_summary": summary.get("timing_summary"),
        },
    }
    path = candidates_dir() / f"run_{int(run_id)}.json"
    path.write_text(
        json.dumps(redact_otp_codes(candidate), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    try:
        emit_event(
            db,
            event_type="EVAL_CANDIDATE_RECORDED",
            run_id=int(run_id),
            actor="eval_flywheel",
            payload={
                "candidate_kind": "blocked_run_failure",
                "path": str(path),
                "final_state": candidate["snapshot"]["final_state"],
                "reason": reason,
            },
        )
        db.commit()
    except Exception:  # noqa: BLE001 - observability must never break capture
        db.rollback()
    return {"path": str(path), "run_id": int(run_id), "reason": reason}


def export_correction_cases(db: Any, *, user_id: int | None = None) -> dict[str, Any]:
    """Write the harvested ANSWER_CORRECTION cases to the candidates directory.

    Same dedupe rule as agent_eval.answer_quality.harvested_cases: the latest correction
    for a field key wins.
    """
    cases: dict[str, dict[str, Any]] = {}
    for record in load_corrections(db, user_id=user_id):
        spec = correction_to_case(record)
        if spec:
            cases[str(spec.get("id"))] = spec
    payload = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "candidate_kind": "answer_correction_cases",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "case_count": len(cases),
        "cases": list(cases.values()),
    }
    path = candidates_dir() / "correction_cases.json"
    path.write_text(
        json.dumps(redact_otp_codes(payload), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return {"path": str(path), "case_count": len(cases)}


def list_candidates() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(candidates_dir().glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            entries.append({"file": path.name, "error": "unreadable"})
            continue
        kind = str(data.get("candidate_kind") or "")
        entry: dict[str, Any] = {"file": path.name, "candidate_kind": kind}
        if kind == "blocked_run_failure":
            entry.update(
                {
                    "run_id": (data.get("provenance") or {}).get("run_id"),
                    "host": (data.get("provenance") or {}).get("host"),
                    "final_state": (data.get("snapshot") or {}).get("final_state"),
                    "reason": (data.get("failure") or {}).get("reason"),
                    "observed_at": (data.get("provenance") or {}).get("observed_at"),
                }
            )
        elif kind == "answer_correction_cases":
            entry.update(
                {
                    "case_count": data.get("case_count"),
                    "exported_at": data.get("exported_at"),
                }
            )
        entries.append(entry)
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Failure -> eval-case flywheel: list candidates, export correction cases."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List captured eval candidates awaiting human review.")
    export = sub.add_parser(
        "export-corrections",
        help="Export harvested ANSWER_CORRECTION cases to the candidates directory.",
    )
    export.add_argument("--user-id", type=int, default=None)
    args = parser.parse_args()

    if args.command == "list":
        entries = list_candidates()
        if not entries:
            print(f"No eval candidates in {candidates_dir()}")
            return
        for entry in entries:
            print(json.dumps(entry, ensure_ascii=False))
        return

    if args.command == "export-corrections":
        from app.db import SessionLocal, init_db

        init_db()
        with SessionLocal() as db:
            result = export_correction_cases(db, user_id=args.user_id)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
