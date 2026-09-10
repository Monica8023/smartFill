from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright

from smartfill.browser_jobs import (
    AuthenticationMode,
    BrowserJobStatus,
    BrowserRunRequest,
    EntryActionConfig,
    EntryActionMode,
    ExecutionStatistics,
    HumanResolution,
    InterventionKind,
    JobProgress,
    SubmissionConfig,
    SubmissionPolicy,
    WorkflowStep,
)
from smartfill.browser_worker import PlaywrightBrowserWorker, PlaywrightPageObserver
from smartfill.execution import ActionProposal, ActionType
from smartfill.field_schema import FieldDefinition
from smartfill.secrets import InMemorySecretStore
from smartfill.vision import (
    BrowserElement,
    BrowserObservation,
    FieldMapping,
    RequiredPageField,
    VisionDecision,
    VisionRequest,
    VisionResponseError,
)
from smartfill.vision_poc import ObservedVisualPage

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

PROPERTY_PORTAL_HTML = """<!doctype html><html lang="zh-CN"><body>
<main id="app"><button id="login">登录</button></main>
<script>
const app = document.querySelector('#app');
document.querySelector('#login').onclick = () => {
  app.innerHTML = `<label>用户名 <input name="username"></label>
    <label>密码 <input name="password" type="password"></label>
    <button id="login-submit">登录</button>`;
  document.querySelector('#login-submit').onclick = () => {
    app.innerHTML = `<button id="services">办事服务</button>`;
    document.querySelector('#services').onclick = () => {
      app.innerHTML = `<button id="assets">资产认证</button>`;
      document.querySelector('#assets').onclick = () => {
        app.innerHTML = `<button id="property">房产认证</button>`;
        document.querySelector('#property').onclick = () => {
          app.innerHTML = `<h1>房产认证信息</h1>
            <label>产权人姓名 <input name="owner"></label>
            <label>房产证号 <input name="certificate"></label>
            <label>本人居住 <input name="ownerOccupied" type="checkbox"></label>`;
        };
      };
    };
  };
};
</script></body></html>"""

MANUAL_LOGIN_HTML = """<!doctype html><html lang="zh-CN"><body><main id="app"></main>
<script>
const app = document.querySelector('#app');
if (document.cookie.includes('manualAuth=1')) {
  app.innerHTML = '<h1>认证业务中心</h1>';
} else {
  app.innerHTML = '<h1>短信验证登录</h1><button id="verify">模拟完成手机验证</button>';
  document.querySelector('#verify').onclick = () => {
    document.cookie = 'manualAuth=1; path=/; SameSite=Lax';
    location.reload();
  };
}
</script></body></html>"""

SERVICE_PORTAL_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>全国人社政务服务平台</title>
<style>#region-modal{position:fixed;inset:0;background:white;z-index:10}</style></head>
<body>
  <div id="region-modal" role="dialog" aria-label="地区确认">
    <p>检测到您所在省份, 是否正确?</p>
    <a id="confirm-region" onclick="confirmRegion()">确认</a>
    <a onclick="chooseRegion()">重新选择</a>
  </div>
  <main id="service-content">
    <button type="button" id="social-card">社会保障卡</button>
  </main>
  <script>
    function confirmRegion() {
      document.querySelector('#region-modal').remove();
    }
    function chooseRegion() {}
    document.querySelector('#social-card').onclick = () => {
      document.querySelector('#service-content').innerHTML =
        '<a href="/service-login">社保卡应用状态查询 [全国]</a>';
    };
  </script>
</body></html>"""

SERVICE_LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>登录 - 公共服务用户中心</title></head>
<body><main><h1>公共服务用户中心登录</h1>
  <label>账号 <input name="username"></label>
  <label>密码 <input name="password" type="password"></label>
  <button type="button">登录</button>
</main></body></html>"""

POPUP_NAVIGATION_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>新标签页入口</title></head>
<body><main>
  <a href="/popup-destination" target="_blank">目标业务入口</a>
</main></body></html>"""

POPUP_DESTINATION_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>目标业务页面</title></head>
<body><main><h1>目标业务页面</h1></main></body></html>"""

DELAYED_POPUP_NAVIGATION_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>延迟新标签页入口</title></head>
<body><main>
  <button type="button" id="guide">办事指南</button>
  <script>
    document.querySelector('#guide').onclick = () => {
      window.setTimeout(() => window.open('/delayed-popup-destination', '_blank'), 150);
    };
  </script>
</main></body></html>"""

DELAYED_POPUP_DESTINATION_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>最新办事指南</title></head>
<body><main><h1>机动车驾驶员培训备案办事指南</h1></main></body></html>"""

UNAPPROVED_POPUP_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>外部链接入口</title></head>
<body><main>
  <a href="https://www.sc.gov.cn/" target="_blank">四川省人民政府</a>
</main></body></html>"""

NO_PROGRESS_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>无响应入口</title></head>
<body><main><button type="button">无响应入口</button></main></body></html>"""

EDUCATION_SERVICE_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>个人服务</title></head>
<body><main>
  <nav><button type="button" aria-pressed="true">学习教育</button></nav>
  <section aria-label="学习教育事项">
    <article><span>适龄儿童入学审批</span><a href="/wrong-guide">办事指南</a></article>
    <article><span>机动车驾驶员培训备案</span><a href="/education-guide">办事指南</a></article>
  </section>
</main></body></html>"""

EDUCATION_GUIDE_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>机动车驾驶员培训备案</title></head>
<body><main><h1>机动车驾驶员培训备案办事指南</h1></main></body></html>"""

DOWNLOAD_PAGE_HTML = """<!doctype html><html lang="zh-CN"><body>
  <a href="/sample-guide.txt" download>下载文档</a>
</body></html>"""

TERMINAL_CLICK_HTML = """<!doctype html><html lang="zh-CN"><body>
  <button type="button" id="download">下载</button>
  <p id="result"></p>
  <script>
    document.querySelector('#download').onclick = () => {
      document.querySelector('#result').textContent = '下载动作已触发';
    };
  </script>
</body></html>"""

