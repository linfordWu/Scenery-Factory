# SPDX-License-Identifier: GPL-3.0-only
"""Run state machine (doc section 08). All transitions validated here.

PLANNED → ASSET_READY → PROMPT_READY → QUEUED → RENDERING → GENERATED → SCORING → ACCEPTED
reuse path:  ASSET_READY → NORMALIZING → SCORING → ACCEPTED
repair:      SCORING → REPAIRING → PROMPT_READY | ASSET_READY
budget out / uncertain → HUMAN_REVIEW ; infra error → RETRY_WAIT / FAILED
control:     any active → CANCEL_REQUESTED → CANCELLED
"""
from __future__ import annotations

from ..schemas.core import RunState

TRANSITIONS: dict[str, set[str]] = {
    "PLANNED": {"ASSET_READY", "CANCEL_REQUESTED", "FAILED"},
    "ASSET_READY": {"PROMPT_READY", "NORMALIZING", "CANCEL_REQUESTED", "FAILED"},
    "PROMPT_READY": {"QUEUED", "CANCEL_REQUESTED", "FAILED"},
    "QUEUED": {"RENDERING", "CANCEL_REQUESTED", "RETRY_WAIT", "FAILED"},
    "RENDERING": {"GENERATED", "CANCEL_REQUESTED", "RETRY_WAIT", "FAILED"},
    "GENERATED": {"SCORING", "FAILED"},
    "NORMALIZING": {"SCORING", "RETRY_WAIT", "FAILED"},
    "SCORING": {"ACCEPTED", "REPAIRING", "HUMAN_REVIEW", "RETRY_WAIT", "FAILED"},
    "REPAIRING": {"PROMPT_READY", "ASSET_READY", "HUMAN_REVIEW", "FAILED"},
    "ACCEPTED": set(),                      # terminal
    "HUMAN_REVIEW": {"REPAIRING", "ACCEPTED", "FAILED", "CANCELLED"},
    "RETRY_WAIT": {"QUEUED", "FAILED", "CANCEL_REQUESTED"},
    "CANCEL_REQUESTED": {"CANCELLED", "FAILED"},
    "CANCELLED": set(),                     # terminal
    "FAILED": set(),                        # terminal
}

TERMINAL = {"ACCEPTED", "CANCELLED", "FAILED"}
ACTIVE = set(TRANSITIONS) - TERMINAL


def can_transition(src: RunState, dst: RunState) -> bool:
    return dst in TRANSITIONS.get(src, set())


def transition(src: RunState, dst: RunState) -> RunState:
    if not can_transition(src, dst):
        raise ValueError(f"illegal transition {src} -> {dst}")
    return dst
