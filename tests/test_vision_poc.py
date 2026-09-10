import base64
import json
import struct
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from smartfill.execution import ActionProposal, ActionType
from smartfill.vision import FieldMapping, VisionDecision, VisionRequest
from smartfill.vision_poc import VisualPageObserver, VisualPocAgent, _is_ad_url


@pytest.mark.asyncio
async def test_visual_observer_falls_back_to_cdp_when_font_wait_times_out(
    tmp_path: Path,
) -> None:
    page = AsyncMock()
    page.screenshot.side_effect = PlaywrightTimeoutError("waiting for fonts to load")
    cdp = AsyncMock()
    cdp.send.side_effect = [
        {"cssContentSize": {"width": 1200, "height": 1800}},
        {"data": base64.b64encode(b"fallback-png").decode()},
    ]
    page.context.new_cdp_session.return_value = cdp
    screenshot_path = tmp_path / "fallback.png"

    screenshot = await VisualPageObserver(
        screenshot_timeout_ms=1_234
    )._capture_screenshot(page, screenshot_path)

    assert screenshot == b"fallback-png"
    assert screenshot_path.read_bytes() == b"fallback-png"
    page.screenshot.assert_awaited_once_with(
        path=screenshot_path,
        type="png",
        full_page=True,
        timeout=1_234,
    )
    cdp.detach.assert_awaited_once()

NOTES_POC_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Notes POC</title></head>
<body><main id="app">
  <h1>Welcome to Notes</h1>
  <button type="button">Login</button>
  <button type="button" id="register-entry">Register</button>
