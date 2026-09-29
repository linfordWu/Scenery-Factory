# SPDX-License-Identifier: GPL-3.0-only
"""Offline adapter/agent tests. No network, no ffmpeg, no torch.

The repo root is registered as the synthetic package `svf` (its directory name
contains a hyphen) so the package-relative imports used by the adapters
(`from ...domain...`) resolve.
"""
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if "svf" not in sys.modules:
    _pkg = types.ModuleType("svf")
    _pkg.__path__ = [ROOT]
    sys.modules["svf"] = _pkg

from svf.adapters.asset_store.local import LocalAssetStore
from svf.adapters.decision.laya_shadow import LayaShadowDecision
from svf.adapters.text_model.client import TextModelClient, extract_json
from svf.agents.repair import RepairAgent, failure_tags
from svf.domain.schemas.core import Run, ScoreReport


def _png_bytes(width=640, height=480) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
            + b"\x00" * 32)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _chat_payload(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


# ----------------------------------------------------------- asset store ---

class TestLocalAssetStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = LocalAssetStore(root=self.tmp)

    def test_save_read_sha256(self):
        data = _png_bytes()
        asset = self.store.save_bytes(data, "p1", "image", "char.png",
                                      source="imported")
        self.assertEqual(asset.status, "READY")
        self.assertEqual(asset.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(asset.storage_key, f"p1/{asset.asset_id}/char.png")
        self.assertEqual(self.store.read_bytes(asset.storage_key), data)
        self.assertTrue(os.path.exists(self.store.path_for(asset.storage_key)))

    def test_probe_png_header(self):
        asset = self.store.save_bytes(_png_bytes(320, 240), "p1", "image", "a.png")
        probe = self.store.probe(asset.storage_key)
        self.assertEqual(probe.get("width"), 320)
        self.assertEqual(probe.get("height"), 240)
        self.assertEqual(asset.metadata.get("width"), 320)

    def test_probe_missing_file_is_empty(self):
        self.assertEqual(self.store.probe("nope/none/x.mp4"), {})

    def test_image_preview_is_self(self):
        asset = self.store.save_bytes(_png_bytes(), "p1", "image", "a.png")
        self.assertEqual(asset.preview_key, asset.storage_key)

    def test_save_file(self):
        src = os.path.join(self.tmp, "src.png")
        with open(src, "wb") as f:
            f.write(_png_bytes(100, 100))
        asset = self.store.save_file(src, "p2", "image", "copy.png")
        self.assertEqual(self.store.read_bytes(asset.storage_key), _png_bytes(100, 100))


# ------------------------------------------------------------ text model ---

class TestChatJson(unittest.TestCase):
    def setUp(self):
        # 显式选无密钥 provider:测试不依赖本地 .env(DEEPSEEK_API_KEY)
        self.client = TextModelClient(provider="ollama",
                                      base="http://x/v1", model="m")

    def test_extract_json_with_prose(self):
        out = extract_json('Sure! Here is it: {"a": 1, "b": {"c": 2}} hope that helps')
        self.assertEqual(out, {"a": 1, "b": {"c": 2}})

    def test_extract_json_none_found(self):
        with self.assertRaises(ValueError):
            extract_json("no json here")

    def test_chat_json_first_try(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=_FakeResponse(_chat_payload('{"ok": true}'))) as u:
            out = self.client.chat_json("do it", {"x": 1}, '{"ok": bool}')
        self.assertEqual(out, {"ok": True})
        self.assertEqual(u.call_count, 1)

    def test_chat_json_retries_once_then_succeeds(self):
        replies = [_FakeResponse(_chat_payload("not json at all")),
                   _FakeResponse(_chat_payload('{"ok": 2}'))]
        with mock.patch("urllib.request.urlopen", side_effect=replies) as u:
            out = self.client.chat_json("do it", {"x": 1}, '{"ok": int}')
        self.assertEqual(out, {"ok": 2})
        self.assertEqual(u.call_count, 2)

    def test_chat_json_raises_after_failed_retry(self):
        replies = [_FakeResponse(_chat_payload("bad")),
                   _FakeResponse(_chat_payload("still bad")),
                   _FakeResponse(_chat_payload("bad again"))]
        with mock.patch("urllib.request.urlopen", side_effect=replies) as u:
            with self.assertRaises(ValueError):
                self.client.chat_json("do it", {}, '{}')
        self.assertEqual(u.call_count, 3)


class TestExtractJsonSalvage(unittest.TestCase):
    def test_truncated_after_complete_element(self):
        raw = '{"shots": [{"action": "a", "duration_s": 5}, {"action": "b", "dura'
        out = extract_json(raw)
        self.assertEqual(out, {"shots": [{"action": "a", "duration_s": 5}]})

    def test_truncated_mid_second_element(self):
        raw = '{"shots": [{"action": "a"}, {"act'
        out = extract_json(raw)
        self.assertEqual(out, {"shots": [{"action": "a"}]})

    def test_truncated_inside_first_element_raises(self):
        # 首个元素都不完整时没有可挽救的边界,应当抛出
        with self.assertRaises(ValueError):
            extract_json('{"shots": [{"action": "端盘落')

    def test_unrecoverable_raises(self):
        with self.assertRaises(ValueError):
            extract_json('{"a": ')

    def test_valid_json_unaffected(self):
        self.assertEqual(extract_json('{"ok": 1}'), {"ok": 1})
        self.assertEqual(extract_json('pre {"ok": 2} post'), {"ok": 2})


# ------------------------------------------------------------ repair map ---

class TestRepairMapping(unittest.TestCase):
    def setUp(self):
        self.agent = RepairAgent()
        self.run = Run(shot_id="s1", project_id="p1")

    def test_decode_error_is_infra(self):
        score = ScoreReport(run_id=self.run.run_id, verdict="repair",
                            hard_checks={"decodable": False})
        plan = self.agent.plan(score, self.run)
        self.assertEqual(plan.target, "infra")
        self.assertEqual(plan.action, "requeue_render")
        self.assertEqual(plan.invalidate_from, "QUEUED")

    def test_low_identity_rebinds_reference(self):
        score = ScoreReport(run_id=self.run.run_id, verdict="repair",
                            hard_checks={"decodable": True},
                            scores={"identity": 0.4, "action": 0.9})
        plan = self.agent.plan(score, self.run)
        self.assertEqual(plan.target, "reference_assets")
        self.assertEqual(plan.action, "rebind_reference")
        self.assertEqual(plan.invalidate_from, "ASSET_READY")

    def test_low_action_rewrites_prompt(self):
        score = ScoreReport(run_id=self.run.run_id, verdict="repair",
                            hard_checks={"decodable": True},
                            scores={"action": 0.2})
        plan = self.agent.plan(score, self.run)
        self.assertEqual((plan.target, plan.action), ("prompt", "rewrite_action"))

    def test_static_image_simulation_rewrites_motion_contract(self):
        score = ScoreReport(run_id=self.run.run_id, verdict="repair",
                            evidence=[{"tag": "static_image_simulation"}])
        plan = self.agent.plan(score, self.run)
        self.assertEqual((plan.target, plan.action, plan.invalidate_from),
                         ("prompt", "rewrite_motion_contract", "PROMPT_READY"))

    def test_story_linkage_and_continuity_have_targeted_repairs(self):
        score = ScoreReport(run_id=self.run.run_id, verdict="repair",
                            scores={"story_linkage": 0.3})
        assert self.agent.plan(score, self.run).action == "rewrite_motion_contract"
        score = ScoreReport(run_id=self.run.run_id, verdict="repair",
                            scores={"continuity": 0.3})
        assert self.agent.plan(score, self.run).action == "rebind_reference"

    def test_uncertain_goes_to_human_review(self):
        score = ScoreReport(run_id=self.run.run_id, verdict="uncertain",
                            uncertain=True)
        self.assertEqual(failure_tags(score), ["uncertain"])
        plan = self.agent.plan(score, self.run)
        self.assertEqual(plan.action, "human_review")

    def test_unknown_tag_defaults_to_prompt_rewrite(self):
        score = ScoreReport(run_id=self.run.run_id, verdict="repair",
                            evidence=[{"tag": "flicker"}])
        plan = self.agent.plan(score, self.run)
        self.assertEqual((plan.target, plan.action), ("prompt", "rewrite_prompt"))


# ----------------------------------------------------------- laya shadow ---

class TestLayaShadowDecision(unittest.TestCase):
    def setUp(self):
        self.port = LayaShadowDecision(python="/nonexistent/python")
        self.state = {"run_state": "SCORING"}
        self.actions = ["retry", "accept", "escalate"]

    def test_missing_interpreter_falls_back(self):
        advice = self.port.advise(self.state, self.actions)
        self.assertEqual(advice.fallback, "rules")
        self.assertFalse(advice.accepted_by_policy)

    def test_timeout_falls_back(self):
        with mock.patch("subprocess.run",
                        side_effect=subprocess.TimeoutExpired("python", 30)):
            advice = self.port.advise(self.state, self.actions)
        self.assertEqual(advice.fallback, "rules")

    def test_nonzero_exit_falls_back(self):
        proc = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"boom")
        with mock.patch("subprocess.run", return_value=proc):
            advice = self.port.advise(self.state, self.actions)
        self.assertEqual(advice.fallback, "rules")

    def test_low_confidence_falls_back(self):
        out = json.dumps({"selected_action": "retry", "confidence": 0.3})
        proc = subprocess.CompletedProcess([], 0, stdout=out.encode(), stderr=b"")
        with mock.patch("subprocess.run", return_value=proc):
            advice = self.port.advise(self.state, self.actions)
        self.assertEqual(advice.fallback, "rules")
        self.assertFalse(advice.accepted_by_policy)

    def test_good_advice_accepted(self):
        out = ("laya load log line\n"
               + json.dumps({"selected_action": "retry", "confidence": 0.9}))
        proc = subprocess.CompletedProcess([], 0, stdout=out.encode(), stderr=b"")
        with mock.patch("subprocess.run", return_value=proc):
            advice = self.port.advise(self.state, self.actions)
        self.assertIsNone(advice.fallback)
        self.assertTrue(advice.accepted_by_policy)
        self.assertEqual(advice.selected_action, "retry")
        self.assertAlmostEqual(advice.confidence, 0.9)


if __name__ == "__main__":
    unittest.main()
