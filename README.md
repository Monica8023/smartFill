# SmartFill

SmartFill 是一个面向批量资料录入场景的 AI 浏览器自动化产品。它通过 DOM/无障碍树理解页面结构，在结构信息不足时调用视觉模型识别字段、弹窗和操作目标，再由受约束的浏览器执行器完成填写、验证和异常恢复。

当前仓库已经包含产品设计，以及可运行的 Web 操作台、FastAPI 控制面和
Playwright/CDP Browser Worker：

- [产品需求文档](docs/01-product-requirements.md)
- [第一版对接与操作文档](docs/02-v1-integration-and-operation.md)
- [产品原型说明](docs/03-product-prototype.md)
- [部署方案](docs/04-deployment-plan.md)
- [开发与运行指南](docs/05-development-guide.md)
- [系统架构与实现进度](docs/06-system-architecture-and-progress.md)
- [可交互产品原型](prototype/index.html)

## 已实现

- FastAPI 任务 API 与受约束的状态流转。
- CSV/XLSX 批量导入、统一字段映射、脱敏预览。
- 密码、身份证号、手机号的 opaque secret reference。
- 目标域名和动作白名单、低置信度转人工，以及可选的仅填写、确认后提交、自动提交策略。
- 广告/Cookie、验证码、MFA、登录失效、限流和跨域跳转的异常策略。
- 可插拔浏览器执行器协议与阿里云百炼 Qwen-VL 适配器。
- API Token、上传限制、请求限速、安全响应头和生产配置校验。
- 单元/集成测试、Ruff、Mypy、覆盖率和依赖审计配置。
- React + TypeScript + Vite Web 操作台、WebSocket 实时进度和脱敏截图。
- 真实 Playwright Chromium Worker，以及连接已有 Chromium 的 CDP 模式。
- 按 frame 保存完整 ARIA Accessibility Tree，支持同源/白名单 iframe 和开放 Shadow DOM。
- 字段歧义、验证码或 MFA 暂停后保留浏览器会话，支持人工确认、重新扫描、恢复或终止。
- Browser Job 支持任务级动态字段 Schema；可在 Web 中新增字段、配置页面别名、数据类型、
  敏感标记和来源字段，不需要修改前后端固定字段代码。
- 提交按钮使用可访问名称和语义别名识别，只允许点击当前页面快照中的候选按钮；每个任务
  最多尝试提交一次，歧义时自动转人工确认。
- 支持从官网首页按“登录 / Login / Sign in”等语义进入登录页，并在跳转后重新扫描
  Accessibility Tree、iframe、开放 Shadow DOM 和表单字段。
- Web 操作台支持“扫描页面字段”，自动载入用户名、密码及未知自定义字段的 Schema；扫描
  只读取控件语义，不读取、猜测或返回账号密码值。
- 支持 1～10 个表单步骤组成的顺序工作流；登录、提交登录表单、进入资料页和填写资料在
  同一个 Browser Context 中执行，Cookie、Session 和登录态不会在步骤间丢失。
- MySQL 持久化业务任务、Browser Job 配置快照及追加式执行时间线；Web 提供任务列表和
  详情页，服务重启后仍可查看历史。
- 目标网页 Origin 白名单保存于 `system_settings`，可在“系统设置”页面编辑并立即生效，
  Worker 路由拦截和页面扫描每次操作都会读取最新值。
- 执行控制台的目标 Origin 从白名单下拉选择，页面路径单独配置，避免手工输入未授权站点。
- “用户导入”页面可上传 CSV/XLSX、选择已有工作流、映射数据列并顺序批量执行；批次、
  每行状态和对应 Browser Job 均持久化到 MySQL，导入字段值不写入批次表。

## 前端启动

前端源码位于 `apps/web`，需要 Node.js 20+ 和 npm。开发时分别启动后端和
Vite；Vite 会把 `/api`、WebSocket 和 `/demo` 请求代理到 `127.0.0.1:8000`。

### 1. 准备后端

