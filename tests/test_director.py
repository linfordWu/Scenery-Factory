# SPDX-License-Identifier: GPL-3.0-only
"""导演台后端测试：角色/地点登记、shot 角色绑定、asset-bindings 同步、
导出字幕烧录路径、director-review 审核接口（全部 mock，离线）。

复用 test_api 的 fake adapters（svf 包别名方式与之一致）。
"""
from __future__ import annotations

import json
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

from svf.agents.reviewer import ReviewerAgent  # noqa: E402
from svf.apps.api.app import _compose_prompt, build_srt, create_app  # noqa: E402
from svf.domain.repositories.store import Store  # noqa: E402
from svf.domain.schemas.core import (  # noqa: E402
    Project, ReviewIssue, Run, Scene, Shot, ShotSpec, new_id,
)

from tests.test_api import FakeAssetStore, FakeJudge, FakeTextModel, wait_for  # noqa: E402


# ------------------------------------------------------------------- helpers --
@pytest.fixture()
def env(tmp_path):
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    app = create_app(store, asset_store, renderer=None, judge=FakeJudge(),
                     decision=None, text_model=FakeTextModel(),
                     data_dir=str(tmp_path / "data"), start_worker=False)
    client = TestClient(app)
    yield client, store, asset_store, app
    app.state.engine.shutdown()
    store.close()


def make_project(client) -> str:
    r = client.post("/projects", json={"title": "雨夜重逢", "style": "写实",
                                       "brief": "旧情人雨夜重逢"})
    assert r.status_code == 201, r.text
    return r.json()["project_id"]


def make_shot(store, pid: str, dialogue: str = "", duration_s: int = 5,
              order: int = 0) -> Shot:
    if store.get("scenes", "sc_1") is None:
        store.put("scenes", Scene(scene_id="sc_1", project_id=pid, order=0,
                                  title="雨夜", summary="重逢"))
    shot_id = new_id("shot")
    shot = Shot(shot_id=shot_id, scene_id="sc_1", project_id=pid, order=order,
                spec=ShotSpec(shot_id=shot_id, action="女主撑伞走近",
                              dialogue=dialogue, duration_s=duration_s))
    store.put("shots", shot)
    return shot


def accept_shot(store, asset_store, pid: str, shot: Shot) -> Run:
    video = asset_store.save_bytes(b"fake-video-bytes", pid, "video",
                                   "clip.mp4", source="generated")
    store.put("assets", video)
    run = Run(shot_id=shot.shot_id, project_id=pid, state="ACCEPTED",
              candidate_asset_ids=[video.asset_id])
    store.put("runs", run)
    shot.accepted_run_id = run.run_id
    store.put("shots", shot)
    return run


def make_image_asset(store, asset_store, pid: str):
    asset = asset_store.save_bytes(b"\x89PNG\r\n\x1a\nfake", pid, "image",
                                   "ref.png", source="imported")
    store.put("assets", asset)
    return asset


def fake_ffmpeg_run(calls: list):
    """假装 ffmpeg 成功：创建输出文件（命令最后一个参数）。"""
    def _run(cmd, **kw):
        calls.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"ffmpeg-output")
        return types.SimpleNamespace(returncode=0, stderr="")
    return _run


# --------------------------------------------------------------- characters --
def test_characters_crud(env):
    client, store, _, _ = env
    pid = make_project(client)

    r = client.post(f"/projects/{pid}/characters",
                    json={"name": "艾米", "kind": "character",
                          "description": "20岁女孩,及肩黑发,米色毛衣"})
    assert r.status_code == 201, r.text
    ch = r.json()
    assert ch["character_id"].startswith("ch_") and ch["project_id"] == pid
    assert ch["kind"] == "character" and ch["asset_id"] == ""

    r = client.post(f"/projects/{pid}/characters",
                    json={"name": "咖啡馆", "kind": "location",
                          "description": "暖色调,木质吧台"})
    assert r.status_code == 201, r.text
    loc_id = r.json()["character_id"]

    r = client.get(f"/projects/{pid}/characters")
    assert r.status_code == 200
    names = [c["name"] for c in r.json()["characters"]]
    assert names == ["艾米", "咖啡馆"]          # 按 created_at 排序

    r = client.delete(f"/characters/{loc_id}")
    assert r.status_code == 200 and r.json()["status"] == "deleted"
    assert [c["name"] for c in
            client.get(f"/projects/{pid}/characters").json()["characters"]] == ["艾米"]
    assert client.delete(f"/characters/{loc_id}").status_code == 404
    # 无效 kind 被 pydantic 拒绝
    assert client.post(f"/projects/{pid}/characters",
                       json={"name": "x", "kind": "robot"}).status_code == 422