</main>
<script>
const app = document.querySelector('#app');
document.querySelector('#register-entry').addEventListener('click', showRegister);
function showRegister() {
  app.innerHTML = `<h1>Create an Account</h1><form id="register-form">
    <label>Name <input name="name"></label>
    <label>Email <input name="email" type="email"></label>
    <label>Password <input name="password" type="password"></label>
    <label>Confirm Password <input name="confirmPassword" type="password"></label>
    <button type="submit">Register</button></form>`;
  document.querySelector('#register-form').addEventListener('submit', event => {
    event.preventDefault();
    app.innerHTML = `<h1>Login after registration</h1><form id="login-form">
      <label>Email <input name="email" type="email"></label>
      <label>Password <input name="password" type="password"></label>
      <button type="submit">Login</button></form>`;
    document.querySelector('#login-form').addEventListener('submit', loginEvent => {
      loginEvent.preventDefault();
      app.innerHTML = `<h1>My Notes</h1><button id="add-note">+ Add Note</button>`;
      document.querySelector('#add-note').addEventListener('click', showNote);
    });
  });
}
function showNote() {
  app.insertAdjacentHTML('beforeend', `<div role="dialog" aria-label="Add new note">
    <label>Category <select name="category">
      <option>Home</option><option>Work</option>
    </select></label>
    <label>Title <input name="title"></label>
    <label>Description <textarea name="description"></textarea></label>
    <button id="create-note">Create</button></div>`);
  document.querySelector('#create-note').addEventListener('click', () => {
    const category = document.querySelector('[name=category]').value;
    const title = document.querySelector('[name=title]').value;
    const description = document.querySelector('[name=description]').value;
    app.innerHTML = `<h1>My Notes</h1><article><strong>${title}</strong>
      <span>${category}</span><p>${description}</p></article>`;
  });
}
</script></body></html>"""


class PocHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        content = NOTES_POC_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def poc_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


class NotesProvider:
    calls = 0

    async def analyze(self, request: VisionRequest) -> VisionDecision:
        self.calls += 1
        elements = request.observation.elements

        def find(name: str):
            return next(
                element
                for element in elements
                if element.accessible_name.casefold() == name.casefold()
            )

        names = {element.accessible_name for element in elements}
        if "Register" in names and "Name" not in names:
            entry = find("Register")
            decision = self._action(ActionType.CLICK, entry.element_id)
            return decision.model_copy(
                update={
                    "mappings": [
                        FieldMapping(
                            canonical_field="account.name",
                            element_id=entry.element_id,
                            confidence=0.99,
                            evidence="incorrect mapping attached to a valid entry action",
                        )
                    ]
                }
            )
        if {"Name", "Email", "Password", "Confirm Password"} <= names:
            mappings = []
            pairs = {
                "account.name": "Name",
                "account.email": "Email",
                "account.password": "Password",
                "account.passwordConfirmation": "Confirm Password",
            }
            for field, label in pairs.items():
                element = find(label)
                if not element.filled:
                    mappings.append(
                        FieldMapping(
                            canonical_field=field,
                            element_id=element.element_id,
                            confidence=0.99,
                            evidence=f"visible label {label}",
                        )
                    )
            if mappings:
                return VisionDecision(
                    page_type="registration",
                    interruptions=[],
                    mappings=mappings,
                )
            return self._action(ActionType.SUBMIT, find("Register").element_id)
        if {"Email", "Password", "Login"} <= names:
            mappings = []
            for field, label in {
                "account.email": "Email",
                "account.password": "Password",
            }.items():
                element = find(label)
                if not element.filled:
                    mappings.append(
                        FieldMapping(
                            canonical_field=field,
                            element_id=element.element_id,
                            confidence=0.99,
                            evidence=f"visible label {label}",
                        )
                    )
            if mappings:
                return VisionDecision(
                    page_type="login_after_registration",
                    interruptions=[],
                    mappings=mappings,
                )
            return self._action(ActionType.SUBMIT, find("Login").element_id)
        if "Create" in names:
            mappings = []
            for field, label in {
                "note.category": "Category",
                "note.title": "Title",
                "note.description": "Description",
            }.items():
                element = find(label)
                if not element.filled:
                    mappings.append(
                        FieldMapping(
                            canonical_field=field,
                            element_id=element.element_id,
                            confidence=0.99,
                            evidence=f"visible label {label}",
                        )
                    )
            if mappings:
                return VisionDecision(
                    page_type="note_dialog",
                    interruptions=[],
                    mappings=mappings,
                )
            return self._action(ActionType.SUBMIT, find("Create").element_id)
        if "+ Add Note" in names:
            return self._action(ActionType.CLICK, find("+ Add Note").element_id)
        return VisionDecision(
            page_type="complete",
            interruptions=[],
            mappings=[],
            next_action=ActionProposal(action=ActionType.FINISH, confidence=0.99),
        )

    @staticmethod
    def _action(action: ActionType, element_id: str) -> VisionDecision:
        return VisionDecision(
            page_type="interactive",
            interruptions=[],
            mappings=[],
            next_action=ActionProposal(
                action=action,
                element_id=element_id,
                confidence=0.99,
            ),
        )


class HallucinatingProvider:
    async def analyze(self, request: VisionRequest) -> VisionDecision:
        return VisionDecision(
            page_type="landing",
            interruptions=[],
            mappings=[],
            next_action=ActionProposal(
                action=ActionType.CLICK,
                element_id="invented-element",
                confidence=0.99,
            ),
        )


@pytest.mark.asyncio
async def test_observer_never_uses_a_password_value_as_semantic_context(
    tmp_path: Path,
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            await page.set_content(
                '<input type="password" value="Do-not-send-this-secret">'
            )
            observed = await VisualPageObserver().observe(
                page,
                tmp_path / "password.png",
            )
        finally:
            await browser.close()

    serialized = observed.observation.model_dump_json()
    assert "Do-not-send-this-secret" not in serialized


@pytest.mark.asyncio
async def test_observer_excludes_controls_occluded_by_a_modal_overlay(
    tmp_path: Path,
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 800, "height": 600})
        try:
            await page.set_content(
                """
                <a href="/hidden-target" style="position:absolute;left:40px;top:40px">
                  社会保障卡
                </a>
                <div style="position:fixed;inset:0;background:white;z-index:10">
                  <button style="position:absolute;left:300px;top:250px">确认</button>
                </div>
                """
            )
            observed = await VisualPageObserver().observe(
                page,
                tmp_path / "occluded.png",
            )
        finally:
            await browser.close()

    names = {element.accessible_name for element in observed.observation.elements}
    assert names == {"确认"}


@pytest.mark.asyncio
async def test_observer_captures_and_grounds_controls_below_initial_viewport(
    tmp_path: Path,
) -> None:
    screenshot_path = tmp_path / "full-page.png"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 800, "height": 600})
        try:
            await page.set_content(
                """
                <main style="height:1400px">
                  <h1>首屏内容</h1>
                  <button id="below-fold" style="position:absolute;top:1050px"
                    onclick="document.body.dataset.targetClicked = 'yes'">
                    页面下方的目标入口
                  </button>
                </main>
                """
            )
            observed = await VisualPageObserver().observe(page, screenshot_path)
            matching = [
                element
                for element in observed.observation.elements
                if element.accessible_name == "页面下方的目标入口"
            ]
            assert len(matching) == 1
            assert matching[0].bounding_box[1] > 600
            assert matching[0].element_id in observed.locators

            await observed.locators[matching[0].element_id].click()
            assert await page.evaluate("document.body.dataset.targetClicked") == "yes"
        finally:
            await browser.close()

    png = screenshot_path.read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    _, screenshot_height = struct.unpack(">II", png[16:24])
    assert screenshot_height > 600


@pytest.mark.asyncio
async def test_observer_adds_row_context_to_repeated_action_labels(
    tmp_path: Path,
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 800, "height": 600})
        try:
            await page.set_content(
                """
                <div class="service-row">
                  <span>其他教育事项</span>
                  <div class="actions"><button>办事指南</button></div>
                </div>
                <div class="service-row">
                  <span>机动车驾驶员培训备案</span>
                  <div class="actions"><button>办事指南</button></div>
                </div>
                """
            )
            observed = await VisualPageObserver().observe(
                page,
                tmp_path / "repeated-actions.png",
            )
        finally:
            await browser.close()

    guides = [
        element
        for element in observed.observation.elements
        if element.accessible_name == "办事指南"
    ]
    assert len(guides) == 2
    assert "其他教育事项" in guides[0].context
    assert "机动车驾驶员培训备案" in guides[1].context


@pytest.mark.asyncio
async def test_observer_excludes_offscreen_background_actions_while_modal_is_open(
    tmp_path: Path,
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 800, "height": 600})
        try:
            await page.set_content(
                """
                <main style="height:1400px">
                  <button style="position:absolute;top:1050px">其他事项办事指南</button>
                </main>
                <div role="dialog" aria-modal="true"
                  style="position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:10">
                  <section style="margin:200px;background:white">
                    <button>成都市</button>
                  </section>
                </div>
                """
            )
            observed = await VisualPageObserver().observe(
                page,
                tmp_path / "modal-actions.png",
            )
        finally:
            await browser.close()

    names = {
        element.accessible_name for element in observed.observation.elements
    }
    assert observed.modal_open is True
    assert names == {"成都市"}


@pytest.mark.asyncio
async def test_observer_grounds_script_driven_anchor_without_href(
    tmp_path: Path,
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 800, "height": 600})
        try:
            await page.set_content(
                """
                <a id="qgfw05" onclick="showCategory('social-card')">
                  社会保障卡 <span>&gt;</span>
                </a>
                <script>function showCategory() { document.body.dataset.clicked = 'yes'; }</script>
                """
            )
            observed = await VisualPageObserver().observe(
                page,
                tmp_path / "script-anchor.png",
            )
        finally:
            await browser.close()

    matching = [
        element
        for element in observed.observation.elements
        if "社会保障卡" in element.accessible_name
    ]
    assert len(matching) == 1
    assert matching[0].role == "link"
    assert matching[0].element_id in observed.locators


def test_eval_ad_filter_does_not_block_target_or_google_accounts() -> None:
    assert _is_ad_url("https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js")
    assert _is_ad_url("https://securepubads.g.doubleclick.net/tag/js/gpt.js")
    assert not _is_ad_url("https://practice.expandtesting.com/notes/app")
    assert not _is_ad_url("https://accounts.google.com/v3/signin")


def test_transient_visual_state_is_recognized() -> None:
    assert VisualPocAgent._is_transient_page("loading")
    assert VisualPocAgent._is_transient_page("registration_spinner")
    assert not VisualPocAgent._is_transient_page("login")


@pytest.mark.asyncio
async def test_visual_poc_completes_notes_flow_and_redacts_trace(
    poc_server: str,
    tmp_path: Path,
) -> None:
    provider = NotesProvider()
    values = {
        "account.name": "SmartFill POC",
        "account.email": "poc@example.com",
        "account.password": "Do-not-leak-123!",
        "account.passwordConfirmation": "Do-not-leak-123!",
        "note.category": "Home",
        "note.title": "VISION-POC-001",
        "note.description": "Created through screenshot semantics",
    }
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            await page.goto(poc_server)
            result = await VisualPocAgent(
                provider=provider,
                allowed_origins={poc_server},
                artifacts_root=tmp_path,
            ).run(
                page,
                model="fake-notes-model",
                trial=1,
                values=values,
                success_marker="VISION-POC-001",
            )
        finally:
            await browser.close()

    assert result.success is True
    assert result.model_calls >= 4
    assert result.browser_actions >= 4
    assert result.false_completions == 0
    assert result.guardrail_corrections == 1
    assert (Path(result.artifact_dir) / "final.png").exists()
    trace = (Path(result.artifact_dir) / "trace.json").read_text(encoding="utf-8")
    assert "Do-not-leak-123!" not in trace
    assert list((Path(result.artifact_dir) / "steps").glob("*.png"))


@pytest.mark.asyncio
async def test_visual_poc_rejects_an_element_not_in_the_current_observation(
    poc_server: str,
    tmp_path: Path,
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            await page.goto(poc_server)
            result = await VisualPocAgent(
                provider=HallucinatingProvider(),
                allowed_origins={poc_server},
                artifacts_root=tmp_path,
            ).run(
                page,
                model="hallucinating-model",
                trial=1,
                values={"note.title": "never-created"},
                success_marker="never-created",
            )
        finally:
            await browser.close()

    assert result.success is False
    assert result.browser_actions == 0
    assert result.failure_category == "invalid_element_reference"
    trace = json.loads((Path(result.artifact_dir) / "trace.json").read_text())
    assert trace[-1]["status"] == "error"