```powershell
cd D:\PythonProject\smartFill
Copy-Item .env.example .env
# 修改 .env 中的 API Token 和允许访问的目标 Origin
uv sync --extra dev
uv run playwright install chromium
uv run alembic upgrade head
uv run smartfill
```

MySQL 连接通过被 Git 忽略的 `.env` 配置，示例：

```dotenv
SMARTFILL_DATABASE_URL=mysql+pymysql://smartfill:your-password@127.0.0.1:3306/smartfill
```

数据库需先创建；表结构始终由 `uv run alembic upgrade head` 建立或升级，不手工改表。

后端默认监听 `http://127.0.0.1:8000`。需要保持这个终端运行。

### 2. 启动前端开发服务器

打开另一个 PowerShell：

```powershell
cd D:\PythonProject\smartFill\apps\web
npm install
npm run dev
```

浏览器打开 `http://127.0.0.1:5173`。如果 `.env` 配置了
`SMARTFILL_API_TOKEN`，将同一个 Token 填入页面右上角的 `API Token` 输入框；
Token 只保存在当前页面内存，不写入 localStorage。

首次联调可以保留默认目标地址 `http://127.0.0.1:8000/demo/target`，填写至少一个
资料字段后点击“启动浏览器填写”。Web 操作台将实时显示任务状态、字段进度、事件时间线和
脱敏截图。提交策略可以选择：

- `仅填写，不点击提交`：默认策略，只填值和回读验证。
- `填写后人工确认提交`：填完后展示当前页面识别到的按钮，操作员确认后只点击一次。
- `唯一匹配时自动提交`：按“提交按钮别名”识别唯一高置信按钮；无法唯一匹配时转人工确认。

需要填写不同网站的额外字段时，点击“添加自定义字段”，再打开“配置字段映射”。新增字段
右侧提供“删除字段”入口，映射配置中也可以删除：

- “字段标识”使用 `person.firstName`、`person.city` 这样的点分标识。
- “页面别名”填写目标页可能出现的 label、ARIA 名称、placeholder 或 name，使用逗号分隔。
- 密码、证件号码等必须勾选“敏感字段”，进入 Worker 前会转换为 secret reference。
- “来源字段”用于派生重复值，例如 `account.passwordConfirmation` 可以引用
  `account.password`，API 请求无需再次携带确认密码明文。

动态 Schema 会进入不含字段值的工作流配置快照，可在“用户导入”页面跨批次复用。

### 从官网首页进入登录页并扫描字段

1. 将目标地址填写为官网首页，例如 `http://127.0.0.1:8000/demo/home`。
2. “进入表单方式”选择“先点击登录入口”。
3. 配置入口按钮别名，例如 `登录, Login, Sign in`。
4. 点击“扫描页面字段”。Worker 会点击唯一高置信入口，进入登录页并将发现的字段载入表单。
5. 检查字段名称和敏感标记，填写账号数据，再启动浏览器任务。

如果发现多个同分候选，正式执行时会暂停并让操作员从当前页面快照中选择；不接受 CSS、
XPath 或脚本。登录入口跳到其他域名或子域名时，目标 Origin 也必须加入
系统设置中的白名单，否则 Worker 会阻止跳转。

### 配置登录后继续填写的工作流

1. 在执行控制台配置“登录”步骤的 URL、账号密码字段，并把提交策略设为确认后提交或自动提交。
2. 点击“保存当前步骤并添加下一步”。
3. 配置“完善资料”步骤的资料页 URL，扫描并填写该页字段。
4. 点击“启动浏览器填写”。后端按顺序执行全部步骤，并在任务详情保存不含字段值的配置快照。
5. 从左侧“任务列表”查看状态、步骤、字段进度和完整执行时间线。

每个步骤都有独立的 URL、字段 Schema、入口策略和提交策略；跨 Origin 步骤必须全部在
动态白名单内。敏感值仅在本次运行的 SecretStore 中保存，配置快照只记录字段名和语义规则。

### 导入用户信息并批量执行

1. 先在“执行控制台”成功执行一次目标工作流，使其出现在“任务列表”中。
2. 打开左侧“用户导入”，选择已有任务工作流。
3. 上传 UTF-8 CSV 或 XLSX 文件；页面读取表头并显示记录数，不会在预览中暴露密码、
   身份证号和手机号明文。
