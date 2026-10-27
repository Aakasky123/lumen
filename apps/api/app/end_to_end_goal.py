from __future__ import annotations

from sqlalchemy.orm import Session

from app import models
from app.event_store import emit_event
from app.runtime import OperatorRuntimeEngine


class EndToEndGoalService:
    """Creates the run behind a chat-started application; the vision loop drives it."""

    def __init__(self, db: Session):
        self.db = db
        self.runtime = OperatorRuntimeEngine(db)

    def start_deferred(self, *, job_url: str) -> dict:
        run = self.runtime.create_goal(job_url=job_url, goal=f"End-to-end Lumen workflow for {job_url}")
        workspace = {
            "phase": "JOB_INGESTION",
            "run_phase": "intake",
            "submission_strategy": "BASE_RESUME_ONLY",
            "execution_mode": "CONTROLLED_LIVE",
            "submission_mode": "APPROVE_ONCE",
        }
        run.status_message = "Lumen is starting this live application session."
        run.confidence_state = {**(run.confidence_state or {}), "end_to_end": workspace}
        run.updated_at = models.utcnow()
        emit_event(
            self.db,
            event_type="END_TO_END_GOAL_BACKGROUND_QUEUED",
            payload={"run_id": run.id, "phase": "JOB_INGESTION"},
            run_id=run.id,
            application_id=run.application_id,
            actor="goal_supervisor",
            confidence=0.82,
        )
        self.db.commit()
        return {"run": run}
