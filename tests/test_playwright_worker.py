from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from smartfill.browser_jobs import (
    BrowserJobStatus,
    BrowserRunRequest,
    EntryActionConfig,
    EntryActionMode,
    HumanResolution,
    JobProgress,
    PageScanRequest,
    SubmissionConfig,
    SubmissionPolicy,
    WorkflowStep,
)
from smartfill.browser_worker import PlaywrightBrowserWorker, PlaywrightPageObserver
from smartfill.field_schema import FieldDefinition
from smartfill.secrets import InMemorySecretStore

TARGET_HTML = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>资料测试页</title></head>
<body>
  <dialog open aria-label="活动广告">
    <form method="dialog"><button type="submit">关闭</button></form>
    <p>限时活动广告</p>
  </dialog>
  <main>
    <label>用户姓名 <input name="display_name" type="text"></label>
    <label>性别
      <select name="gender">
        <option value="">请选择</option><option value="male">男</option>
        <option value="female">女</option>
      </select>
    </label>
    <label>身份证号 <input name="id_number" type="text"></label>
    <label>手机号 <input name="mobile" type="tel" autocomplete="tel"></label>
    <label>邮箱 <input name="email" type="email" autocomplete="email"></label>
    <button type="button">保存资料</button>
  </main>
</body>
</html>"""

COMPOSED_HTML = """<!doctype html>
<html lang="zh-CN">
<body>
  <label>用户姓名 <input name="display_name"></label>
  <iframe title="身份资料" src="/frame"></iframe>
  <section id="contact-widget"></section>
  <script>
    const root = document.querySelector('#contact-widget').attachShadow({mode: 'open'});
    root.innerHTML = `<label>电子邮箱
      <input name="shadow_email" type="email" autocomplete="email">
    </label>`;
  </script>
</body>
</html>"""

FRAME_HTML = """<!doctype html>
<html lang="zh-CN"><body>
  <label>身份证号码 <input name="id_number"></label>
  <label>手机号码 <input name="mobile" type="tel" autocomplete="tel"></label>
</body></html>"""

AMBIGUOUS_HTML = """<!doctype html>
<html lang="zh-CN"><body>
  <label>姓名 <input name="first_candidate"></label>
  <label>姓名 <input name="second_candidate"></label>
</body></html>"""

PARABANK_STYLE_HTML = """<!doctype html>
<html lang="en"><body>
  <aside>
    <label>Username <input name="login.username"></label>
    <label>Password <input name="login.password" type="password"></label>
  </aside>
  <main aria-label="Registration">
    <label>First Name <input name="customer.firstName"></label>
    <label>Last Name <input name="customer.lastName"></label>
    <label>City <input name="customer.address.city"></label>
    <label>State <input name="customer.address.state"></label>
    <label>Zip Code <input name="customer.address.zipCode"></label>
    <label>SSN <input name="customer.ssn"></label>
  </main>
</body></html>"""

SUBMIT_HTML = """<!doctype html>
<html lang="zh-CN"><body>
  <form action="/submitted" method="get">
    <label>姓名 <input name="display_name"></label>
    <button type="submit">Register</button>
  </form>
</body></html>"""

SUBMITTED_HTML = """<!doctype html>
<html lang="zh-CN"><body><h1>Registration complete</h1></body></html>"""

LOGIN_HOME_HTML = """<!doctype html>
<html lang="zh-CN"><body>
  <header><a href="/login-form">登录 / Login</a></header>
  <main><h1>官网首页</h1><p>登录表单尚未加载</p></main>
</body></html>"""

LOGIN_FORM_HTML = """<!doctype html>
<html lang="zh-CN"><body>
  <main aria-label="用户登录">
    <label>账号 <input name="username" autocomplete="username"></label>
    <label>密码 <input name="password" type="password" autocomplete="current-password"></label>
    <button type="submit">登录</button>
  </main>
