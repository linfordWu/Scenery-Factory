# Fusion Xpark GB10 Linux 部署说明

部署目标：**NVIDIA Fusion Xpark GB10 + aarch64 Linux**。

## 1. 部署范围

- 真实主机：`localhost`
- SSH 用户：`wlf`；需要 root 操作时登录后提权。密码不写入仓库。
- 操作系统：aarch64 Linux
- 硬件平台：NVIDIA Fusion Xpark GB10（Grace-Blackwell，统一内存，sm_121）
- 容器运行：Docker + Docker Compose + NVIDIA Container Toolkit / CDI
- 核心服务：ComfyUI、Scenery-Factory FastAPI、StepFun 主 Agent provider、Laya 本地决策、OpenJev `llama-server`（可选）
- 不包含：模型权重分发、外部平台账号配置、非 Fusion Xpark GB10 环境适配

## 2. 总体架构

```text
Fusion Xpark GB10 Linux 宿主机（localhost）
├── Docker Compose：ComfyUI 服务（端口 8188）
│   ├── MiniMax-H3 视频生成
│   ├── H3JevNativeSLAPatch 009jev 节点
│   ├── Laya 本地决策客户端
│   └── OpenJev HTTP 客户端（可选）
├── llama-server（端口 8091，可选 OpenJev）
└── Scenery-Factory FastAPI（端口 8600）
    ├── Web 工作台
    ├── StepFun 主 Agent provider（可插拔）
    ├── 编剧 / 导演 / 提示词 / 修复 Agent
    ├── 素材库、状态机、事件流
    └── 质检、合成、导出
```

决策引擎通过 `H3_DECISION_ENGINE` 切换：`laya` 为默认本地路径，`openjev` 为实验路径，`jev` 为远程接口占位。提交版默认推荐 `laya`，因为它完全在本地运行，无需外部密钥。

## 3. 前置条件

1. 可通过 `ssh wlf@localhost` 登录真实部署主机；登录后按需要切换到 root。
2. Fusion Xpark GB10 Linux 环境已安装驱动、Docker、Docker Compose 与 NVIDIA Container Toolkit。
3. ComfyUI 容器可通过 CDI 使用 GPU，例如 `nvidia.com/gpu=all`。
4. 已准备模型文件，并放置到 ComfyUI 持久化 `workspace/models/`：
   - `diffusion_models/minimax_h3_fused_refdelta_r1024_turbo8_mystic07_int8_convrot.safetensors`
   - `text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`
   - `vae/minimax_h3_video_vae_int8_convrot.safetensors`
   - `vae/minimax_h3_audio_vae_fp32.safetensors`
   - `models/laya/` 决策权重
   - 可选：`APUS-OpenJev-v1-4B.Q6_K.gguf`
5. 本仓库已挂载或复制到 ComfyUI `custom_nodes` 目录。
6. 已准备 StepFun 主 Agent 的 OpenAI-compatible endpoint、模型名和密钥；未准备时可先切换其他 provider 进行功能验证。

## 4. 启动 ComfyUI 与 MiniMax-H3

登录真实主机后进入 compose 项目：

```bash
ssh wlf@localhost
sudo -i
cd /root/comfyui/xfusion/comfyui-Xpark-minimax-h3-nomodel
docker compose up -d
docker logs -f comfyui-spark
```

当前已核验 ComfyUI 可通过 `http://localhost:8188` 访问，启动参数包含：

```bash
--listen 0.0.0.0 --port 8188 --use-sage-attention --fp8_e4m3fn-unet --bf16-vae --bf16-text-enc --disable-pinned-memory --reserve-vram 2.0
```

持久化数据位于 compose 项目的 `workspace/` 中，包括模型、custom_nodes、input、output 和用户配置。容器重建不会清除这些文件。

### 4.1 验证节点

进入 ComfyUI Python 环境后执行（节点文件位于 `comfyui-node/`）：

```bash
python3 -B -X utf8 comfyui-node/test_native_sla.py --comfy-root /opt/ComfyUI
python3 -B comfyui-node/test_laya_client.py
```

如需验证 OpenJev 客户端，可使用离线 stub：

```bash
python3 comfyui-node/test_openjev_client.py
```

## 5. 部署 Laya 本地决策引擎

Laya 是默认决策路径，代码入口为 `comfyui-node/laya_client.py`，依赖见 `comfyui-node/requirements.txt` 与 `comfyui-node/requirements-laya.txt`。

```bash
pip install -r comfyui-node/requirements.txt
```

权重目录默认为 ComfyUI 的 `models/laya/`，也可以用环境变量显式指定：

