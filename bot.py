"""Coinbase paper trader. Only public GET requests; no real order support."""
import argparse
import copy
from contextlib import contextmanager
import csv
import json
import math
import os
from pathlib import Path
import random
import signal
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from dashboard import Dashboard, record_market, serve
from contextlib import ExitStack
from strategy import Candles, evaluate
from ai_review import Reviewer, make_observation, usable, validate_settings, validate_plan

ROOT = Path(__file__).resolve().parent
STOP = threading.Event()


@contextmanager
def session_lock(path):
    """OS releases the lock after SIGKILL too. Keep the inode on disk."""
    handle = Path(path).open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def config(path):
    c = json.loads(Path(path).read_text(encoding="utf-8"))
    if "initial_cash_dkk" in c:
        if c["product"] != "BTC-EUR":
            raise ValueError("DKK-budget kræver BTC-EUR")
        for key in ("initial_cash_dkk", "dkk_per_eur"):
            if not math.isfinite(c[key]) or c[key] <= 0:
                raise ValueError(key)
        c["initial_cash"] = c["initial_cash_dkk"] / c["dkk_per_eur"]
    if c["product"] not in ("BTC-EUR", "BTC-USD"):
        raise ValueError("Produkt skal være BTC-EUR eller BTC-USD")
    if c.get("strategy", "sma_v1") not in ("sma_v1", "trend_v2", "ai_v3"):
        raise ValueError("Ukendt strategi")
    if c.get("strategy") == "ai_v3":
        c["ai"] = dict(c["ai"])
        for env_key, config_key in (("AI_BASE_URL", "url"), ("AI_MODEL", "model")):
            if os.environ.get(env_key, "").strip():
                c["ai"][config_key] = os.environ[env_key].strip()
        if os.environ.get("AI_CONTEXT", "").strip():
            c["ai"]["num_ctx"] = int(os.environ["AI_CONTEXT"])
        validate_settings(c)
    for key in ("initial_cash", "poll_seconds"):
        if not math.isfinite(c[key]) or c[key] <= 0:
            raise ValueError(key)
    for key in ("allocation", "fee_rate", "slippage", "stop_loss", "take_profit", "max_drawdown"):
        if not math.isfinite(c[key]) or not 0 < c[key] < 1:
            raise ValueError(key)
    if not 1 <= c["fast_window"] < c["slow_window"] <= 300:
        raise ValueError("Ugyldige SMA-vinduer")
    if any(type(c[k]) is not int for k in ("fast_window", "slow_window")):
        raise ValueError("SMA-vinduer skal være heltal")
    return c


def public_get(product, endpoint, params):
    url = f"https://api.coinbase.com/api/v3/brokerage/market/products/{product}/{endpoint}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "BitcoinPaperBot/0.1", "Cache-Control": "no-cache"})
    with urlopen(req, timeout=15) as response:
        return json.load(response)


def market(c):
    now = int(time.time())
    step = 300 if c.get("strategy") in ("trend_v2", "ai_v3") else 60
    end = now // step * step
    data = public_get(c["product"], "candles", {
        "start": end - 300 * step, "end": end,
        "granularity": "FIVE_MINUTE" if step == 300 else "ONE_MINUTE", "limit": 300})
    bars = sorted({int(x["start"]): {"start": int(x["start"]), **{k: float(x[k]) for k in ("open", "high", "low", "close", "volume")}}
                   for x in data["candles"] if end - 300 * step <= int(x["start"]) < end}.values(), key=lambda b: b["start"])
    for b in bars:
        if not all(math.isfinite(b[k]) and b[k] > 0 for k in ("open", "high", "low", "close")) or not math.isfinite(b["volume"]) or b["volume"] < 0 or not b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]:
            raise ValueError("Ugyldige OHLCV-data")
    candles = Candles(bars)
    if len(candles) < c["slow_window"] or end - candles[-1][0] > step * 2:
        raise ValueError("For få eller for gamle candles")
    recent = candles[-120:] if step == 300 else candles[-c["slow_window"]:]
    if any(b[0] - a[0] != step for a, b in zip(recent, recent[1:])):
        raise ValueError("Huller i candle-data")
    ticker = public_get(c["product"], "ticker", {"limit": 1})
    bid, ask = float(ticker["best_bid"]), float(ticker["best_ask"])
    if not 0 < bid <= ask or not math.isfinite(ask):
        raise ValueError("Ugyldig bid/ask")
    return candles, bid, ask


