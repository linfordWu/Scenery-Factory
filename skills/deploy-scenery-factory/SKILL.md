---
name: deploy-scenery-factory
description: 引导任意 Agent 从零把「镜界 Scenery 短视频工厂 Agent」（短剧 Agent 工厂）部署到 Linux 主机：环境自检 → 安装依赖 → 配置 .env → 启动 FastAPI 服务(默认 :8600) → 健康检查与验收。当用户要求「部署 / 启动 / 安装 / 把本项目跑起来」「部署短剧工厂 / 短视频工厂 / Scenery-Factory」「在 Fusion Xpark GB10 / aarch64 Linux 上部署本项目」时使用。
---

# Deploy Scenery-Factory Skill：把本项目部署成可用的短剧 Agent 工厂

## 一、这个 skill 做什么

把仓库 `Scenery-Factory/` 部署成一台可用的「短剧 Agent 工厂」：在本机启动一套 FastAPI + 零构建 Web 工作台服务（默认 `http://<host>:8600/`），对外提供创建项目、`#/chat` 对话出片、结构化分镜、逐镜视频生成、质检、修复、成片导出。

部署完成后，任意调用方（人或 Agent）只需通过 HTTP API 或 Web 工作台，就能把「一句话需求 / 一段对话」变成一部短剧成片。

## 二、产出物

| 产出 | 地址 / 位置 |
|---|---|
| 短视频工厂 API + Web 工作台 | `http://<host>:8600/` |
| 状态数据库（SQLite） | `data/factory.db`（`SVF_DB` 可改） |
| 运行配置 | 仓库根 `.env`（由 `.env.example` 生成，已被 gitignore） |

## 三、前置条件

- 操作系统：Linux（aarch64 / x86_64 均可；完整渲染链路建议 NVIDIA Fusion Xpark GB10）
- Python ≥ 3.10，且 `python3-venv` / `pip` 可用
- 文本模型 provider（必需其一）：
  - 默认 `deepseek`（需 `DEEPSEEK_API_KEY`）；赛事首选 StepFun，通过 `custom` provider 接入；离线可切 `ollama` / `vllm` / `kimi`。
- 生成与质检依赖服务（可选，缺省时仍能启动并完成规划，只有真正渲染时才报错）：
  - ComfyUI `http://localhost:8188`（MiniMax-H3 逐镜渲染）
  - Qwen-Image `http://localhost:8601`（定妆图 / 场景图 / 首帧）
  - 质检视觉模型（ollama，见 `VISION_MODEL_BASE` / `VISION_MODEL_NAME`）
- 决策引擎（可选）：Laya 默认本地运行，无需外部密钥；OpenJev 为实验路径。

> 服务本身不强制 GPU：`pip install -r requirements.txt` 后 `python -m apps.api` 即可启动、浏览工作台与跑规划。

## 四、部署流程

按顺序执行；任一步失败先查 `references/troubleshooting.md`。

### 0. 进入仓库根

```bash
cd Scenery-Factory        # 仓库根：含 requirements.txt、apps/、comfyui-node/
```

### 1. 环境自检

```bash
bash skills/deploy-scenery-factory/scripts/preflight.sh
```

检查 Python/venv、端口占用、关键文件、ComfyUI / Qwen-Image 可达性。出现 `[FAIL]` 必须先修复；`[WARN]` 表示可继续，但对应功能不可用（如 ComfyUI 未启动则无法渲染）。

### 2. 安装依赖并生成配置

```bash
bash skills/deploy-scenery-factory/scripts/deploy.sh
```

等价于手动执行：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # .env 已存在则跳过，不覆盖
```

### 3. 配置 `.env`

编辑仓库根 `.env`，按需二选一：

- 赛事首选 StepFun（OpenAI-compatible）：
  ```bash
  TEXT_MODEL_PROVIDER=custom
  SVF_CUSTOM_BASE=<StepFun endpoint>
  SVF_CUSTOM_MODEL=<模型名>
  SVF_CUSTOM_API_KEY=<key>
  ```
- 本地 / 离线：`TEXT_MODEL_PROVIDER=ollama`（或 `vllm`），并设 `TEXT_MODEL_BASE` / `TEXT_MODEL_NAME`。

安全：密钥只写入 `.env`（已 gitignore）或进程环境变量，禁止入库、入工作流、入日志、入演示画面。完整变量表见 `references/deployment.md`。

### 4. 启动服务

```bash
bash skills/deploy-scenery-factory/scripts/run.sh
```

等价于 `.venv/bin/python -m apps.api`，监听 `0.0.0.0:8600`（`SVF_PORT` 可覆盖）。需要常驻时交给 systemd / tmux / nohup。

### 5. 验收

```bash
bash skills/deploy-scenery-factory/scripts/verify.sh                  # 健康检查
RUN_TESTS=1 bash skills/deploy-scenery-factory/scripts/verify.sh      # 附带 pytest
```

健康检查命中 `GET /projects`；通过即服务在线，浏览器打开 `http://localhost:8600/` 应看到工作台。

## 五、端到端功能验收（可选，需 ComfyUI + 模型权重就位）

1. 打开 Web 工作台 → 新建项目，或进入 `#/chat` 用一段对话出片。
2. 查看结构化分镜（ShotSpec）。
3. 启动生成，观察 SSE 事件流与任务状态。
4. 查看质检报告（ScoreReport）与修复计划（RepairPlan）。
5. 审核 → 导出成片。

## 六、让 CodeBuddy 自动加载本 skill（可选）

仓库自带 skill 位于 `skills/<name>/SKILL.md`。若要 CodeBuddy 在仓库内自动发现本 skill，复制或软链接到项目技能目录：

```bash
mkdir -p .codebuddy/skills
ln -s ../../skills/deploy-scenery-factory .codebuddy/skills/deploy-scenery-factory
```

## 七、资源

- `scripts/preflight.sh` — 部署前环境自检
- `scripts/deploy.sh` — 建 venv、装依赖、生成 `.env`
- `scripts/run.sh` — 前台启动服务
- `scripts/verify.sh` — 健康检查（可选 pytest）
- `references/deployment.md` — 环境变量、依赖服务与决策引擎参考
- `references/troubleshooting.md` — 常见故障排查
