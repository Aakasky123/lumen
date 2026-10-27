from conftest import TEST_STORAGE

from app.config import PROJECT_ROOT, get_settings


def test_pytest_artifact_root_is_isolated_from_live_artifacts() -> None:
    root = get_settings().artifact_root.resolve()
    assert root == (TEST_STORAGE / "artifacts").resolve()
    assert (PROJECT_ROOT / "storage" / "test-runs").resolve() in root.parents
