"""Local semantic form used to verify the browser worker end to end."""

DEMO_TARGET_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SmartFill 联调表单</title>
  <style>
    body{font-family:system-ui,"Microsoft YaHei",sans-serif;background:#edf2ee;
      color:#17352b;margin:0;padding:48px}
    main{max-width:760px;margin:auto;background:white;border:1px solid #d5ded8;
      border-radius:18px;padding:32px}
    h1{margin-top:0} form{display:grid;grid-template-columns:1fr 1fr;gap:16px}
    label{display:grid;gap:6px;font-size:13px}.wide{grid-column:1/-1}
    input,select,textarea{font:inherit;padding:10px;border:1px solid #bdc9c1;border-radius:8px}
    iframe{width:100%;min-height:118px;border:1px solid #d5ded8;border-radius:10px}
    button{padding:10px 16px;border:0;border-radius:8px;background:#176c4e;color:white}
    dialog{border:0;border-radius:14px;box-shadow:0 18px 70px #0004;max-width:340px}
  </style>
</head>
<body>
  <main>
    <p>DEVELOPMENT TARGET</p>
    <h1>用户资料</h1>
    <form autocomplete="off" action="/demo/submitted" method="get">
      <label>用户名<input name="username" autocomplete="username"></label>
      <label>登录密码<input name="password" type="password" autocomplete="current-password"></label>
      <label>姓名<input name="display_name" autocomplete="name"></label>
      <label>性别<select name="gender">
        <option value="">请选择</option><option value="male">男</option>
        <option value="female">女</option>
      </select></label>
      <div class="wide">
        <iframe title="身份资料" src="/demo/frame"></iframe>
      </div>
      <section class="wide" id="contact-widget" aria-label="联系方式组件"></section>
      <div class="wide"><button type="submit">保存资料</button></div>
    </form>
  </main>
  <dialog open aria-label="活动广告">
    <h2>欢迎体验新功能</h2>
    <p>这是用于验证意外弹窗处理能力的联调广告。</p>
    <form method="dialog"><button value="close">关闭</button></form>
  </dialog>
  <script src="/demo/shadow.js"></script>
</body>
</html>"""

DEMO_SUBMITTED_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;background:#edf2ee;padding:48px}
main{max-width:620px;margin:auto;background:white;padding:32px;border-radius:16px}
</style></head><body><main>
<p>DEVELOPMENT TARGET</p><h1>演示资料已提交</h1>
<p>此页面仅用于验证 SmartFill 的提交策略, 不保存任何表单数据。</p>
</main></body></html>"""

DEMO_HOME_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;background:#edf2ee;padding:48px}
main{max-width:720px;margin:auto;background:white;padding:32px;border-radius:16px}
nav{display:flex;justify-content:flex-end}a{color:#176c4e;font-weight:700}
</style></head><body><main>
<nav><a href="/demo/login">登录 / Login</a></nav>
<p>DEVELOPMENT TARGET</p><h1>示例官网首页</h1>
<p>此页面不包含登录表单, 需要先进入登录页。</p>
</main></body></html>"""

DEMO_LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;background:#edf2ee;padding:48px}
main{max-width:520px;margin:auto;background:white;padding:32px;border-radius:16px}
form,label{display:grid;gap:10px}form{gap:18px}input,button{font:inherit;padding:11px}
</style></head><body><main>
<p>DEVELOPMENT TARGET</p><h1>用户登录</h1>
<form action="/demo/login-success" method="post">
<label>用户名<input name="username" autocomplete="username"></label>
<label>密码<input name="password" type="password" autocomplete="current-password"></label>
<button type="submit">登录</button>
</form>
</main></body></html>"""

DEMO_LOGIN_SUCCESS_HTML = """<!doctype html>
<html lang="zh-CN"><body><h1>演示登录提交完成</h1></body></html>"""

DEMO_FRAME_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;margin:0;padding:12px;
display:grid;grid-template-columns:1fr 1fr;gap:14px}label{display:grid;gap:6px;font-size:13px}
input{font:inherit;padding:10px;border:1px solid #bdc9c1;border-radius:8px}
</style></head><body>
<label>身份证号码<input name="id_number"></label>
<label>手机号码<input name="mobile" type="tel" autocomplete="tel"></label>
</body></html>"""

DEMO_SHADOW_JS = """const host = document.querySelector('#contact-widget');
const root = host.attachShadow({mode: 'open'});
root.innerHTML = `
  <style>
    div{display:grid;grid-template-columns:1fr 1fr;gap:14px}
    label{display:grid;gap:6px;font:13px system-ui,"Microsoft YaHei",sans-serif}
    input,textarea{font:inherit;padding:10px;border:1px solid #bdc9c1;border-radius:8px}
  </style>
  <div>
    <label>电子邮箱<input name="email" type="email" autocomplete="email"></label>
    <label>联系地址<textarea name="address" autocomplete="street-address"></textarea></label>
  </div>`;
"""

DEMO_AMBIGUOUS_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;background:#edf2ee;padding:48px}
main{max-width:620px;margin:auto;background:white;padding:28px;border-radius:16px}
label{display:grid;gap:7px;margin:14px 0}input{font:inherit;padding:10px;border:1px solid #bdc9c1}
</style></head><body><main>
<h1>字段歧义人工确认演示</h1>
<label>姓名<input name="primary_name"></label>
<label>姓名<input name="secondary_name"></label>
</main></body></html>"""
