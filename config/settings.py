# SPDX-License-Identifier: GPL-3.0-only
"""Runtime configuration: endpoints and paths, all env-overridable."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs from a local .env file; real env vars win.

    Keeps provider keys out of the repo (`.env` is gitignored); deployments
    that prefer plain environment variables keep working unchanged.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and not os.environ.get(key):    # real env (incl. empty) wins
            os.environ[key] = value.strip().strip('"').strip("'")


_load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.environ.get("SVF_DATA_DIR", BASE_DIR / "data"))
ASSET_ROOT = Path(os.environ.get("SVF_ASSET_ROOT", DATA_DIR / "assets"))
DB_PATH = Path(os.environ.get("SVF_DB", DATA_DIR / "factory.db"))

COMFY_BASE = os.environ.get("COMFY_BASE", "http://localhost:8188")
# Ollama sidecar (gemma3:4b, multimodal) in the compose network
TEXT_MODEL_BASE = os.environ.get("TEXT_MODEL_BASE", "http://172.19.0.2:11434/v1")
TEXT_MODEL_NAME = os.environ.get("TEXT_MODEL_NAME", "gemma3:4b")
# Same OpenAI-compatible endpoint serves vision judging (gemma3:4b accepts images)
VISION_MODEL_BASE = os.environ.get("VISION_MODEL_BASE", TEXT_MODEL_BASE)
VISION_MODEL_NAME = os.environ.get("VISION_MODEL_NAME", TEXT_MODEL_NAME)
# llama-server hosting OpenJev (structured one-answer decisions)
OPENJEV_BASE = os.environ.get("OPENJEV_BASE", "http://localhost:8091/v1")
OPENJEV_NAME = os.environ.get("OPENJEV_NAME", "openjev")

# Pluggable text-model providers (agents 通用模型能力，自定义接入).
# Select with TEXT_MODEL_PROVIDER; keys read from env at call time so secrets
# never land in the repo. Add your own provider via SVF_CUSTOM_* env vars.
TEXT_PROVIDERS = {
    # DeepSeek open platform (default agent model): deepseek-flash = V4.1 Flash
    "deepseek": {"base": os.environ.get("DEEPSEEK_BASE",
                                        "https://api.deepseek.com/v1"),
                 "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
                 "key_env": "DEEPSEEK_API_KEY"},
    "ollama": {"base": TEXT_MODEL_BASE, "model": TEXT_MODEL_NAME, "key_env": None},
    # Local vLLM serving Qwen3.8-27B-NVFP4 (xpark container, host network)
    "vllm": {"base": os.environ.get("VLLM_BASE", "http://localhost:8000/v1"),
             "model": os.environ.get("VLLM_MODEL", "unsloth/Qwen3.8-27B-NVFP4"),
             "key_env": None},
    # Remote Kimi coding endpoint (K2.8), OpenAI-compatible; temperature fixed at 1
    "kimi": {"base": os.environ.get("KIMI_BASE", "https://api.kimi.com/coding/v1"),
             "model": os.environ.get("KIMI_MODEL", "kimi-for-coding"),
             "key_env": "KIMI_API_KEY", "params": {"temperature": 1.0}},
    # Fully custom OpenAI-compatible endpoint
    "custom": {"base": os.environ.get("SVF_CUSTOM_BASE", ""),
               "model": os.environ.get("SVF_CUSTOM_MODEL", ""),
               "key_env": "SVF_CUSTOM_API_KEY"},
}
TEXT_MODEL_PROVIDER = os.environ.get("TEXT_MODEL_PROVIDER", "deepseek")

LAYA_MODEL_DIR = os.environ.get("LAYA_MODEL_DIR", "/home/wlf/models/laya/weights")
LAYA_PYTHON = os.environ.get("LAYA_PYTHON", "/home/wlf/models/laya/.venv/bin/python")

# Frozen ComfyUI workflow templates
WORKFLOWS_DIR = Path(os.environ.get("SVF_WORKFLOWS_DIR", BASE_DIR / "workflows"))
H3_SHOT_WORKFLOW = WORKFLOWS_DIR / "h3_shot_render.api.json"
QWEN_IMAGE_WORKFLOW = WORKFLOWS_DIR / "qwen_image.api.json"
# Qwen-Image 2.1 diffusers HTTP service (runs inside the comfyui container)
QWEN_IMAGE_URL = os.environ.get("QWEN_IMAGE_URL", "http://172.19.0.3:8601")

DEFAULT_SEED = int(os.environ.get("SVF_SEED", "2026"))
RENDER_TIMEOUT_S = int(os.environ.get("SVF_RENDER_TIMEOUT", "7200"))
MAX_REPAIRS = int(os.environ.get("SVF_MAX_REPAIRS", "2"))
# 审核员 agent 每批发给视觉模型的抽帧数上限
REVIEW_FRAME_BATCH = int(os.environ.get("SVF_REVIEW_FRAME_BATCH", "4"))
# 提示词密度负载阈值：超过只记 density.warning 事件，不减内容
DENSITY_LIMIT = float(os.environ.get("SVF_DENSITY_LIMIT", "3.0"))

for d in (DATA_DIR, ASSET_ROOT):
    d.mkdir(parents=True, exist_ok=True)
