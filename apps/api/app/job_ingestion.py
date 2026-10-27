from __future__ import annotations

import re
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.agents.job_understanding import detect_platform
from app.config import get_settings
from app.event_store import emit_event
from app.job_extractors import ExtractedJob, extract_job_from_html, html_to_text
from app.runtime import OperatorRuntimeEngine


class IngestionStatus(StrEnum):
    COMPLETED = "COMPLETED"
    COMPLETED_DEGRADED = "COMPLETED_DEGRADED"
    FAILED = "FAILED"
    BLOCKED_UNSUPPORTED_URL = "BLOCKED_UNSUPPORTED_URL"


ALLOWLIST_HOSTS = {"greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co", "lever.co"}


class JobIngestionService:
    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()
        self.runtime = OperatorRuntimeEngine(db)

    def ingest(self, job_url: str) -> dict:
        user = self.runtime.ensure_default_user()
        platform = detect_platform(job_url)
        policy = classify_ingestion_url(job_url, allow_generic_external=self.settings.allow_generic_external_job_ingestion)
        if not policy["allowed"]:
            posting = self._upsert_blocked_posting(user_id=user.id, job_url=job_url, platform=platform, reason=policy["reason"])
            analysis = self._upsert_analysis(posting, IngestionStatus.BLOCKED_UNSUPPORTED_URL, warnings=[policy["reason"]])
            emit_event(self.db, event_type="PLATFORM_DETECTED", payload={"job_posting_id": posting.id, "platform": platform, "url": job_url}, actor="job_ingestion", confidence=0.7)
            emit_event(self.db, event_type="JOB_INGESTION_FAILED", payload={"job_posting_id": posting.id, "status": analysis.ingestion_status, "reason": policy["reason"]}, actor="job_ingestion", confidence=0.9, risk_level="medium")
            self.db.commit()
            return {"job_posting": posting, "job_analysis": analysis}
        try:
            emit_event(self.db, event_type="REAL_SITE_READ_ONLY_MODE", payload={"url": job_url, "platform": platform}, actor="job_ingestion", confidence=1)
            if policy["external"]:
                emit_event(self.db, event_type="REAL_SITE_ACCESS_WARNING", payload={"url": job_url, "message": "External job page accessed in read-only mode only."}, actor="job_ingestion", confidence=1, risk_level="medium")
            page = self._open_read_only(job_url)
            emit_event(self.db, event_type="JOB_URL_OPENED_READ_ONLY", payload={"url": job_url, "title": page.get("title")}, actor="job_ingestion", confidence=page.get("open_confidence", 0.8))
            emit_event(self.db, event_type="PLATFORM_DETECTED", payload={"platform": platform, "url": job_url}, actor="job_ingestion", confidence=0.9)
            extracted = self._extract_or_degrade(
                source_url=job_url,
                html_text=page["raw_html"],
                visible_text=page["visible_text"],
                platform=platform,
                artifact_paths=page["artifact_paths"],
            )
            ingestion_status = self._ingestion_status_for_extraction(extracted)
            posting = self._upsert_posting(user_id=user.id, extracted=extracted, artifact_paths=page["artifact_paths"], ingestion_status=ingestion_status)
            emit_event(self.db, event_type="JOB_EXTRACTED", payload={"job_posting_id": posting.id, **self._safe_extracted_payload(extracted), "artifact_paths": page["artifact_paths"]}, actor="job_ingestion", confidence=extracted.extraction_confidence)
            analysis = self._upsert_analysis(posting, ingestion_status, warnings=extracted.warnings)
            self.db.commit()
            return {"job_posting": posting, "job_analysis": analysis}
        except Exception as exc:
            reason = str(exc) or repr(exc) or exc.__class__.__name__
            posting = self._upsert_blocked_posting(user_id=user.id, job_url=job_url, platform=platform, reason=reason, status=IngestionStatus.FAILED)
            analysis = self._upsert_analysis(posting, IngestionStatus.FAILED, warnings=[reason])
            emit_event(self.db, event_type="JOB_INGESTION_FAILED", payload={"job_posting_id": posting.id, "status": "FAILED", "reason": reason, "exception_type": exc.__class__.__name__}, actor="job_ingestion", confidence=0.5, risk_level="medium")
            self.db.commit()
            return {"job_posting": posting, "job_analysis": analysis}

    def _extract_or_degrade(
        self,
        *,
        source_url: str,
        html_text: str,
        visible_text: str,
        platform: str,
        artifact_paths: dict | None,
    ) -> ExtractedJob:
        try:
            extracted = extract_job_from_html(
                source_url=source_url,
                html_text=html_text,
                visible_text=visible_text,
                platform=platform,
            )
            if self._is_weak_extraction(extracted) and self._has_page_evidence(html_text=html_text, visible_text=visible_text):
                degraded = self._degraded_extraction(
                    source_url=source_url,
                    html_text=html_text,
                    visible_text=visible_text,
                    platform=platform,
                    error="Deterministic extraction returned low confidence or missing required fields.",
                    source_extracted=extracted,
                )
                self._emit_extraction_degraded(
                    source_url=source_url,
                    platform=platform,
                    html_text=html_text,
                    visible_text=visible_text,
                    artifact_paths=artifact_paths,
                    degraded=degraded,
                    reason="weak_structured_extraction",
                )
                return degraded
            return extracted
        except Exception as exc:
            if not self._has_page_evidence(html_text=html_text, visible_text=visible_text):
                raise
            degraded = self._degraded_extraction(source_url=source_url, html_text=html_text, visible_text=visible_text, platform=platform, error=str(exc))
            self._emit_extraction_degraded(
                source_url=source_url,
                platform=platform,
                html_text=html_text,
                visible_text=visible_text,
                artifact_paths=artifact_paths,
                degraded=degraded,
                reason="extractor_exception",
            )
            return degraded

    def _degraded_extraction(self, *, source_url: str, html_text: str, visible_text: str, platform: str, error: str, source_extracted: ExtractedJob | None = None) -> ExtractedJob:
        lines = [line.strip() for line in (visible_text or html_to_text(html_text)).splitlines() if line.strip()]
        title = source_extracted.title if source_extracted and source_extracted.title else (lines[0] if lines else None)
        parsed = urlparse(source_url)
        company = source_extracted.company if source_extracted and source_extracted.company else next((part.replace("-", " ").title() for part in parsed.path.split("/") if part and part.lower() not in {"jobs", "job"}), None)
        location = source_extracted.location if source_extracted and source_extracted.location else next((line for line in lines[:20] if any(token in line.lower() for token in ("remote", "new york", "san francisco", "united states", "hybrid"))), None)
        source_warnings = source_extracted.warnings if source_extracted else []
        responsibilities = source_extracted.responsibilities if source_extracted and source_extracted.responsibilities else lines[1:7]
        requirements = source_extracted.requirements if source_extracted and source_extracted.requirements else lines[7:15]
        preferred = source_extracted.preferred_skills if source_extracted and source_extracted.preferred_skills else []
        return ExtractedJob(
            company=company,
            title=title,
            location=location,
            employment_type=source_extracted.employment_type if source_extracted else None,
            salary_range=source_extracted.salary_range if source_extracted else None,
            responsibilities=responsibilities,
            requirements=requirements,
            preferred_skills=preferred,
            raw_visible_text=visible_text or html_to_text(html_text),
            source_url=source_url,
            source_platform=platform,
            extraction_confidence=0.28,
            warnings=[
                *source_warnings,
                "Deterministic extractor failed or returned weak structured data; degraded visible-text extraction was stored.",
                f"Extractor error: {error or 'unknown error'}",
            ],
            extractor_name=f"{platform}_degraded_visible_text",
        )

    def _emit_extraction_degraded(
        self,
        *,
        source_url: str,
        platform: str,
        html_text: str,
        visible_text: str,
        artifact_paths: dict | None,
        degraded: ExtractedJob,
        reason: str,
    ) -> None:
        emit_event(
            self.db,
            event_type="EXTRACTION_DEGRADED",
            payload={
                "source_url": source_url,
                "platform": platform,
                "reason": reason,
                "visible_text_length": len(visible_text or ""),
                "html_length": len(html_text or ""),
                "artifact_paths": artifact_paths or {},
                "extraction_confidence": degraded.extraction_confidence,
                "missing_title": not bool(degraded.title),
                "missing_company": not bool(degraded.company),
            },
            actor="job_ingestion",
            confidence=degraded.extraction_confidence,
            risk_level="medium",
        )

    def _has_page_evidence(self, *, html_text: str, visible_text: str) -> bool:
        return len(visible_text or "") >= 80 or len(html_text or "") >= 300

    def _open_read_only(self, job_url: str) -> dict:
        http_page = self._open_read_only_http(job_url)
        if http_page:
            return http_page
        self._write_artifacts(job_url=job_url, raw_html="", visible_text="")
        raise RuntimeError("Bounded HTTP read-only fetch returned no usable page evidence; browser fallback is disabled.")

    def _open_read_only_http(self, job_url: str) -> dict | None:
        request = Request(
            job_url,
            headers={
                "User-Agent": "Mozilla/5.0 LumenJobIngestion/1.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                content_type = response.headers.get("content-type") or ""
                if "text/html" not in content_type.lower():
                    return None
                body = response.read(2_000_000)
                encoding = response.headers.get_content_charset() or "utf-8"
                raw_html = body.decode(encoding, errors="replace")
        except Exception:
            return None
        visible_text = html_to_text(raw_html)
        if not self._has_page_evidence(html_text=raw_html, visible_text=visible_text):
            return None
        artifact_paths = self._write_artifacts(job_url=job_url, raw_html=raw_html, visible_text=visible_text)
        return {
            "title": _html_title(raw_html),
            "raw_html": raw_html,
            "visible_text": visible_text,
            "artifact_paths": artifact_paths,
            "open_confidence": 0.82,
            "open_status": "succeeded",
            "open_error": None,
            "open_method": "bounded_http_fetch",
        }

    def _upsert_posting(self, *, user_id: int, extracted: ExtractedJob, artifact_paths: dict, ingestion_status: IngestionStatus) -> models.JobPosting:
        existing = self._find_duplicate(user_id=user_id, extracted=extracted)
        posting = existing or models.JobPosting(user_id=user_id, url=extracted.source_url)
        if not existing:
            self.db.add(posting)
        posting.url = extracted.source_url
        posting.company = extracted.company
        posting.title = extracted.title
        posting.location = extracted.location
        posting.employment_type = extracted.employment_type
        posting.description_text = extracted.raw_visible_text[:12000]
        posting.source_platform = extracted.source_platform
        posting.requirements_json = {
            "responsibilities": extracted.responsibilities,
            "requirements": extracted.requirements,
            "preferred_skills": extracted.preferred_skills,
            "salary_range": extracted.salary_range,
            "source_url": extracted.source_url,
            "ingestion_status": ingestion_status.value,
            "extraction": {
                "status": ingestion_status.value,
                "confidence": extracted.extraction_confidence,
                "warnings": extracted.warnings,
                "extractor_name": extracted.extractor_name,
                "extractor_version": extracted.extractor_version,
                **artifact_paths,
            },
        }
        posting.skills_json = {
            "preferred": extracted.preferred_skills,
            "raw_detected": [],
        }
        self.db.flush()
        return posting

    def _upsert_blocked_posting(
        self,
        *,
        user_id: int,
        job_url: str,
        platform: str,
        reason: str,
        status: IngestionStatus = IngestionStatus.BLOCKED_UNSUPPORTED_URL,
    ) -> models.JobPosting:
        posting = self.db.scalars(select(models.JobPosting).where(models.JobPosting.user_id == user_id, models.JobPosting.url == job_url)).first()
        if not posting:
            posting = models.JobPosting(user_id=user_id, url=job_url)
            self.db.add(posting)
        posting.company = posting.company or None
        posting.title = posting.title or None
        posting.location = posting.location or None
        posting.description_text = posting.description_text or ""
        posting.source_platform = platform
        posting.requirements_json = {
            **(posting.requirements_json or {}),
            "source_url": job_url,
            "ingestion_status": status.value,
            "extraction": {"warnings": [reason], "confidence": 0, "extractor_name": None, "extractor_version": None},
        }
        self.db.flush()
        return posting

    def _upsert_analysis(self, posting: models.JobPosting, status: IngestionStatus, warnings: list[str]) -> models.JobAnalysis:
        analysis = self.db.scalars(select(models.JobAnalysis).where(models.JobAnalysis.job_posting_id == posting.id).order_by(models.JobAnalysis.id.desc())).first()
        if not analysis:
            analysis = models.JobAnalysis(job_posting_id=posting.id)
            self.db.add(analysis)
        analysis.ingestion_status = status.value
        analysis.warnings_json = dedupe(warnings)
        analysis.updated_at = models.utcnow()
        self.db.flush()
        return analysis

    def _find_duplicate(self, *, user_id: int, extracted: ExtractedJob) -> models.JobPosting | None:
        by_url = self.db.scalars(select(models.JobPosting).where(models.JobPosting.user_id == user_id, models.JobPosting.url == extracted.source_url)).first()
        if by_url:
            return by_url
        if not (extracted.company and extracted.title):
            return None
        postings = self.db.scalars(select(models.JobPosting).where(models.JobPosting.user_id == user_id)).all()
        target = (normalize_key(extracted.company), normalize_key(extracted.title), normalize_key(extracted.location or ""))
        for posting in postings:
            candidate = (normalize_key(posting.company or ""), normalize_key(posting.title or ""), normalize_key(posting.location or ""))
            if candidate == target:
                return posting
        return None

    def _write_artifacts(
        self,
        *,
        job_url: str,
        raw_html: str,
        visible_text: str,
    ) -> dict:
        artifact_dir = self._artifact_dir(job_url)
        raw_html_path = artifact_dir / "raw.html"
        visible_text_path = artifact_dir / "visible_text.txt"
        raw_html_path.write_text(raw_html, encoding="utf-8")
        visible_text_path.write_text(visible_text, encoding="utf-8")
        return {
            "raw_html_snapshot_path": str(raw_html_path),
            "visible_text_snapshot_path": str(visible_text_path),
        }

    def _artifact_dir(self, job_url: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", job_url)[:96].strip("_") or "job"
        path = self.settings.artifact_root / "job-ingestions" / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _safe_extracted_payload(self, extracted: ExtractedJob) -> dict:
        payload = asdict(extracted)
        payload["raw_visible_text"] = payload["raw_visible_text"][:1000]
        return payload

    def _is_weak_extraction(self, extracted: ExtractedJob) -> bool:
        title = (extracted.title or "").strip().lower()
        return (
            float(extracted.extraction_confidence or 0) < 0.5
            or not extracted.company
            or not extracted.title
            or title in {"untitled", "untitled role", "unknown role", "unknown"}
        )

    def _ingestion_status_for_extraction(self, extracted: ExtractedJob) -> IngestionStatus:
        if self._is_weak_extraction(extracted) or "degraded" in (extracted.extractor_name or ""):
            return IngestionStatus.COMPLETED_DEGRADED
        return IngestionStatus.COMPLETED


def classify_ingestion_url(job_url: str, *, allow_generic_external: bool = False) -> dict:
    parsed = urlparse(job_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    platform = detect_platform(job_url)
    local = host in {"localhost", "127.0.0.1", "::1"} or parsed.scheme == "file"
    allowlisted = any(host == allowed or host.endswith(f".{allowed}") for allowed in ALLOWLIST_HOSTS)
    if local:
        return {"allowed": True, "external": False, "platform": platform, "reason": None}
    if allowlisted and platform in {"greenhouse", "lever"}:
        return {"allowed": True, "external": True, "platform": platform, "reason": None}
    if allow_generic_external:
        return {"allowed": True, "external": True, "platform": platform, "reason": "Generic external ingestion explicitly enabled."}
    return {
        "allowed": False,
        "external": bool(host),
        "platform": platform,
        "reason": f"Unsupported external job URL host: {host or 'unknown'}",
    }


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _html_title(raw_html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", raw_html or "", flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip() or None


def dedupe(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        if not value or value.lower() in seen:
            continue
        seen.add(value.lower())
        result.append(value)
    return result
