import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
from ai_settings import AISettings
from bot import Paper, config, ROOT
from dashboard import Dashboard, make_server
import test_ai


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.c = config(ROOT / "config-v3.json")
        self.store = AISettings(Path(self.temp.name) / "ai-settings.json", self.c)

    def tearDown(self):
        self.temp.cleanup()

    def test_preferences_persist_secret_is_not_public_and_blank_keeps_key(self):
        self.store.update({"enabled": True, "url": "http://ollama:11434", "model": "qwen3.5:4b", "api_key": "test-secret"})
        self.assertNotIn("test-secret", json.dumps(self.store.public()))
        self.store.update({"api_key": ""})
        loaded = AISettings(self.store.path, self.c)
        self.assertEqual(loaded.value["api_key"], "test-secret")
        self.store.update({"remove_api_key": True})
        self.assertFalse(self.store.public()["has_api_key"])
        before = self.store.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.update({"url": "file:///etc/passwd"})
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_disabled_does_not_request_ai_but_stop_still_sells(self):
        fixture = test_ai.AITests()
        fixture.setUp()
        paper = fixture.p
        self.store.update({"enabled": False})
        self.store.apply(paper)
        paper.reviewer = test_ai.FakeReviewer()
        paper.buy(100, 300000, "fixture", fixture.decision)
        with patch("bot.evaluate", return_value=fixture.decision.copy()):
            paper.tick(fixture.candles, 97, 97.01, 300360)
        self.assertEqual(paper.s["btc"], 0)
        self.assertEqual(paper.s["trades"][-1]["reason"], "ai_plan_stop")
        self.assertEqual(paper.reviewer.submissions, [])
        self.assertEqual(paper.s["decision"]["ai_status"], "disabled")

    def test_model_change_discards_old_response_without_changing_session_config(self):
        paper = Paper(self.c)
        before = json.dumps(paper.s["config"], sort_keys=True)
        self.store.apply(paper)
        old = paper.reviewer
        old.result = {"action": "BUY"}
        self.store.update({"enabled": True, "model": "qwen3.5:4b", "api_key": "secret"})
        self.store.apply(paper)
        self.assertIsNot(old, paper.reviewer)
        self.assertIsNone(paper.reviewer.snapshot())
        self.assertEqual(json.dumps(paper.s["config"], sort_keys=True), before)
        self.assertNotIn("secret", json.dumps(paper.s))

    def test_http_save_requires_token_and_rejects_other_origin(self):
        d = Dashboard(self.store)
        d.update(Paper(self.c))
        server = make_server(d, "127.0.0.1", 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_port}/api/ai-settings"
        def send(token, origin=None):
            headers = {"Content-Type": "application/json", "X-AI-Settings-Token": token}
            if origin:
                headers["Origin"] = origin
            return urlopen(Request(url, data=json.dumps({"enabled": True, "api_key": "secret"}).encode(), headers=headers), timeout=3)
        try:
            for token, origin in (("wrong", None), (self.store.token, "http://other-site.example")):
                with self.assertRaises(HTTPError) as error:
                    send(token, origin)
                self.assertEqual(error.exception.code, 403)
            with send(self.store.token) as response:
                result = json.load(response)
                self.assertTrue(result["has_api_key"])
                self.assertNotIn("secret", json.dumps(result))
            self.assertNotIn("secret", d.json_bytes().decode())
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)
