import asyncio
import json
import logging
import re
import threading
from collections.abc import Iterator

from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import artifact_retention, closed_world_attestations, credential_vault, models, push, remote_access, screen_ocr, user_memory
from app.agent_settings import get_agent_settings, update_agent_settings
from app.application_artifacts import ApplicationArtifactService
from app.candidate_seed import CANDIDATE_SEED_PROVENANCE_KEY
from app.config import get_settings
from app.cognition import OperatorChatService, SensitiveBrowserTakeoverChatBlocked
from app.db import SessionLocal, get_db, init_db
from app.extension_hub import hub as extension_hub
from app.event_store import list_events
from app.mode_guard import compute_mode_report
from app.model_provider import VISION_OPENAI_COMPATIBLE_TYPES, VISION_PROVIDER_TYPES, ModelProviderService
from app.model_provider_health import clear_model_provider_health_cache, get_model_provider_health
from app.runtime import OperatorRuntimeEngine
from app.schemas import (
    AgentSettingsUpdate,
    AnswerMemoryUpdate,
    BenchLanePinRequest,
    ChatMessageCreate,
    ChatMessageOut,
    ChatThreadCreate,
    ChatThreadOut,
    CognitiveEventOut,
    ClosedWorldAttestationCreate,
    ClosedWorldAttestationEnvelope,
    ClosedWorldAttestationListOut,
    CredentialVaultCreate,
    CredentialVaultDeleteOut,
    CredentialVaultItemOut,
    CredentialVaultListOut,
    CredentialVaultUpdate,
    DevicePairRequest,
    DevicePushTokenUpdate,
    MemoryFactCreate,
    MemoryFactUpdate,
    PairedDeviceOut,
    ApplicationArtifactOut,
    ApplyQuestionArtifactRequest,
    GenerateQuestionArtifactRequest,
    UpdateApplicationArtifactRequest,
    ModelProviderAvailableModels,
    ModelProviderCreate,
    OperatorQuestionAnswer,
    OperatorQuestionOut,
    OperatorRunOut,
    ProfileUpsert,
    SensitiveBrowserContinueRequest,
)


logger = logging.getLogger(__name__)


def _raise_credential_vault_http_error(exc: credential_vault.CredentialVaultError) -> None:
    if isinstance(exc, credential_vault.CredentialVaultConfigurationError):
        status_code = 503
    elif isinstance(exc, credential_vault.CredentialVaultNotFoundError):
        status_code = 404
    elif isinstance(exc, credential_vault.CredentialVaultConflictError):
        status_code = 409
    elif isinstance(exc, credential_vault.CredentialVaultAccessError):
        status_code = 403
    elif isinstance(exc, credential_vault.CredentialVaultCorruptError):
        status_code = 500
    else:
        status_code = 400
    raise HTTPException(status_code=status_code, detail=str(exc)) from exc


app = FastAPI(title="Lumen AI Operator Runtime", version="0.1.0")


