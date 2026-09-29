# SPDX-License-Identifier: GPL-3.0-only
"""Prompt-text hygiene shared by the API composer and the dialogue planner."""
from __future__ import annotations

import re

_TRIM_CHARS = '"\'“”‘’ \t\r\n'
_TRAILING_QUOTE = re.compile(r'["\'“”‘’]+([.。!?！？])$')


def clean_dialogue(text: str) -> str:
    """台词清洗：LLM 常带引号/换行/尾标点噪声。

    直接进 `<d>` 标签会被读出来、进 SRT 会被烧进字幕，统一在写提示词和
    字幕前清理。
    """
    t = str(text or "").strip()
    t = t.strip(_TRIM_CHARS)
    # 形如 台词". / 台词"。 的尾部残留引号
    t = _TRAILING_QUOTE.sub(r"\1", t)
    return " ".join(t.split())
