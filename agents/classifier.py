# SPDX-License-Identifier: GPL-3.0-only
"""Asset classifier: laya System 1 choice question, rule-based fallback.

laya 懒加载为进程级全局单例（首次使用时才加载，权重可能要从
ModelScope/HF 下载）；import / 加载 / 推理任何一步失败都回退到规则分类，
绝不抛异常。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from ..domain.schemas.core import ASSET_CATEGORIES, Asset

log = logging.getLogger(__name__)

LAYA_MODEL_ID = "convaiinnovations/laya"

# 规则 fallback 的关键词表（匹配文件名，大小写不敏感）
_RULE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("character", ("角色", "character", "人物")),
    ("location", ("场景", "location", "scene")),
    ("prop", ("道具", "prop")),
    ("style", ("风格", "style")),
]

_QUESTION = {
    "category": {
        "type": "choice",
        "instructions": "根据素材元数据，判断它在短剧生产中的用途分类。",
        "criteria": {
            "character": "角色参考图（人物外观参考）",
            "location": "场景/地点参考图",
            "prop": "道具参考图",
            "style": "风格参考（色调、画风的参照）",
            "footage": "实拍或已有的视频素材",
            "audio": "音频素材（配乐、音效、配音）",
            "export": "导出的成片",
            "other": "以上都不是",
        },
    }
}

# 图片素材专用:选项空间只保留图片相关类别,决策更准
_QUESTION_IMAGE = {
    "category": {
        "type": "choice",
        "instructions": "这是一张图片素材，判断它在短剧制作中作为什么用途的参考图。",
        "criteria": {
            "character": "人物/角色的外观参考图",
            "location": "场景或地点的环境参考图",
            "prop": "道具物品参考图",
            "style": "画风/色调风格参考图",
            "other": "其他用途",
        },
    }
}

_agent = None
_load_failed = False
_agent_lock = threading.Lock()


def _load_agent(model_id: str = LAYA_MODEL_ID):
    """进程级懒加载单例；失败记 _load_failed 并返回 None。"""
    global _agent, _load_failed
    with _agent_lock:
        if _agent is not None or _load_failed:
            return _agent
        try:
            import laya  # noqa: PLC0415 懒加载：可能尚未安装/仍在下载
            from pathlib import Path  # noqa: PLC0415

            from ..config.settings import LAYA_MODEL_DIR  # noqa: PLC0415
            # 本地权重存在则直接加载本地路径(避免无外网时等 HF 下载超时)
            if Path(LAYA_MODEL_DIR, "rl_agent_config.json").is_file():
                _agent = laya.load(LAYA_MODEL_DIR, device="cpu", subfolder="multilingual")
                log.info("laya classifier loaded from local weights: %s", LAYA_MODEL_DIR)
            else:
                _agent = laya.load(model_id, device="cpu")
                log.info("laya classifier loaded: %s", model_id)
        except Exception as exc:
            log.warning("laya unavailable, asset classify falls back to rules: %s", exc)
            _load_failed = True
            _agent = None
        return _agent


def _rules_category(asset: Asset) -> str:
    """关键词 + media_type 兜底。"""
    name = str(asset.metadata.get("original_filename")
               or Path(asset.storage_key).name).lower()
    for category, keywords in _RULE_KEYWORDS:
        if any(k.lower() in name for k in keywords):
            return category
    if asset.media_type == "video":
        return "footage"
    if asset.media_type == "audio":
        return "audio"
    return "other"


class LayaAssetClassifier:
    """素材自动分类器（laya System 1，失败回退规则）。"""

    def __init__(self, model_id: str = LAYA_MODEL_ID):
        self.model_id = model_id

    # ------------------------------------------------------------- state ---
    def _state_text(self, asset: Asset) -> str:
        filename = str(asset.metadata.get("original_filename")
                       or Path(asset.storage_key).name)
        parts = [f"文件名: {filename}", f"媒体类型: {asset.media_type}",
                 f"来源: {asset.source}"]
        size = asset.metadata.get("size_bytes") or asset.metadata.get("size")
        if size:
            parts.append(f"大小: {size} bytes")
        duration = asset.metadata.get("duration")
        if duration:
            parts.append(f"时长: {duration}s")
        if asset.metadata.get("width"):
            parts.append(f"分辨率: {asset.metadata.get('width')}x"
                         f"{asset.metadata.get('height')}")
        return "；".join(parts)

    # ---------------------------------------------------------- classify ---
    def classify(self, asset: Asset) -> str:
        """返回 ASSET_CATEGORIES 之一；任何故障回退规则分类。"""
        # derived 来源的成片直接归 export，不进 laya
        if asset.source == "derived" and asset.media_type == "video":
            return "export"
        # 视频/音频按媒体类型直接归类,无需占用 laya
        if asset.media_type == "video":
            return "footage"
        if asset.media_type == "audio":
            return "audio"
        agent = _load_agent(self.model_id)
        if agent is not None:
            try:
                # 图片素材:只给图片相关选项,缩小决策空间提高准确率
                question = _QUESTION_IMAGE if asset.media_type == "image" else _QUESTION
                out = agent.system_one(self._state_text(asset), question)
                answer = out["answers"]["category"]
                choice = str(answer["choice"])
                confidence = float(answer.get("answer_confidence")
                                   or answer.get("confidence") or 0.0)
                if choice in ASSET_CATEGORIES and confidence >= 0.4:
                    return choice
                log.info("laya low confidence or unknown (%r, %.2f), using rules",
                         choice, confidence)
            except Exception as exc:
                log.warning("laya classify failed (%s), using rules", exc)
        return _rules_category(asset)
