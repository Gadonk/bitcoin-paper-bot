"""Trading dashboard with local AI connection preferences; no external dependencies."""
import copy
import csv
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import secrets
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit


class Dashboard:
    def __init__(self, settings=None):
        self.settings = settings
        self.lock = threading.Lock()
        self.snapshot = {}
        self.trades = []
        self.ai_history = []

    def update(self, paper, error=None):
        s, c = paper.s, paper.c
        telemetry = s.get("telemetry", {})
        bid = telemetry.get("bid")
        equity = paper.equity(bid) if bid is not None else (s["cash"] if not s["btc"] else None)
        snapshot = {k: s[k] for k in ("cash", "btc", "entry", "peak", "halted")}
        snapshot.update({"config": c, "equity": equity, "error": error,
                         "decision": s.get("decision"), "position_risk": s.get("position_risk"),
                         "ai": s.get("ai"), "ai_reviews": len(s.get("ai_history", [])),
                         "last_attempt": int(time.time()), "updated_at": telemetry.get("updated_at"),
                         "bid": bid, "ask": telemetry.get("ask"),
                         "candles": telemetry.get("candles", []),
                         "history": telemetry.get("history", []),
                         "total_trades": len(s["trades"]), "trades": s["trades"][-100:][::-1],
                         "fees": sum(t["fee"] for t in s["trades"])})
        with self.lock:
            self.snapshot = copy.deepcopy(snapshot)
            self.trades = copy.deepcopy(s["trades"])
            self.ai_history = copy.deepcopy(s.get("ai_history", []))

    def ai_bytes(self):
        with self.lock:
            return json.dumps(self.ai_history, ensure_ascii=False, allow_nan=False).encode("utf-8")

    def json_bytes(self):
        with self.lock:
            snapshot = copy.deepcopy(self.snapshot)
        if self.settings:
            snapshot["ai_connection"] = self.settings.public()
        return json.dumps(snapshot, allow_nan=False).encode("utf-8")

    def csv_bytes(self):
        with self.lock:
            trades = list(self.trades)
        out = io.StringIO(newline="")
        writer = csv.DictWriter(out, fieldnames=["time", "side", "price", "btc", "fee", "reason"])
        writer.writeheader()
        writer.writerows(trades)
        return out.getvalue().encode("utf-8-sig")


def record_market(paper, candles, bid, ask, stamp):
    history = paper.s.get("telemetry", {}).get("history", [])
    history = (history + [[stamp, paper.equity(bid)]])[-1440:]
    paper.s["telemetry"] = {"updated_at": stamp, "bid": bid, "ask": ask,
                            "candles": candles[-300:], "history": history}


def make_server(dashboard, host, port):
    page = (Path(__file__).resolve().parent / "web" / "index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if urlsplit(self.path).path != "/api/ai-settings" or not dashboard.settings:
                self.send_error(404)
                return
            token = self.headers.get("X-AI-Settings-Token", "")
            origin = self.headers.get("Origin")
            if not secrets.compare_digest(token, dashboard.settings.token) or (origin and urlsplit(origin).netloc != self.headers.get("Host")):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("Ugyldigt format")
                payload = json.loads(self.rfile.read(length))
                result = dashboard.settings.update(payload)
                status, body = 200, json.dumps(result).encode()
            except (ValueError, KeyError, TypeError):
                status, body = 400, json.dumps({"error": "Kontrollér AI-adresse, model og API-nøgle."}).encode()
            except OSError:
                status, body = 500, json.dumps({"error": "AI-indstillingerne kunne ikke gemmes."}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                body, mime = page, "text/html; charset=utf-8"
            elif path == "/api/status":
                body, mime = dashboard.json_bytes(), "application/json; charset=utf-8"
            elif path == "/trades.csv":
                body, mime = dashboard.csv_bytes(), "text/csv; charset=utf-8"
            elif path == "/ai-decisions.json":
                body, mime = dashboard.ai_bytes(), "application/json; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            if path == "/trades.csv":
                self.send_header("Content-Disposition", 'attachment; filename="paper-trades.csv"')
            elif path == "/ai-decisions.json":
                self.send_header("Content-Disposition", 'attachment; filename="ai-decisions.json"')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


@contextmanager
def serve(dashboard, host, port):
    if not port:
        yield
        return
    server = make_server(dashboard, host, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Webinterface: http://{host}:{server.server_port}", flush=True)
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
