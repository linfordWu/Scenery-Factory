# SPDX-License-Identifier: GPL-3.0-only
"""角色定妆照 + 场景参考图生成：像素级身份/空间锁定。

规划阶段登记的角色/地点只有文字外观描述，渲染时模型仍可能漂移
（换装/换发型/场景布局跳变）。本模块为角色生成定妆照、为地点生成
空镜场景参考图，写回 character.asset_id 并合入各镜头的
reference_assets —— 渲染链路会把第一张参考图注入工作流作为锚点。
"""
from __future__ import annotations

import logging

from ..domain.schemas.core import Character

log = logging.getLogger(__name__)

_PORTRAIT_PROMPT = (
    "{style}，角色定妆参考图，全身像，{name}：{description}，"
    "纯色简洁背景，正面站姿，面部清晰，画质精细，"
    "画面中不出现任何文字、字幕、水印、logo、标识"
)

_LOCATION_PROMPT = (
    "{style}，场景参考图，空镜无人物，{name}：{description}，"
    "广角全景，空间布局、陈设与光线方向清晰稳定，画质精细，"
    "画面中不出现任何文字、字幕、水印、logo、标识"
)


def _portrait_prompt(style: str, ch: Character) -> str:
    style_part = style or "动画风格"
    return _PORTRAIT_PROMPT.format(style=style_part, name=ch.name,
                                   description=ch.description)


def _location_prompt(style: str, ch: Character) -> str:
    style_part = style or "动画风格"
    return _LOCATION_PROMPT.format(style=style_part, name=ch.name,
                                   description=ch.description)


def generate_character_portraits(store, image_model, project_id: str,
                                 style: str = "",
                                 should_stop=None) -> dict[str, str]:
    """为项目下所有缺参考图的角色生成定妆照。

    返回 {character_id: asset_id}（只含本次新生成的）。任何单个角色失败
    只告警不中断其余角色；should_stop 返回 True 时在下一个角色前停止
    （项目级停止用，当前这张图会先完成）。
    """
    made: dict[str, str] = {}
    characters = [c for c in store.all("characters", project_id=project_id)
                  if c.kind == "character" and not c.asset_id]
    if not characters:
        return made

    assets_by_key = {a.storage_key: a for a in store.all("assets")}
    for ch in characters:
        if should_stop is not None and should_stop():
            log.info("portrait generation stopped by request (%s)", project_id)
            break
        try:
            out_key = f"{project_id}/cast/{ch.name}"
            storage_key = image_model.generate(
                _portrait_prompt(style, ch), out_key,
                width=832, height=1216)
            asset = assets_by_key.get(storage_key)
            if asset is None:  # asset_store 落库后重新查一次
                assets_by_key = {a.storage_key: a for a in store.all("assets")}
                asset = assets_by_key.get(storage_key)
            if asset is None:
                log.warning("portrait asset not found in store: %s", storage_key)
                continue
            asset.category = "character"
            store.put("assets", asset)
            ch.asset_id = asset.asset_id
            store.put("characters", ch)
            made[ch.character_id] = asset.asset_id
            log.info("portrait ready: %s -> %s", ch.name, asset.asset_id)
        except Exception:
            log.warning("portrait failed for %s", ch.name, exc_info=True)

    if not made:
        return made

    # 把定妆图合入所有引用该角色的镜头 reference_assets（置最前，渲染取第一张）
    for shot in store.all("shots", project_id=project_id):
        changed = False
        for entry in shot.spec.characters:
            aid = made.get(entry.get("character_id", ""))
            if aid and aid not in shot.spec.reference_assets:
                shot.spec.reference_assets.insert(0, aid)
                changed = True
        if changed:
            store.put("shots", shot)
    return made


def generate_location_references(store, image_model, project_id: str,
                                 style: str = "",
                                 should_stop=None) -> dict[str, str]:
    """为项目下所有缺参考图的地点(kind=location)生成场景参考图。

    空镜全景、横构图,锁定空间布局与光线方向;写回 character.asset_id
    并追加到引用该场景的镜头 reference_assets 末尾(首帧/定妆照保持
    最前优先级,渲染链路只取第一张)。返回 {character_id: asset_id}。
    should_stop 返回 True 时在下一个地点前停止。
    """
    made: dict[str, str] = {}
    locations = [c for c in store.all("characters", project_id=project_id)
                 if c.kind == "location" and not c.asset_id]
    if not locations:
        return made

    assets_by_key = {a.storage_key: a for a in store.all("assets")}
    for ch in locations:
        if should_stop is not None and should_stop():
            log.info("location reference generation stopped by request (%s)",
                     project_id)
            break
        try:
            out_key = f"{project_id}/location/{ch.name}"
            storage_key = image_model.generate(
                _location_prompt(style, ch), out_key,
                width=1216, height=832)
            asset = assets_by_key.get(storage_key)
            if asset is None:  # asset_store 落库后重新查一次
                assets_by_key = {a.storage_key: a for a in store.all("assets")}
                asset = assets_by_key.get(storage_key)
            if asset is None:
                log.warning("location asset not found in store: %s",
                            storage_key)
                continue
            asset.category = "location"
            store.put("assets", asset)
            ch.asset_id = asset.asset_id
            store.put("characters", ch)
            made[ch.character_id] = asset.asset_id
            log.info("location ref ready: %s -> %s", ch.name, asset.asset_id)
        except Exception:
            log.warning("location ref failed for %s", ch.name, exc_info=True)

    if not made:
        return made

    # 场景参考图追加到引用镜头的 reference_assets 末尾,不抢首帧/定妆锚
    for shot in store.all("shots", project_id=project_id):
        changed = False
        for entry in shot.spec.characters:
            aid = made.get(entry.get("character_id", ""))
            if aid and aid not in shot.spec.reference_assets:
                shot.spec.reference_assets.append(aid)
                changed = True
        if changed:
            store.put("shots", shot)
    return made
