# SPDX-License-Identifier: GPL-3.0-only
"""FastAPI surface for the short-video factory.

Adapters are injected through create_app() so tests can wire mocks;
build_default() constructs the real adapters (lazy imports, degraded to
None with a log line when an adapter cannot be loaded).
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import io
import logging
import os
import re
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Literal, Optional
from xml.etree import ElementTree

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ...config import settings
from ...domain.repositories.store import Store
from ...domain.schemas.core import (
    ASSET_CATEGORIES, Acceptance, Asset, AssetBinding, AssetCategory, Character,
    Event, Project, RepairPlan, Run, Scene, Shot, ShotSpec, new_id, now_ts,
)
from ...domain.prompt_text import clean_dialogue as _clean_dialogue
from ...domain.video_constraints import (SYSTEM_VIDEO_CONSTRAINTS,
                                          constrained_acceptance,
                                          locked_visual_block,
                                          normalize_durations, plan_limits)
from ...domain.state_machine.machine import TERMINAL
from ...ingestion.uploader import Uploader, UploadError
from ...quality.density import density_score, emotion_hits
from ...workers.engine import WorkerEngine, next_seed

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_MEDIA_MIME = {"video": "video/mp4", "image": "image/png",
               "audio": "audio/mpeg", "text": "text/plain"}


# ----------------------------------------------------------- request bodies --
class ProjectCreate(BaseModel):
    title: str = ""
    style: str = ""
    brief: str = ""
    duration_target_s: int = 60
    aspect_ratio: str = "16:9"


class ProjectPatch(BaseModel):
    """可在工作台中安全修改的项目创作底稿。"""
    brief: Optional[str] = None


class ManualShotCreate(BaseModel):
    """用户在分镜页手动补充的镜头。未指定场景时自动建立一个场景。"""
    scene_id: Optional[str] = None
    action: str = ""
    dialogue: str = ""
    duration_s: int = Field(default=5, ge=1, le=120)


class UploadCreate(BaseModel):
    project_id: str
    filename: str
    total_size: int
    chunk_size: int = 8 * 1024 * 1024


class CompleteRequest(BaseModel):
    sha256: Optional[str] = None
    media_type: Optional[str] = None


class BindingCreate(BaseModel):
    asset_id: str
    role: str = "reference"          # reference | first_frame | reuse_clip | audio
    clip_range: Optional[list[float]] = None


class RunCreate(BaseModel):
    command_id: str = ""             # idempotency key
    seed: Optional[int] = None
    max_repairs: Optional[int] = None


class RunCommand(BaseModel):
    action: str                      # pause | resume | cancel | retry
    command_id: str = ""


class ReviewRequest(BaseModel):
    decision: str                    # accept | reject | note
    note: str = ""


class CharacterCreate(BaseModel):
    name: str
    kind: Literal["character", "location"] = "character"
    description: str = ""
    asset_id: Optional[str] = None


class ShotCharactersBind(BaseModel):
    character_ids: list[str]


class CharacterPatch(BaseModel):
    """角色卡片就地编辑;None 字段不动,空串 asset_id 表示清除参考图。"""
    description: Optional[str] = None
    asset_id: Optional[str] = None
    kind: Optional[Literal["character", "location"]] = None


class ShotPatch(BaseModel):
    """分镜页生成前的部分编辑；None 字段不动。"""
    action: Optional[str] = None
    dialogue: Optional[str] = None
    duration_s: Optional[int] = None
    camera: Optional[dict[str, str]] = None
    lighting_palette: Optional[str] = None
    object_states: Optional[list[dict[str, str]]] = None


class ExportRequest(BaseModel):
    subtitles: bool = True           # 有台词时烧录 SRT 字幕（重编码）


class CategorySet(BaseModel):
    category: AssetCategory


class QuickCreate(BaseModel):
    title: str = ""
    brief: str                       # 创意文案（必填）
    style: str = ""
    duration_target_s: int = 60
    aspect_ratio: str = "16:9"
    mode: Literal["text", "assets"] = "text"
    asset_ids: list[str] = []        # mode=assets 时选用的素材
    # 对话出片的结构化方案(agents.planner 产出);给出则跳过 LLM 规划,
    # 直接走 _materialize_plan 落库后编排生成。
    plan: Optional[dict] = None


class ChatTurnRequest(BaseModel):
    """对话出片:前端带全量历史(无状态),后端只回一轮结构化回复。"""
    messages: list[dict[str, str]] = []


class ChatPlanRequest(BaseModel):
    """对话出片:需求齐全后把对话拆成完整分镜方案(不落库,前端预览)。"""
    messages: list[dict[str, str]] = []
    proposal: Optional[dict] = None


class OneClickCreate(BaseModel):
    """一句话一键出片:文本里的显式信息(时长/画幅/镜头数)自动提取。

    confirm_after_assets=True 时:素材(定妆/场景图/首帧)生成完暂停,
    等用户确认后再渲染镜头并自动拼接;False=全自动直接出成片。
    上传材料(md/txt 直接给文本;docx 给 base64)时,材料会被确定性切成
    有序段落,逐段覆盖生成(视频与材料保持一致)。
    """
    text: str = ""
    duration_target_s: Optional[int] = None
    aspect_ratio: Optional[str] = None
    confirm_after_assets: bool = False
    material_text: Optional[str] = None       # md/txt/json/csv 等纯文本
    material_docx_b64: Optional[str] = None   # .docx 文件 base64
    material_name: str = ""


_DOCX_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _extract_docx_text(b64: str) -> str:
    """从 .docx(zip + word/document.xml)提取纯文本,标准库实现。"""
    data = base64.b64decode(b64)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = zf.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    lines: list[str] = []
    for para in root.iter(f"{_DOCX_NS}p"):
        text = "".join(node.text or "" for node in para.iter(f"{_DOCX_NS}t"))
        if text.strip():
            lines.append(text.strip())
    return "\n".join(lines)


# -------------------------------------------------------------------- plan --
_MD_PREFIX_RE = re.compile(r"^[\s#>*\-–—·、.]+")
_MD_ORDER_RE = re.compile(r"^\d+\s*[.、)）]\s*")


def _clean_title(raw: str) -> str:
    """材料文件名等派生的标题：去掉 Markdown 前缀/序号并压平空白。"""
    text = _MD_PREFIX_RE.sub("", (raw or "").strip())
    text = _MD_ORDER_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_material_brief(text: str) -> str:
    """材料开头的 Markdown 标记（# 标题、- 列表等）只影响展示，去掉后再入库。"""
    lines = (text or "").splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines:
        lines[0] = _MD_PREFIX_RE.sub("", lines[0].strip())
    return "\n".join(lines).strip()


def _txt(value, limit: int = 2000) -> str:
    """物化字段护栏:LLM/外部方案进来的字符串统一压平并截断。"""
    return " ".join(str(value or "").split())[:limit]


