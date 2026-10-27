from pathlib import Path

from app import models
from app.application_artifacts import ApplicationArtifactService
from app.candidate_context import CandidateContextEngine
from app.db import SessionLocal, init_db
from app.mode_guard import _profile_readiness, _resume_readiness
from app.resume_selection import active_base_resume, selected_resume_artifact


def test_newest_active_base_drives_candidate_context() -> None:
    init_db()
    with SessionLocal() as db:
        user = models.User(email="resume-selection-context@lumen.test", name="Resume Context")
        db.add(user)
        db.flush()

        stale = models.Resume(
            user_id=user.id,
            name="stale-demo.pdf",
            base_resume_text="STALE_RESUME_MARKER outdated candidate experience",
            is_base=True,
        )
        current = models.Resume(
            user_id=user.id,
            name="current-base.pdf",
            base_resume_text="CURRENT_RESUME_MARKER authoritative candidate experience",
            is_base=True,
        )
        newer_nonbase = models.Resume(
            user_id=user.id,
            name="tailored-copy.pdf",
            base_resume_text="TAILORED_COPY_MARKER generated candidate experience",
            is_base=False,
        )
        db.add_all([stale, current, newer_nonbase])
        db.flush()
        current_artifact = models.ResumeArtifact(
            resume_id=current.id,
            artifact_type="BASE_PDF",
            pdf_path="current-base.pdf",
            compile_status="SUCCESS",
        )
        db.add(current_artifact)
        db.flush()

        assert active_base_resume(db, user.id).id == current.id
        assert selected_resume_artifact(db, user.id).id == current_artifact.id

        facts = CandidateContextEngine(db)._facts(user.id)
        fact_text = " ".join(fact.value for fact in facts)
        assert "CURRENT_RESUME_MARKER" in fact_text
        assert "STALE_RESUME_MARKER" not in fact_text
        assert "TAILORED_COPY_MARKER" not in fact_text

        run = models.OperatorRun(
            user_id=user.id,
            goal="Create an application artifact",
            target_url="https://example.test/application",
        )
        task = models.RuntimeTask(run_id=0, name="Generate document", payload={})
        context = ApplicationArtifactService(db)._generation_context(
            run,
            task=task,
            instructions=None,
            artifact_kind="document",
        )
        assert context["candidate"]["resume_text"] == "CURRENT_RESUME_MARKER authoritative candidate experience"


def test_explicit_tailored_selection_is_owned_and_never_falls_back() -> None:
    init_db()
    with SessionLocal() as db:
        user = models.User(email="resume-selection-version@lumen.test", name="Version Owner")
        other_user = models.User(email="resume-selection-other@lumen.test", name="Other Owner")
        db.add_all([user, other_user])
        db.flush()
        resume = models.Resume(user_id=user.id, name="base.pdf", base_resume_text="base", is_base=True)
        posting = models.JobPosting(user_id=user.id, url="https://example.test/jobs/1")
        db.add_all([resume, posting])
        db.flush()
        base_artifact = models.ResumeArtifact(
            resume_id=resume.id,
            artifact_type="BASE_PDF",
            pdf_path="base.pdf",
            compile_status="SUCCESS",
        )
        version = models.ResumeVersion(job_posting_id=posting.id, resume_id=resume.id, tailored_latex="tailored")
        db.add_all([base_artifact, version])
        db.flush()
        matching_application = models.Application(
            user_id=user.id,
            job_posting_id=posting.id,
            resume_version_id=version.id,
        )
        different_posting = models.JobPosting(user_id=user.id, url="https://example.test/jobs/2")
        db.add_all([matching_application, different_posting])
        db.flush()
        mismatched_application = models.Application(
            user_id=user.id,
            job_posting_id=different_posting.id,
            resume_version_id=version.id,
        )
        db.add(mismatched_application)
        db.flush()
        failed_tailored = models.ResumeArtifact(
            resume_id=resume.id,
            resume_version_id=version.id,
            artifact_type="TAILORED_PDF",
            pdf_path=None,
            compile_status="FAILED",
        )
        db.add(failed_tailored)
        db.flush()

        assert selected_resume_artifact(db, user.id, version.id) is None
        assert selected_resume_artifact(
            db,
            other_user.id,
            version.id,
            expected_application_id=matching_application.id,
            expected_job_posting_id=posting.id,
        ) is None

        other_resume = models.Resume(
            user_id=other_user.id,
            name="other-user.pdf",
            base_resume_text="other",
            is_base=True,
        )
        db.add(other_resume)
        db.flush()
        db.add(
            models.ResumeArtifact(
                resume_id=other_resume.id,
                resume_version_id=version.id,
                artifact_type="TAILORED_PDF",
                pdf_path="cross-user-tailored.pdf",
                compile_status="SUCCESS",
            )
        )
        db.flush()

        assert selected_resume_artifact(
            db,
            user.id,
            version.id,
            expected_application_id=matching_application.id,
            expected_job_posting_id=posting.id,
        ) is None

        tailored = models.ResumeArtifact(
            resume_id=resume.id,
            resume_version_id=version.id,
            artifact_type="TAILORED_PDF",
            pdf_path="tailored.pdf",
            compile_status="SUCCESS",
        )
        db.add(tailored)
        db.flush()

        selected = selected_resume_artifact(
            db,
            user.id,
            version.id,
            expected_application_id=matching_application.id,
            expected_job_posting_id=posting.id,
        )
        assert selected is not None
        assert selected.id == tailored.id
        assert selected_resume_artifact(
            db,
            user.id,
            version.id,
            expected_application_id=mismatched_application.id,
            expected_job_posting_id=different_posting.id,
        ) is None
        assert selected_resume_artifact(
            db,
            user.id,
            version.id,
            expected_application_id=matching_application.id,
            expected_job_posting_id=different_posting.id,
        ) is None


