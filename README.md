# Atoms Demo

Atoms Demo 是一个用自然语言创建和迭代网页应用的实验性工作台。用户可以在同一项目里提出需求、查看生成任务、操作预览、阅读和编辑源码、导出应用，并在历史版本之间切换。平台数据使用 SQLite 保存；生成应用的预览运行在独立来源，避免直接共享工作台会话。

> 当前状态（2026-10-22）：核心流程已在私有测试环境中验证，**尚无可供独立评审完整体验的公开链接**。公开工作台入口的登录与项目读取已测，但生成应用的预览地址仍依赖私有环境，不能作为已完成的公网 Demo。

## 能做什么

- **项目与生成**：创建、查找、收藏项目；通过对话生成应用并继续修改；查看真实任务状态与失败原因。
- **成果检查**：在隔离来源预览应用，查看源码与历史版本，恢复旧版，导出源码 ZIP；支持桌面、平板、手机宽度预览。
- **平台数据**：保存对话、版本、生成应用的业务记录，以及工作区、成员、资料、存储等数据。
- **其他界面**：发现与模板、公开作品、发布设置、积分与账本、连接器等已有界面和后端契约；外部服务的可用性取决于实际配置。

私有环境已验证真实模型生成、连续修改、构建预览、版本恢复及业务数据跨版本和服务重启保留。复杂组件的可视化编辑、真实邮件和验证码、OAuth、支付、公开 HTTPS 部署及完整公网预览仍未完成。未配置模型或 Docker 构建器时，页面可以启动，但生成任务不可用。

## 本地启动

需要 Python 3.11+、Node.js 22+ 和 npm。下面的 PowerShell 示例把私有测试数据放在仓库外；本地邮箱验证链接写入 outbox，不会发送邮件。`.env.example` 是配置参考，程序**不会自动加载**它。

### 1. 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
cd frontend
npm ci
cd ..
```

### 2. 启动 API 与预览服务

在仓库根目录打开一个 PowerShell：

```powershell
$dataDir = Join-Path $env:LOCALAPPDATA 'AtomsDemoData'
New-Item -ItemType Directory -Force $dataDir | Out-Null
$dataPath = $dataDir.Replace('\', '/')
$env:ATOMS_DATABASE_URL = "sqlite:///$dataPath/platform.db"
$env:ATOMS_BLOB_DIRECTORY = "$dataDir/blobs"
$env:ATOMS_PUBLIC_ORIGIN = 'http://app.preview.localhost:5173'
$env:ATOMS_PREVIEW_ORIGIN = 'http://preview.localhost:8001'
$env:ATOMS_PRIVATE_TEST_MODE = 'true'
$env:ATOMS_PRIVATE_MAIL_OUTBOX = "$dataDir/private-mail/outbox.jsonl"
.\.venv\Scripts\python main.py
```

API 监听 `127.0.0.1:8000`，预览服务监听 `127.0.0.1:8001`。可以用 `http://127.0.0.1:8000/healthz` 检查 API。

### 3. 启动前端

在仓库根目录另开一个 PowerShell：

```powershell
cd frontend
npm run dev
```

打开 [http://app.preview.localhost:5173](http://app.preview.localhost:5173)。注册后，私有测试模式下的验证链接记录在 `$env:LOCALAPPDATA\AtomsDemoData\private-mail\outbox.jsonl`；取最新一条 `verification` 记录的 `url` 在同一浏览器打开。该文件含一次性链接，请勿提交或分享。

### 4. 启用真实生成（可选）

在启动 API **之前**，准备本机可用的 Docker Engine、模型服务密钥，并在第 2 步的 PowerShell 中设置：

```powershell
docker build -f build-runtime/Dockerfile -t atoms-react-vite:v1 .
$env:ATOMS_BUILD_BACKEND = 'docker'
$env:ATOMS_MODEL_PROVIDER = 'aliyun_token_plan'
$env:ATOMS_MODEL_NAME = 'qwen3.7-plus'
$env:ATOMS_MODEL_SECRET_NAME = 'OPS_TOKEN_PLAN_API_KEY'
$env:OPS_TOKEN_PLAN_API_KEY = '<your-key>'
```

然后运行 `python main.py`。密钥只放进本机进程环境，切勿写入提交文件。模型服务可能产生费用；可在 `.env.example` 中查看超时、额度和端口等配置项。

## 技术结构

```text
frontend/       React + TypeScript + Vite 工作台
atoms_demo/api/ HTTP API、任务事件与隔离预览服务
atoms_demo/workflows/ 生成、项目、预览和发布流程
atoms_demo/services/ 业务规则
atoms_demo/adapters/ SQLite、模型、Docker 构建及外部服务适配
build-runtime/  生成应用的固定 Vite 构建镜像
tests/          Python 测试；前端测试位于 frontend/src/
```

生成流程为「需求与当前版本 → 模型生成文件变更 → 校验并保存候选版本 → 隔离构建 → 成功后切换预览」。构建失败时保留最近的成功版本，并在配置允许时有限重试修复。工作台、生成应用及构建容器分别承担不同职责。

更详细的模块责任、接口与流程见 [DESIGN.md](DESIGN.md)。

## 验证

```powershell
python -m unittest discover -s tests
cd frontend
npm test
npm run build
```

本地测试覆盖服务契约和若干失败路径；真实模型调用、浏览器交互及公网访问需要分别在相应环境验收。仓库不包含私有测试数据库、部署记录、密钥和账号数据。
