"""FastAPI control plane for SmartFill MVP."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

from smartfill.batch_jobs import (
    BatchManager,
    BatchRepository,
    BatchRun,
    InMemoryBatchRepository,
)
from smartfill.browser_jobs import (
    BrowserAutomationWorker,
    BrowserJob,
    BrowserJobCreate,
    BrowserJobManager,
    BrowserJobNotFoundError,
    BrowserJobRepository,
    BrowserJobStatus,
    HumanResolution,
)
from smartfill.browser_worker import PlaywrightBrowserWorker
from smartfill.config import Settings, normalize_origin
from smartfill.demo import (
    DEMO_AMBIGUOUS_HTML,
    DEMO_FRAME_HTML,
    DEMO_HOME_HTML,
    DEMO_LOGIN_HTML,
    DEMO_LOGIN_SUCCESS_HTML,
    DEMO_SHADOW_JS,
    DEMO_SUBMITTED_HTML,
    DEMO_TARGET_HTML,
)
from smartfill.domain import InvalidTransitionError, Task, TaskStatus
from smartfill.execution import is_https_origin
from smartfill.importing import ImportPreview, ImportValidationError, ProfileImporter
from smartfill.persistence import (
    SqlAlchemyBatchRepository,
    SqlAlchemyBrowserJobRepository,
    SqlAlchemySystemSettingsRepository,
    SqlAlchemyTaskRepository,
    TargetOriginSettings,
)
from smartfill.runtime_settings import (
    InMemorySystemSettingsRepository,
    TargetOriginService,
    TargetOriginSettingsRepository,
)
from smartfill.secrets import InMemorySecretStore
from smartfill.tasks import (
    InMemoryTaskRepository,
    TaskNotFoundError,
    TaskRepository,
    TaskService,
)
from smartfill.vision import AliyunVisionProvider

logger = logging.getLogger(__name__)


class TaskCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    target_origin: str = Field(min_length=1, max_length=2_048)
    record_count: int = Field(ge=1, le=1_000_000)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Task name cannot be blank")
        return stripped

    @field_validator("target_origin")
    @classmethod
    def require_https_origin(cls, value: str) -> str:
        normalized = normalize_origin(value)
        hostname = urlsplit(normalized).hostname
        if not is_https_origin(normalized) and hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Target origin must use HTTPS")
        return normalized


class TargetOriginsUpdate(BaseModel):
    origins: list[str] = Field(min_length=1, max_length=100)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Small single-process limiter for the MVP control plane."""

    def __init__(self, app: ASGIApp, requests_per_minute: int) -> None:
        super().__init__(app)
        self._limit = requests_per_minute
        self._requests: defaultdict[str, deque[float]] = defaultdict(deque)

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        client_key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        timestamps = self._requests[client_key]
        while timestamps and timestamps[0] <= now - 60:
            timestamps.popleft()
        if len(timestamps) >= self._limit:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Too many requests"},
            )
        timestamps.append(now)
        return await call_next(request)


