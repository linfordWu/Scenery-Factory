# SPDX-License-Identifier: GPL-3.0-only
"""提示词密度负载分 + 裸情绪词静态检查。

思想来源：视频生成模型对单镜头的承载量有限——台词多、人物多、运镜复杂、
又要换场景时内容会互相挤压导致糊掉；裸情绪词（“紧张地”）没有可拍摄的
形体载体。这里只做静态度量与告警，不改写 prompt。
"""
from __future__ import annotations

import re

from ..domain.schemas.core import ShotSpec

# 台词断句
_SENT_SPLIT = re.compile(r"[。!！?？；;\n]+")

# 运镜键：只有这些键描述镜头运动；shot_size/景别之类不算运镜
_MOVEMENT_KEYS = ("movement", "camera_move", "camera_movement", "move", "运镜")
_STATIC_VALUES = {"static", "fixed", "lockoff", "固定", "定机位", "固定机位", "静止"}

# 裸情绪词小词表（中文 + 英文）：出现时不改写，仅在 density 警告事件里提示
EMOTION_WORDS: tuple[str, ...] = (
    "紧张", "激动", "悲伤地", "悲伤", "兴奋", "愤怒", "恐惧", "绝望", "焦虑",
    "难过", "开心", "委屈", "释然", "戏剧性",
    "epic", "cinematic", "dramatic", "emotional", "tense", "sadly",
)


def density_score(spec: ShotSpec) -> float:
    """负载分：台词每句 +1、每个角色 +1、运镜非静态 +0.5、切入新场景 +2。"""
    load = 0.0
    load += sum(1 for s in _SENT_SPLIT.split(spec.dialogue or "") if s.strip())
    load += len(spec.characters or [])
    camera = spec.camera or {}
    for k, v in camera.items():
        if str(k).lower() in _MOVEMENT_KEYS:
            if str(v).strip() and str(v).strip().lower() not in _STATIC_VALUES:
                load += 0.5
                break
    # sequence_first = 场景首镜 = 切入新场景
    if spec.sequence_relation == "sequence_first":
        load += 2.0
    return load


def emotion_hits(spec: ShotSpec) -> list[str]:
    """action / felt_intent 里出现的裸情绪词（大小写不敏感）。"""
    text = f"{spec.action} {spec.felt_intent}"
    lower = text.lower()
    return [w for w in EMOTION_WORDS if w in text or w.lower() in lower]
