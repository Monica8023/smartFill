"""FastAPI control plane for SmartFill MVP."""

from __future__ import annotations

import hmac
import json
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

from smartfill.config import Settings, normalize_origin
from smartfill.domain import InvalidTransitionError, Task
from smartfill.execution import is_https_origin
from smartfill.importing import ImportPreview, ImportValidationError, ProfileImporter
from smartfill.secrets import InMemorySecretStore
from smartfill.tasks import InMemoryTaskRepository, TaskNotFoundError, TaskService


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
        if not is_https_origin(normalized):
            raise ValueError("Target origin must use HTTPS")
        return normalized


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


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime = settings or Settings()
    app = FastAPI(title="SmartFill API", version="0.1.0", docs_url="/api/docs")
    task_service = TaskService(InMemoryTaskRepository())
    secret_store = InMemorySecretStore()
    importer = ProfileImporter(secret_store, max_bytes=runtime.upload_max_bytes)

    app.state.settings = runtime
    app.state.task_service = task_service
    app.state.secret_store = secret_store
    app.add_middleware(RateLimitMiddleware, requests_per_minute=runtime.request_limit_per_minute)
    if runtime.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=runtime.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
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
                        )
                    )
        response = await call_next(request)
        return _secure_response(response)

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "smartfill-api"}

    @app.post("/api/v1/tasks", response_model=Task, status_code=status.HTTP_201_CREATED)
    def create_task(payload: TaskCreate) -> Task:
        if payload.target_origin not in set(runtime.allowed_target_origins):
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

    return app


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


def _secure_response(response: Response) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response


app = create_app()
