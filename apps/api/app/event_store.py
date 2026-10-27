from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.security import redact_otp_codes


def emit_event(
    db: Session,
    *,
    event_type: str,
    payload: dict | None = None,
    run_id: int | None = None,
    application_id: int | None = None,
    actor: str = "runtime",
    confidence: float = 1.0,
    risk_level: str = "low",
) -> models.CognitiveEvent:
    event = models.CognitiveEvent(
        run_id=run_id,
        application_id=application_id,
        event_type=event_type,
        event_payload=redact_otp_codes(payload or {}),
        actor=actor,
        confidence=confidence,
        risk_level=risk_level,
    )
    db.add(event)
    db.flush()
    return event


def list_events(db: Session, run_id: int, after_id: int | None = None) -> list[models.CognitiveEvent]:
    stmt = select(models.CognitiveEvent).where(models.CognitiveEvent.run_id == run_id)
    if after_id is not None:
        stmt = stmt.where(models.CognitiveEvent.id > after_id)
    stmt = stmt.order_by(models.CognitiveEvent.id.asc())
    return list(db.scalars(stmt).all())