def test_shot_characters_binding(env):
    client, store, asset_store, _ = env
    pid = make_project(client)
    shot = make_shot(store, pid)
    img = make_image_asset(store, asset_store, pid)

    c1 = client.post(f"/projects/{pid}/characters",
                     json={"name": "艾米", "kind": "character",
                           "description": "20岁女孩", "asset_id": img.asset_id}
                     ).json()
    c2 = client.post(f"/projects/{pid}/characters",
                     json={"name": "咖啡馆", "kind": "location",
                           "description": "暖色调"}).json()

    # 预置一个已有参考图 + 重复的角色参考图：角色图应去重并放最前
    shot.spec.reference_assets = ["asset_other", img.asset_id]
    store.put("shots", shot)

    r = client.post(f"/shots/{shot.shot_id}/characters",
                    json={"character_ids": [c1["character_id"], c2["character_id"]]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["spec"]["characters"] == [
        {"character_id": c1["character_id"], "name": "艾米",
         "kind": "character", "description": "20岁女孩"},
        {"character_id": c2["character_id"], "name": "咖啡馆",
         "kind": "location", "description": "暖色调"}]
    assert body["spec"]["reference_assets"] == [img.asset_id, "asset_other"]
    # 不存在角色 -> 404
    assert client.post(f"/shots/{shot.shot_id}/characters",
                       json={"character_ids": ["ch_nope"]}).status_code == 404


def test_compose_prompt_with_characters(env):
    client, store, _, _ = env
    pid = make_project(client)
    shot = make_shot(store, pid, dialogue="你还好吗")
    shot.spec.characters = [{"character_id": "ch_1", "name": "艾米",
                             "kind": "character",
                             "description": "20岁女孩,及肩黑发"}]
    store.put("shots", shot)
    project = store.get("projects", pid)
    prompt = _compose_prompt(project, shot)
    assert "角色锁定（逐字保持）: 艾米: 20岁女孩,及肩黑发" in prompt
    assert "<d>[Chinese] 你还好吗</d>" in prompt
    assert prompt.endswith("画面中不出现任何文字、字幕、水印、logo、标识")


# ------------------------------------------------------------ asset bindings --
def test_asset_binding_syncs_reference_assets(env):
    client, store, asset_store, _ = env
    pid = make_project(client)
    shot = make_shot(store, pid)
    img = make_image_asset(store, asset_store, pid)

    r = client.post(f"/shots/{shot.shot_id}/asset-bindings",
                    json={"asset_id": img.asset_id, "role": "reference"})
    assert r.status_code == 201, r.text
    assert store.get("shots", shot.shot_id).spec.reference_assets == [img.asset_id]

    # 幂等重复绑定不产生重复 reference
    r = client.post(f"/shots/{shot.shot_id}/asset-bindings",
                    json={"asset_id": img.asset_id, "role": "reference"})
    assert r.status_code == 201
    assert store.get("shots", shot.shot_id).spec.reference_assets == [img.asset_id]

    # 非参考类 role 不同步
    audio = asset_store.save_bytes(b"mp3", pid, "audio", "bgm.mp3",
                                   source="imported")
    store.put("assets", audio)
    r = client.post(f"/shots/{shot.shot_id}/asset-bindings",
                    json={"asset_id": audio.asset_id, "role": "audio"})
    assert r.status_code == 201
    assert store.get("shots", shot.shot_id).spec.reference_assets == [img.asset_id]


# ----------------------------------------------------------------- export ----
def test_build_srt():
    clips = [{"dialogue": "你还好吗", "duration_s": 4},
             {"dialogue": "  ", "duration_s": 6},
             {"dialogue": "好久不见", "duration_s": 2}]
    srt = build_srt(clips)
    assert "00:00:00,000 --> 00:00:04,000" in srt
    assert "00:00:10,000 --> 00:00:12,000" in srt
    assert "你还好吗" in srt and "好久不见" in srt
    assert srt.count("-->") == 2


def test_export_concat_subtitles(env, monkeypatch):
    client, store, asset_store, _ = env
    pid = make_project(client)
    accept_shot(store, asset_store, pid, make_shot(store, pid, "你还好吗", 4, 0))
    accept_shot(store, asset_store, pid, make_shot(store, pid, "", 6, 1))

    calls: list = []
    monkeypatch.setattr("svf.workers.compose.subprocess.run", fake_ffmpeg_run(calls))
    monkeypatch.setattr("svf.workers.compose.shutil.which", lambda name: "/usr/bin/ffmpeg")

    r = client.post(f"/projects/{pid}/export", json={"subtitles": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "concat_subtitles" and body["clips"] == 2
    assert body["asset"]["status"] == "READY"
    # calls 里可能先有统一分辨率的 ffprobe/ffmpeg 探测,取真正的 concat 命令
    cmd = next(c for c in calls if "concat" in c)
    assert "libx264" in cmd and "fast" in cmd and "18" in cmd
    assert any(a.startswith("subtitles=") for a in cmd)


def test_export_concat_fast_path(env, monkeypatch):
    client, store, asset_store, _ = env
    pid = make_project(client)
    accept_shot(store, asset_store, pid, make_shot(store, pid, "", 5, 0))

    calls: list = []
    monkeypatch.setattr("svf.workers.compose.subprocess.run", fake_ffmpeg_run(calls))
    monkeypatch.setattr("svf.workers.compose.shutil.which", lambda name: "/usr/bin/ffmpeg")

    # 无台词：默认 subtitles=True 也走 -c copy 快路径；不传 body 也行
    r = client.post(f"/projects/{pid}/export")
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "concat"
    # calls 里可能先有统一分辨率的 ffprobe/ffmpeg 探测,取真正的 concat 命令
    cmd = next(c for c in calls if "concat" in c)
    assert cmd[cmd.index("-c") + 1] == "copy"


# --------------------------------------------------------- director review ---
class FakeReviewer:
    """Mock ReviewerAgent：直接写一份报告。"""

    def __init__(self, store):
        self.store = store
        self.calls: list[str] = []

    def review(self, project):
        from svf.domain.schemas.core import DirectorReview
        self.calls.append(project.project_id)
        self.store.put("director_reviews", DirectorReview(
            project_id=project.project_id, story_coherence=0.8, summary="整体连贯",
            issues=[ReviewIssue(shot_id="s1", run_id="r1", type="text",
                                severity="high", detail="画面出现乱码文字")]))


def test_director_review_api(tmp_path):
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    reviewer = FakeReviewer(store)
    app = create_app(store, asset_store, text_model=FakeTextModel(),
                     data_dir=str(tmp_path / "data"), start_worker=False,
                     reviewer=reviewer)
    client = TestClient(app)
    try:
        pid = make_project(client)
        assert client.get(f"/projects/{pid}/director-review").status_code == 404

        r = client.post(f"/projects/{pid}/director-review")
        assert r.status_code == 202 and r.json() == {"status": "reviewing"}
        assert wait_for(lambda: bool(reviewer.calls), timeout=10)

        r = client.get(f"/projects/{pid}/director-review")
        assert r.status_code == 200, r.text
        report = r.json()["report"]
        assert report["project_id"] == pid
        assert report["story_coherence"] == 0.8
        assert report["issues"][0]["type"] == "text"
        assert report["issues"][0]["severity"] == "high"
    finally:
        app.state.engine.shutdown()
        store.close()


class FakeVisionClient:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list = []

    def chat(self, messages, max_tokens=2048, temperature=0.7):
        self.calls.append(messages)
        return self.reply


def test_reviewer_agent(env, monkeypatch, tmp_path):
    client, store, asset_store, _ = env
    pid = make_project(client)
    client.post(f"/projects/{pid}/characters",
                json={"name": "艾米", "kind": "character", "description": "20岁女孩"})
    shot = make_shot(store, pid, "你还好吗")
    run = accept_shot(store, asset_store, pid, shot)
    project = store.get("projects", pid)

    reply = json.dumps({"story_coherence": 0.7, "summary": "基本连贯",
                        "issues": [{"shot_id": shot.shot_id, "type": "character",
                                    "severity": "mid", "detail": "发色不一致"}]},
                       ensure_ascii=False)
    vision = FakeVisionClient(reply)
    agent = ReviewerAgent(store, asset_store, client=vision, batch_size=4)

    # 不依赖宿主机 ffmpeg：抽帧换成返回一张假 png
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    monkeypatch.setattr(ReviewerAgent, "_extract_middle_frame",
                        lambda self, vp, td, name: frame)

    report = agent.review(project)
    assert report.story_coherence == 0.7 and report.summary == "基本连贯"
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.shot_id == shot.shot_id and issue.run_id == run.run_id
    assert issue.type == "character" and issue.severity == "mid"
    assert store.get("director_reviews", report.report_id) is not None
    # 视觉模型请求带上了图片和角色登记
    content = vision.calls[0][0]["content"]
    assert any(c["type"] == "image_url" for c in content)
    assert "艾米" in content[0]["text"]


def test_reviewer_agent_parse_failure(env, monkeypatch, tmp_path):
    client, store, asset_store, _ = env
    pid = make_project(client)
    accept_shot(store, asset_store, pid, make_shot(store, pid))
    project = store.get("projects", pid)

    agent = ReviewerAgent(store, asset_store,
                          client=FakeVisionClient("这不是JSON"), batch_size=4)
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    monkeypatch.setattr(ReviewerAgent, "_extract_middle_frame",
                        lambda self, vp, td, name: frame)

    report = agent.review(project)
    assert report.issues == [] and report.story_coherence == 0.0
    assert "这不是JSON" in report.raw          # 解析失败容错：留 raw
    assert store.get("director_reviews", report.report_id) is not None
