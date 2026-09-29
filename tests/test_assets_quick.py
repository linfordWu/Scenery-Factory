# SPDX-License-Identifier: GPL-3.0-only
"""素材分类体系 + laya 分类器 + 一键成片（/projects/quick）测试。

laya 不可用（venv 中 import 失败）是预期情况：规则路径直接测，
laya 路径用 monkeypatch 替换 _load_agent。
"""
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

import svf.agents.classifier as classifier_mod  # noqa: E402
from svf.agents.classifier import LayaAssetClassifier  # noqa: E402
from svf.apps.api.app import create_app  # noqa: E402
from svf.domain.repositories.store import Store  # noqa: E402

from tests.test_api import FakeAssetStore, FakeJudge, FakeTextModel, wait_for  # noqa: E402
from tests.test_casting import FakeImageModel  # noqa: E402
from tests.test_director import make_project  # noqa: E402


# ------------------------------------------------------------------- helpers --
@pytest.fixture()
def env(tmp_path):
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    app = create_app(store, asset_store, renderer=None, judge=FakeJudge(),
                     decision=None, text_model=FakeTextModel(),
                     data_dir=str(tmp_path / "data"), start_worker=False,
                     image_model=FakeImageModel(asset_store, store))
    client = TestClient(app)
    yield client, store, asset_store, app
    app.state.engine.shutdown()
    store.close()


def make_asset(store, asset_store, pid, filename, media_type, source="imported"):
    asset = asset_store.save_bytes(b"data-" + filename.encode(), pid, media_type,
                                   filename, source=source)
    asset.metadata["original_filename"] = filename
    store.put("assets", asset)
    return asset


# ----------------------------------------------------------------- category --
def test_category_set_and_filter(env):
    client, store, asset_store, _ = env
    pid = make_project(client)
    img = make_asset(store, asset_store, pid, "a.png", "image")
    vid = make_asset(store, asset_store, pid, "b.mp4", "video")

    r = client.post(f"/assets/{img.asset_id}/category", json={"category": "character"})
    assert r.status_code == 200, r.text
    assert r.json()["category"] == "character"
    assert store.get("assets", img.asset_id).category == "character"
    # 非法分类被 pydantic 拒绝
    assert client.post(f"/assets/{img.asset_id}/category",
                       json={"category": "alien"}).status_code == 422
    assert client.post("/assets/asset_nope/category",
                       json={"category": "prop"}).status_code == 404

    # 组合筛选
    got = client.get("/assets", params={"category": "character"}).json()
    assert [a["asset_id"] for a in got] == [img.asset_id]
    got = client.get("/assets", params={"media_type": "video"}).json()
    assert [a["asset_id"] for a in got] == [vid.asset_id]
    got = client.get("/assets", params={"project_id": pid,
                                        "category": "character"}).json()
    assert [a["asset_id"] for a in got] == [img.asset_id]
    # 不带参数：全部素材（保持现有行为）
    assert len(client.get("/assets").json()) == 2


def test_classify_rules_path(env, monkeypatch):
    """laya 不可用 -> 规则关键词 + media_type 兜底。"""
    client, store, asset_store, _ = env
    pid = make_project(client)
    monkeypatch.setattr(classifier_mod, "_load_agent", lambda model_id: None)

    cases = [("角色_艾米.png", "image", "imported", "character"),
             ("scene_咖啡馆.png", "image", "imported", "location"),
             ("道具_雨伞.png", "image", "imported", "prop"),
             ("style_ref.png", "image", "imported", "style"),
             ("clip.mp4", "video", "imported", "footage"),
             ("bgm.mp3", "audio", "imported", "audio"),
             ("plain.png", "image", "imported", "other"),
             ("final.mp4", "video", "derived", "export")]   # derived 成片直归 export
    ids = {}
    for filename, media_type, source, _expected in cases:
        a = make_asset(store, asset_store, pid, filename, media_type, source)
        ids[a.asset_id] = _expected
    # 已分类素材不动
    preset = make_asset(store, asset_store, pid, "preset.png", "image")
    preset.category = "style"
    store.put("assets", preset)

    r = client.post(f"/projects/{pid}/assets/classify")
    assert r.status_code == 200, r.text
    classified = r.json()["classified"]
    assert classified == ids
    assert preset.asset_id not in classified
    assert store.get("assets", preset.asset_id).category == "style"
    # 再跑一次：全部已分类，空结果
    assert client.post(f"/projects/{pid}/assets/classify").json()["classified"] == {}