</body></html>"""

AMBIGUOUS_ENTRY_HTML = """<!doctype html>
<html lang="zh-CN"><body>
  <nav><a href="/login-form">Login</a></nav>
  <main><a href="/login-form">Login</a></main>
</body></html>"""

AUTO_ENTRY_HOME_HTML = """<!doctype html>
<html lang="en"><head><title>Notes App</title></head><body>
  <main>
    <h1>Welcome to Notes App</h1>
    <a href="/email-login-form">Login</a>
    <a href="/register-form">Create an account</a>
  </main>
</body></html>"""

EMAIL_LOGIN_FORM_HTML = """<!doctype html>
<html lang="en"><head><title>Login</title></head><body>
  <main aria-label="Login">
    <h1>Login</h1>
    <form>
      <label>Email address <input name="email" type="email"></label>
      <label>Password <input name="password" type="password"></label>
      <button type="button">Login</button>
    </form>
  </main>
</body></html>"""

REGISTER_FORM_HTML = """<!doctype html>
<html lang="en"><head><title>Register</title></head><body>
  <main aria-label="Registration">
    <h1>Register</h1>
    <form>
      <label>Email address <input name="email" type="email"></label>
      <label>Name <input name="name" type="text"></label>
      <label>Password <input name="password" type="password">
        <label>Confirm Password <input name="confirmPassword" type="password"></label>
      </label>
      <button type="button">Register</button>
    </form>
  </main>
</body></html>"""

WORKFLOW_LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><body>
  <form action="/workflow-profile" method="get">
    <label>用户名 <input name="username" autocomplete="username"></label>
    <label>密码 <input name="password" type="password" autocomplete="current-password"></label>
    <button type="submit">Login</button>
  </form>
</body></html>"""

WORKFLOW_PROFILE_HTML = """<!doctype html>
<html lang="zh-CN"><body>
  <form>
    <label>姓名 <input name="display_name" autocomplete="name"></label>
    <label>邮箱 <input name="email" type="email" autocomplete="email"></label>
  </form>
</body></html>"""

SPA_NOTES_HTML = """<!doctype html>
<html lang="en"><body>
  <main id="app">
    <form id="login-form">
      <label>Email address <input name="email" type="email"></label>
      <label>Password <input name="password" type="password"></label>
      <button type="button" id="login">Login</button>
    </form>
  </main>
  <script>
    document.querySelector('#login').addEventListener('click', () => {
      window.setTimeout(() => {
        document.querySelector('#app').innerHTML = `
          <p>Logged in</p><button type="button" id="add-note">+ Add Note</button>`
        document.querySelector('#add-note').addEventListener('click', () => {
          window.setTimeout(() => {
            document.querySelector('#app').insertAdjacentHTML('beforeend', `
              <div role="dialog" aria-label="Add new note">
                <label>Category <select name="category">
                  <option value="Home">Home</option>
                </select></label>
                <label>Title <input name="title"></label>
                <label>Description <textarea name="description"></textarea></label>
                <button type="button" id="create-note">Create</button>
              </div>`)
          }, 150)
        })
      }, 150)
    })
  </script>
</body></html>"""

DELAYED_LOGIN_HTML = """<!doctype html>
<html lang="en"><body>
  <form>
    <label>Email address <input name="email" type="email"></label>
    <label>Password <input name="password" type="password"></label>
    <button type="button" id="login">Login</button>
  </form>
  <script>
    document.querySelector('#login').addEventListener('click', () => {
      window.location.href = '/delayed-notes'
    })
  </script>
</body></html>"""

DELAYED_NOTES_HTML = """<!doctype html>
<html lang="en"><body>
  <nav>""" + "".join(
    f'<button type="button">Navigation {index}</button>' for index in range(25)
) + """</nav>
  <main id="app">Loading...</main>
  <script>
    window.setTimeout(() => {
      document.querySelector('#app').innerHTML =
        '<button type="button" id="add-note">+ Add Note</button>'
      document.querySelector('#add-note').addEventListener('click', () => {
        document.querySelector('#app').innerHTML = `
          <div role="dialog" aria-label="Add new note">
            <button type="button" aria-label="Close">&times;</button>
            <label>Category <select name="category">
              <option value="Home">Home</option>
            </select></label>
            <label>Title <input name="title"></label>
            <label>Description <textarea name="description"></textarea></label>
            <button type="button" id="create-note">Create</button>
          </div>`
      })
    }, 250)
  </script>
</body></html>"""

