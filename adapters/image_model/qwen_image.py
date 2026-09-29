# SPDX-License-Identifier: GPL-3.0-only
"""Qwen-Image 2.1 text-to-image adapter.

The weights are a diffusers bundle that ComfyUI cannot load directly, so a
small persistent HTTP service (qwen_image_server.py, started inside the
comfyui container) hosts the pipeline; this adapter just calls it.
"""
from __future__ import annotations

import base64
import json
import urllib.request
from typing import Optional

from ...config import settings


class QwenImageModel:
    """ImageModel protocol implementation (HTTP service backend)."""

    def __init__(self, asset_store, base: Optional[str] = None,
                 timeout_s: float = 1800.0, project_id: str = "factory",
                 store=None):
        self.asset_store = asset_store
        self.base = (base or settings.QWEN_IMAGE_URL).rstrip("/")
        self.timeout_s = timeout_s
        self.project_id = project_id
        self.store = store  # 传入则 generate 后自动登记 assets 表

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
                return json.loads(resp.read()).get("status") == "ok"
        except Exception:
            return False

    def generate(self, prompt: str, out_key: str, width: int = 1080,
                 height: int = 1920, seed: Optional[int] = None,
                 steps: int = 30) -> str:
        body = {"prompt": prompt, "width": width, "height": height,
                "steps": steps}
        if seed is not None:
            body["seed"] = seed
        req = urllib.request.Request(
            self.base + "/generate",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            data = json.loads(resp.read())
        if "error" in data:
            raise RuntimeError(f"qwen-image: {data['error'][:300]}")
        png = base64.b64decode(data["image_b64"])
        project_id = out_key.split("/", 1)[0] if "/" in out_key else self.project_id
        asset = self.asset_store.save_bytes(
            png, project_id, "image", out_key.rsplit("/", 1)[-1] + ".png"
            if not out_key.endswith(".png") else out_key.rsplit("/", 1)[-1],
            source="generated")
        if self.store is not None:
            self.store.put("assets", asset)
        return asset.storage_key
