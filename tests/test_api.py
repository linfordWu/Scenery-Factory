# SPDX-License-Identifier: GPL-3.0-only
"""Offline end-to-end API test with fully mocked adapters.

The repo root is not a valid Python package name, so it is aliased as the
``svf`` package (matching how apps/api/__main__.py boots the service).
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import types
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if "svf" not in sys.modules:
    pkg = types.ModuleType("svf")
    pkg.__path__ = [str(ROOT)]
    sys.modules["svf"] = pkg

from fastapi.testclient import TestClient  # noqa: E402

from svf.apps.api.app import create_app  # noqa: E402
from svf.domain.repositories.store import Store  # noqa: E402
from svf.domain.schemas.core import Asset, DecisionAdvice, Run, ScoreReport  # noqa: E402


# ------------------------------------------------------------- fake adapters --
class FakeAssetStore:
    """Filesystem AssetStore stand-in (implements adapters.contracts.AssetStore)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save_bytes(self, data, project_id, media_type, filename,
                   source="imported", parent_asset_ids=None):
        key = f"{project_id}/{uuid.uuid4().hex[:8]}_{filename}"
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return Asset(project_id=project_id, source=source, media_type=media_type,
                     sha256=hashlib.sha256(data).hexdigest(), storage_key=key,
                     status="READY", parent_asset_ids=list(parent_asset_ids or []))

    def save_file(self, src_path, project_id, media_type, filename,
                  source="imported", parent_asset_ids=None):
        return self.save_bytes(Path(src_path).read_bytes(), project_id, media_type,
                               filename, source=source,
                               parent_asset_ids=parent_asset_ids)

    def path_for(self, storage_key):
        return str(self.root / storage_key)

    def read_bytes(self, storage_key):
        return (self.root / storage_key).read_bytes()

    def probe(self, storage_key):
        return {"size": (self.root / storage_key).stat().st_size}

    def make_preview(self, asset):
        return ""


class FakeRenderer:
    def __init__(self, asset_store):
        self.asset_store = asset_store
        self.render_calls = 0

    def render_shot(self, prompt_spec, ref_image_keys, seed, duration_s=5,
                    aspect_ratio="9:16", on_progress=None):
        self.render_calls += 1
        if on_progress:
            on_progress(1, 2, "rendering")
            on_progress(2, 2, "done")
        asset = self.asset_store.save_bytes(
            f"fake-video|{prompt_spec}|{seed}".encode(), "proj", "video",
            "render.mp4", source="generated")
        return asset.storage_key

    def normalize_clip(self, source_key, duration_s, aspect_ratio):
        asset = self.asset_store.save_bytes(
            b"normalized|" + self.asset_store.read_bytes(source_key),
            "proj", "video", "normalized.mp4", source="derived")
        return asset.storage_key


class FakeJudge:
    def __init__(self, verdict="accept"):
        self.verdict = verdict

    def score_video(self, video_key, spec):
        return ScoreReport(run_id="?", verdict=self.verdict,
                           scores={"overall": 0.91})


class FakeDecision:
    def advise(self, state, allowed_actions):
        return DecisionAdvice(state_hash="x", allowed_actions=allowed_actions,
                              selected_action=allowed_actions[0], confidence=0.9,
                              accepted_by_policy=True)


class FakeTextModel:
    def chat(self, messages, max_tokens=2048, temperature=0.7):
        return "ok"

    def chat_json(self, instructions, payload, schema_hint, max_tokens=2048):
        if "scenes" in schema_hint:
            return {"scenes": [{"title": "雨夜", "summary": "男女主在雨夜重逢"}]}
        return {"shots": [{"action": "女主撑伞走近", "dialogue": "你还好吗",
                           "duration_s": 5, "aspect_ratio": "9:16",
                           "camera": {"shot_size": "close-up"},
                           "acceptance": {"required": ["雨伞"], "forbidden": []}}]}


# ------------------------------------------------------------------- helpers --
@pytest.fixture()
def env(tmp_path):
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    renderer = FakeRenderer(asset_store)
    app = create_app(store, asset_store, renderer=renderer, judge=FakeJudge(),
                     decision=FakeDecision(), text_model=FakeTextModel(),
                     data_dir=str(tmp_path / "data"), worker_poll_interval_s=0.05)
    client = TestClient(app)
    yield client, store, asset_store, renderer, app
    app.state.engine.shutdown()
    store.close()


