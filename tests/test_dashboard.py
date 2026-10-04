import json
from pathlib import Path
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen
from bot import Paper, ROOT, config
from dashboard import Dashboard, make_server, record_market


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.paper = Paper(config(ROOT / "config.json"))
        self.paper.c["initial_cash"] = 10000
        self.paper.s["cash"] = self.paper.s["peak"] = 10000
        self.dashboard = Dashboard()

    def test_old_session_and_error_keep_last_good_measurement(self):
        self.dashboard.update(self.paper)
        first = json.loads(self.dashboard.json_bytes())
        self.assertEqual(first["equity"], 10000)
        self.assertIsNone(first["updated_at"])
        record_market(self.paper, [(60, 100)], 100, 101, 120)
        self.dashboard.update(self.paper, "forbindelsesfejl")
        result = json.loads(self.dashboard.json_bytes())
        self.assertEqual(result["updated_at"], 120)
        self.assertEqual(result["error"], "forbindelsesfejl")
        self.assertEqual(result["bid"], 100)

    def test_history_bounded_and_snapshot_detached(self):
        for i in range(1450):
            record_market(self.paper, [(i, 100)], 100, 101, i)
        self.dashboard.update(self.paper)
        self.paper.s["telemetry"]["history"][-1][1] = 5
        result = json.loads(self.dashboard.json_bytes())
        self.assertEqual(len(result["history"]), 1440)
        self.assertEqual(result["history"][-1][1], 10000)

    def test_server_only_exposes_dashboard_status_and_csv(self):
        self.dashboard.update(self.paper)
        server = make_server(self.dashboard, "127.0.0.1", 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base + "/", timeout=3) as response:
                self.assertIn("Din paper-bot", response.read().decode())
            with urlopen(base + "/api/status", timeout=3) as response:
                self.assertEqual(json.load(response)["cash"], 10000)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            with urlopen(base + "/trades.csv", timeout=3) as response:
                self.assertIn(b"time,side,price,btc,fee,reason", response.read())
            with urlopen(base + "/ai-decisions.json", timeout=3) as response:
                self.assertEqual(json.load(response), [])
                self.assertIn("attachment", response.headers["Content-Disposition"])
            with self.assertRaises(HTTPError) as error:
                urlopen(base + "/../config.json", timeout=3)
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