def _txt_list(value, limit: int = 200, max_items: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = _txt(item, limit)
        if text and text not in out:
            out.append(text)
        if len(out) >= max_items:
            break
    return out


def _materialize_plan(store: Store, project: Project, plan: dict,
                      ev: Optional[Callable[[str, str, str], Event]] = None) -> dict:
    """把结构化方案(场次/角色/镜头)确定性落库,返回统计。

    plan 形状与 agents.planner.normalize_plan 的输出一致:
    {"scenes":[{"title","summary","shots":[...]}],
     "characters":[{"name","kind","description"}]}
    手动规划(_run_plan)与对话出片(/projects/quick 携带 plan)共用本函数,
    两条链路产出完全相同的 ShotSpec 结构;LLM 数值一律不信任,在这里做
    最后钳制(数量/时长/画幅/角色展开/地点兜底/时长归一化)。
    """
    pid = project.project_id
    if store.get("projects", pid) is None:
        # 项目已被删除:生产线程可能删除后才走到这一步,不能再写入
        log.info("materialize skipped: project %s no longer exists", pid)
        return {"scenes": 0, "shots": 0, "characters": 0, "locations": 0}
    max_scenes, max_shots = plan_limits(project.duration_target_s)
    raw_scenes = [s for s in (plan.get("scenes") or []) if isinstance(s, dict)]
    raw_scenes = raw_scenes[:max_scenes]

    # 角色/地点登记：description 写成可复用的固定外观描述，保证跨镜头一致
    cast: list[Character] = []
    seen_names: set[str] = set()
    for c in plan.get("characters") or []:
        if len(cast) >= 40:            # 防外部接口塞入超大登记表
            break
        if not isinstance(c, dict):
            continue
        name = _txt(c.get("name"), 40)
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        ch = Character(project_id=pid, name=name,
                       kind="location" if c.get("kind") == "location"
                       else "character",
                       description=_txt(c.get("description"), 300))
        store.put("characters", ch)
        cast.append(ch)
    # 地点兜底:LLM 漏登记 location 时按场景合成,保证场景参考图与
    # 空间锁定链路不断(名称=场景名,描述=场景摘要)
    if raw_scenes and not any(c.kind == "location" for c in cast):
        for s in raw_scenes:
            title = _txt(s.get("title"), 20)
            if not title:
                continue
            ch = Character(project_id=pid, name=title, kind="location",
                           description=_txt(s.get("summary"), 300) or title)
            store.put("characters", ch)
            cast.append(ch)
        if ev is not None:
            ev("cast.location_fallback", "director",
               "synthesized locations from scenes")
    cast_by_name = {c.name: c for c in cast}
    loc_by_scene = {c.name: c for c in cast if c.kind == "location"}

    shot_total = 0
    for i, s in enumerate(raw_scenes):
        scene = Scene(scene_id=new_id("scene"), project_id=pid, order=i,
                      title=_txt(s.get("title"), 40) or f"场景 {i + 1:02d}",
                      summary=_txt(s.get("summary"), 300))
        store.put("scenes", scene)
        shots_raw = s.get("shots") if isinstance(s.get("shots"), list) else []
        shot_index = 0
        for d in shots_raw:
            if shot_total >= max_shots or not isinstance(d, dict):
                break
            action = _txt(d.get("action"), 600)
            if not action:
                continue        # 没有画面描述的镜头不落库
            try:
                acceptance = constrained_acceptance(
                    Acceptance(**(d.get("acceptance") or {})))
            except Exception:
                acceptance = constrained_acceptance()
            # 角色名 -> 登记表展开，参考图合入 reference_assets
            spec_chars = []
            ref_assets: list[str] = []
            for name in d.get("characters") or []:
                ch = cast_by_name.get(str(name))
                if ch is None:
                    continue
                spec_chars.append({"character_id": ch.character_id,
                                   "name": ch.name, "kind": ch.kind,
                                   "description": ch.description})
                if ch.asset_id and ch.asset_id not in ref_assets:
                    ref_assets.append(ch.asset_id)
            # 场景地点注入:该镜未引用任何地点时,挂上本场景的地点,
            # 让空间/光线锁定描述进入提示词(名称与场景标题一致)
            if not any(c.get("kind") == "location" for c in spec_chars):
                loc = loc_by_scene.get(scene.title) or \
                    loc_by_scene.get(scene.title[:20])
                if loc is not None:
                    spec_chars.append({"character_id": loc.character_id,
                                       "name": loc.name, "kind": "location",
                                       "description": loc.description})
                    if loc.asset_id and loc.asset_id not in ref_assets:
                        ref_assets.append(loc.asset_id)
            # 三桶节拍 + 序列关系（LLM 不给/给错时按镜头位置兜底）
            beats_raw = d.get("beats") if isinstance(d.get("beats"), dict) else {}
            beats = {k: _txt_list(beats_raw.get(k)) for k in
                     ("already_happened", "this_clip_only",
                      "reserved_for_later")}
            relation = str(d.get("sequence_relation") or "")
            if relation not in ("standalone", "sequence_first",
                                "seamless_continuation", "next_shot",
                                "reanchor"):
                relation = "sequence_first" if shot_index == 0 else "next_shot"
            # 关键物体连续性契约：只保留四要素齐全的有效条目
            object_states = []
            for o in d.get("object_states") or []:
                if not isinstance(o, dict) or not _txt(o.get("name"), 40):
                    continue
                object_states.append({
                    "name": _txt(o.get("name"), 40),
                    "count": _txt(o.get("count"), 40),
                    "start_state": _txt(o.get("start_state"), 200),
                    "end_state": _txt(o.get("end_state"), 200),
                })
            try:
                duration = int(float(d.get("duration_s") or 5))
            except (TypeError, ValueError):
                duration = 5
            motion_raw = d.get("motion_contract")
            camera_raw = d.get("camera")
            shot_id = new_id("shot")
            spec = ShotSpec(
                shot_id=shot_id,
                action=action,
                dialogue=_clean_dialogue(d.get("dialogue")),
                # 视频模型单镜时长有限,LLM 给的时长收敛到 [3,8] 秒
                duration_s=max(3, min(8, duration)),
                # 画幅以项目为准,LLM 逐镜给的值不信任(会混出竖屏镜头)
                aspect_ratio=project.aspect_ratio,
                characters=spec_chars,
                reference_assets=ref_assets,
                sequence_relation=relation,
                beats=beats,
                object_states=object_states,
                felt_intent=_txt(d.get("felt_intent"), 300),
                narrative_beat=_txt(d.get("narrative_beat"), 300),
                motion_contract={_txt(k, 40): _txt(v, 300)
                                 for k, v in motion_raw.items()}
                if isinstance(motion_raw, dict) else {},
                camera={_txt(k, 40): _txt(v, 80)
                        for k, v in camera_raw.items()}
                if isinstance(camera_raw, dict) else {},
                lighting_palette=_txt(d.get("lighting_palette"), 300),
                acceptance=acceptance)
            store.put("shots", Shot(shot_id=shot_id, scene_id=scene.scene_id,
                                    project_id=pid, order=shot_index, spec=spec))
            shot_index += 1
            shot_total += 1

    # 规划后时长归一化：提示词约束是软性的，LLM 仍可能超发，
    # 这里按比例确定性缩放到目标时长（单镜 3-8s 钳制）。
    planned = store.all("shots", project_id=pid)
    durations = [s.spec.duration_s for s in planned]
    normalized = normalize_durations(durations, project.duration_target_s)
    if planned and durations != normalized:
        for s, dur in zip(planned, normalized):
            s.spec.duration_s = dur
            store.put("shots", s)
        if ev is not None:
            ev("plan.normalized", "director",
               f"{sum(durations)}s -> {sum(normalized)}s "
               f"(target {project.duration_target_s}s)")
    return {"scenes": len(raw_scenes), "shots": shot_total,
            "characters": sum(1 for c in cast if c.kind == "character"),
            "locations": sum(1 for c in cast if c.kind == "location")}


def _run_plan(store: Store, text_model, project: Project) -> None:
    """Background planning thread: screenwriter -> scenes, cast -> characters,
    director per scene -> shots; 组装成 plan 后走 _materialize_plan 落库。

    物化逻辑与对话出片(/projects/quick 携带 plan)共用,保证两条链路的
    结构化提示词契约完全一致。
    """
    pid = project.project_id

    def ev(type_: str, actor: str, summary: str = "") -> Event:
        event = Event(project_id=pid, type=type_, actor=actor, summary=summary)
        store.append_event(event)
        return event

    try:
        ev("agent.started", "screenwriter", "planning scenes")
        sc = text_model.chat_json(
            "你是短剧编剧 agent。把创意简报拆成有序场景，只输出 JSON。"
            "同一角色在所有场景中名称与外观描述必须逐字一致。"
            f"全片目标时长 {project.duration_target_s} 秒是硬约束："
            "场景数量据此从严控制（每场约 5-8 秒，总时长不得超过目标）。",
            {"title": project.title, "style": project.style,
             "brief": project.brief,
             "duration_target_s": project.duration_target_s},
            '{"scenes":[{"title":str,"summary":str}]}')
        scenes = sc.get("scenes") or []
        ev("agent.completed", "screenwriter", f"{len(scenes)} scenes")

        # 角色/地点登记：description 写成可复用的固定外观描述，保证跨镜头一致
        cast_raw: list[dict] = []
        try:
            ev("agent.started", "director", "extracting cast")
            cast_out = text_model.chat_json(
                "你是短剧导演 agent。从剧本提取全部角色和固定地点清单，只输出 JSON。"
                "人物、动物、宠物、幻想生物一律 kind=character；"
                "kind=location 只指固定场景/建筑/空间（如厨房、便利店、街道），"
                "不要把动物或人物登记成地点。"
                "每个场景的发生地必须登记为一个 location（如「乡村厨房」），"
                "地点清单不允许为空。"
                "每个角色的 description 写成可复用的固定外观描述"
                "（如「艾米:20岁女孩,及肩黑发,米色毛衣」），地点同理；"
                "同一角色/地点的描述在所有镜头中必须逐字一致。",
                {"title": project.title, "style": project.style,
                 "brief": project.brief, "scenes": scenes},
                '{"characters":[{"name":str,'
                '"kind":"character|location","description":str}]}')
            cast_raw = [c for c in (cast_out.get("characters") or [])
                        if isinstance(c, dict)]
            ev("agent.completed", "director",
               f"{len(cast_raw)} characters/locations")
        except Exception:
            log.warning("cast extraction failed for %s", pid, exc_info=True)
        # 导演提示词里的演员表(名称/类型/外观逐字下发,物化时再登记入库)
        cast_preview: list[dict] = []
        seen_cast: set[str] = set()
        for c in cast_raw:
            name = _txt(c.get("name"), 40)
            if not name or name in seen_cast:
                continue
            seen_cast.add(name)
            cast_preview.append({
                "name": name,
                "kind": "location" if c.get("kind") == "location"
                else "character",
                "description": _txt(c.get("description"), 300),
            })

        # 时长预算：全片镜头数 ≈ 目标时长/5s，每场秒数均分，约束逐场景下发
        shot_budget = max(1, project.duration_target_s // 5)
        n_scenes = max(1, len(scenes))
        scene_seconds = max(4, round(project.duration_target_s / n_scenes))
        scene_max_shots = max(1, round(scene_seconds / 5))

        plan_scenes: list[dict] = []
        for i, s in enumerate(scenes):
            if not isinstance(s, dict):
                continue
            title = _txt(s.get("title"), 40) or f"场景 {i + 1:02d}"
            summary = _txt(s.get("summary"), 300)
            ev("agent.started", "director", f"shots for scene {title}")
            sh = text_model.chat_json(
                "你是短剧导演 agent。把场景拆成有序镜头，只输出 JSON。"
                "shot.characters 填本镜头出现的角色/地点名，必须与给定清单逐字一致；"
                "同一角色在所有镜头中外观描述逐字一致，场景描述同理。"
                "动作拆分原则：一个镜头只承担一个主动作（如「端盘落桌」），"
                "不要把多个主动作（做早餐+端盘+猫靠近）塞进同一镜头；"
                "猫、窗帘、蒸汽、窗外景色等次要元素只允许微动，不得抢占主事件。"
                "object_states 列出本镜头的关键物体（餐盘/食物/动物/道具），"
                "每个物体写清：name 名称、count 数量（如「仅一只」）、"
                "start_state 开始状态（如「在女孩手中」「桌面为空」）、"
                "end_state 结束状态（如「盘子在桌面中央，双手离开」）；"
                "物体不得复制、悬浮、穿模、突然出现或消失。"
                "每个镜头的 beats 分三桶：already_happened=已演完不许重播的情节、"
                "this_clip_only=本镜头独占的节拍、reserved_for_later=后续镜头预留"
                "不许提前泄露的节拍；felt_intent=角色内心意图（不进画面描述）。"
                "每镜必须填写 narrative_beat（它推动的剧情）和 motion_contract（开始状态、"
                "角色或关键道具的主运动、环境/道具/光影的独立次运动、结束状态）。"
                "每镜填写 lighting_palette：本镜的光线与调色板"
                "（光线方向/色温/主色调，如「低调工业光,地面霓虹灯管,深黑+电蓝+品红」），"
                "必须与场景描述和项目风格一致，同一场景内逐字一致。"
                "禁止把人物图或风景图的裁切、平移、缩放、推拉、Ken Burns 效果当作视频；"
                "相机运动不能代替角色、道具和环境的剧情运动。"
                "sequence_relation：场景内第一镜=sequence_first，后续镜头=next_shot。"
                f"时长硬约束：本场景约 {scene_seconds} 秒，最多 {scene_max_shots} 个镜头，"
                f"所有镜头 duration_s 之和不得超过 {scene_seconds + 2} 秒"
                f"（全片目标 {project.duration_target_s} 秒、约 {shot_budget} 个镜头）。",
                {"project_style": project.style,
                 "scene": {"title": title, "summary": summary},
                 "cast": cast_preview},
                '{"shots":[{"action":str,"dialogue":str,"duration_s":int(每镜4-8秒),'
                '"characters":[str],'
                '"sequence_relation":"sequence_first|next_shot",'
                '"felt_intent":str,"narrative_beat":str,'
                '"motion_contract":{"start_state":str,"primary_motion":str,'
                '"secondary_motion":str,"end_state":str,"camera_motion":str},'
                '"beats":{"already_happened":[str],"this_clip_only":[str],'
                '"reserved_for_later":[str]},'
                '"object_states":[{"name":str,"count":str,'
                '"start_state":str,"end_state":str}],'
                '"camera":{str:str},"lighting_palette":str,'
                '"acceptance":{"required":[str],"forbidden":[str]}}]}',
                max_tokens=4096)
            plan_scenes.append({
                "title": title,
                "summary": summary,
                "shots": [d for d in (sh.get("shots") or [])
                          if isinstance(d, dict)],
            })
            ev("agent.completed", "director", f"scene {i} shots saved")

        stats = _materialize_plan(
            store, project,
            {"scenes": plan_scenes, "characters": cast_raw}, ev)
        ev("plan.completed", "screenwriter",
           f"{stats['scenes']} scenes / {stats['shots']} shots planned")
    except Exception as exc:
        log.exception("planning failed for project %s", pid)
        ev("plan.failed", "screenwriter", str(exc)[:300])
        raise   # 让 quick 编排走 quick.failed,而不是空项目静默"成功"


def _compose_prompt(project: Optional[Project], shot: Shot,
                    prev_shot: Optional[Shot] = None) -> str:
    """导演级分段结构渲染提示词:

    Duration/Aspect/Style 头 → SCENE(角色/场景锁定+参考图锚定) →
    SHOT(续接/动作/节拍/motion_contract/台词) → CAMERA →
    LIGHTING & PALETTE → AVOID(三桶负约束/物体契约/多人与微动约束/
    无台词声景否定/系统约束/禁文字)。
    不单独设 AUDIO 段:台词内嵌 SHOT,无台词的声景否定并入 AVOID。
    """
    spec = shot.spec
    header = (f"Duration: {spec.duration_s} seconds | "
              f"Aspect ratio: {spec.aspect_ratio}")
    if project and project.style:
        header += f" | Style: {project.style}"

    scene_parts: list[str] = []
    visual_lock = locked_visual_block(spec)
    if visual_lock:
        scene_parts.append(visual_lock)
    # Source-Carries-State：外观与场景由参考图承载，文字只写动作与变化
    if spec.reference_assets:
        scene_parts.append("参考图是角色、场景和关键道具的唯一视觉锚点，逐帧严格保持；"
                           "文字只描述本镜头的动作、因果变化与镜头语言")

    shot_parts: list[str] = []
    # 观测态续接：上一镜的实际末态优先于任何计划描述
    if prev_shot is not None and prev_shot.spec.observed_end_state:
        shot_parts.append(f"开场接续上一镜实际末态: {prev_shot.spec.observed_end_state}")
    if spec.action:
        shot_parts.append(spec.action)
    if spec.narrative_beat:
        shot_parts.append(f"本镜剧情节拍: {spec.narrative_beat}")
    motion = spec.motion_contract or {}
    for field, label in (("start_state", "动作开始状态"),
                         ("primary_motion", "主动作"),
                         ("secondary_motion", "环境/道具变化"),
                         ("end_state", "动作结束状态")):
        value = str(motion.get(field) or "").strip()
        if value:
            shot_parts.append(f"{label}: {value}")
    dialogue = _clean_dialogue(spec.dialogue)
    if dialogue:
        # H3 原生音频:参考学习视频验证过的结构化语法,
        # <d>[Chinese] ...</d> 才能产出清晰中文语音,纯文字"台词:"不行
        speaker = next((str(c.get("name") or "") for c in spec.characters
                        if c.get("kind") != "location"
                        and str(c.get("name") or "").strip()), "角色")
        shot_parts.append(f"本镜头中{speaker}用中文普通话清晰地说: "
                          f"<d>[Chinese] {dialogue}</d> "
                          "声音清晰、稳定、贴近麦克风。除此之外只有贴合场景的"
                          "轻微环境音,没有其他人声。")

    avoid_parts: list[str] = []
    # 三桶节拍：已演不重演、未来不泄露
    beats = spec.beats or {}
    already = [str(b) for b in beats.get("already_happened") or [] if str(b).strip()]
    if already:
        avoid_parts.append("以下情节已发生，不要重演: " + "；".join(already))
    reserved = [str(b) for b in beats.get("reserved_for_later") or [] if str(b).strip()]
    if reserved:
        avoid_parts.append("不要提前出现: " + "；".join(reserved))
    # 关键物体连续性契约：数量 + 初末状态逐条写死，防止复制/悬浮/混帧
    obj_lines = []
    for o in spec.object_states or []:
        if not isinstance(o, dict):
            continue
        name = str(o.get("name") or "").strip()
        if not name:
            continue
        seg = name
        if str(o.get("count") or "").strip():
            seg += f"（{str(o['count']).strip()}）"
        start = str(o.get("start_state") or "").strip()
        end = str(o.get("end_state") or "").strip()
        if start or end:
            seg += f"：开始[{start or '保持现状'}] → 结束[{end or '保持'}]"
        obj_lines.append(seg)
    if obj_lines:
        avoid_parts.append("关键物体约束(全片数量恒定,不得复制/悬浮/穿模/突现/消失): "
                           + "；".join(obj_lines))
        avoid_parts.append("动作开始状态与结束状态必须分别成立,不得混合在同一帧")
    # 多人三层动作层级：非焦点人物只允许微动
    if len(spec.characters) >= 2:
        avoid_parts.append("非焦点人物保持自然微动(呼吸/眨眼)，不得擅自起身/走动/拿取物品；"
                           "只有焦点角色执行主要动作")
    # 次要元素（动物/窗帘/蒸汽/远景）只允许微动，不与主动作抢事件
    avoid_parts.append("次要元素(动物、窗帘、蒸汽、窗外远景)只做轻微响应式微动，"
                       "不得引入新的叙事事件或与主动作竞争视觉焦点")
    if not dialogue:
        # 正向声景描述 + 英文否定关键词(否定中文描述会被模型反向 priming)
        avoid_parts.append("overall_soundscape: 仅贴合场景的安静环境音与动作音效。"
                           "No voice, no speech, no dialogue, no narration, "
                           "no murmuring, no whispering, no singing, in any language. "
                           "non_diegetic_music: None.")
    avoid_parts.append(SYSTEM_VIDEO_CONSTRAINTS)
    # 末尾保留明确的禁文字指令，兼容现有渲染工作流的 prompt 解析习惯。
    avoid_parts.append("画面中不出现任何文字、字幕、水印、logo、标识")

    sections = [header]
    if scene_parts:
        sections.append("SCENE " + " | ".join(scene_parts))
    sections.append("SHOT " + (" | ".join(shot_parts) or f"shot {shot.shot_id}"))
    if spec.camera:
        sections.append("CAMERA " + ", ".join(f"{k}={v}"
                                              for k, v in spec.camera.items()))
    lighting = str(spec.lighting_palette or "").strip()
    if lighting:
        sections.append(f"LIGHTING & PALETTE {lighting}")
    sections.append("AVOID: " + " | ".join(avoid_parts))
    return "\n\n".join(sections)


# 导出/拼接逻辑在 workers.compose：手动导出与一键出片自动拼接共用。
# 这里 re-export build_srt / unify_export_clips,兼容既有调用方与测试。
from ...workers.compose import (  # noqa: E402  (kept after _compose_prompt)
    ComposeError, build_srt, compose_project, maybe_auto_compose,
    unify_export_clips as _unify_export_clips,
)


# --------------------------------------------------------------- application --
def create_app(store: Store, asset_store, renderer=None, judge=None,
               decision=None, text_model=None, data_dir: Optional[str] = None,
               start_worker: bool = True, reviewer=None, classifier=None,
               image_model=None,
               worker_poll_interval_s: float = 0.5) -> FastAPI:
    engine = WorkerEngine(store, asset_store, renderer=renderer, judge=judge,
                          decision=decision, text_model=text_model,
                          poll_interval_s=worker_poll_interval_s)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        engine.shutdown()

    app = FastAPI(title="short-video-factory", lifespan=lifespan)
    uploader = Uploader(store, asset_store, data_dir or settings.DATA_DIR)
    app.state.engine = engine
    app.state.uploader = uploader
    if start_worker:
        engine.start()

    # 服务重启自愈：接上被进程重启打断的生产任务。
    # 生产是「定妆 → 首帧 → 逐镜排队」多步链路，任一步被重启打断都会留下
    # quick.started 无终态；这里扫描并幂等续跑(_produce_shots 会跳过已完成的部分)。
    def _resume_interrupted_productions() -> None:
        time.sleep(2.0)          # 等服务完成启动,再开始慢任务
        # 可自动续跑的「开始点」; waiting 语义的标记出现后不自动续
        resume_from = ("quick.started", "produce.confirmed")
        marker = resume_from + ("quick.orchestrated", "quick.failed",
                                "produce.stopped", "produce.stop_requested",
                                "produce.awaiting_confirm")
        try:
            projects = sorted(store.all("projects"),
                              key=lambda p: p.created_at)
        except Exception:
            log.exception("resume scan failed")
            return
        # 清理孤儿 run:项目已删除但 run 还活着(旧版删除未停在途工作的遗留),
        # 这些会一直占着 GPU/队列,先取消掉
        try:
            for run in store.all("runs"):
                if run.state in TERMINAL:
                    continue
                if store.get("projects", run.project_id) is not None:
                    continue
                try:
                    engine.cancel_run(run.run_id)
                except Exception:
                    fresh = store.get("runs", run.run_id)
                    if fresh is not None and fresh.state not in TERMINAL:
                        fresh.state = "CANCELLED"
                        fresh.updated_at = now_ts()
                        store.put("runs", fresh)
                log.info("orphan run %s cancelled (project %s missing)",
                         run.run_id, run.project_id)
        except Exception:
            log.warning("orphan run cleanup failed", exc_info=True)
        for project in projects:
            pid = project.project_id
            try:
                events = sorted(store.all("events", project_id=pid),
                                key=lambda e: e.seq)
                last = next((e for e in reversed(events)
                             if e.type in marker), None)
                # 只续「最后一次生产事件是开始/已确认」的项目;
                # 用户主动停止、暂停待确认的项目不自动续
                if last is None or last.type not in resume_from:
                    continue
                shots = store.all("shots", project_id=pid)
                with produce_lock:
                    if pid in producing:
                        continue
                    producing.add(pid)
                store.append_event(Event(project_id=pid, type="produce.resumed",
                                         actor="orchestrator",
                                         summary="resume after restart"))
                log.info("resuming interrupted production: %s (%d shots)",
                         project.title, len(shots))
                try:
                    if not shots:
                        if text_model is None:
                            raise RuntimeError("text model not configured")
                        _run_plan(store, text_model, project)
                    _produce_shots(project, "text", [])
                except Exception as exc:
                    log.exception("resume failed for %s", pid)
                    store.append_event(Event(project_id=pid, type="quick.failed",
                                             actor="orchestrator",
                                             summary=f"resume failed: {exc}"[:300]))
                finally:
                    with produce_lock:
                        producing.discard(pid)
                    _clear_stop_event(pid)
            except Exception:
                log.warning("resume scan: project %s failed", pid, exc_info=True)

    if start_worker:
        threading.Thread(target=_resume_interrupted_productions,
                         name="svf-resume", daemon=True).start()

    def _get_or_404(table: str, key: str):
        obj = store.get(table, key)
        if obj is None:
            raise HTTPException(404, f"{table}/{key} not found")
        return obj

    # ------------------------------------------------------------ projects --
    @app.get("/projects")
    def list_projects() -> dict:
        """项目列表 + 六步进度聚合（一次性查表，内存聚合，避免 N+1）。"""
        projects = sorted(store.all("projects"),
                          key=lambda p: p.created_at, reverse=True)
        scenes = store.all("scenes")
        shots = store.all("shots")
        runs = store.all("runs")
        chars = store.all("characters")
        assets = store.all("assets")
        # 规划期(编剧/导演/定妆/首帧)尚无 run,不能漏掉:
        # 预先聚合每个项目的编排事件,区分"一键成片编排中"与"已结束/失败"
        plan_terminal: dict[str, str] = {}
        quick_started: set[str] = set()
        for e in store.all("events"):
            if e.type == "quick.started":
                quick_started.add(e.project_id)
            elif e.type in ("quick.orchestrated", "quick.failed", "plan.failed"):
                plan_terminal[e.project_id] = e.type
        out = []
        running_states = {"RENDERING", "GENERATED", "NORMALIZING", "SCORING",
                          "REPAIRING", "CANCEL_REQUESTED"}
        waiting_states = {"PLANNED", "ASSET_READY", "PROMPT_READY", "QUEUED",
                          "RETRY_WAIT"}
        task_summary = {"running_projects": 0, "waiting_projects": 0,
                        "completed_projects": 0}
        for p in projects:
            pid = p.project_id
            p_shots = [s for s in shots if s.project_id == pid]
            p_runs = [r for r in runs if r.project_id == pid]
            p_chars = [c for c in chars if c.project_id == pid]
            p_assets = [a for a in assets if a.project_id == pid]
            # 首页/项目库直接预览：优先用最新派生成片；没有成片时，回退到已验收镜头的候选视频。
            preview = next((a for a in sorted(p_assets, key=lambda a: a.created_at,
                                              reverse=True)
                            if a.media_type == "video" and a.source == "derived"), None)
            if preview is None:
                accepted_runs = {
                    s.accepted_run_id for s in p_shots if s.accepted_run_id
                }
                for run in sorted((r for r in runs if r.run_id in accepted_runs),
                                  key=lambda r: r.created_at, reverse=True):
                    preview = next((store.get("assets", aid)
                                    for aid in run.candidate_asset_ids
                                    if store.get("assets", aid)
                                    and store.get("assets", aid).media_type == "video"), None)
                    if preview is not None:
                        break
            progress = {
                "scenes": sum(1 for s in scenes if s.project_id == pid),
                "shots": len(p_shots),
                "runs": len(p_runs),
                "accepted": sum(1 for s in p_shots if s.accepted_run_id),
                "characters": sum(1 for c in p_chars if c.kind == "character"),
                "locations": sum(1 for c in p_chars if c.kind == "location"),
                "portraits": sum(1 for c in p_chars if c.asset_id),
                "props": sum(1 for a in p_assets
                             if a.project_id == pid and a.category == "prop"),
                "exports": sum(1 for a in p_assets
                               if a.project_id == pid and a.source == "derived"
                               and a.media_type == "video"),
            }
            # 首页以项目为单位展示队列状态：一个项目命中执行态就优先算执行中；
            # 没有活跃任务且所有镜头验收后才计为已完成。
            states = {r.state for r in p_runs}
            if states & running_states:
                task_summary["running_projects"] += 1
            elif states & waiting_states:
                task_summary["waiting_projects"] += 1
            elif p_shots and len(p_shots) == progress["accepted"]:
                task_summary["completed_projects"] += 1
            elif not p_runs and pid in quick_started \
                    and pid not in plan_terminal:
                # 一键成片编排中(规划/定妆/首帧,run 尚未创建):计入等待
                task_summary["waiting_projects"] += 1
            # 停止入口的可见性:有生产线程或在途 run 的项目才显示「停止」
            active_runs = [r for r in p_runs if r.state not in TERMINAL]
            is_producing = pid in producing
            out.append({**p.model_dump(), "progress": progress,
                        "preview_video_asset_id": preview.asset_id if preview else None,
                        "preview_video_kind": "export" if preview and preview.source == "derived" else "clip",
                        "producing": is_producing,
                        "active_runs": len(active_runs),
                        "stoppable": is_producing or bool(active_runs)})
        # 估时是队列预估而非渲染服务承诺：执行中的项目按约 1 分钟、等待中的
        # 项目按约 90 秒折算，便于用户在首页判断是否需要等待。
        eta_seconds = (task_summary["running_projects"] * 60
                       + task_summary["waiting_projects"] * 90)
        task_summary["eta_seconds"] = eta_seconds
        task_summary["estimated_finish_at"] = now_ts() + eta_seconds if eta_seconds else None
        return {"projects": out, "task_summary": task_summary}

    @app.post("/projects", status_code=201)
    def create_project(body: ProjectCreate) -> Project:
        project = Project(title=body.title, style=body.style, brief=body.brief,
                          duration_target_s=body.duration_target_s,
                          aspect_ratio=body.aspect_ratio)
        store.put("projects", project)
        store.append_event(Event(project_id=project.project_id,
                                 type="project.created", actor="api",
                                 summary=project.title))
        return project

    @app.patch("/projects/{project_id}")
    def patch_project(project_id: str, body: ProjectPatch) -> Project:
        project = _get_or_404("projects", project_id)
        if body.brief is not None:
            project.brief = body.brief.strip()
        store.put("projects", project)
        store.append_event(Event(project_id=project_id, type="project.updated",
                                 actor="api", summary="brief updated"))
        return project

    @app.post("/projects/{project_id}/plan", status_code=202)
    def plan_project(project_id: str, force: bool = False) -> dict:
        project = _get_or_404("projects", project_id)
        if text_model is None:
            raise HTTPException(503, "text model not configured")
        existing = store.all("scenes", project_id=project_id)
        if existing and not force:
            return {"status": "planned", "scenes": len(existing)}
        if existing:
            # force 重新规划：取消未终结 runs，删旧 shots/scenes（events 保留）
            for run in store.all("runs", project_id=project_id):
                if run.state in TERMINAL:
                    continue
                try:
                    engine.cancel_run(run.run_id)
                except (ValueError, KeyError):
                    # 瞬态（GENERATED 等）状态机不许 cancel，直接落 CANCELLED
                    fresh = store.get("runs", run.run_id)
                    if fresh is not None and fresh.state not in TERMINAL:
                        fresh.state = "CANCELLED"
                        fresh.updated_at = now_ts()
                        store.put("runs", fresh)
            for shot in store.all("shots", project_id=project_id):
                store.delete("shots", shot.shot_id)
            for scene in existing:
                store.delete("scenes", scene.scene_id)
            store.append_event(Event(project_id=project_id, type="plan.reset",
                                     actor="api", summary="force re-plan"))
        threading.Thread(target=_run_plan, args=(store, text_model, project),
                         name=f"svf-plan-{project_id}", daemon=True).start()
        return {"status": "planning"}

    # ---------------------------------------------------------------- quick --
    @app.post("/projects/quick", status_code=201)
    def quick_project(body: QuickCreate) -> dict:
        """一键成片：建项目 + 后台编排。

        携带 plan（对话出片）时只**同步**落库结构化分镜，不自动生成：
        API 返回时场次/角色/镜头已可读，前端进工作室直接看到刚生成的内容；
        确认/修改后由用户点「开始生成」调 /projects/{id}/produce 才开工。
        无 plan 的标准链路保持原行为：后台规划后自动生成。
        """
        if text_model is None and body.plan is None:
            raise HTTPException(503, "text model not configured")
        if body.plan is not None:
            scenes = body.plan.get("scenes") if isinstance(body.plan, dict) else None
            if not isinstance(scenes, list) or not scenes:
                raise HTTPException(422, "plan.scenes 不能为空")
        assets = []
        if body.mode == "assets":
            for aid in body.asset_ids:
                assets.append(_get_or_404("assets", aid))
        project = Project(title=body.title or body.brief[:20], style=body.style,
                          brief=body.brief,
                          duration_target_s=body.duration_target_s,
                          aspect_ratio=body.aspect_ratio,
                          auto_compose=True)   # 一键出片:全部镜头验收后自动拼成片
        store.put("projects", project)
        store.append_event(Event(project_id=project.project_id,
                                 type="project.created", actor="api",
                                 summary=f"quick: {project.title}"))
        if body.plan is not None:
            # 同步物化：接口返回前分镜就已入库，工作室跳转不再扑空
            def ev(type_: str, actor: str, summary: str = "") -> Event:
                event = Event(project_id=project.project_id, type=type_,
                              actor=actor, summary=summary)
                store.append_event(event)
                return event

            ev("plan.started", "orchestrator", "dialogue plan")
            stats = _materialize_plan(store, project, body.plan, ev)
            ev("plan.completed", "orchestrator",
               f"{stats['scenes']} scenes / {stats['shots']} shots (dialogue plan)")
            return {"project_id": project.project_id, "status": "planned",
                    "scenes": stats["scenes"], "shots": stats["shots"]}
        threading.Thread(target=_run_quick, args=(project, body.mode, assets),
                         name=f"svf-quick-{project.project_id}",
                         daemon=True).start()
        return {"project_id": project.project_id, "status": "producing"}

    def _run_quick(project: Project, mode: str, assets: list[Asset]) -> None:
        """后台编排：LLM 规划（分钟级）完成后自动定妆/首帧/逐镜排队。"""
        pid = project.project_id
        with produce_lock:
            producing.add(pid)
        try:
            store.append_event(Event(project_id=pid, type="quick.started",
                                     actor="orchestrator", summary=mode))
            _run_plan(store, text_model, project)
            _produce_shots(project, mode, assets)
        except Exception as exc:
            log.exception("quick orchestration failed for %s", pid)
            store.append_event(Event(project_id=pid, type="quick.failed",
                                     actor="orchestrator", summary=str(exc)[:300]))
        finally:
            with produce_lock:
                producing.discard(pid)

    # 生成编排互斥：同一项目只允许一个生产线程在跑
    producing: set[str] = set()
    produce_lock = threading.Lock()
    # 项目级停止：协作式事件，生产线程在每个生成项之间检查。
    # 服务端行为，与浏览器无关；停止后再次「开始生成」会创建全新事件。
    stop_events: dict[str, threading.Event] = {}
    stop_lock = threading.Lock()

    def _stop_event(pid: str) -> threading.Event:
        with stop_lock:
            event = stop_events.get(pid)
            if event is None:
                event = threading.Event()
                stop_events[pid] = event
            return event

    def _clear_stop_event(pid: str) -> None:
        with stop_lock:
            stop_events.pop(pid, None)

    def _produce_shots(project: Project, mode: str = "text",
                       assets: Optional[list[Asset]] = None,
                       force_queue: bool = False) -> None:
        """后台生产：定妆参考图 → 镜头首帧 → 为尚无 run 的镜头建 run。

        幂等：已有 run 的镜头不重复排队；生产项之间检查停止事件
        （当前这一张图会先完成），停止后不再排队新 run。
        force_queue=True 时跳过「素材后确认」的暂停（用户已确认/手动开始）。
        对话出片的「开始生成」与标准一键成片共用本函数。
        """
        pid = project.project_id
        assets = assets or []
        if store.get("projects", pid) is None:
            log.info("produce skipped: project %s no longer exists", pid)
            return
        stop = _stop_event(pid)

        def stopped() -> bool:
            return stop.is_set()

        def emit_stopped(stage: str) -> None:
            store.append_event(Event(project_id=pid, type="produce.stopped",
                                     actor="orchestrator",
                                     summary=f"stopped at {stage}"))
        try:
            if stopped():
                emit_stopped("start")
                return
            # 角色定妆照：像素级身份锁定(图片服务不可用时跳过,仅文字锁定)
            try:
                from ...agents.casting import (
                    generate_character_portraits, generate_location_references)
                model = image_model or _make_image_model()
                if model.available():
                    store.append_event(Event(project_id=pid,
                                             type="casting.started",
                                             actor="orchestrator",
                                             summary="generating portraits"))
                    made = generate_character_portraits(
                        store, model, pid, style=project.style,
                        should_stop=stopped)
                    made_loc = generate_location_references(
                        store, model, pid, style=project.style,
                        should_stop=stopped)
                    store.append_event(Event(project_id=pid,
                                             type="casting.completed",
                                             actor="orchestrator",
                                             summary=f"{len(made)} portraits, "
                                                     f"{len(made_loc)} locations"))
            except Exception:
                log.warning("portrait step skipped for %s", pid, exc_info=True)
            if stopped():
                emit_stopped("casting")
                return
            # 镜头首帧图：固定关键物体的初始状态(数量/位置/姿态),
            # 用首帧约束代替纯文本,防止初末态混帧与物体复制
            try:
                from ...agents.first_frame import generate_shot_first_frames
                model = image_model or _make_image_model()
                if model.available():
                    store.append_event(Event(project_id=pid,
                                             type="first_frame.started",
                                             actor="orchestrator",
                                             summary="generating first frames"))
                    made_ff = generate_shot_first_frames(
                        store, model, pid, style=project.style,
                        should_stop=stopped)
                    store.append_event(Event(project_id=pid,
                                             type="first_frame.completed",
                                             actor="orchestrator",
                                             summary=f"{len(made_ff)} first frames"))
            except Exception:
                log.warning("first-frame step skipped for %s", pid,
                            exc_info=True)
            if stopped():
                emit_stopped("first_frame")
                return
            # 素材后确认模式:参考图/首帧已就绪,等用户确认后再排队渲染
            if project.confirm_after_assets and not force_queue:
                store.append_event(Event(
                    project_id=pid, type="produce.awaiting_confirm",
                    actor="orchestrator",
                    summary="materials ready; waiting for confirmation"))
                return
            shots = sorted(store.all("shots", project_id=pid),
                           key=lambda s: (s.scene_id, s.order))
            # mode=assets：所有图片素材设进每个 shot 的 reference_assets
            # （第一张为主参考，渲染只取第一张）；video 素材从简忽略
            image_refs = [a.asset_id for a in assets if a.media_type == "image"]
            runs = []
            for shot in shots:
                if store.all("runs", shot_id=shot.shot_id):
                    continue        # 已有 run 的镜头不重复排队
                if mode == "assets" and image_refs:
                    shot.spec.reference_assets = image_refs + [
                        r for r in shot.spec.reference_assets
                        if r not in image_refs]
                    store.put("shots", shot)
                runs.append(create_run(shot.shot_id, RunCreate()))
            store.append_event(Event(project_id=pid, type="quick.orchestrated",
                                     actor="orchestrator",
                                     summary=f"{len(shots)} shots, "
                                             f"{len(runs)} runs queued"))
        except Exception as exc:
            log.exception("produce failed for %s", pid)
            store.append_event(Event(project_id=pid, type="quick.failed",
                                     actor="orchestrator", summary=str(exc)[:300]))
        finally:
            with produce_lock:
                producing.discard(pid)
            _clear_stop_event(pid)

    @app.post("/projects/{project_id}/produce", status_code=202)
    def produce_project(project_id: str) -> dict:
        """开始生成：工作室预览确认后手动触发（定妆→首帧→逐镜排队）。

        幂等：已有 run 的镜头不重复排队；同一项目并发请求只跑一个生产线程。
        """
        project = _get_or_404("projects", project_id)
        shots = store.all("shots", project_id=project_id)
        if not shots:
            raise HTTPException(409, "project has no shots")
        if not project.auto_compose:
            # 手动点了「开始生成」= 要一条完整成片:验收后自动拼接
            project.auto_compose = True
            store.put("projects", project)
        with produce_lock:
            if project_id in producing:
                return {"status": "producing", "shots": len(shots)}
            producing.add(project_id)
        _clear_stop_event(project_id)   # 清除上一次的停止标记,重新开始
        store.append_event(Event(project_id=project_id, type="quick.started",
                                 actor="api", summary="manual start"))
        threading.Thread(target=_produce_shots, args=(project, "text", [], True),
                         name=f"svf-produce-{project_id}", daemon=True).start()
        # 已经全部验收的项目(如历史项目补点开始生成):直接补一次拼接
        maybe_auto_compose(store, asset_store, project_id)
        return {"status": "producing", "shots": len(shots)}

    @app.post("/projects/{project_id}/stop", status_code=202)
    def stop_project(project_id: str) -> dict:
        """停止项目的一切执行：生产线程协作退出 + 取消全部在途 run。

        服务端行为，关掉浏览器后依然有效；已验收的镜头与素材保留，
        之后可以随时重新「开始生成」（会重新排队未完成的镜头）。
        """
        _get_or_404("projects", project_id)
        _stop_event(project_id).set()
        cancelled, forced = [], []
        for run in store.all("runs", project_id=project_id):
            if run.state in TERMINAL:
                continue
            try:
                engine.cancel_run(run.run_id)
                cancelled.append(run.run_id)
            except (ValueError, KeyError):
                # 瞬态(GENERATED 等)状态机不许 cancel,直接落 CANCELLED
                fresh = store.get("runs", run.run_id)
                if fresh is not None and fresh.state not in TERMINAL:
                    fresh.state = "CANCELLED"
                    fresh.updated_at = now_ts()
                    store.put("runs", fresh)
                    forced.append(run.run_id)
        with produce_lock:
            producing.discard(project_id)
        store.append_event(Event(project_id=project_id,
                                 type="produce.stop_requested", actor="api",
                                 summary=f"runs: {len(cancelled)} requested, "
                                         f"{len(forced)} forced"))
        return {"status": "stopping", "runs": cancelled + forced,
                "accepted_kept": sum(1 for s in
                                     store.all("shots", project_id=project_id)
                                     if s.accepted_run_id)}

    @app.post("/projects/{project_id}/confirm", status_code=202)
    def confirm_project(project_id: str) -> dict:
        """确认素材(定妆/场景图/首帧)：解除暂停，开始渲染镜头并自动拼接。

        一键出片「素材后确认」模式的第二步；确认后项目转全自动
        （渲染 → 质检 → 全部验收后自动拼接），无需再次确认。
        """
        project = _get_or_404("projects", project_id)
        shots = store.all("shots", project_id=project_id)
        if not shots:
            raise HTTPException(409, "project has no shots")
        if project.confirm_after_assets:
            # 一次性开关:确认后本次生产转全自动
            project.confirm_after_assets = False
            store.put("projects", project)
        with produce_lock:
            if project_id in producing:
                return {"status": "producing", "shots": len(shots)}
            producing.add(project_id)
        store.append_event(Event(project_id=project_id, type="produce.confirmed",
                                 actor="api", summary="materials confirmed"))
        threading.Thread(target=_produce_shots,
                         args=(project, "text", [], True),
                         name=f"svf-confirm-{project_id}", daemon=True).start()
        return {"status": "producing", "shots": len(shots)}

    # -------------------------------------------------------------- oneclick --
    @app.post("/projects/oneclick", status_code=201)
    def oneclick_project(body: OneClickCreate) -> dict:
        """一句话一键出片:提取显式信息 -> 自动补齐 -> 直接开始生成(全自动)。

        与「一键成片」的区别:不需要人工确认分镜,创建后立即跑
        规划分镜 -> 定妆 -> 首帧 -> 逐镜渲染 -> 质检 -> 自动拼接;
        进度通过 /tasks 与项目详情实时可见,可随时 /projects/{id}/stop。
        """
        text = (body.text or "").strip()
        material_text = (body.material_text or "").strip()
        material_name = (body.material_name or "").strip()
        if not material_text and body.material_docx_b64:
            try:
                material_text = _extract_docx_text(body.material_docx_b64).strip()
            except Exception as exc:
                raise HTTPException(422, f"docx 解析失败：{exc}") from exc
        if not material_text and not text:
            raise HTTPException(422, "请先描述一句要拍什么，或上传材料（md / txt / docx）")
        if text_model is None:
            raise HTTPException(503, "text model not configured")
        from ...agents.planner import extract_oneclick_params
        extracted = extract_oneclick_params(text or material_text[:200])
        title = (os.path.splitext(material_name)[0].strip() if material_name
                 else "") or text[:20] or "材料短片"
        title = _clean_title(title) or "材料短片"
        brief = _clean_material_brief(text) if text else _clean_material_brief(material_text[:200])
        project = Project(
            title=title[:40], brief=brief, style="",
            duration_target_s=body.duration_target_s
            or extracted.get("duration_target_s") or 30,
            aspect_ratio=body.aspect_ratio
            or extracted.get("aspect_ratio") or "9:16",
            auto_compose=True,
            confirm_after_assets=body.confirm_after_assets,
            source_material_name=material_name[:120])
        store.put("projects", project)
        if material_text:
            material_text = material_text[:24000]
            try:
                asset = asset_store.save_bytes(
                    material_text.encode("utf-8"), project.project_id, "text",
                    f"{_clean_title(os.path.splitext(material_name)[0]) or 'material'}.txt",
                    source="imported")
                asset.status = "READY"
                asset.category = "other"
                store.put("assets", asset)
                project.source_material_asset_id = asset.asset_id
                store.put("projects", project)
            except Exception:
                log.warning("material asset save failed", exc_info=True)
        store.append_event(Event(project_id=project.project_id,
                                 type="project.created", actor="api",
                                 summary=f"oneclick: {project.title}"
                                         + (" (material)" if material_text else "")))
        with produce_lock:
            producing.add(project.project_id)
        threading.Thread(
            target=_run_oneclick,
            args=(project, text,
                  {"text": material_text, "name": material_name}
                  if material_text else None),
            name=f"svf-oneclick-{project.project_id}",
            daemon=True).start()
        return {"project_id": project.project_id, "extracted": extracted,
                "duration_target_s": project.duration_target_s,
                "aspect_ratio": project.aspect_ratio,
                "material_name": material_name or None,
                "material_chars": len(material_text),
                "material_asset_id": project.source_material_asset_id or None}

    def _run_oneclick(project: Project, text: str,
                      material: Optional[dict] = None) -> None:
        """一句话出片的后台编排:助理补全 -> 结构化分镜 -> 定妆/首帧/排队。

        material 模式:材料先被确定性切成有序段落,每个段落对应一个场景,
        导演逐段展开 —— 成片与材料逐段对应(高覆盖)。
        """
        pid = project.project_id

        def ev(type_: str, actor: str, summary: str = "") -> Event:
            event = Event(project_id=pid, type=type_, actor=actor, summary=summary)
            store.append_event(event)
            return event

        try:
            ev("quick.started", "orchestrator", "oneclick")
            from ...agents.planner import run_assistant_plan
            if material and material.get("text"):
                from ...agents.planner import split_material
                beats = split_material(
                    material["text"],
                    max_beats=max(1, project.duration_target_s // 5))
                if not beats:
                    raise RuntimeError("材料内容为空，无法拆分镜")
                ev("material.split", "orchestrator",
                   f"{material.get('name') or 'material'}: {len(beats)} 段")
                proposal = {
                    "title": project.title,
                    "brief": ((text + " ") if text else "")
                    + material["text"][:800],
                    "style": "",
                    "duration_target_s": project.duration_target_s,
                    "aspect_ratio": project.aspect_ratio}
                messages = ([{"role": "user", "content": text}]
                            if text else [])
                out = run_assistant_plan(
                    text_model, messages, proposal,
                    material={"beats": beats,
                              "name": material.get("name", "")})
            else:
                from ...agents.assistant import run_assistant_chat
                messages = [{"role": "user", "content": text}]
                proposal = None
                try:
                    turn = run_assistant_chat(text_model, messages)
                    proposal = turn.get("proposal")
                except Exception:
                    log.warning("oneclick proposal failed for %s", pid,
                                exc_info=True)
                if isinstance(proposal, dict):
                    # 标题/风格/brief 让助理补;时长与画幅以创建时定的为准
                    if proposal.get("title"):
                        project.title = str(proposal["title"])[:40]
                    if proposal.get("style") and not project.style:
                        project.style = str(proposal["style"])[:200]
                    if proposal.get("brief"):
                        project.brief = str(proposal["brief"])[:1200]
                    proposal["duration_target_s"] = project.duration_target_s
                    proposal["aspect_ratio"] = project.aspect_ratio
                    store.put("projects", project)
                out = run_assistant_plan(text_model, messages, proposal)
            plan = out.get("plan") or {}
            stats = _materialize_plan(store, project, plan, ev)
            ev("plan.completed", "orchestrator",
               f"{stats['scenes']} scenes / {stats['shots']} shots (oneclick)")
            if not stats["shots"]:
                raise RuntimeError("规划没有产出可用镜头")
            _produce_shots(project, "text", [])
        except Exception as exc:
            log.exception("oneclick failed for %s", pid)
            ev("quick.failed", "orchestrator", str(exc)[:300])
        finally:
            with produce_lock:
                producing.discard(pid)

    # ----------------------------------------------------------------- tasks --
    @app.get("/tasks")
    def list_tasks() -> dict:
        """任务中心：所有项目的执行阶段 / 逐镜头进度 / 当前活动 / 管理标记。

        单次扫描聚合（项目数在单机规模下很小），前端 5 秒轮询即可；
        停止/取消/重试的动作复用 /projects/{id}/stop 与 /runs/{id}/commands。
        """
        projects = sorted(store.all("projects"),
                          key=lambda p: p.created_at, reverse=True)
        runs_all = store.all("runs")
        shots_all = store.all("shots")
        scenes_all = store.all("scenes")
        assets_all = store.all("assets")
        events_all = sorted(store.all("events"), key=lambda e: e.seq)
        last_run_event: dict[str, Event] = {}
        for e in events_all:
            if e.run_id:
                last_run_event[e.run_id] = e
        now = now_ts()
        active_states = {"RENDERING", "SCORING"}
        waiting_states = {"PLANNED", "ASSET_READY", "PROMPT_READY", "QUEUED",
                          "RETRY_WAIT", "REPAIRING", "CANCEL_REQUESTED"}
        stage_map = {
            "quick.started": ("planning", "规划中"),
            "plan.started": ("planning", "规划中"),
            "plan.completed": ("preparing", "准备生成"),
            "casting.started": ("casting", "生成定妆 / 场景参考图"),
            "casting.completed": ("preparing", "准备生成"),
            "first_frame.started": ("first_frame", "生成镜头首帧"),
            "first_frame.completed": ("preparing", "准备生成"),
        }
        summary = {"active": 0, "waiting": 0, "review": 0, "completed": 0,
                   "awaiting": 0, "idle": 0}
        out_projects = []
        for p in projects:
            pid = p.project_id
            p_shots = sorted([s for s in shots_all if s.project_id == pid],
                             key=lambda s: (s.scene_id, s.order))
            p_runs = [r for r in runs_all if r.project_id == pid]
            p_assets = [a for a in assets_all if a.project_id == pid]
            shot_no = {s.shot_id: i + 1 for i, s in enumerate(p_shots)}
            accepted = sum(1 for s in p_shots if s.accepted_run_id)
            active_runs = [r for r in p_runs if r.state in active_states]
            waiting_runs = [r for r in p_runs if r.state in waiting_states]
            review_runs = [r for r in p_runs if r.state == "HUMAN_REVIEW"]
            failed_runs = [r for r in p_runs if r.state == "FAILED"]
            is_producing = pid in producing

            produce_stage = ""
            if is_producing:
                for e in reversed(events_all):
                    if e.project_id == pid and e.type in stage_map:
                        produce_stage = e.type
                        break

            # 素材后确认模式:最后一次生产标记是否为「待确认」
            last_marker = next(
                (e for e in reversed(events_all)
                 if e.project_id == pid and e.type in (
                     "quick.started", "quick.orchestrated", "quick.failed",
                     "produce.stopped", "produce.stop_requested",
                     "produce.awaiting_confirm", "produce.confirmed")),
                None)
            awaiting_confirm = bool(last_marker
                                    and last_marker.type == "produce.awaiting_confirm")

            if p_shots and accepted == len(p_shots) \
                    and not active_runs and not waiting_runs:
                stage, stage_label = "done", "已完成"
                summary["completed"] += 1
            elif active_runs:
                is_render = any(r.state == "RENDERING" for r in active_runs)
                stage = "rendering" if is_render else "scoring"
                stage_label = "渲染中" if is_render else "质检中"
                summary["active"] += 1
            elif review_runs:
                stage, stage_label = "review", "待人工审核"
                summary["review"] += 1
            elif awaiting_confirm:
                stage, stage_label = "awaiting_confirm", "素材已就绪 · 待确认"
                summary["awaiting"] += 1
            elif waiting_runs:
                stage, stage_label = "queued", "排队中"
                summary["waiting"] += 1
            elif is_producing:
                stage, stage_label = stage_map.get(produce_stage,
                                                   ("preparing", "准备中"))
                summary["active"] += 1
            elif p_shots:
                stage, stage_label = "planned", "待生成"
                summary["idle"] += 1
            else:
                stage, stage_label = "empty", "空白项目"
                summary["idle"] += 1

            current = None
            current_run = None
            for pool in (active_runs, waiting_runs, review_runs):
                if pool:
                    current_run = sorted(pool, key=lambda r: r.created_at)[0]
                    break
            if current_run is not None:
                ev = last_run_event.get(current_run.run_id)
                current = {
                    "run_id": current_run.run_id,
                    "shot_no": shot_no.get(current_run.shot_id),
                    "state": current_run.state,
                    "repair_count": current_run.repair_count,
                    "elapsed_s": int(now - current_run.created_at),
                    "summary": ev.summary if ev else "",
                }

            run_items = []
            for r in sorted(p_runs, key=lambda r: r.created_at):
                ev = last_run_event.get(r.run_id)
                rep = store.get("score_reports", r.run_id)
                run_items.append({
                    "run_id": r.run_id,
                    "shot_no": shot_no.get(r.shot_id),
                    "state": r.state,
                    "seed": r.seed,
                    "repair_count": r.repair_count,
                    "created_at": r.created_at,
                    "updated_at": r.updated_at,
                    "elapsed_s": int(now - r.created_at),
                    "verdict": rep.verdict if rep else "",
                    "summary": ev.summary if ev else "",
                    "cancellable": r.state not in TERMINAL,
                    "retryable": r.state in ("RETRY_WAIT", "HUMAN_REVIEW"),
                })

            last_ev = next((e for e in reversed(events_all)
                            if e.project_id == pid), None)
            out_projects.append({
                "project_id": pid,
                "title": p.title,
                "style": p.style,
                "aspect_ratio": p.aspect_ratio,
                "duration_target_s": p.duration_target_s,
                "created_at": p.created_at,
                "producing": is_producing,
                "stage": stage,
                "stage_label": stage_label,
                "progress": round(accepted / len(p_shots), 3) if p_shots else 0,
                "counts": {
                    "scenes": sum(1 for s in scenes_all
                                  if s.project_id == pid),
                    "shots": len(p_shots),
                    "accepted": accepted,
                    "active": len(active_runs),
                    "queued": len(waiting_runs),
                    "review": len(review_runs),
                    "failed": len(failed_runs),
                    "cancelled": sum(1 for r in p_runs
                                     if r.state == "CANCELLED"),
                    "exports": sum(1 for a in p_assets
                                   if a.source == "derived"
                                   and a.media_type == "video"),
                },
                "current": current,
                "awaiting_confirm": awaiting_confirm,
                "material_name": p.source_material_name,
                "last_event": ({"type": last_ev.type,
                                "summary": last_ev.summary,
                                "timestamp": last_ev.timestamp}
                               if last_ev is not None else None),
                "stoppable": is_producing or bool(active_runs)
                or bool(waiting_runs),
                "runs": run_items,
            })
        summary["paused"] = engine.paused
        return {"summary": summary, "projects": out_projects, "now": now}

    @app.post("/engine/pause")
    def engine_pause() -> dict:
        """暂停全部在途任务（引擎下一轮询前停下；已在途的调用会跑完）。"""
        engine.pause_all()
        return {"status": "paused", "paused": True}

    @app.post("/engine/resume")
    def engine_resume() -> dict:
        """恢复全部暂停的任务。"""
        engine.resume_all()
        return {"status": "running", "paused": False}

    @app.get("/projects/{project_id}")
    def get_project(project_id: str, prompts: bool = False) -> dict:
        """项目全量快照。prompts=1 时额外返回每镜的渲染提示词预览。

        预览由 _compose_prompt 现场组装,与提交生成时写入 run.prompt_spec
        的文本一致(手动链路与对话链路同一消费端),便于在工作室核对。
        """
        project = _get_or_404("projects", project_id)
        scenes = sorted(store.all("scenes", project_id=project_id),
                        key=lambda s: s.order)
        shots = store.all("shots", project_id=project_id)
        runs = store.all("runs", project_id=project_id)
        runs_by_shot: dict[str, list[Run]] = {}
        for r in runs:
            runs_by_shot.setdefault(r.shot_id, []).append(r)
        shot_order = sorted(shots, key=lambda s: (s.scene_id, s.order))
        prompt_map: dict[str, str] = {}
        if prompts:
            ordered: dict[str, list[Shot]] = {}
            for shot in shot_order:
                ordered.setdefault(shot.scene_id, []).append(shot)
            for group in ordered.values():
                group.sort(key=lambda s: s.order)
                for i, shot in enumerate(group):
                    prev = group[i - 1] if i > 0 else None
                    prompt_map[shot.shot_id] = _compose_prompt(project, shot, prev)
        out_scenes = []
        for scene in scenes:
            scene_shots = sorted([s for s in shots if s.scene_id == scene.scene_id],
                                 key=lambda s: s.order)
            # 拍平:场景字段置顶 + shots 数组,前端直接读 sc.title/sc.scene_id
            out_scenes.append({
                **scene.model_dump(),
                "shots": [{"shot": s,
                           "runs": sorted(runs_by_shot.get(s.shot_id, []),
                                          key=lambda r: r.created_at)}
                          for s in scene_shots],
            })
        return {"project": project, "scenes": out_scenes,
                "shots": [{**s.model_dump(),
                           **({"prompt_preview": prompt_map[s.shot_id]}
                              if prompts and s.shot_id in prompt_map else {})}
                          for s in shot_order],
                "runs": sorted(runs, key=lambda r: r.created_at),
                "counts": {"scenes": len(scenes), "shots": len(shots),
                           "runs": len(runs),
                           "accepted": sum(1 for s in shots if s.accepted_run_id)}}

    @app.delete("/projects/{project_id}")
    def delete_project(project_id: str) -> dict:
        """删除项目全部记录;素材与成片媒体文件保留在磁盘(单机资产不物理删除)。

        删除前先停掉在途工作:生产线程协作退出、非终态 run 取消、渲染任务打断——
        否则后台线程会在删除后继续写库/排队(僵尸任务继续吃 GPU)。
        """
        _get_or_404("projects", project_id)
        _stop_event(project_id).set()
        cancelled: list[str] = []
        for run in store.all("runs", project_id=project_id):
            if run.state in TERMINAL:
                continue
            try:
                engine.cancel_run(run.run_id)
                cancelled.append(run.run_id)
            except Exception:
                fresh = store.get("runs", run.run_id)
                if fresh is not None and fresh.state not in TERMINAL:
                    fresh.state = "CANCELLED"
                    fresh.updated_at = now_ts()
                    store.put("runs", fresh)
                    cancelled.append(run.run_id)
        if cancelled and renderer is not None:
            interrupt = getattr(renderer, "interrupt", None)
            if callable(interrupt):
                try:
                    interrupt()      # 打断 ComfyUI 当前任务,立即释放 GPU
                except Exception:
                    log.warning("renderer interrupt failed", exc_info=True)
        with produce_lock:
            producing.discard(project_id)
        removed = {}
        for table in ("runs", "shots", "scenes", "characters",
                      "director_reviews"):
            rows = store.all(table, project_id=project_id)
            pk = {"runs": "run_id", "shots": "shot_id", "scenes": "scene_id",
                  "characters": "character_id",
                  "director_reviews": "report_id"}[table]
            for row in rows:
                store.delete(table, getattr(row, pk))
            removed[table] = len(rows)
        removed["cancelled_runs"] = cancelled
        store.delete("projects", project_id)
        store.append_event(Event(project_id=project_id, type="project.deleted",
                                 actor="api",
                                 summary=str(removed)))
        return {"deleted": project_id, "removed": removed}

    # -------------------------------------------------------------- uploads --
    @app.post("/assets/uploads", status_code=201)
    def create_upload(body: UploadCreate):
        _get_or_404("projects", body.project_id)
        return uploader.create(body.project_id, body.filename, body.total_size,
                               body.chunk_size)

    @app.put("/assets/uploads/{upload_id}/parts/{part_no}")
    async def put_part(upload_id: str, part_no: int, request: Request):
        data = await request.body()
        try:
            return uploader.write_part(upload_id, part_no, data)
        except KeyError:
            raise HTTPException(404, f"upload {upload_id} not found")
        except UploadError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/assets/uploads/{upload_id}/complete")
    def complete_upload(upload_id: str, body: Optional[CompleteRequest] = None):
        body = body or CompleteRequest()
        try:
            return uploader.complete(upload_id, sha256=body.sha256,
                                     media_type=body.media_type)
        except KeyError:
            raise HTTPException(404, f"upload {upload_id} not found")
        except UploadError as exc:
            raise HTTPException(400, str(exc))

    # ------------------------------------------------------------ assistant --
    @app.post("/assistant/chat")
    def assistant_chat(body: ChatTurnRequest) -> dict:
        """对话出片的一轮回复。只读:方案确认/出片由前端另调 /projects/quick。"""
        if text_model is None:
            raise HTTPException(503, "text model not configured")
        from ...agents.assistant import run_assistant_chat
        try:
            return run_assistant_chat(text_model, body.messages)
        except HTTPException:
            raise
        except Exception as exc:
            log.warning("assistant chat failed: %s", exc, exc_info=True)
            raise HTTPException(502, f"assistant unavailable: {exc}") from exc

    @app.post("/assistant/plan")
    def assistant_plan(body: ChatPlanRequest) -> dict:
        """对话出片：把已聊齐的需求拆成完整结构化分镜（场次/角色/镜头）。

        只产出方案供前端预览（对话内直接看到每镜渲染提示词的字段结构），
        点「一键出片」才调 /projects/quick 携带 plan 落库并开始生成。
        """
        if text_model is None:
            raise HTTPException(503, "text model not configured")
        from ...agents.planner import run_assistant_plan
        try:
            return run_assistant_plan(text_model, body.messages, body.proposal)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            log.warning("assistant plan failed: %s", exc, exc_info=True)
            raise HTTPException(502, f"assistant unavailable: {exc}") from exc

    # --------------------------------------------------------------- assets --
    @app.get("/assets")
    def list_assets(project_id: Optional[str] = None,
                    category: Optional[str] = None,
                    media_type: Optional[str] = None) -> list:
        where = {}
        if project_id:
            where["project_id"] = project_id
        if category:
            where["category"] = category
        if media_type:
            where["media_type"] = media_type
        return store.all("assets", **where)

    @app.get("/assets/gallery")
    def assets_gallery(project_id: Optional[str] = None) -> dict:
        """中途资产画廊:按「项目 -> 生产阶段」聚合全部资产。

        阶段判定优先用生产结构(比分类器可靠):材料/定妆/场景参考/镜头首帧/
        镜头片段(含 TAKE 序号与是否被采用)/成片/其他;项目已删除的资产归入
        orphans(文件仍在磁盘)。
        """
        projects = store.all("projects")
        scenes = store.all("scenes")
        shots = store.all("shots")
        runs = store.all("runs")
        chars = store.all("characters")
        bindings = store.all("bindings")
        assets = store.all("assets")

        scene_order = {s.scene_id: s.order for s in scenes}
        by_project_shots: dict[str, list[Shot]] = {}
        for shot in shots:
            by_project_shots.setdefault(shot.project_id, []).append(shot)
        shot_no: dict[str, int] = {}
        for group in by_project_shots.values():
            group.sort(key=lambda s: (scene_order.get(s.scene_id, 0), s.order))
            for i, shot in enumerate(group, start=1):
                shot_no[shot.shot_id] = i
        shot_by_id = {s.shot_id: s for s in shots}

        # run -> 候选视频;同一镜头的 run 按时间编号为 TAKE 1/2/3…
        run_by_asset: dict[str, Run] = {}
        take_no: dict[str, int] = {}
        shot_take_counter: dict[str, int] = {}
        for run in sorted(runs, key=lambda r: r.created_at):
            shot_take_counter[run.shot_id] = \
                shot_take_counter.get(run.shot_id, 0) + 1
            take_no[run.run_id] = shot_take_counter[run.shot_id]
            for aid in run.candidate_asset_ids:
                run_by_asset[aid] = run

        char_by_asset = {c.asset_id: c for c in chars if c.asset_id}
        first_frame_shots = {b.asset_id: b.shot_id for b in bindings
                             if b.role == "first_frame"}

        groups = ("material", "portrait", "location", "first_frame",
                  "clip", "export", "other")
        buckets: dict[str, dict[str, list]] = {}
        orphans: list[dict] = []
        project_by_id = {p.project_id: p for p in projects}
        latest: dict[str, float] = {}

        def item_of(a: Asset) -> dict:
            meta = a.metadata or {}
            return {
                "asset_id": a.asset_id,
                "media_type": a.media_type,
                "source": a.source,
                "category": a.category,
                "filename": meta.get("original_filename")
                or a.storage_key.rsplit("/", 1)[-1],
                "size": meta.get("size_bytes") or meta.get("size") or 0,
                "width": meta.get("width"),
                "height": meta.get("height"),
                "created_at": a.created_at,
            }

        for asset in sorted(assets, key=lambda a: a.created_at):
            if project_id and asset.project_id != project_id:
                continue
            item = item_of(asset)
            group = "other"
            if asset.source == "derived":
                group = "export"
            elif asset.media_type == "text" or asset.source == "imported":
                group = "material"
            elif asset.media_type == "image":
                ch = char_by_asset.get(asset.asset_id)
                if ch is not None:
                    group = "portrait" if ch.kind == "character" else "location"
                    item["label"] = ch.name
                elif asset.asset_id in first_frame_shots:
                    group = "first_frame"
                    item["shot_id"] = first_frame_shots[asset.asset_id]
                    item["shot_no"] = shot_no.get(item["shot_id"])
                elif asset.source == "generated":
                    group = "other"
                else:
                    group = "material"
            elif asset.media_type == "video":
                run = run_by_asset.get(asset.asset_id)
                if run is not None:
                    group = "clip"
                    shot = shot_by_id.get(run.shot_id)
                    item.update({
                        "run_id": run.run_id,
                        "shot_id": run.shot_id,
                        "shot_no": shot_no.get(run.shot_id),
                        "take": take_no.get(run.run_id),
                        "seed": run.seed,
                        "run_state": run.state,
                        "accepted": bool(shot
                                         and shot.accepted_run_id == run.run_id),
                    })
                elif asset.source == "generated":
                    group = "clip"
                else:
                    group = "material"
            elif asset.media_type == "audio":
                group = "material"
            item["group"] = group
            if asset.project_id in project_by_id:
                bucket = buckets.setdefault(
                    asset.project_id, {g: [] for g in groups})
                bucket[group].append(item)
                latest[asset.project_id] = max(
                    latest.get(asset.project_id, 0), asset.created_at)
            else:
                orphans.append(item)

        out_projects = []
        for pid, bucket in buckets.items():
            project = project_by_id[pid]
            counts = {g: len(bucket[g]) for g in groups}
            if not any(counts.values()):
                continue
            out_projects.append({
                "project_id": pid,
                "title": project.title,
                "aspect_ratio": project.aspect_ratio,
                "duration_target_s": project.duration_target_s,
                "material_name": project.source_material_name,
                "updated_at": latest.get(pid, project.created_at),
                "counts": counts,
                "groups": bucket,
            })
        out_projects.sort(key=lambda p: p["updated_at"], reverse=True)
        return {"projects": out_projects, "orphans": orphans,
                "groups": list(groups)}

    @app.post("/assets/{asset_id}/category")
    def set_asset_category(asset_id: str, body: CategorySet) -> Asset:
        asset = _get_or_404("assets", asset_id)
        asset.category = body.category
        store.put("assets", asset)
        store.append_event(Event(project_id=asset.project_id,
                                 type="asset.categorized", actor="api",
                                 summary=f"{asset.asset_id}: {body.category}"))
        return asset

    @app.post("/projects/{project_id}/assets/classify")
    def classify_assets(project_id: str) -> dict:
        _get_or_404("projects", project_id)
        clf = classifier or _make_classifier()
        classified = {}
        for asset in store.all("assets", project_id=project_id):
            if asset.category:
                continue                # 只分类未分类素材
            asset.category = clf.classify(asset)
            store.put("assets", asset)
            classified[asset.asset_id] = asset.category
        store.append_event(Event(project_id=project_id, type="assets.classified",
                                 actor="classifier",
                                 summary=f"{len(classified)} assets classified"))
        return {"classified": classified}

    def _make_classifier():
        from ...agents.classifier import LayaAssetClassifier
        return LayaAssetClassifier()

    @app.get("/assets/{asset_id}/file")
    def get_asset_file(asset_id: str):
        asset = _get_or_404("assets", asset_id)
        if not asset.storage_key:
            raise HTTPException(404, "asset has no stored file")
        path = asset_store.path_for(asset.storage_key)
        filename = asset.metadata.get("original_filename", "file")
        # FileResponse 自带 HTTP Range 支持（starlette>=1.0，视频拖动返回 206）
        return FileResponse(
            path, media_type=_MEDIA_MIME.get(asset.media_type,
                                             "application/octet-stream"),
            filename=filename, content_disposition_type="inline")

    # ----------------------------------------------------------- characters --
    @app.get("/projects/{project_id}/characters")
    def list_characters(project_id: str) -> dict:
        _get_or_404("projects", project_id)
        chars = sorted(store.all("characters", project_id=project_id),
                       key=lambda c: c.created_at)
        return {"characters": chars}

    @app.post("/projects/{project_id}/characters", status_code=201)
    def create_character(project_id: str, body: CharacterCreate) -> Character:
        _get_or_404("projects", project_id)
        if body.asset_id:
            _get_or_404("assets", body.asset_id)
        ch = Character(project_id=project_id, name=body.name, kind=body.kind,
                       description=body.description,
                       asset_id=body.asset_id or "")
        store.put("characters", ch)
        store.append_event(Event(project_id=project_id, type="character.created",
                                 actor="api", summary=f"{ch.kind}: {ch.name}"))
        return ch

    @app.patch("/characters/{character_id}")
    def patch_character(character_id: str, body: CharacterPatch) -> Character:
        ch = _get_or_404("characters", character_id)
        if body.description is not None:
            ch.description = body.description
        if body.kind is not None:
            ch.kind = body.kind
        if body.asset_id is not None:
            if body.asset_id:
                _get_or_404("assets", body.asset_id)
            ch.asset_id = body.asset_id
        store.put("characters", ch)
        store.append_event(Event(project_id=ch.project_id,
                                 type="character.updated", actor="api",
                                 summary=ch.name))
        return ch

    @app.delete("/characters/{character_id}")
    def delete_character(character_id: str) -> dict:
        if store.get("characters", character_id) is None:
            raise HTTPException(404, f"characters/{character_id} not found")
        store.delete("characters", character_id)
        return {"status": "deleted"}

    @app.post("/shots/{shot_id}/characters")
    def bind_shot_characters(shot_id: str, body: ShotCharactersBind) -> Shot:
        shot = _get_or_404("shots", shot_id)
        chars = []
        for cid in body.character_ids:
            ch = _get_or_404("characters", cid)
            chars.append(ch)
        shot.spec.characters = [
            {"character_id": c.character_id, "name": c.name, "kind": c.kind,
             "description": c.description} for c in chars]
        # 有参考图的角色 asset 合入 reference_assets：去重、保持顺序，
        # 第一个角色参考图放最前（渲染只取第一张）
        char_refs = [c.asset_id for c in chars if c.asset_id]
        shot.spec.reference_assets = char_refs + [
            a for a in shot.spec.reference_assets if a not in char_refs]
        store.put("shots", shot)
        store.append_event(Event(project_id=shot.project_id,
                                 type="shot.characters_bound", actor="api",
                                 summary=", ".join(c.name for c in chars)))
        return shot

    @app.patch("/shots/{shot_id}")
    def patch_shot(shot_id: str, body: ShotPatch) -> Shot:
        """部分更新 shot.spec（分镜页生成前编辑）。"""
        shot = _get_or_404("shots", shot_id)
        if body.action is not None:
            shot.spec.action = body.action
        if body.dialogue is not None:
            shot.spec.dialogue = body.dialogue
        if body.duration_s is not None:
            shot.spec.duration_s = body.duration_s
        if body.camera is not None:
            shot.spec.camera = {str(k): str(v) for k, v in body.camera.items()}
        if body.lighting_palette is not None:
            shot.spec.lighting_palette = body.lighting_palette.strip()
        if body.object_states is not None:
            shot.spec.object_states = [
                {"name": str(o.get("name") or ""),
                 "count": str(o.get("count") or ""),
                 "start_state": str(o.get("start_state") or ""),
                 "end_state": str(o.get("end_state") or "")}
                for o in body.object_states
                if isinstance(o, dict) and str(o.get("name") or "").strip()]
        store.put("shots", shot)
        store.append_event(Event(project_id=shot.project_id, type="shot.updated",
                                 actor="api", summary=shot.shot_id))
        return shot

    @app.post("/projects/{project_id}/shots", status_code=201)
    def create_manual_shot(project_id: str, body: ManualShotCreate) -> Shot:
        """创建可立即编辑和生成的手动分镜，解决 AI 规划为空时无法继续的问题。"""
        project = _get_or_404("projects", project_id)
        action = body.action.strip()
        if not action:
            raise HTTPException(422, "镜头画面描述不能为空")

        scenes = sorted(store.all("scenes", project_id=project_id),
                        key=lambda scene: scene.order)
        scene = None
        if body.scene_id:
            scene = next((item for item in scenes if item.scene_id == body.scene_id), None)
            if scene is None:
                raise HTTPException(404, f"scenes/{body.scene_id} not found")
        elif scenes:
            scene = scenes[0]
        else:
            scene = Scene(scene_id=new_id("scene"), project_id=project_id,
                          order=0, title="场景 01", summary="手动补充场景")
            store.put("scenes", scene)

        scene_shots = store.all("shots", project_id=project_id, scene_id=scene.scene_id)
        shot_id = new_id("shot")
        shot = Shot(
            shot_id=shot_id,
            scene_id=scene.scene_id,
            project_id=project_id,
            order=max((item.order for item in scene_shots), default=-1) + 1,
            spec=ShotSpec(shot_id=shot_id, duration_s=body.duration_s,
                          action=action, dialogue=body.dialogue.strip(),
                          aspect_ratio=project.aspect_ratio,
                          acceptance=constrained_acceptance()),
        )
        store.put("shots", shot)
        store.append_event(Event(project_id=project_id, type="shot.created",
                                 actor="api", summary=f"manual: {shot_id}"))
        return shot

    @app.delete("/shots/{shot_id}/references/{asset_id}")
    def remove_shot_reference(shot_id: str, asset_id: str) -> Shot:
        """从 spec.reference_assets 移除指定参考图（不存在则幂等不动）。"""
        shot = _get_or_404("shots", shot_id)
        if asset_id in shot.spec.reference_assets:
            shot.spec.reference_assets = [
                a for a in shot.spec.reference_assets if a != asset_id]
            store.put("shots", shot)
            store.append_event(Event(project_id=shot.project_id,
                                     type="shot.reference_removed", actor="api",
                                     summary=asset_id))
        return shot

    # ----------------------------------------------------------- shot wiring --
    @app.post("/shots/{shot_id}/asset-bindings", status_code=201)
    def bind_asset(shot_id: str, body: BindingCreate) -> AssetBinding:
        _get_or_404("shots", shot_id)
        asset = _get_or_404("assets", body.asset_id)
        for b in store.all("bindings", shot_id=shot_id):
            if b.asset_id == body.asset_id and b.role == body.role:
                return b            # idempotent re-bind
        binding = AssetBinding(shot_id=shot_id, asset_id=asset.asset_id,
                               asset_version=asset.version, role=body.role,
                               clip_range=body.clip_range)
        store.put("bindings", binding)
        shot = store.get("shots", shot_id)
        # reference/first_frame 绑定的资产同步进 spec.reference_assets（去重），
        # 否则渲染时 _process_generate 看不到参考图
        if body.role in ("reference", "first_frame") \
                and asset.asset_id not in shot.spec.reference_assets:
            shot.spec.reference_assets = shot.spec.reference_assets + [asset.asset_id]
            store.put("shots", shot)
        store.append_event(Event(project_id=shot.project_id, type="asset.bound",
                                 actor="api",
                                 summary=f"{body.role}: {asset.asset_id}"))
        return binding

    @app.post("/shots/{shot_id}/runs", status_code=201)
    def create_run(shot_id: str, body: RunCreate) -> Run:
        shot = _get_or_404("shots", shot_id)
        if body.command_id:
            for r in store.all("runs", shot_id=shot_id):
                if r.input_hash == body.command_id:
                    return r        # same command_id: return the existing run
        project = store.get("projects", shot.project_id)
        # 同场景前一镜（order 小 1）的观测末态用于续接
        prev_shot = None
        if shot.order > 0:
            prev_shot = next(
                (s for s in store.all("shots", scene_id=shot.scene_id)
                 if s.order == shot.order - 1 and s.shot_id != shot.shot_id),
                None)
        run = Run(shot_id=shot.shot_id, project_id=shot.project_id,
                  seed=body.seed if body.seed is not None else settings.DEFAULT_SEED,
                  prompt_spec=_compose_prompt(project, shot, prev_shot),
                  input_hash=body.command_id,
                  max_repairs=(body.max_repairs if body.max_repairs is not None
                               else settings.MAX_REPAIRS))
        store.put("runs", run)

        def ev(type_: str, summary: str = "") -> Event:
            return Event(project_id=run.project_id, run_id=run.run_id,
                         attempt_id=run.attempt_id, type=type_,
                         actor="orchestrator", summary=summary)

        store.transition_run(run.run_id, "ASSET_READY", ev("run.created"))
        if shot.spec.production_mode == "reuse":
            return store.get("runs", run.run_id)
        store.transition_run(run.run_id, "PROMPT_READY",
                             ev("prompt.ready", run.prompt_spec[:200]))
        store.transition_run(run.run_id, "QUEUED", ev("step.queued"))
        # 密度负载 / 裸情绪词静态检查：不减内容、不改写，只记警告事件
        load = density_score(shot.spec)
        hits = emotion_hits(shot.spec)
        if load > settings.DENSITY_LIMIT or hits:
            advice = ("建议拆分镜头" if load > settings.DENSITY_LIMIT else "")
            if hits:
                advice += ("；" if advice else "") + \
                    "裸情绪词无画面载体: " + ",".join(hits)
            store.append_event(Event(
                project_id=run.project_id, run_id=run.run_id,
                attempt_id=run.attempt_id, type="density.warning",
                actor="orchestrator",
                summary=f"load={load:.1f}/{settings.DENSITY_LIMIT} {advice}",
                progress={"load": load, "limit": settings.DENSITY_LIMIT,
                          "emotion_words": hits}))
        return store.get("runs", run.run_id)

    # ------------------------------------------------------------ run control --
    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        run = _get_or_404("runs", run_id)
        return {"run": run, "score_report": store.get("score_reports", run_id)}

    @app.get("/runs/{run_id}/steps")
    def get_run_steps(run_id: str) -> dict:
        _get_or_404("runs", run_id)
        steps = sorted(store.all("events", run_id=run_id), key=lambda e: e.seq)
        return {"steps": steps}

    @app.post("/runs/{run_id}/commands")
    def run_command(run_id: str, body: RunCommand) -> dict:
        _get_or_404("runs", run_id)
        if body.command_id:
            for e in store.all("events", run_id=run_id):
                if e.type == "command.received" and e.summary == body.command_id:
                    return {"status": "duplicate",
                            "run": store.get("runs", run_id)}
        run = store.get("runs", run_id)
        store.append_event(Event(project_id=run.project_id, run_id=run_id,
                                 attempt_id=run.attempt_id,
                                 type="command.received", actor="api",
                                 summary=body.command_id or body.action,
                                 progress={"action": body.action}))
        if body.action == "pause":
            engine.pause_all()
        elif body.action == "resume":
            engine.resume_all()
        elif body.action == "cancel":
            try:
                engine.cancel_run(run_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc))
        elif body.action == "retry":
            _retry_run(run_id)
        else:
            raise HTTPException(400, f"unknown action {body.action}")
        return {"status": "ok", "run": store.get("runs", run_id)}

    def _retry_run(run_id: str) -> None:
        run = store.get("runs", run_id)
        engine.reset_retries(run_id)

        def ev(type_: str, summary: str) -> Event:
            return Event(project_id=run.project_id, run_id=run_id,
                         attempt_id=run.attempt_id, type=type_,
                         actor="orchestrator", summary=summary)

        if run.state == "RETRY_WAIT":
            store.transition_run(run_id, "QUEUED", ev("run.requeued", "manual retry"),
                                 patch={"attempt_id": new_id("att"),
                                        "seed": next_seed(run.seed, run.repair_count + 1)})
            return
        if run.state == "HUMAN_REVIEW":
            store.transition_run(run_id, "REPAIRING", ev("run.repairing", "manual retry"))
            store.put("repair_plans", RepairPlan(
                run_id=run_id, target="prompt", action="manual retry",
                detail="retry requested via API", invalidate_from="PROMPT_READY"))
            store.transition_run(run_id, "PROMPT_READY",
                                 ev("run.invalidated", "manual retry"))
            store.transition_run(run_id, "QUEUED", ev("step.queued", "manual retry"),
                                 patch={"attempt_id": new_id("att"),
                                        "seed": next_seed(run.seed, run.repair_count + 1)})
            return
        raise HTTPException(409, f"cannot retry run in state {run.state}")

    @app.post("/runs/{run_id}/review")
    def review_run(run_id: str, body: ReviewRequest) -> Run:
        run = _get_or_404("runs", run_id)
        if run.state != "HUMAN_REVIEW":
            raise HTTPException(409, f"run is {run.state}, not HUMAN_REVIEW")

        def ev(type_: str, summary: str) -> Event:
            return Event(project_id=run.project_id, run_id=run_id,
                         attempt_id=run.attempt_id, type=type_,
                         actor="human-reviewer", summary=summary)

        if body.decision == "accept":
            store.transition_run(run_id, "ACCEPTED",
                                 ev("run.accepted", body.note or "human accept"))
            shot = store.get("shots", run.shot_id)
            shot.accepted_run_id = run_id
            if body.note:
                shot.review_note = body.note
            store.put("shots", shot)
            # 一键出片:人工验收补齐最后一个镜头时也自动拼接
            try:
                maybe_auto_compose(store, asset_store, run.project_id)
            except Exception:
                log.warning("auto compose trigger failed", exc_info=True)
        elif body.decision == "reject":
            store.transition_run(run_id, "REPAIRING",
                                 ev("run.repairing", body.note or "human reject"))
            store.put("repair_plans", RepairPlan(
                run_id=run_id, target="prompt", action="human rejected",
                detail=body.note, invalidate_from="PROMPT_READY"))
            store.transition_run(run_id, "PROMPT_READY",
                                 ev("run.invalidated", body.note or "rejected"))
            store.transition_run(run_id, "QUEUED",
                                 ev("step.queued", "requeued after human reject"),
                                 patch={"attempt_id": new_id("att"),
                                        "seed": next_seed(run.seed, run.repair_count + 1)})
        elif body.decision == "note":
            store.append_event(ev("review.note", body.note))
            shot = store.get("shots", run.shot_id)
            shot.review_note = body.note
            store.put("shots", shot)
        else:
            raise HTTPException(400, f"unknown decision {body.decision}")
        return store.get("runs", run_id)

    # ---------------------------------------------------------------- export --
    @app.post("/projects/{project_id}/export")
    def export_project(project_id: str, body: Optional[ExportRequest] = None) -> dict:
        """把已验收镜头拼接成完整成片(手动导出;一键出片会自动触发同一逻辑)。"""
        body = body or ExportRequest()
        try:
            return compose_project(store, asset_store, project_id,
                                   subtitles=body.subtitles, actor="api")
        except ComposeError as exc:
            if exc.pending_shot_ids:
                raise HTTPException(409, {"detail": exc.detail,
                                          "pending_shot_ids": exc.pending_shot_ids}) from exc
            raise HTTPException(exc.status_code, exc.detail) from exc

    # ------------------------------------------------------- director review --
    reviewing: set[str] = set()
    review_lock = threading.Lock()

    @app.post("/projects/{project_id}/director-review", status_code=202)
    def director_review(project_id: str) -> dict:
        project = _get_or_404("projects", project_id)
        with review_lock:
            if project_id in reviewing:
                return {"status": "reviewing"}
            reviewing.add(project_id)

        def _run_review() -> None:
            try:
                agent = reviewer or _make_reviewer()
                agent.review(project)
            except Exception:
                log.exception("director review failed for %s", project_id)
            finally:
                with review_lock:
                    reviewing.discard(project_id)

        threading.Thread(target=_run_review, name=f"svf-review-{project_id}",
                         daemon=True).start()
        return {"status": "reviewing"}

    def _make_reviewer():
        from ...agents.reviewer import ReviewerAgent
        return ReviewerAgent(store, asset_store)

    def _make_image_model():
        from ...adapters.image_model.qwen_image import QwenImageModel
        return QwenImageModel(asset_store, store=store)

    @app.post("/projects/{project_id}/characters/portraits", status_code=202)
    def character_portraits(project_id: str) -> dict:
        """为缺参考图的角色/地点生成定妆照与场景参考图(后台线程)。"""
        project = _get_or_404("projects", project_id)

        def _run_portraits() -> None:
            from ...agents.casting import (
                generate_character_portraits, generate_location_references)
            try:
                store.append_event(Event(project_id=project_id,
                                         type="casting.started", actor="api",
                                         summary="generating portraits"))
                model = image_model or _make_image_model()
                made = generate_character_portraits(
                    store, model, project_id, style=project.style)
                made_loc = generate_location_references(
                    store, model, project_id, style=project.style)
                store.append_event(Event(project_id=project_id,
                                         type="casting.completed", actor="api",
                                         summary=f"{len(made)} portraits, "
                                                 f"{len(made_loc)} locations"))
            except Exception as exc:
                log.exception("portrait generation failed for %s", project_id)
                store.append_event(Event(project_id=project_id,
                                       type="casting.failed", actor="api",
                                       summary=str(exc)[:300]))

        threading.Thread(target=_run_portraits,
                         name=f"svf-casting-{project_id}", daemon=True).start()
        return {"status": "casting"}

    @app.post("/projects/{project_id}/shots/first-frames", status_code=202)
    def shot_first_frames(project_id: str) -> dict:
        """为带 object_states 的镜头生成首帧参考图(后台线程,首帧约束)。"""
        project = _get_or_404("projects", project_id)

        def _run_first_frames() -> None:
            from ...agents.first_frame import generate_shot_first_frames
            try:
                store.append_event(Event(project_id=project_id,
                                         type="first_frame.started",
                                         actor="api",
                                         summary="generating first frames"))
                model = image_model or _make_image_model()
                made = generate_shot_first_frames(
                    store, model, project_id, style=project.style)
                store.append_event(Event(project_id=project_id,
                                         type="first_frame.completed",
                                         actor="api",
                                         summary=f"{len(made)} first frames"))
            except Exception as exc:
                log.exception("first-frame generation failed for %s",
                              project_id)
                store.append_event(Event(project_id=project_id,
                                         type="first_frame.failed", actor="api",
                                         summary=str(exc)[:300]))

        threading.Thread(target=_run_first_frames,
                         name=f"svf-firstframe-{project_id}",
                         daemon=True).start()
        return {"status": "generating_first_frames"}

    @app.get("/projects/{project_id}/director-review")
    def get_director_review(project_id: str) -> dict:
        _get_or_404("projects", project_id)
        reports = store.all("director_reviews", project_id=project_id)
        if not reports:
            raise HTTPException(404, "no director review yet")
        latest = max(reports, key=lambda r: r.created_at)
        return {"report": latest}

    # ------------------------------------------------------------------ SSE --
    @app.get("/projects/{project_id}/events")
    async def project_events(project_id: str, request: Request, seq: int = 0):
        _get_or_404("projects", project_id)

        async def gen():
            last = seq
            # backlog first, then live tail
            for e in store.events_since(project_id, last):
                last = e.seq
                yield f"data: {e.model_dump_json()}\n\n"
            while not await request.is_disconnected():
                await asyncio.sleep(1)
                new = store.events_since(project_id, last)
                for e in new:
                    last = e.seq
                    yield f"data: {e.model_dump_json()}\n\n"
                if not new:
                    yield ": hb\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ------------------------------------------------------------- web page --
    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app


# ------------------------------------------------------------- default wiring --
def _load_adapter(candidates: list[tuple[str, list[str], list[tuple]]],
                  label: str):
    """Try (relative module, attr names, ctor arg tuples) in order."""
    for mod_name, attrs, ctor_args in candidates:
        try:
            mod = importlib.import_module(mod_name, package=__package__)
        except Exception as exc:
            log.info("adapter %s: module %s unavailable: %s", label, mod_name, exc)
            continue
        for attr in attrs:
            cls = getattr(mod, attr, None)
            if cls is None:
                continue
            for args in ctor_args:
                try:
                    return cls(*args)
                except Exception as exc:
                    log.info("adapter %s: %s.%s%r failed: %s",
                             label, mod_name, attr, args, exc)
    log.warning("adapter %s: no implementation available, capability disabled",
                label)
    return None


def build_default() -> FastAPI:
    """Wire the real adapters; any that fail to import degrade to None."""
    store = Store(settings.DB_PATH)
    asset_store = _load_adapter([
        ("...adapters.asset_store.local", ["LocalAssetStore"],
         [(settings.ASSET_ROOT,), (str(settings.ASSET_ROOT),), ()]),
        ("...adapters.asset_store", ["LocalAssetStore", "FileAssetStore"],
         [(settings.ASSET_ROOT,), (str(settings.ASSET_ROOT),), ()]),
    ], "asset_store")
    if asset_store is None:
        raise RuntimeError("asset_store adapter is required but unavailable")
    renderer = _load_adapter([
        ("...adapters.comfyui.adapter", ["H3ComfyRenderer"],
         [(asset_store,), ()]),
        ("...adapters.comfyui", ["ComfyRenderer", "ComfyUIRenderer", "Renderer"],
         [(asset_store,), ()]),
    ], "renderer")
    judge = _load_adapter([
        ("...adapters.vision_judge.judge", ["GemmaVisionJudge"],
         [(asset_store,), ()]),
        ("...adapters.vision_judge", ["VisionJudge", "OllamaVisionJudge", "Judge"],
         [(asset_store,), ()]),
    ], "judge")
    decision = _load_adapter([
        ("...adapters.decision.laya_shadow", ["LayaShadowDecision"], [()]),
        ("...adapters.decision", ["DecisionAdapter", "OpenJevDecision",
                                  "LayaDecision", "Decision"],
         [(), (settings.OPENJEV_BASE, settings.OPENJEV_NAME)]),
    ], "decision")
    text_model = _load_adapter([
        ("...adapters.text_model.client", ["TextModelClient"],
         [()]),
        ("...adapters.text_model", ["TextModel", "OllamaTextModel", "ChatModel"],
         [(settings.TEXT_MODEL_BASE, settings.TEXT_MODEL_NAME), ()]),
    ], "text_model")
    if text_model is not None:
        profile = settings.TEXT_PROVIDERS.get(settings.TEXT_MODEL_PROVIDER, {})
        key_env = profile.get("key_env")
        if key_env and not os.environ.get(key_env):
            log.warning("text model provider %s: %s is not set "
                        "(put it in short-video-factory/.env or the "
                        "environment); agent calls will fail until then",
                        settings.TEXT_MODEL_PROVIDER, key_env)
    return create_app(store, asset_store, renderer=renderer, judge=judge,
                      decision=decision, text_model=text_model)
