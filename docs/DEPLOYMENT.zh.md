# MiniMax-H3 视频生成系统部署文档

测试平台：NVIDIA GB10（Grace-Blackwell, aarch64, 121GB 统一内存, sm_121）。

## 1. 系统总览

```
┌──────────────────────────── 宿主机 (aarch64 Linux) ───────────────────────────┐
│  docker compose 项目: /root/comfyui/xfusion/comfyui-Xpark-minimax-h3-nomodel  │
│  ┌─────────────────────────────────┐   ┌──────────────────────────────┐      │
│  │ 容器 comfyui-spark              │   │ 宿主机进程                    │      │
│  │  ComfyUI :8188                  │   │  llama-server :8091          │      │
│  │  ├─ MiniMax-H3 (fp8, GPU)       │◄──│  OpenJev 4B Q6_K (GPU)      │      │
│  │  ├─ Laya 决策引擎 (进程内)       │   │  (仅 H3_DECISION_ENGINE=    │      │
│  │  └─ OpenJev 客户端 (HTTP)       │   │   openjev 时需要)            │      │
│  └─────────────────────────────────┘   └──────────────────────────────┘      │
└──────────────────────────────────────────────────────────────────────────────┘
```

- 决策引擎三选一（容器环境变量 `H3_DECISION_ENGINE`）：`laya`（默认）/ `openjev` / `jev`（远程 API）
- 所有持久化数据（模型、节点、输入输出）都在 compose 项目的 `workspace/` 卷中，容器重建不丢失

## 2. ComfyUI 安装（docker compose）

compose 项目：`/root/comfyui/xfusion/comfyui-Xpark-minimax-h3-nomodel/`

| 组件 | 说明 |
|---|---|
| 镜像 | `comfyui-xpark-minimax-h3:jev`（本地构建：基础镜像 + ComfyUI v0.36.x + `pip install laya`，见 `Dockerfile` / `Dockerfile.jev`） |
| 服务 | `comfyui`（ComfyUI，端口 8188）、`ollama`（sidecar，健康依赖） |
| GPU | CDI 设备 `nvidia.com/gpu=all` |
| 关键启动参数 | `--use-sage-attention --fp8_e4m3fn-unet --bf16-vae --bf16-text-enc --reserve-vram 2.0` |
| 持久化 | `workspace/`（models、custom_nodes、input、output、user 全在内）；`entrypoint.sh` 首启动播种 15 个内置节点包并安装各节点 `requirements.txt` |

常用命令：

```bash
cd /root/comfyui/xfusion/comfyui-Xpark-minimax-h3-nomodel
docker compose up -d              # 启动 / 按配置重建
docker compose restart comfyui    # 重启（不重建，保留运行时 pip 安装）
docker logs -f comfyui-spark      # 日志（含 [Laya VSA]/[OpenJev] 决策日志）
```

## 3. MiniMax-H3 模型安装

全部权重位于 `workspace/models/`（容器内 `/opt/ComfyUI/models/`）：

