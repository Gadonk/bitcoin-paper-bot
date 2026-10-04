import unittest
from unittest.mock import patch
from bot import ROOT, config
from backtest import simulate, validate


class BacktestTests(unittest.TestCase):
    def test_signal_cannot_see_current_or_future_bar(self):
        bars = [{"start": i * 60, "open": 100, "high": 110, "low": 90,
                 "close": 109, "volume": 1} for i in range(15)]
        five = [{"start": i * 300, "open": 100, "high": 110, "low": 90,
                 "close": 109, "volume": 5} for i in range(3)]
        captured = []
        def tick(paper, closed, bid, ask, stamp):
            captured.append((stamp, list(closed)))
            self.assertTrue(all(t + 300 <= stamp for t, _ in closed))
        with patch("backtest.Paper.tick", new=tick):
            result = simulate(bars, 0, 900, config(ROOT / "config-v2.json"), five_minute=five)
        self.assertEqual(captured[0][1], [])
        self.assertEqual(captured[5][1], [(0, 109)])
        self.assertEqual(result["equity_dkk"], 500)

    def test_gaps_reported_without_synthetic_prices(self):
        bars = [{"start": t, "open": 100, "high": 100, "low": 100,
                 "close": 100, "volume": 0} for t in (0, 120)]
        self.assertEqual(validate(bars, 0, 180), 1)
        self.assertEqual(len(bars), 2)
