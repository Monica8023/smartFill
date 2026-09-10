# SmartFill

SmartFill 是一个面向批量资料录入场景的视觉浏览器代理。页面截图由多模态模型负责理解，浏览器执行器只负责对当次观察中已落地的目标执行受约束动作、回读验证和异常恢复。

当前仓库已经包含产品设计，以及可运行的 Web 操作台、FastAPI 控制面和
Playwright/CDP Browser Worker：

- [产品需求文档](docs/01-product-requirements.md)
- [第一版对接与操作文档](docs/02-v1-integration-and-operation.md)
- [产品原型说明](docs/03-product-prototype.md)
- [部署方案](docs/04-deployment-plan.md)
- [开发与运行指南](docs/05-development-guide.md)
- [系统架构与实现进度](docs/06-system-architecture-and-progress.md)
- [原生客户端与视觉执行迁移路线图](plans/native-client-visual-agent-roadmap.md)
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
- 浏览器会为每次视觉观察生成短期目标 ID；ID 只用于动作落地，不承担页面语义理解。
- 视觉低置信度、验证码或 MFA 暂停后保留浏览器会话，支持人工处理页面后恢复或终止。
- Browser Job 支持任务级业务数据 Schema；可在 Web 中新增字段、配置数据类型、敏感标记和
  来源字段，不需要修改前后端固定字段代码。
- 提交按钮由视觉模型识别，只允许点击当前页面观察中的候选按钮；每个任务
  最多尝试提交一次，歧义时自动转人工确认。
- 双模型视觉 POC 支持从官网首页规划登录或注册入口，并在每次导航、弹窗和页面变化后重新截图决策。
- 生产 Browser Worker 已接入目标驱动视觉循环：操作员选择“不登录 / 登录 / 注册”，输入自然语言业务目标，
  Worker 默认每 5 秒重新截图并让模型规划菜单或子菜单动作，直到目标表单可见。
- 视觉模型会把登录、注册和目标表单的可见必填项与客户预设资料匹配；缺少资料时返回准确字段清单，
  控制台补齐后沿用同一 Browser Context、Cookie 和当前页面继续执行。
- 手机号、邮箱验证码等登录可选择“人工登录并复用会话”：首次由用户在真实浏览器完成验证，
  后续任务用会话标识复用内存中的 Browser Context；同源只读心跳负责续期和失效检测。
- 已移除页面字段扫描接口、页面别名配置和人工 DOM 字段候选映射。
- 支持 1～10 个表单步骤组成的顺序工作流；登录、提交登录表单、进入资料页和填写资料在
  同一个 Browser Context 中执行，Cookie、Session 和登录态不会在步骤间丢失。
- MySQL 持久化业务任务、Browser Job 配置快照及追加式执行时间线；Web 提供任务列表和
  详情页，服务重启后仍可查看历史；任务行可一键复用并回填执行控制台。
- 目标网页 Origin 白名单保存于 `system_settings`，可在“系统设置”页面编辑并立即生效，
  Worker 路由拦截和视觉执行每次操作都会读取最新值。
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

首次联调可以保留默认目标地址 `http://127.0.0.1:8000/demo/target`，填写“目标任务描述”，
再选择是否登录或注册；客户资料允许暂时不完整。点击“启动浏览器填写”后，Web 操作台会实时显示
视觉观察次数、字段进度、事件时间线和脱敏截图。生产视觉执行必须配置
`SMARTFILL_DASHSCOPE_API_KEY`；未配置时会返回明确的配置错误。提交策略可以选择：

- `仅填写，不点击提交`：默认策略，只填值和回读验证。
- `填写后人工确认提交`：填完后展示当前页面识别到的按钮，操作员确认后只点击一次。
- `唯一匹配时自动提交`：视觉模型识别高置信按钮；无法安全落地时转人工确认。

需要填写不同网站的额外业务数据时，点击“添加自定义字段”，再打开“配置数据字段”。新增字段
右侧提供“删除字段”入口，数据配置中也可以删除：

- “字段标识”使用 `person.firstName`、`person.city` 这样的点分标识。
- 密码、证件号码等必须勾选“敏感字段”，进入 Worker 前会转换为 secret reference。
- “来源字段”用于派生重复值，例如 `account.passwordConfirmation` 可以引用
  `account.password`，API 请求无需再次携带确认密码明文。

动态 Schema 和非敏感字段值会进入工作流配置快照，可在任务列表一键回填，也可在“用户导入”
页面跨批次复用。密码、证件号、手机号等敏感字段值不会进入快照，复用时必须重新填写。

### 自动从入口页进入目标表单

1. 将目标地址填写为官网首页，例如 `http://127.0.0.1:8000/demo/home`。
2. 在“账户流程”选择“不处理登录或注册”“登录已有账户”“注册新账户”或
   “人工登录并复用会话”。
3. 在“目标任务描述”输入业务结果，例如“找到填写房产认证信息的入口并填写资料”。
4. 配置已知客户资料；不必事先知道目标网站的字段名或菜单层级。
5. Worker 首次立即截图，随后按“截图理解频率”（默认 5 秒）重新观察；每次只执行当前截图中
   由模型引用的一个菜单动作或一组字段匹配，页面变化后旧元素 ID 立即失效。
