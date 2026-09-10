"""Playwright execution and grounded browser-action boundary."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Download,
    Frame,
    Locator,
    Page,
    Playwright,
    Request,
    Route,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)
from pydantic import BaseModel, ConfigDict, Field

from smartfill.browser_jobs import (
    AuthenticationMode,
    BrowserJobStatus,
    BrowserRunRequest,
    BrowserRunResult,
    EntryActionMode,
    ExecutionStatistics,
    HumanIntervention,
    HumanResolution,
    InterventionCandidate,
    InterventionKind,
    JobProgress,
    RequiredDataField,
    SubmissionPolicy,
    WorkflowStep,
)
from smartfill.config import normalize_origin
from smartfill.execution import ActionPolicy, ActionProposal, ActionType
from smartfill.field_schema import (
    FieldDefinition,
    FieldInputKind,
    default_field_definition,
)
from smartfill.secrets import SecretStore
from smartfill.vision import (
    BrowserElement,
    FieldMapping,
    VisionProvider,
    VisionRequest,
    VisionResponseError,
)
from smartfill.vision_poc import ObservedVisualPage, VisualPageObserver

logger = logging.getLogger(__name__)


class DomElement(BaseModel):
    model_config = ConfigDict(frozen=True)

    element_id: str
    tag: str
    input_type: str = ""
    label: str = ""
    aria_label: str = ""
    placeholder: str = ""
    name: str = ""
    autocomplete: str = ""
    role: str = ""
    accessible_name: str = ""
    form_context: str = ""
    frame_path: str = "main"
    frame_url: str = ""
    tree_scope: str = "document"
    options: list[dict[str, str]] = Field(default_factory=list)


class AccessibilityTreeSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    frame_path: str
    frame_url: str
    snapshot: str


class PageObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    elements: list[DomElement]
    accessibility_trees: list[AccessibilityTreeSnapshot]


@dataclass(slots=True)
class ObservedPage:
    observation: PageObservation
    locators: dict[str, Locator]

    @property
    def elements(self) -> list[DomElement]:
        return self.observation.elements

    @property
    def accessibility_trees(self) -> list[AccessibilityTreeSnapshot]:
        return self.observation.accessibility_trees


@dataclass(slots=True)
class ObservedActions:
    candidates: list[InterventionCandidate]
    locators: dict[str, Locator]


@dataclass(slots=True)
class _BrowserSession:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    request: BrowserRunRequest
    owns_browser: bool
    trusted_navigation_origins: set[str] = dataclass_field(default_factory=set)
    navigated: bool = False
    observed: ObservedPage | None = None
    fields_verified: bool = False
    completed_fields: int = 0
    submit_actions: ObservedActions | None = None
    submission_attempted: bool = False
    entry_actions: ObservedActions | None = None
    entry_action_performed: bool = False
    workflow_steps: list[WorkflowStep] | None = None
    step_index: int = 0
    step_announced: bool = False
    visual_values: dict[str, str] = dataclass_field(default_factory=dict)
    visual_definitions: dict[str, FieldDefinition] = dataclass_field(default_factory=dict)
    visual_completed: set[str] = dataclass_field(default_factory=set)
    visual_recent_actions: list[str] = dataclass_field(default_factory=list)
    visual_last_observation_at: float | None = None
    last_visual_action_label: str | None = None
    last_visual_action_key: str | None = None
    manual_login_confirmed: bool = False
    authentication_reused: bool = False
    authentication_session_valid: bool = True
    heartbeat_page: Page | None = None
    statistics_started_at: float = dataclass_field(default_factory=time.perf_counter)
    screenshot_count: int = 0
    model_call_count: int = 0
    model_latency_ms: int = 0
    browser_action_count: int = 0
    click_count: int = 0
    scroll_count: int = 0
    wait_count: int = 0
    fill_count: int = 0
    consecutive_model_errors: int = 0
    consecutive_semantic_rejections: int = 0
    download_paths: list[str] = dataclass_field(default_factory=list)
    download_tasks: set[asyncio.Task[None]] = dataclass_field(default_factory=set)
    download_error: str | None = None


class FieldMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    canonical_field: str
    element_id: str
    confidence: float = Field(ge=0, le=1)
    evidence: str


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)


def _tokens(value: str) -> list[str]:
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    normalized = unicodedata.normalize("NFKC", expanded).casefold()
    return re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", normalized)


def _term_is_within(alias: str, value: str) -> bool:
    normalized_alias = _normalize(alias)
    normalized_value = _normalize(value)
    if not normalized_alias or not normalized_value:
        return False
    if normalized_alias == normalized_value:
        return True
    if re.search(r"[a-zA-Z]", alias):
        alias_tokens = _tokens(alias)
        value_tokens = _tokens(value)
        width = len(alias_tokens)
        return any(
            value_tokens[index : index + width] == alias_tokens
            for index in range(len(value_tokens) - width + 1)
        )
    return normalized_alias in normalized_value


def _form_kind(context: str) -> str:
    normalized = " ".join(_tokens(context))
    compact = _normalize(context)
    registration_terms = ("register", "registration", "sign up", "create account")
    login_terms = ("login", "log in", "sign in")
    if any(term in normalized for term in registration_terms) or "注册" in compact:
        return "registration"
    if any(term in normalized for term in login_terms) or "登录" in compact or "登陆" in compact:
        return "login"
    return "unknown"


class SemanticFieldMapper:
    """Map canonical fields using portable user-facing and HTML semantics."""

    def __init__(self, *, threshold: float = 0.85, ambiguity_delta: float = 0.03) -> None:
        self._threshold = threshold
        self._ambiguity_delta = ambiguity_delta

    def map_fields(
        self,
        elements: list[DomElement],
        fields: Sequence[str | FieldDefinition],
    ) -> list[FieldMatch]:
        matches: list[FieldMatch] = []
        used_elements: set[str] = set()
        for definition in self._coerce_definitions(fields):
            canonical = definition.key
            candidates = sorted(
                (
                    (self._score(definition, element), element)
                    for element in elements
                    if element.element_id not in used_elements
                ),
                key=lambda item: item[0][0],
                reverse=True,
            )
            if not candidates or candidates[0][0][0] < self._threshold:
                continue
            if (
                len(candidates) > 1
                and candidates[0][0][0] - candidates[1][0][0] < self._ambiguity_delta
            ):
                continue
            (confidence, evidence), element = candidates[0]
            matches.append(
                FieldMatch(
                    canonical_field=canonical,
                    element_id=element.element_id,
                    confidence=confidence,
                    evidence=evidence,
                )
            )
            used_elements.add(element.element_id)
        return matches

    @staticmethod
    def _score(definition: FieldDefinition, element: DomElement) -> tuple[float, str]:
        form_kind = _form_kind(element.form_context)
        input_type = _normalize(element.input_type)
        semantic_values = [
            element.label,
            element.aria_label,
            element.placeholder,
            element.name,
            element.role,
            element.accessible_name,
        ]
        is_confirmation = any(
            _term_is_within(term, value)
            for term in ("confirm", "confirmation", "确认", "再次")
            for value in semantic_values
            if value
        )
        is_login_identifier = input_type in {"email", "tel"} or any(
            _term_is_within(term, value)
            for term in ("email", "email address", "username", "user name", "账号", "账户")
            for value in semantic_values
            if value
        )

        if (
            form_kind == "login"
            and definition.key == "account.username"
            and is_login_identifier
        ):
            return 0.99, f"{form_kind} form uses type={element.input_type} as login identifier"
        if (
            form_kind == "registration"
            and definition.key == "account.email"
            and input_type == "email"
        ):
            return 0.99, "registration form email"
        if (
            definition.key == "account.passwordConfirmation"
            and input_type == "password"
            and is_confirmation
        ):
            return 0.99, "password confirmation semantics"
        if definition.key == "account.password" and is_confirmation:
            return 0.0, "confirmation field is not the source password"

        autocomplete = _normalize(element.autocomplete)
        autocomplete_hints = {_normalize(value) for value in definition.autocomplete_hints}
        if autocomplete and autocomplete in autocomplete_hints:
            return 0.99, f"autocomplete={element.autocomplete}"

        semantic_parts = [part for part in semantic_values if part]
        normalized_parts = [_normalize(part) for part in semantic_parts]
        aliases = sorted(definition.semantic_aliases, key=len, reverse=True)
        for alias in aliases:
            normalized_alias = _normalize(alias)
            if normalized_alias and normalized_alias in normalized_parts:
                return 0.97, f"semantic field equals {alias}"
            if any(_term_is_within(alias, part) for part in semantic_parts):
                return 0.92, f"semantic text contains {alias}"
        if definition.input_kind in {
            FieldInputKind.PASSWORD,
            FieldInputKind.EMAIL,
            FieldInputKind.TEL,
        } and input_type == definition.input_kind:
            return 0.88, f"type={element.input_type}"
        return 0.0, "no semantic evidence"

    def ranked_candidates(
        self,
        elements: list[DomElement],
        field: str | FieldDefinition,
        *,
        limit: int = 10,
    ) -> list[tuple[float, DomElement]]:
        definition = (
            field if isinstance(field, FieldDefinition) else default_field_definition(field)
        )
        ranked = sorted(
            ((self._score(definition, element)[0], element) for element in elements),
            key=lambda item: item[0],
            reverse=True,
        )
        evidenced = [candidate for candidate in ranked if candidate[0] > 0]
        return (evidenced or ranked)[:limit]

    @staticmethod
    def _coerce_definitions(
        fields: Sequence[str | FieldDefinition],
    ) -> list[FieldDefinition]:
        return [
            field if isinstance(field, FieldDefinition) else default_field_definition(field)
            for field in fields
        ]


class PlaywrightPageObserver:
    """Capture one page-wide DOM and ARIA observation across every attached frame."""

    _FORM_SELECTOR = (
        "input:not([type=hidden]):not([type=submit]):not([type=button])"
        ":not([type=reset]):not([type=image]), textarea, select"
    )
    _ACTION_SELECTOR = "button, input[type=submit], input[type=button]"
    _ENTRY_ACTION_SELECTOR = (
        "a, button, [role=button], input[type=button], input[type=submit]"
    )

    async def observe(self, page: Page, job_id: str) -> ObservedPage:
        elements: list[DomElement] = []
        accessibility_trees: list[AccessibilityTreeSnapshot] = []
        locators: dict[str, Locator] = {}

        for frame_index, frame in enumerate(page.frames):
            frame_path = self._frame_path(frame)
            snapshot = await frame.locator("html").aria_snapshot()
            accessibility_trees.append(
                AccessibilityTreeSnapshot(
                    frame_path=frame_path,
                    frame_url=frame.url,
                    snapshot=snapshot,
                )
            )
            prefix = f"sf-{job_id}-{frame_index}"
            raw_elements = await frame.locator(self._FORM_SELECTOR).evaluate_all(
                """
                (items, observation) => items
                  .filter(element => {
                    const style = window.getComputedStyle(element)
                    return style.visibility !== 'hidden'
                      && style.display !== 'none'
                      && !element.disabled
                  })
                  .map((element, index) => {
                    const elementId = `${observation.prefix}-${index}`
                    element.setAttribute('data-smartfill-id', elementId)
                    const labelText = label => {
                      const clone = label.cloneNode(true)
                      clone.querySelectorAll('label, input, textarea, select, button')
                        .forEach(child => child.remove())
                      return (clone.textContent || '').trim()
                    }
                    const closestLabel = element.closest('label')
                    const siblingLabel = element.parentElement
                      ? element.parentElement.querySelector(':scope > label')
                      : null
                    const preferredLabel = closestLabel || siblingLabel
                    const labels = preferredLabel
                      ? labelText(preferredLabel)
                      : (element.labels
                        ? Array.from(element.labels).map(labelText).join(' ')
                        : '')
                    const ariaLabel = element.getAttribute('aria-label') || ''
                    const placeholder = element.getAttribute('placeholder') || ''
                    const name = element.getAttribute('name') || ''
                    const tag = element.tagName.toLowerCase()
                    const type = element.getAttribute('type') || ''
                    let role = element.getAttribute('role') || ''
                    if (!role && tag === 'select') role = 'combobox'
                    if (!role && tag === 'textarea') role = 'textbox'
                    if (!role && tag === 'input') {
                      role = ['checkbox', 'radio', 'button'].includes(type) ? type : 'textbox'
                    }
                    const options = tag === 'select'
                      ? Array.from(element.options).map(option => ({
                          value: option.value,
                          label: option.textContent || ''
                        }))
                      : []
                    const form = element.form || element.closest('form')
                    const region = form || element.closest(
                      'main, [role=main], section, dialog, fieldset'
                    ) || document.body
                    const regionLabel = (
                      region.getAttribute('aria-label')
                      || region.querySelector('h1, h2, h3, legend')?.textContent
                      || ''
                    )
                    const actionText = Array.from(region.querySelectorAll(
                      'button, input[type=submit], input[type=button]'
                    )).map(action => (
                      action.getAttribute('aria-label')
                      || action.innerText
                      || action.value
                      || ''
                    )).join(' ')
                    return {
                      element_id: elementId,
                      tag,
                      input_type: type,
                      label: labels,
                      aria_label: ariaLabel,
                      placeholder,
                      name,
                      autocomplete: element.getAttribute('autocomplete') || '',
                      role,
                      accessible_name: labels || ariaLabel || placeholder || name,
                      form_context: (
                        [regionLabel, actionText].filter(Boolean).join(' ')
                        || document.title
                      ).slice(0, 500),
                      frame_path: observation.framePath,
                      frame_url: observation.frameUrl,
                      tree_scope: element.getRootNode() instanceof ShadowRoot
                        ? 'shadow'
                        : 'document',
                      options
                    }
                  })
                """,
                {
                    "prefix": prefix,
                    "framePath": frame_path,
                    "frameUrl": frame.url,
                },
            )
            frame_elements = [DomElement.model_validate(element) for element in raw_elements]
            elements.extend(frame_elements)
            for element in frame_elements:
                locators[element.element_id] = frame.locator(
                    f'[data-smartfill-id="{element.element_id}"]'
                )

        return ObservedPage(
            observation=PageObservation(
                url=page.url,
                elements=elements,
                accessibility_trees=accessibility_trees,
            ),
            locators=locators,
        )

    async def observe_submission_actions(
        self,
        page: Page,
        job_id: str,
        aliases: list[str],
    ) -> ObservedActions:
        return await self._observe_actions(
            page,
            job_id,
            aliases,
            selector=self._ACTION_SELECTOR,
            namespace="submit",
        )

    async def observe_entry_actions(
        self,
        page: Page,
        job_id: str,
        aliases: list[str],
    ) -> ObservedActions:
        return await self._observe_actions(
            page,
            job_id,
            aliases,
            selector=self._ENTRY_ACTION_SELECTOR,
            namespace="entry",
        )

    async def _observe_actions(
        self,
        page: Page,
        job_id: str,
        aliases: list[str],
        *,
        selector: str,
        namespace: str,
    ) -> ObservedActions:
        candidates: list[InterventionCandidate] = []
        locators: dict[str, Locator] = {}
        normalized_aliases = [_normalize(alias) for alias in aliases]

        for frame_index, frame in enumerate(page.frames):
            frame_path = self._frame_path(frame)
            prefix = f"sf-{namespace}-{job_id}-{frame_index}"
            raw_actions = await frame.locator(selector).evaluate_all(
                """
                (items, observation) => items
                  .filter(element => {
                    const style = window.getComputedStyle(element)
                    return style.visibility !== 'hidden'
                      && style.display !== 'none'
                      && !element.disabled
                  })
                  .map((element, index) => {
                    const elementId = `${observation.prefix}-${index}`
                    element.setAttribute(observation.attribute, elementId)
                    const tag = element.tagName.toLowerCase()
                    const text = (element.innerText || element.value || '').trim()
                    const accessibleName = (
                      element.getAttribute('aria-label')
                      || text
                      || element.getAttribute('title')
                      || element.getAttribute('name')
                      || ''
                    ).trim().slice(0, 300)
                    return {
                      element_id: elementId,
                      accessible_name: accessibleName,
                      role: element.getAttribute('role') || (tag === 'a' ? 'link' : 'button'),
                      tag,
                      frame_path: observation.framePath
                    }
                  })
                """,
                {
                    "prefix": prefix,
                    "framePath": frame_path,
                    "attribute": f"data-smartfill-{namespace}-id",
                },
            )
            for raw_action in raw_actions:
                element_id = str(raw_action["element_id"])
                locator = frame.locator(
                    f'[data-smartfill-{namespace}-id="{element_id}"]'
                )
                if not await locator.is_visible() or not await locator.is_enabled():
                    continue
                accessible_name = str(raw_action["accessible_name"])
                normalized_name = _normalize(accessible_name)
                score = 0.0
                for alias in normalized_aliases:
                    if normalized_name == alias:
                        score = max(score, 1.0)
                    elif alias and alias in normalized_name:
                        score = max(score, 0.92)
                    elif normalized_name and normalized_name in alias:
                        score = max(score, 0.86)
                candidate = InterventionCandidate.model_validate(
                    {**raw_action, "confidence": score}
                )
                candidates.append(candidate)
                locators[candidate.element_id] = locator

        candidates.sort(key=lambda candidate: candidate.confidence, reverse=True)
        return ObservedActions(candidates=candidates, locators=locators)

    @staticmethod
    def _frame_path(frame: Frame) -> str:
        parts: list[str] = []
        current = frame
        while current.parent_frame is not None:
            parent = current.parent_frame
            index = parent.child_frames.index(current)
            parts.append(current.name or f"iframe[{index}]")
            current = parent
        return "/".join(["main", *reversed(parts)])


class PlaywrightBrowserWorker:
    """Run one semantic form-fill job in an isolated Chromium context."""

    _MAX_ACTION_CANDIDATES = 20
    _VISUAL_SEARCH_BATCH_SIZE = 3
    _VISUAL_SEARCH_BATCHES = 3
    _VISUAL_SEARCH_MAX_ATTEMPTS = _VISUAL_SEARCH_BATCH_SIZE * _VISUAL_SEARCH_BATCHES

    def __init__(
        self,
        *,
        secret_store: SecretStore,
        allowed_origins: set[str],
        allowed_origins_provider: Callable[[], set[str]] | None = None,
        artifacts_root: Path,
        headless: bool = True,
        relaxed_manual_navigation: bool = False,
        cdp_url: str | None = None,
        navigation_timeout_ms: int = 30_000,
        action_timeout_ms: int = 10_000,
        screenshot_timeout_ms: int = 10_000,
        vision_provider: VisionProvider | None = None,
    ) -> None:
        self._secret_store = secret_store
        self._allowed_origins = {normalize_origin(origin) for origin in allowed_origins}
        self._allowed_origins_provider = allowed_origins_provider
        self._artifacts_root = artifacts_root
        self._headless = headless
        self._relaxed_manual_navigation = relaxed_manual_navigation
        self._cdp_url = cdp_url
        self._navigation_timeout_ms = navigation_timeout_ms
        self._action_timeout_ms = action_timeout_ms
        self._screenshot_timeout_ms = screenshot_timeout_ms
        self._vision_provider = vision_provider
        self._visual_observer = VisualPageObserver(
            screenshot_timeout_ms=screenshot_timeout_ms
        )
        self._mapper = SemanticFieldMapper()
        self._observer = PlaywrightPageObserver()
        self._sessions: dict[str, _BrowserSession] = {}
        self._authentication_sessions: dict[str, _BrowserSession] = {}
        self._authentication_session_owners: dict[str, str] = {}
        self._retained_session_keys: dict[str, str] = {}
        self._heartbeat_tasks: dict[str, asyncio.Task[None]] = {}

    async def run(
        self,
        request: BrowserRunRequest,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        statistics_started_at = time.perf_counter()
        self._require_allowed_url(request.target_url)
        if request.heartbeat_url is not None:
            self._require_allowed_url(request.heartbeat_url)
        for step in request.workflow_steps:
            self._require_allowed_url(step.target_url)
            if step.heartbeat_url is not None:
                self._require_allowed_url(step.heartbeat_url)
        if request.job_id in self._sessions:
            raise ValueError("Browser job already has an active session")
        if request.authentication_session_key is not None and any(
            active.request.authentication_session_key
            == request.authentication_session_key
            for active in self._sessions.values()
        ):
            raise ValueError("Authentication session is already in use by another job")
        session = await self._take_authentication_session(request)
        if session is None:
            session = await self._open_session(request)
        session.statistics_started_at = statistics_started_at
        self._sessions[request.job_id] = session
        try:
            result = await self._process(session, HumanResolution(), report)
        except Exception:
            await self.cancel(request.job_id)
            raise
        return await self._finalize_session(request.job_id, session, result)

    async def resume(
        self,
        job_id: str,
        resolution: HumanResolution,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        try:
            session = self._sessions[job_id]
        except KeyError as error:
            raise ValueError("Paused browser session is unavailable") from error
        session.consecutive_model_errors = 0
        session.consecutive_semantic_rejections = 0
        try:
            result = await self._process(session, resolution, report)
        except Exception:
            await self.cancel(job_id)
            raise
        return await self._finalize_session(job_id, session, result)

    async def _finalize_session(
        self,
        job_id: str,
        session: _BrowserSession,
        result: BrowserRunResult,
    ) -> BrowserRunResult:
        statistics = self._execution_statistics(session)
        result = result.model_copy(
            update={
                "statistics": statistics,
                "download_paths": list(session.download_paths),
            }
        )
        session_key = session.request.authentication_session_key
        if (
            result.status is BrowserJobStatus.COMPLETED
            and session_key is not None
            and session.manual_login_confirmed
        ):
            self._sessions.pop(job_id, None)
            self._authentication_sessions[session_key] = session
            self._authentication_session_owners[session_key] = job_id
            self._retained_session_keys[job_id] = session_key
            self._start_heartbeat(session_key, session)
            return result.model_copy(
                update={
                    "message": (
                        f"{result.message}; 人工登录会话已保留并启用心跳, "
                        f"后续任务可直接复用; {statistics.summary()}"
                    )[:500],
                    "browser_session_open": True,
                }
            )
        if result.status is BrowserJobStatus.COMPLETED and session.request.keep_browser_open:
            return result.model_copy(
                update={
                    "message": (
                        f"{result.message}; 目标浏览器保持打开, 可检查后手动关闭; "
                        f"{statistics.summary()}"
                    )[:500],
                    "browser_session_open": True,
                }
            )
        if result.status is not BrowserJobStatus.NEED_HUMAN:
            await self.cancel(job_id)
        if result.status is BrowserJobStatus.COMPLETED:
            return result.model_copy(
                update={"message": f"{result.message}; {statistics.summary()}"[:500]}
            )
        return result

    @staticmethod
    def _execution_statistics(session: _BrowserSession) -> ExecutionStatistics:
        return ExecutionStatistics(
            duration_ms=round((time.perf_counter() - session.statistics_started_at) * 1_000),
            screenshot_count=session.screenshot_count,
            model_call_count=session.model_call_count,
            model_latency_ms=session.model_latency_ms,
            browser_action_count=session.browser_action_count,
            click_count=session.click_count,
            scroll_count=session.scroll_count,
            wait_count=session.wait_count,
            fill_count=session.fill_count,
        )

    async def cancel(self, job_id: str) -> None:
        session = self._sessions.pop(job_id, None)
        if session is not None and session.request.authentication_session_key is not None:
            await self._stop_heartbeat(session.request.authentication_session_key)
        if session is None:
            session_key = self._retained_session_keys.pop(job_id, None)
            if (
                session_key is not None
                and self._authentication_session_owners.get(session_key) == job_id
            ):
                self._authentication_session_owners.pop(session_key, None)
                session = self._authentication_sessions.pop(session_key, None)
                await self._stop_heartbeat(session_key)
        if session is None:
            return
        await self._close_session(session)

    async def _close_session(self, session: _BrowserSession) -> None:
        await self._wait_for_downloads(session)
        try:
            await session.context.close()
        finally:
            try:
                if session.owns_browser:
                    await session.browser.close()
            finally:
                await session.playwright.stop()

    async def _take_authentication_session(
        self,
        request: BrowserRunRequest,
    ) -> _BrowserSession | None:
        session_key = request.authentication_session_key
        if session_key is None or session_key not in self._authentication_sessions:
            return None
        existing_session = self._authentication_sessions[session_key]
        if normalize_origin(existing_session.request.target_url) != normalize_origin(
            request.target_url
        ):
            raise ValueError("Authentication session key belongs to a different origin")
        await self._stop_heartbeat(session_key)
        healthy = await self._heartbeat_once(session_key)
        session = self._authentication_sessions.pop(session_key)
        owner = self._authentication_session_owners.pop(session_key, None)
        if owner is not None:
            self._retained_session_keys.pop(owner, None)
        if not healthy or not session.authentication_session_valid:
            await self._close_session(session)
            return None
        self._reset_session_for_request(session, request, authentication_reused=True)
        return session

    @staticmethod
    def _reset_session_for_request(
        session: _BrowserSession,
        request: BrowserRunRequest,
        *,
        authentication_reused: bool,
    ) -> None:
        session.request = request
        session.workflow_steps = request.workflow_steps or None
        session.navigated = False
        session.observed = None
        session.fields_verified = False
        session.completed_fields = 0
        session.submit_actions = None
        session.submission_attempted = False
        session.entry_actions = None
        session.entry_action_performed = False
        session.step_index = 0
        session.step_announced = False
        session.visual_values = {}
        session.visual_definitions = {}
        session.visual_completed = set()
        session.visual_recent_actions = ["reused authenticated browser session"]
        session.visual_last_observation_at = None
        session.last_visual_action_label = None
        session.last_visual_action_key = None
        session.manual_login_confirmed = authentication_reused
        session.authentication_reused = authentication_reused
        session.authentication_session_valid = True
        session.statistics_started_at = time.perf_counter()
        session.screenshot_count = 0
        session.model_call_count = 0
        session.model_latency_ms = 0
        session.browser_action_count = 0
        session.click_count = 0
        session.scroll_count = 0
        session.wait_count = 0
        session.fill_count = 0
        session.consecutive_model_errors = 0
        session.consecutive_semantic_rejections = 0
        session.download_paths = []
        session.download_tasks = set()
        session.download_error = None

    def _start_heartbeat(self, session_key: str, session: _BrowserSession) -> None:
        existing = self._heartbeat_tasks.pop(session_key, None)
        if existing is not None:
            existing.cancel()
        self._heartbeat_tasks[session_key] = asyncio.create_task(
            self._heartbeat_loop(session_key, session),
            name=f"smartfill-auth-heartbeat-{session_key}",
        )

    async def _stop_heartbeat(self, session_key: str) -> None:
        task = self._heartbeat_tasks.pop(session_key, None)
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _heartbeat_loop(
        self,
        session_key: str,
        session: _BrowserSession,
    ) -> None:
        try:
            while (
                self._authentication_sessions.get(session_key) is session
                or any(active is session for active in self._sessions.values())
            ):
                await asyncio.sleep(session.request.heartbeat_interval_seconds)
                if not await self._heartbeat_once(session_key, session):
                    return
        except asyncio.CancelledError:
            raise
        finally:
            if self._heartbeat_tasks.get(session_key) is asyncio.current_task():
                self._heartbeat_tasks.pop(session_key, None)

    async def _heartbeat_once(
        self,
        session_key: str,
        session: _BrowserSession | None = None,
    ) -> bool:
        session = session or self._authentication_sessions.get(session_key)
        if session is None or session.request.heartbeat_url is None:
            return False
        try:
            heartbeat_page = session.heartbeat_page
            if heartbeat_page is None or heartbeat_page.is_closed():
                heartbeat_page = await session.context.new_page()
                session.heartbeat_page = heartbeat_page
            response = await heartbeat_page.goto(
                session.request.heartbeat_url,
                wait_until="domcontentloaded",
                timeout=self._action_timeout_ms,
            )
            healthy = (
                response is not None
                and 200 <= response.status < 300
                and self._same_page_url(
                    heartbeat_page.url,
                    session.request.heartbeat_url,
                )
            )
        except Exception:
            healthy = False
        session.authentication_session_valid = healthy
        return healthy

    async def _open_session(self, request: BrowserRunRequest) -> _BrowserSession:
        playwright = await async_playwright().start()
        try:
            manual_login = self._uses_manual_authentication(request)
            if self._cdp_url:
                browser = await playwright.chromium.connect_over_cdp(
                    self._cdp_url,
                    timeout=self._navigation_timeout_ms,
                    is_local=self._is_loopback(self._cdp_url),
                )
                owns_browser = False
            else:
                browser = await playwright.chromium.launch(
                    headless=self._headless and not manual_login
                )
                owns_browser = True

            context = await browser.new_context(
                accept_downloads=True,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
            )
            context.set_default_timeout(self._action_timeout_ms)
            trusted_navigation_origins: set[str] = set()

            async def route_handler(route: Route, browser_request: Request) -> None:
                await self._route_allowed_requests(
                    route,
                    browser_request,
                    job_id=request.job_id,
                    allow_new_https_origin=(
                        self._relaxed_manual_navigation and manual_login
                    ),
                    session_origins=trusted_navigation_origins,
                )

            await context.route("**/*", route_handler)
            page = await context.new_page()
            session = _BrowserSession(
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
                request=request,
                owns_browser=owns_browser,
                trusted_navigation_origins=trusted_navigation_origins,
                workflow_steps=request.workflow_steps or None,
            )
            self._watch_downloads(session, page)
            return session
        except Exception:
            await playwright.stop()
            raise

    async def _process(
        self,
        session: _BrowserSession,
        resolution: HumanResolution,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        while True:
            steps = session.workflow_steps
            if steps:
                step = steps[session.step_index]
                session.request = session.request.model_copy(
                    update={
                        "target_url": step.target_url,
                        "fields": step.fields,
                        "field_definitions": step.field_definitions,
                        "submission": step.submission,
                        "entry_action": step.entry_action,
                        "target_intent": step.target_intent,
                        "authentication_mode": step.authentication_mode,
                        "observation_interval_seconds": step.observation_interval_seconds,
                        "authentication_session_key": (
                            step.authentication_session_key
                            or session.request.authentication_session_key
                        ),
                        "heartbeat_url": step.heartbeat_url or session.request.heartbeat_url,
                        "heartbeat_interval_seconds": (
                            step.heartbeat_interval_seconds
                            if step.authentication_session_key is not None
                            else session.request.heartbeat_interval_seconds
                        ),
                    }
                )
                if not session.step_announced:
                    await report(
                        JobProgress(
                            status=BrowserJobStatus.STARTING,
                            message=(
                                f"开始工作流步骤 {session.step_index + 1}/{len(steps)}: "
                                f"{step.name}"
                            ),
                            current_url=step.target_url,
                            completed_fields=session.completed_fields,
                            current_step=session.step_index + 1,
                            current_step_name=step.name,
                        )
                    )
                    session.step_announced = True

            result = await self._process_step(session, resolution, report)
            if result.status is BrowserJobStatus.NEED_HUMAN or not steps:
                return result
            if session.step_index >= len(steps) - 1:
                return result.model_copy(
                    update={
                        "message": f"工作流 {len(steps)} 个步骤全部执行完成",
                        "current_step": len(steps),
                        "current_step_name": steps[-1].name,
                    }
                )

            session.step_index += 1
            session.step_announced = False
            session.navigated = False
            session.observed = None
            session.fields_verified = False
            session.submit_actions = None
            session.submission_attempted = False
            session.entry_actions = None
            session.entry_action_performed = False
            session.visual_values = {}
            session.visual_definitions = {}
            session.visual_completed = set()
            session.visual_recent_actions = []
            session.visual_last_observation_at = None
            session.last_visual_action_label = None
            session.last_visual_action_key = None
            session.consecutive_semantic_rejections = 0
            resolution = HumanResolution()

    async def _process_step(
        self,
        session: _BrowserSession,
        resolution: HumanResolution,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        request = session.request
        page = session.page
        completed_fields = session.completed_fields
        screenshot_path: str | None = None
        if not session.navigated:
            if self._same_page_url(page.url, request.target_url):
                await report(
                    JobProgress(
                        status=BrowserJobStatus.OBSERVING,
                        message="目标地址未变化, 复用当前页面会话",
                        current_url=page.url,
                    )
                )
            else:
                await report(
                    JobProgress(
                        status=BrowserJobStatus.NAVIGATING,
                        message="正在访问目标页面",
                        current_url=request.target_url,
                    )
                )
                await page.goto(
                    request.target_url,
                    wait_until="domcontentloaded",
                    timeout=self._navigation_timeout_ms,
                )
            self._require_allowed_url(
                page.url,
                session_origins=session.trusted_navigation_origins,
            )
            session.navigated = True

        if (
            request.authentication_session_key is not None
            and not session.authentication_session_valid
        ):
            session.manual_login_confirmed = False
        if (
            request.authentication_mode is AuthenticationMode.MANUAL
            or request.authentication_session_key is not None
        ) and not session.manual_login_confirmed:
            if resolution.manual_login_completed:
                session.manual_login_confirmed = True
                session.authentication_session_valid = True
                session.visual_recent_actions.append("user confirmed manual login")
                if request.authentication_session_key is not None:
                    self._start_heartbeat(request.authentication_session_key, session)
            else:
                screenshot_path = await self._capture(page, request.job_id)
                return BrowserRunResult(
                    status=BrowserJobStatus.NEED_HUMAN,
                    message="等待用户在真实浏览器中完成手机、邮箱或其他人工登录验证",
                    current_url=page.url,
                    completed_fields=completed_fields,
                    screenshot_path=screenshot_path,
                    intervention=HumanIntervention(
                        kind=InterventionKind.MANUAL_LOGIN,
                        instruction=(
                            "请在保留的目标浏览器中完成登录和验证码, 确认已进入登录后页面再继续"
                        ),
                        requires_browser_interaction=True,
                    ),
                )

        if not request.target_intent and self._vision_provider is not None:
            request = request.model_copy(
                update={"target_intent": "找到当前页面的目标业务表单并填写预设资料"}
            )
            session.request = request
        if request.target_intent:
            return await self._process_visual_step(session, resolution, report)

        should_enter = request.entry_action.mode is EntryActionMode.CLICK
        if (
            request.entry_action.mode in {EntryActionMode.AUTO, EntryActionMode.CLICK}
            and not session.entry_action_performed
        ):
            session.observed = await self._observer.observe(page, request.job_id)
            if self._requested_form_is_present(
                session.observed,
                request.fields,
                request.field_definitions,
            ):
                should_enter = False
                if request.entry_action.mode is EntryActionMode.CLICK:
                    session.entry_action_performed = True
            elif request.entry_action.mode is EntryActionMode.AUTO:
                should_enter = self._should_auto_enter(request, session.observed)

        if should_enter and not session.entry_action_performed:
            dismissed = await self._dismiss_safe_popup(page)
            if dismissed:
                await report(
                    JobProgress(
                        status=BrowserJobStatus.OBSERVING,
                        message="已关闭弹窗, 正在识别表单入口",
                        current_url=page.url,
                    )
                )
            if resolution.approve_entry_action:
                await self._click_entry_action(
                    session,
                    resolution.entry_element_id,
                    report=report,
                )
            else:
                aliases = (
                    request.entry_action.aliases
                    if request.entry_action.mode is EntryActionMode.CLICK
                    else self._auto_entry_aliases(request)
                )
                actions = await self._observer.observe_entry_actions(
                    page,
                    request.job_id,
                    aliases,
                )
                session.entry_actions = actions
                matched = [
                    candidate
                    for candidate in actions.candidates
                    if candidate.confidence > 0
                ]
                selected = self._unique_action_candidate(matched)
                if selected is not None:
                    await self._click_entry_action(
                        session,
                        selected.element_id,
                        report=report,
                    )
                else:
                    candidates = (matched or actions.candidates)[
                        : self._MAX_ACTION_CANDIDATES
                    ]
                    screenshot_path = await self._capture(page, request.job_id)
                    return BrowserRunResult(
                        status=BrowserJobStatus.NEED_HUMAN,
                        message="表单入口无法唯一识别, 需要人工确认",
                        current_url=page.url,
                        completed_fields=completed_fields,
                        screenshot_path=screenshot_path,
                        intervention=HumanIntervention(
                            kind=InterventionKind.ENTRY_ACTION_CONFIRMATION,
                            instruction=(
                                "选择当前页面的表单入口; 系统只允许点击本次扫描候选"
                            ),
                            entry_candidates=candidates,
                            requires_browser_interaction=not candidates,
                        ),
                    )
            page = session.page

        await report(
            JobProgress(
                status=BrowserJobStatus.OBSERVING,
                message="正在读取完整 Accessibility Tree、iframe、Shadow DOM 和表单语义",
                current_url=page.url,
                entry_action_performed=session.entry_action_performed,
            )
        )
        if await self._requires_human(page):
            screenshot_path = await self._capture(page, request.job_id)
            return BrowserRunResult(
                status=BrowserJobStatus.NEED_HUMAN,
                message="检测到验证码或 MFA, 请在保留的浏览器会话中处理",
                current_url=page.url,
                completed_fields=0,
                screenshot_path=screenshot_path,
                entry_action_performed=session.entry_action_performed,
                intervention=HumanIntervention(
                    kind=InterventionKind.HUMAN_CHALLENGE,
                    instruction="在浏览器中完成验证码或 MFA, 然后点击重新扫描",
                    requires_browser_interaction=True,
                ),
            )

        dismissed = False
        if not session.entry_action_performed:
            dismissed = await self._dismiss_safe_popup(page)
        if dismissed:
            await report(
                JobProgress(
                    status=BrowserJobStatus.OBSERVING,
                    message="已关闭弹窗, 正在重新读取页面",
                    current_url=page.url,
                )
            )

        if not session.fields_verified:
            observed = await self._observer.observe(page, request.job_id)
            session.observed = observed
            elements = observed.elements
            definitions = {
                definition.key: definition for definition in request.field_definitions
            }
            matches = self._mapper.map_fields(elements, request.field_definitions)
            current_elements = {element.element_id: element for element in elements}
            targets = {
                match.canonical_field: (
                    match,
                    current_elements[match.element_id],
                    observed.locators[match.element_id],
                )
                for match in matches
            }
            missing_fields = set(request.fields) - targets.keys()
            if missing_fields:
                screenshot_path = await self._capture(page, request.job_id)
                return BrowserRunResult(
                    status=BrowserJobStatus.NEED_HUMAN,
                    message="当前页面无法高置信完成理解, 需要人工处理页面状态",
                    current_url=page.url,
                    completed_fields=completed_fields,
                    screenshot_path=screenshot_path,
                    intervention=HumanIntervention(
                        kind=InterventionKind.VISUAL_REVIEW,
                        instruction=(
                            "请在保留的浏览器中处理遮挡、滚动或页面状态后继续视觉识别; "
                            "不再提供 DOM 候选字段映射。"
                        ),
                        requires_browser_interaction=True,
                    ),
                )

            ordered_targets = [targets[canonical] for canonical in request.fields]
            for match, element, locator in ordered_targets:
                canonical = match.canonical_field
                value = self._resolve_value(request.fields[canonical])
                await report(
                    JobProgress(
                        status=BrowserJobStatus.FILLING,
                        message=f"正在填写 {canonical}",
                        current_url=page.url,
                        current_field=canonical,
                        completed_fields=completed_fields,
                    )
                )
                definition = definitions[canonical]
                expected_value = await self._fill(locator, element, definition, value)
                await report(
                    JobProgress(
                        status=BrowserJobStatus.VERIFYING,
                        message=f"正在回读验证 {canonical}",
                        current_url=page.url,
                        current_field=canonical,
                        completed_fields=completed_fields,
                    )
                )
                actual_value = await locator.input_value()
                if actual_value != expected_value:
                    screenshot_path = await self._capture(page, request.job_id)
                    return BrowserRunResult(
                        status=BrowserJobStatus.NEED_HUMAN,
                        message=f"字段 {canonical} 回读验证不一致",
                        current_url=page.url,
                        current_field=canonical,
                        completed_fields=completed_fields,
                        screenshot_path=screenshot_path,
                        intervention=HumanIntervention(
                            kind=InterventionKind.VERIFICATION,
                            instruction="请检查当前页面状态后继续视觉识别",
                            requires_browser_interaction=True,
                        ),
                    )
                completed_fields += 1
                session.completed_fields = completed_fields
                if definition.sensitive:
                    await locator.evaluate("element => element.style.filter = 'blur(7px)'")
                screenshot_path = await self._capture(page, request.job_id)
            session.fields_verified = True

        return await self._handle_submission(
            session,
            resolution,
            report,
            screenshot_path,
        )

    async def _process_visual_step(
        self,
        session: _BrowserSession,
        resolution: HumanResolution,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        if self._vision_provider is None:
            raise RuntimeError(
                "目标驱动任务需要视觉模型; 请配置 SMARTFILL_DASHSCOPE_API_KEY"
            )
        request = session.request
        page = session.page
        if not session.visual_values:
            session.visual_values.update(request.fields)
            session.visual_definitions.update(
                {definition.key: definition for definition in request.field_definitions}
            )
        if resolution.field_values:
            for key, value in resolution.field_values.items():
                session.visual_values[key] = value
                if key not in session.visual_definitions:
                    session.visual_definitions[key] = FieldDefinition(
                        key=key,
                        display_name=key,
                        aliases=[key],
                        sensitive=value.startswith("secret://"),
                    )

        for observation_number in range(1, 41):
            await self._wait_for_visual_cadence(session)
            page_switch_error = await self._adopt_latest_business_page(session, report)
            page = session.page
            if page_switch_error is not None:
                return self._visual_review_result(
                    page.url,
                    len(session.visual_completed),
                    None,
                    page_switch_error,
                )
            await self._wait_for_downloads(session)
            screenshot_path = (
                self._artifacts_root
                / "jobs"
                / request.job_id
                / f"visual-{session.step_index + 1}-{observation_number:02d}.png"
            )
            observed = await self._visual_observer.observe(page, screenshot_path)
            session.screenshot_count += 1
            session.visual_last_observation_at = asyncio.get_running_loop().time()
            await report(
                JobProgress(
                    status=BrowserJobStatus.OBSERVING,
                    message=(
                        f"正在分析页面截图并寻找目标入口 (第 {observation_number} 次截图)"
                    ),
                    current_url=page.url,
                    completed_fields=len(session.visual_completed),
                    screenshot_path=str(screenshot_path.resolve()),
                    entry_action_performed=session.entry_action_performed,
                    statistics=self._execution_statistics(session),
                )
            )
            resolved_screenshot_path = str(screenshot_path.resolve())
            if session.download_error is not None:
                return self._visual_review_result(
                    page.url,
                    len(session.visual_completed),
                    resolved_screenshot_path,
                    f"目标文档下载失败: {session.download_error}",
                )
            if session.download_paths and "下载" in request.target_intent:
                filenames = "、".join(
                    Path(path).name for path in session.download_paths
                )
                return BrowserRunResult(
                    status=BrowserJobStatus.COMPLETED,
                    message=f"已完成目标文档下载并截取结果页面: {filenames}"[:500],
                    current_url=page.url,
                    completed_fields=len(session.visual_completed),
                    screenshot_path=resolved_screenshot_path,
                    download_paths=list(session.download_paths),
                    entry_action_performed=session.entry_action_performed,
                )
            if self._last_action_completes_visual_goal(
                request.target_intent,
                session.last_visual_action_label,
            ):
                return BrowserRunResult(
                    status=BrowserJobStatus.COMPLETED,
                    message=(
                        f"已执行目标末步“{session.last_visual_action_label}”并完成结果截图"
                    )[:500],
                    current_url=page.url,
                    completed_fields=len(session.visual_completed),
                    screenshot_path=resolved_screenshot_path,
                    entry_action_performed=session.entry_action_performed,
                )
            if self._entry_goal_completed_at_login(session, observed):
                return BrowserRunResult(
                    status=BrowserJobStatus.COMPLETED,
                    message="已找到目标入口; 入口后页面要求登录, 本次查找任务已完成",
                    current_url=page.url,
                    completed_fields=len(session.visual_completed),
                    screenshot_path=str(screenshot_path.resolve()),
                    entry_action_performed=True,
                )
            target_region = self._target_region_action(
                request.target_intent,
                observed,
            )
            if target_region is not None:
                action_error = await self._execute_visual_action(
                    session,
                    observed,
                    target_region,
                    report,
                    False,
                )
                if action_error is None:
                    session.consecutive_semantic_rejections = 0
                    continue
                return self._visual_review_result(
                    page.url,
                    len(session.visual_completed),
                    str(screenshot_path.resolve()),
                    action_error,
                )
            region_confirmation = self._region_confirmation_action(observed)
            if region_confirmation is not None:
                action_error = await self._execute_visual_action(
                    session,
                    observed,
                    region_confirmation,
                    report,
                    False,
                )
                if action_error is None:
                    session.consecutive_semantic_rejections = 0
                    continue
                return self._visual_review_result(
                    page.url,
                    len(session.visual_completed),
                    str(screenshot_path.resolve()),
                    action_error,
                )

            session.model_call_count += 1
            task = self._visual_task_prompt(session)
            model_started_at = time.perf_counter()
            try:
                decision = await self._vision_provider.analyze(
                    VisionRequest(
                        task=task,
                        observation=observed.observation,
                        canonical_fields=list(session.visual_values),
                    )
                )
            except VisionResponseError as error:
                session.consecutive_model_errors += 1
                session.visual_recent_actions.append(
                    "previous model decision was rejected as ungrounded; use only current IDs"
                )
                session.visual_recent_actions = session.visual_recent_actions[-8:]
                if session.consecutive_model_errors >= 3:
                    return self._visual_review_result(
                        page.url,
                        len(session.visual_completed),
                        str(screenshot_path.resolve()),
                        f"视觉模型连续 3 次返回不可执行决策: {error}",
                    )
                continue
            finally:
                session.model_latency_ms += round(
                    (time.perf_counter() - model_started_at) * 1_000
                )
            session.consecutive_model_errors = 0
            if self._decision_requires_human(decision.interruptions):
                return BrowserRunResult(
                    status=BrowserJobStatus.NEED_HUMAN,
                    message="视觉模型检测到验证码、MFA 或需要人工处理的页面状态",
                    current_url=page.url,
                    completed_fields=len(session.visual_completed),
                    screenshot_path=str(screenshot_path.resolve()),
                    intervention=HumanIntervention(
                        kind=InterventionKind.HUMAN_CHALLENGE,
                        instruction="请在保留的浏览器中处理页面挑战, 完成后继续视觉识别",
                        requires_browser_interaction=True,
                    ),
                )

            missing = [
                field
                for field in decision.required_fields
                if field.key not in session.visual_values
            ]
            if missing:
                required = [
                    RequiredDataField(
                        key=field.key,
                        display_name=field.display_name,
                        input_kind=field.input_kind,
                        sensitive=field.sensitive,
                        reason=field.reason,
                    )
                    for field in missing
                ]
                session.visual_definitions.update(
                    {
                        field.key: FieldDefinition(
                            key=field.key,
                            display_name=field.display_name,
                            aliases=[field.display_name],
                            input_kind=field.input_kind,
                            sensitive=field.sensitive,
                        )
                        for field in required
                    }
                )
                return BrowserRunResult(
                    status=BrowserJobStatus.NEED_HUMAN,
                    message=(
                        "客户预设资料不完整: "
                        + "、".join(field.display_name for field in required)
                    ),
                    current_url=page.url,
                    completed_fields=len(session.visual_completed),
                    screenshot_path=str(screenshot_path.resolve()),
                    intervention=HumanIntervention(
                        kind=InterventionKind.DATA_REQUIRED,
                        instruction="请补充目标表单所需资料, 提交后从当前页面继续执行",
                        missing_fields=required,
                    ),
                )

            filled = await self._execute_visual_mappings(
                session,
                observed,
                decision.mappings,
                report,
            )
            if filled:
                session.consecutive_semantic_rejections = 0
                continue

            action = decision.next_action
            if action is None:
                search_result = await self._record_visual_search_miss(
                    session,
                    report,
                    page.url,
                    resolved_screenshot_path,
                    "视觉模型没有给出可执行的下一步",
                )
                if search_result is not None:
                    return search_result
                continue
            if action.action is ActionType.FINISH:
                if not decision.target_reached:
                    search_result = await self._record_visual_search_miss(
                        session,
                        report,
                        page.url,
                        resolved_screenshot_path,
                        "模型在找到目标表单前尝试结束任务",
                    )
                    if search_result is not None:
                        return search_result
                    continue
                return BrowserRunResult(
                    status=BrowserJobStatus.COMPLETED,
                    message="视觉模型已找到目标入口并完成资料填写",
                    current_url=page.url,
                    completed_fields=len(session.visual_completed),
                    screenshot_path=str(screenshot_path.resolve()),
                    entry_action_performed=session.entry_action_performed,
                )
            if action.action is ActionType.REQUEST_HUMAN:
                if self._decision_requires_human([action.evidence or ""]):
                    return self._visual_review_result(
                        page.url,
                        len(session.visual_completed),
                        resolved_screenshot_path,
                        action.evidence or "页面存在必须人工处理的验证",
                    )
                search_result = await self._record_visual_search_miss(
                    session,
                    report,
                    page.url,
                    resolved_screenshot_path,
                    action.evidence or "视觉模型请求人工确认页面状态",
                )
                if search_result is not None:
                    return search_result
                continue
            action = self._prefer_progressing_target_action(
                request.target_intent,
                observed,
                action,
                last_action_label=session.last_visual_action_label,
            )
            if not self._action_matches_target_context(
                request.target_intent,
                observed,
                action,
            ):
                element = next(
                    (
                        item
                        for item in observed.observation.elements
                        if item.element_id == action.element_id
                    ),
                    None,
                )
                context = element.context if element is not None else ""
                session.visual_recent_actions.append(
                    "rejected unrelated repeated action: "
                    f"{action.accessible_name or action.element_id}; row={context[:120]}"
                )
                session.visual_recent_actions = session.visual_recent_actions[-8:]
                search_result = await self._record_visual_search_miss(
                    session,
                    report,
                    page.url,
                    resolved_screenshot_path,
                    "视觉模型选择了与目标路径不一致的入口",
                )
                if search_result is not None:
                    return search_result
                continue
            session.consecutive_semantic_rejections = 0

            policy = ActionPolicy(
                self._current_allowed_origins() | session.trusted_navigation_origins,
                auto_submit=True,
            ).evaluate(action, current_url=page.url)
            if not policy.allowed or policy.requires_human:
                return self._visual_review_result(
                    page.url,
                    len(session.visual_completed),
                    str(screenshot_path.resolve()),
                    policy.reason,
                )
            try:
                action_error = await self._execute_visual_action(
                    session,
                    observed,
                    action,
                    report,
                    decision.target_reached,
                )
            except PlaywrightTimeoutError:
                session.visual_recent_actions.append(
                    "grounded action became blocked or stale; re-observe current screenshot"
                )
                session.visual_recent_actions = session.visual_recent_actions[-8:]
                continue
            if action_error is not None:
                return self._visual_review_result(
                    page.url,
                    len(session.visual_completed),
                    str(screenshot_path.resolve()),
                    action_error,
                )

        return self._visual_review_result(
            page.url,
            len(session.visual_completed),
            None,
            "达到 40 次视觉观察上限, 已安全暂停",
        )

    async def _record_visual_search_miss(
        self,
        session: _BrowserSession,
        report: Callable[[JobProgress], Awaitable[None]],
        current_url: str,
        screenshot_path: str,
        reason: str,
    ) -> BrowserRunResult | None:
        session.consecutive_semantic_rejections += 1
        attempt = session.consecutive_semantic_rejections
        session.visual_recent_actions.append(
            f"search miss {attempt}/{self._VISUAL_SEARCH_MAX_ATTEMPTS}: {reason}"
        )
        session.visual_recent_actions = session.visual_recent_actions[-8:]
        if attempt >= self._VISUAL_SEARCH_MAX_ATTEMPTS:
            return BrowserRunResult(
                status=BrowserJobStatus.COMPLETED,
                message=(
                    "已完成 3*3 (共 9 次)视觉查找, "
                    "未发现与任务匹配的入口, 任务按无入口正常结束"
                ),
                current_url=current_url,
                completed_fields=len(session.visual_completed),
                screenshot_path=screenshot_path,
                entry_action_performed=session.entry_action_performed,
            )
        if attempt % self._VISUAL_SEARCH_BATCH_SIZE == 0:
            completed_batch = attempt // self._VISUAL_SEARCH_BATCH_SIZE
            await report(
                JobProgress(
                    status=BrowserJobStatus.OBSERVING,
                    message=(
                        f"第 {completed_batch}/{self._VISUAL_SEARCH_BATCHES} 轮的 "
                        f"{self._VISUAL_SEARCH_BATCH_SIZE} 次查找未命中, 自动进入下一轮"
                    ),
                    current_url=current_url,
                    completed_fields=len(session.visual_completed),
                    screenshot_path=screenshot_path,
                    entry_action_performed=session.entry_action_performed,
                    statistics=self._execution_statistics(session),
                )
            )
        return None

    async def _execute_visual_mappings(
        self,
        session: _BrowserSession,
        observed: ObservedVisualPage,
        mappings: Sequence[FieldMapping],
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> int:
        observation = observed.observation
        locators = observed.locators
        elements = {element.element_id: element for element in observation.elements}
        count = 0
        for raw_mapping in mappings:
            canonical = raw_mapping.canonical_field
            element_id = raw_mapping.element_id
            confidence = raw_mapping.confidence
            if confidence < 0.85 or canonical not in session.visual_values:
                continue
            element = elements.get(element_id)
            locator = locators.get(element_id)
            if element is None or locator is None or element.tag not in {
                "input",
                "textarea",
                "select",
            }:
                continue
            if element.filled:
                continue
            await report(
                JobProgress(
                    status=BrowserJobStatus.FILLING,
                    message=f"视觉匹配成功, 正在填写 {canonical}",
                    current_url=session.page.url,
                    current_field=canonical,
                    completed_fields=len(session.visual_completed),
                )
            )
            value = self._resolve_value(session.visual_values[canonical])
            checked_value: bool | None = None
            if element.input_type == "checkbox":
                checked_value = self._parse_checkbox_value(canonical, value)
                await locator.set_checked(checked_value)
            elif element.tag == "select":
                try:
                    await locator.select_option(label=value)
                except PlaywrightTimeoutError:
                    await locator.select_option(value=value)
            else:
                await locator.fill(value)
            await locator.evaluate(
                "element => element.dataset.smartfillVisionFilled = 'true'"
            )
            value_matches = (
                await locator.is_checked() == checked_value
                if checked_value is not None
                else await locator.input_value() == value
            )
            if not value_matches:
                raise ValueError(f"视觉字段 {canonical} 回读验证不一致")
            definition = session.visual_definitions.get(canonical)
            if definition is not None and definition.sensitive:
                await locator.evaluate("element => element.style.filter = 'blur(7px)'")
            session.visual_completed.add(canonical)
            session.fill_count += 1
            session.browser_action_count += 1
            session.visual_recent_actions.append(f"filled {canonical}")
            session.visual_recent_actions = session.visual_recent_actions[-8:]
            count += 1
            await report(
                JobProgress(
                    status=BrowserJobStatus.VERIFYING,
                    message=f"已回读验证 {canonical}",
                    current_url=session.page.url,
                    current_field=canonical,
                    completed_fields=len(session.visual_completed),
                )
            )
        return count

    @staticmethod
    def _parse_checkbox_value(canonical: str, value: str) -> bool:
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "y", "是", "勾选"}:
            return True
        if normalized in {"0", "false", "no", "n", "否", "不勾选"}:
            return False
        raise ValueError(f"视觉复选字段 {canonical} 需要明确的是/否值")

    async def _execute_visual_action(
        self,
        session: _BrowserSession,
        observed: ObservedVisualPage,
        action: ActionProposal,
        report: Callable[[JobProgress], Awaitable[None]],
        target_reached: bool,
    ) -> str | None:
        action_type = action.action
        element_id = action.element_id
        locators = observed.locators
        locator = locators.get(str(element_id)) if element_id is not None else None
        element: BrowserElement | None = None
        if action_type in {ActionType.CLICK, ActionType.SUBMIT}:
            if locator is None:
                return "模型引用的点击目标不在当前截图中"
            element = next(
                (
                    item
                    for item in observed.observation.elements
                    if item.element_id == element_id
                ),
                None,
            )
            action_label = (
                action.accessible_name
                or (element.accessible_name if element is not None else "")
                or str(element_id)
            )
            action_key = "|".join(
                (
                    action_type.value,
                    session.page.url,
                    str(element_id),
                    _normalize(action_label),
                )
            )
            if action_key == session.last_visual_action_key:
                return (
                    f"检测到视觉模型在未推进的页面上重复点击“{action_label}”; "
                    "已暂停以避免不断打开重复标签页"
                )
            if element is not None and element.tag == "a":
                raw_href = await locator.get_attribute("href")
                if raw_href:
                    destination = urljoin(session.page.url, raw_href)
                    if urlsplit(destination).scheme in {"http", "https"}:
                        try:
                            destination_origin = normalize_origin(destination)
                        except ValueError:
                            destination_origin = ""
                        destination_allowed = destination_origin in (
                            self._current_allowed_origins()
                            | session.trusted_navigation_origins
                        )
                        relaxed_navigation = (
                            self._relaxed_manual_navigation
                            and self._uses_manual_authentication(session.request)
                            and urlsplit(destination).username is None
                            and urlsplit(destination).password is None
                        )
                        if not destination_allowed and not relaxed_navigation:
                            return (
                                f"点击“{action_label}”会触发未授权跳转, 目标 Origin 为 "
                                f"{destination_origin or destination}; 已停止且未打开新标签页"
                            )
            if action_type is ActionType.SUBMIT and target_reached:
                if session.request.submission.policy is SubmissionPolicy.FILL_ONLY:
                    return "当前策略禁止提交目标表单"
                if session.request.submission.policy is SubmissionPolicy.CONFIRM_BEFORE_SUBMIT:
                    return "目标表单提交需要人工确认"
            await report(
                JobProgress(
                    status=(
                        BrowserJobStatus.SUBMITTING
                        if action_type is ActionType.SUBMIT
                        else BrowserJobStatus.ENTERING
                    ),
                    message=(
                        f"视觉模型已定位并执行 {action_label} ({element_id}; "
                        f"高置信自动执行 {action.confidence:.2f})"
                    ),
                    current_url=session.page.url,
                    completed_fields=len(session.visual_completed),
                )
            )
            previous_page = session.page
            previous_pages = set(session.context.pages)
            opens_new_tab = (
                element is not None
                and element.tag == "a"
                and (await locator.get_attribute("target") or "").casefold() == "_blank"
            )
            if opens_new_tab:
                async with session.context.expect_page(
                    timeout=self._action_timeout_ms
                ) as page_info:
                    await locator.click()
                opened_pages = [await page_info.value]
            else:
                await locator.click()
                await asyncio.sleep(0)
                opened_pages = [
                    page for page in session.context.pages if page not in previous_pages
                ]
            if opened_pages:
                opened_page = opened_pages[-1]
                with suppress(PlaywrightTimeoutError):
                    await opened_page.wait_for_load_state("domcontentloaded", timeout=2_000)
                try:
                    self._require_allowed_url(
                        opened_page.url,
                        session_origins=session.trusted_navigation_origins,
                    )
                except ValueError:
                    blocked_url = opened_page.url
                    await opened_page.close()
                    session.page = previous_page
                    try:
                        blocked_origin = normalize_origin(blocked_url)
                    except ValueError:
                        blocked_origin = blocked_url
                    return (
                        f"新标签页跳转到未授权 Origin {blocked_origin}; "
                        "已关闭该标签页并暂停执行"
                    )
                self._watch_downloads(session, opened_page)
                session.page = opened_page
                await opened_page.bring_to_front()
            session.last_visual_action_key = action_key
            session.last_visual_action_label = action_label
            session.entry_action_performed = True
            session.click_count += 1
            session.browser_action_count += 1
        elif action_type is ActionType.SCROLL:
            viewport = session.page.viewport_size or {"height": 900}
            await session.page.mouse.wheel(0, round(viewport["height"] * 0.75))
            session.scroll_count += 1
            session.browser_action_count += 1
        elif action_type is ActionType.WAIT:
            session.wait_count += 1
            session.browser_action_count += 1
        else:
            return f"目标驱动执行暂不支持动作 {action_type.value}"
        action_label = action.accessible_name or (
            element.accessible_name
            if action_type in {ActionType.CLICK, ActionType.SUBMIT}
            and element is not None
            else ""
        )
        session.visual_recent_actions.append(
            f"{action_type.value} {element_id or ''} {action_label}".strip()
        )
        session.visual_recent_actions = session.visual_recent_actions[-8:]
        with suppress(PlaywrightTimeoutError):
            await session.page.wait_for_load_state("domcontentloaded", timeout=2_000)
        return None

    async def _adopt_latest_business_page(
        self,
        session: _BrowserSession,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> str | None:
        business_pages = [
            page
            for page in session.context.pages
            if not page.is_closed() and page is not session.heartbeat_page
        ]
        if not business_pages:
            return "浏览器中已没有可继续操作的业务页面"

        latest_page = business_pages[-1]
        if latest_page is session.page:
            return None

        with suppress(PlaywrightTimeoutError):
            await latest_page.wait_for_load_state("domcontentloaded", timeout=2_000)
        if latest_page.url == "about:blank":
            with suppress(PlaywrightTimeoutError):
                await latest_page.wait_for_url(
                    lambda url: url != "about:blank",
                    timeout=2_000,
                )
        if latest_page.url == "about:blank":
            return None

        try:
            self._require_allowed_url(
                latest_page.url,
                session_origins=session.trusted_navigation_origins,
            )
        except ValueError:
            blocked_url = latest_page.url
            await latest_page.close()
            if not session.page.is_closed():
                await session.page.bring_to_front()
            try:
                blocked_origin = normalize_origin(blocked_url)
            except ValueError:
                blocked_origin = blocked_url
            return (
                f"最新标签页跳转到未授权 Origin {blocked_origin}; "
                "已关闭该标签页并暂停执行"
            )

        self._watch_downloads(session, latest_page)
        session.page = latest_page
        await latest_page.bring_to_front()
        await report(
            JobProgress(
                status=BrowserJobStatus.OBSERVING,
                message="检测到新标签页, 已切换到最新业务页面继续截图和执行",
                current_url=latest_page.url,
                completed_fields=len(session.visual_completed),
                entry_action_performed=session.entry_action_performed,
                statistics=self._execution_statistics(session),
            )
        )
        return None

    def _watch_downloads(self, session: _BrowserSession, page: Page) -> None:
        page.on("download", lambda download: self._queue_download(session, download))

    def _queue_download(self, session: _BrowserSession, download: Download) -> None:
        task = asyncio.create_task(self._save_download(session, download))
        session.download_tasks.add(task)

        def completed(finished: asyncio.Task[None]) -> None:
            session.download_tasks.discard(finished)
            if finished.cancelled():
                session.download_error = "下载任务被取消"
                return
            error = finished.exception()
            if error is not None:
                session.download_error = str(error)[:300]

        task.add_done_callback(completed)

    async def _save_download(
        self,
        session: _BrowserSession,
        download: Download,
    ) -> None:
        downloads_dir = (
            self._artifacts_root / "jobs" / session.request.job_id / "downloads"
        )
        downloads_dir.mkdir(parents=True, exist_ok=True)
        filename = Path(download.suggested_filename).name
        if not filename or filename in {".", ".."}:
            filename = "download.bin"
        destination = downloads_dir / filename
        counter = 1
        while destination.exists():
            destination = downloads_dir / (
                f"{Path(filename).stem}-{counter}{Path(filename).suffix}"
            )
            counter += 1
        await download.save_as(destination)
        session.download_paths.append(str(destination.resolve()))

    @staticmethod
    async def _wait_for_downloads(session: _BrowserSession) -> None:
        if session.download_tasks:
            await asyncio.gather(*tuple(session.download_tasks), return_exceptions=True)

    @staticmethod
    def _target_region_action(
        target_intent: str,
        observed: ObservedVisualPage,
    ) -> ActionProposal | None:
        if not observed.modal_open:
            return None
        normalized_target = _normalize(target_intent)
        candidates: list[BrowserElement] = []
        region_suffixes = ("特别行政区", "自治州", "自治区", "新区", "市", "区", "县", "州")
        for element in observed.observation.elements:
            name = " ".join(element.accessible_name.split())
            normalized_name = _normalize(name)
            if (
                element.enabled
                and element.role in {"button", "link"}
                and element.tag in {"button", "a"}
                and 2 <= len(normalized_name) <= 20
                and normalized_name.endswith(region_suffixes)
                and normalized_name in normalized_target
            ):
                candidates.append(element)
        if not candidates:
            return None
        target = max(candidates, key=lambda item: len(_normalize(item.accessible_name)))
        return ActionProposal(
            action=ActionType.CLICK,
            element_id=target.element_id,
            accessible_name=target.accessible_name,
            confidence=1,
            evidence="地区弹窗包含任务明确指定的地区, 按目标地区路径继续",
        )

    @staticmethod
    def _action_matches_target_context(
        target_intent: str,
        observed: ObservedVisualPage,
        action: ActionProposal,
    ) -> bool:
        if action.action not in {ActionType.CLICK, ActionType.SUBMIT}:
            return True
        element = next(
            (
                item
                for item in observed.observation.elements
                if item.element_id == action.element_id
            ),
            None,
        )
        if element is None:
            return True
        label = _normalize(element.accessible_name)
        guarded_labels = {
            _normalize(value)
            for value in ("办事指南", "在线办理", "查看详情", "查看", "详情")
        }
        target = _normalize(target_intent)
        repeated = sum(
            _normalize(item.accessible_name) == label
            for item in observed.observation.elements
        )
        if label in guarded_labels and repeated > 1:
            context = _normalize(element.context)
            milestones = [
                _normalize(item)
                for item in PlaywrightBrowserWorker._visual_goal_milestones(
                    target_intent
                )
                if len(_normalize(item)) >= 4
                and _normalize(item) not in guarded_labels
            ]
            if milestones:
                most_specific = max(milestones, key=len)
                return most_specific in context
            return any(
                target[index : index + 4] in context
                for index in range(max(0, len(target) - 3))
            )
        if label and label in target:
            return True
        safe_navigation = {
            _normalize(value)
            for value in (
                "登录",
                "注册",
                "确认",
                "确定",
                "同意",
                "关闭",
                "下一页",
                "上一页",
                "搜索",
                "查询",
                "更多",
                "展开",
            )
        }
        if label in safe_navigation:
            return True
        if label in guarded_labels:
            context = _normalize(element.context)
            return any(
                len(_normalize(milestone)) >= 4
                and _normalize(milestone) in context
                for milestone in PlaywrightBrowserWorker._visual_goal_milestones(
                    target_intent
                )
            )
        actionable = [
            item
            for item in observed.observation.elements
            if item.enabled
            and item.role in {"button", "link"}
            and item.tag in {"button", "a"}
        ]
        return len(actionable) == 1

    @staticmethod
    def _prefer_progressing_target_action(
        target_intent: str,
        observed: ObservedVisualPage,
        action: ActionProposal,
        *,
        last_action_label: str | None,
    ) -> ActionProposal:
        """Replace a repeated category click with the requested target-row action."""

        if (
            action.action not in {ActionType.CLICK, ActionType.SUBMIT}
            or not last_action_label
        ):
            return action
        elements = {
            element.element_id: element for element in observed.observation.elements
        }
        proposed_element = elements.get(str(action.element_id))
        proposed_label = _normalize(
            action.accessible_name
            or (proposed_element.accessible_name if proposed_element is not None else "")
        )
        if not proposed_label or proposed_label != _normalize(last_action_label):
            return action

        milestones = PlaywrightBrowserWorker._visual_goal_milestones(target_intent)
        guarded_labels = {
            _normalize(value)
            for value in ("办事指南", "在线办理", "查看详情", "查看", "详情")
        }
        requested_row_actions = {
            _normalize(milestone)
            for milestone in milestones
            if _normalize(milestone) in guarded_labels
        }
        business_milestones = [
            milestone
            for milestone in milestones
            if len(_normalize(milestone)) >= 4
            and _normalize(milestone) not in guarded_labels
            and not re.search(
                r"(?:特别行政区|自治州|自治区|新区|市|区|县|州)$",
                milestone,
            )
            and "下载" not in milestone
        ]
        if not requested_row_actions or not business_milestones:
            return action
        most_specific = max(business_milestones, key=lambda value: len(_normalize(value)))
        target_context = _normalize(most_specific)
        candidates = [
            element
            for element in observed.observation.elements
            if element.enabled
            and element.role in {"button", "link"}
            and element.tag in {"button", "a"}
            and _normalize(element.accessible_name) in requested_row_actions
            and target_context in _normalize(element.context)
        ]
        if len(candidates) != 1:
            return action
        candidate = candidates[0]
        return ActionProposal(
            action=ActionType.CLICK,
            element_id=candidate.element_id,
            accessible_name=candidate.accessible_name,
            confidence=1,
            evidence=(
                f"已完成“{last_action_label}”, 目标事项“{most_specific}”已在当前列表可见; "
                f"按同一事项上下文执行“{candidate.accessible_name}”"
            ),
        )

    @staticmethod
    def _visual_goal_milestones(target_intent: str) -> list[str]:
        clauses = [
            clause.strip()
            for clause in re.split(r"[,\uFF0C;\uFF1B\u3002]+", target_intent)
            if clause.strip()
        ]
        milestones: list[str] = []

        def cleaned(value: str) -> str:
            value = re.sub(
                r"^(?:(?:找到|点击|进入|打开|前往|在新页面|然后|随后|再))+",
                "",
                value.strip(),
            )
            value = re.sub(r"(?:板块|菜单|入口|按钮)$", "", value.strip())
            return value.strip()

        for clause in clauses:
            if "地区选择" in clause:
                region_text = clause.split("地区选择", 1)[1]
                regions = re.findall(
                    r"[\u4e00-\u9fff]{1,8}?(?:特别行政区|自治州|自治区|新区|市|区|县|州)",
                    region_text,
                )
                milestones.extend(regions)
                continue
            for part in re.split(r"的|并|->|→", clause):
                milestone = cleaned(part)
                if milestone:
                    milestones.append(milestone)
        return milestones

    @staticmethod
    def _last_action_completes_visual_goal(
        target_intent: str,
        last_action_label: str | None,
    ) -> bool:
        if not last_action_label:
            return False
        milestones = PlaywrightBrowserWorker._visual_goal_milestones(target_intent)
        if not milestones:
            return False

        def action_text(value: str) -> str:
            return _normalize(
                re.sub(r"(?:板块|菜单|入口|按钮)$", "", value.strip())
            )

        action = action_text(last_action_label)
        final_milestone = action_text(milestones[-1])
        if not action or not final_milestone:
            return False
        if action == final_milestone:
            return True
        terminal_actions = (
            "下载",
            "提交",
            "保存",
            "查询",
            "申请",
            "办事指南",
            "在线办理",
        )
        return any(term in action and term in final_milestone for term in terminal_actions)

    @staticmethod
    def _region_confirmation_action(
        observed: ObservedVisualPage,
    ) -> ActionProposal | None:
        by_name = {
            " ".join(element.accessible_name.split()): element
            for element in observed.observation.elements
            if element.enabled
        }
        confirm = next(
            (
                by_name[name]
                for name in ("确认", "确定")
                if name in by_name
                and by_name[name].role in {"button", "link"}
                and by_name[name].tag in {"button", "a"}
            ),
            None,
        )
        has_region_alternative = any(
            any(term in name for term in ("重新选择", "切换地区", "选择地区"))
            for name in by_name
        )
        if confirm is None or not has_region_alternative:
            return None
        return ActionProposal(
            action=ActionType.CLICK,
            element_id=confirm.element_id,
            accessible_name=confirm.accessible_name,
            confidence=1,
            evidence="截图显示地区确认弹窗, 按默认地区继续",
        )

    @staticmethod
    def _entry_goal_completed_at_login(
        session: _BrowserSession,
        observed: ObservedVisualPage,
    ) -> bool:
        if session.visual_values or not session.last_visual_action_label:
            return False
        elements = observed.observation.elements
        normalized_url = observed.observation.url.casefold()
        is_login_wall = (
            any(element.input_type == "password" for element in elements)
            or any(term in normalized_url for term in ("/login", "/signin", "/cas/", "/auth/"))
        )
        if not is_login_wall:
            return False

        def semantic_text(value: str) -> str:
            without_scope = re.sub(r"[\[(].*?[\])]", "", value)
            return _normalize(without_scope).replace("社会保障卡", "社保卡")

        action_label = semantic_text(session.last_visual_action_label)
        target_intent = semantic_text(session.request.target_intent)
        return len(action_label) >= 4 and action_label in target_intent

    async def _wait_for_visual_cadence(self, session: _BrowserSession) -> None:
        previous = session.visual_last_observation_at
        if previous is None:
            return
        interval = session.request.observation_interval_seconds
        remaining = interval - (asyncio.get_running_loop().time() - previous)
        if remaining > 0:
            await session.page.wait_for_timeout(round(remaining * 1_000))

    @staticmethod
    def _decision_requires_human(interruptions: list[str]) -> bool:
        text = " ".join(interruptions).casefold()
        for negation in (
            "无需人工",
            "不需人工",
            "不需要人工",
            "no human intervention required",
            "no human action required",
        ):
            text = text.replace(negation, "")
        return any(
            term in text
            for term in (
                "captcha",
                "验证码",
                "mfa",
                "two-factor",
                "人工",
                "human intervention required",
            )
        )

    @staticmethod
    def _visual_review_result(
        current_url: str,
        completed_fields: int,
        screenshot_path: str | None,
        reason: str,
    ) -> BrowserRunResult:
        return BrowserRunResult(
            status=BrowserJobStatus.NEED_HUMAN,
            message=reason[:500],
            current_url=current_url,
            completed_fields=completed_fields,
            screenshot_path=screenshot_path,
            intervention=HumanIntervention(
                kind=InterventionKind.VISUAL_REVIEW,
                instruction="请检查当前页面状态, 处理后继续视觉识别",
                requires_browser_interaction=True,
            ),
        )

    def _visual_task_prompt(self, session: _BrowserSession) -> str:
        request = session.request
        profile_items: list[str] = []
        for key in session.visual_values:
            definition = session.visual_definitions.get(key)
            profile_items.append(f"{key}({definition.display_name if definition else key})")
        profile = (", ".join(profile_items) or "none")[:280]
        history = (
            " | ".join(reversed(session.visual_recent_actions[-8:])) or "none"
        )[:320]
        last_click = session.last_visual_action_label or "none"
        milestones = " -> ".join(
            self._visual_goal_milestones(request.target_intent)
        )[:260]
        prompt = (
            f"Business goal: {request.target_intent[:260]}. Auth mode: "
            f"{request.authentication_mode.value}. Ordered milestones: {milestones or 'none'}. "
            f"Last successful click (already completed): {last_click[:80]}. "
            f"Recent actions (newest first): {history}. "
            "Never return the last successful click again. If the requested business item is "
            "visible in a list, advance with its requested row-level action such as 办事指南; "
            "the business-item text itself may be non-clickable. "
            "Use the screenshot to navigate visible menus and use only current grounded IDs. "
            "Each element context identifies its surrounding service row. When action labels "
            "repeat, choose only the action whose context contains the requested business item. "
            "While a region modal is open, resolve only the requested region path and do not "
            "click dimmed background content. "
            "Treat a merely shared word as insufficient evidence: an action naming a business "
            "item must exactly match a milestone, while repeated generic actions must match the "
            "most specific business item in their context. Report confidence below 0.85 when "
            "uncertain; actions at or above 0.85 are executed automatically after guardrails. "
            "Match semantic equivalents and abbreviations such as 社会保障卡/社保卡. For an "
            "entry-only goal with no customer fields, if the latest matching target click led to "
            "a login page, the entry is found: set target_reached=true and finish; do not click "
            "unrelated login-page links or request credentials. In manual auth mode never type "
            "credentials; request human help if a login wall reappears. On the visible target "
            "form, report required data absent from the customer schema; never infer future "
            f"fields. Available customer fields: {profile}. Return mappings or one action, never "
            "literal values."
        )
        return prompt[:1_000]

    async def _handle_submission(
        self,
        session: _BrowserSession,
        resolution: HumanResolution,
        report: Callable[[JobProgress], Awaitable[None]],
        screenshot_path: str | None,
    ) -> BrowserRunResult:
        request = session.request
        page = session.page
        policy = request.submission.policy
        if policy is SubmissionPolicy.FILL_ONLY:
            return BrowserRunResult(
                status=BrowserJobStatus.COMPLETED,
                message="填写和回读验证完成; 当前策略为仅填写",
                current_url=page.url,
                completed_fields=session.completed_fields,
                screenshot_path=screenshot_path,
                entry_action_performed=session.entry_action_performed,
            )

        if resolution.approve_submission:
            actions = session.submit_actions
            element_id = resolution.submit_element_id
            if actions is None or element_id is None:
                raise ValueError("Submission observation is unavailable")
            locator = actions.locators.get(element_id)
            if locator is None:
                raise ValueError("Submission candidate is no longer available")
            return await self._click_submission(
                session,
                locator,
                element_id,
                report,
            )

        actions = await self._observer.observe_submission_actions(
            page,
            request.job_id,
            request.submission.button_aliases,
        )
        session.submit_actions = actions
        matched = [candidate for candidate in actions.candidates if candidate.confidence > 0]
        selected = self._unique_submission_candidate(matched)
        if policy is SubmissionPolicy.AUTO_SUBMIT and selected is not None:
            return await self._click_submission(
                session,
                actions.locators[selected.element_id],
                selected.element_id,
                report,
            )

        candidates = matched or actions.candidates
        screenshot_path = await self._capture(page, request.job_id)
        if not candidates:
            instruction = "未发现可操作的提交按钮, 请检查页面状态后终止任务"
        elif policy is SubmissionPolicy.CONFIRM_BEFORE_SUBMIT:
            instruction = "请选择提交按钮并明确确认, 系统随后只点击一次"
        else:
            instruction = "提交按钮无法唯一识别, 请选择正确按钮并明确确认"
        return BrowserRunResult(
            status=BrowserJobStatus.NEED_HUMAN,
            message="填写已完成, 正在等待提交确认",
            current_url=page.url,
            completed_fields=session.completed_fields,
            screenshot_path=screenshot_path,
            intervention=HumanIntervention(
                kind=InterventionKind.SUBMISSION_CONFIRMATION,
                instruction=instruction,
                submission_candidates=candidates,
                requires_browser_interaction=not candidates,
            ),
        )

    @staticmethod
    def _unique_submission_candidate(
        candidates: list[InterventionCandidate],
    ) -> InterventionCandidate | None:
        return PlaywrightBrowserWorker._unique_action_candidate(candidates)

    @staticmethod
    def _unique_action_candidate(
        candidates: list[InterventionCandidate],
    ) -> InterventionCandidate | None:
        if not candidates or candidates[0].confidence < 0.9:
            return None
        if len(candidates) == 1:
            return candidates[0]
        if candidates[0].confidence - candidates[1].confidence >= 0.04:
            return candidates[0]
        return None

    def _should_auto_enter(
        self,
        request: BrowserRunRequest,
        observed: ObservedPage,
    ) -> bool:
        matches = self._mapper.map_fields(observed.elements, request.field_definitions)
        requested = set(request.fields)
        if any(match.canonical_field in requested for match in matches):
            return False
        for definition in request.field_definitions:
            if definition.key not in requested:
                continue
            ranked = self._mapper.ranked_candidates(
                observed.elements,
                definition,
                limit=1,
            )
            if ranked and ranked[0][0] >= 0.85:
                return False
        return True

    @staticmethod
    def _auto_entry_aliases(request: BrowserRunRequest) -> list[str]:
        fields = set(request.fields)
        registration = (
            "account.passwordConfirmation" in fields
            or "account.email" in fields
            or (
                "account.password" in fields
                and "person.fullName" in fields
                and "account.username" not in fields
            )
        )
        if registration:
            return [
                "Create an account",
                "Create account",
                "Register",
                "Sign up",
                "注册",
                "创建账户",
                "创建账号",
            ]
        if "account.username" in fields or "account.password" in fields:
            return ["Login", "Log in", "Sign in", "登录", "登陆"]
        return []

    async def _click_entry_action(
        self,
        session: _BrowserSession,
        element_id: str | None,
        *,
        report: Callable[[JobProgress], Awaitable[None]] | None,
    ) -> None:
        actions = session.entry_actions
        if actions is None or element_id is None:
            raise ValueError("Entry action observation is unavailable")
        locator = actions.locators.get(element_id)
        candidate = next(
            (
                item
                for item in actions.candidates
                if item.element_id == element_id
            ),
            None,
        )
        if locator is None or candidate is None:
            raise ValueError("Entry action candidate is no longer available")
        if await locator.count() != 1 or not await locator.is_visible():
            raise ValueError("Entry action candidate changed after observation")
        if not await locator.is_enabled():
            raise ValueError("Entry action candidate is disabled")
        current_name = await self._action_accessible_name(locator)
        if _normalize(current_name) != _normalize(candidate.accessible_name):
            raise ValueError("Entry action meaning changed after observation")
        if report is not None:
            await report(
                JobProgress(
                    status=BrowserJobStatus.ENTERING,
                    message=f"正在点击表单入口 {candidate.accessible_name}",
                    current_url=session.page.url,
                    completed_fields=session.completed_fields,
                )
            )

        previous_pages = set(session.context.pages)
        previous_url = session.page.url
        await locator.click()
        await asyncio.sleep(0)
        opened_pages = [
            page for page in session.context.pages if page not in previous_pages
        ]
        if opened_pages:
            session.page = opened_pages[-1]
        await self._wait_for_requested_form(session, previous_url)
        self._require_allowed_url(
            session.page.url,
            session_origins=session.trusted_navigation_origins,
        )
        session.entry_action_performed = True
        session.observed = None

    async def _wait_for_requested_form(
        self,
        session: _BrowserSession,
        previous_url: str,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + self._action_timeout_ms / 1_000
        while asyncio.get_running_loop().time() < deadline:
            if session.page.url != previous_url:
                with suppress(PlaywrightTimeoutError):
                    await session.page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=self._action_timeout_ms,
                    )
                return
            try:
                observed = await self._observer.observe(
                    session.page,
                    session.request.job_id,
                )
                if self._requested_fields_are_present(
                    observed,
                    session.request.fields,
                    session.request.field_definitions,
                ):
                    return
            except PlaywrightError:
                pass
            await asyncio.sleep(0.1)
        raise TimeoutError("点击表单入口后, 目标表单未在超时时间内出现")

    @staticmethod
    async def _action_accessible_name(locator: Locator) -> str:
        value = await locator.evaluate(
            """
            element => (
              element.getAttribute('aria-label')
              || element.innerText
              || element.value
              || element.getAttribute('title')
              || element.getAttribute('name')
              || ''
            ).trim()
            """
        )
        return str(value)

    async def _click_submission(
        self,
        session: _BrowserSession,
        locator: Locator,
        element_id: str,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        if session.submission_attempted:
            raise ValueError("Submission was already attempted and will not be retried")
        if await locator.count() != 1 or not await locator.is_visible():
            raise ValueError("Submission candidate changed after observation")
        if not await locator.is_enabled():
            raise ValueError("Submission candidate is disabled")
        actions = session.submit_actions
        observed_candidate = next(
            (
                candidate
                for candidate in actions.candidates
                if candidate.element_id == element_id
            ),
            None,
        ) if actions is not None else None
        if observed_candidate is None:
            raise ValueError("Submission candidate is outside the current observation")
        current_name = await locator.evaluate(
            """
            element => (
              element.getAttribute('aria-label')
              || element.innerText
              || element.value
              || element.getAttribute('title')
              || element.getAttribute('name')
              || ''
            ).trim()
            """
        )
        if _normalize(str(current_name)) != _normalize(observed_candidate.accessible_name):
            raise ValueError("Submission candidate meaning changed after observation")

        session.submission_attempted = True
        previous_url = session.page.url
        await report(
            JobProgress(
                status=BrowserJobStatus.SUBMITTING,
                message="正在执行一次提交点击",
                current_url=session.page.url,
                completed_fields=session.completed_fields,
            )
        )
        await locator.click()
        next_step = self._next_workflow_step(session)
        if next_step is None:
            with suppress(PlaywrightTimeoutError):
                await session.page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=min(self._action_timeout_ms, 2_000),
                )
        else:
            await self._wait_for_next_step(session, next_step, previous_url)
        self._require_allowed_url(
            session.page.url,
            session_origins=session.trusted_navigation_origins,
        )
        screenshot_path = await self._capture(session.page, session.request.job_id)
        return BrowserRunResult(
            status=BrowserJobStatus.COMPLETED,
            message=f"填写验证完成, 已点击提交按钮 {element_id}",
            current_url=session.page.url,
            completed_fields=session.completed_fields,
            screenshot_path=screenshot_path,
            submitted=True,
        )

    @staticmethod
    def _next_workflow_step(session: _BrowserSession) -> WorkflowStep | None:
        steps = session.workflow_steps
        if steps is None or session.step_index >= len(steps) - 1:
            return None
        return steps[session.step_index + 1]

    async def _wait_for_next_step(
        self,
        session: _BrowserSession,
        next_step: WorkflowStep,
        previous_url: str,
    ) -> None:
        transition_timeout_ms = max(self._action_timeout_ms, 5_000)
        deadline = asyncio.get_running_loop().time() + transition_timeout_ms / 1_000
        navigated_url: str | None = None
        while asyncio.get_running_loop().time() < deadline:
            page = session.page
            if page.url != previous_url and page.url != navigated_url:
                with suppress(PlaywrightTimeoutError):
                    await page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=self._action_timeout_ms,
                    )
                self._require_allowed_url(
                    page.url,
                    session_origins=session.trusted_navigation_origins,
                )
                navigated_url = page.url
            try:
                observed = await self._observer.observe(page, session.request.job_id)
                if self._requested_form_is_present(
                    observed,
                    next_step.fields,
                    next_step.field_definitions,
                ):
                    return
                if next_step.entry_action.mode is not EntryActionMode.DIRECT:
                    aliases = (
                        next_step.entry_action.aliases
                        if next_step.entry_action.mode is EntryActionMode.CLICK
                        else self._auto_entry_aliases(
                            session.request.model_copy(
                                update={
                                    "fields": next_step.fields,
                                    "field_definitions": next_step.field_definitions,
                                }
                            )
                        )
                    )
                    actions = await self._observer.observe_entry_actions(
                        page,
                        session.request.job_id,
                        aliases,
                    )
                    if any(candidate.confidence > 0 for candidate in actions.candidates):
                        return
                if (
                    next_step.entry_action.mode is not EntryActionMode.CLICK
                    and self._requested_fields_are_present(
                        observed,
                        next_step.fields,
                        next_step.field_definitions,
                    )
                ):
                    return
            except PlaywrightError:
                pass
            await asyncio.sleep(0.1)
        raise TimeoutError(
            f"提交后未在超时时间内出现下一步“{next_step.name}”的入口或字段"
        )

    def _requested_fields_are_present(
        self,
        observed: ObservedPage,
        fields: dict[str, str],
        definitions: list[FieldDefinition],
    ) -> bool:
        requested = set(fields)
        matches = self._mapper.map_fields(observed.elements, definitions)
        return any(match.canonical_field in requested for match in matches)

    def _requested_form_is_present(
        self,
        observed: ObservedPage,
        fields: dict[str, str],
        definitions: list[FieldDefinition],
    ) -> bool:
        requested = set(fields)
        if not requested:
            return False
        matches = self._mapper.map_fields(observed.elements, definitions)
        matched = {match.canonical_field for match in matches}
        return requested.issubset(matched)

    async def _route_allowed_requests(
        self,
        route: Route,
        request: Request,
        *,
        job_id: str | None = None,
        allow_new_https_origin: bool = False,
        session_origins: set[str] | None = None,
    ) -> None:
        parsed = urlsplit(request.url)
        if parsed.scheme in {"data", "blob"}:
            await route.continue_()
            return
        allowed_origins = self._current_allowed_origins() | (session_origins or set())
        try:
            request_origin = normalize_origin(request.url)
            allowed = request_origin in allowed_origins
        except ValueError:
            request_origin = ""
            allowed = False
        if allowed:
            await route.continue_()
            return
        if (
            allow_new_https_origin
            and session_origins is not None
            and self._is_approved_www_site_family_origin(
                request_origin,
                allowed_origins,
            )
        ):
            session_origins.add(request_origin)
            logger.info(
                "Allowed session-scoped first-party HTTPS resource during manual "
                "login: job_id=%s target_origin=%s",
                job_id or "unknown",
                request_origin,
            )
            await route.continue_()
            return
        if (
            allow_new_https_origin
            and session_origins is not None
            and parsed.scheme.lower() == "https"
            and self._is_safe_top_level_navigation(request)
        ):
            session_origins.add(request_origin)
            logger.info(
                "Allowed session-scoped HTTPS navigation during manual login: "
                "job_id=%s target_origin=%s",
                job_id or "unknown",
                request_origin,
            )
            await route.continue_()
            return
        upgraded_url = self._safe_https_navigation_upgrade(
            request,
            allowed_origins=allowed_origins,
            allow_new_https_origin=allow_new_https_origin,
        )
        if upgraded_url is not None:
            upgraded_origin = normalize_origin(upgraded_url)
            if session_origins is not None:
                session_origins.add(upgraded_origin)
            logger.info(
                "Upgraded insecure top-level navigation to HTTPS: "
                "job_id=%s target_origin=%s",
                job_id or "unknown",
                upgraded_origin,
            )
            await route.fulfill(
                status=307,
                headers={
                    "cache-control": "no-store",
                    "location": upgraded_url,
                },
                body="",
            )
            return
        await route.abort("blockedbyclient")

    @staticmethod
    def _uses_manual_authentication(request: BrowserRunRequest) -> bool:
        return (
            request.authentication_mode is AuthenticationMode.MANUAL
            or any(
                step.authentication_mode is AuthenticationMode.MANUAL
                for step in request.workflow_steps
            )
        )

    @staticmethod
    def _is_safe_top_level_navigation(request: Request) -> bool:
        parsed = urlsplit(request.url)
        return (
            parsed.scheme.lower() in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and request.method.upper() in {"GET", "HEAD"}
            and request.resource_type == "document"
            and request.is_navigation_request()
            and request.frame.parent_frame is None
        )

    @staticmethod
    def _is_approved_www_site_family_origin(
        candidate_origin: str,
        allowed_origins: set[str],
    ) -> bool:
        candidate = urlsplit(candidate_origin)
        candidate_host = (candidate.hostname or "").lower().rstrip(".")
        if candidate.scheme.lower() != "https" or not candidate_host:
            return False
        for allowed_origin in allowed_origins:
            allowed = urlsplit(allowed_origin)
            allowed_host = (allowed.hostname or "").lower().rstrip(".")
            if allowed.scheme.lower() != "https" or not allowed_host.startswith("www."):
                continue
            site_family = allowed_host.removeprefix("www.")
            if site_family.count(".") < 2:
                continue
            if candidate_host == site_family or candidate_host.endswith(f".{site_family}"):
                return True
        return False

    def _safe_https_navigation_upgrade(
        self,
        request: Request,
        *,
        allowed_origins: set[str] | None = None,
        allow_new_https_origin: bool = False,
    ) -> str | None:
        parsed = urlsplit(request.url)
        if (
            parsed.scheme.lower() != "http"
            or not self._is_safe_top_level_navigation(request)
        ):
            return None
        upgraded_url = urlunsplit(parsed._replace(scheme="https"))
        try:
            upgraded_origin = normalize_origin(upgraded_url)
        except ValueError:
            return None
        effective_origins = allowed_origins or self._current_allowed_origins()
        if upgraded_origin not in effective_origins and not allow_new_https_origin:
            return None
        return upgraded_url

    def _require_allowed_url(
        self,
        value: str,
        *,
        session_origins: set[str] | None = None,
    ) -> None:
        try:
            origin = normalize_origin(value)
        except ValueError as error:
            raise ValueError("Browser target URL is invalid") from error
        allowed_origins = self._current_allowed_origins() | (session_origins or set())
        if origin not in allowed_origins:
            raise ValueError("Browser target origin is not approved")
        if not value.startswith("https://") and not self._is_loopback(value):
            raise ValueError("Browser target must use HTTPS outside loopback development")

    def _current_allowed_origins(self) -> set[str]:
        if self._allowed_origins_provider is None:
            return self._allowed_origins
        return {
            normalize_origin(origin)
            for origin in self._allowed_origins_provider()
        }

    @staticmethod
    def _safe_display_url(value: str) -> str:
        parsed = urlsplit(value)
        return parsed._replace(query="", fragment="").geturl()

    @staticmethod
    def _same_page_url(left: str, right: str) -> bool:
        if left == "about:blank" or right == "about:blank":
            return False
        left_url = urlsplit(left)
        right_url = urlsplit(right)
        return (
            left_url.scheme.casefold(),
            left_url.hostname.casefold() if left_url.hostname else "",
            left_url.port,
            left_url.path or "/",
            left_url.query,
        ) == (
            right_url.scheme.casefold(),
            right_url.hostname.casefold() if right_url.hostname else "",
            right_url.port,
            right_url.path or "/",
            right_url.query,
        )

    @staticmethod
    def _is_loopback(value: str) -> bool:
        hostname = urlsplit(value).hostname
        return hostname in {"127.0.0.1", "localhost", "::1"}

    @staticmethod
    async def _requires_human(page: Page) -> bool:
        for frame in page.frames:
            indicators = frame.get_by_text(
                re.compile(
                    r"验证码|captcha|人机验证|双重验证|two.?factor|mfa",
                    re.IGNORECASE,
                )
            )
            for index in range(await indicators.count()):
                if await indicators.nth(index).is_visible():
                    return True
        return False

    @staticmethod
    async def _dismiss_safe_popup(page: Page) -> bool:
        for frame in page.frames:
            try:
                candidates = frame.get_by_role(
                    "button",
                    name=re.compile(
                        r"^(关闭|稍后|拒绝|仅必要|不再提示|\u00d7|close|not now)$",
                        re.IGNORECASE,
                    ),
                )
                for index in range(await candidates.count()):
                    candidate = candidates.nth(index)
                    if await candidate.is_visible():
                        await candidate.click()
                        return True
            except PlaywrightError:
                continue
        return False

    async def _fill(
        self,
        locator: Locator,
        element: DomElement,
        definition: FieldDefinition,
        value: str,
    ) -> str:
        if element.tag == "select":
            option_value = self._select_value(element, definition.key, value)
            await locator.select_option(value=option_value)
            return option_value
        await locator.fill(value)
        return value

    @staticmethod
    def _select_value(element: DomElement, canonical: str, value: str) -> str:
        aliases = {value.casefold()}
        if canonical == "person.gender":
            gender_aliases = {
                "male": {"male", "m", "男", "1"},
                "female": {"female", "f", "女", "2"},
                "other": {"other", "其他", "0"},
            }
            aliases = gender_aliases.get(value.casefold(), aliases)
        for option in element.options:
            if (
                option["value"].casefold() in aliases
                or option["label"].strip().casefold() in aliases
            ):
                return option["value"]
        raise ValueError(f"No matching option for canonical field {canonical}")

    def _resolve_value(self, value: str) -> str:
        if value.startswith("secret://"):
            return self._secret_store.resolve(value)
        return value

    async def _capture(self, page: Page, job_id: str) -> str:
        job_directory = self._artifacts_root / "jobs" / job_id
        job_directory.mkdir(parents=True, exist_ok=True)
        path = job_directory / "latest.png"
        await page.screenshot(
            path=path,
            full_page=True,
            timeout=self._screenshot_timeout_ms,
        )
        session = self._sessions.get(job_id)
        if session is not None:
            session.screenshot_count += 1
        return str(path)
