# 部署参考：环境变量与依赖服务

更完整的平台部署说明见仓库 `docs/hackathon/02-Fusion-Xpark-GB10-Linux部署说明.md` 与 `docs/DEPLOYMENT.zh.md`。

## 一、文本模型 provider（主 Agent，必需其一）

| 变量 | 默认 | 说明 |
|---|---|---|
| `TEXT_MODEL_PROVIDER` | `deepseek` | `deepseek` \| `ollama` \| `vllm` \| `kimi` \| `custom` |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE` / `DEEPSEEK_MODEL` | key 空 / 官方 endpoint / `deepseek-flash` | 默认 provider |
| `TEXT_MODEL_BASE` / `TEXT_MODEL_NAME` | 依 provider | `ollama` / `vllm` 本地模型端点与模型名 |
| `SVF_CUSTOM_BASE` / `SVF_CUSTOM_MODEL` / `SVF_CUSTOM_API_KEY` | 空 | 赛事首选 StepFun（OpenAI-compatible），`TEXT_MODEL_PROVIDER=custom` 时启用 |

## 二、依赖服务（可选，按需配置）

| 变量 | 默认 | 用途 |
|---|---|---|
| `COMFY_BASE` | `http://localhost:8188` | ComfyUI：MiniMax-H3 逐镜渲染 |
| `QWEN_IMAGE_URL` | `http://localhost:8601` | Qwen-Image 2.1：定妆 / 场景 / 首帧 |
| `VISION_MODEL_BASE` / `VISION_MODEL_NAME` | 本地视觉模型 | 质检抽帧评分 |
| `OPENJEV_BASE` | `http://localhost:8091/v1` | 可选：本地决策引擎 |

## 三、运行参数

| 变量 | 默认 | 说明 |
|---|---|---|
| `SVF_PORT` | `8600` | 服务端口 |
| `SVF_DATA_DIR` | `./data` | 数据目录 |
| `SVF_DB` | `data/factory.db` | SQLite 状态数据库 |
| `SVF_MAX_REPAIRS` | `2` | 单镜自动修复预算 |
| `SVF_RENDER_TIMEOUT` | `7200` | 单镜渲染超时（秒） |

## 四、决策引擎（ComfyUI 侧）

容器环境变量 `H3_DECISION_ENGINE` 三选一，提交版默认 `laya`（完全本地、无需外部密钥）：

| 值 | 路径 | 说明 |
|---|---|---|
| `laya` | 默认 | 本地权重 `models/laya/`，代码 `comfyui-node/laya_client.py` |
| `openjev` | 实验 | 宿主机 `llama-server :8091` + GGUF 权重 |
| `jev` | 占位 | 远程接口 |

Laya 相关环境变量：`LAYA_MODEL_DIR`（默认 ComfyUI `models/laya/`）、`LAYA_DEVICE`（`cuda`）、`LAYA_BATCH`（`16`）、`LAYA_STATE_MODE`（`compact`）、`LAYA_SUBFOLDER`（`multilingual`）。