class Paper:
    def __init__(self, c, state=None):
        self.c = c
        self.ai_enabled = True
        self.ai_runtime = c.get("ai", {})
        self.reviewer = Reviewer(c["ai"]) if c.get("strategy") == "ai_v3" else None
        self.s = state or {"config": c, "cash": c["initial_cash"], "btc": 0,
                           "entry": 0, "peak": c["initial_cash"], "halted": False,
                           "last_signal": 0, "trades": []}
        old, new = copy.deepcopy(self.s["config"]), copy.deepcopy(c)
        if old.get("strategy") == new.get("strategy") == "ai_v3":
            for value in (old, new):
                for key in ("url", "model"):
                    value["ai"].pop(key, None)
        if old != new:
            raise ValueError("Konfiguration ændret. Brug en ny state-fil til et nyt forsøg.")

    def equity(self, bid):
        return self.s["cash"] + self.s["btc"] * bid * (1 - self.c["slippage"]) * (1 - self.c["fee_rate"])

    def sell(self, bid, stamp, reason):
        s, c = self.s, self.c
        price = bid * (1 - c["slippage"])
        gross = s["btc"] * price
        fee = gross * c["fee_rate"]
        s["trades"].append({"time": stamp, "side": "SELL", "price": price,
                            "btc": s["btc"], "fee": fee, "reason": reason})
        s["cash"] += gross - fee
        s["btc"] = s["entry"] = 0
        s["last_exit"] = stamp
        s.pop("position_risk", None)

    def buy(self, ask, stamp, reason, risk=None, allocation=None):
        s, c = self.s, self.c
        budget = s["cash"] * (c["allocation"] if allocation is None else allocation)
        price = ask * (1 + c["slippage"])
        gross = budget / (1 + c["fee_rate"])
        fee = budget - gross
        s["btc"], s["entry"] = gross / price, price
        s["cash"] -= budget
        if risk:
            s["position_risk"] = {"stop": price * (1 - risk["stop_fraction"]),
                                  "target": price * (1 + risk["target_fraction"]),
                                  "atr": risk["atr"], "high": price, "opened": stamp}
            if allocation is not None:
                s["position_risk"]["allocation"] = allocation
        s["trades"].append({"time": stamp, "side": "BUY", "price": price,
                            "btc": s["btc"], "fee": fee, "reason": reason})

    def tick_v2(self, candles, bid, ask, stamp):
        s, c = self.s, self.c
        if s["btc"]:
            risk = s["position_risk"]
            risk["high"] = max(risk["high"], bid)
            cost = (1 + c["fee_rate"]) / ((1 - c["fee_rate"]) * (1 - c["slippage"])) - 1
            if risk["high"] >= s["entry"] * (1 + cost * 1.2):
                risk["stop"] = max(risk["stop"], risk["high"] - 3 * risk["atr"])
            reason = ("drawdown" if s["halted"] else "atr_stop" if bid <= risk["stop"] else
                      "atr_target" if bid >= risk["target"] else
                      "time_exit" if stamp - risk["opened"] >= 12 * 3600 else None)
            if reason:
                self.sell(bid, stamp, reason)
        decision = evaluate(candles, c, bid, ask)
        cooldown = max(0, 1800 - (stamp - s.get("last_exit", 0)))
        decision["cooldown_seconds"] = cooldown
        s["decision"] = decision
        if not candles or not decision["ready"]:
            return
        new_signal = candles[-1][0] > s["last_signal"]
        if not new_signal:
            return
        if new_signal:
            s["last_signal"] = candles[-1][0]
            s["bearish_count"] = s.get("bearish_count", 0) + 1 if decision["bearish"] else 0
        if new_signal and s["btc"] and s["bearish_count"] >= 2 and stamp - s["position_risk"]["opened"] >= 1800:
            self.sell(bid, stamp, "confirmed_trend_exit")
        elif not s["btc"] and not s["halted"] and not cooldown and decision["entry_ok"]:
            self.buy(ask, stamp, "filtered_breakout_entry", decision)

    def tick_v3(self, candles, bid, ask, stamp):
        s, c, b = self.s, self.c, self.c["ai_risk"]
        if s["btc"]:
            risk = s["position_risk"]
            reason = ("drawdown" if s["halted"] else "ai_plan_stop" if bid <= risk["stop"] else
                      "ai_plan_target" if bid >= risk["target"] else
                      "time_exit" if stamp-risk["opened"] >= b["max_hold_seconds"] else None)
            if reason:
                self.sell(bid, stamp, reason)
        decision = evaluate(candles, c, bid, ask)
        decision["name"] = "Selvstændig AI v3"
        decision["reference_entry_ok"] = decision.get("entry_ok", False)
        decision["entry_ok"] = False
        decision["cooldown_seconds"] = max(0, b["cooldown_seconds"]-(stamp-s.get("last_exit", 0)))
        s["decision"] = decision
        r = self.update_ai(candles, decision, bid, ask, stamp)
        if not decision["ready"] or s["halted"]:
            return
        if not usable(r, candles[-1][0], ask, stamp, self.ai_runtime):
            return
        # A cached opinion must belong to the same position context as the current account.
        context = r.get("input", {})
        if bool(s["btc"]) != context.get("position_open") or (s["btc"] and (context.get("account", {}).get("position_risk") or {}).get("opened") != s["position_risk"]["opened"]):
            decision["ai_status"] = "context_changed"
            return
        if s["btc"] and r["action"] == "SELL":
            self.sell(bid, stamp, "ai_exit")
            if s.get("ai_history"):
                s["ai_history"][-1]["execution"] = {"time": stamp, "side": "SELL", "trade_index": len(s["trades"])-1}
        elif not s["btc"] and r["action"] == "BUY" and not decision["cooldown_seconds"] and s.get("last_ai_entry_candle") != candles[-1][0]:
            blocked = validate_plan(r, c, s["cash"], self.equity(bid), bid, ask)
            decision["ai_block_reason"] = blocked
            if s.get("ai_history"):
                s["ai_history"][-1]["risk_validation"] = {"time": stamp, "accepted": not bool(blocked), "reason": blocked}
            if blocked:
                decision["ai_status"] = "plan_rejected"
                return
            decision["entry_ok"] = True
            risk = {"atr": decision["atr"], "stop_fraction": r["stop_fraction"], "target_fraction": r["target_fraction"]}
            self.buy(ask, stamp, "ai_approved_entry", risk, allocation=r["allocation"])
            s["last_ai_entry_candle"] = candles[-1][0]
            if s.get("ai_history"):
                s["ai_history"][-1]["execution"] = {"time": stamp, "side": "BUY", "trade_index": len(s["trades"])-1}

    def update_ai(self, candles, decision, bid, ask, stamp):
        s, a = self.s, self.ai_runtime
        if not self.ai_enabled:
            decision["ai_status"] = "disabled"
            return None
        # Only the main trading thread modifies state; an AI request cannot delay risk exits.
        result = self.reviewer.snapshot()
        if result:
            s["ai"] = result
            history = s.setdefault("ai_history", [])
            if not history or history[-1]["requested_at"] != result["requested_at"] or history[-1]["candle"] != result["candle"]:
                history.append(result)
                s["ai_history"] = history[-2016:]
        if decision["ready"] and not s["halted"]:
            self.reviewer.submit(make_observation(candles, decision, self.c, s, bid, ask, stamp))
        candle = candles[-1][0] if candles else None
        current = bool(result and result["candle"] == candle)
        decision["ai_status"] = ("warming_up" if not decision["ready"] else "pending" if not current else
                                 "error" if result["status"] == "error" else
                                 "stale" if not 0 <= stamp - result["requested_at"] <= a["max_age_seconds"] else
                                 "price_changed" if abs(ask / result["ask"] - 1) > a["max_price_drift"] else result["action"])
        return result

    def tick(self, candles, bid, ask, stamp):
        s, c = self.s, self.c
        if not all(math.isfinite(x) and x > 0 for x in (bid, ask)) or bid > ask:
            raise ValueError("Ugyldig pris")
        equity = self.equity(bid)
        s["peak"] = max(s["peak"], equity)
        if equity <= s["peak"] * (1 - c["max_drawdown"]):
            s["halted"] = True
        if c.get("strategy") == "ai_v3":
            self.tick_v3(candles, bid, ask, stamp)
            return
        if c.get("strategy") == "trend_v2":
            self.tick_v2(candles, bid, ask, stamp)
            return
        risk_exit = False
        if s["btc"]:
            reason = ("drawdown" if s["halted"] else
                      "stop_loss" if bid <= s["entry"] * (1 - c["stop_loss"]) else
                      "take_profit" if bid >= s["entry"] * (1 + c["take_profit"]) else None)
            if reason:
                self.sell(bid, stamp, reason)
                risk_exit = True
        if len(candles) < c["slow_window"] or candles[-1][0] <= s["last_signal"]:
            return
        closes = [p for _, p in candles[-c["slow_window"]:]]
        if not all(math.isfinite(p) and p > 0 for p in closes):
            raise ValueError("Ugyldige candles")
        s["last_signal"] = candles[-1][0]
        fast = sum(closes[-c["fast_window"]:]) / c["fast_window"]
        slow = sum(closes) / c["slow_window"]
        if s["btc"] and fast < slow:
            self.sell(bid, stamp, "trend_exit")
        elif not s["btc"] and not s["halted"] and not risk_exit and fast > slow:
            self.buy(ask, stamp, "trend_entry")


