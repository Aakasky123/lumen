"""Package the Interaction Gauntlet as a standalone zip for testing external agents.

Generates manifest.json from tasks.py (single source of truth), then bundles pages/, serve.py,
grade.py, validate_golden_path.py, and the standalone README into a zip that has zero dependencies
on this repo. A bundle that includes generations 3-6 also carries their harness (v3_harness.py and the
v3/v4/v5 mission and desktop modules), their validators, and the shared page runtime and style.

Usage:  python -m app.agent_eval.interaction_bench.package [--out DIR_OR_ZIP] [--generation 1|2|3|4|5|all]
"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

from app.agent_eval.interaction_bench.tasks import GAUNTLET_PAGES
from app.agent_eval.interaction_bench.v3_harness import MISSIONS

HERE = Path(__file__).parent
VERSION = "6.0"

# helper pages a gauntlet page opens in other windows or tabs; bundled alongside their owner
AUX_PAGES = {
    "os_dialogs": ["os_dialogs_aux"],
    "v2_os_gauntlet": ["v2_os_aux1", "v2_os_aux2"],
    "v3_kiosk": ["v3_kiosk_ticket"],
    "v3_incident": ["v3_incident_portal"],
    "v4_clicks": ["v4_clicks_notes"],
    "v4_browser": ["v4_browser_dest", "v4_browser_vault", "v4_browser_hop", "v4_browser_tab"],
    "v5_onboarding": ["v5_onboarding_badge"],
    "v5_marathon": ["v5_marathon_lookup"],
}
# generations 3-6 run under one harness; it travels whole with any bundle that includes one of them
HARNESS_FILES = [
    "v3_harness.py", "v3_missions.py", "v3_desktop.py", "v4_missions.py", "v4_desktop.py", "v5_missions.py",
    "v5_desktop.py", "v6_missions.py", "validate_v3.py", "validate_v4.py", "validate_v5.py", "validate_v6.py",
]
HARNESS_PAGE_FILES = ["v3_common.js", "v4_style.css", "v6_kit.js", "v6_style.css", "v6_offsite.html"]


def _selected(generation: int | None):
    return [p for p in GAUNTLET_PAGES if generation is None or p.generation == generation]


def build_manifest(generation: int | None = None) -> dict:
    pages = _selected(generation)
    return {
        "benchmark": "lumen-interaction-gauntlet",
        "version": VERSION,
        "generations_included": sorted({p.generation for p in pages}),
        "fail_marker": "BENCH_FAIL",
        "progress_pattern": r"Challenges complete:\s*(\d+)\s*/\s*(\d+)",
        "pages": [
            {
                "name": p.name,
                "url_path": f"/eval/bench/{p.name}",
                "family": p.family,
                "tier": p.tier,
                "generation": p.generation,
                "challenges": p.challenges,
                "goal": p.goal,
                "success_token": p.token,
                "max_steps": p.max_steps,
                "harness": p.harness,
                "budget_min": p.budget_min,
                # harness pages: the challenge ids, and whether one can run alone (?focus=, live_run --focus)
                "challenge_ids": list(MISSIONS[p.name].steps) if p.name in MISSIONS else [],
                "focusable": bool(MISSIONS[p.name].focusable) if p.name in MISSIONS else False,
                "desktop": p.desktop,
            }
            for p in pages
        ],
    }


def write_manifest() -> Path:
    """The repo copy of manifest.json always carries the full benchmark."""
    path = HERE / "manifest.json"
    path.write_text(json.dumps(build_manifest(None), indent=2) + "\n", encoding="utf-8")
    return path


def package(out: Path, generation: int | None = None) -> Path:
    write_manifest()
    label = "all" if generation is None else f"v{generation}"
    if out.suffix != ".zip":
        out = out / f"interaction-gauntlet-{label}-{VERSION}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    validator = (HERE / "validate_golden_path.py").read_text(encoding="utf-8").replace(
        "python -m app.agent_eval.interaction_bench.validate_golden_path", "python validate_golden_path.py"
    )
    selected = _selected(generation)
    page_files: list[str] = []
    for p in selected:
        page_files.append(f"{p.name}.html")
        page_files.extend(f"{aux}.html" for aux in AUX_PAGES.get(p.name, []))
    with_harness = any(p.harness for p in selected)
    if with_harness:
        page_files.extend(HARNESS_PAGE_FILES)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        root = "interaction-gauntlet/"
        zf.writestr(root + "README.md", (HERE / "README_STANDALONE.md").read_text(encoding="utf-8"))
        zf.writestr(root + "manifest.json", json.dumps(build_manifest(generation), indent=2) + "\n")
        zf.writestr(root + "serve.py", (HERE / "serve.py").read_text(encoding="utf-8"))
        zf.writestr(root + "grade.py", (HERE / "grade.py").read_text(encoding="utf-8"))
        zf.writestr(root + "validate_golden_path.py", validator)
        if with_harness:
            for name in HARNESS_FILES:
                text = (HERE / name).read_text(encoding="utf-8")
                zf.writestr(root + name, text.replace("python -m app.agent_eval.interaction_bench.", "python -m "))
        for name in page_files:
            page = HERE / "pages" / name
            zf.writestr(root + "pages/" + page.name, page.read_text(encoding="utf-8"))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Package the Interaction Gauntlet.")
    parser.add_argument("--out", default=str(HERE / "dist"), help="output directory or .zip path")
    parser.add_argument("--generation", choices=["1", "2", "3", "4", "5", "6", "all"], default="all",
                        help="which gauntlet generation to bundle (default: all)")
    args = parser.parse_args()
    generation = None if args.generation == "all" else int(args.generation)
    out = package(Path(args.out), generation)
    print(f"packaged: {out}")


if __name__ == "__main__":
    main()
