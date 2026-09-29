# SPDX-License-Identifier: GPL-3.0-only
"""Director agent: script + characters -> per-scene ShotSpec lists.

Shot budget follows the 5-second rule: one shot per ~5s of target runtime.
Every LLM-produced shot is validated through the ShotSpec pydantic model.
"""
from __future__ import annotations

from typing import Any, Optional

from ..domain.schemas.core import Acceptance, ShotSpec, new_id
from ..domain.video_constraints import SYSTEM_VIDEO_CONSTRAINTS, constrained_acceptance

SHOT_SECONDS = 5

_SCHEMA_HINT = (
    '{"shots": [{"scene": str, "duration_s": int, '
    '"narrative_beat": str, "action": str, "dialogue": str, '
    '"motion_contract": {"start_state": str, "primary_motion": str, '
    '"secondary_motion": str, "end_state": str, "camera_motion": str}, '
    '"camera": {"shot": str, "movement": str}, '
    '"acceptance": {"required": [str], "forbidden": [str]}}]}'
)


class DirectorAgent:
    def __init__(self, text_model, store=None):
        self.text_model = text_model
        self.store = store

    def storyboard(self, script: str, scenes: list[dict],
                   characters: Optional[list[dict]] = None,
                   duration_s: int = 60,
                   aspect_ratio: str = "9:16") -> list[ShotSpec]:
        budget = max(1, duration_s // SHOT_SECONDS)
        instructions = (
            "You are the director of a short-drama factory. Break the script "
            f"into at most {budget} shots (about {SHOT_SECONDS}s each). For every "
            "shot give: which scene it belongs to, the narrative beat, the visible "
            "action, the spoken dialogue line (may be empty), a motion contract "
            "(start state, character/prop motion, environment/prop motion, end state), "
            "camera framing/movement, and "
            "acceptance.required as concrete checkable points for a vision judge."
            " " + SYSTEM_VIDEO_CONSTRAINTS
        )
        out = self.text_model.chat_json(
            instructions,
            {"script": script, "scenes": scenes,
             "characters": characters or [], "shot_budget": budget,
             "aspect_ratio": aspect_ratio},
            _SCHEMA_HINT, max_tokens=4096)
        return self._validate(out.get("shots") or [], aspect_ratio)

    def _validate(self, raw_shots: list[dict], aspect_ratio: str) -> list[ShotSpec]:
        specs: list[ShotSpec] = []
        for raw in raw_shots:
            if not isinstance(raw, dict):
                continue
            acceptance_raw = raw.get("acceptance") or {}
            camera_raw = raw.get("camera") or {}
            spec = ShotSpec(
                shot_id=new_id("shot"),
                duration_s=int(raw.get("duration_s") or SHOT_SECONDS),
                # 画幅以项目参数为准,LLM 逐镜给的值不信任(会混出竖屏镜头)
                aspect_ratio=aspect_ratio,
                action=str(raw.get("action") or ""),
                dialogue=str(raw.get("dialogue") or ""),
                narrative_beat=str(raw.get("narrative_beat") or ""),
                motion_contract={str(k): str(v) for k, v
                                 in (raw.get("motion_contract") or {}).items()},
                camera={str(k): str(v) for k, v in camera_raw.items()},
                acceptance=constrained_acceptance(Acceptance(
                    required=[str(r) for r in acceptance_raw.get("required") or []],
                    forbidden=[str(f) for f in acceptance_raw.get("forbidden") or []],
                )),
                continuity={"scene": str(raw.get("scene") or "")},
            )
            specs.append(spec)
        return specs

    def storyboard_into_store(self, project_id: str, script: str,
                              scenes: list[dict], **kwargs: Any) -> list[ShotSpec]:
        specs = self.storyboard(script, scenes, **kwargs)
        if self.store is not None:
            from ..domain.schemas.core import Shot
            scene_ids = {s.get("title"): s.get("scene_id", "") for s in scenes}
            for i, spec in enumerate(specs):
                self.store.put("shots", Shot(
                    shot_id=spec.shot_id,
                    scene_id=scene_ids.get(spec.continuity.get("scene", ""), ""),
                    project_id=project_id, order=i, spec=spec))
        return specs
