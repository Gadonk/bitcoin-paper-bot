import unittest
from unittest.mock import patch
from bot import Paper, ROOT, config
from strategy import Candles, aggregate, evaluate, indicators


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.c = config(ROOT / "config-v2.json")
        self.p = Paper(self.c)

    def test_partial_hour_cannot_change_hour_indicator(self):
        bars = [{"start": i * 300, "open": 100, "high": 102, "low": 98,
                 "close": 100, "volume": 1} for i in range(13)]
        hours = aggregate(bars, 3600, 300)
        self.assertEqual(len(hours), 1)
        self.assertEqual(hours[0]["volume"], 12)
        bars.pop(5)
        self.assertEqual(aggregate(bars, 3600, 300), [])

    def test_flat_low_volatility_market_does_not_trade(self):
        bars = [{"start": 180000 + i * 300, "open": 100, "high": 100.01,
                 "low": 99.99, "close": 100, "volume": 1} for i in range(150)]
        result = evaluate(Candles(bars), self.c, 100, 100.01)
        self.assertTrue(result["ready"])
        self.assertFalse(result["entry_ok"])
        self.assertEqual(result["rsi"], 50)
        self.assertFalse(next(x["ok"] for x in result["checks"] if "handelsomkostninger" in x["label"]))
        self.p.tick(Candles(bars), 100, 100.01, 300000)
        self.assertEqual(self.p.s["trades"], [])

    def test_risk_exit_on_same_candle_and_persisted_cooldown(self):
        decision = {"ready": True, "bearish": False, "entry_ok": True,
                    "atr": 1, "stop_fraction": .015, "target_fraction": .04}
        with patch("bot.evaluate", return_value=decision.copy()):
            self.p.tick([(300000, 100)], 100, 100.01, 300300)
        self.assertGreater(self.p.s["btc"], 0)
        with patch("bot.evaluate", return_value=decision.copy()):
            self.p.tick([(300000, 100)], 97, 97.01, 300360)
            self.assertEqual(self.p.s["btc"], 0)
            restarted = Paper(self.c, self.p.s)
            restarted.tick([(300600, 100)], 100, 100.01, 300900)
        self.assertEqual(len(restarted.s["trades"]), 2)
        self.assertEqual(restarted.s["trades"][-1]["reason"], "atr_stop")

    def test_confirmed_exit_requires_new_candles_and_minimum_hold(self):
        risk = {"atr": 1, "stop_fraction": .02, "target_fraction": .08}
        self.p.buy(100, 300000, "filtered_breakout_entry", risk)
        decision = {"ready": True, "bearish": True, "entry_ok": False}
        with patch("bot.evaluate", return_value=decision.copy()):
            self.p.tick([(300000, 100)], 100, 100.01, 300300)
            self.p.tick([(300000, 100)], 100, 100.01, 300360)
            self.assertEqual(self.p.s["bearish_count"], 1)
            self.p.tick([(301500, 100)], 100, 100.01, 301800)
        self.assertEqual(self.p.s["btc"], 0)
        self.assertEqual(self.p.s["trades"][-1]["reason"], "confirmed_trend_exit")
