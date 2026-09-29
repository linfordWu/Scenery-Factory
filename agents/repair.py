# SPDX-License-Identifier: GPL-3.0-only
"""Repair agent: ScoreReport + Run -> RepairPlan.

Failure-tag -> repair-action mapping follows doc section 08: hard/infra
failures requeue; identity failures rebind references; everything else is a
prompt rewrite from PROMPT_READY.
"""
from __future__ import annotations

from ..domain.schemas.core import RepairPlan, Run, ScoreReport

# failure tag -> (target, action, invalidate_from)
REPAIR_MAP: dict[str, tuple[str, str, str]] = {
    "missing_file": ("infra", "requeue_render", "QUEUED"),
    "frame_extract_failed": ("infra", "requeue_render", "QUEUED"),
    "decode_error": ("infra", "requeue_render", "QUEUED"),
    "render_error": ("infra", "requeue_render", "QUEUED"),
    "duration_mismatch": ("shot_spec", "adjust_duration", "PROMPT_READY"),
    "resolution_mismatch": ("shot_spec", "adjust_resolution", "PROMPT_READY"),
    "identity_drift": ("reference_assets", "rebind_reference", "ASSET_READY"),
    "character_mismatch": ("reference_assets", "rebind_reference", "ASSET_READY"),
    "identity": ("reference_assets", "rebind_reference", "ASSET_READY"),
    "action_missing": ("prompt", "rewrite_action", "PROMPT_READY"),
    "action": ("prompt", "rewrite_action", "PROMPT_READY"),
    "static_image_simulation": ("prompt", "rewrite_motion_contract", "PROMPT_READY"),
    "camera_motion_only": ("prompt", "rewrite_motion_contract", "PROMPT_READY"),
    "story_disconnected": ("prompt", "rewrite_motion_contract", "PROMPT_READY"),
    "story_linkage": ("prompt", "rewrite_motion_contract", "PROMPT_READY"),
    "continuity_break": ("reference_assets", "rebind_reference", "ASSET_READY"),
    "continuity": ("reference_assets", "rebind_reference", "ASSET_READY"),
    "extra_person": ("prompt", "lock_cast_and_scene", "PROMPT_READY"),
    "anatomy_error": ("prompt", "simplify_subject_action", "PROMPT_READY"),
    "artifact_free": ("prompt", "forbid_visual_artifacts", "PROMPT_READY"),
    "object_popping": ("prompt", "lock_key_props", "PROMPT_READY"),
    "scene_mismatch": ("prompt", "rewrite_scene", "PROMPT_READY"),
    "scene": ("prompt", "rewrite_scene", "PROMPT_READY"),
    "garbled_text": ("prompt", "simplify_text", "PROMPT_READY"),
    "text_ok": ("prompt", "simplify_text", "PROMPT_READY"),
    "watermark": ("prompt", "forbid_watermark", "PROMPT_READY"),
    "uncertain": ("prompt", "human_review", "PROMPT_READY"),
}

_DEFAULT = ("prompt", "rewrite_prompt", "PROMPT_READY")


def failure_tags(score: ScoreReport) -> list[str]:
    """Ordered failure labels derived from a ScoreReport."""
    tags: list[str] = []
    for item in score.evidence or []:
        tag = str(item.get("tag", "")) if isinstance(item, dict) else str(item)
        if tag and tag not in tags:
            tags.append(tag)
    for check, ok in (score.hard_checks or {}).items():
        if not ok:
            label = {
                "decodable": "decode_error",
                "duration_ok": "duration_mismatch",
                "resolution_ok": "resolution_mismatch",
            }.get(check, check)
            if label not in tags:
                tags.append(label)
    for key, value in (score.scores or {}).items():
        if value < 0.6 and key not in tags:
            tags.append(key)
    if not tags and score.uncertain:
        tags.append("uncertain")
    return tags


class RepairAgent:
    """Deterministic mapping first; the text model is only a fallback for
    unknown failure tags."""

    def __init__(self, text_model=None, store=None):
        self.text_model = text_model
        self.store = store

    def plan(self, score: ScoreReport, run: Run) -> RepairPlan:
        tags = failure_tags(score)
        tag = next((t for t in tags if t in REPAIR_MAP), tags[0] if tags else "")
        if tag in REPAIR_MAP:
            target, action, invalidate_from = REPAIR_MAP[tag]
            detail = f"failure tag '{tag}' from verdict={score.verdict}"
        elif tag:
            target, action, invalidate_from = _DEFAULT
            detail = f"unmapped failure tag '{tag}'; defaulting to prompt rewrite"
        else:
            target, action, invalidate_from = _DEFAULT
            detail = f"no failure tags; verdict={score.verdict}"
        plan = RepairPlan(run_id=run.run_id, target=target, action=action,
                          detail=detail, invalidate_from=invalidate_from)
        if self.store is not None:
            self.store.put("repair_plans", plan)
        return plan
