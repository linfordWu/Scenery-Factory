# SPDX-License-Identifier: GPL-3.0-only
"""对话出片的结构化规划 agent：对话需求 -> 完整分镜计划。

产出与 `_run_plan`（编剧→角色/地点登记→导演拆镜）同一套 ShotSpec 契约：
场次 / 角色与地点 / 每镜 action·dialogue·motion_contract·beats·object_states·
camera·lighting_palette·acceptance，落库后由 `_compose_prompt` 组装成
H3 导演级分段渲染提示词。对话路径因此能拿到「和手动组织一样」的结构化
提示词，而不是只有一段 brief。

与手动链路的两点差异（为交互式等待预算）：
1. 编剧+选角合并为一次调用，导演一次拆完全片（手动链路逐场调用）；
2. LLM 输出不可信：数量/时长/画幅/字段清洗全部在本模块确定性钳制，
   与 `domain.video_constraints` 的共享上限/归一化函数保持一致；
   落库时还会再走一遍 `_materialize_plan`（同一条物化路径，二次保险）。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..domain.prompt_text import clean_dialogue
from ..domain.video_constraints import normalize_durations, plan_limits
from .assistant import _clamp_proposal, _fallback_proposal_from_history

# ---------------------------------------------------------------- constants --
_MIN_SHOT_S, _MAX_SHOT_S, _DEFAULT_SHOT_S = 3, 8, 5
_MAX_CAST = 20
_MAX_CHARACTERS_PER_SHOT = 6
_MAX_NOTES = 6                     # 带进导演调用的最近用户消息条数
_MOTION_FIELDS = ("start_state", "primary_motion", "secondary_motion",
                  "end_state", "camera_motion")
_BEAT_BUCKETS = ("already_happened", "this_clip_only", "reserved_for_later")
_RELATIONS = ("standalone", "sequence_first", "seamless_continuation",
              "next_shot", "reanchor")

WRITER_CAST_INSTRUCTIONS = (
    "你是短剧工厂的编剧+选角 agent。把创意简报拆成有序场景，并登记全片角色与"
    "固定地点，只输出 JSON。\n"
    "场景规则：\n"
    "1) 每场约 5-8 秒；场景数 ≈ 目标时长/7，从严控制，总时长不得超过目标。"
    "summary 只写「发生什么、结果是什么」，不写镜头语言和情绪形容词。\n"
    "2) 每个场景的发生地必须登记为 kind=location 的固定地点（如「乡村厨房」），"
    "地点清单不允许为空；抽象空间（内心世界/回忆）要落到具体物理场景。\n"
    "3) 人物、动物、宠物、幻想生物一律 kind=character；不要把动物登记成地点。\n"
    "4) description 写成可复用的固定外观描述，全片逐字一致。人物模板："
    "「名字:年龄段+性别,发型发色,上衣,下装,标志性特征(至多1个)」；"
    "地点模板：「名字:空间类型,光源方向与色温,3件以内关键陈设,时段」。"
    "禁止相对描述（和上一场一样的衣服）和情绪词。\n"
    "5) 主要角色 ≤ 4 个；单镜出场 ≤ 3 人。"
)

WRITER_CAST_SCHEMA = (
    '{"scenes":[{"title":str,"summary":str}],'
    '"characters":[{"name":str,"kind":"character|location","description":str}]}'
)

DIRECTOR_INSTRUCTIONS = (
    "你是短剧工厂的导演 agent。把已定稿的场次拆成有序镜头，只输出 JSON。\n"
    "硬性写作规则：\n"
    "1) 一个镜头只承担一个主动作（如「端盘落桌」），多个主动作必须拆成多镜；"
    "猫、窗帘、蒸汽、窗外景色等次要元素只允许响应式微动，不得抢占主事件。\n"
    "2) shot.characters 填本镜头出现的角色/地点名，必须与给定清单逐字一致；"
    "action 中的人数必须等于 characters 里非地点角色的数量。\n"
    "3) 每镜必填 motion_contract 五段：start_state 动作开始状态、primary_motion "
    "角色/关键道具的主运动、secondary_motion 环境/道具/光影的独立次运动、"
    "end_state 动作结束状态、camera_motion 运镜。次运动必须独立于主运动，"
    "否则会产出「会呼吸的静图」。\n"
    "4) object_states 列出本镜关键物体，四要素齐全：name、count 数量限定词"
    "（「仅一只」「两把」，不许写「一些」）、start_state、end_state；"
    "初末态必须互斥且分别成立，不得复制/悬浮/穿模/突然出现或消失；"
    "有拿取动作必须先有起点。只登记推动剧情的关键物体。\n"
    "5) beats 三桶必填：already_happened 已演完不重播 / this_clip_only 本镜独占 / "
    "reserved_for_later 后续预留不许提前泄露。\n"
    "6) camera 用 {\"shot\": 景别, \"movement\": 运镜}；相邻镜头避免同景别同机位。"
    "相机运动不能是画面唯一变化，禁止把静态图的裁切/平移/缩放/Ken Burns 当视频。\n"
    "7) lighting_palette 写光线方向/色温/主色调（如「低调工业光,地面霓虹灯管,"
    "深黑+电蓝+品红」），与场景描述和项目风格一致，同一场景内逐字一致。\n"
    "8) 台词只写角色口中说出的话，≤45 字，单镜至多一个说话人，且说话人必须是"
    "本镜 characters 中第一个非地点角色；不要写引号、换行和句尾标点（会被朗读"
    "出来）；没有台词就填空串。\n"
    "9) 情绪必须写成可见的身体语言（「她双手绞着衣角」），不要裸情绪词"
    "（紧张/悲伤/激动/愤怒/恐惧…）。\n"
    "10) acceptance.required/forbidden 写可验证的验收点；forbidden 至少包含"
    "「多余人物」「肢体畸形」「物体凭空出现/消失」「画面文字/水印」。\n"
    "11) sequence_relation：场景内第一镜=sequence_first，后续镜头=next_shot。\n"
    "12) 时长硬约束：duration_s 为 4-8 的整数；每个场景 shots 的 duration_s"
    "之和不得超过该场景预算+2 秒；全片镜头总数不得超过 shot_budget.total。\n"
    "13) 负面约束用英文写（中文否定会被 H3 反向 priming）。"
)

DIRECTOR_SCHEMA = (
    '{"scenes":[{"title":str,"shots":[{"action":str,"dialogue":str,'
    '"duration_s":int(4-8),'
    '"characters":[str],'
    '"sequence_relation":"sequence_first|next_shot",'
    '"felt_intent":str,"narrative_beat":str,'
    '"motion_contract":{"start_state":str,"primary_motion":str,'
    '"secondary_motion":str,"end_state":str,"camera_motion":str},'
    '"beats":{"already_happened":[str],"this_clip_only":[str],'
    '"reserved_for_later":[str]},'
    '"object_states":[{"name":str,"count":str,"start_state":str,'
    '"end_state":str}],'
    '"camera":{"shot":str,"movement":str},"lighting_palette":str,'
    '"acceptance":{"required":[str],"forbidden":[str]}}]}]}'
)

# 材料模式:材料是唯一事实来源,逐段覆盖(用户要求「和材料保持一致/高覆盖」)
MATERIAL_DIRECTOR_NOTE = (
    "\n【材料模式·必须遵守】上面每个场景对应上传材料的一个段落,必须逐段覆盖,"
    "不得增删材料中的要点、人物或事件;镜头 action 只能来自本段材料的内容;"
    "台词必须是材料中的原话或对原话的忠实转写(没有原话就不写台词)。"
)

CAST_ONLY_INSTRUCTIONS = (
    "你是短剧工厂的选角 agent。从给定的材料段落和项目信息里登记全部角色与固定地点,"
    "只输出 JSON。\n"
    "1) 人物、动物、宠物、幻想生物一律 kind=character;kind=location 只指固定场景/"
    "建筑/空间,不要把人物或动物登记成地点;每个场景的发生地必须登记为一个 location。\n"
    "2) description 写成可复用的固定外观描述,模板:「名字:年龄段+性别,发型发色,"
    "上衣,下装,标志性特征(至多1个)」;地点模板:「名字:空间类型,光源方向与色温,"
    "3件以内关键陈设,时段」。\n"
    "3) 禁止相对描述和情绪词;主要角色 ≤ 4 个。"
)

CAST_ONLY_SCHEMA = (
    '{"characters":[{"name":str,"kind":"character|location","description":str}]}'
)

# 材料切段:markdown 标题 / 章节行 / 空行为界
_MATERIAL_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s+|第[一二三四五六七八九十百0-9]+[章节回][^\n]{0,20}$|"
    r"[0-9]{1,3}[.、]\s+|[一二三四五六七八九十]+[、.]\s*)")


def split_material(text: str, max_beats: int = 12,
                   max_chars: int = 24000) -> list[dict]:
    """把上传材料切成有序节拍(每个 = 一个场景来源):title + body。

    以 markdown 标题/章节行/空行为界;段数超过镜头预算时相邻合并,
    保证「逐段覆盖」在目标时长内可行。确定性实现,不依赖 LLM。
    """
    text = (text or "")[:max_chars]
    blocks: list[dict] = []
    title = ""
    lines: list[str] = []

    def flush() -> None:
        nonlocal title, lines
        body = " ".join(line.strip() for line in lines if line.strip())
        head = title.strip() or body[:18]
        if head or body:
            blocks.append({"title": (head or f"段落 {len(blocks) + 1}")[:40],
                           "body": body[:600]})
        title, lines = "", []

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        m = _MATERIAL_HEADING_RE.match(line)
        if m and len(line.strip()) <= 40:
            flush()
            title = line[m.end():].strip() or line.strip()
            continue
        lines.append(line)
    flush()
    if not blocks:
        return []
    while len(blocks) > max(1, int(max_beats)):
        merged: list[dict] = []
        for i in range(0, len(blocks) - 1, 2):
            a, b = blocks[i], blocks[i + 1]
            merged.append({"title": a["title"],
                           "body": (a["body"] + " " + b["body"])[:600]})
        if len(blocks) % 2 == 1:
            merged.append(blocks[-1])
        blocks = merged
    return blocks


# ----------------------------------------------------------------- helpers --
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# 「一共三个镜头 / 3个分镜 / 共 3 镜」——用户明确指定的镜头总数
_SHOT_COUNT_RE = re.compile(
    r"(?:一共|共|总共|只|就|做|拍)?\s*"
    r"([0-9]{1,3}|[一二两三四五六七八九十]{1,3})\s*(?:个|段)?\s*"
    r"(?:镜头|分镜|镜|shots?)", re.IGNORECASE)
# 「每镜 5 秒 / 每个镜头约 6 秒」
_SHOT_SECONDS_RE = re.compile(
    r"每(?:个)?(?:镜头|分镜|镜|视频)\s*(?:约|大概|各)?\s*([0-9]{1,2})\s*秒")


def _cn_number(raw: str) -> Optional[int]:
    """把「三 / 十二 / 二十三 / 3」这类写法转成整数。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if "十" in raw:
        left, _, right = raw.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    value = 0
    for ch in raw:
        if ch not in _CN_DIGITS:
            return None
        value = value * 10 + _CN_DIGITS[ch]
    return value


