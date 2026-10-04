import unittest
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from bot import Paper, ROOT, config, session_lock


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.c = config(ROOT / "config.json")
        self.c["initial_cash"] = 10000
        self.p = Paper(self.c)
        self.candles = [(1000 + i * 60, 100 + i) for i in range(20)]

    def test_fees_and_duplicate_signal(self):
        self.p.tick(self.candles, 119, 120, 2300)
        self.assertAlmostEqual(self.p.s["cash"], 9000)
        buy = self.p.s["trades"][0]
        self.assertAlmostEqual(buy["btc"] * buy["price"] + buy["fee"], 1000)
        self.p.tick(self.candles, 119, 120, 2301)
        self.assertEqual(len(self.p.s["trades"]), 1)

    def test_stop_between_signals(self):
        self.p.tick(self.candles, 119, 120, 2300)
        self.p.tick(self.candles, 115, 116, 2301)
        self.assertEqual(self.p.s["btc"], 0)
        self.assertEqual(self.p.s["trades"][-1]["reason"], "stop_loss")

    def test_halt_persists_after_restart(self):
        self.p.s["cash"] = 9400
        self.p.tick(self.candles, 119, 120, 2300)
        restarted = Paper(self.c, self.p.s)
        restarted.tick(self.candles + [(2300, 140)], 140, 141, 2400)
        self.assertTrue(restarted.s["halted"])
        self.assertEqual(restarted.s["trades"], [])

    def test_bad_quote_does_not_trade(self):
        with self.assertRaises(ValueError):
            self.p.tick(self.candles, float("nan"), 120, 2300)
        self.assertEqual(self.p.s["trades"], [])

    def test_real_budget_starts_with_500_dkk(self):
        c = config(ROOT / "config.json")
        p = Paper(c)
        self.assertAlmostEqual(p.s["cash"] * c["dkk_per_eur"], 500)
        p.tick(self.candles, 119, 120, 2300)
        self.assertAlmostEqual(p.s["cash"] * c["dkk_per_eur"], 450)
        trade = p.s["trades"][0]
        self.assertAlmostEqual((trade["btc"] * trade["price"] + trade["fee"]) * c["dkk_per_eur"], 50)

    def test_lock_released_after_process_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "paper.lock")
            code = "from bot import session_lock; import sys,time;\nwith session_lock(sys.argv[1]):\n print('locked',flush=True)\n time.sleep(30)"
            proc = subprocess.Popen([sys.executable, "-c", code, path], cwd=ROOT,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(proc.stdout.readline().strip(), "locked")
                with self.assertRaises(OSError):
                    with session_lock(path):
                        pass
                proc.kill()
                proc.communicate(timeout=5)
                with session_lock(path):
                    self.assertTrue(Path(path).exists())
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=5)

    @unittest.skipIf(os.name == "nt", "SIGTERM handler requires POSIX")
    def test_container_stop_and_halted_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "paper.json"
            # The subprocess reads the real config, not the 10,000 EUR unit-test fixture.
            self.p = Paper(config(ROOT / "config.json"))
            self.p.s["halted"] = True
            state.write_text(json.dumps(self.p.s), encoding="utf-8")
            proc = subprocess.Popen([sys.executable, str(ROOT / "bot.py"), "paper", "--state", str(state), "--web-port", "0"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertIn("PAPER startet", proc.stdout.readline())
                self.assertIn("RISIKOSTOP", proc.stdout.readline())
                proc.terminate()
                stdout, stderr = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 0, stderr)
                self.assertIn("gemt session bevares", stdout)
                self.assertEqual(json.loads(state.read_text(encoding="utf-8")), self.p.s)
                with session_lock(state.with_suffix(".lock")):
                    pass
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=5)

    def test_halted_session_loads_in_real_process(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "paper.json"
            paper = Paper(config(ROOT / "config.json"))
            paper.s["halted"] = True
            state.write_text(json.dumps(paper.s), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "bot.py"), "paper", "--state", str(state),
                 "--web-port", "0", "--once"], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PAPER startet", result.stdout)
            self.assertIn("RISIKOSTOP", result.stdout)
            self.assertEqual(json.loads(state.read_text(encoding="utf-8")), paper.s)


if __name__ == "__main__":
    unittest.main()
