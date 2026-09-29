# SPDX-License-Identifier: GPL-3.0-only
"""Laya shadow-mode decision adapter.

Runs the laya System 1 model in a separate interpreter (its own venv) so the
factory process never imports torch. Shadow mode: any failure, timeout, or
low confidence falls back to rule-based policy and never raises.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import Optional

from ...config import settings
from ...domain.schemas.core import DecisionAdvice

_MIN_CONFIDENCE = 0.5
_TIMEOUT_S = 30.0

_CHILD_SCRIPT = r"""
import json, os, sys

state = json.loads(sys.argv[1])
actions = json.loads(sys.argv[2])

import laya
agent = laya.load(os.environ["LAYA_MODEL_DIR"], device="cpu")
questions = {"next_action": {
    "type": "choice",
    "instructions": "Given this run state of a short-video factory, "
                    "choose the single best next action.",
    "criteria": {a: a for a in actions},
}}
out = agent.system_one(state, questions)
ans = out["answers"]["next_action"]
print(json.dumps({"selected_action": ans["choice"],
                  "confidence": ans["confidence"]}))
"""


class LayaShadowDecision:
    """DecisionPort protocol implementation (shadow mode: never raises)."""

    def __init__(self, python: Optional[str] = None,
                 model_dir: Optional[str] = None,
                 timeout_s: float = _TIMEOUT_S,
                 min_confidence: float = _MIN_CONFIDENCE):
        self.python = python or settings.LAYA_PYTHON
        self.model_dir = model_dir or settings.LAYA_MODEL_DIR
        self.timeout_s = timeout_s
        self.min_confidence = min_confidence

    def _fallback(self, allowed_actions: list[str], state: dict) -> DecisionAdvice:
        return DecisionAdvice(
            state_hash=hashlib.sha256(
                json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()[:16],
            allowed_actions=list(allowed_actions),
            selected_action="",
            confidence=0.0,
            accepted_by_policy=False,
            fallback="rules",
        )

    def advise(self, state: dict, allowed_actions: list[str]) -> DecisionAdvice:
        if not allowed_actions:
            return self._fallback(allowed_actions, state)
        env = dict(os.environ)
        env["TORCH_DISABLE_NATIVE_JIT"] = "1"
        env["LAYA_MODEL_DIR"] = self.model_dir
        try:
            proc = subprocess.run(
                [self.python, "-c", _CHILD_SCRIPT,
                 json.dumps(state, default=str),
                 json.dumps(list(allowed_actions))],
                capture_output=True, timeout=self.timeout_s, env=env)
            if proc.returncode != 0:
                return self._fallback(allowed_actions, state)
            # The child prints laya load logs before the JSON; take the last line.
            last = proc.stdout.decode("utf-8", "replace").strip().splitlines()[-1]
            out = json.loads(last)
            action = str(out["selected_action"])
            confidence = float(out["confidence"])
        except (subprocess.SubprocessError, OSError, ValueError,
                KeyError, IndexError, TypeError):
            return self._fallback(allowed_actions, state)

        if action not in allowed_actions or confidence < self.min_confidence:
            return self._fallback(allowed_actions, state)
        return DecisionAdvice(
            state_hash=hashlib.sha256(
                json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()[:16],
            allowed_actions=list(allowed_actions),
            selected_action=action,
            confidence=confidence,
            accepted_by_policy=True,
            fallback=None,
        )