class _FakeLayaAgent:
    def __init__(self, choice):
        self.choice = choice
        self.states: list[str] = []

    def system_one(self, state, questions):
        self.states.append(state)
        return {"answers": {"category": {"choice": self.choice,
                                         "confidence": 0.93}}}


def test_classify_mock_laya(env, monkeypatch):
    """laya 路径：choice 合法直接用；非法回退规则。"""
    client, store, asset_store, _ = env
    pid = make_project(client)
    agent = _FakeLayaAgent("character")   # 规则会给 other，验证确实走了 laya
    monkeypatch.setattr(classifier_mod, "_load_agent", lambda model_id: agent)

    neutral = make_asset(store, asset_store, pid, "zz_neutral.png", "image")
    r = client.post(f"/projects/{pid}/assets/classify")
    assert r.json()["classified"] == {neutral.asset_id: "character"}
    assert agent.states and "zz_neutral.png" in agent.states[0]

    # laya 返回非法分类 -> 规则兜底
    bad = _FakeLayaAgent("weird")
    monkeypatch.setattr(classifier_mod, "_load_agent", lambda model_id: bad)
    neutral2 = make_asset(store, asset_store, pid, "zz2.mp4", "video")
    r = client.post(f"/projects/{pid}/assets/classify")
    assert r.json()["classified"] == {neutral2.asset_id: "footage"}


def test_classifier_derived_shortcut(monkeypatch):
    """derived 视频不进 laya，直接 export。"""
    def _boom(model_id):
        raise AssertionError("laya must not be loaded for derived video")
    monkeypatch.setattr(classifier_mod, "_load_agent", _boom)
    from svf.domain.schemas.core import Asset
    asset = Asset(project_id="p", source="derived", media_type="video")
    assert LayaAssetClassifier().classify(asset) == "export"


# ------------------------------------------------------------------- quick ---
def test_quick_text_mode(env):
    client, store, _, _ = env
    r = client.post("/projects/quick",
                    json={"brief": "旧情人雨夜重逢", "style": "写实",
                          "duration_target_s": 30})
    assert r.status_code == 201, r.text
    pid = r.json()["project_id"]
    assert store.get("projects", pid).brief == "旧情人雨夜重逢"

    # 后台编排：规划 -> 逐镜头建 run（FakeTextModel 出 1 场景 1 镜头）
    assert wait_for(lambda: len(store.all("runs", project_id=pid)) == 1,
                    timeout=10)
    shots = store.all("shots", project_id=pid)
    runs = store.all("runs", project_id=pid)
    assert len(shots) == 1 and runs[0].shot_id == shots[0].shot_id
    assert runs[0].state == "QUEUED"      # worker 未启动，停留在队列
    assert runs[0].prompt_spec              # 复用 create_run 的 prompt 逻辑
    types_seen = {e.type for e in store.events_since(pid)}
    assert {"quick.started", "quick.orchestrated"} <= types_seen

    # brief 必填
    assert client.post("/projects/quick", json={"title": "x"}).status_code == 422


def test_quick_assets_mode(env):
    client, store, asset_store, _ = env
    pid0 = make_project(client)   # 素材挂在任意项目下即可（素材库共享）
    img1 = make_asset(store, asset_store, pid0, "角色_艾米.png", "image")
    img2 = make_asset(store, asset_store, pid0, "scene.png", "image")

    r = client.post("/projects/quick",
                    json={"brief": "雨夜重逢", "mode": "assets",
                          "asset_ids": [img1.asset_id, img2.asset_id]})
    assert r.status_code == 201, r.text
    pid = r.json()["project_id"]
    assert wait_for(lambda: len(store.all("runs", project_id=pid)) == 1,
                    timeout=10)
    shot = store.all("shots", project_id=pid)[0]
    # 所有图片素材进 reference_assets，第一张为主参考
    assert shot.spec.reference_assets[:2] == [img1.asset_id, img2.asset_id]

    # 不存在的素材 -> 404，不建项目
    before = len(store.all("projects"))
    r = client.post("/projects/quick",
                    json={"brief": "x", "mode": "assets",
                          "asset_ids": ["asset_nope"]})
    assert r.status_code == 404
    assert len(store.all("projects")) == before
