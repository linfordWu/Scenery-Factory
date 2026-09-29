# SPDX-License-Identifier: GPL-3.0-only
"""画幅锁与音频提示词测试：项目级 aspect_ratio 贯穿建项/手动补镜/导演分镜,
导出时分辨率不一致的片段统一重编码;_compose_prompt 音频段使用 H3
原生结构化语法(<d>[Chinese] 对白 / 正向 soundscape 描述)。
全部 mock,不碰真实 LLM/渲染/ffmpeg。
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

from svf.agents.director import DirectorAgent  # noqa: E402
from svf.apps.api.app import (  # noqa: E402
    _compose_prompt, _unify_export_clips, create_app,
)
from svf.domain.repositories.store import Store  # noqa: E402
from svf.domain.schemas.core import Project, Shot, ShotSpec  # noqa: E402

from tests.test_api import (  # noqa: E402
    FakeAssetStore, FakeJudge, FakeTextModel, wait_for,
)


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


# ------------------------------------------------------------- 项目画幅锁 ----
def test_project_aspect_ratio_persisted(env):
    client, store, _, _ = env
    r = client.post("/projects", json={"title": "t", "brief": "b",
                                       "aspect_ratio": "9:16"})
    assert r.status_code == 201, r.text
    pid = r.json()["project_id"]
    assert r.json()["aspect_ratio"] == "9:16"
    detail = client.get(f"/projects/{pid}").json()
    assert detail["project"]["aspect_ratio"] == "9:16"
    # 缺省 16:9
    r2 = client.post("/projects", json={"title": "t2", "brief": "b"})
    assert r2.json()["aspect_ratio"] == "16:9"


def test_quick_project_aspect_ratio_persisted(env):
    client, store, _, _ = env
    r = client.post("/projects/quick", json={"brief": "竖屏短剧",
                                             "aspect_ratio": "9:16"})
    assert r.status_code == 201, r.text
    pid = r.json()["project_id"]
    assert store.get("projects", pid).aspect_ratio == "9:16"
    # 等后台编排线程结束,避免 teardown 关库后线程访问 sqlite
    assert wait_for(lambda: any(
        e.type in ("quick.completed", "quick.failed",
                   "plan.completed", "plan.failed")
        for e in store.events_since(pid)), timeout=30)


def test_manual_shot_inherits_project_aspect(env):
    client, store, _, _ = env
    r = client.post("/projects", json={"title": "t", "brief": "b",
                                       "aspect_ratio": "9:16"})
    pid = r.json()["project_id"]
    r = client.post(f"/projects/{pid}/shots",
                    json={"action": "女孩推开窗", "duration_s": 5})
    assert r.status_code == 201, r.text
    assert r.json()["spec"]["aspect_ratio"] == "9:16"


def test_director_ignores_llm_aspect_ratio():
    class LLM:
        def chat_json(self, instructions, payload, schema_hint, max_tokens=4096):
            return {"shots": [{"action": "a", "duration_s": 5,
                               "aspect_ratio": "9:16"}]}

    specs = DirectorAgent(LLM()).storyboard("剧本", [], duration_s=10,
                                            aspect_ratio="16:9")
    assert specs and all(s.aspect_ratio == "16:9" for s in specs)


# --------------------------------------------------------------- 音频提示词 --
def _shot(dialogue="", characters=None):
    return Shot(shot_id="s", scene_id="sc", project_id="p",
                spec=ShotSpec(shot_id="s", action="女孩切菜",
                              dialogue=dialogue,
                              characters=characters or []))


def test_compose_prompt_dialogue_uses_h3_d_tag():
    prompt = _compose_prompt(
        Project(title="t", style="手绘"),
        _shot(dialogue="早餐做好了。",
              characters=[{"name": "女孩", "kind": "character"},
                          {"name": "厨房", "kind": "location"}]))
    assert "<d>[Chinese] 早餐做好了。</d>" in prompt
    assert "女孩用中文普通话清晰地说" in prompt
    assert "台词:" not in prompt


def test_dialogue_quotes_stripped():
    from svf.apps.api.app import _clean_dialogue, build_srt
    assert _clean_dialogue('“谢谢您，外面雨太大了”') == "谢谢您，外面雨太大了"
    assert _clean_dialogue('"慢点喝，别烫着".') == "慢点喝，别烫着."
    assert _clean_dialogue("") == ""
    srt = build_srt([{"dialogue": "“你好”", "duration_s": 4}])
    assert "“" not in srt and "你好" in srt
    prompt = _compose_prompt(None, _shot(dialogue='"明天见"'))
    assert "<d>[Chinese] 明天见</d>" in prompt


def test_compose_prompt_dialogue_speaker_fallback():
    prompt = _compose_prompt(None, _shot(dialogue="你好。"))
    assert "角色用中文普通话清晰地说" in prompt


def test_compose_prompt_no_dialogue_positive_soundscape():
    prompt = _compose_prompt(None, _shot())
    assert "overall_soundscape" in prompt
    assert "No voice, no speech" in prompt
    assert "non_diegetic_music: None." in prompt
    assert "呢喃" not in prompt


# ------------------------------------------------------------- 导出统一分辨率 --
def test_unify_export_clips_reencodes_mismatched(tmp_path, monkeypatch):
    project = Project(title="t", aspect_ratio="16:9")
    clips = [{"shot_id": "s0", "storage_key": "p/a/ok.mp4"},
             {"shot_id": "s1", "storage_key": "p/a/vertical.mp4"}]
    sizes = {"p/a/ok.mp4": (1920, 1080), "p/a/vertical.mp4": (1024, 1792)}
    monkeypatch.setattr("svf.workers.compose.probe_video_size",
                        lambda ffprobe, path: sizes[path])
    calls: list = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"reencoded")
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("svf.workers.compose.subprocess.run", fake_run)
    out = _unify_export_clips("/usr/bin/ffmpeg", project, clips, tmp_path,
                              path_for=lambda key: key)
    assert "_path" not in out[0]                       # 符合目标的不动
    assert out[1]["_path"].endswith("unified_01.mp4")  # 竖屏片段被重编码
    cmd = calls[0]
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=1920:1080" in vf and "pad=1920:1080" in vf