4. 为工作流中的每个输入字段选择对应的数据列，然后点击“开始批量执行”。
5. 右侧查看批次总数、完成数和失败数；系统按文件行顺序创建独立 Browser Job。

批量任务默认串行执行，以限制本机 Chromium 数量和目标站点压力。若某条记录触发验证码、
MFA 或字段歧义，批次进入“需要人工处理”，并保留该条记录对应的 Browser Job 供审计。
单批默认最多 1000 条，可通过 `SMARTFILL_BATCH_MAX_RECORDS` 下调或调整（上限 10000）。

### 3. 生产构建与一体化启动

FastAPI 会从 `apps/web/dist` 提供构建后的前端。构建完成后只需运行后端：

```powershell
cd D:\PythonProject\smartFill\apps\web
npm ci
npm run build

cd D:\PythonProject\smartFill
uv run smartfill
```

此时统一入口为：

- Web 操作台：`http://127.0.0.1:8000/`
- API 文档：`http://127.0.0.1:8000/api/docs`
- 本地联调目标页：`http://127.0.0.1:8000/demo/target`
- 人工确认演示页：`http://127.0.0.1:8000/demo/ambiguous`
- 官网首页 → 登录页演示：`http://127.0.0.1:8000/demo/home`
- 健康检查：`http://127.0.0.1:8000/api/v1/health`

### 前端常用命令

```powershell
cd D:\PythonProject\smartFill\apps\web
npm run dev       # 开发服务器与热更新
npm test -- --run # 组件、HTTP 和 WebSocket 测试及覆盖率
npm run build     # TypeScript 检查和生产构建
```

### Browser Worker 配置

默认由 Playwright 启动隔离的 Chromium。需要观察浏览器实际操作时，在 `.env` 设置：

```dotenv
SMARTFILL_BROWSER_HEADLESS=false
```

需要接管已经用 `--remote-debugging-port=9222` 启动的 Chromium 时设置：

```dotenv
SMARTFILL_BROWSER_CDP_URL=http://127.0.0.1:9222
```

CDP 模式仅建议连接可信的本机或内网浏览器端点，禁止在 URL 中携带用户名和密码。

### 常见问题

- `127.0.0.1:8000/api/docs` 是接口文档，不是 Web 操作台；操作台地址是根路径 `/`。
- 根路径提示“SmartFill Web 尚未构建”时，执行 `cd apps/web` 和 `npm run build`，然后重启后端。
- 页面请求返回 `401` 时，检查 Web 操作台填写的 Token 是否与 `.env` 中
  `SMARTFILL_API_TOKEN` 一致；输入完成并移开焦点后会重新加载白名单。
- Worker 提示找不到 Chromium 时，执行 `uv run playwright install chromium`。
- 目标地址返回 `403` 时，把目标 Origin（仅协议、域名和端口）加入
  Web 的“系统设置 → 目标网页白名单”；保存后无需重启。

详细步骤见
[开发与运行指南](docs/05-development-guide.md)。

## 第一版建议技术路线

- 浏览器控制：Chromium + CDP，执行层预留 Stagehand/Playwright 适配器
- 页面理解：DOM/Accessibility Tree 优先，截图视觉理解兜底
- 国内视觉 API：阿里云百炼 `qwen3-vl-flash` + `qwen3-vl-plus`
- 后端：Python 3.12、FastAPI、MySQL 8、SQLAlchemy、Alembic
- 前端建议：React、TypeScript、Vite
- 敏感数据：密钥服务或系统凭据库，模型仅接收字段标识和脱敏截图

## 产品原则

1. 不依赖固定坐标、XPath 或录制鼠标轨迹。
2. 模型负责理解和推荐，执行器负责校验和执行。
3. 密码、身份证号等敏感值不进入模型上下文。
4. 填写后必须回读验证，提交动作由策略或人工确认控制。
5. 页面变化、广告、弹窗和登录失效通过状态机与异常处理器恢复。
