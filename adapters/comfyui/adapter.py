# SPDX-License-Identifier: GPL-3.0-only
"""ComfyUI renderer for the frozen H3 shot workflow (workflows/h3_shot_render.api.json).

Patches the api-format graph in place, optionally uploads one reference image,
submits /prompt, polls /history/{prompt_id} every 5s, then downloads the
resulting video through /view and persists it via the AssetStore.
"""
from __future__ import annotations

import copy
import json
import logging
import mimetypes
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

from ...config import settings
from ..contracts import ProgressFn
from ..asset_store.local import FFMPEG

log = logging.getLogger(__name__)

ASPECT_SIZES = {
    "9:16": (1024, 1792),
    "16:9": (1920, 1080),
    "1:1": (1024, 1024),
}
FPS = 24  # node 130 CreateVideo fps in the frozen template


# ----------------------------------------------------------------- http ----

def _http_json(base: str, method: str, path: str, body: Optional[dict] = None,
               timeout: float = 60.0) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_bytes(base: str, path: str, timeout: float = 300.0) -> bytes:
    with urllib.request.urlopen(base + path, timeout=timeout) as resp:
        return resp.read()


def upload_image(base: str, data: bytes, filename: str,
                 timeout: float = 120.0) -> str:
    """POST /upload/image (multipart, field name 'image'), return the stored name."""
    boundary = f"----svf{uuid.uuid4().hex}"
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    parts = [
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n".encode("utf-8"),
        data,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ]
    req = urllib.request.Request(
        base + "/upload/image", data=b"".join(parts), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    return out["name"]


def submit_prompt(base: str, workflow: dict, timeout: float = 60.0) -> str:
    out = _http_json(base, "POST", "/prompt", {"prompt": workflow}, timeout)
    return out["prompt_id"]


def poll_history(base: str, prompt_id: str, timeout_s: float,
                 on_progress: Optional[ProgressFn] = None,
                 interval_s: float = 5.0) -> dict:
    """Block until the prompt finishes; return its history entry.

    ComfyUI's HTTP loop can be starved for minutes while the GPU is saturated
    (observed on GB10 under memory pressure), so per-request timeouts are
    transient: keep polling until the overall deadline instead of failing.
    """
    deadline = time.monotonic() + timeout_s
    http_errors = 0
    while True:
        elapsed = int(time.monotonic() - (deadline - timeout_s))
        try:
            history = _http_json(base, "GET", f"/history/{prompt_id}", timeout=120.0)
            http_errors = 0
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            http_errors += 1
            if on_progress:
                on_progress(elapsed, int(timeout_s),
                            f"comfyui unreachable ({type(exc).__name__}) x{http_errors}, retrying")
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"ComfyUI prompt {prompt_id} unreachable and past deadline "
                    f"{timeout_s}s (last error: {exc})")
            time.sleep(interval_s)
            continue
        entry = history.get(prompt_id)
        if entry:
            status = entry.get("status") or {}
            if status.get("completed"):
                return entry
            if status.get("status_str") == "error":
                msgs = status.get("messages") or []
                raise RuntimeError(
                    f"ComfyUI prompt {prompt_id} failed: "
                    f"status={status.get('status_str')} messages={msgs}")
            summary = status.get("status_str") or "running"
        else:
            summary = "queued"
        if on_progress:
            on_progress(elapsed, int(timeout_s),
                        f"comfyui {summary} after {elapsed}s")
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"ComfyUI prompt {prompt_id} timed out after {timeout_s}s")
        time.sleep(interval_s)


def find_output_file(entry: dict, kinds: tuple[str, ...]) -> dict:
    """First {filename, subfolder, type} of the wanted kind in history outputs.

    SaveVideo in this ComfyUI version reports mp4 under "images" with
    animated=[true], so video kinds additionally match video extensions there.
    """
    video_ext = (".mp4", ".webm", ".mov", ".mkv", ".gif")
    for node_out in (entry.get("outputs") or {}).values():
        for kind in kinds:
            for item in node_out.get(kind) or []:
                if item.get("filename"):
                    return item
        if any(k in ("videos", "gifs") for k in kinds):
            for item in node_out.get("images") or []:
                if str(item.get("filename", "")).lower().endswith(video_ext):
                    return item
    raise RuntimeError(
        f"ComfyUI history has no {'/'.join(kinds)} output: "
        f"{list((entry.get('outputs') or {}).keys())}")


