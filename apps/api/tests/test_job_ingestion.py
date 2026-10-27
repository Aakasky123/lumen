from pathlib import Path

from sqlalchemy import select

from app import models
from app.db import SessionLocal, init_db
from app.job_extractors import ExtractedJob, html_to_text
from app.job_ingestion import IngestionStatus, JobIngestionService, classify_ingestion_url


FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "fixtures" / "job-pages"


def _fake_open_read_only(html: str, title: str = "Job page"):
    def fake(self: JobIngestionService, job_url: str) -> dict:
        artifact_paths = self._write_artifacts(
            job_url=job_url,
            raw_html=html,
            visible_text=html_to_text(html),
        )
        return {
            "title": title,
            "raw_html": html,
            "visible_text": html_to_text(html),
            "artifact_paths": artifact_paths,
            "open_confidence": 0.96,
        }

    return fake


def test_unsupported_url_policy_blocks_by_default() -> None:
    policy = classify_ingestion_url("https://example.com/jobs/backend-engineer")

    assert policy["allowed"] is False
    assert policy["reason"] == "Unsupported external job URL host: example.com"


def test_unsupported_url_ingest_stores_blocked_status() -> None:
    init_db()
    with SessionLocal() as db:
        result = JobIngestionService(db).ingest("https://example.com/jobs/backend-engineer")

        analysis = result["job_analysis"]
        posting = result["job_posting"]
        assert analysis.ingestion_status == IngestionStatus.BLOCKED_UNSUPPORTED_URL.value
        assert posting.requirements_json["ingestion_status"] == IngestionStatus.BLOCKED_UNSUPPORTED_URL.value
        event_types = [event.event_type for event in db.scalars(select(models.CognitiveEvent)).all()]
        assert "JOB_INGESTION_FAILED" in event_types


def test_greenhouse_hostname_allowlist_uses_normalized_suffix_matching() -> None:
    accepted_urls = [
        "https://job-boards.greenhouse.io/warp/jobs/123",
        "https://boards.greenhouse.io/example/jobs/123",
        "https://foo.job-boards.greenhouse.io/jobs/123",
        "https://job-boards.greenhouse.io./warp/jobs/123",
    ]
    for url in accepted_urls:
        policy = classify_ingestion_url(url)
        assert policy["allowed"] is True
        assert policy["platform"] == "greenhouse"

    for url in ["https://fakegreenhouse.io/jobs/123", "https://maliciousgreenhouse.io/jobs/123"]:
        policy = classify_ingestion_url(url)
        assert policy["allowed"] is False


def test_duplicate_url_updates_existing_record(monkeypatch, tmp_path) -> None:
    init_db()
    html = (FIXTURE_ROOT / "greenhouse_saved.html").read_text(encoding="utf-8")
    monkeypatch.setattr(JobIngestionService, "_open_read_only", _fake_open_read_only(html))
    with SessionLocal() as db:
        settings = JobIngestionService(db).settings
        old_root = settings.artifact_root
        settings.artifact_root = tmp_path
        try:
            service = JobIngestionService(db)
            first = service.ingest("https://boards.greenhouse.io/exampleco/jobs/12345")
            first_posting = first["job_posting"]
            first_id = first_posting.id
            first_posting.title = "Old Title"
            db.commit()
            second = service.ingest("https://boards.greenhouse.io/exampleco/jobs/12345")

            assert second["job_posting"].id == first_id
            assert second["job_posting"].title == "Backend Software Engineer"
        finally:
            settings.artifact_root = old_root


def test_duplicate_company_title_location_updates_existing_record(monkeypatch, tmp_path) -> None:
    init_db()
    html = (FIXTURE_ROOT / "greenhouse_saved.html").read_text(encoding="utf-8")
    monkeypatch.setattr(JobIngestionService, "_open_read_only", _fake_open_read_only(html))
    with SessionLocal() as db:
        settings = JobIngestionService(db).settings
        old_root = settings.artifact_root
        settings.artifact_root = tmp_path
        try:
            service = JobIngestionService(db)
            first = service.ingest("https://boards.greenhouse.io/exampleco/jobs/12345")
            first_id = first["job_posting"].id
            second = service.ingest("https://boards.greenhouse.io/exampleco/jobs/99999")

            assert second["job_posting"].id == first_id
            assert second["job_posting"].url == "https://boards.greenhouse.io/exampleco/jobs/99999"
        finally:
            settings.artifact_root = old_root