def save(paper, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(paper.s, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temp, path)
    with path.with_suffix(".csv").open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=["time", "side", "price", "btc", "fee", "reason"])
        writer.writeheader()
        writer.writerows(paper.s["trades"])


def main():
    parser = argparse.ArgumentParser(description="Kun simuleret Bitcoin-handel")
    parser.add_argument("mode", choices=["demo", "paper"])
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--state")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--web-port", type=int, default=int(os.environ.get("WEB_PORT", "0")))
    parser.add_argument("--web-host", default="0.0.0.0")
    args = parser.parse_args()
    c = config(args.config)
    path = Path(args.state or ROOT / "runs" / f"{args.mode}.json")
    lock = path.with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    STOP.clear()
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    with ExitStack() as stack:
        stack.enter_context(session_lock(lock))
        if args.mode == "demo":
            paper = Paper(c)
            rng, candles, price = random.Random(42), [], 50000.0
            for i in range(500):
                # Signal uses past closes; execution occurs at the next synthetic price.
                price *= 1 + 0.002 * math.sin(i / 18) + rng.uniform(-0.002, 0.002)
                paper.tick(candles, price * 0.9999, price * 1.0001, 1700000040 + i * 60)
                candles.append((1700000040 + i * 60, price))
            save(paper, path)
            print(f"SYNTETISK DEMO: equity={paper.equity(price):.2f}, handler={len(paper.s['trades'])}")
            return
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        paper = Paper(c, state)
        from ai_settings import AISettings
        preferences = AISettings(path.parent / "ai-settings.json", c) if c.get("strategy") == "ai_v3" else None
        if preferences:
            preferences.apply(paper)
        dashboard = Dashboard(preferences)
        dashboard.update(paper)
        print(f"PAPER startet: {c['product']}; state={path}", flush=True)
        stack.enter_context(serve(dashboard, args.web_host, args.web_port))
        while not STOP.is_set():
            if paper.s["halted"]:
                print("RISIKOSTOP: sessionen er stoppet; ingen nye handler. Brug ny state-fil til et nyt forsøg.", flush=True)
                if args.once:
                    break
                STOP.wait(c["poll_seconds"])
                continue
            try:
                candles, bid, ask = market(c)
            except (OSError, ValueError, KeyError) as error:
                print(f"Ingen handel ved datafejl: {error}", flush=True)
                dashboard.update(paper, str(error))
                if args.once:
                    raise
                STOP.wait(c["poll_seconds"])
                continue
            if STOP.is_set():
                break
            if preferences:
                with preferences.lock:
                    preferences.apply(paper)
                    paper.tick(candles, bid, ask, int(time.time()))
            else:
                paper.tick(candles, bid, ask, int(time.time()))
            record_market(paper, candles, bid, ask, int(time.time()))
            # Disk errors terminate the process; never keep trading unsaved state.
            save(paper, path)
            dashboard.update(paper)
            print(f"PAPER {c['product']}: equity={paper.equity(bid):.2f}, BTC={paper.s['btc']:.8f}, handler={len(paper.s['trades'])}, stoppet={paper.s['halted']}", flush=True)
            if args.once:
                break
            STOP.wait(c["poll_seconds"])
        print("Paper-bot stoppet; gemt session bevares.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Paper-bot stoppet.")