def test_readiness_uses_current_base_not_stale_or_tailored_artifact(tmp_path: Path) -> None:
    init_db()
    with SessionLocal() as db:
        user = models.User(email="resume-selection-ready@lumen.test", name="Ready Candidate")
        db.add(user)
        db.flush()
        db.add(
            models.UserProfile(
                user_id=user.id,
                phone="+1 555 0100",
                location="Jersey City, New Jersey",
                linkedin="https://www.linkedin.com/in/ready-candidate",
                work_authorization="Authorized",
                sponsorship_status="Not required",
                salary_expectation="90000",
            )
        )
        stale = models.Resume(
            user_id=user.id,
            name="stale-demo.pdf",
            base_resume_text="stale",
            is_base=True,
        )
        current = models.Resume(
            user_id=user.id,
            name="current-base.pdf",
            base_resume_text="current",
            is_base=True,
        )
        nonbase = models.Resume(
            user_id=user.id,
            name="newer-tailored-copy.pdf",
            base_resume_text="",
            is_base=False,
        )
        db.add_all([stale, current, nonbase])
        db.flush()

        current_pdf = tmp_path / "current-base.pdf"
        current_pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 256)
        current_artifact = models.ResumeArtifact(
            resume_id=current.id,
            artifact_type="BASE_PDF",
            pdf_path=str(current_pdf),
            compiler_used="uploaded",
            compile_status="SUCCESS",
            file_size=current_pdf.stat().st_size,
        )
        posting = models.JobPosting(user_id=user.id, url="https://example.test/jobs/ready")
        db.add_all([current_artifact, posting])
        db.flush()
        version = models.ResumeVersion(job_posting_id=posting.id, resume_id=stale.id, tailored_latex="demo")
        db.add(version)
        db.flush()
        tailored_pdf = tmp_path / "newer-tailored.pdf"
        tailored_pdf.write_bytes(b"%PDF-1.4\n" + b"y" * 256)
        db.add(
            models.ResumeArtifact(
                resume_id=stale.id,
                resume_version_id=version.id,
                artifact_type="TAILORED_PDF",
                pdf_path=str(tailored_pdf),
                compiler_used="uploaded",
                compile_status="SUCCESS",
                file_size=tailored_pdf.stat().st_size,
            )
        )
        profile = _profile_readiness(db, user=user)
        resume = _resume_readiness(db, user=user)

        assert profile["mode"] == "complete"
        assert resume["mode"] == "available"
        assert resume["artifact_id"] == current_artifact.id
        assert resume["artifact_type"] == "BASE_PDF"
