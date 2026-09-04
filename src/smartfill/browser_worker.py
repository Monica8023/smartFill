"""DOM semantic mapping and the Playwright worker plugin boundary."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.async_api import (
    Browser,
    BrowserContext,
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
    BrowserJobStatus,
    BrowserRunRequest,
    BrowserRunResult,
    EntryActionMode,
    FieldCandidateSet,
    HumanIntervention,
    HumanResolution,
    InterventionCandidate,
    InterventionKind,
    JobProgress,
    PageScanRequest,
    PageScanResult,
    SubmissionPolicy,
    WorkflowStep,
)
from smartfill.config import normalize_origin
from smartfill.field_schema import (
    FieldDefinition,
    FieldInputKind,
    default_field_definition,
    discoverable_field_definitions,
)
from smartfill.secrets import SecretStore


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

    def __init__(
        self,
        *,
        secret_store: SecretStore,
        allowed_origins: set[str],
        allowed_origins_provider: Callable[[], set[str]] | None = None,
        artifacts_root: Path,
        headless: bool = True,
        cdp_url: str | None = None,
        navigation_timeout_ms: int = 30_000,
        action_timeout_ms: int = 10_000,
    ) -> None:
        self._secret_store = secret_store
        self._allowed_origins = {normalize_origin(origin) for origin in allowed_origins}
        self._allowed_origins_provider = allowed_origins_provider
        self._artifacts_root = artifacts_root
        self._headless = headless
        self._cdp_url = cdp_url
        self._navigation_timeout_ms = navigation_timeout_ms
        self._action_timeout_ms = action_timeout_ms
        self._mapper = SemanticFieldMapper()
        self._observer = PlaywrightPageObserver()
        self._sessions: dict[str, _BrowserSession] = {}

    async def scan_page(self, request: PageScanRequest) -> PageScanResult:
        """Open a target page, optionally enter a form, and return its field schema."""

        self._require_allowed_url(request.target_url)
        scan_request = BrowserRunRequest(
            job_id=f"scan-{uuid4()}",
            task_id="page-scan",
            target_url=request.target_url,
            fields={"account.username": "page-scan-placeholder"},
            entry_action=request.entry_action,
        )
        session = await self._open_session(scan_request)
        try:
            await session.page.goto(
                request.target_url,
                wait_until="domcontentloaded",
                timeout=self._navigation_timeout_ms,
            )
            self._require_allowed_url(session.page.url)
            await self._dismiss_safe_popup(session.page)
            initial_observation: ObservedPage | None = None
            should_enter = request.entry_action.mode is EntryActionMode.CLICK
            if request.entry_action.mode is EntryActionMode.AUTO:
                initial_observation = await self._observer.observe(
                    session.page,
                    scan_request.job_id,
                )
                should_enter = self._should_auto_enter(scan_request, initial_observation)
            if should_enter:
                aliases = (
                    request.entry_action.aliases
                    if request.entry_action.mode is EntryActionMode.CLICK
                    else self._auto_entry_aliases(scan_request)
                )
                actions = await self._observer.observe_entry_actions(
                    session.page,
                    scan_request.job_id,
                    aliases,
                )
                matched = [
                    candidate
                    for candidate in actions.candidates
                    if candidate.confidence > 0
                ]
                selected = self._unique_action_candidate(matched)
                if selected is None:
                    raise ValueError(
                        "Form entry could not be uniquely identified; refine its aliases"
                    )
                session.entry_actions = actions
                await self._click_entry_action(
                    session,
                    selected.element_id,
                    report=None,
                )
            await self._dismiss_safe_popup(session.page)
            observed = initial_observation
            if observed is None or session.entry_action_performed:
                observed = await self._observer.observe(
                    session.page,
                    scan_request.job_id,
                )
            return PageScanResult(
                initial_url=self._safe_display_url(request.target_url),
                final_url=self._safe_display_url(session.page.url),
                entry_action_performed=session.entry_action_performed,
                fields=self._discover_field_schema(observed.elements),
            )
        finally:
            await session.context.close()
            if session.owns_browser:
                await session.browser.close()
            await session.playwright.stop()

    async def run(
        self,
        request: BrowserRunRequest,
        report: Callable[[JobProgress], Awaitable[None]],
    ) -> BrowserRunResult:
        self._require_allowed_url(request.target_url)
        for step in request.workflow_steps:
            self._require_allowed_url(step.target_url)
        if request.job_id in self._sessions:
            raise ValueError("Browser job already has an active session")
        session = await self._open_session(request)
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
        if (
            result.status is BrowserJobStatus.COMPLETED
            and session.request.keep_browser_open
        ):
            return result.model_copy(
                update={
                    "message": f"{result.message}; 目标浏览器保持打开, 可检查后手动关闭",
                    "browser_session_open": True,
                }
            )
        if result.status is not BrowserJobStatus.NEED_HUMAN:
            await self.cancel(job_id)
        return result

    async def cancel(self, job_id: str) -> None:
        session = self._sessions.pop(job_id, None)
        if session is None:
            return
        try:
            await session.context.close()
        finally:
            try:
                if session.owns_browser:
                    await session.browser.close()
            finally:
                await session.playwright.stop()

    async def _open_session(self, request: BrowserRunRequest) -> _BrowserSession:
        playwright = await async_playwright().start()
        try:
            if self._cdp_url:
                browser = await playwright.chromium.connect_over_cdp(
                    self._cdp_url,
                    timeout=self._navigation_timeout_ms,
                    is_local=self._is_loopback(self._cdp_url),
                )
                owns_browser = False
            else:
                browser = await playwright.chromium.launch(headless=self._headless)
                owns_browser = True

            context = await browser.new_context(
                accept_downloads=False,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
            )
            context.set_default_timeout(self._action_timeout_ms)
            await context.route("**/*", self._route_allowed_requests)
            page = await context.new_page()
            page.on("download", lambda download: asyncio.create_task(download.cancel()))
            return _BrowserSession(
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
                request=request,
                owns_browser=owns_browser,
                workflow_steps=request.workflow_steps or None,
            )
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
            self._require_allowed_url(page.url)
            session.navigated = True

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
            manual_targets: dict[str, tuple[FieldMatch, DomElement, Locator]] = {}
            previous = session.observed
            if previous is not None:
                previous_elements = {
                    element.element_id: element for element in previous.elements
                }
                for canonical, element_id in resolution.field_mappings.items():
                    locator = previous.locators.get(element_id)
                    element = previous_elements.get(element_id)
                    if (
                        locator is not None
                        and element is not None
                        and await locator.count() == 1
                        and await locator.is_visible()
                    ):
                        manual_targets[canonical] = (
                            FieldMatch(
                                canonical_field=canonical,
                                element_id=element_id,
                                confidence=1,
                                evidence="human confirmed current observation candidate",
                            ),
                            element,
                            locator,
                        )

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
            targets.update(manual_targets)

            missing_fields = set(request.fields) - targets.keys()
            if missing_fields:
                screenshot_path = await self._capture(page, request.job_id)
                return BrowserRunResult(
                    status=BrowserJobStatus.NEED_HUMAN,
                    message="部分字段无法唯一映射, 需要人工确认",
                    current_url=page.url,
                    completed_fields=completed_fields,
                    screenshot_path=screenshot_path,
                    intervention=HumanIntervention(
                        kind=InterventionKind.FIELD_MAPPING,
                        instruction="为每个未识别字段选择当前页面中的候选控件",
                        field_candidates=self._candidate_sets(
                            elements,
                            [definitions[key] for key in sorted(missing_fields)],
                        ),
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
                            instruction="确认当前字段后重新扫描和填写",
                            field_candidates=self._candidate_sets(
                                elements,
                                [definition],
                            ),
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
        self._require_allowed_url(session.page.url)
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

    def _discover_field_schema(
        self,
        elements: list[DomElement],
    ) -> list[FieldDefinition]:
        supported = [element for element in elements if self._is_discoverable(element)]
        known_matches: list[tuple[float, FieldDefinition, DomElement]] = []
        for definition in discoverable_field_definitions():
            ranked = self._mapper.ranked_candidates(supported, definition, limit=1)
            if ranked and ranked[0][0] >= 0.85:
                score, element = ranked[0]
                known_matches.append((score, definition, element))

        assigned: dict[str, FieldDefinition] = {}
        used_keys: set[str] = set()
        for _score, definition, element in sorted(
            known_matches,
            key=lambda item: item[0],
            reverse=True,
        ):
            if element.element_id in assigned or definition.key in used_keys:
                continue
            aliases = list(
                dict.fromkeys(
                    [
                        *definition.aliases,
                        *self._element_semantic_terms(element),
                    ]
                )
            )
            assigned[element.element_id] = definition.model_copy(
                update={"aliases": aliases[:30]}
            )
            used_keys.add(definition.key)

        discovered: list[FieldDefinition] = []
        custom_index = 1
        for element in supported:
            discovered_definition = assigned.get(element.element_id)
            if discovered_definition is None:
                terms = self._element_semantic_terms(element)
                display_name = terms[0] if terms else f"页面字段 {custom_index}"
                input_kind = self._input_kind(element)
                discovered_definition = FieldDefinition(
                    key=f"custom.field{custom_index}",
                    display_name=display_name,
                    aliases=terms or [display_name],
                    input_kind=input_kind,
                    sensitive=input_kind is FieldInputKind.PASSWORD,
                    autocomplete_hints=(
                        [element.autocomplete] if element.autocomplete else []
                    ),
                )
                custom_index += 1
            discovered.append(discovered_definition)
        return discovered[:30]

    @staticmethod
    def _element_semantic_terms(element: DomElement) -> list[str]:
        terms: list[str] = []
        seen: set[str] = set()
        for value in [
            element.label,
            element.aria_label,
            element.placeholder,
            element.name,
            element.accessible_name,
        ]:
            term = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
            term = " ".join(term.split())[:100].strip()
            folded = term.casefold()
            if term and folded not in seen:
                terms.append(term)
                seen.add(folded)
        return terms

    @staticmethod
    def _is_discoverable(element: DomElement) -> bool:
        if element.tag in {"textarea", "select"}:
            return True
        return element.tag == "input" and element.input_type.casefold() in {
            "",
            "text",
            "password",
            "email",
            "tel",
            "search",
            "url",
            "number",
            "date",
        }

    @staticmethod
    def _input_kind(element: DomElement) -> FieldInputKind:
        if element.tag == "select":
            return FieldInputKind.SELECT
        by_type = {
            "password": FieldInputKind.PASSWORD,
            "email": FieldInputKind.EMAIL,
            "tel": FieldInputKind.TEL,
        }
        return by_type.get(element.input_type.casefold(), FieldInputKind.TEXT)

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
        self._require_allowed_url(session.page.url)
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
        deadline = asyncio.get_running_loop().time() + self._action_timeout_ms / 1_000
        navigated_url: str | None = None
        while asyncio.get_running_loop().time() < deadline:
            page = session.page
            if page.url != previous_url and page.url != navigated_url:
                with suppress(PlaywrightTimeoutError):
                    await page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=self._action_timeout_ms,
                    )
                self._require_allowed_url(page.url)
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

    def _candidate_sets(
        self,
        elements: list[DomElement],
        definitions: list[FieldDefinition],
    ) -> list[FieldCandidateSet]:
        return [
            FieldCandidateSet(
                canonical_field=definition.key,
                candidates=[
                    InterventionCandidate(
                        element_id=element.element_id,
                        accessible_name=element.accessible_name,
                        role=element.role,
                        tag=element.tag,
                        frame_path=element.frame_path,
                        confidence=score,
                    )
                    for score, element in self._mapper.ranked_candidates(
                        elements,
                        definition,
                    )
                ],
            )
            for definition in definitions
        ]

    async def _route_allowed_requests(self, route: Route, request: Request) -> None:
        parsed = urlsplit(request.url)
        if parsed.scheme in {"data", "blob"}:
            await route.continue_()
            return
        try:
            allowed = normalize_origin(request.url) in self._current_allowed_origins()
        except ValueError:
            allowed = False
        if allowed:
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    def _require_allowed_url(self, value: str) -> None:
        try:
            origin = normalize_origin(value)
        except ValueError as error:
            raise ValueError("Browser target URL is invalid") from error
        if origin not in self._current_allowed_origins():
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
        await page.screenshot(path=path, full_page=True)
        return str(path)
