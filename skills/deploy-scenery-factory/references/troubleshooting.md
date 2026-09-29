# 部署故障排查

## 服务启动类

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: fastapi / uvicorn / pydantic` | 未装依赖或用了系统 Python | 用虚拟环境：`.venv/bin/python -m apps.api`，先跑 `scripts/deploy.sh` |
| `python: command not found` / `venv` 不可用 | 缺 python3-venv | `apt install python3-venv`，重跑 `scripts/preflight.sh` |
| 端口被占用 / 8600 起不来 | 已有实例或端口冲突 | `SVF_PORT=8601 ... scripts/run.sh` 换端口；或先停掉占用进程 |
| 启动即报 `.env` 缺失 | 未生成配置 | `cp .env.example .env`（或跑 `scripts/deploy.sh`） |
| 打开 `http://localhost:8600/` 无响应 | 服务未启动 / 防火墙 / 绑定了别的 host | 确认进程存活、`curl localhost:8600/projects` 有响应 |

## 规划 / 生成类

| 现象 | 原因 | 处理 |
|---|---|---|
| 新项目能创建，但规划一直不动 | 文本模型 provider 不可用或 key 无效 | 检查 `TEXT_MODEL_PROVIDER` 与对应 key/endpoint；离线切 `ollama` 并确认其服务在线 |
| 生成阶段报 ComfyUI 连接失败 | ComfyUI 未启动或无模型权重 | 先启动 ComfyUI `:8188`，放置 MiniMax-H3 / Qwen3VL / VAE / Laya 权重 |
| 定妆图 / 首帧失败 | Qwen-Image 服务不可达 | 检查 `QWEN_IMAGE_URL` 对应服务 |
| 质检无结果 | 视觉模型未配置 | 设置 `VISION_MODEL_BASE` / `VISION_MODEL_NAME` 并确认服务在线 |
| 渲染非常慢 | Laya 显存不足降级到 CPU | 查看日志 `[Laya VSA]`；降低 `LAYA_BATCH` 或释放显存 |

## 测试类

| 现象 | 原因 | 处理 |
|---|---|---|
| `pytest` 收集失败 | 未装开发依赖 | `.venv/bin/pip install -r requirements-dev.txt`（`RUN_TESTS=1 scripts/verify.sh` 会自动装） |
| 提示找不到 `apps` / `svf` | 不在仓库根运行 | 所有命令都在仓库根 `Scenery-Factory/` 执行 |

## 安全提醒

- 密钥只写入 `.env` 或进程环境变量；`.env` 已被 gitignore，禁止提交。
- 演示 / 截图 / 日志中只出现变量名，不出现密钥值。
