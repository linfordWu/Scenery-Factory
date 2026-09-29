# SPDX-License-Identifier: GPL-3.0-only
"""Local filesystem AssetStore.

Layout: {ASSET_ROOT}/{project_id}/{asset_id}/{filename}. ffprobe/ffmpeg are
only present inside the worker containers, so probing and preview generation
degrade gracefully when they are missing on the host.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Optional

from ...config import settings
from ...domain.schemas.core import Asset

FFPROBE = shutil.which("ffprobe")
FFMPEG = shutil.which("ffmpeg")

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_AV_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".mp3", ".wav", ".aac", ".flac", ".m4a"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _probe_image_header(path: Path) -> dict:
    """Width/height from PNG/JPEG/GIF headers without any external tool."""
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return {}
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        w, h = struct.unpack(">II", head[16:24])
        return {"width": w, "height": h, "codec": "png"}
    if head[:6] in (b"GIF87a", b"GIF89a") and len(head) >= 10:
        w, h = struct.unpack("<HH", head[6:10])
        return {"width": w, "height": h, "codec": "gif"}
    if head[:2] == b"\xff\xd8":
        # JPEG: walk segments looking for a start-of-frame marker.
        i = 2
        while i + 9 < len(head):
            if head[i] != 0xFF:
                i += 1
                continue
            marker = head[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", head[i + 5:i + 9])
                return {"width": w, "height": h, "codec": "jpeg"}
            seg_len = struct.unpack(">H", head[i + 2:i + 4])[0]
            i += 2 + max(seg_len, 1)
        return {"codec": "jpeg"}
    return {}


def _probe_ffprobe(path: Path) -> dict:
    if not FFPROBE:
        return {}
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_streams", "-show_format",
             "-of", "json", str(path)],
            capture_output=True, timeout=30, check=True)
        info = json.loads(out.stdout.decode("utf-8", "replace"))
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return {}
    out_info: dict = {}
    fmt = info.get("format") or {}
    if fmt.get("duration"):
        try:
            out_info["duration"] = float(fmt["duration"])
        except (TypeError, ValueError):
            pass
    for stream in info.get("streams") or []:
        if stream.get("codec_type") == "video" and "width" not in out_info:
            out_info["width"] = stream.get("width")
            out_info["height"] = stream.get("height")
            out_info["codec"] = stream.get("codec_name")
        elif stream.get("codec_type") == "audio" and "audio_codec" not in out_info:
            out_info["audio_codec"] = stream.get("codec_name")
    return {k: v for k, v in out_info.items() if v is not None}


class LocalAssetStore:
    """AssetStore protocol implementation over a local directory."""

    def __init__(self, root: Optional[str | Path] = None):
        self.root = Path(root) if root else Path(settings.ASSET_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- writes --
    def save_bytes(self, data: bytes, project_id: str, media_type: str,
                   filename: str, source: str = "imported",
                   parent_asset_ids: Optional[list[str]] = None) -> Asset:
        asset = Asset(project_id=project_id, source=source, media_type=media_type,
                      status="PROCESSING",
                      parent_asset_ids=list(parent_asset_ids or []))
        asset.sha256 = _sha256(data)
        safe_name = os.path.basename(filename) or "blob.bin"
        asset.storage_key = f"{project_id}/{asset.asset_id}/{safe_name}"
        dest = self.root / asset.storage_key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        asset.metadata["size_bytes"] = len(data)
        asset.metadata.update(self.probe(asset.storage_key))
        asset.preview_key = self.make_preview(asset)
        asset.status = "READY"
        return asset

    def save_file(self, src_path: str, project_id: str, media_type: str,
                  filename: str, source: str = "imported",
                  parent_asset_ids: Optional[list[str]] = None) -> Asset:
        with open(src_path, "rb") as f:
            data = f.read()
        return self.save_bytes(data, project_id, media_type, filename,
                               source=source, parent_asset_ids=parent_asset_ids)

    # -------------------------------------------------------------- reads --
    def path_for(self, storage_key: str) -> str:
        return str(self.root / storage_key)

    def read_bytes(self, storage_key: str) -> bytes:
        with open(self.path_for(storage_key), "rb") as f:
            return f.read()

    # -------------------------------------------------------------- probe --
    def probe(self, storage_key: str) -> dict:
        path = Path(self.path_for(storage_key))
        if not path.exists():
            return {}
        ext = path.suffix.lower()
        if ext in _IMAGE_EXT:
            return _probe_image_header(path)
        if ext in _AV_EXT:
            return _probe_ffprobe(path)
        # Unknown type: try ffprobe anyway (it recognizes most containers).
        return _probe_ffprobe(path)

    # ------------------------------------------------------------ preview --
    def make_preview(self, asset: Asset) -> str:
        path = Path(self.path_for(asset.storage_key))
        if not path.exists():
            return ""
        if asset.media_type == "image":
            return asset.storage_key
        if asset.media_type == "video" and FFMPEG:
            preview_key = f"{asset.project_id}/{asset.asset_id}/preview.png"
            dest = self.root / preview_key
            try:
                subprocess.run(
                    [FFMPEG, "-y", "-v", "error", "-i", str(path),
                     "-frames:v", "1", str(dest)],
                    capture_output=True, timeout=60, check=True)
                return preview_key
            except (subprocess.SubprocessError, OSError):
                return ""
        return ""
