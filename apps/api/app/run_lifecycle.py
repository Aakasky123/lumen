from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from typing import Iterator

from sqlalchemy import update
from sqlalchemy.orm import Session

from app import models


# A bounded set of re-entrant stripes keeps same-process lifecycle mutations for one
# run ordered without retaining one lock forever for every historical run.
_RUN_LIFECYCLE_LOCKS = tuple(RLock() for _ in range(64))


@contextmanager
def run_lifecycle_guard(run_id: int) -> Iterator[None]:
    """Serialize same-process lifecycle mutations for one run."""
    lock = _RUN_LIFECYCLE_LOCKS[int(run_id) % len(_RUN_LIFECYCLE_LOCKS)]
    with lock:
        yield


def serialize_run_lifecycle_row(
    db: Session,
    run_id: int,
    *,
    excluded_states: set[str] | frozenset[str] | None = None,
) -> bool:
    """Claim the run row until transaction end, optionally only while it is live.

    The conditional no-op UPDATE is both a CAS and a row write lock. It works on
    SQLite, where SELECT FOR UPDATE is ignored, and on databases with row locks.
    """
    statement = update(models.OperatorRun).where(models.OperatorRun.id == int(run_id))
    if excluded_states:
        statement = statement.where(models.OperatorRun.current_state.not_in(sorted(excluded_states)))
    result = db.execute(
        statement.values(current_state=models.OperatorRun.current_state).execution_options(
            synchronize_session=False
        )
    )
    return result.rowcount == 1