OPEN_NOTE_FORM_HTML = """<!doctype html>
<html lang="en"><body>
  <nav>""" + "".join(
    f'<button type="button">Navigation {index}</button>' for index in range(25)
) + """</nav>
  <div role="dialog" aria-label="Add new note">
    <button type="button" aria-label="Close">&times;</button>
    <label>Category <select name="category">
      <option value="Home">Home</option>
    </select></label>
    <label>Title <input name="title"></label>
    <label>Description <textarea name="description"></textarea></label>
    <button type="button" id="create-note">Create</button>
  </div>
</body></html>"""

MANY_ACTIONS_HTML = """<!doctype html>
<html lang="en"><body><main>""" + "".join(
    f'<button type="button">Navigation {index}</button>' for index in range(25)
) + """</main></body></html>"""


class TargetHandler(BaseHTTPRequestHandler):
    spa_notes_requests = 0

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/spa-notes":
            type(self).spa_notes_requests += 1
        pages = {
            "/composed": COMPOSED_HTML,
            "/frame": FRAME_HTML,
            "/ambiguous": AMBIGUOUS_HTML,
            "/parabank-style": PARABANK_STYLE_HTML,
            "/submit": SUBMIT_HTML,
            "/submitted": SUBMITTED_HTML,
            "/login-home": LOGIN_HOME_HTML,
            "/login-form": LOGIN_FORM_HTML,
            "/ambiguous-entry": AMBIGUOUS_ENTRY_HTML,
            "/auto-entry-home": AUTO_ENTRY_HOME_HTML,
            "/email-login-form": EMAIL_LOGIN_FORM_HTML,
            "/register-form": REGISTER_FORM_HTML,
            "/workflow-login": WORKFLOW_LOGIN_HTML,
            "/workflow-profile": WORKFLOW_PROFILE_HTML,
            "/spa-notes": SPA_NOTES_HTML,
            "/delayed-login": DELAYED_LOGIN_HTML,
            "/delayed-notes": DELAYED_NOTES_HTML,
            "/open-note-form": OPEN_NOTE_FORM_HTML,
            "/many-actions": MANY_ACTIONS_HTML,
        }
        content = pages.get(path, TARGET_HTML).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def target_server() -> str:
    TargetHandler.spa_notes_requests = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def note_step(target_url: str) -> WorkflowStep:
    return WorkflowStep(
        name="添加笔记",
        target_url=target_url,
        fields={
            "custom.category": "Home",
            "custom.title": "TDD note",
            "custom.description": "Created after SPA login",
        },
        field_definitions=[
            FieldDefinition(
                key="custom.category",
                display_name="Category",
                aliases=["Category"],
                input_kind="select",
            ),
            FieldDefinition(
                key="custom.title",
                display_name="Title",
                aliases=["Title"],
            ),
            FieldDefinition(
                key="custom.description",
                display_name="Description",
                aliases=["Description"],
            ),
        ],
        entry_action=EntryActionConfig(
            mode=EntryActionMode.CLICK,
            aliases=["Add Note"],
        ),
        submission=SubmissionConfig(
            policy=SubmissionPolicy.AUTO_SUBMIT,
            button_aliases=["Create"],
        ),
    )


