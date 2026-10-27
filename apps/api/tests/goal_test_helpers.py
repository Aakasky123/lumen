import json
from dataclasses import dataclass
from typing import Any

from app.db import SessionLocal
from app.runtime import OperatorRuntimeEngine
from app.schemas import OperatorRunOut


@dataclass
class GoalTestResponse:
    status_code: int
    _body: dict[str, Any]

    @property
    def text(self) -> str:
        return json.dumps(self._body, default=str)

    def json(self) -> dict[str, Any]:
        return self._body


def create_operator_goal_response(payload: dict[str, Any]) -> GoalTestResponse:
    """Create a run without the retired direct goal HTTP route."""
    with SessionLocal() as db:
        engine = OperatorRuntimeEngine(db)
        job_url = payload["job_url"]
        run = engine.create_goal(job_url=job_url, goal=payload.get("goal") or f"Prepare and fill job application for {job_url}")
        if payload.get("auto_start", True):
            run = engine.run_to_review(run.id)
        db.refresh(run)
        return GoalTestResponse(200, OperatorRunOut.model_validate(run).model_dump(mode="json"))


def create_run_thread(run_id: int) -> int:
    """A chat thread bound to a run, the way a run's own chat is created."""
    from app import models
    from app.cognition import OperatorChatService

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        thread = OperatorChatService(db).create_thread(user_id=run.user_id, run_id=run_id)
        db.commit()
        return thread.id