def create_app(
    settings: Settings | None = None,
    *,
    browser_worker: BrowserAutomationWorker | None = None,
    database_engine: Engine | None = None,
) -> FastAPI:
    runtime = settings or Settings()
    app = FastAPI(title="SmartFill API", version="0.1.0", docs_url="/api/docs")
    secret_store = InMemorySecretStore()
    importer = ProfileImporter(secret_store, max_bytes=runtime.upload_max_bytes)
    configured_origins = set(runtime.allowed_target_origins)
    if runtime.environment in {"development", "test"}:
        configured_origins.update({"http://127.0.0.1:8000", "http://localhost:8000"})
    engine = database_engine
    if engine is None and runtime.database_url is not None:
        engine = create_engine(
            runtime.database_url.get_secret_value(),
            pool_pre_ping=True,
            pool_recycle=1_800,
        )
    if engine is None:
        task_repository: TaskRepository
        job_repository: BrowserJobRepository | None
        batch_repository: BatchRepository
        settings_repository: TargetOriginSettingsRepository
        task_repository = InMemoryTaskRepository()
        job_repository = None
        batch_repository = InMemoryBatchRepository()
        settings_repository = InMemorySystemSettingsRepository()
    else:
        task_repository = SqlAlchemyTaskRepository(engine)
        job_repository = SqlAlchemyBrowserJobRepository(engine)
        batch_repository = SqlAlchemyBatchRepository(engine)
        settings_repository = SqlAlchemySystemSettingsRepository(engine)
    task_service = TaskService(task_repository)
    target_origins = TargetOriginService(settings_repository, configured_origins)
    worker = browser_worker
    if worker is None:
        vision_provider = (
            AliyunVisionProvider(
                api_key=runtime.dashscope_api_key.get_secret_value(),
                model=runtime.dashscope_fast_model,
                base_url=runtime.dashscope_base_url,
            )
            if runtime.dashscope_api_key is not None
            else None
        )
        worker = PlaywrightBrowserWorker(
            secret_store=secret_store,
            allowed_origins=target_origins.origins(),
            allowed_origins_provider=target_origins.origins,
            artifacts_root=runtime.browser_artifacts_root.resolve(),
            headless=runtime.browser_headless,
            relaxed_manual_navigation=runtime.browser_relaxed_manual_navigation,
            cdp_url=runtime.browser_cdp_url,
            navigation_timeout_ms=runtime.browser_navigation_timeout_ms,
            action_timeout_ms=runtime.browser_action_timeout_ms,
            screenshot_timeout_ms=runtime.browser_screenshot_timeout_ms,
            vision_provider=vision_provider,
        )
    browser_jobs = BrowserJobManager(
        worker=worker,
        secret_store=secret_store,
        repository=job_repository,
        artifacts_root=runtime.browser_artifacts_root,
        terminal_status_callback=lambda task_id, job_status: _sync_task_failure(
            task_service,
            task_id,
            job_status,
        ),
    )
    batches = BatchManager(
        repository=batch_repository,
        browser_jobs=browser_jobs,
        task_service=task_service,
        secret_store=secret_store,
    )
    web_dist = Path(__file__).resolve().parents[2] / "apps" / "web" / "dist"

    app.state.settings = runtime
    app.state.task_service = task_service
    app.state.secret_store = secret_store
    app.state.browser_jobs = browser_jobs
    app.state.batches = batches
    app.state.database_engine = engine
    app.state.target_origins = target_origins
    app.add_middleware(RateLimitMiddleware, requests_per_minute=runtime.request_limit_per_minute)
    if runtime.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=runtime.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.middleware("http")
    async def authenticate_and_secure(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if request.url.path.startswith("/api/v1") and request.url.path != "/api/v1/health":
            configured = runtime.api_token
            if configured is not None:
                expected = f"Bearer {configured.get_secret_value()}"
                provided = request.headers.get("Authorization", "")
                if not hmac.compare_digest(provided, expected):
                    return _secure_response(
                        JSONResponse(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            content={"detail": "Authentication required"},
                            headers={"WWW-Authenticate": "Bearer"},
                        ),
                        is_api_docs=False,
                    )
        response = await call_next(request)
        return _secure_response(
            response,
            is_api_docs=request.url.path.startswith("/api/docs"),
            is_web_content=(
                request.url.path == "/"
                or request.url.path.startswith("/assets/")
                or request.url.path.startswith("/demo/")
            ),
            allow_same_origin_frame=request.url.path == "/demo/frame",
        )

    @app.get("/", include_in_schema=False)
    def web_console() -> Response:
        index = web_dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return HTMLResponse(
            "<h1>SmartFill Web 尚未构建</h1>"
            "<p>请执行 <code>cd apps/web &amp;&amp; npm install "
            "&amp;&amp; npm run build</code>。</p>",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    if web_dist.is_dir():
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="web-assets")

    @app.get("/demo/target", response_class=HTMLResponse, include_in_schema=False)
    def demo_target() -> str:
        if runtime.environment not in {"development", "test"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return DEMO_TARGET_HTML

    @app.get("/demo/frame", response_class=HTMLResponse, include_in_schema=False)
    def demo_frame() -> str:
        if runtime.environment not in {"development", "test"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return DEMO_FRAME_HTML

    @app.get("/demo/home", response_class=HTMLResponse, include_in_schema=False)
    def demo_home() -> str:
        if runtime.environment not in {"development", "test"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return DEMO_HOME_HTML

    @app.get("/demo/login", response_class=HTMLResponse, include_in_schema=False)
    def demo_login() -> str:
        if runtime.environment not in {"development", "test"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return DEMO_LOGIN_HTML

    @app.get(
        "/demo/login-success",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def demo_login_success() -> str:
        if runtime.environment not in {"development", "test"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return DEMO_LOGIN_SUCCESS_HTML

    @app.post(
        "/demo/login-success",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def submit_demo_login() -> str:
        if runtime.environment not in {"development", "test"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return DEMO_LOGIN_SUCCESS_HTML

    @app.get("/demo/submitted", response_class=HTMLResponse, include_in_schema=False)
    def demo_submitted() -> str:
        if runtime.environment not in {"development", "test"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return DEMO_SUBMITTED_HTML

    @app.get("/demo/shadow.js", include_in_schema=False)
    def demo_shadow_script() -> Response:
        if runtime.environment not in {"development", "test"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return Response(DEMO_SHADOW_JS, media_type="application/javascript")

    @app.get("/demo/ambiguous", response_class=HTMLResponse, include_in_schema=False)
    def demo_ambiguous() -> str:
        if runtime.environment not in {"development", "test"}:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return DEMO_AMBIGUOUS_HTML

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "smartfill-api"}

    @app.post("/api/v1/tasks", response_model=Task, status_code=status.HTTP_201_CREATED)
    def create_task(payload: TaskCreate) -> Task:
        if not target_origins.contains(payload.target_origin):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Target origin is not approved",
            )
        return task_service.create(
            name=payload.name,
            target_origin=payload.target_origin,
            record_count=payload.record_count,
        )

    @app.get("/api/v1/tasks", response_model=list[Task])
    def list_tasks() -> list[Task]:
        return task_service.list()

    @app.get(
        "/api/v1/settings/target-origins",
        response_model=TargetOriginSettings,
    )
    def get_target_origins() -> TargetOriginSettings:
        return target_origins.get()

    @app.put(
        "/api/v1/settings/target-origins",
        response_model=TargetOriginSettings,
    )
    def replace_target_origins(payload: TargetOriginsUpdate) -> TargetOriginSettings:
        try:
            return target_origins.replace(payload.origins)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error

    @app.get("/api/v1/tasks/{task_id}", response_model=Task)
    def get_task(task_id: str) -> Task:
        return _get_or_404(task_service, task_id)

    @app.post("/api/v1/tasks/{task_id}/validate", response_model=Task)
    def validate_task(task_id: str) -> Task:
        return _mutate_task(task_service.validate, task_id)

    @app.post("/api/v1/tasks/{task_id}/start", response_model=Task)
    def start_task(task_id: str) -> Task:
        return _mutate_task(task_service.start, task_id)

    @app.post("/api/v1/tasks/{task_id}/pause", response_model=Task)
    def pause_task(task_id: str) -> Task:
        return _mutate_task(task_service.pause, task_id)

    @app.post("/api/v1/tasks/{task_id}/resume", response_model=Task)
    def resume_task(task_id: str) -> Task:
        return _mutate_task(task_service.resume, task_id)

    @app.post("/api/v1/imports/preview", response_model=ImportPreview)
    async def preview_import(
        file: Annotated[UploadFile, File()],
        mapping_json: Annotated[str, Form()],
    ) -> ImportPreview:
        content = await file.read(runtime.upload_max_bytes + 1)
        try:
            mapping = json.loads(mapping_json)
            if not isinstance(mapping, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in mapping.items()
            ):
                raise ValueError
        except (json.JSONDecodeError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Field mapping must be a JSON object of strings",
            ) from error
        try:
            return importer.preview(file.filename or "upload", content, mapping)
        except ImportValidationError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error

    @app.post(
        "/api/v1/batches",
        response_model=BatchRun,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_batch(
        file: Annotated[UploadFile, File()],
        mapping_json: Annotated[str, Form()],
        workflow_job_id: Annotated[str, Form(min_length=1, max_length=100)],
        name: Annotated[str, Form(min_length=1, max_length=120)],
    ) -> BatchRun:
        try:
            template = browser_jobs.get(workflow_job_id)
        except BrowserJobNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Workflow template was not found",
            ) from error
        raw_steps = template.configuration_snapshot.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Workflow template has no reusable steps",
            )
        try:
            mapping = json.loads(mapping_json)
            if not isinstance(mapping, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in mapping.items()
            ):
                raise ValueError
        except (json.JSONDecodeError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Field mapping must be a JSON object of strings",
            ) from error
        definitions = [
            definition
            for step in raw_steps
            for definition in step.get("field_definitions", [])
            if isinstance(definition, dict)
        ]
        required_fields = {
            str(definition.get("key", ""))
            for definition in definitions
            if definition.get("source_field") is None
        }
        if not required_fields:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Workflow template has no importable fields",
            )
        allowed_fields = required_fields
        sensitive_fields = {
            str(definition.get("key", ""))
            for definition in definitions
            if definition.get("sensitive") is True
        }
        step_urls = [str(step.get("target_url", "")) for step in raw_steps]
        if any(not target_origins.contains(target_url) for target_url in step_urls):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="A workflow step target origin is not approved",
            )
        content = await file.read(runtime.upload_max_bytes + 1)
        try:
            preview = importer.preview(
                file.filename or "upload",
                content,
                mapping,
                allowed_fields=allowed_fields,
                required_fields=required_fields,
                sensitive_fields=sensitive_fields,
            )
        except ImportValidationError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
        if preview.total_rows > runtime.batch_max_records:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Batch exceeds the {runtime.batch_max_records} record limit",
            )
        clean_name = name.strip()
        if not clean_name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Batch name cannot be blank",
            )
        task = task_service.create(
            name=clean_name,
            target_origin=normalize_origin(step_urls[0]),
            record_count=preview.total_rows,
        )
        task_service.validate(task.id)
        task_service.start(task.id)
        batch = batches.create(
            name=clean_name,
            task_id=task.id,
            template=template,
            preview=preview,
            mapping=mapping,
        )
        batches.start(batch.id)
        return batch

    @app.get("/api/v1/batches", response_model=list[BatchRun])
    def list_batches() -> list[BatchRun]:
        return batches.list()

    @app.get("/api/v1/batches/{batch_id}", response_model=BatchRun)
    def get_batch(batch_id: str) -> BatchRun:
        try:
            return batches.get(batch_id)
        except KeyError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Batch was not found",
            ) from error

    @app.post(
        "/api/v1/browser/jobs",
        response_model=BrowserJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_browser_job(
        payload: BrowserJobCreate,
    ) -> BrowserJob:
        task = _get_or_404(task_service, payload.task_id)
        step_urls = [step.target_url for step in payload.workflow_steps]
        target_urls = step_urls or [payload.target_url]
        if normalize_origin(target_urls[0]) != task.target_origin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Browser target origin does not match the task",
            )
        if any(not target_origins.contains(target_url) for target_url in target_urls):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="A workflow step target origin is not approved",
            )
        if task.status is not TaskStatus.RUNNING:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Task must be running before browser execution",
            )
        job, created = browser_jobs.create_or_reuse_active(
            payload,
            task_name=task.name,
        )
        if created:
            browser_jobs.start(job.id)
        return job

    @app.get("/api/v1/browser/jobs", response_model=list[BrowserJob])
    def list_browser_jobs() -> list[BrowserJob]:
        return browser_jobs.list()

    @app.get("/api/v1/browser/jobs/{job_id}", response_model=BrowserJob)
    def get_browser_job(job_id: str) -> BrowserJob:
        return _get_job_or_404(browser_jobs, job_id)

    @app.post(
        "/api/v1/browser/jobs/{job_id}/resolve",
        response_model=BrowserJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def resolve_browser_job(
        job_id: str,
        resolution: HumanResolution,
    ) -> BrowserJob:
        _get_job_or_404(browser_jobs, job_id)
        try:
            return await browser_jobs.resolve(job_id, resolution)
        except ValueError as error:
            detail = str(error)
            response_status = (
                status.HTTP_409_CONFLICT
                if "not waiting" in detail
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(status_code=response_status, detail=detail) from error

    @app.post(
        "/api/v1/browser/jobs/{job_id}/cancel",
        response_model=BrowserJob,
    )
    async def cancel_browser_job(job_id: str) -> BrowserJob:
        _get_job_or_404(browser_jobs, job_id)
        try:
            return await browser_jobs.cancel_job(job_id)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(error),
            ) from error

    @app.post(
        "/api/v1/browser/jobs/{job_id}/browser/close",
        response_model=BrowserJob,
    )
    async def close_browser_job(job_id: str) -> BrowserJob:
        _get_job_or_404(browser_jobs, job_id)
        try:
            return await browser_jobs.close_browser(job_id)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(error),
            ) from error

    @app.get("/api/v1/browser/jobs/{job_id}/screenshot")
    def get_browser_job_screenshot(job_id: str) -> FileResponse:
        _get_job_or_404(browser_jobs, job_id)
        try:
            screenshot_path = browser_jobs.get_screenshot_path(job_id)
        except FileNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Screenshot is not available",
            ) from error
        return FileResponse(
            screenshot_path,
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/browser/jobs/{job_id}/diagnostic")
    def get_browser_job_diagnostic(job_id: str) -> FileResponse:
        _get_job_or_404(browser_jobs, job_id)
        try:
            diagnostic_path = browser_jobs.get_diagnostic_path(job_id)
        except FileNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Diagnostic log is not available",
            ) from error
        return FileResponse(
            diagnostic_path,
            media_type="text/plain; charset=utf-8",
            filename=Path(diagnostic_path).name,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/browser/jobs/{job_id}/downloads/{index}")
    def get_browser_job_download(job_id: str, index: int) -> FileResponse:
        _get_job_or_404(browser_jobs, job_id)
        try:
            download_path = browser_jobs.get_download_path(job_id, index)
        except FileNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Downloaded document is not available",
            ) from error
        return FileResponse(
            download_path,
            filename=Path(download_path).name,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/browser/jobs/{job_id}/statistics")
    def get_browser_job_statistics(job_id: str) -> FileResponse:
        _get_job_or_404(browser_jobs, job_id)
        try:
            statistics_path = browser_jobs.get_statistics_path(job_id)
        except FileNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Execution statistics are not available",
            ) from error
        return FileResponse(
            statistics_path,
            media_type="text/plain; charset=utf-8",
            filename=Path(statistics_path).name,
            headers={"Cache-Control": "no-store"},
        )

    @app.websocket("/api/v1/browser/jobs/{job_id}/stream")
    async def stream_browser_job(websocket: WebSocket, job_id: str) -> None:
        try:
            browser_jobs.get(job_id)
        except BrowserJobNotFoundError:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        configured_token = runtime.api_token
        if configured_token is not None:
            try:
                authentication = await asyncio.wait_for(websocket.receive_json(), timeout=5)
                provided = authentication.get("token", "")
            except (TimeoutError, ValueError, WebSocketDisconnect):
                await websocket.close(code=4401)
                return
            if not hmac.compare_digest(provided, configured_token.get_secret_value()):
                await websocket.close(code=4401)
                return

        queue = browser_jobs.subscribe(job_id)
        terminal = {
            BrowserJobStatus.COMPLETED,
            BrowserJobStatus.FAILED,
            BrowserJobStatus.CANCELLED,
            BrowserJobStatus.NEED_HUMAN,
        }
        try:
            while True:
                job = await queue.get()
                await websocket.send_text(job.model_dump_json())
                if job.status in terminal:
                    break
        except WebSocketDisconnect:
            pass
        finally:
            browser_jobs.unsubscribe(job_id, queue)

    return app


