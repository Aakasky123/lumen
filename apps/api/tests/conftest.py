import os
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

# Agent shells export PYTHONDONTWRITEBYTECODE=1, which freezes pytest's assertion-rewrite cache:
# every edited test module was rewritten and recompiled on each run (~6s of collection per process
# and per -n worker for the 23k-line test_extension_run.py). __pycache__ is gitignored.
sys.dont_write_bytecode = False

API_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = API_ROOT.parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

# Every pytest process (each `-n` worker, and each concurrent run in this checkout) gets its
# own storage dir, so parallel runs never share a SQLite file or delete each other's state.
TEST_RUNS_ROOT = PROJECT_ROOT / "storage" / "test-runs"
TEST_STORAGE = TEST_RUNS_ROOT / f"{os.environ.get('PYTEST_XDIST_WORKER', 'main')}-{os.getpid()}"
STALE_TEST_STORAGE_SECONDS = 12 * 3600
# `-n auto` (pytest.ini): past ~8 workers, startup and collection cost more than they save.
MAX_XDIST_WORKERS = 8
MIN_TESTS_FOR_XDIST = 100
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def test_", re.MULTILINE)

# Assigned, not defaulted: `-n` workers inherit the controller's environment, and a DATABASE_URL
# from the shell must never point tests at a live database.
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_STORAGE / 'test_lumen.db'}"
os.environ["LUMEN_ARTIFACT_ROOT"] = str(TEST_STORAGE / "artifacts")
# Isolate the markdown user-memory dir (derived from LUMEN_DATA_ROOT) so test runs
# never write facts into the real user's memory files.
os.environ["LUMEN_DATA_ROOT"] = str(TEST_STORAGE / "data")
# The developer machine's .env enables OS-level computer use; tests must never move the
# real mouse, press keys, or launch real applications (e.g. Chrome self-launch).
os.environ["LUMEN_DESKTOP_CONTROL"] = "false"
# Runs hold a real Windows keep-awake power request; tests must not change the machine's power state.
os.environ["LUMEN_KEEP_AWAKE"] = "false"
# An ask_user parks the in-browser loop for the answer (up to 10 minutes); tests that want it set it.
os.environ["LUMEN_ASK_USER_PARK_SECONDS"] = "0"
# No background housekeeping threads in tests.
os.environ["LUMEN_FRAME_KEEP_RUNS"] = "0"
os.environ["LUMEN_FRAME_KEEP_DAYS"] = "0"
os.environ["USE_MOCK_AI"] = "true"
# No run-start prewarm requests in tests (run_prewarm.py); its own tests re-enable it.
os.environ["LUMEN_RUN_PREWARM"] = "false"
# Tests never bind the remote listener or push to real phones.
os.environ["LUMEN_REMOTE_PORT"] = "0"
os.environ["LUMEN_FIREBASE_CREDENTIALS"] = str(TEST_STORAGE / "data" / "no-firebase.json")
# Vault and provider-key tests need a key; a fresh clone has no .env to supply one.
os.environ.setdefault("LUMEN_TOKEN_ENCRYPTION_KEY", "lumen-test-only-encryption-key")
# Keep the deferred application synchronous in tests so runs are deterministic
# (no background threads); production defaults to async chat narration.
os.environ.setdefault("LUMEN_CHAT_APPLICATION_ASYNC", "false")


