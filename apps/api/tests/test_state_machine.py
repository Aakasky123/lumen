import pytest

from app.state_machine import OperatorState, assert_transition, can_transition


def test_core_state_machine_path_is_valid() -> None:
    path = [
        OperatorState.CREATED,
        OperatorState.WAITING_FOR_USER,
        OperatorState.RUNNING,
        OperatorState.READY_FOR_REVIEW,
        OperatorState.COMPLETED,
    ]
    for source, target in zip(path, path[1:]):
        assert can_transition(source.value, target.value)


def test_brain_cannot_skip_runtime_owned_review_gate() -> None:
    assert not can_transition(OperatorState.FILLING_APPLICATION.value, OperatorState.COMPLETED.value)
    with pytest.raises(ValueError):
        assert_transition(OperatorState.FILLING_APPLICATION.value, OperatorState.COMPLETED.value)


def test_active_browser_run_can_be_cancelled() -> None:
    assert can_transition(OperatorState.RUNNING.value, OperatorState.CANCELLED.value)
    assert not can_transition(OperatorState.RUNNING.value, OperatorState.COMPLETED.value)


def test_run_in_a_retired_state_can_only_be_cancelled() -> None:
    assert can_transition("WAITING_FOR_USER_OTP", OperatorState.CANCELLED.value)
    assert not can_transition("WAITING_FOR_USER_OTP", OperatorState.RUNNING.value)
