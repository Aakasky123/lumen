"""Replay retained vision model inputs against candidate providers and score them.

Every vision decision retains its exact model input (system + prompt + schema + screenshot)
under <artifact_root>/runs/<run>/vision-trajectory/. This module turns that retention into a
candidate-model bake-off: replay a stratified sample of real decisions through the production
``call_model_json`` transport for each candidate and score

  * structured validity  — ``vision_agent.normalize_action`` accepts the completion;
  * latency              — wall-clock per decision (p50 / p95);
  * champion agreement   — action, control label, and click-coordinate distance versus the
                           decision the configured champion actually executed in the run.

Replaying the champion itself measures the self-agreement noise floor that candidate
agreement must be read against. Spends tokens only when invoked — never wired into the loop.

Usage (from apps/api):
  python -m app.agent_eval.model_replay --sample 40 --days 14 \
      --candidate google/gemini-3.1-flash-lite \
      --candidate qwen/qwen3.7-flash:json_object --include-champion

A candidate suffixed ``:json_object`` replays without the strict json_schema response format,
mirroring how non-structured providers run in production (deterministic validation + re-decide
still applies). ``model@effort`` (e.g. ``openai/gpt-6-luna@low``) overrides the base provider's
reasoning effort for that leg, so effort tiers can be baked off like models.
Results append to <artifact_root>/eval_scores/model_replay/<timestamp>.jsonl.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CHAMPION_LABEL = "champion"
DEFAULT_PER_RUN_CAP = 2
DEFAULT_CLICK_SHARE_CAP = 3  # clicks may fill at most 1/3 of the sample beyond a floor of 6


def normalize_label(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def collect_pool(db: Any, *, days: int, champion_model: str) -> list[dict[str, Any]]:
    """Champion decisions with retained inputs, newest first, actionable for replay."""
    from sqlalchemy import text as sql_text

    rows = db.execute(
        sql_text(
            "SELECT run_id, event_payload FROM cognitive_events "
            "WHERE event_type='VISION_AGENT_DECISION' "
            "AND timestamp > datetime('now', :window) ORDER BY id DESC"
        ),
        {"window": f"-{max(1, int(days))} day"},
    ).fetchall()
    pool: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row[1])
        except (TypeError, ValueError):
            continue
        metadata = payload.get("model_metadata") or {}
        if str(metadata.get("model") or "") != champion_model:
            continue
        if payload.get("outcome_type") != "valid_action":
            continue
        ref = payload.get("model_input_ref") or {}
        input_path = str(ref.get("model_input_path") or "")
        screenshot_path = str(ref.get("model_screenshot_path") or "")
        if not (input_path and screenshot_path):
            continue
        if not (Path(input_path).is_file() and Path(screenshot_path).is_file()):
            continue
        pool.append(
            {
                "run_id": int(row[0] or 0),
                "step": payload.get("step"),
                "url": payload.get("url"),
                "input": input_path,
                "shot": screenshot_path,
                "action": payload.get("action"),
                "control": payload.get("control"),
                "x": payload.get("x"),
                "y": payload.get("y"),
            }
        )
    return pool


def stratified_sample(
    pool: list[dict[str, Any]],
    *,
    target: int,
    per_run_cap: int = DEFAULT_PER_RUN_CAP,
) -> list[dict[str, Any]]:
    """Cap per-run share and stop clicks from crowding out rarer action kinds."""
    per_run: dict[int, int] = defaultdict(int)
    per_action: dict[str, int] = defaultdict(int)
    click_cap = max(6, target // DEFAULT_CLICK_SHARE_CAP)
    sample: list[dict[str, Any]] = []
    for item in pool:
        if per_run[item["run_id"]] >= per_run_cap:
            continue
        action = str(item.get("action") or "?")
        if action == "click" and per_action[action] >= click_cap:
            continue
        per_run[item["run_id"]] += 1
        per_action[action] += 1
        sample.append(item)
        if len(sample) >= target:
            break
    return sample


def score_against_recorded(recorded: dict[str, Any], normalized: dict[str, Any]) -> dict[str, Any]:
    """Agreement of one candidate decision with the champion's executed decision."""
    score: dict[str, Any] = {
        "action": normalized.get("action"),
        "control": normalized.get("control"),
        "action_match": normalized.get("action") == recorded.get("action"),
    }
    recorded_control = normalize_label(recorded.get("control"))
    candidate_control = normalize_label(normalized.get("control"))
    score["control_match"] = (
        (recorded_control == candidate_control) if (recorded_control or candidate_control) else None
    )
    try:
        if recorded.get("x") is not None and normalized.get("x") is not None:
            dx = float(normalized.get("x") or 0) - float(recorded.get("x") or 0)
            dy = float(normalized.get("y") or 0) - float(recorded.get("y") or 0)
            score["coord_delta_px"] = round((dx * dx + dy * dy) ** 0.5, 1)
    except (TypeError, ValueError):
        pass
    return score