NO_MATCHING_ENTRY_HTML = """<!doctype html><html lang="zh-CN"><body>
  <button type="button">拖拉机培训机构理论教员考核</button>
  <button type="button">普通话水平测试</button>
</body></html>"""


class TargetHandler(BaseHTTPRequestHandler):
    spa_notes_requests = 0
    heartbeat_requests = 0

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/spa-notes":
            type(self).spa_notes_requests += 1
        if path == "/session/heartbeat":
            type(self).heartbeat_requests += 1
            authenticated = "manualAuth=1" in (self.headers.get("Cookie") or "")
            content = b"<html><body>session alive</body></html>"
            self.send_response(200 if authenticated else 401)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if path == "/sample-guide.txt":
            content = "机动车驾驶员培训备案办事指南".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header(
                "Content-Disposition",
                'attachment; filename="service-guide.txt"',
            )
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
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
            "/property-portal": PROPERTY_PORTAL_HTML,
            "/manual-login": MANUAL_LOGIN_HTML,
            "/service-portal": SERVICE_PORTAL_HTML,
            "/service-login": SERVICE_LOGIN_HTML,
            "/popup-navigation": POPUP_NAVIGATION_HTML,
            "/popup-destination": POPUP_DESTINATION_HTML,
            "/delayed-popup-navigation": DELAYED_POPUP_NAVIGATION_HTML,
            "/delayed-popup-destination": DELAYED_POPUP_DESTINATION_HTML,
            "/unapproved-popup": UNAPPROVED_POPUP_HTML,
            "/no-progress": NO_PROGRESS_HTML,
            "/education-services": EDUCATION_SERVICE_HTML,
            "/education-guide": EDUCATION_GUIDE_HTML,
            "/download-page": DOWNLOAD_PAGE_HTML,
            "/terminal-click": TERMINAL_CLICK_HTML,
            "/no-matching-entry": NO_MATCHING_ENTRY_HTML,
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
    TargetHandler.heartbeat_requests = 0
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
async def test_worker_pauses_ambiguous_field_without_exposing_dom_candidates(
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
    assert paused.intervention.kind is InterventionKind.VISUAL_REVIEW
    assert paused.intervention.requires_browser_interaction is True
    await worker.cancel("job-human")


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


def _fake_browser_request(
    url: str,
    *,
    resource_type: str = "document",
    method: str = "GET",
    navigation: bool = True,
    top_level: bool = True,
    stable_main_frame_identity: bool = True,
) -> SimpleNamespace:
    page = SimpleNamespace()
    main_frame = SimpleNamespace(page=page, parent_frame=None)
    page.main_frame = (
        main_frame
        if stable_main_frame_identity
        else SimpleNamespace(page=page, parent_frame=None)
    )
    frame = (
        main_frame
        if top_level
        else SimpleNamespace(page=page, parent_frame=main_frame)
    )
    return SimpleNamespace(
        url=url,
        resource_type=resource_type,
        method=method,
        frame=frame,
        is_navigation_request=lambda: navigation,
    )


@pytest.mark.asyncio
async def test_route_upgrades_allowed_top_level_http_navigation_without_logging_query(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={"https://secure.example.com"},
        artifacts_root=tmp_path,
        headless=True,
    )
    route = SimpleNamespace(
        continue_=AsyncMock(),
        fulfill=AsyncMock(),
        abort=AsyncMock(),
    )
    request = _fake_browser_request(
        "http://secure.example.com/login?return_to=private-value"
    )

    with caplog.at_level("INFO", logger="smartfill.browser_worker"):
        await worker._route_allowed_requests(route, request, job_id="job-upgrade")

    route.fulfill.assert_awaited_once_with(
        status=307,
        headers={
            "cache-control": "no-store",
            "location": "https://secure.example.com/login?return_to=private-value",
        },
        body="",
    )
    route.continue_.assert_not_awaited()
    route.abort.assert_not_awaited()
    assert "job-upgrade" in caplog.text
    assert "https://secure.example.com" in caplog.text
    assert "private-value" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "resource_type", "method", "navigation", "top_level"),
    [
        ("http://secure.example.com/app.js", "script", "GET", False, True),
        ("http://secure.example.com/frame", "document", "GET", True, False),
        ("http://unknown.example.com/login", "document", "GET", True, True),
        ("http://secure.example.com/login", "document", "POST", True, True),
        ("http://user:password@secure.example.com/login", "document", "GET", True, True),
    ],
)
async def test_route_does_not_upgrade_unsafe_http_requests(
    tmp_path: Path,
    url: str,
    resource_type: str,
    method: str,
    navigation: bool,
    top_level: bool,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={"https://secure.example.com"},
        artifacts_root=tmp_path,
        headless=True,
    )
    route = SimpleNamespace(
        continue_=AsyncMock(),
        fulfill=AsyncMock(),
        abort=AsyncMock(),
    )
    request = _fake_browser_request(
        url,
        resource_type=resource_type,
        method=method,
        navigation=navigation,
        top_level=top_level,
    )

    await worker._route_allowed_requests(route, request, job_id="job-block")

    route.continue_.assert_not_awaited()
    route.fulfill.assert_not_awaited()
    route.abort.assert_awaited_once_with("blockedbyclient")


@pytest.mark.asyncio
async def test_manual_login_allows_new_https_origin_for_current_session(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={"https://www.sczwfw.gov.cn"},
        artifacts_root=tmp_path,
        headless=True,
    )
    session_origins: set[str] = set()
    route = SimpleNamespace(
        continue_=AsyncMock(),
        fulfill=AsyncMock(),
        abort=AsyncMock(),
    )
    request = _fake_browser_request("https://zxbl.sczwfw.gov.cn/app/login")

    with caplog.at_level("INFO", logger="smartfill.browser_worker"):
        await worker._route_allowed_requests(
            route,
            request,
            job_id="job-manual-login",
            allow_new_https_origin=True,
            session_origins=session_origins,
        )

    route.continue_.assert_awaited_once_with()
    route.fulfill.assert_not_awaited()
    route.abort.assert_not_awaited()
    assert session_origins == {"https://zxbl.sczwfw.gov.cn"}
    assert "job-manual-login" in caplog.text
    assert "/app/login" not in caplog.text