@pytest.mark.asyncio
async def test_real_playwright_worker_closes_popup_fills_and_verifies_form(
    target_server: str,
    tmp_path: Path,
) -> None:
    secret_store = InMemorySecretStore()
    id_reference = secret_store.put("record/id", "110101199001011234")
    phone_reference = secret_store.put("record/phone", "13800138000")
    reports: list[JobProgress] = []

    async def report(progress: JobProgress) -> None:
        reports.append(progress)

    worker = PlaywrightBrowserWorker(
        secret_store=secret_store,
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )
    result = await worker.run(
        BrowserRunRequest(
            job_id="job-e2e",
            task_id="task-e2e",
            target_url=f"{target_server}/profile",
            fields={
                "person.fullName": "张三",
                "person.gender": "male",
                "person.idNumber": id_reference,
                "person.phone": phone_reference,
                "person.email": "zhangsan@example.com",
            },
        ),
        report,
    )

    assert result.status is BrowserJobStatus.COMPLETED
    assert result.completed_fields == 5
    assert result.screenshot_path is not None
    assert Path(result.screenshot_path).is_file()
    assert any("关闭弹窗" in progress.message for progress in reports)
    assert [
        progress.current_field
        for progress in reports
        if progress.status is BrowserJobStatus.FILLING
    ] == [
        "person.fullName",
        "person.gender",
        "person.idNumber",
        "person.phone",
        "person.email",
    ]


@pytest.mark.asyncio
async def test_worker_executes_login_then_profile_steps_in_one_browser_context(
    target_server: str,
    tmp_path: Path,
) -> None:
    reports: list[JobProgress] = []

    async def report(progress: JobProgress) -> None:
        reports.append(progress)

    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )
    steps = [
        WorkflowStep(
            name="登录",
            target_url=f"{target_server}/workflow-login",
            fields={"account.username": "demo", "account.password": "secret"},
            submission=SubmissionConfig(
                policy=SubmissionPolicy.AUTO_SUBMIT,
                button_aliases=["Login"],
            ),
        ),
        WorkflowStep(
            name="完善资料",
            target_url=f"{target_server}/workflow-profile",
            fields={"person.fullName": "张三", "person.email": "zhang@example.com"},
        ),
    ]
    result = await worker.run(
        BrowserRunRequest(
            job_id="job-workflow",
            task_id="task-workflow",
            target_url=steps[0].target_url,
            fields=steps[0].fields,
            field_definitions=steps[0].field_definitions,
            workflow_steps=steps,
        ),
        report,
    )

    assert result.status is BrowserJobStatus.COMPLETED
    assert result.completed_fields == 4
    assert result.current_url == f"{target_server}/workflow-profile"
    assert [report.current_step_name for report in reports if report.current_step] == [
        "登录",
        "完善资料",
    ]


@pytest.mark.asyncio
async def test_worker_waits_for_spa_transition_and_reuses_same_url_for_next_step(
    target_server: str,
    tmp_path: Path,
) -> None:
    reports: list[JobProgress] = []

    async def report(progress: JobProgress) -> None:
        reports.append(progress)

    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
        action_timeout_ms=3_000,
    )
    target_url = f"{target_server}/spa-notes"
    steps = [
        WorkflowStep(
            name="登录",
            target_url=target_url,
            fields={
                "account.username": "demo@example.com",
                "account.password": "secret",
            },
            submission=SubmissionConfig(
                policy=SubmissionPolicy.AUTO_SUBMIT,
                button_aliases=["Login"],
            ),
        ),
        WorkflowStep(
            name="添加笔记",
            target_url=target_url,
            fields={
                "custom.category": "Home",
                "custom.title": "TDD note",
                "custom.description": "Created after SPA login",
            },
            field_definitions=[
                FieldDefinition(
                    key="custom.category",
                    display_name="Category",
                    aliases=["Category"],
                    input_kind="select",
                ),
                FieldDefinition(
                    key="custom.title",
                    display_name="Title",
                    aliases=["Title"],
                ),
                FieldDefinition(
                    key="custom.description",
                    display_name="Description",
                    aliases=["Description"],
                ),
            ],
            entry_action=EntryActionConfig(
                mode=EntryActionMode.CLICK,
                aliases=["Add Note"],
            ),
            submission=SubmissionConfig(
                policy=SubmissionPolicy.AUTO_SUBMIT,
                button_aliases=["Create"],
            ),
        ),
    ]

    result = await worker.run(
        BrowserRunRequest(
            job_id="job-spa-notes",
            task_id="task-spa-notes",
            target_url=target_url,
            fields=steps[0].fields,
            workflow_steps=steps,
        ),
        report,
    )

    assert result.status is BrowserJobStatus.COMPLETED
    assert result.completed_fields == 5
    assert result.submitted is True
    assert TargetHandler.spa_notes_requests == 1
    assert any("复用当前页面" in item.message for item in reports)
    assert any(
        item.status is BrowserJobStatus.ENTERING and "Add Note" in item.message
        for item in reports
    )