| 文件 | 位置 | 来源 |
|---|---|---|
| `minimax_h3_fused_refdelta_r1024_turbo8_mystic07_int8_convrot.safetensors`（21GB，主力） | `diffusion_models/` | [MATLOWAI/minimax-h3-fused-turbo-int8-convrot](https://huggingface.co/MATLOWAI/minimax-h3-fused-turbo-int8-convrot)，本机经 ModelScope 镜像下载 |
| `minimax_h3_ref2va_pruned_int8_convrot.safetensors` / `fl2va` | `diffusion_models/` | 备选管线 |
| `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | `text_encoders/` | Comfy-Org/MiniMax-H3 |
| `minimax_h3_video_vae_int8_convrot.safetensors`、`minimax_h3_audio_vae_fp32.safetensors` | `vae/` | Kijai / Comfy-Org |
| `fasth3_vsa_gate.safetensors`（3.85GB，备用） | `models/` | barelymining/ComfyUI-MiniMax-H3-FastVideo |

**平台限制（sm_121）**：W4A4 预转换管线（`convert.py` + `h3_preconverted` 缓存）要求 native ConvRot INT4
与编译版 Sol-Attn 内核，当前容器 PyTorch 2.9.1 最高支持 sm_120，`convert.py` 会拒绝执行。
gate 权重已备妥，容器升级支持 sm_121 的 PyTorch 后重跑 `convert.py` 即可启用。

## 4. Laya 决策引擎（默认）

- **代码**：`comfyui-node/laya_client.py`（对 `jev_client` 的透明替换，契约一致）
- **安装**：仓库根 `requirements.txt`（`laya>=0.3.5`），容器启动时由 entrypoint 自动安装；
  离线 wheelhouse 位于 `workspace/.wheels/`（秒装、不依赖网络）；`Dockerfile.jev` 亦内置
- **权重**：`workspace/models/laya/`（2.3GB，[convaiinnovations/laya](https://www.modelscope.cn/models/convaiinnovations/laya)），经 folder_paths 零配置发现；也可用 `LAYA_MODEL_DIR` 显式指定
- **关键环境变量**：`LAYA_DEVICE`（默认 cuda）、`LAYA_BATCH`（16）、`LAYA_STATE_MODE`（compact）、`LAYA_SUBFOLDER`（multilingual）
- **性能**：GPU 上 50 问 p50 ≈ 136ms；显存不足会降级 CPU（慢 ~29 倍），注意日志告警
- **测试**：`python3 comfyui-node/test_laya_client.py`（16 项）

## 5. OpenJev 决策引擎（实验，分支 exp/openjev-engine）

- **代码**：`comfyui-node/openjev_client.py`（同一契约；OpenJev 是"一问一答"生成式模型，逐问请求 +
  状态前置 + llama-server 前缀缓存，~250ms/问）
- **模型**：`APUS-OpenJev-v1-4B.Q6_K.gguf`（3.46GB，[ModelScope](https://www.modelscope.cn/models/prithivMLmods/APUS-OpenJev-v1-4B-GGUF)）
- **推理服务**：宿主机运行（llama.cpp 需 CUDA sm_121 源码编译）：

```bash
llama-server -m APUS-OpenJev-v1-4B.Q6_K.gguf -ngl 99 --host 0.0.0.0 --port 8091 -c 40960 --jinja -fa on
```

- **容器接入**：compose 的 comfyui 服务加 `H3_DECISION_ENGINE: "openjev"` 后 `docker compose up -d comfyui`；
  容器经 docker 网关访问宿主机（默认 `OPENJEV_URL=http://172.19.0.1:8091`）
- **测试**：`python3 comfyui-node/test_openjev_client.py`（11 项，stub 服务器离线可跑）

## 6. 项目使用

### 节点

| 节点 | 用途 |
|---|---|
| `H3JevNativeSLAPatch`（009jev） | 原生 SLA 稀疏注意力 + 决策引擎，**推荐**（无需预转换缓存） |
| `H3V2JevAdaptiveVSAPatch` | VSA 自适应（需 W4A4 预转换管线，sm_121 暂不可用） |
| `H3V2PreconvertedLoader` | 预转换缓存加载（同上） |

### 工作流（`comfyui-node/examples/`）

| 文件 | 用途 |
|---|---|
| `matlow_fused_4step.api.json` | 基线（无决策补丁），A/B 对照 |
| `009jev_cold_start.api.json` | 决策引擎冷启动（推荐模板） |
| `009jev_4step.api.json` | 带历史上下文的决策 |
| `learning_video_1080p.api.json` | 1080P 图解动画片段（见 `comfyui-node/docs/LEARNING_VIDEO.md`） |

### 基准脚本（参考实现）

`/home/wlf/models/` 下：`bench_examples.py`（基线 vs laya）、`bench_openjev.py`、
`gen_learning_video.py`（学习视频批量生成）、`concat_learn.sh`（ffmpeg 拼接）。

### 实测性能（GB10，5 秒 4 步视频，热缓存）

| 引擎 | 耗时 | vs 基线 | SSIM vs 基线 |
|---|---|---|---|
| 基线 | 530.2s | — | 1.0 |
| laya | 340.1s | -35.9% | 0.761 |
| openjev | 400.1s | -24.5% | 0.760 |