def download_output(base: str, item: dict, timeout: float = 600.0) -> bytes:
    qs = urllib.parse.urlencode({
        "filename": item["filename"],
        "subfolder": item.get("subfolder") or "",
        "type": item.get("type") or "output",
    })
    return _http_get_bytes(base, f"/view?{qs}", timeout=timeout)


# ------------------------------------------------------------- renderer ----

class H3ComfyRenderer:
    """ComfyRenderer protocol implementation."""

    def __init__(self, asset_store, base: Optional[str] = None,
                 workflow_path: Optional[str | Path] = None,
                 timeout_s: Optional[float] = None,
                 project_id: str = "factory"):
        self.asset_store = asset_store
        self.base = (base or settings.COMFY_BASE).rstrip("/")
        self.workflow_path = Path(workflow_path or settings.H3_SHOT_WORKFLOW)
        self.timeout_s = float(timeout_s or settings.RENDER_TIMEOUT_S)
        self.project_id = project_id

    def interrupt(self) -> None:
        """打断 ComfyUI 当前正在执行的任务。

        停止/删除项目时调用,避免已取消的任务继续占用 GPU(客户端已死、
        ComfyUI 服务端仍会把它跑完)。
        """
        try:
            _http_json(self.base, "POST", "/interrupt", {}, timeout=5.0)
            log.info("comfy interrupt sent to %s", self.base)
        except Exception:
            log.warning("comfy interrupt failed", exc_info=True)

    def _project_id(self, ref_image_keys: list[str]) -> str:
        # storage_key is {project_id}/{asset_id}/{filename}
        if ref_image_keys and "/" in ref_image_keys[0]:
            return ref_image_keys[0].split("/", 1)[0]
        return self.project_id

    def render_shot(self, prompt_spec: str, ref_image_keys: list[str], seed: int,
                    duration_s: int = 5, aspect_ratio: str = "9:16",
                    on_progress: Optional[ProgressFn] = None) -> str:
        workflow = copy.deepcopy(
            json.loads(self.workflow_path.read_text(encoding="utf-8")))
        width, height = ASPECT_SIZES.get(aspect_ratio, ASPECT_SIZES["9:16"])

        n131 = workflow["131"]["inputs"]
        n131["prompt"] = prompt_spec
        n131["width"] = width
        n131["height"] = height
        n131["length"] = duration_s * FPS + 4
        workflow["900"]["inputs"]["prompt_context"] = prompt_spec
        workflow["129"]["inputs"]["noise_seed"] = seed
        workflow["92"]["inputs"]["filename_prefix"] = f"video/svf_{seed}"

        if ref_image_keys:
            # The template wires exactly one ref image (node 901); extra keys
            # are dropped — multi-reference workflows need a different template.
            key = ref_image_keys[0]
            data = self.asset_store.read_bytes(key)
            name = upload_image(self.base, data, Path(key).name)
            workflow["901"]["inputs"]["image"] = name

        prompt_id = submit_prompt(self.base, workflow)
        entry = poll_history(self.base, prompt_id, self.timeout_s, on_progress)
        item = find_output_file(entry, ("videos", "gifs"))
        video_bytes = download_output(self.base, item)
        asset = self.asset_store.save_bytes(
            video_bytes, self._project_id(ref_image_keys), "video",
            item["filename"], source="generated")
        return asset.storage_key

    def normalize_clip(self, source_key: str, duration_s: int,
                       aspect_ratio: str) -> str:
        project_id = source_key.split("/", 1)[0] if "/" in source_key else self.project_id
        parent_ids = [source_key.split("/")[1]] if source_key.count("/") >= 2 else []
        src_path = self.asset_store.path_for(source_key)
        filename = Path(source_key).name

        if not FFMPEG:
            data = self.asset_store.read_bytes(source_key)
            asset = self.asset_store.save_bytes(
                data, project_id, "video", filename,
                source="derived", parent_asset_ids=parent_ids)
            return asset.storage_key

        width, height = ASPECT_SIZES.get(aspect_ratio, ASPECT_SIZES["9:16"])
        with tempfile.TemporaryDirectory() as td:
            out_path = Path(td) / (Path(filename).stem + "_h264.mp4")
            subprocess.run(
                [FFMPEG, "-y", "-v", "error", "-i", src_path,
                 "-t", str(duration_s),
                 "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-movflags", "+faststart", str(out_path)],
                capture_output=True, timeout=600, check=True)
            asset = self.asset_store.save_file(
                str(out_path), project_id, "video", out_path.name,
                source="derived", parent_asset_ids=parent_ids)
        return asset.storage_key
