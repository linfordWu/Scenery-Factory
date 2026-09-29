# SPDX-License-Identifier: GPL-3.0-only
"""Reviewer agent: project-level coherence review over accepted shots.

对每个已验收镜头抽中间帧，分批发给视觉模型（gemma3，OpenAI 兼容），
检测剧情连贯性 / 人物一致性 / 穿帮 / 非预期画面 / 乱码文字，
输出 DirectorReview 报告存入 "director_reviews" 表。
"""
from __future__ import annotations

import base64
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from ..adapters.asset_store.local import FFMPEG, _probe_ffprobe
from ..adapters.text_model.client import TextModelClient, extract_json
from ..config import settings
from ..domain.schemas.core import (
    DirectorReview, Event, Project, ReviewIssue, Shot,
)

log = logging.getLogger(__name__)

_INSTRUCTIONS = """\
你是短剧审核员 agent。下面按剧情顺序给你一批镜头的中间帧（每帧标注了 shot_id），
以及故事梗概和角色/地点的固定外观描述。

故事梗概: {brief}
风格: {style}
角色/地点登记:
{characters}

本批镜头（按剧情顺序）:
{shots}

从以下维度逐镜头检查，输出问题清单（没有问题就留空）:
- story: 剧情连贯性问题
- character: 人物外观与登记描述不一致
- goof: 穿帮/不合常理
- unexpected: 非预期行为/画面异常
- text: 画面中出现乱码文字/字幕/水印/logo

只输出一个 JSON 对象:
{{"story_coherence": 0-1, "summary": str,
  "issues": [{{"shot_id": str, "type": "story|character|goof|unexpected|text",
               "severity": "low|mid|high", "detail": str}}]}}"""


class ReviewerAgent:
    """项目级连贯性审核。"""

    def __init__(self, store, asset_store, client=None,
                 batch_size: Optional[int] = None):
        self.store = store
        self.asset_store = asset_store
        self.client = client or TextModelClient(
            base=settings.VISION_MODEL_BASE, model=settings.VISION_MODEL_NAME)
        self.batch_size = batch_size or settings.REVIEW_FRAME_BATCH

    # ------------------------------------------------------------- frames --
    def _extract_middle_frame(self, video_path: str, tmpdir: Path,
                              name: str) -> Optional[Path]:
        """用 ffmpeg 抽视频中间一帧；失败返回 None。"""
        if not FFMPEG:
            return None
        probe = _probe_ffprobe(Path(video_path))
        duration = float(probe.get("duration") or 0)
        t = max(duration / 2, 0.0)
        out = tmpdir / f"{name}.png"
        try:
            subprocess.run(
                [FFMPEG, "-y", "-v", "error", "-ss", f"{t:.2f}",
                 "-i", video_path, "-frames:v", "1", str(out)],
                capture_output=True, timeout=60, check=True)
            return out
        except (subprocess.SubprocessError, OSError):
            return None

    # ------------------------------------------------------------- review --
    def review(self, project: Project) -> DirectorReview:
        pid = project.project_id
        characters = sorted(self.store.all("characters", project_id=pid),
                            key=lambda c: c.created_at)
        scene_order = {s.scene_id: s.order
                       for s in self.store.all("scenes", project_id=pid)}
        shots = sorted(self.store.all("shots", project_id=pid),
                       key=lambda s: (scene_order.get(s.scene_id, 0), s.order))
        shots = [s for s in shots if s.accepted_run_id]

        # 抽帧：(shot, run_id, frame_path)
        frames: list[tuple[Shot, str, Path]] = []
        tmpdir = Path(tempfile.mkdtemp(prefix="svf_review_"))
        for shot in shots:
            run = self.store.get("runs", shot.accepted_run_id)
            if run is None:
                continue
            video = next((self.store.get("assets", aid)
                          for aid in run.candidate_asset_ids
                          if self.store.get("assets", aid)
                          and self.store.get("assets", aid).media_type == "video"),
                         None)
            if video is None or not video.storage_key:
                continue
            frame = self._extract_middle_frame(
                self.asset_store.path_for(video.storage_key), tmpdir, shot.shot_id)
            if frame is not None:
                frames.append((shot, run.run_id, frame))

        coherences: list[float] = []
        summaries: list[str] = []
        issues: list[ReviewIssue] = []
        raws: list[str] = []
        for i in range(0, len(frames), self.batch_size):
            batch = frames[i:i + self.batch_size]
            result, raw = self._review_batch(project, characters, batch)
            if raw:
                raws.append(raw)
            if result is None:
                continue
            try:
                coherences.append(max(0.0, min(1.0,
                                  float(result.get("story_coherence", 0.0)))))
            except (TypeError, ValueError):
                pass
            if result.get("summary"):
                summaries.append(str(result["summary"]))
            run_by_shot = {shot.shot_id: rid for shot, rid, _ in batch}
            for item in result.get("issues") or []:
                issues.append(_parse_issue(item, run_by_shot))

        report = DirectorReview(
            project_id=pid,
            story_coherence=(sum(coherences) / len(coherences)
                             if coherences else 0.0),
            summary="; ".join(summaries),
            issues=issues,
            raw="\n---\n".join(raws))
        self.store.put("director_reviews", report)
        self.store.append_event(Event(
            project_id=pid, type="review.completed", actor="reviewer",
            summary=f"{len(shots)} shots reviewed, {len(issues)} issues"))
        return report

    def _review_batch(self, project: Project, characters: list,
                      batch: list[tuple[Shot, str, Path]]) -> tuple[Optional[dict], str]:
        """返回 (解析结果, 解析失败时的原始回复)。"""
        char_lines = "\n".join(
            f"- {c.name}({c.kind}): {c.description}" for c in characters) or "- (无)"
        shot_lines = "\n".join(
            f"- {shot.shot_id}: {shot.spec.action} 台词: {shot.spec.dialogue}"
            for shot, _, _ in batch)
        content: list[dict] = [{"type": "text", "text": _INSTRUCTIONS.format(
            brief=project.brief, style=project.style,
            characters=char_lines, shots=shot_lines)}]
        for shot, _, frame in batch:
            b64 = base64.b64encode(frame.read_bytes()).decode("ascii")
            content.append({"type": "text", "text": f"frame of {shot.shot_id}:"})
            content.append({"type": "image_url", "image_url":
                            {"url": f"data:image/png;base64,{b64}"}})
        reply = ""
        try:
            reply = self.client.chat([{"role": "user", "content": content}],
                                     max_tokens=2048, temperature=0.1)
            return extract_json(reply), ""
        except Exception as exc:
            log.warning("review batch parse failed: %s", exc)
            return None, str(reply or exc)[:2000]


def _parse_issue(item: dict, run_by_shot: dict[str, str]) -> ReviewIssue:
    """容错解析单条 issue。"""
    if not isinstance(item, dict):
        return ReviewIssue(type="unexpected", detail=str(item)[:300])
    severity = str(item.get("severity") or "low")
    if severity not in ("low", "mid", "high"):
        severity = "low"
    shot_id = str(item.get("shot_id") or "")
    return ReviewIssue(shot_id=shot_id, run_id=run_by_shot.get(shot_id, ""),
                       type=str(item.get("type") or "unexpected"),
                       severity=severity,
                       detail=str(item.get("detail") or "")[:500])