@pytest.mark.asyncio
async def test_manual_login_allows_popup_main_frame_with_provisional_page_wrapper(
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={"https://www.sczwfw.gov.cn"},
        artifacts_root=tmp_path,
        headless=True,
    )
    route = SimpleNamespace(
        continue_=AsyncMock(),
        fulfill=AsyncMock(),
        abort=AsyncMock(),
    )

    await worker._route_allowed_requests(
        route,
        _fake_browser_request(
            "https://zxbl.sczwfw.gov.cn/app/login",
            stable_main_frame_identity=False,
        ),
        allow_new_https_origin=True,
        session_origins=set(),
    )

    route.continue_.assert_awaited_once_with()
    route.abort.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_login_allows_first_party_www_site_family_resources_only(
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={"https://www.sczwfw.gov.cn"},
        artifacts_root=tmp_path,
        headless=True,
    )
    session_origins: set[str] = set()
    first_party_route = SimpleNamespace(
        continue_=AsyncMock(),
        fulfill=AsyncMock(),
        abort=AsyncMock(),
    )
    deceptive_route = SimpleNamespace(
        continue_=AsyncMock(),
        fulfill=AsyncMock(),
        abort=AsyncMock(),
    )

    await worker._route_allowed_requests(
        first_party_route,
        _fake_browser_request(
            "https://qjd.sczwfw.gov.cn/assets/jquery.js",
            resource_type="script",
            navigation=False,
        ),
        allow_new_https_origin=True,
        session_origins=session_origins,
    )
    await worker._route_allowed_requests(
        deceptive_route,
        _fake_browser_request(
            "https://qjd.sczwfw.gov.cn.attacker.example/jquery.js",
            resource_type="script",
            navigation=False,
        ),
        allow_new_https_origin=True,
        session_origins=session_origins,
    )

    first_party_route.continue_.assert_awaited_once_with()
    assert "https://qjd.sczwfw.gov.cn" in session_origins
    deceptive_route.abort.assert_awaited_once_with("blockedbyclient")


@pytest.mark.asyncio
async def test_manual_login_navigation_trust_is_scoped_and_keeps_background_requests_strict(
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={"https://www.sczwfw.gov.cn"},
        artifacts_root=tmp_path,
        headless=True,
    )
    session_origins = {"https://zxbl.sczwfw.gov.cn"}

    trusted_resource_route = SimpleNamespace(
        continue_=AsyncMock(),
        fulfill=AsyncMock(),
        abort=AsyncMock(),
    )
    await worker._route_allowed_requests(
        trusted_resource_route,
        _fake_browser_request(
            "https://zxbl.sczwfw.gov.cn/app.js",
            resource_type="script",
            navigation=False,
        ),
        session_origins=session_origins,
    )
    trusted_resource_route.continue_.assert_awaited_once_with()

    cross_origin_route = SimpleNamespace(
        continue_=AsyncMock(),
        fulfill=AsyncMock(),
        abort=AsyncMock(),
    )
    await worker._route_allowed_requests(
        cross_origin_route,
        _fake_browser_request(
            "https://tracking.example.net/collect",
            resource_type="xhr",
            navigation=False,
        ),
        allow_new_https_origin=True,
        session_origins=session_origins,
    )
    cross_origin_route.abort.assert_awaited_once_with("blockedbyclient")
    assert "https://tracking.example.net" not in session_origins

    other_session_route = SimpleNamespace(
        continue_=AsyncMock(),
        fulfill=AsyncMock(),
        abort=AsyncMock(),
    )
    await worker._route_allowed_requests(
        other_session_route,
        _fake_browser_request(
            "https://zxbl.sczwfw.gov.cn/app.js",
            resource_type="script",
            navigation=False,
        ),
        session_origins=set(),
    )
    other_session_route.abort.assert_awaited_once_with("blockedbyclient")


class PropertyScenarioProvider:
    def __init__(self) -> None:
        self.tasks: list[str] = []

    async def analyze(self, request: VisionRequest) -> VisionDecision:
        self.tasks.append(request.task)
        by_name = {
            element.accessible_name: element
            for element in request.observation.elements
        }

        def action(name: str, action_type: ActionType = ActionType.CLICK) -> VisionDecision:
            return VisionDecision(
                page_type="navigation",
                interruptions=[],
                mappings=[],
                next_action=ActionProposal(
                    action=action_type,
                    element_id=by_name[name].element_id,
                    confidence=0.99,
                    evidence=f"visible target {name}",
                ),
            )

        if set(by_name) == {"登录"}:
            return action("登录")
        if {"用户名", "密码", "登录"} <= set(by_name):
            if "account.password" not in request.canonical_fields:
                return VisionDecision(
                    page_type="login",
                    interruptions=[],
                    mappings=[],
                    required_fields=[
                        RequiredPageField(
                            key="account.password",
                            display_name="登录密码",
                            input_kind="password",
                            sensitive=True,
                            reason="登录表单必填",
                        )
                    ],
                )
            mappings = [
                FieldMapping(
                    canonical_field=field,
                    element_id=by_name[label].element_id,
                    confidence=0.99,
                    evidence=f"visible {label}",
                )
                for field, label in {
                    "account.username": "用户名",
                    "account.password": "密码",
                }.items()
                if not by_name[label].filled
            ]
            if mappings:
                return VisionDecision(
                    page_type="login",
                    interruptions=[],
                    mappings=mappings,
                )
            return action("登录", ActionType.SUBMIT)
        for menu in ("办事服务", "资产认证", "房产认证"):
            if menu in by_name:
                return action(menu)
        if {"产权人姓名", "房产证号", "本人居住"} <= set(by_name):
            if "property.certificateNumber" not in request.canonical_fields:
                return VisionDecision(
                    page_type="property_form",
                    interruptions=[],
                    mappings=[],
                    target_reached=True,
                    required_fields=[
                        RequiredPageField(
                            key="property.certificateNumber",
                            display_name="房产证号",
                            sensitive=True,
                            reason="目标表单必填",
                        )
                    ],
                )
            mappings = [
                FieldMapping(
                    canonical_field=field,
                    element_id=by_name[label].element_id,
                    confidence=0.99,
                    evidence=f"visible {label}",
                )
                for field, label in {
                    "property.ownerName": "产权人姓名",
                    "property.certificateNumber": "房产证号",
                    "property.ownerOccupied": "本人居住",
                }.items()
                if not by_name[label].filled
            ]
            if mappings:
                return VisionDecision(
                    page_type="property_form",
                    interruptions=[],
                    mappings=mappings,
                    target_reached=True,
                )
            return VisionDecision(
                page_type="property_form_complete",
                interruptions=[],
                mappings=[],
                target_reached=True,
                next_action=ActionProposal(action=ActionType.FINISH, confidence=0.99),
            )
        raise AssertionError(f"unexpected elements: {set(by_name)}")


