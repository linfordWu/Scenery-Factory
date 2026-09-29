# SPDX-License-Identifier: GPL-3.0-only
"""镜头首帧生成：用首帧约束代替纯文本，稳定关键物体的初始状态。

纯文生视频容易把动作的开始态/结束态混进同一帧（如盘子既在手中又在桌上）。
本模块为带关键物体契约（object_states）的镜头先画一张「开始状态」首帧图，
插入 reference_assets 最前并登记 first_frame 绑定 —— 渲染工作流的
MiniMaxH3ReferenceToVideo 节点会把它作为视觉锚点（reference-to-video），
物体的数量、位置和初始姿态由此固定。
"""
from __future__ import annotations

import logging

from ..domain.schemas.core import AssetBinding, Shot

log = logging.getLogger(__name__)

_FIRST_FRAME_PROMPT = (
    "{style}，视频镜头首帧静帧，{characters}"
    "{scene_part}画面内容：{start_states}。"
    "{action_part}"
    "构图为该镜头开始瞬间，主体姿态与物体位置严格符合上述开始状态，"
    "关键物体数量严格恒定，不得重复、悬浮、穿模，"
    "画面中不出现任何文字、字幕、水印、logo、标识"
)


def _first_frame_prompt(style: str, shot: Shot, scene_title: str) -> str:
    spec = shot.spec
    characters = "; ".join(
        f"{c.get('name')}: {c.get('description')}"
        for c in spec.characters
        if c.get("kind") != "location" and c.get("name")
    )
    locations = "; ".join(
        f"{c.get('name')}: {c.get('description')}"
        for c in spec.characters
        if c.get("kind") == "location" and c.get("name")
    )
    scene_part = f"场景：{locations}。" if locations else \
        (f"场景：{scene_title}。" if scene_title else "")
    start_states = []
    for o in spec.object_states or []:
        name = str(o.get("name") or "").strip()
        if not name:
            continue
        count = str(o.get("count") or "").strip()
        start = str(o.get("start_state") or "").strip()
        entry = f"{count}{name}" if count else name
        if start:
            entry += f"开始于「{start}」"
        start_states.append(entry)
    if not start_states and spec.action:
        start_states.append(spec.action)
    action_part = ""
    motion = spec.motion_contract or {}
    if str(motion.get("start_state") or "").strip():
        action_part = f"动作开始状态：{motion['start_state']}。"
    return _FIRST_FRAME_PROMPT.format(
        style=style or "动画风格",
        characters=(characters + "，") if characters else "",
        scene_part=scene_part,
        start_states="；".join(start_states),
        action_part=action_part)


def generate_shot_first_frames(store, image_model, project_id: str,
                               style: str = "",
                               should_stop=None) -> dict[str, str]:
    """为所有带 object_states 且尚无首帧绑定的镜头生成首帧图。

    返回 {shot_id: asset_id}（只含本次新生成的）。单个镜头失败只告警
    不中断其余镜头；首帧图插入 reference_assets 最前（渲染取第一张）。
    should_stop 返回 True 时在下一个镜头前停止。
    """
    made: dict[str, str] = {}
    scene_titles = {s.scene_id: s.title
                    for s in store.all("scenes", project_id=project_id)}
    bound = {b.shot_id for b in store.all("bindings")
             if b.role == "first_frame"}
    shots = [s for s in store.all("shots", project_id=project_id)
             if s.spec.object_states and s.shot_id not in bound]
    if not shots:
        return made

    assets_by_key = {a.storage_key: a for a in store.all("assets")}
    aspect_sizes = {"16:9": (1216, 832), "9:16": (832, 1216),
                    "1:1": (1024, 1024)}
    for shot in shots:
        if should_stop is not None and should_stop():
            log.info("first-frame generation stopped by request (%s)",
                     project_id)
            break
        try:
            width, height = aspect_sizes.get(shot.spec.aspect_ratio,
                                             (832, 1216))
            out_key = f"{project_id}/first_frame/{shot.shot_id}"
            storage_key = image_model.generate(
                _first_frame_prompt(style, shot,
                                    scene_titles.get(shot.scene_id, "")),
                out_key, width=width, height=height)
            asset = assets_by_key.get(storage_key)
            if asset is None:  # asset_store 落库后重新查一次
                assets_by_key = {a.storage_key: a for a in store.all("assets")}
                asset = assets_by_key.get(storage_key)
            if asset is None:
                log.warning("first-frame asset not found in store: %s",
                            storage_key)
                continue
            asset.category = "location"
            store.put("assets", asset)
            store.put("bindings", AssetBinding(
                shot_id=shot.shot_id, asset_id=asset.asset_id,
                asset_version=asset.version, role="first_frame"))
            if asset.asset_id not in shot.spec.reference_assets:
                shot.spec.reference_assets.insert(0, asset.asset_id)
                store.put("shots", shot)
            made[shot.shot_id] = asset.asset_id
            log.info("first frame ready: %s -> %s", shot.shot_id,
                     asset.asset_id)
        except Exception:
            log.warning("first frame failed for %s", shot.shot_id,
                        exc_info=True)
    return made