def wait_for(cond, timeout=15.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


def flat_shots(detail):
    return [s["shot"] for sc in detail["scenes"] for s in sc["shots"]]


def plan_and_get_shot(client):
    r = client.post("/projects", json={"title": "雨夜重逢", "style": "写实",
                                       "brief": "旧情人雨夜重逢", "duration_target_s": 30})
    assert r.status_code == 201, r.text
    pid = r.json()["project_id"]
    r = client.post(f"/projects/{pid}/plan")
    assert r.status_code == 202, r.text
    assert wait_for(lambda: len(flat_shots(client.get(f"/projects/{pid}").json())) == 1)
    return pid, flat_shots(client.get(f"/projects/{pid}").json())[0]["shot_id"]


def upload_asset(client, pid, filename, data: bytes, chunks=2):
    r = client.post("/assets/uploads", json={
        "project_id": pid, "filename": filename, "total_size": len(data)})
    assert r.status_code == 201, r.text
    uid = r.json()["upload_id"]
    step = (len(data) + chunks - 1) // chunks
    for i in range(chunks):
        part = data[i * step:(i + 1) * step]
        r = client.put(f"/assets/uploads/{uid}/parts/{i}", content=part)
        assert r.status_code == 200, r.text
    # re-send part 0: must be idempotent
    r = client.put(f"/assets/uploads/{uid}/parts/0", content=data[:step])
    assert r.status_code == 200
    sha = hashlib.sha256(data).hexdigest()
    r = client.post(f"/assets/uploads/{uid}/complete", json={"sha256": sha})
    assert r.status_code == 200, r.text
    asset = r.json()
    assert asset["status"] == "READY" and asset["sha256"] == sha
    # complete twice: idempotent, same asset
    r2 = client.post(f"/assets/uploads/{uid}/complete", json={"sha256": sha})
    assert r2.json()["asset_id"] == asset["asset_id"]
    return asset["asset_id"]


# --------------------------------------------------------------------- tests --
def test_full_pipeline(env):
    client, store, asset_store, renderer, app = env
    pid, shot_id = plan_and_get_shot(client)

    asset_id = upload_asset(client, pid, "ref.png", b"fake-image-bytes-" * 100)

    r = client.get("/assets", params={"project_id": pid})
    assert any(a["asset_id"] == asset_id for a in r.json())
    r = client.get(f"/assets/{asset_id}/file")
    assert r.status_code == 200 and r.content == b"fake-image-bytes-" * 100

    r = client.post(f"/shots/{shot_id}/asset-bindings",
                    json={"asset_id": asset_id, "role": "reference"})
    assert r.status_code == 201, r.text

    r = client.post(f"/shots/{shot_id}/runs", json={"command_id": "cmd-1"})
    assert r.status_code == 201, r.text
    run_id = r.json()["run_id"]
    assert r.json()["state"] == "QUEUED"
    # same command_id returns the existing run instead of creating a new one
    r2 = client.post(f"/shots/{shot_id}/runs", json={"command_id": "cmd-1"})
    assert r2.json()["run_id"] == run_id

    assert wait_for(lambda: client.get(f"/runs/{run_id}").json()["run"]["state"]
                    in ("ACCEPTED", "HUMAN_REVIEW", "FAILED", "CANCELLED"))
    detail = client.get(f"/runs/{run_id}").json()
    assert detail["run"]["state"] == "ACCEPTED", detail["run"].get("failure")
    assert renderer.render_calls == 1
    assert detail["score_report"]["verdict"] == "accept"
    assert detail["score_report"]["hard_checks"]["file_readable"] is True

    shot = flat_shots(client.get(f"/projects/{pid}").json())[0]
    assert shot["accepted_run_id"] == run_id

    steps = client.get(f"/runs/{run_id}/steps").json()["steps"]
    types_seen = {s["type"] for s in steps}
    assert {"run.created", "step.queued", "tool.progress"} <= types_seen

    # export: ffmpeg is not installed here -> manifest mode
    r = client.post(f"/projects/{pid}/export")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["clips"] == 1 and body["asset"]["status"] == "READY"
    if body["mode"] == "manifest":
        manifest = json.loads(asset_store.read_bytes(body["asset"]["storage_key"]))
        assert manifest["clips"][0]["run_id"] == run_id


def test_sse_backlog(env):
    """SSE: backlog events are replayed first. TestClient buffers responses
    (starlette 1.7), so the infinite stream is exercised via a real uvicorn
    server on a loopback port (still fully offline)."""
    import socket

    import httpx
    import uvicorn

    client, store, _, _, app = env
    r = client.post("/projects", json={"title": "sse", "brief": "b"})
    pid = r.json()["project_id"]

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        assert wait_for(lambda: server.started, timeout=10)
        got = []
        with httpx.Client(base_url=f"http://127.0.0.1:{port}",
                          timeout=10) as http:
            with http.stream("GET", f"/projects/{pid}/events?seq=0") as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                for line in resp.iter_lines():
                    if line.startswith("data:"):
                        got.append(json.loads(line[len("data:"):]))
                        break
        assert got and got[0]["type"] == "project.created"
        assert got[0]["seq"] >= 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)