class ManualSessionProvider:
    async def analyze(self, request: VisionRequest) -> VisionDecision:
        assert "认证业务中心" in request.task or request.observation.elements == []
        return VisionDecision(
            page_type="authenticated_portal",
            interruptions=[],
            mappings=[],
            target_reached=True,
            next_action=ActionProposal(action=ActionType.FINISH, confidence=0.99),
        )


class ServicePortalProvider:
    def __init__(self) -> None:
        self.tasks: list[str] = []
        self.rejected_ungrounded_action = False

    async def analyze(self, request: VisionRequest) -> VisionDecision:
        self.tasks.append(request.task)
        if request.observation.url.endswith("/service-login"):
            raise AssertionError("entry-only workflow must finish before analyzing login")
        by_name = {
            element.accessible_name: element
            for element in request.observation.elements
        }
        if "确认" in by_name:
            raise AssertionError("region confirmation must be handled before model analysis")
        if "社会保障卡" in by_name and not self.rejected_ungrounded_action:
            self.rejected_ungrounded_action = True
            raise VisionResponseError(
                "Vision provider referenced an unrecognized field or element: "
                "action.element_id='search-button'"
            )
        for name in ("社会保障卡", "社保卡应用状态查询 [全国]"):
            if name in by_name:
                return VisionDecision(
                    page_type="service_navigation",
                    interruptions=[],
                    mappings=[],
                    next_action=ActionProposal(
                        action=ActionType.CLICK,
                        element_id=by_name[name].element_id,
                        confidence=0.99,
                        evidence=f"截图中可见目标: {name}",
                        accessible_name=name,
                    ),
                )
        raise AssertionError(f"unexpected service portal elements: {set(by_name)}")


class PopupNavigationProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, request: VisionRequest) -> VisionDecision:
        self.calls += 1
        if request.observation.url.endswith("/popup-destination"):
            return VisionDecision(
                page_type="target_page",
                interruptions=[],
                mappings=[],
                target_reached=True,
                next_action=ActionProposal(action=ActionType.FINISH, confidence=0.99),
            )
        if self.calls > 1:
            raise AssertionError("worker did not switch to the newly opened page")
        target = next(
            element
            for element in request.observation.elements
            if element.accessible_name == "目标业务入口"
        )
        return VisionDecision(
            page_type="navigation",
            interruptions=[],
            mappings=[],
            next_action=ActionProposal(
                action=ActionType.CLICK,
                element_id=target.element_id,
                accessible_name=target.accessible_name,
                confidence=0.99,
                evidence="目标入口在当前截图中可见",
            ),
        )


class DelayedPopupNavigationProvider:
    def __init__(self) -> None:
        self.observed_urls: list[str] = []

    async def analyze(self, request: VisionRequest) -> VisionDecision:
        self.observed_urls.append(request.observation.url)
        if request.observation.url.endswith("/delayed-popup-destination"):
            return VisionDecision(
                page_type="guide",
                interruptions=[],
                mappings=[],
                target_reached=True,
                next_action=ActionProposal(action=ActionType.FINISH, confidence=0.99),
            )
        target = next(
            element
            for element in request.observation.elements
            if element.accessible_name == "办事指南"
        )
        return VisionDecision(
            page_type="navigation",
            interruptions=[],
            mappings=[],
            next_action=ActionProposal(
                action=ActionType.CLICK,
                element_id=target.element_id,
                accessible_name=target.accessible_name,
                confidence=0.99,
                evidence="办事指南入口在当前截图中可见",
            ),
        )


class UnapprovedPopupProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, request: VisionRequest) -> VisionDecision:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("worker reopened the unapproved target")
        target = next(
            element
            for element in request.observation.elements
            if element.accessible_name == "四川省人民政府"
        )
        return VisionDecision(
            page_type="navigation",
            interruptions=[],
            mappings=[],
            next_action=ActionProposal(
                action=ActionType.CLICK,
                element_id=target.element_id,
                accessible_name=target.accessible_name,
                confidence=0.99,
                evidence="外部链接在当前截图中可见",
            ),
        )


class NoProgressProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, request: VisionRequest) -> VisionDecision:
        self.calls += 1
        target = next(
            element
            for element in request.observation.elements
            if element.accessible_name == "无响应入口"
        )
        return VisionDecision(
            page_type="navigation",
            interruptions=[],
            mappings=[],
            next_action=ActionProposal(
                action=ActionType.CLICK,
                element_id=target.element_id,
                accessible_name=target.accessible_name,
                confidence=0.99,
                evidence="入口在当前截图中可见",
            ),
        )


class RepeatedEducationProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.tasks: list[str] = []

    async def analyze(self, request: VisionRequest) -> VisionDecision:
        self.calls += 1
        self.tasks.append(request.task)
        if request.observation.url.endswith("/education-guide"):
            return VisionDecision(
                page_type="guide",
                interruptions=[],
                mappings=[],
                target_reached=True,
                next_action=ActionProposal(action=ActionType.FINISH, confidence=0.99),
            )
        education = next(
            element
            for element in request.observation.elements
            if element.accessible_name == "学习教育"
        )
        return VisionDecision(
            page_type="service_list",
            interruptions=[],
            mappings=[],
            next_action=ActionProposal(
                action=ActionType.CLICK,
                element_id=education.element_id,
                accessible_name=education.accessible_name,
                confidence=0.99,
                evidence="学习教育板块已显示",
            ),
        )


class DownloadProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, request: VisionRequest) -> VisionDecision:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("worker did not complete after saving the download")
        target = next(
            element
            for element in request.observation.elements
            if element.accessible_name == "下载文档"
        )
        return VisionDecision(
            page_type="guide",
            interruptions=[],
            mappings=[],
            next_action=ActionProposal(
                action=ActionType.CLICK,
                element_id=target.element_id,
                accessible_name=target.accessible_name,
                confidence=0.99,
                evidence="任务要求下载办事指南文档",
            ),
        )


class TerminalClickProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, request: VisionRequest) -> VisionDecision:
        self.calls += 1
        if self.calls > 1:
            return VisionDecision(
                page_type="guide",
                interruptions=[],
                mappings=[],
                next_action=None,
            )
        target = next(
            element
            for element in request.observation.elements
            if element.accessible_name == "下载"
        )
        return VisionDecision(
            page_type="guide",
            interruptions=[],
            mappings=[],
            next_action=ActionProposal(
                action=ActionType.CLICK,
                element_id=target.element_id,
                accessible_name=target.accessible_name,
                confidence=0.99,
                evidence="下载是目标描述中的最后一步",
            ),
        )


class NoMatchingEntryProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, request: VisionRequest) -> VisionDecision:
        self.calls += 1
        target = next(
            element
            for element in request.observation.elements
            if element.accessible_name == "拖拉机培训机构理论教员考核"
        )
        return VisionDecision(
            page_type="service_list",
            interruptions=[],
            mappings=[],
            next_action=ActionProposal(
                action=ActionType.CLICK,
                element_id=target.element_id,
                accessible_name=target.accessible_name,
                confidence=0.99,
                evidence="当前页面没有目标入口",
            ),
        )


def _visual_observation(
    *elements: BrowserElement,
    modal_open: bool = False,
) -> ObservedVisualPage:
    return ObservedVisualPage(
        observation=BrowserObservation(
            url="https://example.test/services",
            screenshot_base64="",
            elements=list(elements),
        ),
        locators={},
        screenshot_path=Path("observation.png"),
        modal_open=modal_open,
    )


def test_region_target_action_selects_each_requested_location_level() -> None:
    city_page = _visual_observation(
        BrowserElement(
            element_id="chengdu",
            role="button",
            tag="button",
            accessible_name="成都市",
        ),
        BrowserElement(
            element_id="mianyang",
            role="button",
            tag="button",
            accessible_name="绵阳市",
        ),
        modal_open=True,
    )
    district_page = _visual_observation(
        BrowserElement(
            element_id="tianfu",
            role="button",
            tag="button",
            accessible_name="天府新区",
        ),
        BrowserElement(
            element_id="jinjiang",
            role="button",
            tag="button",
            accessible_name="锦江区",
        ),
        modal_open=True,
    )
    target = "地区选择成都市天府新区, 在新页面进入办事指南并下载文档"

    city_action = PlaywrightBrowserWorker._target_region_action(target, city_page)
    district_action = PlaywrightBrowserWorker._target_region_action(
        target,
        district_page,
    )

    assert city_action is not None
    assert city_action.element_id == "chengdu"
    assert district_action is not None
    assert district_action.element_id == "tianfu"


def test_repeated_generic_action_must_match_its_service_row_context() -> None:
    observed = _visual_observation(
        BrowserElement(
            element_id="wrong-guide",
            role="button",
            tag="button",
            accessible_name="办事指南",
            context="辖区学校中小学学生学籍管理服务 办事指南 在线办理",
        ),
        BrowserElement(
            element_id="target-guide",
            role="button",
            tag="button",
            accessible_name="办事指南",
            context="机动车驾驶员培训备案 办事指南 在线办理",
        ),
    )
    target = "找到学习教育板块的机动车驾驶员培训备案, 点击办事指南"

    assert not PlaywrightBrowserWorker._action_matches_target_context(
        target,
        observed,
        ActionProposal(
            action=ActionType.CLICK,
            element_id="wrong-guide",
            accessible_name="办事指南",
            confidence=0.99,
        ),
    )
    assert PlaywrightBrowserWorker._action_matches_target_context(
        target,
        observed,
        ActionProposal(
            action=ActionType.CLICK,
            element_id="target-guide",
            accessible_name="办事指南",
            confidence=0.99,
        ),
    )


def test_repeated_category_action_advances_to_target_service_row_guide() -> None:
    observed = _visual_observation(
        BrowserElement(
            element_id="education",
            role="button",
            tag="button",
            accessible_name="学习教育",
            context="按生命周期 学习教育 当前已选中",
        ),
        BrowserElement(
            element_id="wrong-guide",
            role="button",
            tag="button",
            accessible_name="办事指南",
            context="适龄儿童入学审批 办事指南 在线办理",
        ),
        BrowserElement(
            element_id="target-guide",
            role="button",
            tag="button",
            accessible_name="办事指南",
            context="机动车驾驶员培训备案 办事指南 在线办理",
        ),
    )
    target = (
        "找到个人服务的学习教育板块的机动车驾驶员培训备案,点击办事指南,"
        "地区选择成都市天府新区,在新页面进入办事指南并下载文档"
    )

    preferred = PlaywrightBrowserWorker._prefer_progressing_target_action(
        target,
        observed,
        ActionProposal(
            action=ActionType.CLICK,
            element_id="education",
            accessible_name="学习教育",
            confidence=0.99,
        ),
        last_action_label="学习教育",
    )

    assert preferred.element_id == "target-guide"
    assert preferred.accessible_name == "办事指南"
    assert preferred.confidence == 1
    assert "机动车驾驶员培训备案" in preferred.evidence