def _sync_task_failure(
    task_service: TaskService,
    task_id: str,
    job_status: BrowserJobStatus,
) -> None:
    if job_status is not BrowserJobStatus.FAILED:
        return
    try:
        task = task_service.get(task_id)
        if task.status in {TaskStatus.RUNNING, TaskStatus.PAUSED}:
            task_service.fail(task_id)
    except (TaskNotFoundError, InvalidTransitionError):
        logger.exception(
            "Failed to synchronize browser failure to task task_id=%s",
            task_id,
        )


def _get_or_404(service: TaskService, task_id: str) -> Task:
    try:
        return service.get(task_id)
    except TaskNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        ) from error
def _mutate_task(operation: Callable[[str], Task], task_id: str) -> Task:
    try:
        return operation(task_id)
    except TaskNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        ) from error
    except InvalidTransitionError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task state does not allow this operation",
        ) from error


def _get_job_or_404(manager: BrowserJobManager, job_id: str) -> BrowserJob:
    try:
        return manager.get(job_id)
    except BrowserJobNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Browser job not found",
        ) from error


def _secure_response(
    response: Response,
    *,
    is_api_docs: bool,
    is_web_content: bool = False,
    allow_same_origin_frame: bool = False,
) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = (
        "SAMEORIGIN" if allow_same_origin_frame else "DENY"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    if is_api_docs:
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            "script-src https://cdn.jsdelivr.net 'unsafe-inline'; "
            "style-src https://cdn.jsdelivr.net 'unsafe-inline'; "
            "img-src https://fastapi.tiangolo.com data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
    elif is_web_content:
        frame_ancestors = "'self'" if allow_same_origin_frame else "'none'"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' blob: data:; "
            "connect-src 'self' ws: wss:; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; "
            f"frame-ancestors {frame_ancestors}"
        )
    else:
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response


app = create_app()
