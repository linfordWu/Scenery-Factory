# SPDX-License-Identifier: GPL-3.0-only
"""角色定妆照生成(agents/casting.py + portraits 端点)测试,全 mock 离线。"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if "svf" not in sys.modules:
    pkg = types.ModuleType("svf")
    pkg.__path__ = [str(ROOT)]
    sys.modules["svf"] = pkg

from fastapi.testclient import TestClient  # noqa: E402

from svf.agents.casting import generate_character_portraits  # noqa: E402
from svf.apps.api.app import create_app  # noqa: E402
from svf.domain.repositories.store import Store  # noqa: E402
from svf.domain.schemas.core import Character, Shot, ShotSpec  # noqa: E402

from tests.test_api import FakeAssetStore, FakeJudge, FakeTextModel  # noqa: E402


class FakeImageModel:
    """模拟 QwenImageModel:generate 落一个真 asset,返回 storage_key。"""

    def __init__(self, asset_store, store=None):
        self.asset_store = asset_store
        self.store = store
        self.prompts: list[str] = []

    def available(self):
        return True

    def generate(self, prompt, out_key, width=832, height=1216):
        self.prompts.append(prompt)
        asset = self.asset_store.save_bytes(
            b"png-" + out_key.encode(), out_key.split("/", 1)[0], "image",
            out_key.rsplit("/", 1)[-1] + ".png", source="generated")
        if self.store is not None:
            self.store.put("assets", asset)
        return asset.storage_key


@pytest.fixture()
def env(tmp_path):
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    image_model = FakeImageModel(asset_store, store)
    app = create_app(store, asset_store, renderer=None, judge=FakeJudge(),
                     decision=None, text_model=FakeTextModel(),
                     data_dir=str(tmp_path / "data"), start_worker=False,
                     image_model=image_model)
    client = TestClient(app)
    yield client, store, asset_store, image_model
    app.state.engine.shutdown()
    store.close()


def _make_cast(store, pid):
    ch1 = Character(project_id=pid, name="小林", kind="character",
                    description="25岁女店员,棕色短发,蓝色制服")
    ch2 = Character(project_id=pid, name="老人", kind="character",
                    description="75岁,银发,深色风衣")
    loc = Character(project_id=pid, name="便利店", kind="location",
                    description="深夜便利店,暖光")
    has_ref = Character(project_id=pid, name="路人", kind="character",
                        description="路人甲", asset_id="asset_existing")
    for c in (ch1, ch2, loc, has_ref):
        store.put("characters", c)
    return ch1, ch2, loc, has_ref


def test_portraits_generated_and_bound(env):
    client, store, asset_store, image_model = env
    pid = "p_test_cast"
    ch1, ch2, loc, has_ref = _make_cast(store, pid)
    shot = Shot(shot_id="shot_x", scene_id="scene_x", project_id=pid, order=0,
                spec=ShotSpec(shot_id="shot_x",
                              characters=[{"character_id": ch1.character_id,
                                           "name": "小林", "kind": "character",
                                           "description": ch1.description}]))
    store.put("shots", shot)

    made = generate_character_portraits(store, image_model, pid,
                                        style="写实电影感")

    # 两个无参考图的角色生成成功,地点跳过,已有参考图的角色不重复生成
    assert set(made) == {ch1.character_id, ch2.character_id}
    assert len(image_model.prompts) == 2
    assert "纯色简洁背景" in image_model.prompts[0]
    assert "写实电影感" in image_model.prompts[0]
    # character.asset_id 写回 + 素材归类 character
    ch1_after = store.get("characters", ch1.character_id)
    assert ch1_after.asset_id == made[ch1.character_id]
    asset = store.get("assets", made[ch1.character_id])
    assert asset.category == "character"
    # 定妆图插入引用该角色的镜头 reference_assets 最前
    shot_after = store.get("shots", "shot_x")
    assert shot_after.spec.reference_assets[0] == made[ch1.character_id]
    # 幂等:再跑一遍没有新生成
    assert generate_character_portraits(store, image_model, pid) == {}


def test_portraits_endpoint(env):
    client, store, asset_store, image_model = env
    r = client.post("/projects", json={"title": "t"})
    pid = r.json()["project_id"]
    _make_cast(store, pid)
    r = client.post(f"/projects/{pid}/characters/portraits")
    assert r.status_code == 202
    import time
    for _ in range(50):
        cast = store.all("characters", project_id=pid)
        if sum(1 for c in cast if c.asset_id and c.asset_id != "asset_existing") == 2:
            break
        time.sleep(0.1)
    cast = {c.name: c for c in store.all("characters", project_id=pid)}
    assert cast["小林"].asset_id
    assert cast["老人"].asset_id
    assert cast["便利店"].asset_id  # 地点也生成场景参考图
    assert cast["路人"].asset_id == "asset_existing"  # 已有参考不覆盖


def test_location_references_generated_and_appended(env):
    client, store, asset_store, image_model = env
    pid = "p_test_loc"
    ch1, ch2, loc, has_ref = _make_cast(store, pid)
    shot = Shot(shot_id="shot_loc", scene_id="scene_x", project_id=pid, order=0,
                spec=ShotSpec(shot_id="shot_loc",
                              characters=[{"character_id": loc.character_id,
                                           "name": "便利店", "kind": "location",
                                           "description": loc.description}]))
    store.put("shots", shot)

    from svf.agents.casting import generate_location_references
    made = generate_location_references(store, image_model, pid,
                                        style="写实电影感")

    # 只有缺参考图的地点生成;角色跳过
    assert set(made) == {loc.character_id}
    assert len(image_model.prompts) == 1
    assert "空镜无人物" in image_model.prompts[0]
    assert "广角全景" in image_model.prompts[0]
    # asset_id 写回 + 素材归类 location + 追加到镜头末尾(不抢首锚)
    assert store.get("characters", loc.character_id).asset_id == \
        made[loc.character_id]
    assert store.get("assets", made[loc.character_id]).category == "location"
    assert store.get("shots", "shot_loc").spec.reference_assets[-1] == \
        made[loc.character_id]
    # 幂等
    assert generate_location_references(store, image_model, pid) == {}