def test_unrelated_business_action_is_rejected_despite_model_confidence() -> None:
    observed = _visual_observation(
        BrowserElement(
            element_id="wrong-training",
            role="button",
            tag="button",
            accessible_name="拖拉机培训机构理论教员考核",
            context="学习教育 拖拉机培训机构理论教员考核",
        ),
        BrowserElement(
            element_id="target-training",
            role="button",
            tag="button",
            accessible_name="机动车驾驶员培训备案",
            context="学习教育 机动车驾驶员培训备案",
        ),
    )
    target = "找到个人服务的学习教育板块的机动车驾驶员培训备案"

    assert not PlaywrightBrowserWorker._action_matches_target_context(
        target,
        observed,
        ActionProposal(
            action=ActionType.CLICK,
            element_id="wrong-training",
            accessible_name="拖拉机培训机构理论教员考核",
            confidence=0.99,
        ),
    )
    assert PlaywrightBrowserWorker._action_matches_target_context(
        target,
        observed,
        ActionProposal(
            action=ActionType.CLICK,
            element_id="target-training",
            accessible_name="机动车驾驶员培训备案",
            confidence=0.99,
        ),
    )


def test_single_unambiguous_gateway_can_advance_toward_the_goal() -> None:
    observed = _visual_observation(
        BrowserElement(
            element_id="assets",
            role="button",
            tag="button",
            accessible_name="资产认证",
        )
    )

    assert PlaywrightBrowserWorker._action_matches_target_context(
        "找到填写房产认证信息的入口并填写资料",
        observed,
        ActionProposal(
            action=ActionType.CLICK,
            element_id="assets",
            accessible_name="资产认证",
            confidence=0.96,
        ),
    )


def test_visual_goal_is_split_into_ordered_semantic_milestones() -> None:
    target = (
        "找到个人服务的学习教育板块的机动车驾驶员培训备案, 点击办事指南, "
        "地区选择成都市天府新区, 在新页面进入办事指南并下载文档"
    )

    milestones = PlaywrightBrowserWorker._visual_goal_milestones(target)

    assert milestones == [
        "个人服务",
        "学习教育",
        "机动车驾驶员培训备案",
        "办事指南",
        "成都市",
        "天府新区",
        "办事指南",
        "下载文档",
    ]


def test_only_last_requested_action_completes_visual_goal_after_screenshot() -> None:
    target = (
        "找到个人服务的学习教育板块的机动车驾驶员培训备案,"
        "点击办事指南,地区选择成都市天府新区,随后点击下载按钮"
    )

    assert not PlaywrightBrowserWorker._last_action_completes_visual_goal(
        target,
        "办事指南",
    )
    assert PlaywrightBrowserWorker._last_action_completes_visual_goal(
        target,
        "下载",
    )
    assert not PlaywrightBrowserWorker._last_action_completes_visual_goal(
        "找到社会保障卡应用状态查询入口并点击",
        "社会保障卡",
    )


def test_negated_human_interruption_does_not_pause_visual_execution() -> None:
    assert not PlaywrightBrowserWorker._decision_requires_human(
        ["当前页面无需人工处理"]
    )
    assert not PlaywrightBrowserWorker._decision_requires_human(
        ["no human intervention required"]
    )
    assert PlaywrightBrowserWorker._decision_requires_human(["需要人工处理验证码"])