```bash
export H3_DECISION_ENGINE=laya
export LAYA_MODEL_DIR=/opt/ComfyUI/models/laya
export LAYA_DEVICE=cuda
export LAYA_BATCH=16
export LAYA_STATE_MODE=compact
export LAYA_SUBFOLDER=multilingual
```

推荐保持 `LAYA_STATE_MODE=compact`，它在保证可用性的前提下显著降低长状态输入开销。若显存紧张，可降低 `LAYA_BATCH`；若需要调试，可查看日志中的 `[Laya VSA]` 输出。

## 6. 部署 OpenJev 实验决策引擎（可选）

OpenJev 通过宿主机 `llama-server` 提供服务：

```bash
llama-server \
  -m APUS-OpenJev-v1-4B.Q6_K.gguf \
  -ngl 99 \
  --host 0.0.0.0 \
  --port 8091 \
  -c 40960 \
  --jinja \
  -fa on
```

在 ComfyUI 容器中切换：

```bash
export H3_DECISION_ENGINE=openjev
export OPENJEV_URL=http://localhost:8091
```

如果使用 Docker Compose，可将上述变量写入 comfyui 服务的 `environment`，然后执行 `docker compose up -d comfyui`。

## 7. 部署短视频工厂 Agent（Scenery-Factory）

```bash
cd Scenery-Factory
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m apps.api
```

访问地址：`http://localhost:8600`。服务在主机上以 `0.0.0.0` 绑定，本机与局域网均可通过对应端口访问。

> 也可以用仓库内置的 `deploy-scenery-factory` skill 完成上述部署（环境自检 → 安装依赖 → 生成 `.env` → 启动 → 健康检查），见第 8 节。

### 7.1 StepFun 主 Agent 首选配置

主 Agent 模型是可插拔的。赛事推荐配置将 StepFun 阶跃星辰作为主 Agent，通过 OpenAI-compatible `custom` provider 接入：

```bash
TEXT_MODEL_PROVIDER=custom
SVF_CUSTOM_BASE=<StepFun OpenAI-compatible endpoint>
SVF_CUSTOM_MODEL=<StepFun 模型名>
SVF_CUSTOM_API_KEY=<StepFun API Key>
```

安全要求：密钥只写入本地 `.env` 或进程环境变量，不写入 Git、工作流、日志或演示画面。演示视频中如需展示配置，只展示变量名，不展示密钥值。

### 7.2 其他可选 provider

未配置 StepFun 时，可将 `TEXT_MODEL_PROVIDER` 切换为 `ollama`、`vllm`、`deepseek`、`kimi` 或自定义 endpoint。该设计保证主 Agent 可替换，但赛事文档与推荐部署以 StepFun 为主。

关键环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `COMFY_BASE` | `http://localhost:8188` | ComfyUI 服务地址（本机默认） |
| `SVF_PORT` | `8600` | 短视频工厂服务端口 |
| `SVF_DATA_DIR` | `data/` | 数据目录 |
| `SVF_DB` | `data/factory.db` | SQLite 状态数据库 |
| `TEXT_MODEL_PROVIDER` | `deepseek` | 文本 Agent provider；StepFun 通过 `custom` 接入 |
| `SVF_CUSTOM_BASE` | 空 | StepFun OpenAI-compatible endpoint |
| `SVF_CUSTOM_MODEL` | 空 | StepFun 模型名 |
| `SVF_CUSTOM_API_KEY` | 空 | StepFun API Key |
| `QWEN_IMAGE_URL` | `http://localhost:8601` | 图像服务地址 |
| `VISION_MODEL_BASE` / `VISION_MODEL_NAME` | 本地视觉模型配置 | 质检抽帧评分 |
| `SVF_MAX_REPAIRS` | `2` | 单镜自动修复预算 |
| `SVF_RENDER_TIMEOUT` | `7200` | 单镜渲染超时时间 |

## 8. Agent Skills 部署与使用

仓库内置三个 skill，均以 Markdown 形式保存：

| Skill | 文件 | 用途 |
|---|---|---|
| `deploy-scenery-factory` | `skills/deploy-scenery-factory/SKILL.md` | 任意 Agent 从零部署本项目（短剧 Agent 工厂） |
| `learning-video` | `skills/learning-video/SKILL.md` | 文档 / 图片集 → 1080P 学习视频 |
| `short-drama-script` | `skills/short-drama-script/SKILL.md` | 短剧剧本与分镜结构化约束 |

### 8.1 deploy-scenery-factory

该 skill 让任意 Agent 把本项目部署成一台可用的短剧 Agent 工厂：