def pytest_xdist_auto_num_workers(config) -> int:
    # A directory or a big file is spread over workers; a few tests run in-process, where starting
    # workers (~2s) would cost more than they save. `-n 0` / `-n 4` on the command line override this.
    workers = min(MAX_XDIST_WORKERS, os.cpu_count() or 1)
    if config.getoption("keyword"):  # `-k` usually narrows to a handful
        return 0
    selected = 0
    for arg in config.args:
        path = Path(config.invocation_params.dir, arg.split("::")[0])
        if "::" in arg:
            selected += 1
        elif path.is_dir():
            selected += MIN_TESTS_FOR_XDIST
        elif path.is_file():
            selected += len(_TEST_DEF.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return workers if workers > 1 and selected >= MIN_TESTS_FOR_XDIST else 0


def pytest_sessionstart(session) -> None:
    # A crashed or killed run leaves its dir behind; no live run is this old.
    cutoff = time.time() - STALE_TEST_STORAGE_SECONDS
    for other in TEST_RUNS_ROOT.glob("*") if TEST_RUNS_ROOT.exists() else ():
        try:
            stale = other != TEST_STORAGE and other.stat().st_mtime < cutoff
        except OSError:  # a concurrent run removed its own dir after the glob
            continue
        if stale:
            shutil.rmtree(other, ignore_errors=True)
    shutil.rmtree(TEST_STORAGE, ignore_errors=True)
    TEST_STORAGE.mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="session", autouse=True)
def _database_schema():
    # Create the schema up front so no test depends on an earlier one having called init_db();
    # with `-n` a test can land on a worker that ran nothing before it.
    from app.db import init_db

    init_db()


@pytest.fixture(autouse=True)
def _instant_screen_settle(monkeypatch):
    # The loop's real-time pauses wait for a live page to settle; test bodies are fakes that are
    # settled at once, and these pauses were most of the suite's wall time. A test that is about
    # the pause sets its own value.
    from app import extension_run

    monkeypatch.setattr(extension_run, "_VISION_STEP_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(extension_run, "_LATE_EFFECT_RECHECK_SECONDS", 0.0)
    monkeypatch.setattr(extension_run, "_SECRET_CLASSIFIER_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr("app.lumen_model.GROUNDING_JUDGE_RETRY_BACKOFF_SECONDS", 0.0)
    # Existing gate tests pin the terminal block; deferral has its own tests that raise this.
    monkeypatch.setattr(extension_run, "_SECRET_CLASSIFIER_DEFERRALS_MAX", 0)
    monkeypatch.setattr(extension_run, "_FINAL_ACTION_VERIFICATION_REOBSERVE_DELAYS_SECONDS", (0.0, 0.0, 0.0))
    # The desktop bodies wait for the screen to stop changing after an action; fakes are settled at once.
    from app import desktop_settle

    monkeypatch.setattr(desktop_settle, "INSTANT", True)


@pytest.fixture(autouse=True)
def _field_subject_default(monkeypatch):
    # The whose-fact gate is a live model call; tests that are not about it see a field that is not a
    # personal fact. Tests of the gate set their own verdict (or call the real client with no provider).
    from app import extension_run

    def fake(*args, **kwargs):
        return {"subject": "not_personal", "reason": "test default", "fail_closed": False}

    # The real wrapper stays reachable for tests that exercise it end to end.
    fake.real = extension_run._call_field_subject_judge
    monkeypatch.setattr(extension_run, "_FIELD_SUBJECT_CACHE", {})
    monkeypatch.setattr(extension_run, "_call_field_subject_judge", fake)


@pytest.fixture(autouse=True)
def _one_monitor(monkeypatch):
    # The desktop body looks at one monitor of several; tests see a single-monitor machine (the whole
    # desktop is the view) whatever is plugged into the machine running them. A test about monitors
    # sets its own.
    from app import desktop_body

    monkeypatch.setattr(desktop_body, "_monitors", lambda: [])
    monkeypatch.setitem(desktop_body._VIEW, "pinned", None)
    monkeypatch.setitem(desktop_body._VIEW, "rect", None)


@pytest.fixture(autouse=True)
def _windows_desktop_platform():
    # Tests written against app.desktop_body patch that module; the platform facade serves it on every OS
    # (a Mac included) unless a test pins app.desktop_body_macos itself.
    from app import desktop_body, desktop_platform

    desktop_platform.pin(desktop_body)
    yield
    desktop_platform.pin(None)


def pytest_sessionfinish(session, exitstatus) -> None:
    db = sys.modules.get("app.db")
    if db is not None:
        db.engine.dispose()  # release the SQLite handles Windows won't delete under
    shutil.rmtree(TEST_STORAGE, ignore_errors=True)
