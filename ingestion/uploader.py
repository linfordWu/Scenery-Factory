# SPDX-License-Identifier: GPL-3.0-only
"""Chunked upload sessions (ingestion).

Parts are written to DATA_DIR/uploads/{upload_id}/part_{n}; complete()
concatenates them in order, verifies size/sha256, hands the file to the
AssetStore, records the Asset, and removes the part files.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Optional

from ..adapters.contracts import AssetStore
from ..domain.repositories.store import Store
from ..domain.schemas.core import Asset, UploadSession

DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024

_MEDIA_BY_EXT = {
    ".mp4": "video", ".mov": "video", ".webm": "video", ".mkv": "video",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".flac": "audio",
    ".txt": "text", ".srt": "text", ".json": "text", ".md": "text",
}


def guess_media_type(filename: str) -> str:
    return _MEDIA_BY_EXT.get(Path(filename).suffix.lower(), "other")


class UploadError(Exception):
    """Raised when an upload session cannot accept parts or fails validation."""


class Uploader:
    def __init__(self, store: Store, asset_store: AssetStore, data_dir: Path | str):
        self.store = store
        self.asset_store = asset_store
        self.uploads_dir = Path(data_dir) / "uploads"
        self.uploads_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- session --
    def create(self, project_id: str, filename: str, total_size: int,
               chunk_size: int = DEFAULT_CHUNK_SIZE) -> UploadSession:
        session = UploadSession(project_id=project_id, filename=filename,
                                total_size=int(total_size), chunk_size=int(chunk_size))
        (self.uploads_dir / session.upload_id).mkdir(parents=True, exist_ok=True)
        self.store.put("uploads", session)
        return session

    def get(self, upload_id: str) -> Optional[UploadSession]:
        return self.store.get("uploads", upload_id)

    def _part_path(self, upload_id: str, part_no: int) -> Path:
        return self.uploads_dir / upload_id / f"part_{part_no}"

    # --------------------------------------------------------------- parts --
    def write_part(self, upload_id: str, part_no: int, data: bytes) -> UploadSession:
        session = self.store.get("uploads", upload_id)
        if session is None:
            raise KeyError(upload_id)
        if session.status == "READY":
            return session          # re-sent part after completion: ignore
        if session.status != "UPLOADING":
            raise UploadError(f"upload {upload_id} not accepting parts (status={session.status})")
        path = self._part_path(upload_id, part_no)
        if not (path.exists() and path.stat().st_size == len(data)):
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)       # atomic-ish; same-size re-send is a no-op
        if part_no not in session.received_parts:
            session.received_parts.append(part_no)
            session.received_parts.sort()
            self.store.put("uploads", session)
        return session

    # ------------------------------------------------------------ complete --
    def complete(self, upload_id: str, sha256: Optional[str] = None,
                 media_type: Optional[str] = None) -> Asset:
        session = self.store.get("uploads", upload_id)
        if session is None:
            raise KeyError(upload_id)
        if session.status == "READY" and session.asset_id:
            return self.store.get("assets", session.asset_id)   # idempotent

        part_dir = self.uploads_dir / upload_id
        part_files = sorted(part_dir.glob("part_*"),
                            key=lambda p: int(p.name.rsplit("_", 1)[1]))
        if not part_files:
            raise UploadError(f"upload {upload_id} has no parts")

        session.status = "VALIDATING"
        self.store.put("uploads", session)
        try:
            digest = hashlib.sha256()
            total = 0
            assembled = part_dir / "_assembled"
            with open(assembled, "wb") as out:
                for pf in part_files:
                    with open(pf, "rb") as src:
                        while True:
                            buf = src.read(1 << 20)
                            if not buf:
                                break
                            digest.update(buf)
                            total += len(buf)
                            out.write(buf)
            if total != session.total_size:
                raise UploadError(
                    f"size mismatch: got {total}, expected {session.total_size}")
            if sha256 and digest.hexdigest().lower() != sha256.lower():
                raise UploadError("sha256 mismatch")
        except Exception:
            session.status = "FAILED"
            self.store.put("uploads", session)
            raise

        asset = self.asset_store.save_file(
            str(assembled), project_id=session.project_id,
            media_type=media_type or guess_media_type(session.filename),
            filename=session.filename, source="imported")
        asset.sha256 = digest.hexdigest()
        asset.status = "READY"
        asset.metadata["size"] = total
        asset.metadata["original_filename"] = session.filename
        self.store.put("assets", asset)

        session.status = "READY"
        session.asset_id = asset.asset_id
        self.store.put("uploads", session)
        shutil.rmtree(part_dir, ignore_errors=True)
        return asset