6. 登录、注册或最终业务表单出现但资料不全时，任务进入“缺少资料”；控制台列出字段，补齐后
   从原浏览器页面继续。低置信度、验证码、MFA 或遮挡同样保留会话并转人工处理。
7. 截图出现“确认/重新选择”地区提示时默认确认当前地区，再继续识别当地业务目录。对于只要求
   “找到并点击入口”的任务，目标入口点击后若跳到登录墙，系统直接判定查找完成，不再操作登录页。

### 人工登录与会话心跳

短信、邮箱验证码、扫码或企业 SSO 场景选择“人工登录并复用会话”，并配置：

- “登录会话标识”：区分站点和账号，例如 `property-account-a`；任务复用时会一起回填。
- “登录心跳 URL”：必须与目标页面同源且不能携带查询参数或片段；应是业务方确认安全、只读、能运行站点续期逻辑的页面地址，避免把令牌写入配置或浏览历史。
- “心跳间隔”：默认 300 秒，可配置 30～3600 秒。

第一次执行会保留真实浏览器并等待用户完成验证。点击“我已完成登录，继续执行”后，Worker
继续当前目标；任务完成后 Browser Context 留在进程内并按心跳保活。下一个使用相同会话标识的
任务会先在同一 Context 的独立页加载心跳 URL，让站点自身的 Cookie/Local Storage 刷新逻辑运行，
再接管原登录态。心跳出现网络错误、跨 Origin 跳转或非 2xx 响应时，
会话会被判为失效，下一次任务重新进入人工登录门禁。Cookie/Token 不写入任务数据库或诊断日志。

当前实现复用的是客户端进程内的浏览器会话；服务、桌面客户端或浏览器进程重启后需要重新登录。
未来桌面客户端若要跨重启恢复，应接入操作系统密钥库加密的浏览器 Profile，而不是把 Cookie 明文
写入工作流快照。

系统不接受 CSS、XPath、页面字段别名或脚本作为业务配置。表单入口跳到其他域名或子域名时，目标 Origin 也必须加入
系统设置中的白名单，否则 Worker 会阻止跳转。

### 配置登录后继续填写的工作流

1. 在执行控制台配置“登录”步骤的 URL、账号密码字段，并把提交策略设为确认后提交或自动提交。
2. 点击“保存当前步骤并添加下一步”。
3. 配置“完善资料”步骤的资料页 URL 和需要填写的业务数据。
4. 点击“启动浏览器填写”。后端按顺序执行全部步骤，并在任务详情保存脱敏配置快照。
5. 从左侧“任务列表”查看状态、步骤、字段进度和完整执行时间线；点击某一任务的“复用”会
   直接返回控制台并回填全部步骤，可逐步编辑后再次执行。
6. 控制台任务成功后会保留真实目标浏览器，便于检查完整页面和操作结果；检查完毕后点击
   时间线结果区的“关闭目标浏览器”释放会话。批量任务仍在每条记录完成后自动关闭浏览器。

每个步骤都有独立的 URL、字段 Schema、入口策略和提交策略；跨 Origin 步骤必须全部在
动态白名单内。敏感值仅在本次运行的 SecretStore 中保存，配置快照不会记录敏感字段值；复用
任务时这些输入保持为空并显示重填提示。

### 双模型视觉 POC

配置 `SMARTFILL_DASHSCOPE_API_KEY` 后，可直接运行 ExpandTesting Notes 的真实截图优先
评测。模型负责页面语义与动作规划，Playwright 只执行当前截图中已落地且通过策略门禁的动作：

```powershell
uv run smartfill-vision-poc --models qwen3-vl-flash qwen3-vl-plus --trials 3
```

加上 `--headed` 可观察真实 Chromium。每轮结果写入 `artifacts/vision-poc/<UTC时间>/`，
包含脱敏步骤截图、`trace.json`、`result.json` 和汇总 `summary.json`。密码不会进入模型上下文
或 trace；成功必须由页面上可见的唯一标题、描述和分类共同确认。POC 的固定协议和实测结果见
`.Codex/evals/dual-model-vision-poc.md` 与
`.Codex/evals/dual-model-vision-poc-results.md`。

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

非无头模式下，执行控制台会在任务成功后继续保留该 Chromium 窗口，直到操作员点击
“关闭目标浏览器”或服务停止；无头模式虽然也可保留会话，但只能通过预览图检查结果。

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
- Browser Worker 失败时，任务详情会显示诊断 ID 和“下载诊断日志”入口；原始文件位于
  `artifacts/jobs/<job-id>/diagnostic-<diagnostic-id>.log`。日志包含失败阶段、步骤、URL
  和异常堆栈，不记录任务字段值。
- 任务结束后结果区展示耗时、截图次数、模型调用次数和浏览器动作次数，并可下载
  `artifacts/jobs/<job-id>/execution-statistics.log` 查看模型耗时及点击、滚动、等待、填写明细。
- 目标地址返回 `403` 时，把目标 Origin（仅协议、域名和端口）加入
  Web 的“系统设置 → 目标网页白名单”；保存后无需重启。

详细步骤见
[开发与运行指南](docs/05-development-guide.md)。

## 第一版建议技术路线

- 浏览器控制：Chromium + CDP，执行层预留 Stagehand/Playwright 适配器
- 页面理解：截图视觉模型负责语义判断，DOM/Accessibility 仅为截图中的可交互目标生成短期 ID
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
