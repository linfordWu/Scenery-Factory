# SPDX-License-Identifier: GPL-3.0-only
"""Adapter contracts. Implementations live in the sibling modules; anything that
talks to the outside world (models, ComfyUI, filesystem) goes through these."""
from __future__ import annotations

from typing import Any, Callable, Optional, Protocol

from ..domain.schemas.core import Asset, DecisionAdvice, ScoreReport, ShotSpec

ProgressFn = Callable[[int, int, str], None]   # current, total, summary


class TextModel(Protocol):
    def chat(self, messages: list[dict], max_tokens: int = 2048,
             temperature: float = 0.7) -> str: ...
    def chat_json(self, instructions: str, payload: dict,
                  schema_hint: str, max_tokens: int = 2048) -> dict: ...


class ImageModel(Protocol):
    def generate(self, prompt: str, out_key: str, width: int = 1080,
                 height: int = 1920) -> str:
        """Generate an image, return its storage key. Raise RuntimeError when not ready."""
        ...


class ComfyRenderer(Protocol):
    def render_shot(self, prompt_spec: str, ref_image_keys: list[str], seed: int,
                    duration_s: int = 5, aspect_ratio: str = "9:16",
                    on_progress: Optional[ProgressFn] = None) -> str:
        """Render one shot video, return the video storage key."""
        ...
    def normalize_clip(self, source_key: str, duration_s: int,
                       aspect_ratio: str) -> str:
        """Normalize an imported clip for direct reuse, return new storage key."""
        ...


class VisionJudge(Protocol):
    def score_video(self, video_key: str, spec: ShotSpec) -> ScoreReport: ...


class DecisionPort(Protocol):
    def advise(self, state: dict, allowed_actions: list[str]) -> DecisionAdvice:
        """Shadow-mode advice: never blocks, never raises."""
        ...


class AssetStore(Protocol):
    def save_bytes(self, data: bytes, project_id: str, media_type: str,
                   filename: str, source: str = "imported",
                   parent_asset_ids: Optional[list[str]] = None) -> Asset: ...
    def save_file(self, src_path: str, project_id: str, media_type: str,
                  filename: str, source: str = "imported",
                  parent_asset_ids: Optional[list[str]] = None) -> Asset: ...
    def path_for(self, storage_key: str) -> str: ...
    def read_bytes(self, storage_key: str) -> bytes: ...
    def probe(self, storage_key: str) -> dict: ...
    def make_preview(self, asset: Asset) -> str: ...