@pytest.mark.asyncio
async def test_visual_worker_saves_requested_download_and_completes(
    target_server: str,
    tmp_path: Path,
) -> None:
    provider = DownloadProvider()
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
        vision_provider=provider,
    )

    completed = await worker.run(
        BrowserRunRequest(
            job_id="job-download-guide",
            task_id="task-download-guide",
            target_url=f"{target_server}/download-page",
            fields={},
            target_intent="进入办事指南并下载文档",
            observation_interval_seconds=1,
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert completed.status is BrowserJobStatus.COMPLETED
    assert provider.calls == 1
    assert len(completed.download_paths) == 1
    downloaded = Path(completed.download_paths[0])
    assert downloaded.name == "service-guide.txt"
    assert downloaded.read_text(encoding="utf-8") == "机动车驾驶员培训备案办事指南"


@pytest.mark.asyncio
async def test_visual_worker_completes_after_screenshot_following_terminal_click(
    target_server: str,
    tmp_path: Path,
) -> None:
    provider = TerminalClickProvider()
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
        vision_provider=provider,
    )

    completed = await worker.run(
        BrowserRunRequest(
            job_id="job-terminal-click",
            task_id="task-terminal-click",
            target_url=f"{target_server}/terminal-click",
            fields={},
            target_intent="进入办事指南, 随后点击下载按钮",
            observation_interval_seconds=1,
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert completed.status is BrowserJobStatus.COMPLETED
    assert "结果截图" in completed.message
    assert completed.screenshot_path is not None
    assert Path(completed.screenshot_path).exists()
    assert completed.statistics is not None
    assert completed.statistics.screenshot_count == 2
    assert completed.statistics.model_call_count == 1
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_visual_worker_completes_without_entry_after_three_by_three_searches(
    target_server: str,
    tmp_path: Path,
) -> None:
    provider = NoMatchingEntryProvider()
    progress_events: list[JobProgress] = []

    async def record_progress(progress: JobProgress) -> None:
        progress_events.append(progress)

    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
        vision_provider=provider,
    )

    completed = await worker.run(
        BrowserRunRequest(
            job_id="job-no-matching-entry",
            task_id="task-no-matching-entry",
            target_url=f"{target_server}/no-matching-entry",
            fields={},
            target_intent="找到机动车驾驶员培训备案入口",
            observation_interval_seconds=1,
        ),
        record_progress,
    )

    assert completed.status is BrowserJobStatus.COMPLETED
    assert "9 次" in completed.message
    assert "未发现与任务匹配的入口" in completed.message
    assert completed.statistics is not None
    assert completed.statistics.screenshot_count == 9
    assert completed.statistics.model_call_count == 9
    assert completed.statistics.browser_action_count == 0
    assert provider.calls == 9
    fallback_messages = [
        progress.message
        for progress in progress_events
        if "自动进入下一轮" in progress.message
    ]
    assert fallback_messages == [
        "第 1/3 轮的 3 次查找未命中, 自动进入下一轮",
        "第 2/3 轮的 3 次查找未命中, 自动进入下一轮",
    ]


@pytest.mark.asyncio
async def test_visual_worker_switches_to_new_tab_instead_of_reopening_it(
    target_server: str,
    tmp_path: Path,
) -> None:
    provider = PopupNavigationProvider()
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
        vision_provider=provider,
    )

    completed = await worker.run(
        BrowserRunRequest(
            job_id="job-popup-navigation",
            task_id="task-popup-navigation",
            target_url=f"{target_server}/popup-navigation",
            fields={},
            target_intent="找到目标业务入口并进入",
            observation_interval_seconds=1,
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert completed.status is BrowserJobStatus.COMPLETED
    assert completed.current_url == f"{target_server}/popup-destination"
    assert completed.statistics is not None
    assert completed.statistics.screenshot_count == 2
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_visual_worker_adopts_delayed_newest_tab_before_next_screenshot(
    target_server: str,
    tmp_path: Path,
) -> None:
    provider = DelayedPopupNavigationProvider()
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
        vision_provider=provider,
    )

    completed = await worker.run(
        BrowserRunRequest(
            job_id="job-delayed-popup-navigation",
            task_id="task-delayed-popup-navigation",
            target_url=f"{target_server}/delayed-popup-navigation",
            fields={},
            target_intent="点击办事指南并进入最新打开的业务页面",
            observation_interval_seconds=1,
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert completed.status is BrowserJobStatus.COMPLETED
    assert completed.current_url == f"{target_server}/delayed-popup-destination"
    assert completed.statistics is not None
    assert completed.statistics.screenshot_count == 2
    assert provider.observed_urls == [
        f"{target_server}/delayed-popup-navigation",
        f"{target_server}/delayed-popup-destination",
    ]


@pytest.mark.asyncio
async def test_visual_worker_rejects_unapproved_new_tab_before_clicking(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
        vision_provider=UnapprovedPopupProvider(),
    )

    paused = await worker.run(
        BrowserRunRequest(
            job_id="job-unapproved-popup",
            task_id="task-unapproved-popup",
            target_url=f"{target_server}/unapproved-popup",
            fields={},
            target_intent="找到外部业务入口并进入",
            observation_interval_seconds=1,
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert paused.status is BrowserJobStatus.NEED_HUMAN
    assert "未授权跳转" in paused.message
    assert len(worker._sessions["job-unapproved-popup"].context.pages) == 1
    await worker.cancel("job-unapproved-popup")


@pytest.mark.asyncio
async def test_visual_worker_completes_after_repeated_no_progress_searches(
    target_server: str,
    tmp_path: Path,
) -> None:
    provider = NoProgressProvider()
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
        vision_provider=provider,
    )

    completed = await worker.run(
        BrowserRunRequest(
            job_id="job-no-progress",
            task_id="task-no-progress",
            target_url=f"{target_server}/no-progress",
            fields={},
            target_intent="点击无响应入口",
            observation_interval_seconds=1,
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert completed.status is BrowserJobStatus.COMPLETED
    assert "完成结果截图" in completed.message
    assert provider.calls == 1
    assert completed.statistics is not None
    assert completed.statistics.screenshot_count == 2
    assert completed.statistics.click_count == 1


@pytest.mark.asyncio
async def test_visual_worker_advances_from_selected_category_to_target_row_guide(
    target_server: str,
    tmp_path: Path,
) -> None:
    provider = RepeatedEducationProvider()
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
        vision_provider=provider,
    )

    completed = await worker.run(
        BrowserRunRequest(
            job_id="job-education-guide",
            task_id="task-education-guide",
            target_url=f"{target_server}/education-services",
            fields={},
            target_intent=(
                "找到个人服务的学习教育板块的机动车驾驶员培训备案,"
                "点击办事指南"
            ),
            observation_interval_seconds=1,
        ),
        lambda _progress: _completed_awaitable(),
    )

    assert completed.status is BrowserJobStatus.COMPLETED
    assert completed.current_url == f"{target_server}/education-guide"
    assert completed.statistics is not None
    assert completed.statistics.click_count == 2
    assert provider.calls == 2
    assert any(
        "Last successful click (already completed): 学习教育" in task
        for task in provider.tasks
    )
    assert any(
        "advance with its requested row-level action such as 办事指南" in task
        for task in provider.tasks
    )


@pytest.mark.asyncio
async def test_visual_service_portal_finds_target_button_and_reports_statistics(
    target_server: str,
    tmp_path: Path,
) -> None:
    provider = ServicePortalProvider()
    progress_events: list[JobProgress] = []

    async def record_progress(progress: JobProgress) -> None:
        progress_events.append(progress)

    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
        vision_provider=provider,
    )

    completed = await worker.run(
        BrowserRunRequest(
            job_id="job-service-portal",
            task_id="task-service-portal",
            target_url=f"{target_server}/service-portal",
            fields={},
            target_intent="找到社会保障卡应用状态查询入口并点击",
            observation_interval_seconds=1,
        ),
        record_progress,
    )

    assert completed.status is BrowserJobStatus.COMPLETED
    assert completed.current_url == f"{target_server}/service-login"
    assert "完成结果截图" in completed.message
    assert isinstance(completed.statistics, ExecutionStatistics)
    assert completed.statistics.screenshot_count == 5
    assert completed.statistics.model_call_count == 3
    assert completed.statistics.browser_action_count == 3
    assert completed.statistics.click_count == 3
    assert completed.statistics.model_latency_ms >= 0
    assert completed.statistics.duration_ms >= 4_000
    assert all("社会保障卡应用状态查询" in task for task in provider.tasks)
    assert "Recent actions (newest first):" in provider.tasks[-1]
    assert "社会保障卡" in provider.tasks[-1]
    entering_messages = [
        progress.message
        for progress in progress_events
        if progress.status is BrowserJobStatus.ENTERING
    ]
    assert entering_messages[0].startswith("视觉模型已定位并执行 确认")
    assert "高置信自动执行 1.00" in entering_messages[0]
    assert any(
        "社会保障卡" in progress.message
        for progress in progress_events
        if progress.status is BrowserJobStatus.ENTERING
    )


@pytest.mark.asyncio
async def test_visual_scenario_navigates_menus_requests_missing_data_and_resumes(
    target_server: str,
    tmp_path: Path,
) -> None:
    provider = PropertyScenarioProvider()
    reports: list[JobProgress] = []

    async def report(progress: JobProgress) -> None:
        reports.append(progress)

    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
        vision_provider=provider,
    )
    paused = await worker.run(
        BrowserRunRequest(
            job_id="job-property",
            task_id="task-property",
            target_url=f"{target_server}/property-portal",
            fields={
                "account.username": "demo-user",
                "property.ownerName": "张三",
                "property.ownerOccupied": "true",
            },
            field_definitions=[
                FieldDefinition(
                    key="account.username",
                    display_name="用户名",
                    aliases=["用户名"],
                ),
                FieldDefinition(
                    key="property.ownerName",
                    display_name="产权人姓名",
                    aliases=["产权人姓名"],
                ),
                FieldDefinition(
                    key="property.ownerOccupied",
                    display_name="本人居住",
                    aliases=["本人居住"],
                ),
            ],
            target_intent="找到填写房产认证信息的入口并填写资料",
            authentication_mode=AuthenticationMode.LOGIN,
            observation_interval_seconds=1,
            keep_browser_open=True,
        ),
        report,
    )

    assert paused.status is BrowserJobStatus.NEED_HUMAN
    assert paused.intervention is not None
    assert paused.intervention.kind is InterventionKind.DATA_REQUIRED
    assert paused.intervention.missing_fields[0].display_name == "登录密码"

    property_paused = await worker.resume(
        "job-property",
        HumanResolution(
            field_values={"account.password": "temporary-password"}
        ),
        report,
    )

    assert property_paused.status is BrowserJobStatus.NEED_HUMAN
    assert property_paused.intervention is not None
    assert property_paused.intervention.kind is InterventionKind.DATA_REQUIRED
    assert property_paused.intervention.missing_fields[0].display_name == "房产证号"

    completed = await worker.resume(
        "job-property",
        HumanResolution(
            field_values={"property.certificateNumber": "沪房权证123456"}
        ),
        report,
    )

    assert completed.status is BrowserJobStatus.COMPLETED
    assert completed.browser_session_open is True
    assert completed.completed_fields == 5
    assert all("房产认证" in task for task in provider.tasks)
    assert len(provider.tasks) >= 7
    await worker.cancel("job-property")


@pytest.mark.asyncio
async def test_manual_login_session_heartbeats_and_is_reused_by_next_job(
    target_server: str,
    tmp_path: Path,
) -> None:
    worker = PlaywrightBrowserWorker(
        secret_store=InMemorySecretStore(),
        allowed_origins={target_server},
        artifacts_root=tmp_path,
        headless=True,
        vision_provider=ManualSessionProvider(),
    )

    def request(job_id: str) -> BrowserRunRequest:
        return BrowserRunRequest(
            job_id=job_id,
            task_id=f"task-{job_id}",
            target_url=f"{target_server}/manual-login",
            fields={},
            target_intent="进入认证业务中心",
            authentication_mode=AuthenticationMode.MANUAL,
            authentication_session_key="mobile-account",
            heartbeat_url=f"{target_server}/session/heartbeat",
            heartbeat_interval_seconds=30,
        )

    async def report(_: JobProgress) -> None:
        return None

    paused = await worker.run(request("job-manual-1"), report)
    assert paused.status is BrowserJobStatus.NEED_HUMAN
    assert paused.intervention is not None
    assert paused.intervention.kind is InterventionKind.MANUAL_LOGIN

    page = worker._sessions["job-manual-1"].page
    await page.get_by_role("button", name="模拟完成手机验证").click()
    await page.get_by_role("heading", name="认证业务中心").wait_for()
    completed = await worker.resume(
        "job-manual-1",
        HumanResolution(manual_login_completed=True),
        report,
    )
    assert completed.status is BrowserJobStatus.COMPLETED
    assert completed.browser_session_open is True

    await worker._heartbeat_once("mobile-account")
    assert TargetHandler.heartbeat_requests >= 1

    reused = await worker.run(request("job-manual-2"), report)
    assert reused.status is BrowserJobStatus.COMPLETED
    assert reused.browser_session_open is True
    assert "job-manual-1" not in worker._sessions

    retained = worker._authentication_sessions["mobile-account"]
    await retained.context.clear_cookies()
    expired = await worker.run(request("job-manual-3"), report)
    assert expired.status is BrowserJobStatus.NEED_HUMAN
    assert expired.intervention is not None
    assert expired.intervention.kind is InterventionKind.MANUAL_LOGIN
    await worker.cancel("job-manual-3")
