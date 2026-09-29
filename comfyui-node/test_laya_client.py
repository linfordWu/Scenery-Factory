# SPDX-License-Identifier: GPL-3.0-only
"""Laya engine tests: offline contract tests with a stub agent, plus a real-checkpoint
end-to-end pass when LAYA_MODEL_DIR points at downloaded weights (GPU optional)."""
import json
import math
import os
import pathlib
import sys
import time
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).parent
pkg = types.ModuleType("laya_test_package")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg
from laya_test_package import jev_client, laya_client  # noqa: E402
from laya_test_package.adaptive import AdaptiveController, ask as engine_ask  # noqa: E402


class StubAgent:
    """Mimics laya.Agent.predict: one canned choice answer per question."""

    def __init__(self):
        self.calls = []

    def predict(self, state, questions):
        self.calls.append((state, dict(questions)))
        answers = {}
        for qid, q in questions.items():
            keys = list(q["criteria"])
            rest = round(0.1 / max(1, len(keys) - 1), 4) if len(keys) > 1 else 0.0
            answers[qid] = {"type": "choice", "choice": keys[0],
                            "probabilities": {k: (0.9 if k == keys[0] else rest) for k in keys},
                            "confidence": 0.9, "action": {"act_probability": 1.0}}
        return {"model": "laya-rl-agent", "answers": answers,
                "usage": {"input_tokens": 100 * len(questions), "output_tokens": 0}}


def layer_state(policy="layer_v5"):
    return {"policy": policy, "measured_step": 1, "next_step": 2, "total_steps": 4,
            "sigma": 0.8, "next_sigma": 0.5,
            "blocks": {str(b): {"current_keep": 5.0,
                                "candidate_keep": {"reduce": 1.0, "baseline": 5.0, "protect": 7.5},
                                "depth": b / 49,
                                "audio": {"residual_relative_l2": 0.01, "residual_rank": 0.5,
                                          "residual_cross_step_change": None, "gate_mean_abs": 0.1,
                                          "gate_top10_mass": 0.3},
                                "video": {"residual_relative_l2": 0.02, "residual_rank": 0.5,
                                          "residual_cross_step_change": None, "gate_mean_abs": 0.1,
                                          "gate_top10_mass": 0.3}}
                       for b in range(50)}}


def block_state():
    return {"policy": "block_v3", "current_step": 1, "next_step": 2, "total_steps": 4,
            "current_sigma": 0.8, "next_sigma": 0.5, "keep_percent": 5.0,
            "max_skip_blocks": 20, "max_consecutive_skips": 1, "mandatory_execute": [0],
            "blocks": {str(b): {"relative_residual_l2": {"audio": 0.01, "video": 0.02},
                                "measured_step": 1, "relative_rank": 0.5,
                                "was_skipped": False, "consecutive_skips": 0}
                       for b in range(1, 50)}}


def gate_state():
    return {"current_step": 1, "next_step": 2, "total_steps": 4, "sigma": 0.8,
            "next_sigma": 0.5, "current_keep_percent": 10.0,
            "gate_stats": {"block": 0, "samples": 1024, "mean_abs": 0.5, "std_abs": 0.1,
                           "top10_mass": 0.3, "relative_mean_change": None}}


def av_state():
    modal = {"samples": 1024, "mean_abs": 0.5, "std_abs": 0.1, "top10_mass": 0.3,
             "relative_l2_change": None}
    return {"current_step": 1, "next_step": 2, "total_steps": 4, "sigma": 0.8,
            "next_sigma": 0.5, "current_keep_percent": 5.0,
            "gate_stats": {"policy": "av_v2", "complete": True,
                           "blocks": {str(b): {"audio": dict(modal), "video": dict(modal)}
                                      for b in (0, 24, 49)}}}


def sla_step_state():
    return {"step_measured": 1, "next_step": 2, "choices_percent": [1, 3, 5, 10],
            "protected": "rules", "objective": "obj", "limitations": "lim",
            "blocks": {str(b): {"audio": {"residual_relative_l2": 0.01, "rank": 0.5,
                                          "cross_step_change": None},
                                "video": {"residual_relative_l2": 0.02, "rank": 0.5,
                                          "cross_step_change": None}}
                       for b in range(50)}, "current_keep": [5.0] * 50}


