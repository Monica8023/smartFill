# SmartFill 开发与运行指南

## 1. 当前可运行范围

第一版代码已经形成一个纵向切片：

- FastAPI 控制面和任务状态机。
- CSV/XLSX 导入、字段映射、基础校验与脱敏预览。
- 开发用密钥引用存储，模型和任务对象不持有密码/证件明文。
- 目标 origin 白名单、动作白名单、置信度阈值和提交人工确认。
- 浏览器驱动插件协议与安全执行器。
- 阿里云百炼 Qwen-VL 的 OpenAI 兼容 API 适配器。

当前尚未包含真实 Playwright/CDP Worker、PostgreSQL、Redis、Web 操作台和生产级
Vault。这些组件可以在现有协议边界上继续实现，不需要改动模型输出或任务 API。

## 2. 本机启动

要求：Python 3.12、uv 0.11 或兼容版本。

```powershell
cd D:\PythonProject\smartFill
Copy-Item .env.example .env
# 编辑 .env：设置随机 API token，并填写实际允许的网站 origin
uv sync --extra dev
uv run smartfill
```

访问：

- 健康检查：`http://127.0.0.1:8000/api/v1/health`
- OpenAPI：`http://127.0.0.1:8000/api/docs`
- 产品原型：直接打开 `prototype/index.html`

除健康检查外，API 在配置 `SMARTFILL_API_TOKEN` 后需要请求头：

```text
Authorization: Bearer <SMARTFILL_API_TOKEN>
```

## 3. 运行质量检查

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pip-audit
```

测试要求覆盖率不低于 80%。

## 4. Docker 启动

```powershell
Copy-Item .env.example .env
# 修改 .env 后再启动
docker compose up --build
```

容器默认只绑定到宿主机 `127.0.0.1:8000`，使用非 root 用户、只读根文件系统、
删除全部 Linux capabilities，并启用健康检查。该 Compose 文件面向本地开发或受控试点，
生产环境仍应按部署方案拆分 API、队列、数据库、对象存储和 Browser Worker。

## 5. API 最短流程

创建任务：

```powershell
$headers = @{ Authorization = "Bearer <token>" }
$body = @{
  name = "批量完善用户资料"
  target_origin = "https://target.example.com"
  record_count = 10
} | ConvertTo-Json

$task = Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/tasks `
  -Headers $headers `
  -ContentType application/json `
  -Body $body
```

校验并启动：

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/tasks/$($task.id)/validate" -Headers $headers
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/tasks/$($task.id)/start" -Headers $headers
```

任务创建时只接受 `SMARTFILL_ALLOWED_TARGET_ORIGINS` 中的精确 HTTPS origin。

## 6. 百炼视觉接入

填写以下环境变量：

```dotenv
SMARTFILL_DASHSCOPE_API_KEY=<your-key>
SMARTFILL_DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
SMARTFILL_DASHSCOPE_FAST_MODEL=qwen3-vl-flash
SMARTFILL_DASHSCOPE_STRONG_MODEL=qwen3-vl-plus
```

企业业务空间应将 Base URL 替换为控制台提供的专属 API Host。视觉请求只包含脱敏截图、
元素语义摘要和统一字段名；模型输出必须通过 Pydantic 生成的 JSON Schema 校验。

## 7. 下一步实现顺序

1. 实现 `BrowserDriver` 的 Playwright/CDP 适配器和本地测试页面。
2. 增加 DOM/ARIA 快照、稳定元素 ID 和逐字段回读验证。
3. 增加弹窗分类器、验证码人工接管和检查点恢复。
4. 将内存任务库替换为 PostgreSQL，将任务调度接入 Redis。
5. 将内存密钥库替换为 Vault/KMS，并单独部署 Browser Worker。