1. `bash skills/deploy-scenery-factory/scripts/preflight.sh` 环境自检（Python / venv / 端口 / ComfyUI 与 Qwen-Image 可达性）。
2. `bash skills/deploy-scenery-factory/scripts/deploy.sh` 建 venv、装 `requirements.txt`、由 `.env.example` 生成 `.env`。
3. 编辑 `.env` 选择文本模型 provider（赛事首选 StepFun `custom`；离线切 `ollama`）。
4. `bash skills/deploy-scenery-factory/scripts/run.sh` 启动服务（`http://localhost:8600`）。
5. `bash skills/deploy-scenery-factory/scripts/verify.sh` 健康检查（`GET /projects`），`RUN_TESTS=1` 可附带 pytest。
6. 环境变量与故障排查见 `references/deployment.md`、`references/troubleshooting.md`。

### 8.2 learning-video

1. 将文档图片放入 ComfyUI `input/`。
2. 在 `skills/learning-video/scripts/gen_learning_video.py` 的 `CLIPS` 中填写图片名、旁白和动效脚本。
3. 先单片试跑并抽头 / 中 / 尾帧质检。
4. 批量生成后用 `concat_learn.sh` 拼接，必要时用 `dewatermark.sh` 处理贴边水印。

### 8.3 short-drama-script

该 skill 约束短视频工厂的编剧、选角、导演、提示词和修复 Agent。核心原则是：一镜一主动作、角色与场景描述全片一致、关键物体登记数量与起止状态、beats 三桶必填、台词进入 `<d>[Chinese] ...</d>` 语法、否定约束使用英文、提示词密度不超过阈值。

### 8.4 Agent Skills 的设计原则（如何设计 Skills）

本项目把可复用经验沉淀为 **skill markdown 文件**（`skills/*/SKILL.md`），设计遵循四条原则：

1. **声明式元数据**：每个 `SKILL.md` 用 YAML frontmatter 声明 `name` 与 `description`，`description` 明确写出「做什么 + 何时触发（when to use）」，使 Agent 能在合适的任务上自动加载该 skill。
2. **流程即契约**：skill 正文把一次调通的操作固化成有序步骤（素材提取 → 三要素 → 试跑质检 → 批量生成 → 拼接 → 去水印），并显式给出硬规则与负面清单（如「否定约束用英文」），让后续执行不再依赖个人记忆。
3. **与代码契约对齐**：skill 描述的输出结构与 `domain/`（ShotSpec）和 `agents/`（编剧 / 导演 / 提示词）的代码契约一一对应，避免「文档一套、代码一套」。
4. **可增量演进**：skill 以纯 Markdown 保存，新增内容生产场景（学习视频、短剧、后续其它形态）只需新增一个 skill 目录，不侵入核心流水线，从而把「这次调通了」变成「下次可复用」的组织资产。

## 9. 大模型优化策略

1. **StepFun 主 Agent**：承担高层创作和修复推理；通过 provider 抽象避免模型锁定。
2. **MiniMax-H3 推理**：使用 FP8 UNet、BF16 VAE / 文本编码器，并保留 2GB 显存余量。
3. **稀疏注意力**：优先使用 009jev 原生 SLA 路径，从首步开始进行逐层 keep rate 决策。
4. **Laya 决策**：本地加载、批处理、紧凑状态和低置信回退，避免外部 API 抖动影响生产。
5. **OpenJev 决策**：GGUF 量化与 `llama-server` 前缀缓存降低逐问成本。
6. **素材复用**：导入素材先哈希去重和版本绑定，能复用的视频不重复生成。
7. **资源管理**：重型任务使用独占租约、心跳和 fencing token，阶段切换时卸载模型并确认资源释放。
8. **修复预算**：首轮失败后最多自动修复有限次数，超过预算转人工审核。

## 10. 验收命令

```bash
# ComfyUI 节点与 Laya
python3 -B -X utf8 comfyui-node/test_native_sla.py --comfy-root /opt/ComfyUI
python3 -B comfyui-node/test_laya_client.py

# 短视频工厂测试
cd Scenery-Factory
.venv/bin/python -m pytest tests/
```

功能验收路径：打开 Web 工作台 → 创建项目 → 上传或生成素材 → 查看结构化分镜 → 启动生成 → 观察任务事件 → 查看质检报告 → 审核 → 导出成片。

## 11. 当前实测结果

Fusion Xpark GB10 Linux、5 秒、4 step、热缓存条件下：

| 路径 | 耗时 | 相对基线 | SSIM |
|---|---:|---:|---:|
| 基线 | 530.2 s | — | 1.000 |
| Laya | 340.1 s | -35.9% | 0.761 |
| OpenJev | 400.1 s | -24.5% | 0.760 |

以上数值用于说明当前仓库在 Fusion Xpark GB10 Linux 上的本地部署效果，不代表所有输入、所有模型版本或所有工作负载的固定结果。