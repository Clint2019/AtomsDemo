# Atoms Demo

Atoms Demo 是一个用自然语言创建和迭代网页应用的实验性工作台。用户可以在同一项目里提出需求、查看生成任务、操作预览、阅读和编辑源码、导出应用，并在历史版本之间切换。平台数据使用 SQLite 保存；生成应用的预览运行在独立来源，避免直接共享工作台会话。

> 当前状态：公网 IP 的 HTTPS 工作台与隔离预览已开放。最新镜像 `demo-20261022-4` 修复了公网 IP 模式下生成应用的数据刷新后不可见的问题；同一平台账号在预览刷新、重新签票和重新登录后继续访问自己的应用记录。当前镜像已完成浏览器持久化与分享隔离回归；真实模型生成仍以先前镜像的验收记录为准。源码提交、运行镜像和验收范围见 [发布验收记录](RELEASE_VERIFICATION.md)。

## 在线体验

- [打开 Atoms Demo 工作台](https://8.137.197.104/)
- [阅读简要交付说明](DELIVERY_NOTE.md)
- 生成应用的预览和分享由工作台打开，使用同一公网 IP 的 HTTPS `18443` 端口；无需手工拼接票据链接。

评审使用交付文档中的测试账号登录并选择工作区后，可以新建项目，输入一个网页应用需求，等待任务完成，再打开预览操作生成的应用。回到同一项目继续提出修改需求，可在历史版本中查看或恢复结果；刷新工作台后，项目、对话和成功版本仍可找回。评审账号已完成预验证，不依赖评审自行接收验证邮件。

当前部署的注册验证码采用测试配置，验证链接只写入服务器受限 outbox，不会发送到邮箱，因此尚未开放自助注册。公网 HTTPS 入口使用短期 IP 证书并自动续期，供受控验收使用；正式对外服务仍需合适的域名、备案与服务端 API 凭据。

## 能做什么

- **项目与生成**：创建、查找、收藏项目；通过对话生成应用并继续修改；助手回复支持 Markdown 与 GitHub 风格表格、任务清单；查看真实任务状态与失败原因。
- **成果检查**：在隔离来源预览应用，查看源码与历史版本，恢复旧版，导出源码 ZIP；支持桌面、平板、手机宽度预览。
- **平台数据**：保存对话、版本、生成应用的业务记录，以及工作区、成员、资料、存储等数据。
- **其他界面**：发现与模板、公开作品、发布设置、积分与账本、连接器等已有界面和后端契约；外部服务的可用性取决于实际配置。

指定账号已验证真实模型生成、连续修改、构建预览、版本恢复，以及业务数据跨版本和 Docker 重启保留；最新公网 IP 镜像还验证了同一账号的应用记录在预览刷新、整页刷新和重新登录后可见，以及匿名分享与账号数据隔离。复杂组件的完整可视化布局编辑、真实邮件发送、生产验证码、OAuth、支付和正式域名部署仍未完成。未配置模型或 Docker 构建器时，页面可以启动，但生成任务不可用。

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
npm test -- --run
npm run build
```

最新线上镜像 `demo-20261022-4` 对应应用源码提交 `77459c8`，通过后端 66 项、前端 35 项测试及生产构建。浏览器回归确认了账号数据持久化与分享隔离；HTTPS 真实模型生成属于先前镜像的验收范围，细节见[发布验收记录](RELEASE_VERIFICATION.md)。旧版公网 IP 预览创建的匿名应用记录没有账号归属，无法安全地自动迁移到新账号身份；记录仍保留在数据库中。仓库不包含私有测试数据库、原始运行日志、密钥和账号密码。
