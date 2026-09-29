# SPDX-License-Identifier: GPL-3.0-only
"""成片拼接：已验收镜头 -> ffmpeg concat（+ SRT 字幕）-> 完整成片。

手动「导出成片」与一键出片收尾共用本模块，两条链路产出同一种成片；
ffmpeg 不可用/失败时降级为 manifest 资产（不伪造视频）。
自动拼接放后台线程，不占用引擎工作线程。
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional

from ..config import settings
from ..domain.prompt_text import clean_dialogue
from ..domain.schemas.core import Event, Project, new_id, now_ts

log = logging.getLogger(__name__)

# 导出统一分辨率目标(与 adapters.comfyui.adapter.ASPECT_SIZES 对齐)
EXPORT_SIZES = {"16:9": (1920, 1080), "9:16": (1024, 1792), "1:1": (1024, 1024)}

_export_lock = threading.Lock()
_exporting: set[str] = set()


class ComposeError(Exception):
    """导出前置条件不满足（未全部验收 / 无镜头 / 项目不存在 / 重复导出）。"""

    def __init__(self, detail: str,
                 pending_shot_ids: Optional[list[str]] = None,
                 status_code: int = 409):
        super().__init__(detail)
        self.detail = detail
        self.pending_shot_ids = pending_shot_ids or []
        self.status_code = status_code


# ----------------------------------------------------------------- helpers --
def probe_video_size(ffprobe: Optional[str], path: str) -> Optional[tuple]:
    if not ffprobe:
        return None
    try:
        res = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60)
        w, h = res.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except Exception:
        return None


def unify_export_clips(ffmpeg: str, project: Project, clips: list,
                       workdir: Path, path_for) -> list:
    """分辨率不一致的片段重编码到统一尺寸(scale+pad 居中)。

    concat -c copy 不转码,混入竖屏片段会让播放器在中段切换横竖屏。
    目标尺寸取所有片段的众数分辨率(即项目实际渲染画布,如 H3 的
    1920x1072),探测失败时回退到项目画幅的标准尺寸;只有偏离
    众数的少数片段会被重编码,并在 clip["_path"] 记下新文件。
    path_for: asset_store.path_for,把 storage_key 解析为本地路径。
    """
    ffprobe = shutil.which("ffprobe")
    probed = [(clip, probe_video_size(ffprobe, path_for(clip["storage_key"])))
              for clip in clips]
    known = [size for _, size in probed if size is not None]
    if known:
        target_w, target_h = max(set(known), key=known.count)
    else:
        target_w, target_h = EXPORT_SIZES.get(project.aspect_ratio,
                                              EXPORT_SIZES["16:9"])
    for i, (clip, size) in enumerate(probed):
        if size is None or size == (target_w, target_h):
            continue
        src = path_for(clip["storage_key"])
        out = workdir / f"unified_{i:02d}.mp4"
        vf = (f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
              f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2")
        res = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", src, "-vf", vf,
             "-c:v", "libx264", "-crf", "18", "-preset", "fast",
             "-c:a", "aac", str(out)],
            capture_output=True, text=True, timeout=600)
        if res.returncode == 0 and out.exists() and out.stat().st_size > 0:
            clip["_path"] = str(out)
        else:
            log.warning("unify clip failed (%s), keep original: %s",
                        clip.get("shot_id"), (res.stderr or "")[-300:])
    return clips


def _srt_timestamp(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(clips: list) -> str:
    """按镜头顺序和累计时长生成 SRT（每个有台词的镜头一段）。"""
    blocks: list[str] = []
    t = 0.0
    idx = 1
    for c in clips:
        dur = float(c.get("duration_s") or 5)
        dialogue = clean_dialogue(c.get("dialogue"))
        if dialogue:
            blocks.append(f"{idx}\n{_srt_timestamp(t)} --> "
                          f"{_srt_timestamp(t + dur)}\n{dialogue}")
            idx += 1
        t += dur
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _escape_filter_path(path: str) -> str:
    """ffmpeg filter 里的路径转义（subtitles= 参数）。"""
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


# ------------------------------------------------------------------ concat --
def _try_ffmpeg_concat(store, asset_store, ffmpeg: str, project: Project,
                       clips: list, actor: str) -> Optional[object]:
    export_id = new_id("export")
    workdir = Path(settings.DATA_DIR) / "exports" / export_id
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        list_file = workdir / "concat.txt"
        list_file.write_text("".join(
            f"file '{c.get('_path') or asset_store.path_for(c['storage_key'])}'\n"
            for c in clips))
        out = workdir / f"{export_id}.mp4"
        res = subprocess.run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
             "-c", "copy", str(out)],
            capture_output=True, text=True, timeout=600)
        if res.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            log.warning("ffmpeg concat failed for %s: %s",
                        project.project_id, res.stderr[-500:])
            return None
        asset = asset_store.save_file(
            str(out), project.project_id, "video",
            f"{project.title or export_id}.mp4", source="derived",
            parent_asset_ids=[c["asset_id"] for c in clips])
        asset.status = "READY"
        store.put("assets", asset)
        store.append_event(Event(project_id=project.project_id,
                                 type="export.completed", actor=actor,
                                 summary=f"concat {len(clips)} clips"))
        return asset
    except Exception:
        log.exception("ffmpeg concat errored")
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _try_ffmpeg_concat_subtitles(store, asset_store, ffmpeg: str,
                                 project: Project, clips: list,
                                 actor: str) -> Optional[object]:
    """concat + SRT 字幕烧录：subtitles 滤镜必须重编码（libx264）。"""
    export_id = new_id("export")
    workdir = Path(settings.DATA_DIR) / "exports" / export_id
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        srt_file = workdir / "subtitles.srt"
        srt_file.write_text(build_srt(clips), encoding="utf-8")
        list_file = workdir / "concat.txt"
        list_file.write_text("".join(
            f"file '{c.get('_path') or asset_store.path_for(c['storage_key'])}'\n"
            for c in clips))
        out = workdir / f"{export_id}.mp4"
        vf = f"subtitles={_escape_filter_path(str(srt_file))}"
        res = subprocess.run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
             "-vf", vf, "-c:v", "libx264", "-crf", "18", "-preset", "fast",
             "-c:a", "copy", str(out)],
            capture_output=True, text=True, timeout=1800)
        if res.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            log.warning("ffmpeg subtitle concat failed for %s: %s",
                        project.project_id, res.stderr[-500:])
            return None
        asset = asset_store.save_file(
            str(out), project.project_id, "video",
            f"{project.title or export_id}.mp4", source="derived",
            parent_asset_ids=[c["asset_id"] for c in clips])
        asset.status = "READY"
        store.put("assets", asset)
        store.append_event(Event(project_id=project.project_id,
                                 type="export.completed", actor=actor,
                                 summary=f"concat {len(clips)} clips + subtitles"))
        return asset
    except Exception:
        log.exception("ffmpeg subtitle concat errored")
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ----------------------------------------------------------------- compose --
def _compose_locked(store, asset_store, project_id: str,
                    subtitles: bool = True, actor: str = "orchestrator") -> dict:
    """真正的拼接逻辑（调用方需已持有项目导出锁）。"""
    project = store.get("projects", project_id)
    if project is None:
        raise ComposeError("project not found", status_code=404)
    scene_order = {s.scene_id: s.order
                   for s in store.all("scenes", project_id=project_id)}
    shots = sorted(store.all("shots", project_id=project_id),
                   key=lambda s: (scene_order.get(s.scene_id, 0), s.order))
    if not shots:
        raise ComposeError("project has no shots", status_code=400)
    pending = [s.shot_id for s in shots if not s.accepted_run_id]
    if pending:
        raise ComposeError("shots without accepted run",
                           pending_shot_ids=pending)
    clips = []
    for shot in shots:
        run = store.get("runs", shot.accepted_run_id)
        video = next((store.get("assets", aid)
                      for aid in run.candidate_asset_ids
                      if store.get("assets", aid)
                      and store.get("assets", aid).media_type == "video"), None)
        if video is None:
            raise ComposeError(f"accepted run {run.run_id} has no video asset")
        clips.append({"shot_id": shot.shot_id, "run_id": run.run_id,
                      "asset_id": video.asset_id,
                      "storage_key": video.storage_key,
                      "dialogue": shot.spec.dialogue,
                      "duration_s": shot.spec.duration_s})

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        unify_dir = Path(tempfile.mkdtemp(prefix="svf-unify-"))
        try:
            # 画幅不一致的片段(如历史项目里的竖屏镜头)先统一到项目画幅
            clips = unify_export_clips(ffmpeg, project, clips, unify_dir,
                                       asset_store.path_for)
            # 有台词且要求字幕：烧录 SRT（必须重编码）；否则走 concat -c copy 快路径
            if subtitles and any(clean_dialogue(c["dialogue"]) for c in clips):
                asset = _try_ffmpeg_concat_subtitles(
                    store, asset_store, ffmpeg, project, clips, actor)
                if asset is not None:
                    return {"asset": asset, "mode": "concat_subtitles",
                            "clips": len(clips)}
            asset = _try_ffmpeg_concat(store, asset_store, ffmpeg, project,
                                       clips, actor)
            if asset is not None:
                return {"asset": asset, "mode": "concat", "clips": len(clips)}
        finally:
            shutil.rmtree(unify_dir, ignore_errors=True)
    manifest = {"project_id": project_id, "title": project.title,
                "generated_at": now_ts(), "clips": clips}
    asset = asset_store.save_bytes(
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        project_id, "text", "export_manifest.json", source="derived",
        parent_asset_ids=[c["asset_id"] for c in clips])
    asset.status = "READY"
    store.put("assets", asset)
    store.append_event(Event(project_id=project_id, type="export.completed",
                             actor=actor,
                             summary=f"manifest with {len(clips)} clips"))
    return {"asset": asset, "mode": "manifest", "clips": len(clips)}


def compose_project(store, asset_store, project_id: str,
                    subtitles: bool = True, actor: str = "orchestrator") -> dict:
    """拼接完整成片（手动导出与自动拼接共用；同项目同时刻只跑一个）。"""
    with _export_lock:
        if project_id in _exporting:
            raise ComposeError("export already running")
        _exporting.add(project_id)
    try:
        return _compose_locked(store, asset_store, project_id,
                               subtitles=subtitles, actor=actor)
    finally:
        with _export_lock:
            _exporting.discard(project_id)


def maybe_auto_compose(store, asset_store, project_id: str) -> bool:
    """一键出片收尾：所有镜头验收后自动拼接成完整成片。

    仅当 project.auto_compose 为真；拼接在后台线程跑（ffmpeg 重编码可能
    几分钟），不阻塞引擎工作线程。返回是否触发了拼接。
    """
    project = store.get("projects", project_id)
    if project is None or not getattr(project, "auto_compose", False):
        return False
    shots = store.all("shots", project_id=project_id)
    if not shots or any(not s.accepted_run_id for s in shots):
        return False
    with _export_lock:
        if project_id in _exporting:
            return False
        _exporting.add(project_id)

    def _run() -> None:
        try:
            store.append_event(Event(project_id=project_id,
                                     type="export.started", actor="orchestrator",
                                     summary="auto compose"))
            result = _compose_locked(store, asset_store, project_id,
                                     subtitles=True, actor="orchestrator")
            log.info("auto compose %s: %s", project_id, result.get("mode"))
        except Exception:
            log.exception("auto compose failed for %s", project_id)
            try:
                store.append_event(Event(project_id=project_id,
                                         type="export.failed",
                                         actor="orchestrator",
                                         summary="auto compose failed"))
            except Exception:
                pass
        finally:
            with _export_lock:
                _exporting.discard(project_id)

    threading.Thread(target=_run, name=f"svf-compose-{project_id}",
                     daemon=True).start()
    return True