class BlockingRenderer(FakeRenderer):
    """Render blocks until released, so cancel lands mid-render."""

    def __init__(self, asset_store):
        super().__init__(asset_store)
        self.started = threading.Event()
        self.release = threading.Event()

    def render_shot(self, prompt_spec, ref_image_keys, seed, duration_s=5,
                    aspect_ratio="9:16", on_progress=None):
        self.render_calls += 1
        if on_progress:
            on_progress(1, 2, "rendering")
        self.started.set()
        assert self.release.wait(timeout=10)
        return super().render_shot(prompt_spec, ref_image_keys, seed,
                                   duration_s, aspect_ratio, on_progress)


def test_cancel_rejects_late_result(tmp_path):
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    renderer = BlockingRenderer(asset_store)
    app = create_app(store, asset_store, renderer=renderer, judge=FakeJudge(),
                     decision=None, text_model=FakeTextModel(),
                     data_dir=str(tmp_path / "data"), worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        r = client.post("/runs/run_nope/commands",
                        json={"action": "pause", "command_id": "c0"})
        assert r.status_code == 404

        pid, shot_id = plan_and_get_shot(client)
        r = client.post(f"/shots/{shot_id}/runs", json={"command_id": "cmd-cancel"})
        run_id = r.json()["run_id"]
        assert renderer.started.wait(timeout=10)   # run is RENDERING now

        # command_id dedupe
        client.post(f"/runs/{run_id}/commands", json={"action": "pause", "command_id": "c1"})
        dup = client.post(f"/runs/{run_id}/commands",
                          json={"action": "pause", "command_id": "c1"})
        assert dup.json()["status"] == "duplicate"
        client.post(f"/runs/{run_id}/commands", json={"action": "resume", "command_id": "c2"})

        # cancel mid-render, then let the renderer finish late
        r = client.post(f"/runs/{run_id}/commands",
                        json={"action": "cancel", "command_id": "c3"})
        assert r.status_code == 200, r.text
        assert r.json()["run"]["state"] == "CANCEL_REQUESTED"
        renderer.release.set()
        assert wait_for(lambda: client.get(f"/runs/{run_id}").json()["run"]["state"]
                        == "CANCELLED")
        run = client.get(f"/runs/{run_id}").json()["run"]
        assert run["candidate_asset_ids"] == []    # late product discarded
        # terminal state rejects further cancels
        r = client.post(f"/runs/{run_id}/commands",
                        json={"action": "cancel", "command_id": "c4"})
        assert r.status_code == 409
    finally:
        renderer.release.set()
        app.state.engine.shutdown()
        store.close()


def test_human_review_accept(tmp_path):
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    renderer = FakeRenderer(asset_store)
    app = create_app(store, asset_store, renderer=renderer,
                     judge=FakeJudge(verdict="uncertain"), decision=None,
                     text_model=FakeTextModel(), data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        pid, shot_id = plan_and_get_shot(client)
        r = client.post(f"/shots/{shot_id}/runs", json={"command_id": "cmd-hr"})
        run_id = r.json()["run_id"]
        assert wait_for(lambda: client.get(f"/runs/{run_id}").json()["run"]["state"]
                        == "HUMAN_REVIEW")
        r = client.post(f"/runs/{run_id}/review",
                        json={"decision": "accept", "note": "ok"})
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "ACCEPTED"
        shot = flat_shots(client.get(f"/projects/{pid}").json())[0]
        assert shot["accepted_run_id"] == run_id
    finally:
        app.state.engine.shutdown()
        store.close()


def test_upload_sha_mismatch_fails(env):
    client, store, _, _, _ = env
    r = client.post("/projects", json={"title": "bad", "brief": "b"})
    pid = r.json()["project_id"]
    data = b"some-bytes"
    uid = client.post("/assets/uploads", json={
        "project_id": pid, "filename": "x.mp4", "total_size": len(data)}).json()["upload_id"]
    client.put(f"/assets/uploads/{uid}/parts/0", content=data)
    r = client.post(f"/assets/uploads/{uid}/complete", json={"sha256": "0" * 64})
    assert r.status_code == 400
    session = store.get("uploads", uid)
    assert session.status == "FAILED"


def test_stop_project_cancels_active_runs(tmp_path):
    """项目级停止:在途 run 取消;列表暴露 stoppable 供前端显示停止入口。"""
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    renderer = BlockingRenderer(asset_store)
    app = create_app(store, asset_store, renderer=renderer, judge=FakeJudge(),
                     decision=None, text_model=FakeTextModel(),
                     data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        pid, shot_id = plan_and_get_shot(client)
        rid = client.post(f"/shots/{shot_id}/runs",
                          json={"command_id": "cmd-stop"}).json()["run_id"]
        assert renderer.started.wait(timeout=10)     # run 正在渲染
        proj = next(p for p in client.get("/projects").json()["projects"]
                    if p["project_id"] == pid)
        assert proj["stoppable"] is True and proj["active_runs"] >= 1
        r = client.post(f"/projects/{pid}/stop")
        assert r.status_code == 202, r.text
        renderer.release.set()
        assert wait_for(lambda: client.get(f"/runs/{rid}").json()["run"]["state"]
                        == "CANCELLED", timeout=15)
        proj = next(p for p in client.get("/projects").json()["projects"]
                    if p["project_id"] == pid)
        assert proj["stoppable"] is False
        types = [e["type"] for e in
                 client.get(f"/runs/{rid}/steps").json()["steps"]]
        assert "run.cancel_requested" in types
    finally:
        renderer.release.set()
        app.state.engine.shutdown()
        store.close()


def test_tasks_center_and_pause(tmp_path):
    """任务中心:阶段/进度/明细可读;暂停与恢复可用。"""
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    renderer = BlockingRenderer(asset_store)
    app = create_app(store, asset_store, renderer=renderer, judge=FakeJudge(),
                     decision=None, text_model=FakeTextModel(),
                     data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        pid, shot_id = plan_and_get_shot(client)
        rid = client.post(f"/shots/{shot_id}/runs",
                          json={"command_id": "cmd-tasks"}).json()["run_id"]
        assert renderer.started.wait(timeout=10)     # run 正在渲染
        data = client.get("/tasks").json()
        proj = next(p for p in data["projects"] if p["project_id"] == pid)
        assert proj["stage"] == "rendering" and proj["stoppable"] is True
        assert proj["counts"]["shots"] == 1
        assert proj["current"]["run_id"] == rid
        assert proj["current"]["elapsed_s"] >= 0
        assert any(r["run_id"] == rid and r["cancellable"] for r in proj["runs"])
        assert data["summary"]["active"] >= 1
        # 暂停 / 恢复
        assert client.post("/engine/pause").json()["paused"] is True
        assert client.get("/tasks").json()["summary"]["paused"] is True
        assert client.post("/engine/resume").json()["paused"] is False
        assert client.get("/tasks").json()["summary"]["paused"] is False
        renderer.release.set()
        assert wait_for(lambda: client.get(f"/runs/{rid}").json()["run"]["state"]
                        == "ACCEPTED", timeout=15)
        proj = next(p for p in client.get("/tasks").json()["projects"]
                    if p["project_id"] == pid)
        assert proj["stage"] == "done" and proj["progress"] == 1.0
    finally:
        renderer.release.set()
        app.state.engine.shutdown()
        store.close()


# ------------------------------------------------- repair from ASSET_READY --
class RepairingJudge:
    """第一次评审要求修复(失效点 ASSET_READY),之后接受。"""

    def __init__(self):
        self.calls = 0

    def score_video(self, video_key, spec):
        self.calls += 1
        if self.calls == 1:
            return ScoreReport(run_id="?", verdict="repair",
                               scores={"overall": 0.4})
        return ScoreReport(run_id="?", verdict="accept",
                           scores={"overall": 0.9})


class RepairTextModel(FakeTextModel):
    """修复 agent 返回 invalidate_from=ASSET_READY(生成模式最容易卡死的点)。"""

    def chat_json(self, instructions, payload, schema_hint, max_tokens=2048):
        if "invalidate_from" in schema_hint:
            return {"target": "reference_assets", "action": "regenerate_video",
                    "detail": "资产级修复", "invalidate_from": "ASSET_READY"}
        return super().chat_json(instructions, payload, schema_hint, max_tokens)


def test_repair_from_asset_ready_requeues(tmp_path):
    """修复失效点=ASSET_READY 的生成 run 不能卡死:自动续到 QUEUED 并重渲染。"""
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    renderer = FakeRenderer(asset_store)
    app = create_app(store, asset_store, renderer=renderer,
                     judge=RepairingJudge(), decision=None,
                     text_model=RepairTextModel(),
                     data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        pid, shot_id = plan_and_get_shot(client)
        r = client.post(f"/shots/{shot_id}/runs", json={"command_id": "cmd-repair"})
        rid = r.json()["run_id"]
        assert wait_for(lambda: client.get(f"/runs/{rid}").json()["run"]["state"]
                        == "ACCEPTED", timeout=30)
        assert renderer.render_calls == 2          # 修复后确实重渲染一次
        final = client.get(f"/runs/{rid}").json()["run"]
        assert final["seed"] != 2026               # 修复换了种子,避免缓存出同一条
        types = [e["type"] for e
                 in client.get(f"/runs/{rid}/steps").json()["steps"]]
        assert types.count("run.repairing") == 1
        assert types.count("step.queued") >= 2
    finally:
        app.state.engine.shutdown()
        store.close()


# ------------------------------------------ 删除项目/启动自愈:不留僵尸任务 --
class InterruptRecordingRenderer(BlockingRenderer):
    """记录 interrupt 调用,并让阻塞中的渲染立即结束(模拟 ComfyUI interrupt)。"""

    def __init__(self, asset_store):
        super().__init__(asset_store)
        self.interrupted = threading.Event()

    def interrupt(self):
        self.interrupted.set()
        self.release.set()


def test_delete_project_stops_active_runs(tmp_path):
    """删除项目:取消在途 run + 打断渲染,不留继续吃 GPU 的僵尸任务。"""
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    renderer = InterruptRecordingRenderer(asset_store)
    app = create_app(store, asset_store, renderer=renderer, judge=FakeJudge(),
                     decision=None, text_model=FakeTextModel(),
                     data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        pid, shot_id = plan_and_get_shot(client)
        rid = client.post(f"/shots/{shot_id}/runs",
                          json={"command_id": "cmd-del"}).json()["run_id"]
        assert renderer.started.wait(timeout=10)     # run 正在渲染
        r = client.delete(f"/projects/{pid}")
        assert r.status_code == 200, r.text
        assert r.json()["removed"]["cancelled_runs"] == [rid]
        assert renderer.interrupted.wait(timeout=5)  # 渲染被主动打断
        assert store.get("projects", pid) is None
        # run 记录已随项目删除;引擎不会把它复活
        assert wait_for(lambda: store.get("runs", rid) is None, timeout=10)
        time.sleep(0.3)
        assert store.get("runs", rid) is None
    finally:
        renderer.release.set()
        app.state.engine.shutdown()
        store.close()


def test_startup_cancels_orphan_runs(tmp_path):
    """启动自愈:项目已删除但 run 还活着的僵尸任务会被取消。"""
    db = tmp_path / "factory.db"
    store = Store(db)
    orphan = Run(shot_id="shot_gone", project_id="p_gone", state="QUEUED")
    store.put("runs", orphan)
    store.close()

    store = Store(db)
    asset_store = FakeAssetStore(tmp_path / "assets")
    app = create_app(store, asset_store, renderer=FakeRenderer(asset_store),
                     judge=FakeJudge(), text_model=None,
                     data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        assert wait_for(lambda: store.get("runs", orphan.run_id)
                        and store.get("runs", orphan.run_id).state == "CANCELLED",
                        timeout=15)
    finally:
        app.state.engine.shutdown()
        store.close()


def test_assets_gallery_classifies_stages(tmp_path):
    """资产画廊:按生产结构归档(材料/定妆/场景/首帧/片段/成片),孤儿资产单列。"""
    from svf.domain.schemas.core import (Asset, AssetBinding, Character, Project,
                                         Run, Scene, Shot, ShotSpec)
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    app = create_app(store, asset_store, renderer=FakeRenderer(asset_store),
                     judge=FakeJudge(), text_model=None,
                     data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        project = Project(title="资产测试", brief="b", duration_target_s=12)
        store.put("projects", project)
        scene = Scene(scene_id="sc1", project_id=project.project_id, order=0,
                      title="场景")
        store.put("scenes", scene)
        shot = Shot(shot_id="sh1", scene_id="sc1",
                    project_id=project.project_id, order=0,
                    spec=ShotSpec(shot_id="sh1", action="走近"))
        store.put("shots", shot)
        store.put("characters", Character(project_id=project.project_id, name="小夏",
                                          kind="character", asset_id="a_portrait"))
        store.put("characters", Character(project_id=project.project_id, name="便利店",
                                          kind="location", asset_id="a_loc"))

        def put_asset(aid, media, source, category="", metadata=None, pid=None):
            store.put("assets", Asset(
                asset_id=aid, project_id=pid or project.project_id,
                source=source, media_type=media, category=category,
                storage_key=f"{pid or project.project_id}/{aid}/f.bin",
                metadata=metadata or {}, status="READY"))

        put_asset("a_portrait", "image", "generated", "character")
        put_asset("a_loc", "image", "generated", "location")
        put_asset("a_ff", "image", "generated", "location")
        store.put("bindings", AssetBinding(shot_id="sh1", asset_id="a_ff",
                                           asset_version=1, role="first_frame"))
        put_asset("a_text", "text", "imported", "other",
                  {"original_filename": "剧本.md", "size_bytes": 123})
        run = Run(shot_id="sh1", project_id=project.project_id, state="ACCEPTED",
                  candidate_asset_ids=["a_clip"], seed=2026)
        store.put("runs", run)
        shot.accepted_run_id = run.run_id
        store.put("shots", shot)
        put_asset("a_clip", "video", "generated", "footage", {"size": 456})
        put_asset("a_export", "video", "derived", "export", {"size": 789})
        put_asset("a_orphan", "video", "generated", "", pid="p_gone")

        data = client.get("/assets/gallery").json()
        proj = next(p for p in data["projects"]
                    if p["project_id"] == project.project_id)
        g = proj["groups"]
        assert [a["asset_id"] for a in g["portrait"]] == ["a_portrait"]
        assert [a["asset_id"] for a in g["location"]] == ["a_loc"]
        assert g["first_frame"][0]["asset_id"] == "a_ff"
        assert g["first_frame"][0]["shot_no"] == 1
        clip = g["clip"][0]
        assert (clip["shot_no"], clip["take"], clip["accepted"]) == (1, 1, True)
        assert clip["run_state"] == "ACCEPTED" and clip["seed"] == 2026
        assert [a["asset_id"] for a in g["export"]] == ["a_export"]
        assert g["material"][0]["filename"] == "剧本.md"
        assert any(a["asset_id"] == "a_orphan" for a in data["orphans"])
        # 单项目过滤:只返回该项目,不含孤儿
        only = client.get(
            f"/assets/gallery?project_id={project.project_id}").json()
        assert len(only["projects"]) == 1 and not only["orphans"]
    finally:
        app.state.engine.shutdown()
        store.close()
