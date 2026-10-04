import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
import unittest
from unittest.mock import patch
from ai_review import Reviewer, review, usable, validate_plan
from bot import Paper, ROOT, config
from dashboard import Dashboard
from strategy import Candles


class FakeReviewer:
    def __init__(self):
        self.result = None
        self.submissions = []

    def snapshot(self):
        return copy.deepcopy(self.result)

    def submit(self, observation):
        self.submissions.append(observation)


class AITests(unittest.TestCase):
    def setUp(self):
        self.c = config(ROOT / "config-v3.json")
        self.p = Paper(self.c)
        self.p.reviewer = FakeReviewer()
        self.candles = Candles([{"start": 300000, "open": 100, "high": 101,
                                "low": 99, "close": 100, "volume": 1}])
        self.decision = {"ready": True, "bearish": False, "entry_ok": True,
                         "checks": [{"label": "Test", "ok": True}],
                         "fast": 100, "slow": 99, "rsi": 60, "atr": 1,
                         "volume_ratio": 1.2, "breakout": 99, "cost_fraction": .014,
                         "stop_fraction": .02, "target_fraction": .08}

    def result(self, action="BUY", **changes):
        r = {"status": "ok", "action": action, "reason": "Test", "model": self.c["ai"]["model"],
             "allocation": .15, "stop_fraction": .02, "target_fraction": .08,
             "candle": 300000, "requested_at": 300300, "ask": 100.01,
             "completed_at": 300310, "duration_seconds": 10, "input": {"position_open": False}}
        r.update(changes)
        if action != "BUY":
            r.update(allocation=0, stop_fraction=0, target_fraction=0)
        return r

    def tick(self, stamp=300360, bid=100, ask=100.01):
        with patch("bot.evaluate", return_value=copy.deepcopy(self.decision)):
            self.p.tick(self.candles, bid, ask, stamp)

    def test_pending_then_allowed_same_candle_with_fresh_quote(self):
        self.tick(stamp=300300)
        self.assertFalse(self.p.s["btc"])
        self.p.reviewer.result = self.result()
        self.tick()
        self.assertGreater(self.p.s["btc"], 0)
        self.assertEqual(self.p.s["trades"][0]["reason"], "ai_approved_entry")
        self.tick(stamp=300420)
        self.assertEqual(len(self.p.s["trades"]), 1)
        self.assertEqual(len(self.p.s["ai_history"]), 1)

    def test_wait_error_old_candle_expired_and_price_drift_block(self):
        for r in (self.result("WAIT"), self.result(status="error"),
                  self.result(candle=299700), self.result(requested_at=300000),
                  self.result(ask=99)):
            with self.subTest(result=r):
                self.p.reviewer.result = r
                self.tick()
                self.assertEqual(self.p.s["trades"], [])

    def test_ai_can_buy_despite_v2_filters_but_cannot_override_halt(self):
        self.p.reviewer.result = self.result()
        self.decision["entry_ok"] = False
        self.tick()
        self.assertGreater(self.p.s["btc"], 0)
        self.assertAlmostEqual(self.p.s["position_risk"]["allocation"], .15)
        self.p.s["halted"] = True
        self.tick()
        self.assertFalse(self.p.s["btc"])
        self.assertEqual(self.p.s["trades"][-1]["reason"], "drawdown")

    def test_stop_and_drawdown_work_without_ai_and_cooldown_survives_restart(self):
        for bid, expected in ((97, "ai_plan_stop"), (1, "drawdown")):
            with self.subTest(bid=bid):
                self.p = Paper(self.c)
                self.p.reviewer = FakeReviewer()
                self.p.buy(100, 300000, "test", self.decision, allocation=.30)
                self.tick(bid=bid, ask=bid+.01)
                self.assertFalse(self.p.s["btc"])
                self.assertEqual(self.p.s["trades"][-1]["reason"], expected)
                restarted = Paper(self.c, json.loads(json.dumps(self.p.s)))
                restarted.reviewer = FakeReviewer()
                restarted.reviewer.result = self.result()
                with patch("bot.evaluate", return_value=copy.deepcopy(self.decision)):
                    restarted.tick(self.candles, 100, 100.01, 300420)
                self.assertFalse(restarted.s["btc"])

    def test_restart_does_not_use_persisted_buy_without_new_review(self):
        self.p.s["ai"] = self.result()
        self.tick()
        self.assertFalse(self.p.s["btc"])

    def test_background_single_flight_and_failure_handled(self):
        entered, release = threading.Event(), threading.Event()
        observation = {"candle": 1, "observed_at": int(time.time()), "ask": 100}
        def blocked(*args):
            entered.set()
            release.wait(2)
            raise ValueError("invalid output")
        with patch("ai_review.review", side_effect=blocked) as call:
            worker = Reviewer(self.c["ai"])
            worker.submit(observation)
            self.assertTrue(entered.wait(1))
            self.assertIsNone(worker.snapshot())
            worker.submit(dict(observation, candle=2))
            self.assertEqual(call.call_count, 1)
            release.set()
            for _ in range(100):
                if worker.snapshot():
                    break
                time.sleep(.01)
            self.assertEqual(worker.snapshot()["status"], "error")
            self.assertFalse(usable(worker.snapshot(), 1, 100, observation["observed_at"], self.c["ai"]))
            worker.submit(observation)
            self.assertEqual(call.call_count, 1)

    def test_real_http_contract_and_malformed_response_rejected(self):
        responses = [json.dumps({"action": "WAIT", "reason": "Afvent", "allocation": 0, "stop_fraction": 0, "target_fraction": 0}),
                     '{"action":"BUY","reason":"missing risk"}',
                     '{"action":"BUY","reason":"ok","allocation":1,"stop_fraction":NaN,"target_fraction":0.1}', 'not json']
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                body = json.dumps({"done": True, "done_reason": "stop", "model": "test",
                                   "message": {"content": responses.pop(0)}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            settings = dict(self.c["ai"], url=f"http://127.0.0.1:{server.server_port}")
            self.assertEqual(review(settings, {"checks": []})["action"], "WAIT")
            for _ in range(3):
                with self.assertRaises(ValueError):
                    review(settings, {"checks": []})
            path, request = requests[0]
            self.assertEqual(path, "/api/chat")
            self.assertFalse(request["stream"])
            self.assertFalse(request["think"])
            self.assertEqual(request["format"]["properties"]["action"]["enum"], ["BUY", "WAIT", "HOLD", "SELL"])
            self.assertNotIn("tools", request)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_ai_export_is_detached_and_contains_observation(self):
        self.p.reviewer.result = self.result(input={"checks": [{"ok": False}]})
        self.tick()
        d = Dashboard()
        d.update(self.p)
        self.p.s["ai_history"].clear()
        self.assertEqual(json.loads(d.ai_bytes())[0]["input"]["checks"], [{"ok": False}])

    def test_container_ai_settings_override_json_and_validate(self):
        with patch.dict(os.environ, {"AI_BASE_URL": "http://192.168.1.10:11434", "AI_MODEL": "qwen3.5:4b", "AI_CONTEXT": "2048"}):
            c = config(ROOT / "config-v3.json")
            self.assertEqual(c["ai"]["url"], "http://192.168.1.10:11434")
            self.assertEqual(c["ai"]["model"], "qwen3.5:4b")
            self.assertEqual(c["ai"]["num_ctx"], 2048)
            with patch.dict(os.environ, {"AI_CONTEXT": "100000"}):
                with self.assertRaises(ValueError):
                    config(ROOT / "config-v3.json")

    def test_reject_excess_exposure_loss_and_bad_net_reward(self):
        for changes in ({"allocation": .8}, {"stop_fraction": .1},
                        {"allocation": .30, "stop_fraction": .06}, {"target_fraction": .005}):
            with self.subTest(changes=changes):
                self.p.reviewer.result = self.result(**changes)
                self.tick()
                self.assertFalse(self.p.s["btc"])
                self.assertEqual(self.p.s["decision"]["ai_status"], "plan_rejected")
        self.assertIsNotNone(validate_plan(self.result(), self.c, 100, 100, 100, 101))

    def test_ai_sell_is_position_bound_and_hold_does_not_trigger_v2_exit(self):
        self.p.buy(100, 300000, "test", self.decision)
        self.decision["bearish"] = True
        self.p.reviewer.result = self.result("SELL", input={"position_open": False})
        self.tick()
        self.assertGreater(self.p.s["btc"], 0)
        self.p.reviewer.result = self.result("HOLD", input={"position_open": True, "account": {"position_risk": {"opened": 300000}}})
        self.tick()
        self.assertGreater(self.p.s["btc"], 0)
        self.p.reviewer.result["action"] = "SELL"
        self.tick()
        self.assertFalse(self.p.s["btc"])
        self.assertEqual(self.p.s["trades"][-1]["reason"], "ai_exit")


if __name__ == "__main__":
    unittest.main()
