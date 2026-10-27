"""Pinned copies of open-source agent benchmarks, kept out of git under storage/open_bench/src.

    python -m app.agent_eval.open_bench.sources fetch   # clone (or re-pin) every source, build WebGames
    python -m app.agent_eval.open_bench.sources list    # name, commit, license, what it measures

Only source repositories are fetched. Heavy runtime pieces stay out until someone runs that benchmark:
WebArena-Verified's Docker site images and WindowsAgentArena's Windows VM image (tens of GB each).
WebGames (a Vite app) is built to static files; it needs Node, and pnpm runs through npx.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
ROOT = REPO / "storage" / "open_bench" / "src"

SOURCES = {
    "miniwob": {
        "repo": "https://github.com/Farama-Foundation/miniwob-plusplus.git",
        "commit": "33c3b4ddef8c6eb67c57a29663d844b1eda7e614",
        "license": "MIT",
        "measures": "125+ small static web tasks (click, type, forms, drag, date pickers); reward in page JS",
    },
    "webgames": {
        "repo": "https://github.com/convergence-ai/webgames.git",
        "commit": "309866f64642b864414ca2c7ff956e59e01d3317",
        "license": "Apache-2.0",
        "measures": "150 web challenges; success = the task's password in the agent's final answer",
    },
    "real-agisdk": {
        "repo": "https://github.com/agi-inc/agisdk.git",
        "commit": "97a245dcbccd3e1cf4d5ee00d5b1707942258518",
        "license": "Apache-2.0",
        "measures": "REAL: 112 tasks on hosted clones of real sites; state-diff grading (+ rubric for retrieval)",
    },
    "openapps": {
        "repo": "https://github.com/facebookresearch/OpenApps.git",
        "commit": "a14f491fb1fff22bd1b1935a72a5eed741b52ed3",
        "license": "CC-BY-NC-4.0 (non-commercial)",
        "measures": "6 apps with configurable appearance/content; reliability across app variants",
    },
    "formfactory": {
        "repo": "https://github.com/formfactory-ai/formfactory.git",
        "commit": "b7ef0d669d77d2afd41ff4d51ab31becbdeff30b",
        "license": "MIT (README only; the repository has no LICENSE file)",
        "measures": "Flask form apps, 1,250 instances, rule-based field grading (form filling, fabrication probe)",
    },
    "clawbench": {
        "repo": "https://github.com/reacher-z/ClawBench.git",
        "commit": "4b98796382d8a6c6bb140b5518cc280c61aee563",
        "license": "Apache-2.0",
        "measures": "live-site tasks incl. job applications; graded on the intercepted submit payload",
    },
    "browsergym": {
        "repo": "https://github.com/ServiceNow/BrowserGym.git",
        "commit": "e71419aef5759a11cb7896a26c36e5f7e92d82b2",
        "license": "Apache-2.0",
        "measures": "one task API over MiniWoB, WebArena, VisualWebArena, WorkArena, AssistantBench, WebLINX",
    },
    "agentlab": {
        "repo": "https://github.com/ServiceNow/AgentLab.git",
        "commit": "8488c564eea0525aaff5dea189bb03a9b529546e",
        "license": "Apache-2.0",
        "measures": "BrowserGym experiment runner and study tooling",
    },
    "webarena-verified": {
        "repo": "https://github.com/ServiceNow/webarena-verified.git",
        "commit": "6473f72db5dcefc97b5725b59e734504edc28a21",
        "license": "Apache-2.0",
        "measures": "812 WebArena tasks re-verified, deterministic JSON/HAR grading; needs Docker site images",
    },
    "windowsagentarena": {
        "repo": "https://github.com/microsoft/WindowsAgentArena.git",
        "commit": "6d39ed88c545a0d40a7a02e39b928e278df7332b",
        "license": "MIT",
        "measures": "154 Windows tasks graded by in-VM scripts; needs the Windows VM image (OSWorld fork)",
    },
}


def miniwob_html(root: Path = ROOT) -> Path:
    return root / "miniwob" / "miniwob" / "html"


def webgames_dist(root: Path = ROOT) -> Path:
    return root / "webgames" / "webgames" / "dist"


def webgames_tasks(root: Path = ROOT) -> list[dict]:
    path = root / "webgames" / "analysis" / "webgames_tasks.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _git(args: list[str], cwd: Path) -> None:
    # ClawBench's task folders have names longer than Windows' default path limit.
    subprocess.run(["git", "-c", "core.longpaths=true", *args], cwd=str(cwd), check=True)


def fetch(root: Path = ROOT) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, source in SOURCES.items():
        path = root / name
        if not (path / ".git").is_dir():
            _git(["clone", "--depth", "1", "--no-checkout", source["repo"], str(path)], root)
            _git(["config", "core.longpaths", "true"], path)
        _git(["fetch", "--depth", "1", "origin", source["commit"]], path)
        _git(["checkout", "--quiet", "--force", source["commit"]], path)
        print(f"{name}: {source['commit'][:12]} ({source['license']})", flush=True)
    npx = shutil.which("npx")
    if not npx:
        raise SystemExit("WebGames needs Node.js (npx) to build")
    app = root / "webgames" / "webgames"
    subprocess.run([npx, "-y", "pnpm@9", "install", "--frozen-lockfile"], cwd=str(app), check=True)
    # vite alone: the type check (tsc -b) is the upstream project's lint, not part of serving its pages.
    subprocess.run([npx, "-y", "pnpm@9", "exec", "vite", "build"], cwd=str(app), check=True)
    print(f"webgames built: {webgames_dist(root)}", flush=True)


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    if command == "fetch":
        fetch()
    elif command == "list":
        for name, source in SOURCES.items():
            present = "pulled" if (ROOT / name / ".git").is_dir() else "missing"
            print(f"{name:18} {source['commit'][:12]} {present:7} {source['license']:30} {source['measures']}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