@app.exception_handler(RequestValidationError)
async def credential_vault_validation_error_handler(
    request: Request,
    exc: RequestValidationError,
):
    if not request.url.path.startswith("/credential-vault"):
        return await request_validation_exception_handler(request, exc)
    sanitized_errors = [
        {
            "type": str(error.get("type") or "value_error"),
            "loc": list(error.get("loc") or []),
            "msg": str(error.get("msg") or "Invalid value."),
        }
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": sanitized_errors})


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3210",
        "http://127.0.0.1:3210",
        "http://localhost:3211",
        "http://127.0.0.1:3211",
        "http://localhost:3212",
        "http://127.0.0.1:3212",
        "http://localhost:3213",
        "http://127.0.0.1:3213",
        "http://localhost:3214",
        "http://127.0.0.1:3214",
        "http://localhost:3215",
        "http://127.0.0.1:3215",
        "http://localhost:3216",
        "http://127.0.0.1:3216",
        "http://localhost:3217",
        "http://127.0.0.1:3217",
        "http://localhost:3218",
        "http://127.0.0.1:3218",
        "http://localhost:3219",
        "http://127.0.0.1:3219",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup() -> None:
    init_db()
    # Chat endpoints execute in FastAPI's worker threadpool. Retain the owning event loop so both
    # the local Windows body and the Chrome WebSocket body can schedule the same async vision loop.
    extension_hub.loop = asyncio.get_running_loop()
    # Every RUNNING run at startup is orphaned - its drive coroutine died with the
    # previous process. Reconcile them into resumable blocks so they stop claiming a
    # busy body and never replay stale pending actions.
    try:
        with SessionLocal() as reconcile_db:
            reconciled = OperatorRuntimeEngine(reconcile_db).reconcile_orphaned_runs_after_restart()
            if reconciled:
                logger.info("Reconciled orphaned runs after restart: %s", reconciled)
    except Exception:  # noqa: BLE001 - reconciliation must never block startup
        logger.exception("Orphaned-run reconciliation failed at startup")
    try:
        with SessionLocal() as resume_db:
            user = OperatorRuntimeEngine(resume_db).ensure_default_user()
            if ApplicationArtifactService(resume_db).promote_latest_resume_if_no_base(user.id) is not None:
                resume_db.commit()
    except Exception:  # noqa: BLE001 - a repair must never block startup
        logger.exception("Base resume repair failed at startup")
    push.install()
    settings = get_settings()
    artifact_retention.start_background_pruning(
        Path(settings.artifact_root), keep_runs=settings.frame_keep_runs, keep_days=settings.frame_keep_days
    )
    # Paired phones reach a second, gated listener (app.remote_access); local 8001 is unchanged.
    remote_access.start_remote_listener(app)
    # Vision's first recognition loads its model (seconds); pay it off the request path. No-op off macOS.
    threading.Thread(target=screen_ocr.warm_up, name="ocr-warm-up", daemon=True).start()


@app.on_event("shutdown")
async def shutdown() -> None:
    await remote_access.stop_remote_listener()


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    return {"status": "ok", "service": "lumen-operator-runtime", "mode": compute_mode_report(db=db).as_dict()}


@app.get("/eval/ablation")
def eval_ablation_status(registry: bool = False) -> dict:
    """The eval-only rule ablation this process runs under (app.eval_ablation); inactive in production."""
    from app import eval_ablation

    return {**eval_ablation.status(), **({"registry": eval_ablation.registry()} if registry else {})}


@app.get("/eval/pages/{name}")
def eval_page(name: str) -> FileResponse:
    """Controlled local pages for ChromeExtensionBody evals (download/dialog/job sim) — real browser
    mechanics against pages we author, not the live web. Whitelisted by filename."""
    pages_dir = Path(__file__).parent / "agent_eval" / "local_pages"
    path = pages_dir / f"{re.sub(r'[^a-z0-9_-]', '', name.lower())}.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"No eval page {name!r}.")
    return FileResponse(path, media_type="text/html")


@app.get("/eval/bench/{name}")
def interaction_bench_page(name: str) -> FileResponse:
    """Lumen Interaction Gauntlet pages (see app.agent_eval.interaction_bench) — adversarial,
    self-grading gauntlets for page handling / scroll / click / type / forms / computer use.
    Whitelisted by filename, same policy as /eval/pages."""
    pages_dir = Path(__file__).parent / "agent_eval" / "interaction_bench" / "pages"
    path = pages_dir / f"{re.sub(r'[^a-z0-9_-]', '', name.lower())}.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"No interaction bench page {name!r}.")
    return FileResponse(path, media_type="text/html")


@app.post("/eval/benchmarks/webbench")
async def eval_webbench_benchmark(payload: dict = Body(default={})) -> dict:
    """Launch a WebBench-shaped benchmark run in-process on the production Chrome body.

    The extension hub lives in this process, so this is the only place a chrome-body
    benchmark can execute. Runs in the background; results (result.json per run,
    per-task artifacts) land under artifact_root/eval_runs/. Poll the returned
    result_file for completion."""
    from datetime import datetime, timezone

    from app.agent_eval.brain import LlmBrain
    from app.agent_eval.webbench_runner import run_webbench_manifest
    from app.lumen_model import LumenModelClient

    manifest_path = Path(str(payload.get("manifest_path") or "")).expanduser()
    if not manifest_path.is_file():
        raise HTTPException(status_code=400, detail="manifest_path must point to a manifest JSON file")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    output_dir = Path(get_settings().artifact_root) / "eval_runs" / f"webbench-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    body_kind = str(payload.get("body_kind") or "chrome")
    parameters = {
        "task_ids": payload.get("task_ids"),
        "limit": payload.get("limit"),
        "max_steps": int(payload.get("max_steps") or 10),
        "allow_write": bool(payload.get("allow_write")),
        "timeout_seconds": int(payload.get("timeout_seconds") or 180),
        "provider_id": payload.get("provider_id"),
    }

    async def _run() -> None:
        try:
            with SessionLocal() as run_db:
                model = LumenModelClient(run_db)
                provider = (
                    model.provider_service.provider_by_id(int(parameters["provider_id"]))
                    if parameters["provider_id"]
                    else None
                )
                brain = LlmBrain(model, provider=provider)
                await run_webbench_manifest(
                    manifest_path=manifest_path,
                    output_dir=output_dir,
                    brain=brain,
                    task_ids=parameters["task_ids"],
                    limit=parameters["limit"],
                    max_steps=parameters["max_steps"],
                    allow_write=parameters["allow_write"],
                    timeout_seconds=parameters["timeout_seconds"],
                    body_kind=body_kind,
                )
        except Exception as exc:  # noqa: BLE001 - the failure IS the recorded result
            (output_dir / "error.json").write_text(
                json.dumps({"error": str(exc)}, indent=2), encoding="utf-8"
            )

    asyncio.get_running_loop().create_task(_run())
    return {
        "started": True,
        "body_kind": body_kind,
        "output_dir": str(output_dir),
        "result_file": str(output_dir / "result.json"),
        "error_file": str(output_dir / "error.json"),
    }


@app.get("/health/model")
def health_model(refresh: bool = Query(default=False), db: Session = Depends(get_db)) -> dict:
    return get_model_provider_health(db=db, refresh=refresh, probe=True)


@app.get("/model-providers")
def list_model_providers(db: Session = Depends(get_db)) -> dict:
    return ModelProviderService(db).list_public()


@app.post("/model-providers/available-models")
def model_provider_available_models(payload: ModelProviderAvailableModels, db: Session = Depends(get_db)) -> dict:
    """Query a provider for the models the given key can use, so the UI can offer a real dropdown."""
    try:
        models = ModelProviderService(db).available_models(
            provider_type=payload.provider_type or "",
            api_key=payload.api_key or "",
            base_url=payload.base_url or "",
            key_from_provider_id=payload.key_from_provider_id,
        )
        return {"models": models}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/model-providers")
def create_model_provider(payload: ModelProviderCreate, db: Session = Depends(get_db)) -> dict:
    try:
        service = ModelProviderService(db)
        provider = service.create_provider(payload.model_dump())
        db.commit()
        clear_model_provider_health_cache()
        return {"provider": service.public_provider(provider), "active": service.active_provider().public_dict()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/model-providers/{provider_id}/activate")
def activate_model_provider(provider_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        service = ModelProviderService(db)
        provider = service.activate_provider(provider_id)
        # The active row is the Brain: a row that can carry a screenshot also becomes the default user's
        # vision_provider_id, so the two never diverge (update_agent_settings keeps the seat invariants).
        if str(provider.provider_type or "").strip().casefold() in (VISION_PROVIDER_TYPES | VISION_OPENAI_COMPATIBLE_TYPES):
            user = OperatorRuntimeEngine(db).ensure_default_user()
            update_agent_settings(db, user.id, {"vision_provider_id": provider.id})
        db.commit()
        clear_model_provider_health_cache()
        return {"provider": service.public_provider(provider), "active": service.active_provider().public_dict()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _safe_filename(value: str, *, default: str) -> str:
    name = Path(value or default).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or default


@app.get("/resume-artifacts/{artifact_id}/download")
def download_resume_artifact(artifact_id: int, db: Session = Depends(get_db)) -> FileResponse:
    artifact = db.get(models.ResumeArtifact, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Resume artifact not found")
    path = Path(artifact.pdf_path or "")
    if artifact.artifact_type not in {"BASE_PDF", "TAILORED_PDF"} or not path.exists():
        raise HTTPException(status_code=404, detail="PDF artifact is not available")
    ext = path.suffix.lower() or ".pdf"
    media_type = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
    }.get(ext, "application/octet-stream")
    if artifact.artifact_type == "TAILORED_PDF":
        filename = _safe_filename(path.name, default=f"resume-artifact-{artifact.id}{ext}")
    else:
        filename = _safe_filename(f"lumen-resume-{artifact.id}{ext}", default=f"resume-artifact-{artifact.id}{ext}")
    return FileResponse(path, media_type=media_type, filename=filename)


@app.get("/artifacts", response_model=list[ApplicationArtifactOut])
def list_application_artifacts(db: Session = Depends(get_db)) -> list[models.ApplicationArtifact]:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    return ApplicationArtifactService(db).list_active(user_id=user.id)


@app.post("/artifacts/preview")
async def preview_application_artifact(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    preview = await ApplicationArtifactService(db).preview_upload(user_id=user.id, file=file)
    db.commit()
    return preview


@app.post("/artifacts", response_model=ApplicationArtifactOut)
def commit_application_artifact(
    draft_id: str = Form(...),
    selected_type: str = Form(...),
    display_name: str | None = Form(default=None),
    db: Session = Depends(get_db),
) -> models.ApplicationArtifact:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    artifact = ApplicationArtifactService(db).commit_preview(
        user_id=user.id,
        draft_id=draft_id,
        selected_type=selected_type,
        display_name=display_name,
        source="uploaded",
    )
    db.commit()
    return artifact


@app.get("/artifacts/{artifact_id}/download")
def download_application_artifact(artifact_id: int, db: Session = Depends(get_db)) -> FileResponse:
    artifact = ApplicationArtifactService(db).get_active(artifact_id)
    user = OperatorRuntimeEngine(db).ensure_default_user()
    if artifact.user_id != user.id:
        raise HTTPException(status_code=404, detail="Artifact not found")
    path = Path(artifact.storage_path or "")
    root = get_settings().artifact_root.resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="Artifact file is not available") from exc
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Artifact file is not available")
    return FileResponse(resolved, filename=artifact.filename, media_type=artifact.mime_type or "application/octet-stream")


@app.patch("/artifacts/{artifact_id}", response_model=ApplicationArtifactOut)
def update_application_artifact(artifact_id: int, payload: UpdateApplicationArtifactRequest, db: Session = Depends(get_db)) -> models.ApplicationArtifact:
    service = ApplicationArtifactService(db)
    artifact = service.get_active(artifact_id)
    user = OperatorRuntimeEngine(db).ensure_default_user()
    if artifact.user_id != user.id:
        raise HTTPException(status_code=404, detail="Artifact not found")
    updated = service.update_text(artifact_id, text_content=payload.text_content or "")
    db.commit()
    return updated


@app.delete("/artifacts/{artifact_id}")
def delete_application_artifact(artifact_id: int, db: Session = Depends(get_db)) -> dict:
    service = ApplicationArtifactService(db)
    artifact = service.get_active(artifact_id)
    user = OperatorRuntimeEngine(db).ensure_default_user()
    if artifact.user_id != user.id:
        raise HTTPException(status_code=404, detail="Artifact not found")
    result = service.delete(artifact_id)
    db.commit()
    return result


@app.get("/operator/runs", response_model=list[OperatorRunOut])
def list_runs(db: Session = Depends(get_db)) -> list[models.OperatorRun]:
    return list(db.scalars(select(models.OperatorRun).order_by(models.OperatorRun.id.desc()).limit(20)).all())


@app.get("/operator/runs/{run_id}", response_model=OperatorRunOut)
def get_run(run_id: int, db: Session = Depends(get_db)) -> models.OperatorRun:
    run = db.get(models.OperatorRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/operator/runs/{run_id}/screenshot/latest")
def get_latest_run_screenshot(run_id: int, db: Session = Depends(get_db)) -> FileResponse:
    """The most recent screenshot exactly as the model saw it (masked at capture)."""
    if db.get(models.OperatorRun, run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    latest = _latest_model_screenshot(run_id)
    if latest is None:
        raise HTTPException(status_code=404, detail="No screenshot yet")
    path, step = latest
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Lumen-Step": str(step)})


@app.get("/operator/runs/{run_id}/downloads/{token}/{name}")
def get_run_download(run_id: int, token: str, name: str) -> FileResponse:
    """A completed file this active run downloaded, for the browser body to attach to a page (upload with
    a downloads path). Needs the run's own downloads token; local only (not on the remote route table)."""
    import mimetypes

    from app.extension_run import run_download_file_for_token

    path = run_download_file_for_token(run_id, token, name)
    if path is None:
        raise HTTPException(status_code=404, detail="Download not found")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name, headers={"Cache-Control": "no-store"})


def _latest_model_screenshot(run_id: int) -> tuple[Path, int] | None:
    run_dir = (Path(get_settings().artifact_root) / "runs" / str(int(run_id)) / "vision-trajectory").resolve()
    manifest = run_dir / "model-input-manifest.jsonl"
    if not manifest.is_file():
        return None
    with manifest.open("rb") as handle:
        handle.seek(0, 2)
        handle.seek(max(0, handle.tell() - 65536))
        lines = handle.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        candidate = Path(str(record.get("model_screenshot_path") or "")).resolve()
        if candidate.parent == run_dir and candidate.is_file():
            return candidate, int(record.get("step") or 0)
    return None


@app.get("/operator/runs/{run_id}/events", response_model=list[CognitiveEventOut])
def get_events(run_id: int, after_id: int | None = None, db: Session = Depends(get_db)) -> list[models.CognitiveEvent]:
    return list_events(db, run_id, after_id)


@app.get("/operator/metrics")
def get_operator_metrics(
    limit: int = Query(default=25, ge=1, le=500),
    since: str | None = None,
    pages: str | None = None,
    attempts: int = Query(default=5, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict:
    """Metrics across runs (`app.run_metrics`), the JSON `python -m app.agent_eval.metrics --json` prints.
    Read-only; local only (not on the remote route table)."""
    from app import run_metrics

    try:
        since_at = run_metrics.parse_since(since)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return run_metrics.build_report(
        db.connection(),
        Path(get_settings().artifact_root) / "eval_runs",
        limit=limit,
        since=since_at,
        pages=run_metrics.parse_pages(pages),
        trend_attempts=attempts,
    )


@app.post("/operator/runs/{run_id}/pause", response_model=OperatorRunOut)
def pause_run(run_id: int, db: Session = Depends(get_db)) -> models.OperatorRun:
    try:
        return OperatorRuntimeEngine(db).pause_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/operator/runs/{run_id}/resume", response_model=OperatorRunOut)
def resume_run(run_id: int, db: Session = Depends(get_db)) -> models.OperatorRun:
    try:
        return OperatorRuntimeEngine(db).resume_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/operator/runs/{run_id}/sensitive-browser-continue", response_model=OperatorRunOut)
def continue_sensitive_browser_entry(
    run_id: int,
    payload: SensitiveBrowserContinueRequest,
    db: Session = Depends(get_db),
) -> models.OperatorRun:
    """Resume only from the explicit text-free direct-entry control."""

    from app.extension_run import continue_sensitive_browser_takeover

    try:
        return continue_sensitive_browser_takeover(
            run_id,
            handoff_id=payload.handoff_id,
            db=db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/operator/runs/{run_id}/cancel", response_model=OperatorRunOut)
def cancel_run(run_id: int, db: Session = Depends(get_db)) -> models.OperatorRun:
    try:
        return OperatorRuntimeEngine(db).cancel_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/agent-settings")
def get_agent_settings_endpoint(db: Session = Depends(get_db)) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    return {"settings": get_agent_settings(db, user.id)}


@app.patch("/agent-settings")
def update_agent_settings_endpoint(payload: AgentSettingsUpdate, request: Request, db: Session = Depends(get_db)) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    try:
        # Setting the Brain activates its row; a row that cannot be active (disabled, no key) is refused.
        settings = update_agent_settings(db, user.id, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    remote = remote_access.remote_context(request)
    if remote and remote.get("step_up_verified") and remote.get("device_id"):
        # Turning Full autonomy on from a phone is itself fingerprint-signed, so it also unlocks the phone.
        device = db.get(models.PairedDevice, int(remote["device_id"]))
        if device is not None:
            if settings.get("final_action_mode") == "autonomous":
                remote_access.unlock_autonomy(device)
            else:
                device.autonomy_until = None
    db.commit()
    if "vision_provider_id" in payload.model_fields_set:
        clear_model_provider_health_cache()
    return {"settings": settings}


@app.get("/credential-vault", response_model=CredentialVaultListOut)
def list_credential_vault_items(
    origin: str | None = Query(default=None, max_length=512),
    db: Session = Depends(get_db),
) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    try:
        items = (
            credential_vault.list_metadata_for_origin(db, user_id=user.id, origin=origin)
            if origin is not None
            else credential_vault.list_metadata(db, user_id=user.id)
        )
        return {"items": items}
    except credential_vault.CredentialVaultError as exc:
        _raise_credential_vault_http_error(exc)


@app.post("/credential-vault", response_model=CredentialVaultItemOut)
def create_credential_vault_item(
    payload: CredentialVaultCreate,
    db: Session = Depends(get_db),
) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    try:
        item = credential_vault.create(
            db,
            user_id=user.id,
            origin=payload.origin,
            account_label=payload.account_label,
            fields=payload.fields.revealed(),
            auto_use=payload.auto_use,
        )
        db.commit()
        db.refresh(item)
        return credential_vault.metadata_for_item(item)
    except credential_vault.CredentialVaultError as exc:
        db.rollback()
        _raise_credential_vault_http_error(exc)


@app.patch("/credential-vault/{item_id}", response_model=CredentialVaultItemOut)
def update_credential_vault_item(
    item_id: int,
    payload: CredentialVaultUpdate,
    db: Session = Depends(get_db),
) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    try:
        item = credential_vault.update(
            db,
            user_id=user.id,
            item_id=item_id,
            origin=payload.origin if "origin" in payload.model_fields_set else None,
            account_label=payload.account_label if "account_label" in payload.model_fields_set else None,
            fields=payload.fields.revealed() if payload.fields is not None else None,
            auto_use=payload.auto_use if "auto_use" in payload.model_fields_set else None,
        )
        db.commit()
        db.refresh(item)
        return credential_vault.metadata_for_item(item)
    except credential_vault.CredentialVaultError as exc:
        db.rollback()
        _raise_credential_vault_http_error(exc)


@app.delete("/credential-vault/{item_id}", response_model=CredentialVaultDeleteOut)
def delete_credential_vault_item(item_id: int, db: Session = Depends(get_db)) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    try:
        credential_vault.delete(db, user_id=user.id, item_id=item_id)
        db.commit()
        return {"deleted": True, "id": item_id}
    except credential_vault.CredentialVaultError as exc:
        db.rollback()
        _raise_credential_vault_http_error(exc)


@app.post("/profiles")
def upsert_profile(payload: ProfileUpsert, db: Session = Depends(get_db)) -> dict:
    engine = OperatorRuntimeEngine(db)
    user = engine.ensure_default_user()
    user.email = payload.email
    user.name = payload.name
    profile = db.scalars(select(models.UserProfile).where(models.UserProfile.user_id == user.id)).first()
    if not profile:
        profile = models.UserProfile(user_id=user.id)
        db.add(profile)
    profile_payload = payload.model_dump(exclude={"name", "email"})
    profile_json = closed_world_attestations.preserve_server_owned_profile_json(
        profile.profile_json,
        profile_payload.get("profile_json"),
    )
    profile_payload["profile_json"] = profile_json
    if not engine._is_placeholder_email(payload.email):
        provenance = profile_json.get(CANDIDATE_SEED_PROVENANCE_KEY)
        if isinstance(provenance, dict) and provenance.get("label") == "onboarding_default":
            profile_json.pop(CANDIDATE_SEED_PROVENANCE_KEY, None)
        profile_payload["profile_json"] = profile_json
    for field, value in profile_payload.items():
        setattr(profile, field, value)
    # The profile memory is the single store: standard fields set here land there too, so the
    # user sees them in About you and the mirror keeps them.
    for key in user_memory.PROFILE_COLUMN_KEYS:
        value = str(profile_payload.get(key) or "").strip()
        if value:
            user_memory.remember(user.id, key, value, source_note="added by you")
    db.commit()
    return {"user_id": user.id, "profile_id": profile.id, "status": "saved"}


@app.get(
    "/profiles/me/closed-world-attestations",
    response_model=ClosedWorldAttestationListOut,
)
def list_closed_world_attestations(db: Session = Depends(get_db)) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    return {"items": closed_world_attestations.list_for_user(db, user_id=user.id)}


@app.post(
    "/profiles/me/closed-world-attestations",
    response_model=ClosedWorldAttestationEnvelope,
)
def create_closed_world_attestation(
    payload: ClosedWorldAttestationCreate,
    db: Session = Depends(get_db),
) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    try:
        attestation = closed_world_attestations.create(
            db,
            user_id=user.id,
            scope=payload.scope,
            set_key=payload.set_key,
            members=payload.members,
        )
        db.commit()
        return {"attestation": attestation}
    except closed_world_attestations.ClosedWorldAttestationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/profiles/me/closed-world-attestations/{attestation_id}/revoke",
    response_model=ClosedWorldAttestationEnvelope,
)
def revoke_closed_world_attestation(
    attestation_id: str,
    db: Session = Depends(get_db),
) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    try:
        attestation = closed_world_attestations.revoke(
            db,
            user_id=user.id,
            attestation_id=attestation_id,
        )
        db.commit()
        return {"attestation": attestation}
    except closed_world_attestations.ClosedWorldAttestationNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/profiles/me")
def get_profile(db: Session = Depends(get_db)) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    profile = db.scalars(select(models.UserProfile).where(models.UserProfile.user_id == user.id)).first()
    answer_memories = list(
        db.scalars(
            select(models.AnswerMemory)
            .where(models.AnswerMemory.user_id == user.id)
            .order_by(models.AnswerMemory.last_used_at.desc().nullslast(), models.AnswerMemory.id.desc())
        ).all()
    )
    return {
        "user": {"id": user.id, "email": user.email, "name": user.name},
        "profile": {
            "phone": profile.phone,
            "location": profile.location,
            "linkedin": profile.linkedin,
            "github": profile.github,
            "portfolio": profile.portfolio,
            "work_authorization": profile.work_authorization,
            "sponsorship_status": profile.sponsorship_status,
            "salary_expectation": profile.salary_expectation,
            "relocation_preference": profile.relocation_preference,
            "profile_json": profile.profile_json,
        }
        if profile
        else None,
        "common_answers": [
            {
                "id": memory.id,
                "question_pattern": memory.question_pattern,
                "answer": memory.approved_answer,
                "confidence": memory.confidence,
                "last_used_at": memory.last_used_at.isoformat() if memory.last_used_at else None,
            }
            for memory in answer_memories
        ],
    }


@app.patch("/operator/answers/{answer_id}")
def update_answer_memory(answer_id: int, payload: AnswerMemoryUpdate, db: Session = Depends(get_db)) -> dict:
    """Edit a remembered application answer from the profile UI."""
    user = OperatorRuntimeEngine(db).ensure_default_user()
    memory = db.get(models.AnswerMemory, answer_id)
    if not memory or memory.user_id != user.id:
        raise HTTPException(status_code=404, detail="Saved answer not found.")
    memory.approved_answer = payload.answer
    db.commit()
    return {"id": memory.id, "answer": memory.approved_answer}


@app.delete("/operator/answers/{answer_id}")
def delete_answer_memory(answer_id: int, db: Session = Depends(get_db)) -> dict:
    """Forget a remembered application answer from the profile UI."""
    user = OperatorRuntimeEngine(db).ensure_default_user()
    memory = db.get(models.AnswerMemory, answer_id)
    if not memory or memory.user_id != user.id:
        raise HTTPException(status_code=404, detail="Saved answer not found.")
    db.delete(memory)
    db.commit()
    return {"deleted": True, "id": answer_id}


@app.post("/operator/chat/threads", response_model=ChatThreadOut)
def create_chat_thread(payload: ChatThreadCreate, db: Session = Depends(get_db)) -> models.ChatThread:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    thread = OperatorChatService(db).create_thread(user_id=user.id, title=payload.title)
    db.commit()
    return thread


@app.get("/operator/chat/threads", response_model=list[ChatThreadOut])
def list_chat_threads(run_id: int | None = None, db: Session = Depends(get_db)) -> list[models.ChatThread]:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    return OperatorChatService(db).list_threads(user_id=user.id, run_id=run_id)


@app.delete("/operator/chat/threads/{thread_id}", response_model=ChatThreadOut)
def delete_chat_thread(thread_id: int, db: Session = Depends(get_db)) -> models.ChatThread:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    try:
        thread = OperatorChatService(db).delete_thread(user_id=user.id, thread_id=thread_id)
        db.commit()
        return thread
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/operator/chat/threads/{thread_id}/messages", response_model=list[ChatMessageOut])
def list_chat_messages(thread_id: int, db: Session = Depends(get_db)) -> list[models.ChatMessage]:
    try:
        return OperatorChatService(db).list_messages(thread_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc



def _chat_sse_frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _capture_pending_chat_launch(
    service: OperatorChatService | None,
    pending_launch: dict[str, int],
) -> None:
    launch = getattr(service, "_pending_deferred_launch", None) if service else None
    if launch:
        pending_launch["run_id"], pending_launch["thread_id"] = launch


@app.post("/operator/chat/threads/{thread_id}/messages")
def post_chat_message(thread_id: int, payload: ChatMessageCreate) -> StreamingResponse:
    # Server-Sent Events: a single `meta` frame (persisted user message + scope + action
    # side-effects), `token` frames as the reply streams, then a terminal `done` frame with the
    # persisted assistant message. The session is owned by the generator (not Depends) so it stays
    # open for the full stream lifetime.
    # Populated mid-stream when the turn starts a deferred live application. The run is
    # launched as a post-response BackgroundTask (below) — NOT mid-stream — so the chat
    # request fully completes and releases its SQLite writes before the long run begins.
    pending_launch: dict[str, int] = {}

    # Reject before returning a streaming response, and therefore before creating any durable
    # ChatMessage. The generator repeats this check to close the race with a takeover that becomes
    # active between this preflight and the first streamed turn.
    with SessionLocal() as preflight_db:
        try:
            OperatorChatService(preflight_db).assert_chat_message_allowed(thread_id)
        except SensitiveBrowserTakeoverChatBlocked as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def event_stream() -> Iterator[str]:
        db = SessionLocal()
        service: OperatorChatService | None = None
        try:
            service = OperatorChatService(db)
            for kind, data in service.stream_message(thread_id=thread_id, content=payload.content, payload=payload.payload):
                # The service publishes a durable deferred-run handoff before its first frame.
                # Copy it before formatting/yielding that frame so later chat-stream failures
                # cannot suppress the already-authorized browser run.
                _capture_pending_chat_launch(service, pending_launch)
                if kind == "meta":
                    yield _chat_sse_frame(
                        "meta",
                        {
                            "user_message": ChatMessageOut.model_validate(data["user_message"]).model_dump(mode="json"),
                            "chat_scope": data["chat_scope"],
                            "pending_question": OperatorQuestionOut.model_validate(data["pending_question"]).model_dump(mode="json") if data["pending_question"] else None,
                            "task_request": data["task_request"],
                            "application_request": data["application_request"],
                            "run_control": data["run_control"],
                            "runtime_correction": data["runtime_correction"],
                            "question_answer": data["question_answer"],
                        },
                    )
                elif kind == "token":
                    yield _chat_sse_frame("token", {"text": data})
                elif kind == "done":
                    yield _chat_sse_frame(
                        "done",
                        {
                            "assistant_message": ChatMessageOut.model_validate(data["assistant_message"]).model_dump(mode="json"),
                            "context": data["context"],
                            "chat_response": data["chat_response"],
                        },
                    )
        except SensitiveBrowserTakeoverChatBlocked as exc:
            yield _chat_sse_frame("error", {"detail": str(exc), "status": 409})
        except ValueError as exc:
            yield _chat_sse_frame("error", {"detail": str(exc), "status": 404})
        except Exception:  # noqa: BLE001 - surface a terminal error frame instead of a dropped stream
            logger.exception(
                "Lumen chat stream failed after durable turn preparation (thread_id=%s).",
                thread_id,
            )
            yield _chat_sse_frame("error", {"detail": "Lumen chat stream failed.", "status": 500})
        finally:
            # Also capture on generator close/client disconnect. Background dispatch remains
            # post-response, after this session is closed, preserving the SQLite lock boundary.
            _capture_pending_chat_launch(service, pending_launch)
            db.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        background=BackgroundTask(_launch_pending_chat_application, pending_launch),
    )


def _launch_pending_chat_application(pending_launch: dict[str, int]) -> None:
    """Run a chat-started deferred live application AFTER the streaming response has
    fully closed (and the request's SQLite writes are released). Mirrors the proven
    end-to-end background pattern; never launched mid-stream, which previously
    deadlocked the in-process SQLite write lock and hung the server."""
    run_id = pending_launch.get("run_id")
    thread_id = pending_launch.get("thread_id")
    if not run_id or not thread_id:
        return
    # Live application runs must go through the user's connected extension body. When the body is
    # absent or busy, route_deferred_chat_run marks the run blocked/busy and posts the chat notice.
    from app.extension_run import route_deferred_chat_run

    route_deferred_chat_run(int(run_id), int(thread_id))


@app.websocket("/extension/ws")
async def extension_ws(websocket: WebSocket) -> None:
    """Persistent channel to a headless in-browser extension body. The extension connects on
    startup and stays connected; the brain sends commands down and reads responses back. All
    user-facing I/O remains in the chat — this socket only carries body commands/results."""
    await websocket.accept()
    extension_hub.loop = asyncio.get_running_loop()
    device_id = websocket.query_params.get("device_id") or "default"
    connection = extension_hub.register(device_id, websocket)
    try:
        await websocket.send_json({"type": "welcome", "device_id": device_id})
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "hello":
                connection.accept_hello(message)
            elif message.get("type") == "response":
                connection.resolve(str(message.get("id") or ""), message.get("result"), message.get("error"))
            # "event" (lineage/evidence) and "ping" frames are accepted and ignored for now.
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - never let a malformed frame crash the socket loop
        pass
    finally:
        # A replacement worker may already own the same stable device id. Remove this exact
        # websocket only; cleanup from the stale worker must not evict its successor.
        extension_hub.unregister(device_id, connection)


# Local-admin routes (paired-device management, evaluation hygiene that closes browser tabs) need a
# loopback Host and the X-Lumen-Local header, so a web page in the user's browser can't reach them
# through DNS rebinding or a simple cross-site request (a custom header forces a CORS preflight).
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _require_local_admin(request: Request) -> None:
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
    if host not in _LOOPBACK_HOSTS or request.headers.get("x-lumen-local") != "1":
        raise HTTPException(status_code=403, detail="This is only available in Lumen on this PC.")


@app.get("/operator/extension/status")
def extension_status() -> dict:
    devices = extension_hub.connected_devices()
    statuses = extension_hub.connection_statuses()
    return {
        "connected": bool(devices),
        "live_actions_ready": any(item.get("live_actions_ready") for item in statuses),
        "devices": devices,
        "connections": statuses,
    }


@app.get("/operator/extension/observe")
async def extension_observe(device_id: str | None = None) -> dict:
    """Ground-truth read of the connected browser's active tab (url, title, visible text).

    Benchmark graders use this after a run ends: the page itself is the judge (see
    app.agent_eval.interaction_bench), and its verdict lives in the tab's visible text. ``device_id``
    reads a specific body (a bench lane); without it, the user's own body."""
    from app.extension_body import ExtensionBody

    connection = extension_hub.get(device_id)
    if not connection:
        raise HTTPException(status_code=409, detail="No extension connected.")
    raw = await ExtensionBody(connection).observe() or {}
    return {
        "url": str(raw.get("url") or ""),
        "title": str(raw.get("title") or ""),
        "text": str(raw.get("text") or ""),
    }


@app.post("/operator/extension/reload")
async def extension_reload(device_id: str | None = None) -> dict:
    """Hot-reload the connected unpacked extension after a source edit (dev iteration loop)."""
    from app.extension_body import ExtensionBody

    connection = extension_hub.get(device_id)
    if not connection:
        raise HTTPException(status_code=409, detail="No extension connected.")
    try:
        result = await ExtensionBody(connection).reload_extension()
    except Exception as exc:  # noqa: BLE001 - an old build without the command just times out
        raise HTTPException(status_code=501, detail=f"Extension did not accept reload: {exc}") from exc
    return {"ok": bool((result or {}).get("ok")), "device_id": connection.device_id}


@app.post("/operator/extension/close-bench-tabs")
async def extension_close_bench_tabs(device_id: str | None = None, _local: None = Depends(_require_local_admin)) -> dict:
    """Evaluation hygiene: close the connected browser's loopback /eval/bench/ tabs and popups, so a
    gauntlet page never starts beside a stale copy pointing at a harness that is gone."""
    from app.extension_body import ExtensionBody

    connection = extension_hub.get(device_id)
    if not connection:
        raise HTTPException(status_code=409, detail="No extension connected.")
    try:
        result = await ExtensionBody(connection).close_bench_tabs()
    except Exception as exc:  # noqa: BLE001 - an old build without the command just times out
        raise HTTPException(status_code=501, detail=f"Extension did not close bench tabs: {exc}") from exc
    return {"ok": bool((result or {}).get("ok")), "closed": int((result or {}).get("closed") or 0)}


@app.post("/operator/extension/bench-lanes/pin")
def extension_pin_bench_lane(request: BenchLanePinRequest, _local: None = Depends(_require_local_admin)) -> dict:
    """Gauntlet runner: send a chat thread's runs to one bench lane (a headless browser it started),
    so pages run in parallel, one per lane. Lanes never take the user's own tasks."""
    try:
        extension_hub.pin_thread_to_lane(request.thread_id, request.device_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "thread_id": request.thread_id, "device_id": request.device_id,
            "connected": extension_hub.get(request.device_id) is not None}


@app.get("/operator/extension/screenshot-probe")
async def extension_screenshot_probe(device_id: str | None = None) -> dict:
    """Diagnostic: attempt one production screenshot capture on the connected tab and report the
    body's verdict (ok / failure_reason / target-surface reason) without an image payload."""
    import time as _time

    from app.extension_body import ExtensionBody

    connection = extension_hub.get(device_id)
    if not connection:
        raise HTTPException(status_code=409, detail="No extension connected.")
    started = _time.monotonic()
    shot = await ExtensionBody(connection).capture_screenshot() or {}
    return {
        "ok": bool(shot.get("ok")),
        "elapsed_ms": int((_time.monotonic() - started) * 1000),
        "failure_reason": shot.get("failure_reason"),
        "target_surface_failure_reason": shot.get("target_surface_failure_reason"),
        "error": shot.get("error"),
        "tab_active": shot.get("tab_active"),
        "window_focused": shot.get("window_focused"),
        "width": shot.get("width"),
        "height": shot.get("height"),
        "image_bytes": len(shot.get("image") or ""),
    }


@app.get("/operator/desktop/status")
def desktop_status(db: Session = Depends(get_db)) -> dict:
    from app.desktop_platform import desktop_control_status
    from app.keep_awake import holding

    status = desktop_control_status()
    # The pill tells the user why their screen isn't sleeping or screensaving mid-run.
    status["keep_awake"] = holding()
    indicator = status.get("control_indicator") or {}
    run_ids = indicator.get("run_ids") or []
    if indicator.get("active") and run_ids:
        # The floating pill mirrors the live run narration instead of a frozen label.
        try:
            run = db.get(models.OperatorRun, int(run_ids[0]))
            message = " ".join(str(getattr(run, "status_message", "") or "").split())
            if message:
                indicator["message"] = message[:120]
        except Exception:
            pass
    return status


@app.post("/operator/desktop/control")
def desktop_control(enabled: bool = Query(...)) -> dict:
    from app.desktop_platform import desktop_control_status, set_runtime_enabled

    set_runtime_enabled(enabled)
    return desktop_control_status()


@app.post("/operator/desktop/permissions/request")
def desktop_permissions_request() -> dict:
    """Show the OS's own prompts for the permissions computer use needs (macOS Accessibility and Screen
    Recording, charged to the app that launched this backend). Nothing is granted here: the user decides in
    System Settings. Elsewhere there is nothing to ask and the status comes back unchanged."""
    from app.desktop_platform import desktop_control_status, request_permissions

    request_permissions()
    return desktop_control_status()


@app.get("/operator/desktop/pointer")
def desktop_pointer(since: int = 0) -> dict:
    """Pointer gestures for the desktop host's on-screen Lumen pointer overlay."""
    from app.desktop_platform import pointer_events

    return pointer_events(since)


@app.get("/operator/runs/{run_id}/questions", response_model=list[OperatorQuestionOut])
def run_questions(run_id: int, db: Session = Depends(get_db)) -> list[models.OperatorQuestion]:
    return OperatorChatService(db).list_questions(run_id)


@app.post("/operator/questions/{question_id}/answer")
def answer_operator_question(
    question_id: int,
    payload: OperatorQuestionAnswer,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    if payload.final_action_decision == "approve" and remote_access.remote_context(request) is not None:
        question = db.get(models.OperatorQuestion, question_id)
        if question is not None and remote_access.is_payment_approval_question(db, question):
            remote_access.require_step_up(request)
    try:
        answer_payload = {**payload.payload, "save_scope": payload.save_scope}
        structured_final_action = None
        if payload.approval_token is not None or payload.final_action_decision is not None:
            if payload.approval_token is None or payload.final_action_decision is None:
                raise ValueError("Final-action answers require both the rendered approval token and decision.")
            structured_final_action = {
                "approval_token": payload.approval_token,
                "decision": payload.final_action_decision,
            }
        result = OperatorChatService(db).answer_question(
            question_id=question_id,
            answer=payload.answer,
            payload=answer_payload,
            auto_resume=payload.auto_resume,
            structured_final_action=structured_final_action,
        )
        return {
            "question": OperatorQuestionOut.model_validate(result["question"]).model_dump(mode="json"),
            "applied": result["applied"],
            "run": OperatorRunOut.model_validate(result["run"]).model_dump(mode="json"),
            "resolved_value": result.get("resolved_value"),
            "confidence": result.get("confidence"),
            "rationale": result.get("rationale"),
            "can_resume": result.get("can_resume"),
            "saved_common_answers": result.get("saved_common_answers", []),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/operator/questions/{question_id}/artifacts/upload")
async def upload_question_artifact(question_id: int, file: UploadFile = File(...), auto_resume: bool = True, db: Session = Depends(get_db)) -> dict:
    result = await ApplicationArtifactService(db).upload_for_question(question_id=question_id, file=file, auto_resume=auto_resume)
    db.commit()
    return _question_artifact_result_payload(result)


@app.post("/operator/questions/{question_id}/artifacts/generate")
def generate_question_artifact(question_id: int, payload: GenerateQuestionArtifactRequest, db: Session = Depends(get_db)) -> dict:
    result = ApplicationArtifactService(db).generate_for_question(
        question_id=question_id,
        instructions=payload.instructions,
        artifact_kind=payload.artifact_kind,
    )
    db.commit()
    return _question_artifact_result_payload(result)


@app.post("/operator/questions/{question_id}/artifacts/{artifact_id}/apply")
def apply_question_artifact(question_id: int, artifact_id: int, payload: ApplyQuestionArtifactRequest, db: Session = Depends(get_db)) -> dict:
    result = ApplicationArtifactService(db).apply_to_question(question_id=question_id, artifact_id=artifact_id, auto_resume=payload.auto_resume)
    db.commit()
    return _question_artifact_result_payload(result)


def _question_artifact_result_payload(result: dict) -> dict:
    artifact = result.get("artifact")
    question = result.get("question")
    run = result.get("run")
    return {
        "artifact": ApplicationArtifactOut.model_validate(artifact).model_dump(mode="json") if artifact else None,
        "question": OperatorQuestionOut.model_validate(question).model_dump(mode="json") if question else None,
        "run": OperatorRunOut.model_validate(run).model_dump(mode="json") if run else None,
        "applied": result.get("applied"),
    }


# Paired phones (app.remote_access). The admin routes are local-only: the remote gateway does not
# list them, so only the cockpit on this PC can show a pairing code or revoke a device. They require
# a loopback Host and the X-Lumen-Local header (_require_local_admin).


def _device_out(device: models.PairedDevice) -> dict:
    return PairedDeviceOut(
        id=device.id,
        name=device.name,
        platform=device.platform,
        has_push=bool(device.push_token),
        autonomy_until=device.autonomy_until,
        last_seen_at=device.last_seen_at,
        created_at=device.created_at,
    ).model_dump(mode="json")


@app.get("/devices/remote-status")
def get_remote_access_status(refresh: bool = False, _local: None = Depends(_require_local_admin)) -> dict:
    return {**remote_access.remote_access_status(refresh=refresh), "push": push.push_status()}


@app.post("/devices/pairing-codes")
def create_device_pairing_code(_local: None = Depends(_require_local_admin)) -> dict:
    status = remote_access.remote_access_status(refresh=True)
    issued = remote_access.issue_pairing_code()
    return {
        "code": issued["code"],
        "expires_at": issued["expires_at"].isoformat(),
        "expires_in_seconds": issued["expires_in_seconds"],
        "public_url": status["public_url"],
        "pairing_uri": remote_access.pairing_uri(status["public_url"], issued["code"]),
        "ready": status["ready"],
        "problem": status["problem"],
    }


@app.get("/devices")
def list_paired_devices(db: Session = Depends(get_db), _local: None = Depends(_require_local_admin)) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    return {"devices": [_device_out(device) for device in remote_access.active_devices(db, user_id=user.id)]}


@app.delete("/devices/{device_id:int}")
def revoke_paired_device(device_id: int, db: Session = Depends(get_db), _local: None = Depends(_require_local_admin)) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    try:
        remote_access.revoke_device(db, user_id=user.id, device_id=device_id)
    except remote_access.RemoteAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return {"revoked": True, "id": device_id}


@app.post("/devices/pair")
def pair_device(payload: DevicePairRequest, db: Session = Depends(get_db)) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    try:
        device, token = remote_access.pair_device(
            db,
            user_id=user.id,
            code=payload.code,
            name=payload.name,
            platform=payload.platform,
            public_key=payload.public_key,
        )
    except remote_access.RemoteAccessError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {"device": _device_out(device), "token": token}


def _current_device(request: Request, db: Session) -> models.PairedDevice:
    context = remote_access.require_device(request)
    device = db.get(models.PairedDevice, int(context["device_id"]))
    if device is None or device.revoked_at is not None:
        raise HTTPException(status_code=401, detail="This device is not paired.")
    return device


@app.get("/devices/me")
def get_current_device(request: Request, db: Session = Depends(get_db)) -> dict:
    return {"device": _device_out(_current_device(request, db)), "autonomy_hours": get_settings().remote_autonomy_hours}


@app.delete("/devices/me")
def unpair_current_device(request: Request, db: Session = Depends(get_db)) -> dict:
    device = _current_device(request, db)
    remote_access.revoke_device(db, user_id=device.user_id, device_id=device.id)
    db.commit()
    return {"revoked": True, "id": device.id}


@app.post("/devices/me/push-token")
def update_current_device_push_token(payload: DevicePushTokenUpdate, request: Request, db: Session = Depends(get_db)) -> dict:
    device = _current_device(request, db)
    device.push_token = (payload.push_token or "").strip() or None
    device.updated_at = models.utcnow()
    db.commit()
    return {"device": _device_out(device)}


@app.post("/devices/me/autonomy")
def unlock_current_device_autonomy(request: Request, db: Session = Depends(get_db)) -> dict:
    # The gateway already required a fingerprint-signed request for this route.
    device = _current_device(request, db)
    remote_access.unlock_autonomy(device)
    db.commit()
    return {"device": _device_out(device)}


@app.delete("/devices/me/autonomy")
def lock_current_device_autonomy(request: Request, db: Session = Depends(get_db)) -> dict:
    device = _current_device(request, db)
    device.autonomy_until = None
    device.updated_at = models.utcnow()
    db.commit()
    return {"device": _device_out(device)}


@app.post("/devices/me/challenges")
def create_step_up_challenge(request: Request, db: Session = Depends(get_db)) -> dict:
    device = _current_device(request, db)
    return remote_access.issue_challenge(device.id)



# What Lumen knows about the user: the memory file is the profile (app.user_memory). These routes
# are how the user reads and edits it; every change re-mirrors the standard facts into the
# profile columns that older code reads.


def _memory_fact_out(fact: user_memory.UserFact) -> dict:
    learned_on, _, source = fact.note.partition(",")
    return {
        "key": fact.key,
        "label": fact.key.replace("_", " ").capitalize(),
        "value": fact.value,
        "section": fact.section,
        "learned_on": learned_on.strip() or None,
        "source": source.strip() or None,
    }


def _memory_out(user_id: int) -> dict:
    return {
        "sections": [{"key": key, "title": title} for key, title in user_memory.SECTION_TITLES.items()],
        "facts": [_memory_fact_out(fact) for fact in user_memory.facts(user_id)],
    }


@app.get("/memory")
def get_memory(db: Session = Depends(get_db)) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    return _memory_out(user.id)


@app.post("/memory")
def add_memory_fact(payload: MemoryFactCreate, db: Session = Depends(get_db)) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    key = user_memory.normalize_memory_key(payload.label)
    if not key:
        raise HTTPException(status_code=400, detail="Give the fact a name.")
    if any(fact.key == key for fact in user_memory.facts(user.id)):
        raise HTTPException(status_code=409, detail="You already have a fact with that name. Edit it instead.")
    if user_memory.remember(user.id, key, payload.value, section=payload.section or None, source_note="added by you") is None:
        raise HTTPException(status_code=400, detail="That fact couldn't be saved.")
    user_memory.mirror_to_profile(db, user.id)
    db.commit()
    return _memory_out(user.id)


@app.patch("/memory/{key}")
def update_memory_fact(key: str, payload: MemoryFactUpdate, db: Session = Depends(get_db)) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    current = next((fact for fact in user_memory.facts(user.id) if fact.key == user_memory.normalize_memory_key(key)), None)
    if current is None:
        raise HTTPException(status_code=404, detail="That fact is gone.")
    new_key = user_memory.normalize_memory_key(payload.label) if payload.label else current.key
    if not new_key:
        raise HTTPException(status_code=400, detail="Give the fact a name.")
    if new_key != current.key and any(fact.key == new_key for fact in user_memory.facts(user.id)):
        raise HTTPException(status_code=409, detail="You already have a fact with that name.")
    value = payload.value if payload.value is not None else current.value
    section = payload.section or current.section
    # Write the new fact first, so a failed write never loses the old one.
    if user_memory.remember(user.id, new_key, value, section=section, source_note="edited by you") is None:
        raise HTTPException(status_code=400, detail="That fact couldn't be saved.")
    if new_key != current.key:
        user_memory.forget(user.id, current.key)
    user_memory.mirror_to_profile(db, user.id, cleared=(current.key,) if new_key != current.key else ())
    db.commit()
    return _memory_out(user.id)


@app.delete("/memory/{key}")
def delete_memory_fact(key: str, db: Session = Depends(get_db)) -> dict:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    if not user_memory.forget(user.id, key):
        raise HTTPException(status_code=404, detail="That fact is gone.")
    user_memory.mirror_to_profile(db, user.id, cleared=(user_memory.normalize_memory_key(key),))
    db.commit()
    return _memory_out(user.id)