def requested_constraints(messages: list[dict]) -> dict:
    """从用户原话里提取硬性要求（镜头总数 / 每镜时长）。

    规划器默认按时长推导镜数，但用户说「一共三个镜头」时必须照做，
    否则会拆出一堆镜头（后面几场还容易没内容）。
    """
    text = "\n".join(str(m.get("content") or "")
                     for m in messages or [] if m.get("role") == "user")
    out: dict[str, int] = {}
    m = _SHOT_COUNT_RE.search(text)
    if m:
        count = _cn_number(m.group(1))
        if count:
            out["shot_count"] = count
    m = _SHOT_SECONDS_RE.search(text)
    if m:
        seconds = int(m.group(1))
        if _MIN_SHOT_S <= seconds <= _MAX_SHOT_S:
            out["shot_seconds"] = seconds
    return out


def _distribute_shots(total: int, parts: int) -> list[int]:
    """把 total 个镜头尽量均匀分到 parts 个场次。"""
    parts = max(1, parts)
    base, extra = divmod(max(0, total), parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


# 一句话里的显式信息:时长「30秒/三十秒」、画幅「竖屏/9:16/横屏」
_DURATION_RE = re.compile(
    r"([0-9]{1,4}|[一二两三四五六七八九十]{1,3})\s*(?:秒|s\b)", re.IGNORECASE)
_ASPECT_EXPLICIT = re.compile(r"([0-9]{1,2})\s*[:：]\s*([0-9]{1,2})")
_ASPECT_HINTS = (
    ("9:16", ("9:16", "9比16", "九比十六", "竖屏", "竖版", "竖向")),
    ("1:1", ("1:1", "1比1", "一比一", "方形", "方屏")),
    ("16:9", ("16:9", "16比9", "十六比九", "横屏", "横版", "横向")),
)


def extract_oneclick_params(text: str) -> dict:
    """从一句话里提取用户明确给的信息(其余由流程自动补默认)。

    返回 {duration_target_s?, aspect_ratio?, shot_count?, shot_seconds?};
    确定性提取,不依赖 LLM。
    """
    text = text or ""
    out: dict[str, Any] = {}
    m = _DURATION_RE.search(text)
    if m:
        seconds = _cn_number(m.group(1))
        if seconds and 10 <= seconds <= 600:
            out["duration_target_s"] = seconds
    m = _ASPECT_EXPLICIT.search(text)
    if m and f"{m.group(1)}:{m.group(2)}" in ("16:9", "9:16", "1:1"):
        out["aspect_ratio"] = f"{m.group(1)}:{m.group(2)}"
    else:
        for aspect, hints in _ASPECT_HINTS:
            if any(hint in text for hint in hints):
                out["aspect_ratio"] = aspect
                break
    out.update(requested_constraints([{"role": "user", "content": text}]))
    return out


def _s(value: Any, limit: int = 400) -> str:
    return " ".join(str(value or "").split())[:limit]


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _clamp_shot_seconds(value: Any) -> int:
    return max(_MIN_SHOT_S, min(_MAX_SHOT_S,
                               _as_int(value, _DEFAULT_SHOT_S)))


def _str_list(value: Any, limit: int = 12, item_limit: int = 200) -> list[str]:
    out: list[str] = []
    for item in value or []:
        text = _s(item, item_limit)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _norm_scenes(raw: Any) -> list[dict]:
    scenes: list[dict] = []
    for i, item in enumerate(raw or []):
        if not isinstance(item, dict):
            continue
        title = _s(item.get("title"), 40) or f"场景 {i + 1:02d}"
        scenes.append({"title": title, "summary": _s(item.get("summary"), 300)})
    return scenes


def _norm_cast(raw: Any) -> list[dict]:
    cast: list[dict] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = _s(item.get("name"), 40)
        if not name or name in seen:
            continue
        seen.add(name)
        cast.append({
            "name": name,
            "kind": "location" if item.get("kind") == "location" else "character",
            "description": _s(item.get("description"), 300),
        })
        if len(cast) >= _MAX_CAST:
            break
    return cast


def _norm_shot(raw: Any, index: int, cast_names: set[str]) -> Optional[dict]:
    """单镜字段清洗；action 为空视为无效镜头（直接丢弃）。"""
    if not isinstance(raw, dict):
        return None
    action = _s(raw.get("action"), 600)
    if not action:
        return None
    characters: list[str] = []
    for item in raw.get("characters") or []:
        name = _s(item.get("name") if isinstance(item, dict) else item, 40)
        # 未登记的角色不进镜头（物化时同样会丢弃，预览保持一致）
        if name and name in cast_names and name not in characters:
            characters.append(name)
        if len(characters) >= _MAX_CHARACTERS_PER_SHOT:
            break
    relation = _s(raw.get("sequence_relation"), 40)
    if relation not in _RELATIONS:
        relation = "sequence_first" if index == 0 else "next_shot"
    motion = {
        field: _s((raw.get("motion_contract") or {}).get(field), 300)
        for field in _MOTION_FIELDS
    }
    beats_raw = raw.get("beats") or {}
    beats = {bucket: _str_list(beats_raw.get(bucket) if isinstance(beats_raw, dict)
                               else None, limit=8, item_limit=200)
             for bucket in _BEAT_BUCKETS}
    objects: list[dict] = []
    for item in raw.get("object_states") or []:
        if not isinstance(item, dict):
            continue
        name = _s(item.get("name"), 40)
        if not name:
            continue
        objects.append({"name": name,
                        "count": _s(item.get("count"), 40),
                        "start_state": _s(item.get("start_state"), 200),
                        "end_state": _s(item.get("end_state"), 200)})
        if len(objects) >= 8:
            break
    camera = {
        _s(k, 40): _s(v, 80)
        for k, v in (raw.get("camera") or {}).items()
        if _s(k, 40) and _s(v, 80)
    } if isinstance(raw.get("camera"), dict) else {}
    acceptance_raw = raw.get("acceptance") if isinstance(raw.get("acceptance"),
                                                         dict) else {}
    return {
        "action": action,
        "dialogue": clean_dialogue(raw.get("dialogue")),
        "duration_s": _clamp_shot_seconds(raw.get("duration_s")),
        "characters": characters,
        "sequence_relation": relation,
        "felt_intent": _s(raw.get("felt_intent"), 300),
        "narrative_beat": _s(raw.get("narrative_beat"), 300),
        "motion_contract": motion,
        "beats": beats,
        "object_states": objects,
        "camera": camera,
        "lighting_palette": _s(raw.get("lighting_palette"), 300),
        "acceptance": {
            "required": _str_list(acceptance_raw.get("required")),
            "forbidden": _str_list(acceptance_raw.get("forbidden")),
        },
    }


def _group_shots(director_out: Any, scenes: list[dict]) -> list[list[dict]]:
    """把导演输出按场次分组，兼容嵌套 scenes[] 与拍平 shots[] 两种形状。"""
    groups: list[list[dict]] = [[] for _ in scenes]
    if not isinstance(director_out, dict) or not scenes:
        return groups
    by_title = {scene["title"]: i for i, scene in enumerate(scenes)}
    nested = director_out.get("scenes")
    if isinstance(nested, list) and nested:
        for i, item in enumerate(nested):
            if i >= len(groups) or not isinstance(item, dict):
                break
            shots = item.get("shots")
            if isinstance(shots, list):
                groups[i] = [s for s in shots if isinstance(s, dict)]
        if any(groups):
            return groups
    flat = director_out.get("shots")
    if isinstance(flat, list):
        for item in flat:
            if not isinstance(item, dict):
                continue
            raw_index = item.get("scene_index")
            if isinstance(raw_index, (int, float)):
                idx = int(raw_index)
            else:
                idx = by_title.get(_s(item.get("scene"), 40), 0)
            if idx < 0 or idx >= len(groups):
                idx = 0
            groups[idx].append(item)
    return groups


def normalize_plan(scenes_raw: Any, characters_raw: Any, director_out: Any,
                   proposal: dict,
                   constraints: Optional[dict] = None) -> dict:
    """LLM 原始输出 -> 可落库的结构化方案（数量/时长/字段全部确定性钳制）。

    constraints 为用户硬性要求(shot_count/shot_seconds):镜头总数以它为准,
    多余镜头丢弃;没有内容的空场次不进入方案。
    """
    constraints = constraints or {}
    requested = _as_int(constraints.get("shot_count"), 0)
    target = max(10, _as_int(proposal.get("duration_target_s"), 60))
    max_scenes, max_shots = plan_limits(target)
    hard_cap = min(max_shots, requested) if requested else max_shots
    scenes = _norm_scenes(scenes_raw)[:max_scenes]
    if requested:
        scenes = scenes[:max(1, requested)]
    cast = _norm_cast(characters_raw)
    cast_names = {c["name"] for c in cast}
    grouped = _group_shots(director_out, scenes)

    out_scenes: list[dict] = []
    shot_total = 0
    for i, scene in enumerate(scenes):
        shots: list[dict] = []
        for j, raw in enumerate(grouped[i]):
            if shot_total >= hard_cap:
                break
            shot = _norm_shot(raw, j, cast_names)
            if shot is None:
                continue
            shots.append(shot)
            shot_total += 1
        if shots:
            out_scenes.append({**scene, "shots": shots})
        # 空场次(导演没给出镜头)直接丢弃,避免方案卡出现空块

    per_shot = _as_int(constraints.get("shot_seconds"), 0)
    if requested and per_shot:
        for scene in out_scenes:
            for shot in scene["shots"]:
                shot["duration_s"] = per_shot
        durations = [shot["duration_s"] for scene in out_scenes
                     for shot in scene["shots"]]
        # 用户指定了单镜时长:不再为凑总时长缩放(总时长可能短于目标)
    else:
        durations = [shot["duration_s"] for scene in out_scenes
                     for shot in scene["shots"]]
        scaled = normalize_durations(durations, target)
        k = 0
        for scene in out_scenes:
            for shot in scene["shots"]:
                shot["duration_s"] = scaled[k]
                k += 1

    note = ""
    if requested:
        note = f"按你的要求 {requested} 个镜头"
        if per_shot:
            note += f"、每镜约 {per_shot} 秒"
    title = _s(proposal.get("title"), 40) or _s(proposal.get("brief"), 20)
    return {
        "title": title,
        "brief": _s(proposal.get("brief"), 1200),
        "style": _s(proposal.get("style"), 200),
        "duration_target_s": target,
        "aspect_ratio": proposal.get("aspect_ratio") or "16:9",
        "characters": cast,
        "scenes": out_scenes,
        "shot_count": shot_total,
        "total_duration_s": sum(shot["duration_s"] for scene in out_scenes
                                for shot in scene["shots"]),
        "requested_shot_count": requested,
        "constraint_note": note,
    }


def _conversation_notes(messages: list[dict]) -> list[str]:
    """对话里的用户原话（最近若干条），给导演补足 brief 之外的细节。"""
    notes = [_s(m.get("content"), 500) for m in messages or []
             if m.get("role") == "user" and _s(m.get("content"), 500)]
    return notes[-_MAX_NOTES:]


def run_assistant_plan(text_model, messages: list[dict],
                       proposal: Optional[dict] = None,
                       material: Optional[dict] = None) -> dict:
    """对话（含 proposal）-> 结构化方案。返回 {"proposal", "plan"}。

    两轮调用：编剧+选角一次，导演一次拆完全片；proposal 缺失时从历史消息
    兜底组装，信息完全不足则抛 ValueError（端点转 422）。
    material 模式：上传材料先被确定性切成有序节拍（每段=一个场景），
    编剧环节跳过、只做选角，导演逐段展开 —— 保证材料被逐段覆盖。
    """
    prop = _clamp_proposal(proposal) or _fallback_proposal_from_history(messages)
    if prop is None:
        raise ValueError("需求还不完整：先在对话里说清拍什么，再拆解分镜")

    constraints = requested_constraints(messages)
    requested = _as_int(constraints.get("shot_count"), 0)
    target = prop["duration_target_s"]
    max_scenes, max_shots = plan_limits(target)

    material_mode = bool(material and material.get("beats"))
    if material_mode:
        beats = list(material.get("beats") or [])
        if requested:
            beats = beats[:max(1, min(requested, max_scenes))]
        scenes_raw = [{"title": _s(b.get("title"), 40) or f"段落 {i + 1}",
                       "summary": _s(b.get("body"), 300)}
                      for i, b in enumerate(beats)][:max_scenes]
        cast_out = text_model.chat_json(
            CAST_ONLY_INSTRUCTIONS,
            {"project": {"title": prop["title"], "style": prop["style"],
                         "brief": prop["brief"][:600],
                         "duration_target_s": target,
                         "aspect_ratio": prop["aspect_ratio"]},
             "material": {"name": material.get("name", ""),
                          "beats": scenes_raw}},
            CAST_ONLY_SCHEMA, max_tokens=4096)
        cast_raw = (cast_out or {}).get("characters") or []
        director_note = MATERIAL_DIRECTOR_NOTE
    else:
        writer_instructions = WRITER_CAST_INSTRUCTIONS
        if requested:
            # 编剧先把场次控制住,避免后续场次多了导演没内容可拆
            writer_instructions += (
                f"\n【用户硬性要求】全片一共只做 {requested} 个镜头:"
                f"场景数不要超过 {requested} 个,每个场景都必须有戏、至少 1 个镜头。")
        writer_out = text_model.chat_json(
            writer_instructions,
            {"project": {"title": prop["title"], "style": prop["style"],
                         "brief": prop["brief"],
                         "duration_target_s": target,
                         "aspect_ratio": prop["aspect_ratio"]},
             "user_notes": _conversation_notes(messages)},
            WRITER_CAST_SCHEMA, max_tokens=4096)
        scenes_raw = (writer_out or {}).get("scenes") or []
        if not scenes_raw:
            raise ValueError("编剧输出为空，无法拆解分镜；可在对话里再补充拍什么")
        cast_raw = (writer_out or {}).get("characters") or []
        director_note = ""

    scenes = _norm_scenes(scenes_raw)[:max_scenes]
    if requested and not material_mode:
        scenes = scenes[:max(1, min(requested, max_scenes))]
    n_scenes = max(1, len(scenes))
    scene_seconds = max(4, round(target / n_scenes))
    if requested:
        per_scene = _distribute_shots(requested, n_scenes)
        total_budget = requested
    else:
        per_scene = [max(1, round(scene_seconds / 5)) for _ in scenes]
        total_budget = max(1, min(max_shots, target // 5))

    director_instructions = DIRECTOR_INSTRUCTIONS + director_note
    if requested and not material_mode:
        director_instructions += (
            f"\n【用户硬性要求】全片镜头总数必须严格等于 {requested} 个,"
            f"不得多也不得少;请把上面列出的场景全部拆到,不留空场次。")
    if constraints.get("shot_seconds"):
        director_instructions += (
            f"\n【用户硬性要求】每镜时长约 {constraints['shot_seconds']} 秒"
            f"(单镜限 {_MIN_SHOT_S}-{_MAX_SHOT_S} 秒)。")

    director_out = text_model.chat_json(
        director_instructions,
        {"project": {"title": prop["title"], "style": prop["style"],
                     "brief": prop["brief"][:1200] if material_mode else prop["brief"],
                     "duration_target_s": target,
                     "aspect_ratio": prop["aspect_ratio"]},
         "scenes": scenes, "cast": _norm_cast(cast_raw),
         "requested": constraints or None,
         "shot_budget": {"total": total_budget,
                         "per_scene": per_scene,
                         "scene_seconds": scene_seconds},
         "user_notes": _conversation_notes(messages)},
        DIRECTOR_SCHEMA, max_tokens=16384)

    plan = normalize_plan(scenes_raw, cast_raw, director_out, prop, constraints)
    if material_mode:
        # 覆盖兜底:导演漏拆的段落用材料原文补一个最简镜头,保证逐段有画面
        planned_titles = {sc["title"] for sc in plan["scenes"]}
        cast_names = {c["name"] for c in plan["characters"]}
        for i, beat in enumerate(scenes):
            title = scenes_raw[i]["title"]
            if title in planned_titles:
                continue
            shot = _norm_shot({"action": _s(beat.get("summary"), 120) or title,
                               "duration_s": _DEFAULT_SHOT_S}, 0, cast_names)
            if shot is None:
                continue
            plan["scenes"].append({
                "title": title,
                "summary": _s(beat.get("summary"), 300),
                "shots": [shot]})
            planned_titles.add(title)
        plan["shot_count"] = sum(len(sc["shots"]) for sc in plan["scenes"])
        plan["total_duration_s"] = sum(sh["duration_s"]
                                       for sc in plan["scenes"]
                                       for sh in sc["shots"])
        plan["material_name"] = material.get("name") or ""
        plan["material_beats"] = len(plan["scenes"])
        coverage = f"材料逐段覆盖 {len(plan['scenes'])} 段"
        base_note = plan.get("constraint_note") or ""
        plan["constraint_note"] = coverage + (f" · {base_note}" if base_note else "")
    if not plan["shot_count"]:
        raise ValueError("导演输出没有可用镜头（action 为空），请重试")
    return {"proposal": prop, "plan": plan}
