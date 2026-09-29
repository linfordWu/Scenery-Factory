# SPDX-License-Identifier: GPL-3.0-only
"""物体连续性波次测试：ShotSpec.object_states、_compose_prompt 物体约束块、
judge 首/中/末帧采样与 object_consistency 维度、first_frame agent、
ShotPatch.object_states 编辑。全部 mock，不碰真实 LLM/渲染/图片服务。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if "svf" not in sys.modules:
    pkg = types.ModuleType("svf")
    pkg.__path__ = [str(ROOT)]
    sys.modules["svf"] = pkg

from fastapi.testclient import TestClient  # noqa: E402

from svf.adapters.vision_judge.judge import (  # noqa: E402
    GemmaVisionJudge, _JUDGE_INSTRUCTIONS, _object_states_block,
)
from svf.agents.first_frame import (  # noqa: E402
    _first_frame_prompt, generate_shot_first_frames,
)
from svf.apps.api.app import _compose_prompt, create_app  # noqa: E402
from svf.domain.repositories.store import Store  # noqa: E402
from svf.domain.schemas.core import (  # noqa: E402
    Project, Scene, Shot, ShotSpec, new_id,
)

from tests.test_api import FakeAssetStore, FakeJudge, FakeTextModel  # noqa: E402
from tests.test_casting import FakeImageModel  # noqa: E402

OBJECT_STATES = [
    {"name": "蓝白瓷盘", "count": "仅一只",
     "start_state": "在女孩手中", "end_state": "在木桌中央且双手离开"},
    {"name": "橘猫", "count": "仅一只",
     "start_state": "桌边打盹", "end_state": "睁眼看向餐盘"},
]


def make_spec(**kw) -> ShotSpec:
    kw.setdefault("shot_id", new_id("shot"))
    return ShotSpec(**kw)


def make_shot(store, pid: str, scene_id: str = "sc_1", order: int = 0,
              **spec_kw) -> Shot:
    if store.get("scenes", scene_id) is None:
        store.put("scenes", Scene(scene_id=scene_id, project_id=pid, order=0,
                                  title="厨房"))
    shot_id = spec_kw.pop("shot_id", new_id("shot"))
    shot = Shot(shot_id=shot_id, scene_id=scene_id, project_id=pid, order=order,
                spec=ShotSpec(shot_id=shot_id, **spec_kw))
    store.put("shots", shot)
    return shot


# ------------------------------------------------------------ compose prompt --
def test_compose_prompt_includes_object_states_block():
    store = Store(":memory:")
    pid = "p_obj"
    shot = make_shot(store, pid, action="女孩把餐盘端到桌上",
                     object_states=OBJECT_STATES)
    project = Project(project_id=pid, style="手绘")
    prompt = _compose_prompt(project, shot)
    assert "关键物体约束" in prompt
    assert "蓝白瓷盘（仅一只）" in prompt
    assert "开始[在女孩手中] → 结束[在木桌中央且双手离开]" in prompt
    assert "不得混合在同一帧" in prompt
    store.close()


def test_compose_prompt_secondary_demotion_always_present():
    store = Store(":memory:")
    shot = make_shot(store, "p_obj2", action="端盘")
    prompt = _compose_prompt(None, shot)
    assert "次要元素" in prompt and "微动" in prompt
    store.close()


def test_compose_prompt_without_object_states_has_no_block():
    store = Store(":memory:")
    shot = make_shot(store, "p_obj3", action="端盘")
    prompt = _compose_prompt(None, shot)
    assert "关键物体约束" not in prompt
    store.close()


# -------------------------------------------------------------------- judge ---
def test_judge_samples_first_and_last_frames():
    positions = GemmaVisionJudge.FRAME_POSITIONS
    assert positions[0] <= 0.1       # 首帧附近
    assert positions[-1] >= 0.9      # 末帧附近
    assert len(positions) >= 3       # 中间至少一帧


def test_judge_instructions_contain_object_checks():
    text = _JUDGE_INSTRUCTIONS.format(n=4, required="- r", forbidden="- f",
                                      object_states="- plate")
    assert "object_popping" in text
    assert "continuity_break" in text
    assert "object_consistency" in text
    assert "FIRST frame" in text and "LAST frame" in text


def test_object_states_block_formatting():
    spec = make_spec(object_states=OBJECT_STATES)
    block = _object_states_block(spec)
    assert "蓝白瓷盘: count=仅一只" in block
    assert "start=在女孩手中" in block
    assert "end=在木桌中央且双手离开" in block
    empty = _object_states_block(make_spec())
    assert "no key objects" in empty


def test_object_states_block_skips_invalid_entries():
    spec = make_spec(object_states=[{"name": ""},
                                    {"name": "盘", "count": "仅一只"}])
    block = _object_states_block(spec)
    assert block.count("\n") == 0 and "盘" in block


# -------------------------------------------------------------- first frame ---
def test_first_frame_prompt_uses_start_states():
    store = Store(":memory:")
    shot = make_shot(store, "p_ff", action="端盘落桌",
                     characters=[{"name": "女孩", "kind": "character",
                                  "description": "浅色围裙"}],
                     object_states=OBJECT_STATES)
    prompt = _first_frame_prompt("手绘", shot, "厨房")
    assert "首帧" in prompt
    assert "仅一只蓝白瓷盘开始于「在女孩手中」" in prompt
    assert "女孩: 浅色围裙" in prompt
    store.close()


def test_generate_first_frames_binds_and_prepends(tmp_path):
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    image_model = FakeImageModel(asset_store, store)
    with_obj = make_shot(store, "p_ff2", action="端盘",
                         object_states=OBJECT_STATES)
    without_obj = make_shot(store, "p_ff2", scene_id="sc_2", order=1,
                            action="猫摇尾巴")
    made = generate_shot_first_frames(store, image_model, "p_ff2",
                                      style="手绘")
    assert list(made) == [with_obj.shot_id]          # 无契约镜头跳过
    shot = store.get("shots", with_obj.shot_id)
    assert shot.spec.reference_assets[0] == made[with_obj.shot_id]
    bindings = [b for b in store.all("bindings") if b.role == "first_frame"]
    assert len(bindings) == 1 and bindings[0].shot_id == with_obj.shot_id
    assert "首帧" in image_model.prompts[0]
    store.close()


def test_generate_first_frames_idempotent(tmp_path):
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    image_model = FakeImageModel(asset_store, store)
    make_shot(store, "p_ff3", action="端盘", object_states=OBJECT_STATES)
    first = generate_shot_first_frames(store, image_model, "p_ff3")
    second = generate_shot_first_frames(store, image_model, "p_ff3")
    assert len(first) == 1 and second == {}          # 已有绑定不重复生成
    store.close()


# --------------------------------------------------------------- ShotPatch ----
def test_shot_patch_object_states(tmp_path):
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    app = create_app(store, asset_store, renderer=None, judge=FakeJudge(),
                     decision=None, text_model=FakeTextModel(),
                     data_dir=str(tmp_path / "data"), start_worker=False)
    client = TestClient(app)
    store.put("projects", Project(project_id="p_patch"))
    shot = make_shot(store, "p_patch", action="端盘")
    resp = client.patch(f"/shots/{shot.shot_id}",
                        json={"object_states": OBJECT_STATES})
    assert resp.status_code == 200
    updated = store.get("shots", shot.shot_id)
    assert updated.spec.object_states == OBJECT_STATES
    # 非法条目被过滤
    resp = client.patch(f"/shots/{shot.shot_id}",
                        json={"object_states": [{"name": ""},
                                                {"name": "盘", "count": "1"}]})
    assert resp.status_code == 200
    assert store.get("shots", shot.shot_id).spec.object_states == [
        {"name": "盘", "count": "1", "start_state": "", "end_state": ""}]
    app.state.engine.shutdown()
    store.close()
