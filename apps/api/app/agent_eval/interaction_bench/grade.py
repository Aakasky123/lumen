"""Standalone grader for the Interaction Gauntlet — stdlib only, driven by manifest.json.

Generations 1 and 2 are graded from ONE artifact per page: the final visible page text (innerText
of the body) after the agent finishes or gives up. The page itself is the judge; this script only
reads what the page displayed.

Generation 3 pages (manifest ``"harness": true``) are judged by the v3 harness, which saw every
surface the agent touched (native windows, dialogs, files, browser prompts). Pass its report with
``--harness URL`` (fetched live) or ``--harness-report report.json``; without one, those pages fall
back to their final page text, which renders the same progress and verdict lines.

Usage:
    python grade.py results.json [-o scorecard.json] [--harness http://127.0.0.1:PORT]

results.json maps page name -> final visible page text:
    { "page_dynamics": "...full innerText...", "click_gauntlet": "..." }

Per page:  passed   = pass token present AND no BENCH_FAIL
           ratio    = challenges complete / total (zeroed if a trap was sprung)
Overall:   pass rate + mean ratio, overall and per family.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from pathlib import Path

_PROGRESS_RE = re.compile(r"Challenges complete:\s*(\d+)\s*/\s*(\d+)")
_FUTILE_RE = re.compile(r"Ineffective interactions:\s*(\d+)")
_HOPS_RE = re.compile(r"Surface hops:\s*(\d+)")
_FAIL_RE = re.compile(r"BENCH_FAIL [\w-]+: (.+)")


def load_manifest() -> dict:
    return json.loads((Path(__file__).parent / "manifest.json").read_text(encoding="utf-8"))


def grade_page(page_text: str, token: str) -> dict:
    text = page_text or ""
    fail = _FAIL_RE.search(text)
    progress = _PROGRESS_RE.search(text)
    futile = _FUTILE_RE.search(text)
    hops = _HOPS_RE.search(text)
    completed, total = (int(progress.group(1)), int(progress.group(2))) if progress else (0, 0)
    passed = token in text and not fail
    return {
        "passed": passed,
        "completed": completed,
        "total": total,
        "ratio": 0.0 if fail else (completed / total if total else 0.0),
        "failed": bool(fail),
        "fail_reason": fail.group(1).strip() if fail else None,
        "futile_interactions": int(futile.group(1)) if futile else None,
        "surface_hops": int(hops.group(1)) if hops else None,
    }


def grade_from_report(report: dict, page: str) -> dict:
    """A generation-3 page's grade from the harness's own verdict (GET /v3/api/report)."""
    entry = (report.get("pages") or {}).get(page)
    if not entry:
        return {"passed": False, "completed": 0, "total": 0, "ratio": 0.0, "failed": False, "fail_reason": None,
                "futile_interactions": None, "surface_hops": None, "violations": 0, "violation_kinds": [],
                "perturbations": [], "perturb_events": [], "source": "harness_report"}
    best = entry.get("best") or {}
    failed = entry.get("failed")
    total = int(best.get("total") or 0)
    completed = int(best.get("completed") or 0)
    return {
        "passed": bool(entry.get("passed")),
        "completed": completed,
        "total": total,
        "ratio": 0.0 if failed else (completed / total if total else 0.0),
        "failed": bool(failed),
        "fail_reason": failed,
        "futile_interactions": best.get("futile"),
        "surface_hops": best.get("hops"),
        "elapsed_s": best.get("elapsed_s"),
        "violations": int(entry.get("violations") or 0),
        "violation_kinds": list(entry.get("violation_kinds") or []),
        "perturbations": list(entry.get("perturbations") or []),
        "perturb_events": list(entry.get("perturb_events") or []),
        "source": "harness_report",
    }


def fetch_report(base_url: str) -> dict:
    with urllib.request.urlopen(base_url.rstrip("/") + "/v3/api/report", timeout=30) as resp:
        return json.loads(resp.read())


def grade_run(results: dict[str, str], harness_report: dict | None = None) -> dict:
    manifest = load_manifest()
    pages = {p["name"]: p for p in manifest["pages"]}
    per_page: dict[str, dict] = {}
    families: dict[str, dict] = {}
    for name, page in pages.items():
        if page.get("harness") and harness_report is not None:
            g = grade_from_report(harness_report, name)
            g["attempted"] = name in (harness_report.get("pages") or {}) or name in results
        else:
            g = grade_page(results.get(name, ""), page["success_token"])
            g["attempted"] = name in results
        g["family"] = page["family"]
        g["tier"] = page["tier"]
        per_page[name] = g
        fam = families.setdefault(page["family"], {"pages": 0, "passed": 0, "ratio_sum": 0.0, "traps_sprung": 0})
        fam["pages"] += 1
        fam["passed"] += int(g["passed"])
        fam["ratio_sum"] += g["ratio"]
        fam["traps_sprung"] += int(g["failed"])
    for fam in families.values():
        fam["pass_rate"] = fam["passed"] / fam["pages"]
        fam["mean_challenge_ratio"] = fam["ratio_sum"] / fam["pages"]
        del fam["ratio_sum"]
    n = len(pages)
    return {
        "benchmark": manifest["benchmark"],
        "version": manifest["version"],
        "overall": {
            "pages": n,
            "passed": sum(int(g["passed"]) for g in per_page.values()),
            "pass_rate": sum(int(g["passed"]) for g in per_page.values()) / n,
            "mean_challenge_ratio": sum(g["ratio"] for g in per_page.values()) / n,
            "traps_sprung": sum(int(g["failed"]) for g in per_page.values()),
            "unattempted": [k for k, g in per_page.items() if not g["attempted"]],
        },
        "by_family": families,
        "by_page": per_page,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Grade an Interaction Gauntlet run.")
    parser.add_argument("results", help="JSON file mapping page name -> final visible page text")
    parser.add_argument("-o", "--out", help="write the scorecard JSON here (default: stdout)")
    parser.add_argument("--harness", help="v3 harness base URL; generation-3 pages are graded from its report")
    parser.add_argument("--harness-report", help="a saved /v3/api/report JSON, instead of --harness")
    args = parser.parse_args()
    results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    report = None
    if args.harness:
        report = fetch_report(args.harness)
    elif args.harness_report:
        report = json.loads(Path(args.harness_report).read_text(encoding="utf-8"))
    scorecard = grade_run(results, report)
    output = json.dumps(scorecard, indent=2)
    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        print(f"wrote {args.out}")
    summary = scorecard["overall"]
    print(f"pass rate: {summary['passed']}/{summary['pages']}  "
          f"mean challenge ratio: {summary['mean_challenge_ratio']:.2f}  "
          f"traps sprung: {summary['traps_sprung']}")
    if not args.out:
        print(output)


if __name__ == "__main__":
    main()