@pytest.mark.asyncio
async def test_worker_waits_for_cross_url_spa_content_before_starting_next_step(
    target_server: str,
    tmp_path: Path,
) -> None:
    reports: list[JobProgress] = []

    async def report(progress: JobProgress) -> None:
        reports.append(progress)

    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
        action_timeout_ms=3_000,
    )
    login_step = WorkflowStep(
        name="登录",
        target_url=f"{target_server}/delayed-login",
        fields={
            "account.username": "demo@example.com",
            "account.password": "secret",
        },
        submission=SubmissionConfig(
            policy=SubmissionPolicy.AUTO_SUBMIT,
            button_aliases=["Login"],
        ),
    )
    notes_step = note_step(f"{target_server}/delayed-notes")

    result = await worker.run(
        BrowserRunRequest(
            job_id="job-cross-url-spa",
            task_id="task-cross-url-spa",
            target_url=login_step.target_url,
            fields=login_step.fields,
            workflow_steps=[login_step, notes_step],
        ),
        report,
    )

    assert result.status is BrowserJobStatus.COMPLETED
    assert result.completed_fields == 5
    assert result.submitted is True
    assert any(
        item.status is BrowserJobStatus.ENTERING and "Add Note" in item.message
        for item in reports
    )


@pytest.mark.asyncio
async def test_worker_skips_configured_entry_when_target_form_is_already_open(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )
    step = note_step(f"{target_server}/open-note-form")

    result = await worker.run(
        BrowserRunRequest(
            job_id="job-open-note-form",
            task_id="task-open-note-form",
            target_url=step.target_url,
            fields=step.fields,
            field_definitions=step.field_definitions,
            submission=step.submission,
            entry_action=step.entry_action,
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert result.status is BrowserJobStatus.COMPLETED
    assert result.completed_fields == 3


@pytest.mark.asyncio
async def test_worker_limits_unmatched_entry_candidates_for_human_review(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )

    result = await worker.run(
        BrowserRunRequest(
            job_id="job-many-actions",
            task_id="task-many-actions",
            target_url=f"{target_server}/many-actions",
            fields={"person.fullName": "张三"},
            entry_action=EntryActionConfig(
                mode=EntryActionMode.CLICK,
                aliases=["Open form"],
            ),
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert result.status is BrowserJobStatus.NEED_HUMAN
    assert result.intervention is not None
    assert len(result.intervention.entry_candidates) == 20


@pytest.mark.asyncio
async def test_worker_retains_completed_session_when_requested(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )
    request = BrowserRunRequest(
        job_id="job-retain-completed",
        task_id="task-retain-completed",
        target_url=f"{target_server}/profile",
        fields={"person.fullName": "张三"},
        keep_browser_open=True,
    )

    try:
        result = await worker.run(
            request,
            lambda _progress: _completed_awaitable(),
        )

        assert result.status is BrowserJobStatus.COMPLETED
        assert result.browser_session_open is True
        assert request.job_id in worker._sessions
    finally:
        await worker.cancel(request.job_id)

    assert request.job_id not in worker._sessions


@pytest.mark.asyncio
async def test_observer_collects_accessibility_trees_across_iframe_and_shadow_dom(
    target_server: str,
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(f"{target_server}/composed")

        observed = await PlaywrightPageObserver().observe(page, "job-observe")

        await browser.close()

    assert len(observed.accessibility_trees) == 2
    assert any("用户姓名" in tree.snapshot for tree in observed.accessibility_trees)
    assert any("身份证号码" in tree.snapshot for tree in observed.accessibility_trees)
    assert any(element.frame_path != "main" for element in observed.elements)
    assert any(element.tree_scope == "shadow" for element in observed.elements)


@pytest.mark.asyncio
async def test_worker_fills_iframe_and_open_shadow_dom_fields(
    target_server: str,
    tmp_path: Path,
) -> None:
    reports: list[JobProgress] = []

    async def report(progress: JobProgress) -> None:
        reports.append(progress)

    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )
    result = await worker.run(
        BrowserRunRequest(
            job_id="job-composed",
            task_id="task-composed",
            target_url=f"{target_server}/composed",
            fields={
                "person.fullName": "张三",
                "person.idNumber": "110101199001011234",
                "person.phone": "13800138000",
                "person.email": "zhangsan@example.com",
            },
        ),
        report,
    )

    assert result.status is BrowserJobStatus.COMPLETED
    assert result.completed_fields == 4
    assert any("Accessibility Tree" in progress.message for progress in reports)


@pytest.mark.asyncio
async def test_worker_resumes_same_session_after_human_selects_ambiguous_field(
    target_server: str,
    tmp_path: Path,
) -> None:
    async def report(_progress: JobProgress) -> None:
        return None

    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )
    paused = await worker.run(
        BrowserRunRequest(
            job_id="job-human",
            task_id="task-human",
            target_url=f"{target_server}/ambiguous",
            fields={"person.fullName": "张三"},
        ),
        report,
    )

    assert paused.status is BrowserJobStatus.NEED_HUMAN
    assert paused.intervention is not None
    candidates = paused.intervention.field_candidates[0].candidates
    assert len(candidates) == 2

    completed = await worker.resume(
        "job-human",
        HumanResolution(
            field_mappings={"person.fullName": candidates[0].element_id},
        ),
        report,
    )

    assert completed.status is BrowserJobStatus.COMPLETED
    assert completed.completed_fields == 1


@pytest.mark.asyncio
async def test_worker_fills_parabank_style_form_from_dynamic_schema(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )
    definitions = [
        FieldDefinition(key="person.firstName", display_name="名", aliases=["First Name"]),
        FieldDefinition(key="person.lastName", display_name="姓", aliases=["Last Name"]),
        FieldDefinition(key="person.city", display_name="城市", aliases=["City"]),
        FieldDefinition(key="person.state", display_name="州", aliases=["State"]),
        FieldDefinition(key="person.postalCode", display_name="邮编", aliases=["Zip Code"]),
        FieldDefinition(
            key="person.ssn",
            display_name="SSN",
            aliases=["SSN"],
            sensitive=True,
        ),
    ]

    result = await worker.run(
        BrowserRunRequest(
            job_id="job-dynamic",
            task_id="task-dynamic",
            target_url=f"{target_server}/parabank-style",
            fields={
                "person.firstName": "San",
                "person.lastName": "Zhang",
                "person.city": "Nanjing",
                "person.state": "Jiangsu",
                "person.postalCode": "210000",
                "person.ssn": "123-45-6789",
            },
            field_definitions=definitions,
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert result.status is BrowserJobStatus.COMPLETED
    assert result.completed_fields == 6


@pytest.mark.asyncio
async def test_fill_only_policy_never_clicks_submission_button(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )

    result = await worker.run(
        BrowserRunRequest(
            job_id="job-fill-only",
            task_id="task-fill-only",
            target_url=f"{target_server}/submit",
            fields={"person.fullName": "张三"},
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert result.status is BrowserJobStatus.COMPLETED
    assert result.submitted is False
    assert result.current_url == f"{target_server}/submit"


@pytest.mark.asyncio
async def test_auto_submit_clicks_one_semantically_matched_button(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )

    result = await worker.run(
        BrowserRunRequest(
            job_id="job-auto-submit",
            task_id="task-auto-submit",
            target_url=f"{target_server}/submit",
            fields={"person.fullName": "张三"},
            submission=SubmissionConfig(
                policy=SubmissionPolicy.AUTO_SUBMIT,
                button_aliases=["Register"],
            ),
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert result.status is BrowserJobStatus.COMPLETED
    assert result.submitted is True
    assert result.current_url is not None
    assert urlsplit(result.current_url).path == "/submitted"


@pytest.mark.asyncio
async def test_confirm_policy_retains_session_until_operator_approves_candidate(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )
    request = BrowserRunRequest(
        job_id="job-confirm-submit",
        task_id="task-confirm-submit",
        target_url=f"{target_server}/submit",
        fields={"person.fullName": "张三"},
        submission=SubmissionConfig(
            policy=SubmissionPolicy.CONFIRM_BEFORE_SUBMIT,
            button_aliases=["Register"],
        ),
    )

    paused = await worker.run(request, lambda _progress: _completed_awaitable())

    assert paused.status is BrowserJobStatus.NEED_HUMAN
    assert paused.completed_fields == 1
    assert paused.intervention is not None
    assert paused.intervention.kind.value == "submission_confirmation"
    candidate = paused.intervention.submission_candidates[0]
    assert candidate.accessible_name == "Register"

    completed = await worker.resume(
        request.job_id,
        HumanResolution(
            approve_submission=True,
            submit_element_id=candidate.element_id,
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert completed.status is BrowserJobStatus.COMPLETED
    assert completed.completed_fields == 1
    assert completed.submitted is True
    assert completed.current_url is not None
    assert urlsplit(completed.current_url).path == "/submitted"


@pytest.mark.asyncio
async def test_worker_clicks_login_entry_then_scans_and_fills_login_form(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )

    result = await worker.run(
        BrowserRunRequest(
            job_id="job-login-entry",
            task_id="task-login-entry",
            target_url=f"{target_server}/login-home",
            fields={
                "account.username": "demo-user",
                "account.password": "temporary-password",
            },
            entry_action=EntryActionConfig(
                mode=EntryActionMode.CLICK,
                aliases=["登录", "Login"],
            ),
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert result.status is BrowserJobStatus.COMPLETED
    assert result.completed_fields == 2
    assert result.entry_action_performed is True
    assert result.current_url is not None
    assert urlsplit(result.current_url).path == "/login-form"


@pytest.mark.asyncio
async def test_worker_auto_plans_login_entry_and_uses_form_context_for_email_login(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )

    result = await worker.run(
        BrowserRunRequest(
            job_id="job-auto-login-entry",
            task_id="task-auto-login-entry",
            target_url=f"{target_server}/auto-entry-home",
            fields={
                "account.username": "demo@example.com",
                "account.password": "temporary-password",
            },
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert result.status is BrowserJobStatus.COMPLETED
    assert result.completed_fields == 2
    assert result.entry_action_performed is True
    assert result.current_url is not None
    assert urlsplit(result.current_url).path == "/email-login-form"


@pytest.mark.asyncio
async def test_worker_auto_plans_registration_and_maps_related_fields(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )
    definitions = [
        FieldDefinition(
            key="account.email",
            display_name="注册邮箱",
            aliases=["Email", "Email address"],
            input_kind="email",
        ),
        FieldDefinition(
            key="person.fullName",
            display_name="姓名",
            aliases=["Name", "Full Name"],
        ),
        FieldDefinition(
            key="account.password",
            display_name="密码",
            aliases=["Password"],
            input_kind="password",
            sensitive=True,
        ),
        FieldDefinition(
            key="account.passwordConfirmation",
            display_name="确认密码",
            aliases=["Confirm Password", "Confirm"],
            input_kind="password",
            sensitive=True,
            source_field="account.password",
        ),
    ]

    result = await worker.run(
        BrowserRunRequest(
            job_id="job-auto-register-entry",
            task_id="task-auto-register-entry",
            target_url=f"{target_server}/auto-entry-home",
            fields={
                "account.email": "demo@example.com",
                "person.fullName": "Demo User",
                "account.password": "temporary-password",
            },
            field_definitions=definitions,
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert result.status is BrowserJobStatus.COMPLETED
    assert result.completed_fields == 4
    assert result.entry_action_performed is True
    assert result.current_url is not None
    assert urlsplit(result.current_url).path == "/register-form"


@pytest.mark.asyncio
async def test_worker_auto_planning_asks_when_the_task_does_not_imply_an_entry_goal(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )

    result = await worker.run(
        BrowserRunRequest(
            job_id="job-auto-unknown-entry",
            task_id="task-auto-unknown-entry",
            target_url=f"{target_server}/auto-entry-home",
            fields={"person.fullName": "Demo User"},
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert result.status is BrowserJobStatus.NEED_HUMAN
    assert result.intervention is not None
    assert result.intervention.kind.value == "entry_action_confirmation"
    assert {candidate.accessible_name for candidate in result.intervention.entry_candidates} == {
        "Login",
        "Create an account",
    }


@pytest.mark.asyncio
async def test_page_scan_discovers_registration_field_relationships(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )

    result = await worker.scan_page(
        PageScanRequest(
            target_url=f"{target_server}/register-form",
            entry_action=EntryActionConfig(mode=EntryActionMode.DIRECT),
        )
    )

    assert [field.key for field in result.fields] == [
        "account.email",
        "person.fullName",
        "account.password",
        "account.passwordConfirmation",
    ]
    assert result.fields[-1].source_field == "account.password"


@pytest.mark.asyncio
async def test_page_scan_clicks_login_entry_and_discovers_login_field_schema(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )

    result = await worker.scan_page(
        PageScanRequest(
            target_url=f"{target_server}/login-home?state=sensitive-scan-token",
            entry_action=EntryActionConfig(
                mode=EntryActionMode.CLICK,
                aliases=["登录", "Login"],
            ),
        )
    )

    assert result.entry_action_performed is True
    assert "sensitive-scan-token" not in result.initial_url
    assert urlsplit(result.final_url).path == "/login-form"
    assert [field.key for field in result.fields] == [
        "account.username",
        "account.password",
    ]
    assert result.fields[1].input_kind.value == "password"
    assert result.fields[1].sensitive is True


@pytest.mark.asyncio
async def test_page_scan_auto_enters_a_login_form_when_the_landing_page_has_no_fields(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )

    result = await worker.scan_page(
        PageScanRequest(target_url=f"{target_server}/auto-entry-home")
    )

    assert result.entry_action_performed is True
    assert urlsplit(result.final_url).path == "/email-login-form"
    assert [field.key for field in result.fields] == [
        "account.username",
        "account.password",
    ]


@pytest.mark.asyncio
async def test_worker_pauses_for_ambiguous_login_entry_and_resumes_same_session(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
    )
    request = BrowserRunRequest(
        job_id="job-ambiguous-entry",
        task_id="task-ambiguous-entry",
        target_url=f"{target_server}/ambiguous-entry",
        fields={"account.username": "demo-user"},
        entry_action=EntryActionConfig(
            mode=EntryActionMode.CLICK,
            aliases=["Login"],
        ),
    )

    paused = await worker.run(request, lambda _progress: _completed_awaitable())

    assert paused.status is BrowserJobStatus.NEED_HUMAN
    assert paused.intervention is not None
    assert paused.intervention.kind.value == "entry_action_confirmation"
    assert len(paused.intervention.entry_candidates) == 2

    selected = paused.intervention.entry_candidates[0]
    completed = await worker.resume(
        request.job_id,
        HumanResolution(
            approve_entry_action=True,
            entry_element_id=selected.element_id,
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert completed.status is BrowserJobStatus.COMPLETED
    assert completed.entry_action_performed is True
    assert completed.completed_fields == 1


async def _completed_awaitable() -> None:
    return None