class StubAgentTests(unittest.TestCase):
    def setUp(self):
        self.agent = StubAgent()
        self.saved = laya_client._AGENT
        laya_client._AGENT = self.agent
        self.addCleanup(setattr, laya_client, "_AGENT", self.saved)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_gate_v1_single_keep_payload(self):
        answer = laya_client.ask(gate_state())
        self.assertEqual(answer["choice"], "5")
        self.assertEqual(answer["confidence"], 0.9)
        self.assertEqual(set(answer["probabilities"]), set(jev_client.CRITERIA))
        self.assertEqual(answer["model"], "laya-local")
        state, questions = self.agent.calls[0]
        self.assertEqual(set(questions), {"keep"})
        self.assertEqual(questions["keep"]["instructions"], jev_client.INSTRUCTIONS)

    def test_av_v2_uses_v2_wording(self):
        answer = laya_client.ask(av_state())
        self.assertIn(answer["choice"], jev_client.V2_CRITERIA)
        self.assertEqual(self.agent.calls[0][1]["keep"]["instructions"], jev_client.V2_INSTRUCTIONS)

    def test_layer_v5_decisions_plus_budget(self):
        answer = laya_client.ask(layer_state("layer_v5"))
        self.assertEqual(set(answer["decisions"]), {f"b{b:02d}" for b in range(1, 50)})
        self.assertIn(answer["budget"]["choice"], ("2.5", "3", "3.5"))
        for d in answer["decisions"].values():
            self.assertIn(d["choice"], ("reduce", "baseline", "protect"))
            self.assertAlmostEqual(sum(d["probabilities"].values()), 1.0, places=2)

    def test_layer_v4_has_no_budget(self):
        answer = laya_client.ask(layer_state("layer_v4"))
        self.assertNotIn("budget", answer)
        self.assertEqual(len(answer["decisions"]), 49)

    def test_block_v3_execute_skip(self):
        answer = laya_client.ask(block_state())
        self.assertEqual(set(answer["decisions"]), {f"b{b:02d}" for b in range(1, 50)})
        for d in answer["decisions"].values():
            self.assertIn(d["choice"], ("execute", "skip"))

    def test_native_sla_init_and_step(self):
        init = laya_client.native_ask({"initialization": True, "prompt": "a cat", "next_step": 1})
        self.assertEqual(set(init["decisions"]), {str(b) for b in range(50)})
        self.assertEqual(set(init["decisions"]["0"]["probabilities"]), {"5", "10"})
        step = laya_client.native_ask(sla_step_state())
        self.assertEqual(set(step["decisions"]), {str(b) for b in range(1, 50)})
        for d in step["decisions"].values():
            self.assertIn(float(d["choice"]), (1, 3, 5, 10))

    def test_chunked_predict_merges_all_answers(self):
        os.environ["LAYA_STATE_MODE"] = "full"
        os.environ["LAYA_BATCH"] = "10"
        laya_client.ask(block_state())
        self.assertEqual(len(self.agent.calls), 5)  # 49 questions in batches of 10
        seen = [qid for _, part in self.agent.calls for qid in part]
        self.assertEqual(len(seen), 49)

    def test_compact_state_per_question(self):
        laya_client.ask(block_state())
        self.assertEqual(len(self.agent.calls), 49)  # one row per question
        state, questions = self.agent.calls[0]
        self.assertEqual(set(questions), {"b01"})
        self.assertEqual(state["own_block"], "1")
        self.assertEqual(set(state["blocks"]), {"1"})  # own block, full entry
        self.assertIn("relative_residual_l2", state["blocks"]["1"])
        self.assertEqual(len(state["peer_blocks"]), 48)
        self.assertIn("rank", state["peer_blocks"]["2"])
        self.assertIn("peer_legend", state)

    def test_compact_layer_modal_arrays(self):
        laya_client.ask(layer_state("layer_v5"))
        by_qid = {qid: s for s, part in self.agent.calls for qid in part}
        own = by_qid["b17"]
        self.assertEqual(set(own["blocks"]), {"17"})
        self.assertIn("candidate_keep", own["blocks"]["17"])  # full own entry
        self.assertEqual(own["peer_blocks"]["3"]["a"], [0.01, 0.5, None])
        self.assertEqual(own["peer_blocks"]["3"]["v"], [0.02, 0.5, None])
        budget = by_qid["budget"]  # global question: no own block, all peers compact
        self.assertNotIn("blocks", budget)
        self.assertEqual(len(budget["peer_blocks"]), 50)

    def test_full_mode_shares_state_unchanged(self):
        os.environ["LAYA_STATE_MODE"] = "full"
        state = block_state()
        laya_client.ask(state)
        for call_state, _ in self.agent.calls:
            self.assertIs(call_state, state)

    def test_no_api_key_required(self):
        self.assertNotIn("TYPESAFE_API_KEY", os.environ)
        laya_client.ask(gate_state())  # must not raise missing_api_key

    def test_engine_dispatch_defaults_to_laya(self):
        p = types.SimpleNamespace(topk_ratio=0.05)
        c = AdaptiveController(p)  # default chooser goes through adaptive.ask
        c.begin([1, .8, .5, .2, 0])
        c.stats = {"mean_abs": 1}
        c.completed_step(0)
        self.assertEqual(p.topk_ratio, 0.05)  # stub always picks "5"
        self.assertEqual(c.events[-1]["reason"], "jev")  # reason string kept for compatibility
        self.assertEqual(c.events[-1]["answer"]["model"], "laya-local")

    def test_engine_dispatch_jev_opt_in(self):
        with patch.object(jev_client, "ask", return_value={"choice": "15", "confidence": .9,
                                                           "probabilities": {"15": 1}}) as jev:
            os.environ["H3_DECISION_ENGINE"] = "jev"
            answer = engine_ask(gate_state(), "", 1)
        self.assertEqual(answer["choice"], "15")
        jev.assert_called_once()


