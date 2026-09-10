"""Screenshot-primary browser-agent proof of concept and evaluation runner."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import re
import secrets
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Locator, Page, Route, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, ConfigDict, Field

from smartfill.config import Settings, normalize_origin
from smartfill.execution import ActionPolicy, ActionProposal, ActionType
from smartfill.vision import (
    AliyunVisionProvider,
    BrowserElement,
    BrowserObservation,
    VisionDecision,
    VisionProvider,
    VisionRequest,
    VisionUsage,
)

_INTERACTIVE_SELECTOR = (
    "input, textarea, select, button, a, [role=button], [role=checkbox], "
    "[role=combobox], [contenteditable=true]"
)
_MAX_GROUNDED_ELEMENTS = 300
_AD_HOST_SUFFIXES = (
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
)

logger = logging.getLogger(__name__)


def _is_ad_url(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").casefold()
    return any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in _AD_HOST_SUFFIXES
    )


async def _route_without_ads(route: Route) -> None:
    if _is_ad_url(route.request.url):
        await route.abort()
    else:
        await route.continue_()


@dataclass(slots=True)
class ObservedVisualPage:
    observation: BrowserObservation
    locators: dict[str, Locator]
    screenshot_path: Path
    modal_open: bool = False


class VisualPocResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    trial: int = Field(ge=1)
    success: bool
    failure_category: str | None = None
    failure_message: str | None = None
    model_calls: int = Field(default=0, ge=0)
    browser_actions: int = Field(default=0, ge=0)
    guardrail_corrections: int = Field(default=0, ge=0)
    false_completions: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    final_url: str
    artifact_dir: str


class VisualPageObserver:
    """Create a full-page screenshot with fresh, document-grounded element IDs."""

    def __init__(self, *, screenshot_timeout_ms: int = 10_000) -> None:
        self._screenshot_timeout_ms = screenshot_timeout_ms

    async def observe(self, page: Page, screenshot_path: Path) -> ObservedVisualPage:
        raw_result = await page.locator(_INTERACTIVE_SELECTOR).evaluate_all(
            """elements => {
              document.querySelectorAll('[data-smartfill-vision-id]')
                .forEach(element => element.removeAttribute('data-smartfill-vision-id'));
              const rendered = element => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                  Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
              };
              const explicitModal = Array.from(document.querySelectorAll(
                'dialog[open], [role=dialog], [aria-modal=true], .modal.show, '
                + '.ant-modal, .el-dialog, .layui-layer, .ivu-modal'
              )).some(rendered);
              const coveringOverlay = Array.from(document.querySelectorAll('body *'))
                .some(candidate => {
                  if (!rendered(candidate)) return false;
                  const style = window.getComputedStyle(candidate);
                  const rect = candidate.getBoundingClientRect();
                  return style.position === 'fixed' && style.pointerEvents !== 'none' &&
                    rect.width >= window.innerWidth * 0.75 &&
                    rect.height >= window.innerHeight * 0.75;
                });
              const modalOpen = explicitModal || coveringOverlay;
              const actionContext = element => {
                const describesItem = candidate => {
                  const text = (candidate.innerText || '').replace(/\\s+/g, ' ').trim();
                  if (text.length < 4 || text.length > 500) return false;
                  let description = text;
                  const actionTexts = Array.from(candidate.querySelectorAll(
                    'button, a, [role=button]'
                  )).map(action => (action.innerText || action.textContent || '').trim())
                    .filter(Boolean).sort((left, right) => right.length - left.length);
                  for (const actionText of actionTexts) {
                    description = description.split(actionText).join(' ');
                  }
                  return description.replace(/\\s+/g, '').length >= 4;
                };
                let container = element.closest(
                  'tr, li, [role=row], article, [data-service-item], [data-matter-item]'
                );
                if (container && !describesItem(container)) container = null;
                let ancestor = element.parentElement;
                for (let depth = 0; !container && ancestor && depth < 5; depth += 1) {
                  if (['BODY', 'HTML', 'MAIN'].includes(ancestor.tagName)) break;
                  const text = (ancestor.innerText || '').replace(/\\s+/g, ' ').trim();
                  const actions = ancestor.querySelectorAll(
                    'button, a, [role=button], input, select, textarea'
                  ).length;
                  if (
                    text.length >= 4 && text.length <= 500 && actions <= 8 &&
                    describesItem(ancestor)
                  ) {
                    container = ancestor;
                    break;
                  }
                  ancestor = ancestor.parentElement;
                }
                if (!container) return '';
                const clone = container.cloneNode(true);
                clone.querySelectorAll('script, style, input, textarea, select')
                  .forEach(child => child.remove());
                return (clone.innerText || clone.textContent || '')
                  .replace(/\\s+/g, ' ').trim().slice(0, 500);
              };
              const grounded = elements.map((element, index) => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                const visible = style.display !== 'none' && style.visibility !== 'hidden' &&
                    Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
                if (!visible) return null;
                const intersectionLeft = Math.max(0, rect.left);
                const intersectionTop = Math.max(0, rect.top);
                const intersectionRight = Math.min(window.innerWidth, rect.right);
                const intersectionBottom = Math.min(window.innerHeight, rect.bottom);
                const intersectsViewport = intersectionRight > intersectionLeft &&
                    intersectionBottom > intersectionTop;
                if (modalOpen && !intersectsViewport) return null;
                if (intersectsViewport) {
                    const hitTarget = document.elementFromPoint(
                        (intersectionLeft + intersectionRight) / 2,
                        (intersectionTop + intersectionBottom) / 2
                    );
                    if (
                        !hitTarget || (hitTarget !== element && !element.contains(hitTarget))
                    ) return null;
                }
                const id = `vision-${index}`;
                element.setAttribute('data-smartfill-vision-id', id);
                const labels = element.labels
                    ? Array.from(element.labels).map(label => {
                        const clone = label.cloneNode(true);
                        clone.querySelectorAll('input, textarea, select, button')
                            .forEach(control => control.remove());
                        return clone.innerText.trim();
                    }).join(' ')
                    : '';
                const accessibleName = element.getAttribute('aria-label') || labels ||
                    element.getAttribute('placeholder') || element.innerText?.trim() ||
                    element.getAttribute('title') || element.getAttribute('name') || '';
                const tag = element.tagName.toLowerCase();
                const inputType = (element.getAttribute('type') || '').toLowerCase();
                const options = tag === 'select'
                    ? Array.from(element.options).map(option => option.text.trim())
                    : [];
                const filled = element.dataset.smartfillVisionFilled === 'true';
                return {
                    element_id: id,
                    role: element.getAttribute('role') || ({
                        input: inputType === 'checkbox' ? 'checkbox' : 'textbox',
                        textarea: 'textbox', select: 'combobox', button: 'button', a: 'link'
                    }[tag] || tag),
                    accessible_name: accessibleName.slice(0, 300),
                    tag,
                    input_type: inputType,
                    name: (element.getAttribute('name') || '').slice(0, 200),
                    context: actionContext(element),
                    filled,
                    enabled: !element.disabled,
                    bounding_box: [
                        rect.x + window.scrollX,
                        rect.y + window.scrollY,
                        rect.width,
                        rect.height
                    ],
                    options: options.slice(0, 100)
                };
              }).filter(Boolean);
              return {elements: grounded, modal_open: modalOpen};
            }"""
        )
        elements = [
            BrowserElement.model_validate(item)
            for item in raw_result["elements"][:_MAX_GROUNDED_ELEMENTS]
        ]
        locators = {
            element.element_id: page.locator(
                f'[data-smartfill-vision-id="{element.element_id}"]'
            )
            for element in elements
        }
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        await self._prepare_screenshot(page, elements)
        try:
            screenshot = await self._capture_screenshot(page, screenshot_path)
        finally:
            await self._restore_screenshot(page)
        return ObservedVisualPage(
            observation=BrowserObservation(
                url=page.url,
                screenshot_base64=base64.b64encode(screenshot).decode(),
                elements=elements,
            ),
            locators=locators,
            screenshot_path=screenshot_path,
            modal_open=bool(raw_result["modal_open"]),
        )

    async def _capture_screenshot(self, page: Page, screenshot_path: Path) -> bytes:
        try:
            return await page.screenshot(
                path=screenshot_path,
                type="png",
                full_page=True,
                timeout=self._screenshot_timeout_ms,
            )
        except PlaywrightTimeoutError:
            logger.warning(
                "Full-page screenshot timed out while waiting for page resources; "
                "using Chromium CDP fallback"
            )
            return await self._capture_screenshot_via_cdp(page, screenshot_path)

    async def _capture_screenshot_via_cdp(
        self,
        page: Page,
        screenshot_path: Path,
    ) -> bytes:
        cdp = await page.context.new_cdp_session(page)
        try:
            metrics = await cdp.send("Page.getLayoutMetrics")
            content_size = metrics.get("cssContentSize") or metrics["contentSize"]
            result = await cdp.send(
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "fromSurface": True,
                    "captureBeyondViewport": True,
                    "clip": {
                        "x": 0,
                        "y": 0,
                        "width": max(1, content_size["width"]),
                        "height": max(1, content_size["height"]),
                        "scale": 1,
                    },
                },
            )
        finally:
            await cdp.detach()
        screenshot = base64.b64decode(result["data"])
        screenshot_path.write_bytes(screenshot)
        return screenshot

    @staticmethod
    async def _prepare_screenshot(page: Page, elements: list[BrowserElement]) -> None:
        await page.evaluate(
            """elements => {
                document.querySelectorAll('[data-smartfill-vision-mark]')
                    .forEach(mark => mark.remove());
                document.querySelectorAll('input[type=password]').forEach(input => {
                    input.dataset.smartfillPreviousFilter = input.style.filter || '';
                    input.style.filter = 'blur(9px)';
                });
                for (const item of elements) {
                    const target = document.querySelector(
                        `[data-smartfill-vision-id="${item.element_id}"]`
                    );
                    if (!target) continue;
                    const mark = document.createElement('span');
                    mark.setAttribute('data-smartfill-vision-mark', 'true');
                    mark.textContent = item.element_id.replace('vision-', '');
                    Object.assign(mark.style, {
                        position: 'absolute', left: `${Math.max(0, item.bounding_box[0])}px`,
                        top: `${Math.max(0, item.bounding_box[1])}px`, zIndex: '2147483647',
                        color: '#fff', background: '#d3212d', border: '1px solid #fff',
                        borderRadius: '3px', padding: '1px 3px', font: 'bold 11px sans-serif',
                        pointerEvents: 'none'
                    });
                    document.documentElement.appendChild(mark);
                }
            }""",
            [element.model_dump() for element in elements],
        )

    @staticmethod
    async def _restore_screenshot(page: Page) -> None:
        await page.evaluate(
            """() => {
                document.querySelectorAll('[data-smartfill-vision-mark]')
                    .forEach(mark => mark.remove());
                document.querySelectorAll('input[type=password]').forEach(input => {
                    input.style.filter = input.dataset.smartfillPreviousFilter || '';
                    delete input.dataset.smartfillPreviousFilter;
                });
            }"""
        )


class VisualPocAgent:
    """Run a bounded screenshot-first observe-decide-act loop."""

    def __init__(
        self,
        *,
        provider: VisionProvider,
        allowed_origins: set[str],
        artifacts_root: Path,
        minimum_confidence: float = 0.85,
        max_steps: int = 18,
    ) -> None:
        self._provider = provider
        self._allowed_origins = {normalize_origin(origin) for origin in allowed_origins}
        self._artifacts_root = artifacts_root
        self._minimum_confidence = minimum_confidence
        self._max_steps = max_steps
        self._observer = VisualPageObserver()
        self._policy = ActionPolicy(
            self._allowed_origins,
            minimum_confidence=minimum_confidence,
            auto_submit=True,
        )

    async def run(
        self,
        page: Page,
        *,
        model: str,
        trial: int,
        values: dict[str, str],
        success_marker: str,
    ) -> VisualPocResult:
        safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "-", model)
        artifact_dir = self._artifacts_root / safe_model / f"trial-{trial}"
        steps_dir = artifact_dir / "steps"
        trace: list[dict[str, Any]] = []
        completed_fields: set[str] = set()
        recent_actions: list[str] = []
        started = time.perf_counter()
        model_calls = 0
        browser_actions = 0
        guardrail_corrections = 0
        false_completions = 0
        registration_submitted = False
        note_submitted = False

        async def finish(
            success: bool,
            category: str | None = None,
            message: str | None = None,
        ) -> VisualPocResult:
            usage = getattr(self._provider, "usage", VisionUsage())
            if not isinstance(usage, VisionUsage):
                usage = VisionUsage()
            provider_corrections = getattr(
                self._provider,
                "guardrail_corrections",
                0,
            )
            if not isinstance(provider_corrections, int):
                provider_corrections = 0
            result = VisualPocResult(
                model=model,
                trial=trial,
                success=success,
                failure_category=category,
                failure_message=message,
                model_calls=model_calls,
                browser_actions=browser_actions,
                guardrail_corrections=(
                    guardrail_corrections + provider_corrections
                ),
                false_completions=false_completions,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                duration_ms=round((time.perf_counter() - started) * 1000),
                final_url=page.url,
                artifact_dir=str(artifact_dir.resolve()),
            )
            artifact_dir.mkdir(parents=True, exist_ok=True)
            if success:
                await page.screenshot(path=artifact_dir / "final.png", type="png")
            (artifact_dir / "trace.json").write_text(
                json.dumps(trace, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (artifact_dir / "result.json").write_text(
                result.model_dump_json(indent=2),
                encoding="utf-8",
            )
            return result

        for step in range(1, self._max_steps + 1):
            if await self._outcome_visible(page, success_marker, values):
                return await finish(True)
            if normalize_origin(page.url) not in self._allowed_origins:
                trace.append(
                    self._trace_error(step, "origin_violation", "Current origin is not allowed")
                )
                return await finish(False, "origin_violation", "Current origin is not allowed")

            observed = await self._observer.observe(page, steps_dir / f"step-{step:02d}.png")
            request = VisionRequest(
                task=self._task_prompt(
                    completed_fields=completed_fields,
                    recent_actions=recent_actions,
                    success_marker=success_marker,
                    registration_submitted=registration_submitted,
                ),
                observation=observed.observation,
                canonical_fields=self._canonical_fields_for_page(
                    observed.observation,
                    values,
                ),
            )
            call_started = time.perf_counter()
            model_calls += 1
            try:
                decision = await self._provider.analyze(request)
            except Exception as error:
                trace.append(self._trace_error(step, "model_error", str(error)))
                return await finish(False, "model_error", str(error))
            trace.append(
                {
                    "step": step,
                    "status": "decision",
                    "url": page.url,
                    "screenshot": str(observed.screenshot_path.resolve()),
                    "model_latency_ms": round((time.perf_counter() - call_started) * 1000),
                    "elements": [element.model_dump() for element in observed.observation.elements],
                    "workflow_phase": (
                        "post_registration"
                        if registration_submitted
                        else "registration_required"
                    ),
                    "decision": decision.model_dump(mode="json"),
                }
            )

            decision, corrected_mappings = self._sanitize_impossible_mappings(
                decision,
                observed,
            )
            if corrected_mappings:
                guardrail_corrections += len(corrected_mappings)
                trace[-1]["guardrail"] = {
                    "dropped_non_field_mappings": corrected_mappings,
                    "reason": "A grounded action was available; links/buttons cannot be fields",
                }

            if self._is_human_challenge(decision):
                return await finish(
                    False,
                    "human_challenge",
                    "The model detected a CAPTCHA or MFA challenge",
                )

            mapping_error, mapped_count = await self._execute_mappings(
                decision,
                observed,
                values,
                completed_fields,
                recent_actions,
            )
            if mapping_error is not None:
                trace.append(self._trace_error(step, *mapping_error))
                return await finish(False, *mapping_error)
            if mapped_count:
                browser_actions += mapped_count
                await self._settle(page)
                continue

            action = decision.next_action
            if action is None:
                if decision.mappings:
                    guardrail_corrections += len(decision.mappings)
                    recent_actions.append(
                        "all returned mappings were already filled; choose a grounded action"
                    )
                    recent_actions = recent_actions[-6:]
                    continue
                if self._is_transient_page(decision.page_type):
                    guardrail_corrections += 1
                    recent_actions.append(
                        "transient page had no action; waited for a fresh observation"
                    )
                    recent_actions = recent_actions[-6:]
                    await page.wait_for_timeout(1_000)
                    continue
                trace.append(self._trace_error(step, "no_action", "Model returned no action"))
                return await finish(False, "no_action", "Model returned no action")
            if action.action is ActionType.FINISH:
                false_completions += 1
                trace.append(
                    self._trace_error(step, "false_completion", "Success marker is not visible")
                )
                return await finish(False, "false_completion", "Success marker is not visible")
            if action.action is ActionType.REQUEST_HUMAN:
                return await finish(False, "human_requested", "Model requested human review")

            try:
                action_error = await self._execute_action(
                    action,
                    observed,
                    page,
                    values,
                )
            except PlaywrightTimeoutError as error:
                message = f"Grounded action timed out: {error}"
                trace.append(self._trace_error(step, "action_timeout", message))
                return await finish(False, "action_timeout", message)
            if action_error is not None:
                trace.append(self._trace_error(step, *action_error))
                return await finish(False, *action_error)
            browser_actions += 1
            target_element = next(
                (
                    element
                    for element in observed.observation.elements
                    if element.element_id == action.element_id
                ),
                None,
            )
            if (
                action.action in {ActionType.CLICK, ActionType.SUBMIT}
                and target_element is not None
                and "register" in target_element.accessible_name.casefold()
                and {
                    "account.name",
                    "account.email",
                    "account.password",
                    "account.passwordConfirmation",
                }
                <= completed_fields
            ):
                registration_submitted = True
            if (
                action.action in {ActionType.CLICK, ActionType.SUBMIT}
                and target_element is not None
                and target_element.accessible_name.strip().casefold() == "create"
            ):
                note_submitted = True
            action_description = self._describe_action(action)
            if target_element is not None and target_element.accessible_name:
                action_description += f" label={target_element.accessible_name[:80]}"
            recent_actions.append(action_description)
            recent_actions = recent_actions[-6:]
            await self._settle(page)
            if note_submitted:
                with suppress(PlaywrightTimeoutError):
                    await page.get_by_text(success_marker, exact=True).wait_for(
                        state="visible",
                        timeout=5_000,
                    )

        trace.append(
            self._trace_error(self._max_steps, "step_limit", "Maximum agent steps reached")
        )
        return await finish(False, "step_limit", "Maximum agent steps reached")

    @staticmethod
    def _sanitize_impossible_mappings(
        decision: VisionDecision,
        observed: ObservedVisualPage,
    ) -> tuple[VisionDecision, list[dict[str, str]]]:
        action = decision.next_action
        if action is None or action.element_id not in observed.locators:
            return decision, []
        elements = {
            element.element_id: element for element in observed.observation.elements
        }
        invalid = [
            mapping
            for mapping in decision.mappings
            if mapping.element_id in elements
            and elements[mapping.element_id].tag not in {"input", "textarea", "select"}
        ]
        if not invalid:
            return decision, []
        invalid_ids = {id(mapping) for mapping in invalid}
        cleaned = [
            mapping for mapping in decision.mappings if id(mapping) not in invalid_ids
        ]
        corrections = [
            {
                "canonical_field": mapping.canonical_field,
                "element_id": mapping.element_id,
            }
            for mapping in invalid
        ]
        return decision.model_copy(update={"mappings": cleaned}), corrections

    async def _execute_mappings(
        self,
        decision: VisionDecision,
        observed: ObservedVisualPage,
        values: dict[str, str],
        completed_fields: set[str],
        recent_actions: list[str],
    ) -> tuple[tuple[str, str] | None, int]:
        count = 0
        elements = {element.element_id: element for element in observed.observation.elements}
        for mapping in decision.mappings:
            if mapping.canonical_field not in values:
                return ("invalid_field_reference", "Model referenced an unknown field"), count
            element = elements.get(mapping.element_id)
            locator = observed.locators.get(mapping.element_id)
            if element is None or locator is None:
                return ("invalid_element_reference", "Model invented an element ID"), count
            if element.filled:
                continue
            if mapping.confidence < self._minimum_confidence:
                return ("low_confidence", "Field mapping confidence is below policy"), count
            if element.tag not in {"input", "textarea", "select"}:
                return (
                    "invalid_field_target",
                    "Field mapping points to a non-field element",
                ), count
            await self._fill(locator, element, values[mapping.canonical_field])
            completed_fields.add(mapping.canonical_field)
            recent_actions.append(f"mapped {mapping.canonical_field} to {mapping.element_id}")
            count += 1
        del recent_actions[:-6]
        return None, count

    async def _execute_action(
        self,
        action: ActionProposal,
        observed: ObservedVisualPage,
        page: Page,
        values: dict[str, str],
    ) -> tuple[str, str] | None:
        decision = self._policy.evaluate(action, current_url=page.url)
        if not decision.allowed or decision.requires_human:
            return "policy_rejected", decision.reason
        locator = observed.locators.get(action.element_id or "")
        if action.element_id is not None and locator is None:
            return "invalid_element_reference", "Model invented an element ID"
        if action.action in {ActionType.CLICK, ActionType.SUBMIT}:
            if locator is None:
                return "invalid_element_reference", "Click action has no current element"
            await locator.click(timeout=5_000)
            return None
        if action.action in {ActionType.FILL, ActionType.SELECT}:
            if locator is None or action.value_ref is None:
                return "invalid_action", "Value action requires an element and field reference"
            field = action.value_ref.removeprefix("record://")
            if field not in values:
                return "invalid_field_reference", "Action referenced an unknown field"
            element = next(
                item
                for item in observed.observation.elements
                if item.element_id == action.element_id
            )
            await self._fill(locator, element, values[field])
            return None
        if action.action is ActionType.CHECK:
            if locator is None or action.checked is None:
                return "invalid_action", "Check action requires an element and checked state"
            await locator.set_checked(action.checked)
            return None
        if action.action is ActionType.SCROLL:
            viewport = page.viewport_size or {"height": 900}
            await page.mouse.wheel(0, round(viewport["height"] * 0.75))
            return None
        if action.action is ActionType.WAIT:
            return None
        return "unsupported_action", f"Unsupported POC action: {action.action.value}"

    @staticmethod
    async def _fill(locator: Locator, element: BrowserElement, value: str) -> None:
        if element.tag == "select":
            try:
                await locator.select_option(label=value)
            except PlaywrightTimeoutError:
                await locator.select_option(value=value)
        else:
            await locator.fill(value)
        await locator.evaluate(
            "element => element.dataset.smartfillVisionFilled = 'true'"
        )

    @staticmethod
    async def _settle(page: Page) -> None:
        with suppress(PlaywrightTimeoutError):
            await page.wait_for_load_state("domcontentloaded", timeout=2_000)
        with suppress(PlaywrightTimeoutError):
            await page.locator(
                ".spinner-border, [role=progressbar], [aria-busy=true]"
            ).first.wait_for(state="hidden", timeout=5_000)
        await page.wait_for_timeout(250)

    @staticmethod
    async def _outcome_visible(
        page: Page,
        marker: str,
        values: dict[str, str],
    ) -> bool:
        if not {"note.category", "note.description"} <= values.keys():
            return False
        title = page.get_by_text(marker, exact=True)
        description = page.get_by_text(values["note.description"], exact=True)
        category = page.get_by_text(values["note.category"], exact=True)
        return (
            await title.count() == 1
            and await title.is_visible()
            and await description.count() >= 1
            and await description.first.is_visible()
            and await category.count() >= 1
            and await category.first.is_visible()
        )

    @staticmethod
    def _is_human_challenge(decision: VisionDecision) -> bool:
        terms = " ".join(decision.interruptions).casefold()
        return any(term in terms for term in ("captcha", "验证码", "mfa", "two-factor"))

    @staticmethod
    def _is_transient_page(page_type: str) -> bool:
        normalized = page_type.casefold()
        return any(
            term in normalized for term in ("loading", "spinner", "transition")
        )

    @staticmethod
    def _describe_action(action: ActionProposal) -> str:
        target = f" {action.element_id}" if action.element_id else ""
        reference = f" {action.value_ref}" if action.value_ref else ""
        return f"{action.action.value}{target}{reference}"

    @staticmethod
    def _task_prompt(
        *,
        completed_fields: set[str],
        recent_actions: Sequence[str],
        success_marker: str,
        registration_submitted: bool,
    ) -> str:
        completed = ", ".join(sorted(completed_fields)) or "none"
        history = " | ".join(recent_actions[-6:]) or "none"
        phase = (
            "Signup was submitted. Never register again; log in with the new account, then add "
            "the note."
            if registration_submitted
            else "Signup is not submitted yet. Start with registration, not Login."
        )
        return (
            f"Goal: create a NEW Notes account. {phase} Add a note with Category, Title and "
            "Description, then Create it. Values are private. Map only visible writable elements "
            "with filled=false. A field may reappear unfilled on a later page and must be mapped "
            "again. Return mappings OR one grounded action, never both. Submit Register, Login "
            f"or Create as appropriate. Finish only when {success_marker!r} is visible. "
            f"Used fields: {completed}. Recent: {history}."
        )

    @staticmethod
    def _canonical_fields_for_page(
        observation: BrowserObservation,
        values: dict[str, str],
    ) -> list[str]:
        path = urlsplit(observation.url).path.rstrip("/")
        candidates: Sequence[str]
        if path.endswith("/register"):
            candidates = (
                "account.name",
                "account.email",
                "account.password",
                "account.passwordConfirmation",
            )
        elif path.endswith("/login"):
            candidates = ("account.email", "account.password")
        elif any(
            element.tag in {"input", "textarea", "select"}
            and element.accessible_name.casefold()
            in {"category", "title", "description"}
            for element in observation.elements
        ):
            candidates = ("note.category", "note.title", "note.description")
        else:
            candidates = tuple(values)
        return [field for field in candidates if field in values]

    @staticmethod
    def _trace_error(step: int, category: str, message: str) -> dict[str, Any]:
        return {
            "step": step,
            "status": "error",
            "failure_category": category,
            "message": message[:500],
        }


def _generated_values(trial_id: str) -> tuple[dict[str, str], str]:
    marker = f"SF-VISION-{trial_id}"
    password = f"Sf!{secrets.token_urlsafe(10)}9a"
    return (
        {
            "account.name": f"SmartFill {trial_id}",
            "account.email": f"smartfill.{trial_id.casefold()}@example.com",
            "account.password": password,
            "account.passwordConfirmation": password,
            "note.category": "Home",
            "note.title": marker,
            "note.description": "Created by the SmartFill visual agent POC",
        },
        marker,
    )


async def run_real_poc(
    *,
    models: Sequence[str],
    trials: int,
    target_url: str,
    headless: bool,
    artifacts_root: Path,
) -> list[VisualPocResult]:
    settings = Settings()
    if settings.dashscope_api_key is None:
        raise ValueError("SMARTFILL_DASHSCOPE_API_KEY is required")
    origin = normalize_origin(target_url)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_root = artifacts_root / run_id
    results: list[VisualPocResult] = []
    async with async_playwright() as playwright:
        for model in models:
            for trial in range(1, trials + 1):
                browser = await playwright.chromium.launch(headless=headless)
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    device_scale_factor=1,
                    locale="en-US",
                )
                await context.route("**/*", _route_without_ads)
                page = await context.new_page()
                provider = AliyunVisionProvider(
                    api_key=settings.dashscope_api_key.get_secret_value(),
                    model=model,
                    base_url=settings.dashscope_base_url,
                )
                trial_id = f"{run_id[-7:-1]}-{model.rsplit('-', 1)[-1]}-{trial}"
                values, marker = _generated_values(trial_id)
                try:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
                    result = await VisualPocAgent(
                        provider=provider,
                        allowed_origins={origin},
                        artifacts_root=run_root,
                    ).run(
                        page,
                        model=model,
                        trial=trial,
                        values=values,
                        success_marker=marker,
                    )
                except Exception as error:
                    model_dir = re.sub(r"[^A-Za-z0-9_.-]+", "-", model)
                    result = VisualPocResult(
                        model=model,
                        trial=trial,
                        success=False,
                        failure_category="runner_error",
                        failure_message=str(error)[:500],
                        final_url=page.url,
                        artifact_dir=str((run_root / model_dir / f"trial-{trial}").resolve()),
                    )
                finally:
                    await context.close()
                    await browser.close()
                results.append(result)
                print(result.model_dump_json())
    summary_path = run_root / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps([result.model_dump(mode="json") for result in results], indent=2),
        encoding="utf-8",
    )
    print(f"summary={summary_path.resolve()}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the SmartFill dual-model vision POC")
    parser.add_argument(
        "--models",
        nargs="+",
        default=["qwen3-vl-flash", "qwen3-vl-plus"],
    )
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument(
        "--target-url",
        default="https://practice.expandtesting.com/notes/app",
    )
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--artifacts-root", type=Path, default=Path("artifacts/vision-poc"))
    args = parser.parse_args()
    parsed = urlsplit(args.target_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SystemExit("--target-url must be a valid HTTP(S) URL")
    if not 1 <= args.trials <= 10:
        raise SystemExit("--trials must be between 1 and 10")
    asyncio.run(
        run_real_poc(
            models=args.models,
            trials=args.trials,
            target_url=args.target_url,
            headless=not args.headed,
            artifacts_root=args.artifacts_root,
        )
    )


if __name__ == "__main__":
    main()
