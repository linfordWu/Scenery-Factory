# SPDX-License-Identifier: GPL-3.0-only
"""OpenJev engine tests: offline contract tests against a stub llama-server.

The stub emulates /v1/chat/completions (and /health) so no GPU or GGUF is needed.
OpenJev answers one question per request with a bare option key.
"""
import json
import os
import pathlib
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = pathlib.Path(__file__).parent
pkg = types.ModuleType("openjev_test_package")
pkg.__path__ = [str(ROOT)]
sys.modules[pkg.__name__] = pkg
from openjev_test_package import openjev_client  # noqa: E402
from openjev_test_package.adaptive import ask as engine_ask  # noqa: E402


class StubHandler(BaseHTTPRequestHandler):
    replies = []       # per-question replies, popped in order
    default = "5"      # used when replies run out
    requests = []

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        type(self).requests.append(body)
        content = type(self).replies.pop(0) if type(self).replies else type(self).default
        payload = {"choices": [{"message": {"content": content}}],
                   "usage": {"prompt_tokens": 100, "completion_tokens": 2,
                             "prompt_tokens_details": {"cached_tokens": 80}}}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def sla_state():
    return {"step_measured": 1, "next_step": 2, "choices_percent": [1, 3, 5, 10],
            "protected": "block0 keep5", "objective": "reduce compute",
            "limitations": "proxies", "current_keep": [10.0] * 50,
            "blocks": {str(b): {"audio": {"residual_relative_l2": 0.01, "rank": 0.5,
                                          "cross_step_change": None},
                                "video": {"residual_relative_l2": 0.02, "rank": 0.5,
                                          "cross_step_change": None}}
                       for b in range(50)}}


class ServerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        StubHandler.replies = []
        StubHandler.default = "5"
        StubHandler.requests = []
        self.env = patch.dict(os.environ, {"OPENJEV_URL": self.url, "OPENJEV_RETRIES": "1",
                                           "H3_DECISION_ENGINE": "openjev"})
        self.env.start()

    def tearDown(self):
        self.env.stop()


class TestNativeAsk(ServerTestBase):
    def test_bare_choice_replies(self):
        StubHandler.replies = ["10"] + ["3"] * 49
        out = openjev_client.native_ask(dict(sla_state(), initialization=True))
        self.assertEqual(len(out["decisions"]), 50)
        self.assertEqual(len(StubHandler.requests), 50)  # one request per question
        self.assertEqual(out["decisions"]["0"]["choice"], "10")
        self.assertEqual(out["decisions"]["1"]["choice"], "3")
        self.assertEqual(out["model"], "openjev-local")
        self.assertEqual(out["usage"]["engine"], "openjev")
        self.assertEqual(out["usage"]["cached_tokens"], 80 * 50)
        self.assertGreater(out["decisions"]["1"]["confidence"], 0.5)

    def test_wrapped_reply_is_parsed(self):
        StubHandler.replies = ['the answer is ```json\n{"choice": "10"}\n```']
        out = openjev_client.native_ask(sla_state())
        self.assertEqual(out["decisions"]["1"]["choice"], "10")

    def test_prose_reply_extracts_option(self):
        StubHandler.replies = ["I choose 3 for this block."]
        out = openjev_client.native_ask(sla_state())
        self.assertEqual(out["decisions"]["1"]["choice"], "3")

    def test_invalid_reply_retries_then_falls_back(self):
        StubHandler.replies = ["garbage", "also garbage"]
        out = openjev_client.native_ask(sla_state())
        self.assertEqual(StubHandler.replies, [])
        self.assertEqual(out["decisions"]["1"]["choice"], "5")  # safe default
        self.assertEqual(out["decisions"]["2"]["choice"], "5")  # stub default

    def test_thinking_disabled_by_default(self):
        StubHandler.replies = ["5"]
        openjev_client.native_ask(sla_state())
        body = StubHandler.requests[0]
        self.assertFalse(body["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(body["temperature"], 0.0)
        self.assertTrue(body["cache_prompt"])

    def test_state_is_prompt_prefix_for_caching(self):
        StubHandler.replies = ["5"]
        openjev_client.native_ask(sla_state())
        prompt = StubHandler.requests[0]["messages"][1]["content"]
        self.assertLess(prompt.index("State:"), prompt.index("Question:"))

    def test_compact_state_shrinks_blocks(self):
        StubHandler.replies = ["5"]
        openjev_client.native_ask(sla_state())
        prompt = StubHandler.requests[0]["messages"][1]["content"]
        self.assertIn("block_metric_legend", prompt)
        self.assertNotIn('"residual_relative_l2":', prompt.split("Question:")[0])

    def test_full_state_mode(self):
        StubHandler.replies = ["5"]
        with patch.dict(os.environ, {"OPENJEV_STATE_MODE": "full"}):
            openjev_client.native_ask(sla_state())
        self.assertIn('"residual_relative_l2":', StubHandler.requests[0]["messages"][1]["content"])


class TestPolicyDispatch(ServerTestBase):
    def layer_state(self, policy="layer_v5"):
        return {"policy": policy, "measured_step": 1, "next_step": 2, "total_steps": 4,
                "blocks": {str(b): {"audio": {"residual_relative_l2": 0.01},
                                    "video": {"residual_relative_l2": 0.02}}
                           for b in range(50)}}

    def test_layer_v5_includes_budget(self):
        StubHandler.default = "baseline"
        StubHandler.replies = ["reduce"]  # first question; order is b01..b49 then budget
        out = engine_ask(self.layer_state(), "", 8.0)
        self.assertEqual(len(out["decisions"]), 49)
        self.assertEqual(out["decisions"]["b01"]["choice"], "reduce")
        self.assertEqual(out["budget"]["choice"], "3")  # default "baseline" invalid for budget -> middle key
        self.assertEqual(out["model"], "openjev-local")

    def test_block_v3(self):
        StubHandler.default = "execute"
        out = engine_ask(self.layer_state("block_v3"), "", 8.0)
        self.assertEqual(len(out["decisions"]), 49)
        self.assertEqual(out["decisions"]["b01"]["choice"], "execute")

    def test_gate_keep_question(self):
        StubHandler.default = "10"
        out = engine_ask({"policy": "gate_v1", "gate_stats": {}}, "", 8.0)
        self.assertEqual(out["choice"], "10")
        self.assertEqual(out["model"], "openjev-local")


if __name__ == "__main__":
    unittest.main()