WEIGHTS = os.environ.get("LAYA_MODEL_DIR", "").strip()


@unittest.skipUnless(WEIGHTS and (pathlib.Path(WEIGHTS) / "rl_agent_config.json").is_file(),
                     "set LAYA_MODEL_DIR to a downloaded laya bundle to run real-checkpoint tests")
class RealCheckpointTests(unittest.TestCase):
    """End-to-end with the actual mmBERT checkpoint: contract + latency per policy."""

    @classmethod
    def setUpClass(cls):
        laya_client._AGENT = None  # force a real load
        cls.agent = laya_client.get_agent()

    def check_decision(self, d, choices):
        self.assertIn(d["choice"], choices)
        self.assertTrue(math.isfinite(d["confidence"]) and 0 <= d["confidence"] <= 1)
        self.assertEqual(set(d["probabilities"]), set(choices))
        self.assertTrue(all(0 <= v <= 1 for v in d["probabilities"].values()))

    def timed(self, fn, state):
        started = time.perf_counter()
        answer = fn(state)
        return answer, time.perf_counter() - started

    def test_all_policies_real(self):
        report = {}
        answer, dt = self.timed(laya_client.ask, gate_state())
        self.check_decision(answer, jev_client.CRITERIA)
        report["gate_v1"] = round(dt, 3)
        answer, dt = self.timed(laya_client.ask, av_state())
        self.check_decision(answer, jev_client.V2_CRITERIA)
        report["av_v2"] = round(dt, 3)
        answer, dt = self.timed(laya_client.ask, block_state())
        self.assertEqual(len(answer["decisions"]), 49)
        for d in answer["decisions"].values():
            self.check_decision(d, ("execute", "skip"))
        report["block_v3_49q"] = round(dt, 3)
        answer, dt = self.timed(laya_client.ask, layer_state("layer_v5"))
        self.assertEqual(len(answer["decisions"]), 49)
        self.check_decision(answer["budget"], ("2.5", "3", "3.5"))
        for d in answer["decisions"].values():
            self.check_decision(d, ("reduce", "baseline", "protect"))
        report["layer_v5_50q"] = round(dt, 3)
        answer, dt = self.timed(laya_client.native_ask, sla_step_state())
        self.assertEqual(len(answer["decisions"]), 49)
        report["sla_step_49q"] = round(dt, 3)
        answer, dt = self.timed(laya_client.native_ask,
                                {"initialization": True, "prompt": "a cat plays piano", "next_step": 1})
        self.assertEqual(len(answer["decisions"]), 50)
        self.assertIn(float(answer["decisions"]["0"]["choice"]), (5, 10))
        report["sla_init_50q"] = round(dt, 3)
        print("\n[Laya real-checkpoint latency]", json.dumps(report))

    def test_gate_controller_real_engine(self):
        """Full 4-step sampler flow through the engine dispatcher (gate_v1)."""
        p = types.SimpleNamespace(topk_ratio=.05)
        c = AdaptiveController(p, timeout=8.0)
        c.begin([1, .8, .5, .2, 0])
        for i in range(4):
            c.stats = {"block": 0, "samples": 1024, "mean_abs": .5, "std_abs": .1,
                       "top10_mass": .3, "relative_mean_change": None if i == 0 else .05}
            c.completed_step(i)
        self.assertEqual(c.requests, 3)
        applied = [e.get("next_keep_percent") for e in c.events if e["event"] == "step"][:3]
        self.assertTrue(all(v in (5.0, 7.5, 10.0, 15.0, 20.0) for v in applied))
        reasons = [e.get("reason") for e in c.events if e["event"] == "step"]
        # Every request reaches the engine; whether it clears min_confidence is model
        # behaviour (fallback to 10.0 is the designed safe path), not a transport fault.
        self.assertTrue(all(r in ("jev", "low_confidence", "last_step_no_request") for r in reasons))
        confs = [e.get("answer", {}).get("confidence") for e in c.events if e.get("answer")]
        print("\n[Laya controller] gate_v1 keeps:", applied, "reasons:", reasons, "confs:", confs)

    def test_layer_controller_real_engine(self):
        """layer_v4 flow: 50 block measurements -> per-layer keeps from the real engine."""
        import torch
        from laya_test_package.layer_adaptive import LayerController
        p = types.SimpleNamespace(topk_ratio=.05)
        c = LayerController(p, min_confidence=0.0, max_requests=3)
        c.begin([1, .8, .5, .2, 0])
        for step in range(4):
            c.pending = {b: torch.tensor([b / 50 + .01, -1., .1, .3,
                                          b / 60 + .02, -1., .1, .3]) for b in range(50)}
            c.completed_step(step)
        self.assertEqual(c.requests, 3)
        self.assertEqual(len(c.keeps), 50)
        self.assertEqual(c.keeps[0], 5.0)
        allowed = {1.0, 2.0, 3.0, 5.0, 7.5, 10.0}
        self.assertTrue(all(k in allowed for k in c.keeps))
        print("\n[Laya controller] layer_v4 mean keep: %.2f" % (sum(c.keeps) / 50))


if __name__ == "__main__":
    unittest.main(verbosity=2)
