# SPDX-License-Identifier: GPL-3.0-only
"""Shared, non-negotiable constraints for narrative video generation.

The renderer, planner and vision judge must agree on what a valid moving shot
is.  Keeping this in one module prevents a good planning prompt from being
weakened later by the final render prompt or by a permissive reviewer.
"""
from __future__ import annotations

from .schemas.core import Acceptance, ShotSpec


SYSTEM_VIDEO_CONSTRAINTS = (
    "系统级视频约束（必须执行）：禁止生成把单张人物图、风景图或插画进行放大、"
    "缩小、平移、裁切、慢推、Ken Burns 或幻灯片式处理来冒充视频。每个镜头都必须"
    "呈现可见且与本镜剧情节拍直接相关的时间变化：焦点角色完成指定动作，至少一个"
    "关键道具、环境、光影或景深视差对该动作产生独立且合理的响应。人物、风景与道具"
    "不能各自静止或各自动作，必须共同服务同一剧情事件。相机运动只能辅助叙事，不能"
    "成为画面中唯一的变化。人物身份、服装、发型、场景布局和关键道具必须与锁定参考"
    "保持一致；不得出现多余人物、肢体畸形、物体凭空出现/消失、乱码文字、水印或 logo。"
)

DEFAULT_REQUIRED = (
    "焦点角色完成镜头描述中的关键剧情动作",
    "至少一个关键道具、环境、光影或景深变化与该动作产生剧情关联",
    "人物、场景、服装和关键道具与锁定参考保持一致",
    "关键物体数量全片恒定，动作的开始状态与结束状态分别成立",
)

DEFAULT_FORBIDDEN = (
    "静态人物图、风景图或插画的放大、平移、裁切、慢推、Ken Burns、幻灯片式伪视频",
    "只有相机运动而角色、关键道具和环境没有剧情相关变化",
    "角色身份漂移、服装或发型无剧情理由变化、场景布局跳变",
    "额外人物、肢体或手指畸形、关键物体凭空出现或消失",
    "同一关键物体被复制成多份（如两盘相同煎蛋）、物体悬浮或悬空无支撑、"
    "手与物体的抓取/接触关系不成立",
    "动作的开始态与结束态混合在同一帧（如盘子既在手中又已在桌上）",
    "画面文字、乱码字幕、水印、logo、标识",
)


def constrained_acceptance(acceptance: Acceptance | None = None) -> Acceptance:
    """Add baseline checks while preserving director-specified checks verbatim."""
    acceptance = acceptance or Acceptance()
    required = list(acceptance.required or [])
    forbidden = list(acceptance.forbidden or [])
    for item in DEFAULT_REQUIRED:
        if item not in required:
            required.append(item)
    for item in DEFAULT_FORBIDDEN:
        if item not in forbidden:
            forbidden.append(item)
    return Acceptance(required=required, forbidden=forbidden,
                      rubric_version=acceptance.rubric_version)


def plan_limits(duration_target_s: int) -> tuple[int, int]:
    """结构化方案的数量上限 (场次, 镜头)：按时长推导。

    既防 LLM 超发（提示词约束是软性的），也防外部接口塞入超大方案。
    上限只防失控,不替代规划:典型 10-180 秒短剧远达不到上限。
    """
    target = max(10, int(duration_target_s or 60))
    return (max(4, min(60, -(-target // 5))),
            max(3, min(160, -(-target // 4))))


def normalize_durations(durations: list[int], target: int,
                        low: int = 3, high: int = 8,
                        threshold: float = 0.15) -> list[int]:
    """全片时长归一化：偏差超过 threshold 时按比例缩放，单镜钳制 [low, high]。

    渲染器单镜时长有限（H3 4-8 秒），规划链路的软约束挡不住 LLM 超发，
    这里做确定性收敛。对话方案预览与最终落库共用本函数，保证所见即所得。
    """
    total = sum(int(d or 0) for d in durations)
    if not durations or total <= 0 or target <= 0 \
            or abs(total - target) / target <= threshold:
        return [int(d or low) for d in durations]
    scale = target / total
    return [max(low, min(high, round(int(d or low) * scale)))
            for d in durations]


def locked_visual_block(spec: ShotSpec) -> str:
    """Stable role/scene descriptions copied unchanged into every shot prompt."""
    characters: list[str] = []
    locations: list[str] = []
    for item in spec.characters or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("character_id") or "").strip()
        description = str(item.get("description") or "").strip()
        if not name and not description:
            continue
        lock = f"{name}: {description}" if description else name
        if str(item.get("kind") or "character") == "location":
            locations.append(lock)
        else:
            characters.append(lock)
    sections: list[str] = []
    if characters:
        sections.append("角色锁定（逐字保持）: " + "; ".join(characters))
    if locations:
        sections.append("场景锁定（逐字保持）: " + "; ".join(locations))
    return " | ".join(sections)
