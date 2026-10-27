from enum import StrEnum


class OperatorState(StrEnum):
    CREATED = "CREATED"
    FILLING_APPLICATION = "FILLING_APPLICATION"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    RUNNING = "RUNNING"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


ALLOWED_TRANSITIONS: dict[OperatorState, set[OperatorState]] = {
    OperatorState.CREATED: {OperatorState.WAITING_FOR_USER, OperatorState.BLOCKED, OperatorState.CANCELLED},
    OperatorState.FILLING_APPLICATION: {
        OperatorState.WAITING_FOR_USER,
        OperatorState.READY_FOR_REVIEW,
        OperatorState.BLOCKED,
        OperatorState.CANCELLED,
    },
    OperatorState.WAITING_FOR_USER: {
        OperatorState.CREATED,
        OperatorState.FILLING_APPLICATION,
        OperatorState.RUNNING,
        OperatorState.READY_FOR_REVIEW,
        OperatorState.BLOCKED,
        OperatorState.CANCELLED,
    },
    OperatorState.RUNNING: {
        OperatorState.WAITING_FOR_USER,
        OperatorState.READY_FOR_REVIEW,
        OperatorState.BLOCKED,
        OperatorState.CANCELLED,
    },
    OperatorState.READY_FOR_REVIEW: {
        OperatorState.COMPLETED,
        OperatorState.FILLING_APPLICATION,
        OperatorState.WAITING_FOR_USER,
        OperatorState.BLOCKED,
        OperatorState.CANCELLED,
    },
    OperatorState.BLOCKED: {OperatorState.WAITING_FOR_USER, OperatorState.CANCELLED},
    OperatorState.CANCELLED: set(),
    OperatorState.COMPLETED: set(),
}


def can_transition(from_state: str, to_state: str) -> bool:
    try:
        target = OperatorState(to_state)
    except ValueError:
        return False
    try:
        source = OperatorState(from_state)
    except ValueError:
        # Runs persisted in a retired state can still be cancelled.
        return target == OperatorState.CANCELLED
    return target in ALLOWED_TRANSITIONS[source]


def assert_transition(from_state: str, to_state: str) -> None:
    if not can_transition(from_state, to_state):
        raise ValueError(f"Invalid operator state transition: {from_state} -> {to_state}")
