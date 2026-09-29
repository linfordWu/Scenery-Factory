# SPDX-License-Identifier: GPL-3.0-only
"""Vision judge: score a rendered clip against its ShotSpec.

Uses the multimodal ollama endpoint (gemma3:4b). Without ffmpeg on the host
(frames cannot be extracted) it degrades to file-level hard checks only and
marks the report uncertain.
"""
from __future__ import annotations

import base64
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from ...config import settings
from ...domain.schemas.core import ScoreReport, ShotSpec
from ...domain.video_constraints import SYSTEM_VIDEO_CONSTRAINTS
from ..asset_store.local import FFMPEG, FFPROBE, _probe_ffprobe
from ..text_model.client import TextModelClient, extract_json

N_FRAMES = 4  # 仅供外部参考；实际采样位置见 GemmaVisionJudge.FRAME_POSITIONS
ACCEPT_MIN = 0.75
REPAIR_MAX = 0.6

_JUDGE_INSTRUCTIONS = """\
You are a strict video quality judge for short drama shots.
You are given {n} frames sampled from one generated clip: the FIRST frame,
two middle frames and the LAST frame, in chronological order.

The shot must satisfy ALL of these required points:
{required}

It must NOT contain any of these forbidden elements:
{forbidden}

Key-object continuity contract (compare across ALL frames, especially
first vs last):
{object_states}

Object-continuity checks you MUST perform explicitly:
- object count: does the count of each key object stay constant across
  frames? (two identical plates / duplicated food => fail)
- support: is any object floating or hovering without contact/support?
- interaction: are hand-object grasps and contacts physically plausible?
- state arc: do the start_state and end_state hold in the first and last
  frame respectively, without being mixed into the same frame?
Report violations as issue tags: "object_popping" (object appears/vanishes
or count changes), "continuity_break" (start/end states mixed or broken),
"anatomy_error", "extra_person".

Score each dimension from 0.0 to 1.0:
- identity: characters match their reference / description
- action: the required action actually happens
- scene: setting/background matches the spec
- motion: a story-driven subject or prop movement AND a separate environment,
  prop, lighting or depth/parallax change are visible; camera movement alone
  does not count
- story_linkage: characters, landscape and props visibly respond to the same
  narrative event instead of moving independently or remaining decorative
- continuity: identity, clothes, location layout and key props stay consistent
- object_consistency: key-object counts stay constant, nothing floats,
  hand-object contacts hold, start/end states are not mixed
- artifact_free: no extra people, malformed anatomy/hands, object popping or
  disappearing, slideshow/static-image simulation
- text_ok: no clearly readable characters, letters, digits, subtitles,
  watermarks or logos overlaid on the scene. Decorative
  paintings/pictures on walls, fabric patterns, food textures and other
  in-world decorations are NOT text — do not penalize them. If you score
  text_ok below 0.6 you MUST also add an issue tag (garbled_text,
  watermark, logo, subtitle_artifact or on_screen_text) naming exactly
  where the text is and what it reads

Also report:
- verdict: "accept" | "accept_with_deviation" | "repair" | "reject" —
  use accept_with_deviation when the clip basically matches but has an
  acceptable deviation, and explain the deviation in "deviation"
- observation_confidence: "high" | "medium" | "low" — how clearly you
  can actually see the frames (low = blurry/dark/too short to judge)
- observed_end_state: one sentence describing the FINAL frame — character
  positions, posture, props, facing direction (used to chain the next shot)

Respond with ONE JSON object only:
{{"identity": 0-1, "action": 0-1, "scene": 0-1, "text_ok": 0-1,
  "motion": 0-1, "story_linkage": 0-1, "continuity": 0-1,
  "object_consistency": 0-1, "artifact_free": 0-1,
  "verdict": "accept|accept_with_deviation|repair|reject",
  "deviation": "str",
  "observation_confidence": "high|medium|low",
  "observed_end_state": "str",
  "issues": ["short failure tags"]}}"""