def test_ingest_stores_completed_job_analysis_artifacts_and_events(monkeypatch, tmp_path) -> None:
    init_db()
    html = (FIXTURE_ROOT / "lever_saved.html").read_text(encoding="utf-8")
    monkeypatch.setattr(
        JobIngestionService,
        "_open_read_only",
        _fake_open_read_only(html, title="Full Stack Software Engineer - LeverWorks"),
    )
    with SessionLocal() as db:
        result = JobIngestionService(db).ingest("https://jobs.lever.co/leverworks/abc123")

        posting = result["job_posting"]
        analysis = result["job_analysis"]
        extraction = posting.requirements_json["extraction"]
        assert analysis.ingestion_status == IngestionStatus.COMPLETED.value
        assert posting.source_platform == "lever"
        assert Path(extraction["raw_html_snapshot_path"]).exists()
        assert Path(extraction["visible_text_snapshot_path"]).exists()
        assert extraction["extractor_name"] == "lever_deterministic"
        assert extraction["extractor_version"]

        event_types = [event.event_type for event in db.scalars(select(models.CognitiveEvent)).all()]
        assert "REAL_SITE_READ_ONLY_MODE" in event_types
        assert "REAL_SITE_ACCESS_WARNING" in event_types
        assert "JOB_URL_OPENED_READ_ONLY" in event_types
        assert "PLATFORM_DETECTED" in event_types
        assert "JOB_EXTRACTED" in event_types


def test_weak_greenhouse_extraction_completes_degraded(monkeypatch, tmp_path) -> None:
    init_db()

    def weak_extract(**kwargs) -> ExtractedJob:
        return ExtractedJob(
            company=None,
            title=None,
            location=None,
            employment_type=None,
            salary_range=None,
            responsibilities=[],
            requirements=[],
            raw_visible_text=kwargs.get("visible_text") or "",
            source_url=kwargs["source_url"],
            source_platform="greenhouse",
            extraction_confidence=0.2,
            warnings=["synthetic weak extraction"],
            extractor_name="greenhouse_deterministic",
        )

    html = "<html><body><main><h1>Software Engineer</h1><p>Warp is hiring backend engineers for Python distributed systems work. This text is long enough to preserve degraded evidence.</p></main></body></html>"
    monkeypatch.setattr("app.job_ingestion.extract_job_from_html", weak_extract)
    monkeypatch.setattr(JobIngestionService, "_open_read_only", _fake_open_read_only(html))
    with SessionLocal() as db:
        settings = JobIngestionService(db).settings
        old_root = settings.artifact_root
        settings.artifact_root = tmp_path
        try:
            start_event_id = db.scalars(select(models.CognitiveEvent.id).order_by(models.CognitiveEvent.id.desc()).limit(1)).first() or 0
            result = JobIngestionService(db).ingest("https://job-boards.greenhouse.io/warp/jobs/123")

            posting = result["job_posting"]
            analysis = result["job_analysis"]
            assert analysis.ingestion_status == IngestionStatus.COMPLETED_DEGRADED.value
            assert posting.requirements_json["ingestion_status"] == IngestionStatus.COMPLETED_DEGRADED.value
            assert posting.requirements_json["extraction"]["status"] == IngestionStatus.COMPLETED_DEGRADED.value
            event_types = [event.event_type for event in db.scalars(select(models.CognitiveEvent).where(models.CognitiveEvent.id > start_event_id)).all()]
            assert "EXTRACTION_DEGRADED" in event_types
            assert "JOB_INGESTION_FAILED" not in event_types
        finally:
            settings.artifact_root = old_root


def test_no_usable_page_evidence_still_fails(monkeypatch) -> None:
    init_db()

    def fake_open_read_only(self: JobIngestionService, job_url: str) -> dict:
        artifact_paths = self._write_artifacts(job_url=job_url, raw_html="", visible_text="")
        return {
            "title": "",
            "raw_html": "",
            "visible_text": "",
            "artifact_paths": artifact_paths,
            "open_confidence": 0.96,
        }

    def raising_extractor(**kwargs):
        raise RuntimeError("empty page")

    monkeypatch.setattr(JobIngestionService, "_open_read_only", fake_open_read_only)
    monkeypatch.setattr("app.job_ingestion.extract_job_from_html", raising_extractor)
    with SessionLocal() as db:
        result = JobIngestionService(db).ingest("https://job-boards.greenhouse.io/warp/jobs/empty")

        assert result["job_analysis"].ingestion_status == IngestionStatus.FAILED.value
