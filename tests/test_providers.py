# SPDX-License-Identifier: GPL-3.0-only
"""Provider-plugging tests for the text model client (offline stub server)."""
import json
import os
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if "svf" not in sys.modules:
    _pkg = types.ModuleType("svf")
    _pkg.__path__ = [ROOT]
    sys.modules["svf"] = _pkg

from svf.adapters.text_model.client import TextModelClient
from svf.config import settings


class StubHandler(BaseHTTPRequestHandler):
    last_headers = {}
    last_body = {}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        StubHandler.last_headers = dict(self.headers)
        StubHandler.last_body = body
        data = json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class TestProviders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _call(self, provider, base):
        with patch.dict(os.environ, {"TEXT_PROVIDERS_TEST": "1"}):
            # settings imported at module top as svf.config.settings
            settings.TEXT_PROVIDERS[provider]["base"] = base
            c = TextModelClient(provider=provider)
            return c.chat_json("i", {"x": 1}, '{"ok": true}')

    def test_ollama_default_no_auth(self):
        out = self._call("ollama", self.base)
        self.assertTrue(out["ok"])
        self.assertNotIn("Authorization", StubHandler.last_headers)
        self.assertEqual(StubHandler.last_body["model"], "gemma3:4b")

    def test_kimi_sends_bearer_and_fixed_temperature(self):
        with patch.dict(os.environ, {"KIMI_API_KEY": "sk-test-key"}):
            out = self._call("kimi", self.base)
        self.assertTrue(out["ok"])
        self.assertEqual(StubHandler.last_headers.get("Authorization"), "Bearer sk-test-key")
        self.assertEqual(StubHandler.last_body["temperature"], 1.0)  # provider-fixed

    def test_kimi_missing_key_raises(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KIMI_API_KEY", None)
            with self.assertRaises(RuntimeError):
                self._call("kimi", self.base)

    def test_deepseek_sends_bearer_and_default_model(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-deepseek"}):
            out = self._call("deepseek", self.base)
        self.assertTrue(out["ok"])
        self.assertEqual(StubHandler.last_headers.get("Authorization"),
                         "Bearer sk-deepseek")
        self.assertEqual(StubHandler.last_body["model"], "deepseek-flash")

    def test_deepseek_missing_key_raises(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        with self.assertRaises(RuntimeError):
            self._call("deepseek", self.base)

    def test_dotenv_loader_keeps_real_env(self):
        import pathlib
        import tempfile

        from svf.config.settings import _load_dotenv

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / ".env"
            path.write_text('SVF_TEST_FROM_FILE="quoted value"\n'
                            "# comment\n\nSVF_TEST_PLAIN=plain\n",
                            encoding="utf-8")
            os.environ["SVF_TEST_FROM_FILE"] = "from-env"
            try:
                _load_dotenv(path)
                self.assertEqual(os.environ["SVF_TEST_FROM_FILE"], "from-env")
                self.assertEqual(os.environ["SVF_TEST_PLAIN"], "plain")
            finally:
                os.environ.pop("SVF_TEST_FROM_FILE", None)
                os.environ.pop("SVF_TEST_PLAIN", None)

    def test_custom_provider(self):
        settings.TEXT_PROVIDERS["custom"]["base"] = self.base
        settings.TEXT_PROVIDERS["custom"]["model"] = "my-model"
        with patch.dict(os.environ, {"SVF_CUSTOM_API_KEY": "k"}):
            c = TextModelClient(provider="custom")
            out = c.chat_json("i", {}, '{"ok": true}')
        self.assertTrue(out["ok"])
        self.assertEqual(StubHandler.last_body["model"], "my-model")


if __name__ == "__main__":
    unittest.main()
