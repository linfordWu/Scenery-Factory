# SPDX-License-Identifier: GPL-3.0-only
"""Core data contracts shared by every module (per architecture doc section 07).

The database is the source of truth; model context is always rebuilt from
saved state so agent roles never keep conflicting private "memories".
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_ts() -> float:
    return time.time()


# ---------------------------------------------------------------- assets ----

AssetSource = Literal["imported", "generated", "derived"]
MediaType = Literal["image", "video", "audio", "text", "other"]
AssetStatus = Literal["UPLOADING", "VALIDATING", "PROCESSING", "READY", "FAILED"]

# 素材分类：character 角色参考 / location 场景参考 / prop 道具 / style 风格参考 /
# footage 实拍视频素材 / audio 音频 / export 成片 / other；空串 = 未分类
AssetCategory = Literal["character", "location", "prop", "style",
                        "footage", "audio", "export", "other"]
ASSET_CATEGORIES: tuple[str, ...] = (
    "character", "location", "prop", "style", "footage", "audio", "export", "other")


class Asset(BaseModel):
    asset_id: str = Field(default_factory=lambda: new_id("asset"))
    project_id: str
    source: AssetSource
    media_type: MediaType
    sha256: str = ""
    version: int = 1
    category: str = ""              # AssetCategory；空 = 未分类
    metadata: dict[str, Any] = Field(default_factory=dict)
    storage_key: str = ""
    preview_key: str = ""
    status: AssetStatus = "UPLOADING"
    parent_asset_ids: list[str] = Field(default_factory=list)
    created_at: float = Field(default_factory=now_ts)


class AssetBinding(BaseModel):
    """Binds an asset version to a shot with role and optional clip range."""
    binding_id: str = Field(default_factory=lambda: new_id("bind"))
    shot_id: str
    asset_id: str
    asset_version: int
    role: str = "reference"          # reference | first_frame | reuse_clip | audio
    clip_range: Optional[list[float]] = None


class UploadSession(BaseModel):
    upload_id: str = Field(default_factory=lambda: new_id("upl"))
    project_id: str
    filename: str
    total_size: int
    chunk_size: int = 8 * 1024 * 1024
    received_parts: list[int] = Field(default_factory=list)
    status: AssetStatus = "UPLOADING"
    asset_id: Optional[str] = None
    created_at: float = Field(default_factory=now_ts)


# ------------------------------------------------------------- story tree ---

class Character(BaseModel):
    """角色/地点登记表条目：固定外观描述注入提示词保证跨镜头一致性。"""
    character_id: str = Field(default_factory=lambda: new_id("ch"))
    project_id: str = ""
    name: str = ""
    kind: Literal["character", "location"] = "character"
    description: str = ""           # 可复用的固定外观/环境描述
    asset_id: str = ""              # 可选参考图 asset
    created_at: float = Field(default_factory=now_ts)


class Acceptance(BaseModel):
    required: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    rubric_version: str = "shortdrama-v1"


class ShotSpec(BaseModel):
    """Single shared target for generation and scoring (doc section 07)."""
    schema_version: str = "3"
    material_policy: Literal["prefer_imported", "generate_only"] = "prefer_imported"
    production_mode: Literal["generate", "reuse"] = "generate"
    shot_id: str
    duration_s: int = 5
    aspect_ratio: str = "9:16"
    characters: list[dict[str, Any]] = Field(default_factory=list)
    scene_asset_id: Optional[str] = None
    action: str = ""
    dialogue: str = ""
    camera: dict[str, str] = Field(default_factory=dict)
    continuity: dict[str, str] = Field(default_factory=dict)
    reference_assets: list[str] = Field(default_factory=list)
    acceptance: Acceptance = Field(default_factory=Acceptance)
    # 每镜都要有可验证的剧情职责和真实运动，避免静态参考图的缩放伪视频。
    narrative_beat: str = ""
    motion_contract: dict[str, str] = Field(default_factory=dict)
    # ---- 序列/节拍结构（seedance 密度与续拍思想的中文化重写）----
    sequence_relation: Literal["standalone", "sequence_first",
                               "seamless_continuation", "next_shot",
                               "reanchor"] = "standalone"
    # 三桶节拍：already_happened 已演完不许重播 / this_clip_only 本镜头独占 /
    # reserved_for_later 后续镜头预留不许泄露
    beats: dict[str, list[str]] = Field(default_factory=dict)
    # 关键物体连续性契约：每条形如
    # {"name": "煎蛋盘", "count": "仅一只", "start_state": "在女孩手中",
    #  "end_state": "在木桌中央且双手离开"}
    # 渲染提示词与视觉评审共用，防止数量复制/悬浮/初末态混帧。
    object_states: list[dict[str, Any]] = Field(default_factory=list)
    # 光线与调色板（渲染提示词 LIGHTING & PALETTE 段;导演拆镜时填写,
    # 如「低调工业光,地面霓虹灯管,深黑+电蓝+品红」),空串则该段省略。
    lighting_palette: str = ""
    observed_end_state: str = ""    # 验收时由评审写回的实际末态（供下一镜接续）
    felt_intent: str = ""           # 内部意图，不进提示词
    extension_depth: int = 0        # 链式续拍深度


class Shot(BaseModel):
    shot_id: str
    scene_id: str
    project_id: str
    order: int = 0
    spec: ShotSpec
    accepted_run_id: Optional[str] = None
    review_note: str = ""


class Scene(BaseModel):
    scene_id: str
    project_id: str
    order: int = 0
    title: str = ""
    summary: str = ""


class Project(BaseModel):
    project_id: str = Field(default_factory=lambda: new_id("p"))
    schema_version: str = "2"
    title: str = ""
    style: str = ""
    brief: str = ""
    duration_target_s: int = 60
    # 项目级画幅锁：所有镜头强制使用该画幅,不信任 LLM 逐镜给出的值,
    # 否则同一项目会混出竖屏镜头,导出后播放器中段切换横竖屏。
    aspect_ratio: str = "16:9"
    # 一键出片/一键成片：全部镜头验收后自动拼接成完整成片(手动项目为 False)。
    auto_compose: bool = False
    # 一键出片的「素材后确认」模式:定妆/场景参考图/首帧生成完先暂停,
    # 用户确认后才渲染镜头并自动拼接;False=全自动。
    confirm_after_assets: bool = False
    # 一键出片的材料来源(md/txt/docx 上传):只存名称与资产 id,
    # 原文落在资产库里,避免项目列表被长文本撑大。
    source_material_name: str = ""
    source_material_asset_id: str = ""
    created_at: float = Field(default_factory=now_ts)


# ------------------------------------------------------------------- runs ---

RunState = Literal[
    "PLANNED", "ASSET_READY", "PROMPT_READY", "QUEUED", "RENDERING",
    "GENERATED", "NORMALIZING", "SCORING", "REPAIRING", "ACCEPTED",
    "HUMAN_REVIEW", "RETRY_WAIT", "CANCEL_REQUESTED", "CANCELLED", "FAILED",
]


class Run(BaseModel):
    run_id: str = Field(default_factory=lambda: new_id("run"))
    shot_id: str
    project_id: str
    attempt_id: str = Field(default_factory=lambda: new_id("att"))
    state: RunState = "PLANNED"
    state_version: int = 0
    input_hash: str = ""
    seed: int = 0
    model_revision: str = ""
    workflow_hash: str = ""
    external_task_id: str = ""        # comfyui prompt_id
    prompt_spec: str = ""             # final prompt text sent to the renderer
    artifact_ids: list[str] = Field(default_factory=list)
    candidate_asset_ids: list[str] = Field(default_factory=list)
    score: Optional[dict[str, Any]] = None
    failure: Optional[dict[str, Any]] = None
    repair_count: int = 0
    max_repairs: int = 2              # 首轮 + 最多 2 次自动修复
    created_at: float = Field(default_factory=now_ts)
    updated_at: float = Field(default_factory=now_ts)


class ScoreReport(BaseModel):
    run_id: str
    verdict: Literal["accept", "accept_with_deviation", "repair", "reject",
                     "uncertain"]
    hard_checks: dict[str, bool] = Field(default_factory=dict)
    scores: dict[str, float] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    uncertain: bool = False
    rubric_version: str = "shortdrama-v2"
    # accept_with_deviation 时记录可接受偏差的说明
    deviation: str = ""
    # 评审器对画面观察的把握；low 的 accept 不许自动通过
    observation_confidence: Literal["low", "medium", "high"] = "high"
    observed_end_state: str = ""    # 末帧实际状态描述，写回 shot 供续接


class RepairPlan(BaseModel):
    run_id: str
    target: str                     # prompt | reference_assets | shot_spec | infra
    action: str
    detail: str = ""
    invalidate_from: RunState = "PROMPT_READY"


# ------------------------------------------------------- director review ----

class ReviewIssue(BaseModel):
    """审核员 agent 报出的单条问题。"""
    shot_id: str = ""
    run_id: str = ""
    type: str = ""                  # story | character | goof | unexpected | text
    severity: Literal["low", "mid", "high"] = "low"
    detail: str = ""


class DirectorReview(BaseModel):
    """项目级连贯性审核报告。"""
    report_id: str = Field(default_factory=lambda: new_id("rev"))
    project_id: str
    created_at: float = Field(default_factory=now_ts)
    story_coherence: float = 0.0
    summary: str = ""
    issues: list[ReviewIssue] = Field(default_factory=list)
    raw: str = ""                   # LLM 原始返回（解析失败时留档）


# ---------------------------------------------------------------- events ----

class Event(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    seq: int = 0
    project_id: str
    run_id: Optional[str] = None
    step_id: str = ""
    attempt_id: str = ""
    type: str                         # step.queued / agent.started / tool.progress / ...
    actor: str = ""
    timestamp: float = Field(default_factory=now_ts)
    progress: Optional[dict[str, Any]] = None
    summary: str = ""
    artifact_ids: list[str] = Field(default_factory=list)
    state_version: int = 0


class StepRun(BaseModel):
    step_run_id: str = Field(default_factory=lambda: new_id("step"))
    run_id: str
    step_id: str
    actor: str = ""
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    detail: dict[str, Any] = Field(default_factory=dict)


class OutboxMessage(BaseModel):
    outbox_id: str = Field(default_factory=lambda: new_id("out"))
    event: Event
    published: bool = False


class DecisionAdvice(BaseModel):
    """Application-level protocol for small-model routing (doc section 04)."""
    decision_id: str = Field(default_factory=lambda: new_id("d"))
    state_hash: str = ""
    allowed_actions: list[str] = Field(default_factory=list)
    selected_action: str = ""
    confidence: float = 0.0
    model_id: str = "configured-local-laya"
    model_revision: str = ""
    calibration_version: str = "routing-zh-v1"
    evidence_ids: list[str] = Field(default_factory=list)
    accepted_by_policy: bool = False
    fallback: Optional[str] = None
