# SPDX-License-Identifier: GPL-3.0-only
"""Technical quality gates for rendered / imported clips.

Always checks the file layer (readable, non-empty). When ffprobe/ffmpeg are
available it additionally checks resolution, duration, streams, black frames
(blackdetect) and frozen frames (mpdecimate). Without them the media-level
checks are skipped and the degradation is recorded in the evidence list, so
callers can tell "passed" from "not checked".
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_BLACK_RE = re.compile(r"black_duration:([\d.]+)")
_FRAME_RE = re.compile(r"\b(drop|keep)\s+pts")


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def run_checks(path: str | Path, expect_duration_s: Optional[int] = None) -> dict:
    """Return {"hard_checks": {name: bool}, "evidence": [{kind, note, ...}]}.

    A hard check that could not run (missing tool, unreadable media) is left
    out of hard_checks rather than failed, and an evidence entry explains why.
    """
    hard: dict[str, bool] = {}
    evidence: list[dict] = []
    p = Path(path)

    # ---------------------------------------------------------- file layer --
    readable = p.is_file() and os.access(p, os.R_OK)
    hard["file_readable"] = readable
    if not readable:
        evidence.append({"kind": "file", "note": f"cannot read {p}"})
        return {"hard_checks": hard, "evidence": evidence}
    size = p.stat().st_size
    hard["size_nonzero"] = size > 0
    evidence.append({"kind": "file", "note": f"size_bytes={size}"})
    if size == 0:
        return {"hard_checks": hard, "evidence": evidence}

    # --------------------------------------------------------- media layer --
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not ffprobe:
        evidence.append({"kind": "degraded",
                         "note": "ffprobe not available; media-level checks skipped"})
        return {"hard_checks": hard, "evidence": evidence}

    try:
        probe = _run([ffprobe, "-v", "error", "-print_format", "json",
                      "-show_format", "-show_streams", str(p)], timeout=120)
        info = json.loads(probe.stdout) if probe.returncode == 0 else None
    except Exception as exc:
        info = None
        log.warning("ffprobe failed for %s: %s", p, exc)
    if not info:
        evidence.append({"kind": "degraded",
                         "note": "ffprobe could not parse file; media-level checks skipped"})
        return {"hard_checks": hard, "evidence": evidence}

    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    hard["has_video_stream"] = video is not None
    if video:
        w, h = int(video.get("width") or 0), int(video.get("height") or 0)
        hard["resolution_ok"] = w >= 256 and h >= 256
        evidence.append({"kind": "probe",
                         "note": f"video {w}x{h} codec={video.get('codec_name')}"})
    duration = 0.0
    try:
        duration = float(info.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        pass
    dur_ok = duration > 0.1
    if dur_ok and expect_duration_s:
        dur_ok = abs(duration - expect_duration_s) <= max(1.0, 0.3 * expect_duration_s)
    hard["duration_ok"] = dur_ok
    evidence.append({"kind": "probe", "note": f"duration_s={duration:.2f}"})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    evidence.append({"kind": "probe",
                     "note": f"audio_stream={'present' if has_audio else 'absent'}"})

    if ffmpeg and video and duration > 0:
        _check_black_frames(ffmpeg, p, duration, hard, evidence)
        _check_frozen_frames(ffmpeg, p, duration, hard, evidence)
    elif not ffmpeg:
        evidence.append({"kind": "degraded",
                         "note": "ffmpeg not available; black/frozen frame checks skipped"})
    return {"hard_checks": hard, "evidence": evidence}


def _check_black_frames(ffmpeg: str, p: Path, duration: float,
                        hard: dict, evidence: list) -> None:
    try:
        res = _run([ffmpeg, "-i", str(p), "-vf", "blackdetect=d=0.5:pix_th=0.10",
                    "-an", "-f", "null", "-"])
        black_total = sum(float(m) for m in _BLACK_RE.findall(res.stderr))
        hard["no_black_screen"] = black_total < 0.9 * duration
        evidence.append({"kind": "ffmpeg",
                         "note": f"black_detect_s={black_total:.2f}/{duration:.2f}"})
    except Exception as exc:
        evidence.append({"kind": "degraded", "note": f"blackdetect failed: {exc}"})


def _check_frozen_frames(ffmpeg: str, p: Path, duration: float,
                         hard: dict, evidence: list) -> None:
    try:
        res = _run([ffmpeg, "-i", str(p), "-vf", "mpdecimate",
                    "-loglevel", "debug", "-an", "-f", "null", "-"])
        counts = {"drop": 0, "keep": 0}
        for kind in _FRAME_RE.findall(res.stderr):
            counts[kind] += 1
        total = counts["drop"] + counts["keep"]
        if total > 10:
            hard["no_frozen_frames"] = counts["drop"] / total < 0.95
        evidence.append({"kind": "ffmpeg",
                         "note": f"mpdecimate drop={counts['drop']} keep={counts['keep']}"})
    except Exception as exc:
        evidence.append({"kind": "degraded", "note": f"mpdecimate failed: {exc}"})
