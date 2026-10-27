from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def active_base_resume(db: Session, user_id: int) -> models.Resume | None:
    """Return the authoritative base resume for a user.

    Historical databases can contain more than one row marked as the base. The
    newest such row is authoritative until those rows are repaired explicitly.
    """

    return db.scalars(
        select(models.Resume)
        .where(models.Resume.user_id == user_id, models.Resume.is_base.is_(True))
        .order_by(models.Resume.id.desc())
    ).first()


def selected_resume_artifact(
    db: Session,
    user_id: int,
    resume_version_id: int | None = None,
    *,
    expected_application_id: int | None = None,
    expected_job_posting_id: int | None = None,
) -> models.ResumeArtifact | None:
    """Return the exact PDF artifact selected for a user without cross-context fallback.

    Validation happens after selecting the newest exact artifact so a failed
    rebuild cannot expose an older, stale successful PDF. A tailored version is
    usable only when the caller supplies the application and posting identities
    and those rows prove that the application selected that version for that
    exact posting.
    """

    if resume_version_id is not None:
        if expected_application_id is None or expected_job_posting_id is None:
            return None
        artifact = db.scalars(
            select(models.ResumeArtifact)
            .join(models.ResumeVersion, models.ResumeVersion.id == models.ResumeArtifact.resume_version_id)
            .join(models.Resume, models.Resume.id == models.ResumeArtifact.resume_id)
            .join(models.JobPosting, models.JobPosting.id == models.ResumeVersion.job_posting_id)
            .join(models.Application, models.Application.resume_version_id == models.ResumeVersion.id)
            .where(
                models.Application.id == expected_application_id,
                models.Application.user_id == user_id,
                models.Application.job_posting_id == expected_job_posting_id,
                models.JobPosting.user_id == user_id,
                models.JobPosting.id == expected_job_posting_id,
                models.ResumeVersion.job_posting_id == expected_job_posting_id,
                models.Resume.user_id == user_id,
                models.ResumeArtifact.resume_version_id == resume_version_id,
                models.ResumeArtifact.resume_id == models.ResumeVersion.resume_id,
                models.ResumeArtifact.artifact_type == "TAILORED_PDF",
            )
            .order_by(models.ResumeArtifact.id.desc())
        ).first()
    else:
        resume = active_base_resume(db, user_id)
        if resume is None:
            return None
        artifact = db.scalars(
            select(models.ResumeArtifact)
            .where(
                models.ResumeArtifact.resume_id == resume.id,
                models.ResumeArtifact.resume_version_id.is_(None),
                models.ResumeArtifact.artifact_type == "BASE_PDF",
            )
            .order_by(models.ResumeArtifact.id.desc())
        ).first()

    if artifact is None or artifact.compile_status != "SUCCESS" or not str(artifact.pdf_path or "").strip():
        return None
    return artifact
