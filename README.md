# SmartFill

SmartFill 是一个面向批量资料录入场景的 AI 浏览器自动化产品。它通过 DOM/无障碍树理解页面结构，在结构信息不足时调用视觉模型识别字段、弹窗和操作目标，再由受约束的浏览器执行器完成填写、验证和异常恢复。

当前仓库已经包含产品设计和可运行的第一版后端纵向切片：

- [产品需求文档](docs/01-product-requirements.md)
- [第一版对接与操作文档](docs/02-v1-integration-and-operation.md)
- [产品原型说明](docs/03-product-prototype.md)
- [部署方案](docs/04-deployment-plan.md)
- [开发与运行指南](docs/05-development-guide.md)
- [可交互产品原型](prototype/index.html)

## 已实现

- FastAPI 任务 API 与受约束的状态流转。
- CSV/XLSX 批量导入、统一字段映射、脱敏预览。
- 密码、身份证号、手机号的 opaque secret reference。
- 目标域名和动作白名单、低置信度转人工、提交人工确认。
- 广告/Cookie、验证码、MFA、登录失效、限流和跨域跳转的异常策略。
- 可插拔浏览器执行器协议与阿里云百炼 Qwen-VL 适配器。
- API Token、上传限制、请求限速、安全响应头和生产配置校验。
- 单元/集成测试、Ruff、Mypy、覆盖率和依赖审计配置。

## 快速启动

```powershell
cd D:\PythonProject\smartFill
Copy-Item .env.example .env
# 修改 .env 中的 token 和允许访问的目标 origin
uv sync --extra dev
uv run smartfill
```

API 文档：`http://127.0.0.1:8000/api/docs`。详细步骤见
[开发与运行指南](docs/05-development-guide.md)。

## 第一版建议技术路线

- 浏览器控制：Chromium + CDP，执行层预留 Stagehand/Playwright 适配器
- 页面理解：DOM/Accessibility Tree 优先，截图视觉理解兜底
- 国内视觉 API：阿里云百炼 `qwen3-vl-flash` + `qwen3-vl-plus`
- 后端建议：Python 3.12、FastAPI、PostgreSQL、Redis
- 前端建议：React、TypeScript、Vite
- 敏感数据：密钥服务或系统凭据库，模型仅接收字段标识和脱敏截图

## 产品原则

1. 不依赖固定坐标、XPath 或录制鼠标轨迹。
2. 模型负责理解和推荐，执行器负责校验和执行。
3. 密码、身份证号等敏感值不进入模型上下文。
4. 填写后必须回读验证，提交动作由策略或人工确认控制。
5. 页面变化、广告、弹窗和登录失效通过状态机与异常处理器恢复。
