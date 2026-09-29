# SPDX-License-Identifier: GPL-3.0-only
"""对话出片测试:assistant agent 的多轮收集/proposal 钳制/降级,
POST /assistant/chat 端点,proposal 自动出片走 /projects/quick。
全部 mock,不碰真实 LLM/渲染。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if "svf" not in sys.modules:
    pkg = types.ModuleType("svf")
    pkg.__path__ = [str(ROOT)]
    sys.modules["svf"] = pkg

from fastapi.testclient import TestClient  # noqa: E402

from svf.agents.assistant import _clamp_proposal, run_assistant_chat  # noqa: E402
from svf.agents.planner import normalize_plan, run_assistant_plan  # noqa: E402
from svf.apps.api.app import create_app  # noqa: E402
from svf.domain.repositories.store import Store  # noqa: E402
from svf.domain.video_constraints import plan_limits  # noqa: E402

from tests.test_api import (  # noqa: E402
    FakeAssetStore, FakeJudge, FakeRenderer, wait_for,
)


# ----------------------------------------------------------------- fake LLM --
class ScriptChatModel:
    """按预设剧本回复的 text_model(chat / chat_json 共用一条队列)。"""

    def __init__(self, replies: list):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []
        self.json_calls: list[dict] = []

    def _next(self):
        return self.replies.pop(0) if self.replies else None

    def chat(self, messages, max_tokens=2048, temperature=0.7):
        self.calls.append(messages)
        item = self._next()
        if isinstance(item, str):
            return item
        return json.dumps(item or {"reply": "嗯?"}, ensure_ascii=False)

    def chat_json(self, instructions, payload, schema_hint, max_tokens=2048):
        self.json_calls.append({"instructions": instructions, "payload": payload,
                                "schema_hint": schema_hint})
        item = self._next()
        if isinstance(item, str):
            return json.loads(item)
        return item or {}


class FakeImageModel:
    """图片服务离线的确定性替身(定妆/首帧步骤直接跳过)。"""

    def available(self):
        return False


# ------------------------------------------------------------- agent 层 ------
_STRUCTURED_PROPOSAL = {
    "title": "雨夜便利店", "brief": "雨夜便利店,店员在监控里看到另一个自己",
    "style": "写实电影感", "duration_target_s": 12, "aspect_ratio": "9:16",
}

_WRITER_OUT = {
    "scenes": [
        {"title": "监控异常", "summary": "小林摆好牛奶,监控屏幕闪过陌生身影"},
        {"title": "确认身影", "summary": "小林回看监控,发现身影站在自己身后"},
    ],
    "characters": [
        {"name": "小林", "kind": "character",
         "description": "小林:20岁女孩,黑色短发,蓝色便利店围裙"},
        {"name": "便利店", "kind": "location",
         "description": "便利店:雨夜,冷白顶灯,货架+收银台+玻璃门"},
    ],
}

_DIRECTOR_OUT = {
    "scenes": [
        {"title": "监控异常", "shots": [
            {"action": "小林把最后一盒牛奶摆上货架", "dialogue": "“又是平常的夜班。”",
             "duration_s": 99, "characters": ["小林", "便利店", "路人甲"],
             "sequence_relation": "bogus", "felt_intent": "无聊",
             "narrative_beat": "日常铺垫",
             "motion_contract": {
                 "start_state": "货架还空着一格",
                 "primary_motion": "把牛奶盒推进货架",
                 "secondary_motion": "顶灯在塑料包装上滑动反光",
                 "end_state": "牛奶盒在货架最上层",
                 "camera_motion": "慢推"},
             "beats": {"already_happened": ["开场"],
                       "this_clip_only": ["摆牛奶"],
                       "reserved_for_later": ["监控里的身影"]},
             "object_states": [{"name": "牛奶盒", "count": "仅一盒",
                                "start_state": "在小林手中",
                                "end_state": "在货架最上层"}],
             "camera": {"shot": "中景", "movement": "缓推"},
             "lighting_palette": "冷白顶灯,雨夜窗外霓虹反光",
             "acceptance": {"required": ["牛奶盒"], "forbidden": ["多余人物"]}}]},
        {"title": "确认身影", "shots": [
            {"action": "小林回头看向身后的过道", "dialogue": "谁在那里？",
             "duration_s": 2, "characters": ["小林"],
             "sequence_relation": "next_shot", "felt_intent": "警觉",
             "narrative_beat": "发现异常",
             "motion_contract": {
                 "start_state": "小林背对过道",
                 "primary_motion": "回头看向过道深处",
                 "secondary_motion": "监控屏幕的雪花噪声闪动",
                 "end_state": "小林的视线锁定过道",
                 "camera_motion": "手持微晃"},
             "beats": {"already_happened": ["摆牛奶"],
                       "this_clip_only": ["回头确认"],
                       "reserved_for_later": ["身影的真实身份"]},
             "object_states": [],
             "camera": {"shot": "近景", "movement": "手持"},
             "lighting_palette": "冷白顶灯,雨夜窗外霓虹反光",
             "acceptance": {"required": [], "forbidden": []}}]},
    ],
}


def test_assistant_multi_turn_then_proposal():
    model = ScriptChatModel([
        json.dumps({"reply": "想拍什么题材?", "proposal": None},
                   ensure_ascii=False),
        json.dumps({"reply": "好,方案齐了",
                    "proposal": {"title": "雨夜便利店", "brief": "雨夜便利店,店员发现监控里多了一个人",
                                 "style": "写实电影感", "duration_target_s": 30,
                                 "aspect_ratio": "9:16"}}, ensure_ascii=False),
    ])
    r1 = run_assistant_chat(model, [{"role": "user", "content": "想拍个短剧"}])
    assert r1["reply"] == "想拍什么题材?" and r1["proposal"] is None
    r2 = run_assistant_chat(model, [
        {"role": "user", "content": "想拍个短剧"},
        {"role": "assistant", "content": "想拍什么题材?"},
        {"role": "user", "content": "雨夜便利店悬疑,30秒竖屏"},
    ])
    assert r2["proposal"]["brief"] == "雨夜便利店,店员发现监控里多了一个人"
    assert r2["proposal"]["aspect_ratio"] == "9:16"
    assert r2["proposal"]["duration_target_s"] == 30
    # system prompt 在最前,历史按序传入
    assert model.calls[1][0]["role"] == "system"
    assert model.calls[1][-1]["content"] == "雨夜便利店悬疑,30秒竖屏"


def test_assistant_non_json_reply_degrades():
    model = ScriptChatModel(["抱歉我没听懂,能再说说吗?"])
    out = run_assistant_chat(model, [{"role": "user", "content": "hi"}])
    assert out["reply"] == "抱歉我没听懂,能再说说吗?"
    assert out["proposal"] is None


def test_clamp_proposal_rules():
    # 超界时长收进 [10,600],非法画幅回退 16:9,空标题取 brief 前 20 字
    p = _clamp_proposal({"brief": "x" * 30, "duration_target_s": 9999,
                         "aspect_ratio": "4:3", "title": ""})
    assert p["duration_target_s"] == 600 and p["aspect_ratio"] == "16:9"
    assert p["title"] == "x" * 20
    p = _clamp_proposal({"brief": "短片", "duration_target_s": 1,
                         "aspect_ratio": "1:1"})
    assert p["duration_target_s"] == 10 and p["aspect_ratio"] == "1:1"
    # 空 brief / 非 dict 一律未就绪
    assert _clamp_proposal({"brief": "  "}) is None
    assert _clamp_proposal("not a dict") is None
    assert _clamp_proposal(None) is None


def test_assistant_history_capped():
    model = ScriptChatModel(['{"reply":"ok"}'])
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(40)]
    run_assistant_chat(model, msgs)
    # system + 最近 20 条
    assert len(model.calls[0]) == 21
    assert model.calls[0][-1]["content"] == "m39"


def test_assistant_checklist_guided_and_enriched():
    """引导式对话:信息不足只追问;checklist 白名单;用户说过的时长/画幅兜底进清单。"""
    model = ScriptChatModel([json.dumps({
        "reply": "好题材。主角是什么人?", "proposal": None,
        "checklist": {"topic": "雨夜便利店", "lead": "", "conflict": "",
                      "ending": "", "style": "", "duration": "", "aspect": "",
                      "junk": "不该出现"}}, ensure_ascii=False)])
    out = run_assistant_chat(model, [
        {"role": "user", "content": "想拍个雨夜便利店的故事，30秒竖屏"}])
    assert out["proposal"] is None                    # 信息不足,不硬出方案
    assert out["reply"] == "好题材。主角是什么人?"
    assert set(out["checklist"]) == {"topic", "lead", "conflict", "ending",
                                     "style", "duration", "aspect"}
    assert out["checklist"]["topic"] == "雨夜便利店"
    assert out["checklist"]["duration"] == "30 秒"     # 确定性提取
    assert out["checklist"]["aspect"] == "9:16"


def test_assistant_checklist_filled_when_proposal_ready():
    """出方案时清单自动补全(topic/style/duration/aspect 不缺项)。"""
    model = ScriptChatModel([json.dumps({
        "reply": "好,信息够了。", "proposal": {
            "title": "雨夜便利店", "brief": "雨夜便利店悬疑",
            "style": "写实电影感", "duration_target_s": 30,
            "aspect_ratio": "9:16"},
        "checklist": {"topic": "雨夜便利店悬疑", "lead": "店员",
                      "conflict": "监控里的另一个自己", "ending": "",
                      "style": "", "duration": "", "aspect": ""}},
        ensure_ascii=False)])
    out = run_assistant_chat(model, [
        {"role": "user", "content": "雨夜便利店，店员在监控里看到另一个自己，写实电影感，30秒竖屏"}])
    assert out["proposal"] is not None
    checklist = out["checklist"]
    assert checklist["topic"] and checklist["lead"] and checklist["conflict"]
    assert checklist["style"] == "写实电影感"           # 从 proposal 补齐
    assert checklist["duration"] == "30 秒"
    assert checklist["aspect"] == "9:16"
    assert checklist["ending"] == ""                   # 没聊到的仍然为空


_STRUCTURED = (
    "Duration: 15 seconds | Aspect ratio: 16:9 | Style: Cyberpunk grunge\n"
    "SCENE Four East Asian women in an industrial warehouse. "
    "SHOT BREAKDOWN 0-4s group formation, slow push-in. "
    "CAMERA wide lenses, dolly-ins. AVOID natural sunlight, slow pacing. "
    + "细节" * 40)


def test_assistant_structured_prompt_fast_path():
    """粘贴完整结构化提示词:跳过 LLM,直接出方案并提取时长/画幅/风格。"""
    model = ScriptChatModel([])      # 不应被调用
    out = run_assistant_chat(model, [{"role": "user", "content": _STRUCTURED}])
    assert not model.calls
    p = out["proposal"]
    assert p is not None
    assert p["duration_target_s"] == 15 and p["aspect_ratio"] == "16:9"
    assert p["style"] == "Cyberpunk grunge"
    assert p["brief"] == _STRUCTURED.strip()   # 原文完整保留


def test_assistant_start_intent_fallback():
    """用户喊开机但 LLM 没给 proposal:用历史用户消息兜底组装方案。"""
    model = ScriptChatModel(['{"reply":"还差什么吗?"}'])
    out = run_assistant_chat(model, [
        {"role": "user", "content": "拍雨夜便利店,店员在监控里看到另一个自己"},
        {"role": "assistant", "content": "风格时长画幅?"},
        {"role": "user", "content": "直接开始吧"},
    ])
    assert out["proposal"] is not None
    assert "雨夜便利店" in out["proposal"]["brief"]
    assert out["proposal"]["duration_target_s"] == 60      # 默认
    assert out["proposal"]["aspect_ratio"] == "16:9"       # 默认


def test_assistant_deterministic_overrides_llm():
    """时长/画幅以对话中的正则提取为准,覆盖 LLM 给的值。"""
    model = ScriptChatModel([json.dumps(
        {"reply": "好", "proposal": {"title": "t", "brief": "猫咪咖啡馆日常",
         "style": "治愈", "duration_target_s": 60, "aspect_ratio": "16:9"}},
        ensure_ascii=False)])
    out = run_assistant_chat(model, [
        {"role": "user", "content": "想拍个猫咪咖啡馆的治愈日常,30秒竖屏"}])
    p = out["proposal"]
    assert p["duration_target_s"] == 30        # 覆盖 LLM 的 60
    assert p["aspect_ratio"] == "9:16"         # 「竖屏」覆盖 LLM 的 16:9


def test_extract_overrides_no_match_keeps_llm_values():
    model = ScriptChatModel([json.dumps(
        {"reply": "好", "proposal": {"title": "t", "brief": "雨夜便利店悬疑",
         "style": "写实", "duration_target_s": 45, "aspect_ratio": "1:1"}},
        ensure_ascii=False)])
    out = run_assistant_chat(model, [
        {"role": "user", "content": "拍雨夜便利店悬疑,店员看到另一个自己"}])
    p = out["proposal"]
    assert p["duration_target_s"] == 45 and p["aspect_ratio"] == "1:1"


# ------------------------------------------------------------ planner 层 -----
def test_run_assistant_plan_structures_and_clamps():
    model = ScriptChatModel([_WRITER_OUT, _DIRECTOR_OUT])
    out = run_assistant_plan(
        model, [{"role": "user", "content": "雨夜便利店悬疑,12秒竖屏"}],
        _STRUCTURED_PROPOSAL)
    plan = out["plan"]
    assert plan["title"] == "雨夜便利店"
    assert plan["aspect_ratio"] == "9:16"          # 画幅锁在 proposal 上
    assert plan["shot_count"] == 2 and len(plan["scenes"]) == 2
    first = plan["scenes"][0]["shots"][0]
    assert first["duration_s"] == 8                # 99 -> [3,8]
    assert first["dialogue"] == "又是平常的夜班。"   # 台词清洗(去引号)
    assert first["characters"] == ["小林", "便利店"]  # 未登记角色被丢弃
    assert first["sequence_relation"] == "sequence_first"  # 非法值按位置兜底
    assert first["motion_contract"]["secondary_motion"]    # 独立次运动保留
    assert first["object_states"][0]["count"] == "仅一盒"
    second = plan["scenes"][1]["shots"][0]
    assert second["duration_s"] == 3               # 2 -> [3,8]
    assert second["sequence_relation"] == "next_shot"
    assert plan["total_duration_s"] == 11          # 偏差在 15% 内不缩放
    # 两轮调用:编剧+选角 → 导演一次拆完全片
    assert len(model.json_calls) == 2
    assert "编剧+选角" in model.json_calls[0]["instructions"]
    assert "导演" in model.json_calls[1]["instructions"]
    director_payload = model.json_calls[1]["payload"]
    assert director_payload["cast"][1]["kind"] == "location"
    assert director_payload["shot_budget"]["total"] >= 1
    assert director_payload["project"]["duration_target_s"] == 12


def test_run_assistant_plan_accepts_flat_director_shots():
    director = {"shots": [
        {"scene": "监控异常", "action": "小林摆牛奶", "duration_s": 5},
        {"scene_index": 1, "action": "小林回头", "duration_s": 5},
    ]}
    plan = normalize_plan(_WRITER_OUT["scenes"], _WRITER_OUT["characters"],
                          director, _STRUCTURED_PROPOSAL)
    assert plan["scenes"][0]["shots"][0]["action"] == "小林摆牛奶"
    assert plan["scenes"][1]["shots"][0]["action"] == "小林回头"
    assert plan["shot_count"] == 2


def test_normalize_plan_caps_counts_and_drops_empty_actions():
    scenes = [{"title": f"场景{i}", "summary": "s"} for i in range(50)]
    shots = [{"action": f"动作{i}"} for i in range(200)] + [{"action": "  "}]
    director = {"scenes": [{"title": f"场景{i}", "shots": shots}
                           for i in range(50)]}
    plan = normalize_plan(scenes, [], director, {"brief": "简案",
                                                 "duration_target_s": 10})
    max_scenes, max_shots = plan_limits(10)
    assert len(plan["scenes"]) <= max_scenes
    assert plan["shot_count"] <= max_shots
    assert all(sh["action"] for sc in plan["scenes"] for sh in sc["shots"])


def test_run_assistant_plan_falls_back_to_history_proposal():
    model = ScriptChatModel([_WRITER_OUT, _DIRECTOR_OUT])
    out = run_assistant_plan(model, [{"role": "user",
                                      "content": "拍雨夜便利店悬疑,店员看到另一个自己"}],
                             None)
    assert out["proposal"]["brief"]                     # 历史消息兜底组装
    assert out["plan"]["aspect_ratio"] == "16:9"        # 默认画幅


def test_run_assistant_plan_without_scenes_raises():
    model = ScriptChatModel([{"scenes": []}])
    with pytest.raises(ValueError):
        run_assistant_plan(model, [{"role": "user", "content": "拍个雨夜故事"}],
                           _STRUCTURED_PROPOSAL)


# ----------------------------------------------- 用户硬性要求(镜头数/单镜秒数) --
def test_requested_constraints_parsing():
    from svf.agents.planner import requested_constraints
    assert requested_constraints([
        {"role": "user", "content": "一共三个镜头，30秒竖屏"}]) == {"shot_count": 3}
    assert requested_constraints([
        {"role": "user", "content": "只做2个分镜"}]) == {"shot_count": 2}
    assert requested_constraints([
        {"role": "user", "content": "拍12个镜头，每镜5秒"}]) == {
            "shot_count": 12, "shot_seconds": 5}
    assert requested_constraints([
        {"role": "user", "content": "随便拍拍，做个短剧"}]) == {}
    # 助手消息里出现的不算(只认用户原话)
    assert requested_constraints([
        {"role": "assistant", "content": "给你拆 8 个镜头"}]) == {}


def _shot(action, duration_s=5):
    return {"action": action, "duration_s": duration_s}


def test_plan_honors_requested_shot_count():
    """用户说共三个镜头:导演给多了也收敛为 3 镜,且不留空场次。"""
    director = {"scenes": [
        {"title": "监控异常", "shots": [_shot("摆牛奶"), _shot("看向监控"),
                                        _shot("擦台面")]},
        {"title": "确认身影", "shots": [_shot("回头"), _shot("镜头推到屏幕")]},
    ]}
    model = ScriptChatModel([_WRITER_OUT, director])
    out = run_assistant_plan(
        model, [{"role": "user", "content": "雨夜便利店悬疑，一共三个镜头，12秒竖屏"}],
        _STRUCTURED_PROPOSAL)
    plan = out["plan"]
    assert plan["requested_shot_count"] == 3
    assert plan["shot_count"] == 3
    assert plan["constraint_note"] == "按你的要求 3 个镜头"
    assert all(sc["shots"] for sc in plan["scenes"])   # 无空场次
    # 硬性要求进了编剧与导演指令,导演预算也用 3
    assert "只做 3 个镜头" in model.json_calls[0]["instructions"]
    assert "严格等于 3" in model.json_calls[1]["instructions"]
    assert model.json_calls[1]["payload"]["shot_budget"]["total"] == 3
    assert model.json_calls[1]["payload"]["requested"] == {"shot_count": 3}


def test_plan_drops_empty_scenes_from_director():
    """导演漏拆的场次不进入方案(不显示「本场没有可用镜头」空块)。"""
    director = {"scenes": [
        {"title": "监控异常", "shots": [_shot("摆牛奶"), _shot("看向监控")]},
        {"title": "确认身影", "shots": []},          # 导演漏了这场
    ]}
    plan = normalize_plan(_WRITER_OUT["scenes"], _WRITER_OUT["characters"],
                          director, _STRUCTURED_PROPOSAL)
    assert len(plan["scenes"]) == 1
    assert plan["shot_count"] == 2
    assert all(sc["shots"] for sc in plan["scenes"])


def test_plan_honors_requested_shot_seconds():
    """用户说每镜 5 秒:所有镜头按 5 秒落库,不再为凑总时长缩放。"""
    director = {"scenes": [
        {"title": "监控异常", "shots": [_shot("摆牛奶", 3), _shot("看向监控", 8)]},
    ]}
    model = ScriptChatModel([_WRITER_OUT, director])
    out = run_assistant_plan(
        model, [{"role": "user", "content": "雨夜便利店，共2个镜头，每镜5秒"}],
        _STRUCTURED_PROPOSAL)
    plan = out["plan"]
    assert plan["shot_count"] == 2
    assert [s["duration_s"] for sc in plan["scenes"] for s in sc["shots"]] == [5, 5]
    assert "每镜时长约 5 秒" in model.json_calls[1]["instructions"]


# ------------------------------------------------- 一句话一键出片(参数提取/全自动) --
def test_oneclick_extract_params():
    from svf.agents.planner import extract_oneclick_params
    assert extract_oneclick_params("雨夜便利店悬疑，30秒竖屏") == {
        "duration_target_s": 30, "aspect_ratio": "9:16"}
    assert extract_oneclick_params("雪夜公交治愈，共3个镜头，每镜5秒，横屏") == {
        "aspect_ratio": "16:9", "shot_count": 3, "shot_seconds": 5}
    assert extract_oneclick_params("拍个九比十六的短片，二十秒") == {
        "duration_target_s": 20, "aspect_ratio": "9:16"}
    assert extract_oneclick_params("随便拍个小故事") == {}


def test_oneclick_auto_produces_and_composes(tmp_path):
    """一句话一键出片:自动规划 → 自动生成 → 自动拼接,无需任何人工点击。"""
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    model = ScriptChatModel([
        json.dumps({"reply": "好", "proposal": {
            "title": "雪夜末班车", "brief": "雪夜末班公交上的治愈故事",
            "style": "治愈系", "duration_target_s": 60,
            "aspect_ratio": "16:9"}}, ensure_ascii=False),
        _WRITER_OUT, _DIRECTOR_OUT,
    ])
    app = create_app(store, asset_store, renderer=FakeRenderer(asset_store),
                     judge=FakeJudge(), text_model=model,
                     image_model=FakeImageModel(),
                     data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        r = client.post("/projects/oneclick",
                        json={"text": "雪夜末班公交治愈短剧，20秒横屏"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["duration_target_s"] == 20        # 显式时长优先
        assert body["aspect_ratio"] == "16:9"         # 显式画幅优先
        pid = body["project_id"]
        # 全自动跑到自动拼接
        assert wait_for(lambda: any(e.type == "export.completed"
                                    for e in store.events_since(pid)), timeout=30)
        project = store.get("projects", pid)
        assert project.title == "雪夜末班车"           # 标题由助理补
        assert project.style == "治愈系"               # 风格由助理补
        assert project.duration_target_s == 20         # 不被助理的 60 覆盖
        shots = store.all("shots", project_id=pid)
        assert len(shots) == 2
        assert all(s.spec.aspect_ratio == "16:9" for s in shots)
        assert all(s.accepted_run_id for s in shots)
        derived = [a for a in store.all("assets", project_id=pid)
                   if a.source == "derived"]
        assert derived, "全自动流程应产出成片资产(ffmpeg 可用为视频,否则 manifest)"
        types = [e.type for e in store.events_since(pid)]
        assert "plan.completed" in types and "quick.orchestrated" in types
        assert "produce.awaiting_confirm" not in types     # 全自动模式不停
    finally:
        app.state.engine.shutdown()
        store.close()


def test_oneclick_confirm_mode_pauses_then_confirms(tmp_path):
    """素材后确认模式:素材完成即暂停不排队;确认后才渲染并自动拼接。"""
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    model = ScriptChatModel([
        json.dumps({"reply": "好", "proposal": {
            "title": "雨夜", "brief": "雨夜便利店悬疑", "style": "写实",
            "duration_target_s": 12, "aspect_ratio": "9:16"}},
            ensure_ascii=False),
        _WRITER_OUT, _DIRECTOR_OUT,
    ])
    app = create_app(store, asset_store, renderer=FakeRenderer(asset_store),
                     judge=FakeJudge(), text_model=model,
                     image_model=FakeImageModel(),
                     data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        pid = client.post("/projects/oneclick", json={
            "text": "雨夜便利店悬疑，12秒竖屏",
            "confirm_after_assets": True}).json()["project_id"]

        def types():
            return [e.type for e in store.events_since(pid)]
        # 素材完成 -> 暂停等确认,不排队 run
        assert wait_for(lambda: "produce.awaiting_confirm" in types(), timeout=30)
        assert store.all("runs", project_id=pid) == []
        proj = next(p for p in client.get("/tasks").json()["projects"]
                    if p["project_id"] == pid)
        assert proj["stage"] == "awaiting_confirm"
        assert proj["awaiting_confirm"] is True
        assert client.get("/tasks").json()["summary"]["awaiting"] >= 1
        # 确认 -> 渲染 -> 全部验收 -> 自动拼接
        r = client.post(f"/projects/{pid}/confirm")
        assert r.status_code == 202, r.text
        assert wait_for(lambda: any(e.type == "export.completed"
                                    for e in store.events_since(pid)), timeout=30)
        shots = store.all("shots", project_id=pid)
        assert shots and all(s.accepted_run_id for s in shots)
        assert len(store.all("runs", project_id=pid)) == len(shots)
        assert store.get("projects", pid).confirm_after_assets is False
        assert "produce.confirmed" in types()
    finally:
        app.state.engine.shutdown()
        store.close()


def test_oneclick_rejects_empty_text(env):
    client, store, _ = env
    r = client.post("/projects/oneclick", json={"text": "   "})
    assert r.status_code == 422
    assert store.all("projects") == []


# ------------------------------------------------- 服务重启自愈(断点续跑) --
def _seed_interrupted_project(store, *, stopped: bool = False,
                              awaiting: bool = False):
    from svf.domain.schemas.core import (Event, Project, Scene, Shot, ShotSpec)
    from svf.domain.video_constraints import constrained_acceptance
    # auto_compose=False:本测试只验证「续跑」，避免拼接线程与 teardown 竞争
    project = Project(title="断点续跑", brief="雨夜便利店", duration_target_s=12,
                      aspect_ratio="9:16", auto_compose=False)
    store.put("projects", project)
    scene = Scene(scene_id="scene_x", project_id=project.project_id, order=0,
                  title="雨夜", summary="s")
    store.put("scenes", scene)
    store.put("shots", Shot(
        shot_id="shot_x", scene_id=scene.scene_id,
        project_id=project.project_id, order=0,
        spec=ShotSpec(shot_id="shot_x", action="店员走近柜台", duration_s=5,
                      aspect_ratio="9:16", acceptance=constrained_acceptance())))
    store.append_event(Event(project_id=project.project_id,
                             type="quick.started", actor="orchestrator",
                             summary="oneclick"))
    if stopped:
        store.append_event(Event(project_id=project.project_id,
                                 type="produce.stop_requested", actor="api",
                                 summary="user stop"))
    elif awaiting:
        store.append_event(Event(project_id=project.project_id,
                                 type="produce.awaiting_confirm",
                                 actor="orchestrator",
                                 summary="waiting for confirmation"))
    return project


def test_resume_interrupted_production_on_boot(tmp_path):
    """服务重启后自动接上被打断的生产任务:定妆/首帧/排队幂等续跑。"""
    db = tmp_path / "factory.db"
    store = Store(db)
    project = _seed_interrupted_project(store)
    store.close()                                # 模拟旧进程退出

    store = Store(db)                            # 新进程启动
    asset_store = FakeAssetStore(tmp_path / "assets")
    app = create_app(store, asset_store, renderer=FakeRenderer(asset_store),
                     judge=FakeJudge(), text_model=None,
                     image_model=FakeImageModel(),
                     data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        def types():
            return [e.type for e in store.events_since(project.project_id)]
        assert wait_for(lambda: "produce.resumed" in types(), timeout=15)
        assert wait_for(lambda: "quick.orchestrated" in types(), timeout=20)
        assert len(store.all("runs", project_id=project.project_id)) == 1
        assert len(store.all("shots", project_id=project.project_id)) == 1
    finally:
        app.state.engine.shutdown()
        store.close()


def test_resume_skips_user_stopped_projects(tmp_path):
    """用户主动停止过的项目不自动续跑。"""
    db = tmp_path / "factory.db"
    store = Store(db)
    project = _seed_interrupted_project(store, stopped=True)
    store.close()

    store = Store(db)
    asset_store = FakeAssetStore(tmp_path / "assets")
    app = create_app(store, asset_store, renderer=FakeRenderer(asset_store),
                     judge=FakeJudge(), text_model=None,
                     image_model=FakeImageModel(),
                     data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        time.sleep(3.2)                          # 等续跑扫描窗口过去
        types = [e.type for e in store.events_since(project.project_id)]
        assert "produce.resumed" not in types
        assert store.all("runs", project_id=project.project_id) == []
    finally:
        app.state.engine.shutdown()
        store.close()


def test_resume_skips_awaiting_confirm_projects(tmp_path):
    """素材后确认模式暂停中的项目,重启不会绕过确认自动续跑。"""
    db = tmp_path / "factory.db"
    store = Store(db)
    project = _seed_interrupted_project(store, awaiting=True)
    store.close()

    store = Store(db)
    asset_store = FakeAssetStore(tmp_path / "assets")
    app = create_app(store, asset_store, renderer=FakeRenderer(asset_store),
                     judge=FakeJudge(), text_model=None,
                     image_model=FakeImageModel(),
                     data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        time.sleep(3.2)
        types = [e.type for e in store.events_since(project.project_id)]
        assert "produce.resumed" not in types
        assert store.all("runs", project_id=project.project_id) == []
    finally:
        app.state.engine.shutdown()
        store.close()


def test_oneclick_honors_shot_count(tmp_path):
    """一句话里说「共2个镜头」:方案收敛为 2 镜。"""
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    director = {"scenes": [
        {"title": "监控异常", "shots": [_shot("A"), _shot("B"), _shot("C")]},
        {"title": "确认身影", "shots": [_shot("D"), _shot("E")]},
    ]}
    model = ScriptChatModel([
        json.dumps({"reply": "好", "proposal": {
            "title": "雨夜", "brief": "雨夜便利店悬疑", "style": "写实",
            "duration_target_s": 30, "aspect_ratio": "9:16"}},
            ensure_ascii=False),
        _WRITER_OUT, director,
    ])
    app = create_app(store, asset_store, renderer=FakeRenderer(asset_store),
                     judge=FakeJudge(), text_model=model,
                     image_model=FakeImageModel(),
                     data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        pid = client.post("/projects/oneclick", json={
            "text": "雨夜便利店悬疑，共2个镜头，30秒竖屏"}).json()["project_id"]
        assert wait_for(lambda: len(store.all("shots", project_id=pid)) == 2,
                        timeout=30)
        shots = store.all("shots", project_id=pid)
        assert len(shots) == 2
        assert all(s.spec.aspect_ratio == "9:16" for s in shots)
    finally:
        app.state.engine.shutdown()
        store.close()


# --------------------------------------------------------------- API 层 ------
@pytest.fixture()
def env(tmp_path):
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    model = ScriptChatModel([])
    app = create_app(store, asset_store, renderer=FakeRenderer(asset_store),
                     judge=FakeJudge(), text_model=model,
                     image_model=FakeImageModel(),
                     data_dir=str(tmp_path / "data"), start_worker=False)
    client = TestClient(app)
    yield client, store, model
    app.state.engine.shutdown()
    store.close()


def test_chat_endpoint_roundtrip(env):
    client, _, model = env
    model.replies.append(json.dumps(
        {"reply": "方案齐了", "proposal": {"brief": "雨夜便利店悬疑",
         "duration_target_s": 30, "aspect_ratio": "9:16", "style": "写实",
         "title": "雨夜便利店"}}, ensure_ascii=False))
    r = client.post("/assistant/chat",
                    json={"messages": [{"role": "user", "content": "开机"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"] == "方案齐了"
    assert body["proposal"]["aspect_ratio"] == "9:16"
    assert isinstance(body.get("checklist"), dict)     # 引导进度清单


def test_chat_endpoint_503_without_text_model(tmp_path):
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    app = create_app(store, asset_store, renderer=FakeRenderer(asset_store),
                     judge=FakeJudge(), text_model=None,
                     data_dir=str(tmp_path / "data"), start_worker=False)
    client = TestClient(app)
    try:
        r = client.post("/assistant/chat",
                        json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 503
    finally:
        app.state.engine.shutdown()
        store.close()


def test_chat_proposal_feeds_quick_create(env):
    """proposal 的字段直接能当 QuickCreate 用(自动出片链路)。"""
    client, store, model = env
    model.replies.append(json.dumps(
        {"reply": "好", "proposal": {"title": "雨夜", "brief": "雨夜便利店",
         "style": "写实", "duration_target_s": 30, "aspect_ratio": "9:16"}},
        ensure_ascii=False))
    proposal = client.post("/assistant/chat",
                           json={"messages": [{"role": "user",
                                               "content": "开机"}]}).json()["proposal"]
    r = client.post("/projects/quick", json={**proposal, "mode": "text"})
    assert r.status_code == 201, r.text
    pid = r.json()["project_id"]
    project = store.get("projects", pid)
    assert project.title == "雨夜" and project.aspect_ratio == "9:16"
    assert project.duration_target_s == 30 and project.style == "写实"
    # 等后台编排线程结束(空方案也会走 plan.completed),
    # 避免 teardown 关库后线程访问 sqlite
    assert wait_for(lambda: any(
        e.type in ("quick.orchestrated", "quick.failed", "plan.failed")
        for e in store.events_since(pid)), timeout=30)


def test_plan_endpoint_roundtrip(env):
    """对话拆解端点:proposal + 历史 -> 完整结构化方案(不落库)。"""
    client, store, model = env
    model.replies.extend([_WRITER_OUT, _DIRECTOR_OUT])
    r = client.post("/assistant/plan", json={
        "messages": [{"role": "user", "content": "雨夜便利店悬疑,12秒竖屏"}],
        "proposal": _STRUCTURED_PROPOSAL})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan"]["shot_count"] == 2
    assert body["plan"]["aspect_ratio"] == "9:16"
    assert body["plan"]["scenes"][0]["shots"][0]["lighting_palette"]
    # 只预览不落库:没有项目、场景、镜头被创建
    assert store.all("projects") == []
    assert store.all("shots") == []


def test_plan_endpoint_422_when_requirements_missing(env):
    client, _, model = env
    model.replies.append({"scenes": []})
    r = client.post("/assistant/plan", json={
        "messages": [{"role": "user", "content": "帮我做个视频"}],
        "proposal": None})
    assert r.status_code == 422, r.text


def test_plan_endpoint_503_without_text_model(tmp_path):
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    app = create_app(store, asset_store, renderer=FakeRenderer(asset_store),
                     judge=FakeJudge(), text_model=None,
                     data_dir=str(tmp_path / "data"), start_worker=False)
    client = TestClient(app)
    try:
        r = client.post("/assistant/plan", json={"messages": [], "proposal": None})
        assert r.status_code == 503
    finally:
        app.state.engine.shutdown()
        store.close()


def _dialogue_plan() -> dict:
    """用同一套归一化逻辑造一份对话方案(与前端预览的格式一致)。"""
    model = ScriptChatModel([_WRITER_OUT, _DIRECTOR_OUT])
    return run_assistant_plan(model, [{"role": "user", "content": "雨夜便利店"}],
                              _STRUCTURED_PROPOSAL)["plan"]


def test_quick_with_plan_materializes_without_llm(env):
    """一键出片:plan 同步物化(接口返回即可见),但**不自动**开始生成。"""
    client, store, model = env
    plan = _dialogue_plan()
    r = client.post("/projects/quick", json={
        "title": "雨夜便利店", "brief": _STRUCTURED_PROPOSAL["brief"],
        "style": _STRUCTURED_PROPOSAL["style"],
        "duration_target_s": _STRUCTURED_PROPOSAL["duration_target_s"],
        "aspect_ratio": "9:16", "mode": "text", "plan": plan})
    assert r.status_code == 201, r.text
    body = r.json()
    pid = body["project_id"]
    assert body["status"] == "planned" and body["shots"] == 2
    # 同步物化:返回时场景/角色/镜头已可读(前端跳转不会扑空)
    scenes = store.all("scenes", project_id=pid)
    shots = store.all("shots", project_id=pid)
    assert len(scenes) == 2 and len(shots) == 2
    # 物化全程零 LLM 调用(agent 已在对话里产出方案)
    assert model.calls == [] and model.json_calls == []
    # 画幅锁项目值、时长收敛到 [3,8]
    assert all(s.spec.aspect_ratio == "9:16" for s in shots)
    assert all(3 <= s.spec.duration_s <= 8 for s in shots)
    chars = {c.name: c for c in store.all("characters", project_id=pid)}
    assert chars["小林"].kind == "character"
    assert chars["便利店"].kind == "location"
    # 角色/地点锁定进入 ShotSpec(渲染提示词 SCENE 段)
    assert {c["name"] for c in shots[0].spec.characters} == {"小林", "便利店"}
    # 未点「开始生成」前不建 run
    assert store.all("runs", project_id=pid) == []

    # 手动开始生成:定妆→首帧→逐镜排队
    r = client.post(f"/projects/{pid}/produce")
    assert r.status_code == 202, r.text
    assert wait_for(lambda: any(e.type == "quick.orchestrated"
                                for e in store.events_since(pid)), timeout=30)
    runs = store.all("runs", project_id=pid)
    assert len(runs) == len(shots)
    assert model.calls == [] and model.json_calls == []
    # 幂等:再次开始生成不会给同一镜头重复排队
    r = client.post(f"/projects/{pid}/produce")
    assert r.status_code == 202
    assert wait_for(lambda: any(e.type == "quick.orchestrated" and "0 runs"
                                in e.summary
                                for e in store.events_since(pid)), timeout=30)
    assert len(store.all("runs", project_id=pid)) == len(shots)
    # 事件流:对话方案标记 + 手动开始标记
    types = [e.type for e in store.events_since(pid)]
    assert "plan.started" in types and "plan.completed" in types
    assert types.count("quick.started") >= 1


def test_quick_with_plan_works_without_text_model(tmp_path):
    """方案已产出:即使 provider 不可用,填充与开始生成也都能走通。"""
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    app = create_app(store, asset_store, renderer=FakeRenderer(asset_store),
                     judge=FakeJudge(), text_model=None,
                     image_model=FakeImageModel(),
                     data_dir=str(tmp_path / "data"), start_worker=False)
    client = TestClient(app)
    try:
        r = client.post("/projects/quick", json={
            "title": "雨夜便利店", "brief": "雨夜便利店悬疑",
            "duration_target_s": 12, "aspect_ratio": "9:16",
            "mode": "text", "plan": _dialogue_plan()})
        assert r.status_code == 201, r.text
        pid = r.json()["project_id"]
        assert len(store.all("shots", project_id=pid)) == 2
        assert store.all("runs", project_id=pid) == []
        r = client.post(f"/projects/{pid}/produce")
        assert r.status_code == 202, r.text
        assert wait_for(lambda: any(e.type == "quick.orchestrated"
                                    for e in store.events_since(pid)), timeout=30)
        assert len(store.all("runs", project_id=pid)) == 2
    finally:
        app.state.engine.shutdown()
        store.close()


def test_produce_requires_shots(env):
    client, store, _ = env
    r = client.post("/projects", json={"title": "空项目", "brief": "x"})
    pid = r.json()["project_id"]
    r = client.post(f"/projects/{pid}/produce")
    assert r.status_code == 409
    assert store.all("runs", project_id=pid) == []


def test_project_prompts_preview(env):
    """工作室可拿每镜真正下发的渲染提示词(prompts=1),与手动链路同一组装。"""
    client, store, _ = env
    plan = _dialogue_plan()
    pid = client.post("/projects/quick", json={
        "title": "雨夜便利店", "brief": _STRUCTURED_PROPOSAL["brief"],
        "duration_target_s": 12, "aspect_ratio": "9:16",
        "mode": "text", "plan": plan}).json()["project_id"]
    detail = client.get(f"/projects/{pid}?prompts=1").json()
    shots = detail["shots"]
    assert shots and all(s.get("prompt_preview") for s in shots)
    with_dialogue = next(s for s in shots if s["spec"]["dialogue"])
    prompt = with_dialogue["prompt_preview"]
    assert "Duration:" in prompt and "AVOID:" in prompt
    assert "<d>[Chinese]" in prompt
    # 默认不带 prompts 的响应不含预览(刷新更轻)
    plain = client.get(f"/projects/{pid}").json()
    assert all("prompt_preview" not in s for s in plain["shots"])


def test_auto_compose_after_all_accepted(tmp_path):
    """一键出片收尾:全部镜头验收后自动拼接成片(与手动导出同一 compose 逻辑)。"""
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    app = create_app(store, asset_store, renderer=FakeRenderer(asset_store),
                     judge=FakeJudge(), text_model=None,
                     image_model=FakeImageModel(),
                     data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        r = client.post("/projects/quick", json={
            "title": "雨夜便利店", "brief": "雨夜便利店悬疑",
            "duration_target_s": 12, "aspect_ratio": "9:16",
            "mode": "text", "plan": _dialogue_plan()})
        pid = r.json()["project_id"]
        assert store.get("projects", pid).auto_compose is True
        r = client.post(f"/projects/{pid}/produce")
        assert r.status_code == 202
        assert wait_for(lambda: any(e.type == "export.completed"
                                    for e in store.events_since(pid)), timeout=30)
        shots = store.all("shots", project_id=pid)
        assert all(s.accepted_run_id for s in shots)
        derived = [a for a in store.all("assets", project_id=pid)
                   if a.source == "derived"]
        assert derived, "自动拼接应产出一个 derived 成片资产"
        assert any(e.type == "export.started" for e in store.events_since(pid))
    finally:
        app.state.engine.shutdown()
        store.close()


def test_maybe_auto_compose_waits_for_all_shots(tmp_path, env):
    """还有镜头没验收时,自动拼接不触发。"""
    client, store, _ = env
    plan = _dialogue_plan()
    pid = client.post("/projects/quick", json={
        "title": "雨夜便利店", "brief": "雨夜便利店悬疑",
        "duration_target_s": 12, "aspect_ratio": "9:16",
        "mode": "text", "plan": plan}).json()["project_id"]
    from svf.workers.compose import maybe_auto_compose
    assert maybe_auto_compose(store, FakeAssetStore(tmp_path / "a2"), pid) is False
    assert not any(e.type == "export.started" for e in store.events_since(pid))


# ------------------------------------------------------- 项目级停止(服务端) --
class SlowImageModel:
    """available() 阻塞到 release,用于在定妆阶段测试项目级停止。"""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.generate_calls = 0

    def available(self):
        self.entered.set()
        self.release.wait(timeout=10)
        return True

    def generate(self, prompt, out_key, width=1080, height=1920,
                 seed=None, steps=30):
        self.generate_calls += 1
        return out_key


def test_stop_project_halts_production(tmp_path):
    """项目级停止:生产线程在生成项之间协作退出,不再生成参考图/排队 run。"""
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    model = SlowImageModel()
    app = create_app(store, asset_store, renderer=FakeRenderer(asset_store),
                     judge=FakeJudge(), text_model=None, image_model=model,
                     data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        pid = client.post("/projects/quick", json={
            "title": "雨夜便利店", "brief": "雨夜便利店悬疑",
            "duration_target_s": 12, "aspect_ratio": "9:16",
            "mode": "text", "plan": _dialogue_plan()}).json()["project_id"]
        assert client.post(f"/projects/{pid}/produce").status_code == 202
        assert model.entered.wait(timeout=5)        # 生产线程已进入定妆阶段
        r = client.post(f"/projects/{pid}/stop")
        assert r.status_code == 202, r.text
        model.release.set()
        assert wait_for(lambda: any(e.type == "produce.stopped"
                                    for e in store.events_since(pid)), timeout=10)
        assert model.generate_calls == 0            # 停止后不再生成图
        assert store.all("runs", project_id=pid) == []   # 不排队新 run
        assert store.all("shots", project_id=pid)        # 分镜保留
    finally:
        model.release.set()
        app.state.engine.shutdown()
        store.close()


def test_quick_with_plan_rejects_empty_scenes(env):
    client, store, _ = env
    r = client.post("/projects/quick", json={
        "brief": "x", "mode": "text", "plan": {"scenes": []}})
    assert r.status_code == 422
    assert store.all("projects") == []


def test_materialize_skips_deleted_project(tmp_path):
    """项目删除后,晚到的生产线程不能再把场景/镜头写回去(僵尸任务防护)。"""
    from svf.apps.api.app import _materialize_plan
    from svf.domain.schemas.core import Project
    store = Store(tmp_path / "factory.db")
    ghost = Project(title="已删除", brief="b")     # 未落库 = 不存在
    stats = _materialize_plan(store, ghost, {
        "scenes": [{"title": "s", "shots": [{"action": "走近"}]}]})
    assert stats == {"scenes": 0, "shots": 0, "characters": 0, "locations": 0}
    assert store.all("scenes") == [] and store.all("shots") == []
    store.close()


# --------------------------------------------------- 材料上传(逐段覆盖) --
def test_split_material_keeps_order_and_merges():
    from svf.agents.planner import split_material
    text = ("# 开场\n雨夜，小夏走进便利店，店里只有冷白顶灯。\n\n"
            "# 转折\n她在监控里看到另一个自己。\n\n"
            "# 结局\n她握紧手机，画面定格。")
    beats = split_material(text, max_beats=3)
    assert [b["title"] for b in beats] == ["开场", "转折", "结局"]
    assert "监控" in beats[1]["body"]
    merged = split_material(text, max_beats=2)
    assert len(merged) == 2
    assert merged[0]["title"] == "开场"
    assert "监控" in merged[0]["body"]          # 相邻段合并但保序
    assert merged[1]["title"] == "结局"
    assert split_material("", max_beats=3) == []


def test_material_title_markdown_prefix_is_cleaned():
    """材料文件名/正文开头的 Markdown 标记不影响项目标题与简介。"""
    from svf.apps.api.app import _clean_material_brief, _clean_title
    assert _clean_title("# 夏末的自动贩卖机 · 30s 日系动画") == "夏末的自动贩卖机 · 30s 日系动画"
    assert _clean_title("1. 开场") == "开场"
    assert _clean_title("##  ") == ""
    assert _clean_material_brief("# 开场\n雨夜，小夏走进便利店。") == "开场\n雨夜，小夏走进便利店。"
    assert _clean_material_brief("\n\n## 转折\n她在监控里看到自己。").startswith("转折")


def test_oneclick_material_covers_every_beat(tmp_path):
    """上传材料:逐段覆盖(每段至少一个镜头),成片与材料一致。"""
    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    model = ScriptChatModel([
        {"characters": [
            {"name": "小夏", "kind": "character",
             "description": "小夏:20岁女孩,黑色短发,米色毛衣"},
            {"name": "便利店", "kind": "location",
             "description": "便利店:雨夜,冷白顶灯,货架"}]},
        _DIRECTOR_OUT,          # 导演只拆了 2 个场次(第 3 段靠覆盖兜底)
    ])
    app = create_app(store, asset_store, renderer=FakeRenderer(asset_store),
                     judge=FakeJudge(), text_model=model,
                     image_model=FakeImageModel(),
                     data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    material = ("# 开场\n雨夜，小夏走进便利店。\n\n"
                "# 转折\n她在监控里看到另一个自己。\n\n"
                "# 结局\n她握紧手机，画面定格。")
    try:
        r = client.post("/projects/oneclick", json={
            "text": "悬疑风格，15秒竖屏",
            "material_text": material, "material_name": "剧本.md"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["material_name"] == "剧本.md"
        assert body["material_chars"] == len(material)
        assert body["material_asset_id"]
        pid = body["project_id"]
        assert wait_for(lambda: any(e.type == "export.completed"
                                    for e in store.events_since(pid)), timeout=30)
        # 材料原文已作为资产保存,可回溯
        asset = store.get("assets", body["material_asset_id"])
        assert asset.media_type == "text"
        assert asset_store.read_bytes(asset.storage_key).decode("utf-8") == material
        # 逐段覆盖:3 段 -> 3 个场景,每段至少一个镜头
        scenes = store.all("scenes", project_id=pid)
        shots = store.all("shots", project_id=pid)
        assert len(scenes) == 3
        assert len(shots) == 3
        assert all(s.spec.action for s in shots)
        titles = {sc.title for sc in scenes}
        assert titles == {"开场", "转折", "结局"}
        project = store.get("projects", pid)
        assert project.source_material_name == "剧本.md"
        assert project.title == "剧本"                       # 文件名清洗后不带扩展名
        assert project.brief == "悬疑风格，15秒竖屏"          # 有文本时简介优先用文本
        types = [e.type for e in store.events_since(pid)]
        assert "material.split" in types
    finally:
        app.state.engine.shutdown()
        store.close()


def test_oneclick_accepts_docx_material(tmp_path):
    """上传 .docx:标准库提取正文后逐段覆盖生成。"""
    import base64 as b64
    import io
    import zipfile as zf_mod
    buf = io.BytesIO()
    with zf_mod.ZipFile(buf, "w") as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body>'
            '<w:p><w:r><w:t>开场</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>雨夜，小夏走进便利店。</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>她在监控里看到另一个自己。</w:t></w:r></w:p>'
            '</w:body></w:document>')
    docx_b64 = b64.b64encode(buf.getvalue()).decode()

    store = Store(tmp_path / "factory.db")
    asset_store = FakeAssetStore(tmp_path / "assets")
    model = ScriptChatModel([
        {"characters": [{"name": "小夏", "kind": "character",
                         "description": "小夏:20岁女孩,黑色短发"}]},
        _DIRECTOR_OUT,
    ])
    app = create_app(store, asset_store, renderer=FakeRenderer(asset_store),
                     judge=FakeJudge(), text_model=model,
                     image_model=FakeImageModel(),
                     data_dir=str(tmp_path / "data"),
                     worker_poll_interval_s=0.05)
    client = TestClient(app)
    try:
        r = client.post("/projects/oneclick", json={
            "material_docx_b64": docx_b64, "material_name": "剧本.docx",
            "text": "15秒竖屏"})
        assert r.status_code == 201, r.text
        body = r.json()
        pid = body["project_id"]
        asset = store.get("assets", body["material_asset_id"])
        text = asset_store.read_bytes(asset.storage_key).decode("utf-8")
        assert "雨夜，小夏走进便利店。" in text
        assert "监控里看到另一个自己" in text
        assert wait_for(lambda: len(store.all("shots", project_id=pid)) >= 1,
                        timeout=30)
    finally:
        app.state.engine.shutdown()
        store.close()
