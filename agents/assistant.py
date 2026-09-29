# SPDX-License-Identifier: GPL-3.0-only
"""对话出片助理:多轮对话引导用户补齐关键信息,信息足够时产出出片方案。

无状态:前端带全量历史,本模块只负责一轮 system+history -> 结构化回复。
引导式:提示词约束「每轮最多问一个问题、优先问最影响成片的缺口」;
proposal 只在信息足够时出现,checklist 记录已聊到的信息(前端展示进度清单)。
LLM 输出不可信,proposal/checklist 在这里做确定性钳制与兜底。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..adapters.text_model.client import extract_json

ASSISTANT_SYSTEM_PROMPT = (
    "你是短剧制片助理,负责引导用户一步步把想法聊成一份可开拍的方案。\n"
    "工作方式:\n"
    "1) 每轮最多问 1 个问题,只问「目前最影响成片效果、而用户还没说」的信息,"
    "不要一次列一堆问题。提问优先级:主角形象 > 核心冲突或反转 > 结尾情绪/落点 > "
    "视觉风格 > 时长与画幅。\n"
    "2) 时长与画幅合并成一句问一次(如「时长和画幅有偏好吗?默认 60 秒 16:9」);"
    "用户没回答或说随便就用默认,之后不再问。\n"
    "3) 如果用户一条消息就把关键信息说全了(拍什么 + 主角/冲突 + 风格等),"
    "直接出 proposal,不要为了提问而提问。\n"
    "4) 用户说「直接开始/随便/你定/都行/赶时间」时,立即出 proposal,用默认补齐,不再追问。\n"
    "5) reply 简短口语化(一两句);给建议时用 2-3 个选项降低回答成本"
    "(如「想要哪种质感:写实电影感 / 日系动漫 / 水彩绘本?」)。\n"
    "6) checklist 每轮都必须输出,并完整反映到目前为止已聊到的全部信息"
    "(包括前几轮),不要只写本轮新增;值用短词概括,没聊到的留空字符串。"
    "proposal 只在信息足够时出现(至少:拍什么 + 主角或冲突 + 一个风格/视觉线索),"
    "否则为 null。\n"
    "7) brief 是全部已聊细节的中文整合(主角/场景/冲突/氛围/台词风格),给下游编剧用;"
    "不要自己写分镜、不要报场景数,结构化分镜由系统在信息足够后拆解。\n"
    "示例一(信息不足,先引导一个问题):\n"
    "用户:想拍个雨夜便利店的故事\n"
    '输出:{"reply":"好题材。主角是什么人?比如熬夜的店员,还是深夜进店的客人?",'
    '"proposal":null,"checklist":{"topic":"雨夜便利店","lead":"",'
    '"conflict":"","ending":"","style":"","duration":"","aspect":""}}\n'
    "示例二(用户一次说全,直接出方案):\n"
    "用户:雨夜便利店,店员在监控里看到另一个自己,写实电影感,30秒竖屏\n"
    '输出:{"reply":"好,信息够了,我来拆分镜。","proposal":{"title":"雨夜便利店",'
    '"brief":"雨夜便利店悬疑:店员在监控里看到另一个自己,写实电影感,冷色调,'
    '台词极少靠环境音推进。","style":"写实电影感,冷色调","duration_target_s":30,'
    '"aspect_ratio":"9:16"},"checklist":{"topic":"雨夜便利店悬疑","lead":"店员",'
    '"conflict":"监控里出现另一个自己","ending":"","style":"写实电影感",'
    '"duration":"30 秒","aspect":"9:16"}}\n'
    "示例三(用户不想聊了):\n"
    "用户:都可以,你定吧直接开始\n"
    '输出:{"reply":"好,按我理解的直接开拍。","proposal":{"title":"雨夜便利店",'
    '"brief":"雨夜便利店悬疑短片,冷色调,节奏克制。","style":"悬疑冷调",'
    '"duration_target_s":60,"aspect_ratio":"16:9"},'
    '"checklist":{"topic":"雨夜便利店悬疑","lead":"","conflict":"","ending":"",'
    '"style":"悬疑冷调","duration":"60 秒","aspect":"16:9"}}'
)

_CHECKLIST_KEYS = ("topic", "lead", "conflict", "ending", "style",
                   "duration", "aspect")

_SCHEMA_HINT = (
    '{"reply":str,"proposal":null|{"title":str,"brief":str,"style":str,'
    '"duration_target_s":int,"aspect_ratio":"16:9|9:16|1:1"},'
    '"checklist":{"topic":str,"lead":str,"conflict":str,"ending":str,'
    '"style":str,"duration":str,"aspect":str}}'
)

_ASPECTS = ("16:9", "9:16", "1:1")
_DURATION_MIN, _DURATION_MAX, _DURATION_DEFAULT = 10, 600, 60
_MAX_HISTORY = 20     # 无状态对话:只带最近 20 条,防上下文超长

# 结构化提示词快路径:命中 >=2 个分段标记且足够长,跳过 LLM 直接出方案
_STRUCTURED_MARKERS = ("duration:", "aspect ratio:", "style:",
                       "shot breakdown", "scene", "camera", "avoid")
_DURATION_RE = re.compile(r"duration:\s*(\d+)\s*second", re.IGNORECASE)
_ASPECT_RE = re.compile(r"aspect\s*ratio:\s*(\d+)\s*:\s*(\d+)", re.IGNORECASE)
_STYLE_RE = re.compile(r"style:\s*([^\n|]+)", re.IGNORECASE)

# 用户明确喊开机:即使 LLM 没给 proposal,也用历史消息兜底组装
_START_INTENT_RE = re.compile(
    r"(直接|马上|立即|现在)?(开始|开机|出片|生成|可以了|就这样|就这些|"
    "没问题|确认|开工)")

# 时长/画幅的确定性提取(覆盖整段对话,不信任 LLM 的数值):
# 「30秒」「90 s」、画幅「9:16 / 竖屏 / 横屏 / 方形」
_CN_DURATION_RE = re.compile(r"(\d{1,4})\s*(?:秒|s\b|sec)", re.IGNORECASE)
_ASPECT_HINTS = (
    ("9:16", ("9:16", "9比16", "竖屏", "竖版", "竖向")),
    ("1:1", ("1:1", "1比1", "方形", "方屏")),
    ("16:9", ("16:9", "16比9", "横屏", "横版", "横向")),
)


def _extract_overrides(messages: list[dict]) -> dict:
    """从全部用户消息里正则提取时长/画幅;找不到则不覆盖(None)。"""
    text = "\n".join(str(m.get("content") or "")
                     for m in messages if m.get("role") == "user")
    out: dict[str, Any] = {}
    m = _CN_DURATION_RE.search(text)
    if m:
        out["duration_target_s"] = max(_DURATION_MIN,
                                       min(_DURATION_MAX, int(m.group(1))))
    for aspect, hints in _ASPECT_HINTS:
        if any(h in text for h in hints):
            out["aspect_ratio"] = aspect
            break
    return out


def _structured_proposal(text: str) -> Optional[dict]:
    """识别粘贴进来的完整结构化提示词,代码直接提取方案(不依赖 LLM 判断)。"""
    low = text.lower()
    hits = sum(1 for m in _STRUCTURED_MARKERS if m in low)
    if hits < 2 or len(text) < 100:
        return None
    m = _DURATION_RE.search(low)
    duration = int(m.group(1)) if m else _DURATION_DEFAULT
    m = _ASPECT_RE.search(low)
    aspect = f"{m.group(1)}:{m.group(2)}" if m else "16:9"
    m = _STYLE_RE.search(text)
    style = m.group(1).strip() if m else ""
    return _clamp_proposal({"title": style[:20], "brief": text.strip(),
                            "style": style,
                            "duration_target_s": duration,
                            "aspect_ratio": aspect})


def _fallback_proposal_from_history(messages: list[dict]) -> Optional[dict]:
    """用户喊开机但 LLM 没给方案:把用户说过的内容拼成 brief,默认值兜底。"""
    brief = "\n".join(str(m.get("content") or "").strip()
                      for m in messages if m.get("role") == "user"
                      and str(m.get("content") or "").strip())
    return _clamp_proposal({"brief": brief})


def _clamp_proposal(raw: Any) -> Optional[dict]:
    """proposal 的确定性钳制:不满足最低要求(非空 brief)就视为未就绪。"""
    if not isinstance(raw, dict):
        return None
    brief = str(raw.get("brief") or "").strip()
    if not brief:
        return None
    try:
        duration = int(raw.get("duration_target_s") or _DURATION_DEFAULT)
    except (TypeError, ValueError):
        duration = _DURATION_DEFAULT
    duration = max(_DURATION_MIN, min(_DURATION_MAX, duration))
    aspect = str(raw.get("aspect_ratio") or "").strip()
    if aspect not in _ASPECTS:
        aspect = "16:9"
    title = str(raw.get("title") or "").strip() or brief[:20]
    return {"title": title, "brief": brief,
            "style": str(raw.get("style") or "").strip(),
            "duration_target_s": duration, "aspect_ratio": aspect}


def _sanitize_checklist(raw: Any) -> dict:
    """清单字段白名单 + 截断;LLM 输出不可信。"""
    out = {key: "" for key in _CHECKLIST_KEYS}
    if isinstance(raw, dict):
        for key in _CHECKLIST_KEYS:
            out[key] = " ".join(str(raw.get(key) or "").split())[:40]
    return out


def _checklist_from_proposal(proposal: Optional[dict],
                             overrides: Optional[dict] = None) -> dict:
    """proposal/确定性提取 -> 清单兜底(保证清单里不出现「已出方案但时长还空着」)。"""
    checklist = {key: "" for key in _CHECKLIST_KEYS}
    overrides = overrides or {}
    if proposal:
        checklist["topic"] = str(proposal.get("brief") or "")[:40]
        checklist["style"] = str(proposal.get("style") or "")[:40]
        checklist["duration"] = f"{proposal.get('duration_target_s')} 秒"
        checklist["aspect"] = str(proposal.get("aspect_ratio") or "")
    if overrides.get("duration_target_s"):
        checklist["duration"] = f"{overrides['duration_target_s']} 秒"
    if overrides.get("aspect_ratio"):
        checklist["aspect"] = str(overrides["aspect_ratio"])
    return checklist


def run_assistant_chat(text_model, messages: list[dict]) -> dict:
    """跑一轮对话。返回 {"reply": str, "proposal": dict | None, "checklist": dict}。

    引导式:每轮最多问一个问题(由提示词约束);proposal 只在信息足够时才出,
    checklist 记录已聊到的信息供前端展示进度。
    """
    history = [
        {"role": "user" if m.get("role") == "user" else "assistant",
         "content": str(m.get("content") or "")}
        for m in (messages or [])[-_MAX_HISTORY:]
        if str(m.get("content") or "").strip()
    ]
    last_user = next((m["content"] for m in reversed(history)
                      if m["role"] == "user"), "")
    overrides = _extract_overrides(history)
    # 快路径:最后一条用户消息本身就是完整结构化提示词,跳过 LLM 直接出方案
    direct = _structured_proposal(last_user)
    if direct is not None:
        return {"reply": "方案已收到,信息完整,直接开机。", "proposal": direct,
                "checklist": _checklist_from_proposal(direct, overrides)}
    prompt = ASSISTANT_SYSTEM_PROMPT + "\n\n" + (
        "只输出一个 JSON 对象,不要 markdown 代码块、不要任何额外文字。"
        "结构:\n" + _SCHEMA_HINT)
    reply = text_model.chat([{"role": "system", "content": prompt}] + history,
                            max_tokens=1024, temperature=0.7)
    checklist = {key: "" for key in _CHECKLIST_KEYS}
    try:
        data = extract_json(reply)
        text = str(data.get("reply") or "").strip() or reply.strip()
        proposal = _clamp_proposal(data.get("proposal"))
        checklist = _sanitize_checklist(data.get("checklist"))
    except ValueError:
        # 模型没按契约输出:把原文当回复,不出方案,对话继续
        text, proposal = reply.strip(), None
    # 用户明确喊开机而模型仍没给方案:用历史内容兜底组装,保证对话能收敛
    if proposal is None and _START_INTENT_RE.search(last_user):
        proposal = _fallback_proposal_from_history(history)
        if proposal is not None:
            text = "好,按我们聊的内容开机。"
    if proposal is not None:
        # 时长/画幅以代码正则提取为准(用户在对话里明确说过的值覆盖 LLM 的)
        proposal.update(overrides)
        fallback = _checklist_from_proposal(proposal, overrides)
        for key, value in fallback.items():
            if not checklist.get(key):
                checklist[key] = value
    else:
        # 还没到出方案的程度:把用户明确说过的时长/画幅也记进清单
        if overrides.get("duration_target_s"):
            checklist["duration"] = f"{overrides['duration_target_s']} 秒"
        if overrides.get("aspect_ratio"):
            checklist["aspect"] = str(overrides["aspect_ratio"])
    return {"reply": text, "proposal": proposal, "checklist": checklist}