def _replay_one(item: dict[str, Any], label: str, provider: Any, strict_schema: bool) -> dict[str, Any]:
    from app import vision_agent
    from app.model_provider import call_model_json

    record = json.loads(Path(item["input"]).read_text(encoding="utf-8"))
    envelope = (record.get("model_input") or {}).get("envelope") or {}
    image = {
        "media_type": "image/jpeg",
        "data": base64.b64encode(Path(item["shot"]).read_bytes()).decode("ascii"),
    }
    row: dict[str, Any] = {
        "model": label,
        "run_id": item["run_id"],
        "step": item.get("step"),
        "url": item.get("url"),
        "recorded_action": item.get("action"),
        "strict_schema": strict_schema,
    }
    started = time.perf_counter()
    try:
        result = call_model_json(
            provider,
            purpose="agent_eval_model_replay",
            system=envelope.get("system") or "",
            prompt=envelope.get("prompt") or {},
            schema=(envelope.get("schema") if strict_schema else None),
            image=image,
        )
    except Exception as exc:  # noqa: BLE001 - a broken candidate must not sink the bake-off
        return {**row, "outcome": "exception", "error": str(exc)[:300], "latency_s": round(time.perf_counter() - started, 2)}
    row["latency_s"] = round(time.perf_counter() - started, 2)
    if not result.get("ok"):
        return {**row, "outcome": "provider_error", "error": str(result.get("error") or "")[:300]}
    normalized = vision_agent.normalize_action(result.get("data") or {})
    invalid = str(normalized.get("invalid_reason") or "")
    row["outcome"] = "invalid" if invalid else "valid"
    row["invalid_reason"] = invalid[:200]
    return {**row, **score_against_recorded(item, normalized)}


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries = []
    for label in sorted({row["model"] for row in rows}):
        subset = [row for row in rows if row["model"] == label]
        valid = [row for row in subset if row.get("outcome") == "valid"]
        errors = [row for row in subset if row.get("outcome") in ("provider_error", "exception")]
        latencies = sorted(row["latency_s"] for row in subset if row.get("latency_s"))
        agree = [row for row in valid if row.get("action_match")]
        control_known = [row for row in valid if row.get("control_match") is not None]
        control_match = [row for row in control_known if row.get("control_match")]
        deltas = sorted(row["coord_delta_px"] for row in valid if row.get("coord_delta_px") is not None)
        summaries.append(
            {
                "model": label,
                "n": len(subset),
                "valid": len(valid),
                "valid_pct": round(len(valid) / max(len(subset), 1) * 100, 1),
                "errors": len(errors),
                "latency_p50_s": latencies[len(latencies) // 2] if latencies else None,
                "latency_p95_s": latencies[int(0.95 * (len(latencies) - 1))] if latencies else None,
                "action_agree": f"{len(agree)}/{len(valid)}",
                "control_agree": f"{len(control_match)}/{len(control_known)}",
                "coord_delta_med_px": deltas[len(deltas) // 2] if deltas else None,
            }
        )
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sample", type=int, default=40)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--base-provider", type=int, default=None, help="Provider id whose key/transport to reuse (defaults to the configured vision provider).")
    parser.add_argument("--candidate", action="append", default=[], help="Model id, optionally @effort (reasoning tier) and :json_object to skip strict schema.")
    parser.add_argument("--include-champion", action="store_true", help="Also replay the champion model to measure the self-agreement noise floor.")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    from app.config import get_settings
    from app.db import SessionLocal
    from app.model_provider import ModelProviderService

    db = SessionLocal()
    try:
        service = ModelProviderService(db)
        settings = get_settings()
        base_provider_id = args.base_provider or getattr(settings, "vision_provider_id", None)
        if not base_provider_id:
            from sqlalchemy import select

            from app import models
            from app.agent_settings import get_agent_settings

            user_id = db.scalars(select(models.User.id).order_by(models.User.id)).first()
            if user_id:
                base_provider_id = (get_agent_settings(db, int(user_id)) or {}).get("vision_provider_id")
        base = service.provider_by_id(base_provider_id) if base_provider_id else None
        if base is None:
            raise SystemExit(f"base provider {base_provider_id!r} is unavailable or disabled")
        champion_model = base.model_name

        pool = collect_pool(db, days=args.days, champion_model=champion_model)
        sample = stratified_sample(pool, target=args.sample)
        if not sample:
            raise SystemExit(f"no retained {champion_model} decisions found in the last {args.days} days")

        legs: list[tuple[str, Any, bool]] = []
        if args.include_champion:
            legs.append((CHAMPION_LABEL, base, True))
        for spec in args.candidate:
            label, _, mode = str(spec).partition(":")
            model_id, _, effort = label.partition("@")
            legs.append(
                (
                    label,
                    dataclasses.replace(
                        base,
                        model_name=model_id,
                        display_name=f"replay:{label}",
                        reasoning_effort=effort or base.reasoning_effort,
                    ),
                    mode != "json_object",
                )
            )
        if not legs:
            raise SystemExit("nothing to replay: pass --candidate and/or --include-champion")

        print(f"replaying {len(sample)} retained inputs x {len(legs)} models (champion={champion_model})", flush=True)
        rows: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = [
                executor.submit(_replay_one, item, label, provider, strict)
                for item in sample
                for label, provider, strict in legs
            ]
            for index, future in enumerate(futures):
                rows.append(future.result())
                if (index + 1) % 15 == 0:
                    print(f"  {index + 1}/{len(futures)} calls done", flush=True)

        out_dir = settings.artifact_root / "eval_scores" / "model_replay"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"{stamp}.jsonl"
        with out_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, default=str) + "\n")

        print(f"\nresults: {out_path}\n")
        header = f"{'model':34s} {'n':>4} {'valid%':>7} {'err':>4} {'p50s':>6} {'p95s':>6} {'action':>8} {'control':>8} {'coord':>7}"
        print(header)
        for summary in summarize(rows):
            print(
                f"{summary['model']:34s} {summary['n']:4d} {summary['valid_pct']:7.1f} {summary['errors']:4d} "
                f"{str(summary['latency_p50_s']):>6} {str(summary['latency_p95_s']):>6} "
                f"{summary['action_agree']:>8} {summary['control_agree']:>8} {str(summary['coord_delta_med_px']):>7}"
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