def _object_states_block(spec: ShotSpec) -> str:
    lines: list[str] = []
    for o in spec.object_states or []:
        if not isinstance(o, dict):
            continue
        name = str(o.get("name") or "").strip()
        if not name:
            continue
        count = str(o.get("count") or "").strip()
        start = str(o.get("start_state") or "").strip()
        end = str(o.get("end_state") or "").strip()
        lines.append(f"- {name}: count={count or 'unspecified'}, "
                     f"start={start or 'unspecified'}, end={end or 'unspecified'}")
    return "\n".join(lines) or "- (no key objects specified; still apply the checks)"


class GemmaVisionJudge:
    """VisionJudge protocol implementation."""

    def __init__(self, asset_store, base: Optional[str] = None,
                 model: Optional[str] = None, timeout: float = 300.0):
        self.asset_store = asset_store
        self.client = TextModelClient(
            base=base or settings.VISION_MODEL_BASE,
            model=model or settings.VISION_MODEL_NAME, timeout=timeout)

    # ------------------------------------------------------------ frames ---
    # 采样位置：首帧 + 两个中间帧 + 末帧。首/末帧对物体连续性检查
    # （数量恒定、初末态不混帧）至关重要，不能用纯均匀采样避开两端。
    FRAME_POSITIONS = (0.04, 0.36, 0.68, 0.96)

    def _extract_frames(self, video_path: str, duration: float) -> list[Path]:
        frames: list[Path] = []
        tmpdir = Path(tempfile.mkdtemp(prefix="svf_frames_"))
        span = duration if duration > 0 else 4.0
        for i, pos in enumerate(self.FRAME_POSITIONS):
            t = span * pos
            out = tmpdir / f"frame_{i}.png"
            try:
                subprocess.run(
                    [FFMPEG, "-y", "-v", "error", "-ss", f"{t:.2f}",
                     "-i", video_path, "-frames:v", "1", str(out)],
                    capture_output=True, timeout=60, check=True)
                frames.append(out)
            except (subprocess.SubprocessError, OSError):
                break
        return frames

    # ------------------------------------------------------------- score ---
    def score_video(self, video_key: str, spec: ShotSpec) -> ScoreReport:
        rubric = spec.acceptance.rubric_version
        path = Path(self.asset_store.path_for(video_key))
        exists = path.exists() and path.stat().st_size > 0
        hard: dict[str, bool] = {"decodable": bool(exists)}
        if not exists:
            return ScoreReport(run_id="", verdict="repair", hard_checks=hard,
                               uncertain=False, rubric_version=rubric,
                               evidence=[{"tag": "missing_file"}])

        probe: dict[str, Any] = {}
        if FFPROBE:
            probe = _probe_ffprobe(path)
            if probe.get("duration"):
                hard["duration_ok"] = abs(probe["duration"] - spec.duration_s) <= 1.0
            if probe.get("width"):
                hard["resolution_ok"] = probe["width"] >= 512

        if not FFMPEG:
            # No frame extraction on this host: hard checks only, semantics unknown.
            return ScoreReport(run_id="", verdict="uncertain", hard_checks=hard,
                               scores={}, uncertain=True, rubric_version=rubric,
                               evidence=[{"tag": "no_ffmpeg_semantics_skipped"}])

        duration = float(probe.get("duration") or spec.duration_s)
        frames = self._extract_frames(str(path), duration)
        if not frames:
            hard["decodable"] = False
            return ScoreReport(run_id="", verdict="repair", hard_checks=hard,
                               uncertain=False, rubric_version=rubric,
                               evidence=[{"tag": "frame_extract_failed"}])

        content: list[dict] = [{"type": "text", "text":
                                _JUDGE_INSTRUCTIONS.format(
                                    n=len(frames),
                                    required="\n".join(f"- {r}" for r in spec.acceptance.required) or "- (none)",
                                    forbidden="\n".join(f"- {f}" for f in spec.acceptance.forbidden) or "- (none)",
                                    object_states=_object_states_block(spec),
                                ) + "\n" + SYSTEM_VIDEO_CONSTRAINTS}]
        for frame in frames:
            b64 = base64.b64encode(frame.read_bytes()).decode("ascii")
            content.append({"type": "image_url", "image_url":
                            {"url": f"data:image/png;base64,{b64}"}})

        scores: dict[str, float] = {}
        issues: list[str] = []
        parse_failed = False
        model_verdict = ""
        deviation = ""
        obs_confidence = "high"
        observed_end_state = ""
        try:
            reply = self.client.chat([{"role": "user", "content": content}],
                                     max_tokens=512, temperature=0.1)
            parsed = extract_json(reply)
            # 基础字段是旧版评审器已稳定输出的格式。
            for key in ("identity", "action", "scene", "text_ok"):
                scores[key] = max(0.0, min(1.0, float(parsed.get(key, 0.0))))
            # 旧部署的评审模型可能尚未返回 motion；兼容期间用 action 分数
            # 保守代替，并依靠 static_image_simulation 标签一票否决。
            scores["motion"] = max(0.0, min(1.0, float(
                parsed.get("motion", scores["action"]))))
            # 新维度可由新版评审器直接给分；没有时用对应的旧维度作保守回退，
            # 使升级不会把旧 JSON 回复一律降为 repair。
            scores["story_linkage"] = max(0.0, min(1.0, float(
                parsed.get("story_linkage", scores["action"]))))
            scores["continuity"] = max(0.0, min(1.0, float(
                parsed.get("continuity", min(scores["identity"], scores["scene"])))))
            # 物体一致性：新版维度；旧评审模型未返回时回退 continuity
            scores["object_consistency"] = max(0.0, min(1.0, float(
                parsed.get("object_consistency", scores["continuity"]))))
            scores["artifact_free"] = max(0.0, min(1.0, float(
                parsed.get("artifact_free", scores["text_ok"]))))
            issues = [str(i) for i in parsed.get("issues") or []]
            model_verdict = str(parsed.get("verdict") or "")
            deviation = str(parsed.get("deviation") or "")
            obs_confidence = str(parsed.get("observation_confidence") or "high")
            if obs_confidence not in ("low", "medium", "high"):
                obs_confidence = "high"
            observed_end_state = str(parsed.get("observed_end_state") or "")
        except (ValueError, KeyError, TypeError):
            parse_failed = True

        hard_passed = all(hard.values())
        # 文字类失败必须有标签佐证(说出文字位置/内容)才算数:
        # gemma3 级小模型对花纹/挂画/蒸汽的 text_ok 裸零分假阳性率很高
        text_tags = {"garbled_text", "watermark", "logo", "subtitle_artifact",
                     "on_screen_text"}
        text_confirmed = any(i in text_tags for i in issues)
        hard_visual_failure = any(issue in (
            "static_image_simulation", "camera_motion_only", "story_disconnected",
            "continuity_break", "identity_drift", "extra_person", "anatomy_error",
            "object_popping",
        ) for issue in issues) or text_confirmed
        # 无佐证的 text_ok 低分降级为提示,不参与一票否决
        effective = {k: v for k, v in scores.items()
                     if not (k == "text_ok" and not text_confirmed)}
        if hard_visual_failure:
            verdict = "repair"
        elif parse_failed:
            verdict = "uncertain"
        elif model_verdict == "accept_with_deviation" \
                and all(v >= REPAIR_MAX for v in effective.values()):
            # 基本符合但有可接受偏差：放行但记录偏差说明
            verdict = "accept_with_deviation"
        elif any(v < REPAIR_MAX for v in effective.values()):
            verdict = "repair"
        elif all(v >= ACCEPT_MIN for v in effective.values()):
            verdict = "accept"
        else:
            verdict = "uncertain"

        evidence = [{"tag": t} for t in issues]
        return ScoreReport(run_id="", verdict=verdict, hard_checks=hard,
                           scores=scores, evidence=evidence,
                           uncertain=(verdict == "uncertain"),
                           rubric_version=rubric,
                           deviation=deviation,
                           observation_confidence=obs_confidence,
                           observed_end_state=observed_end_state)
