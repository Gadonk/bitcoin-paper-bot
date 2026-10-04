"""Compare fixed v1/v2 rules on unseen historical Coinbase minute OHLCV.

Signals use completed candles, execution uses the next minute's open.
No intrabar high/low fills: risk checks run at minute opens like polling.
"""
import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from bot import Paper, ROOT, config, public_get
from strategy import Candles, aggregate


def timestamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())


def fetch(start, end, path, step=60):
    rows = {}
    for left in range(start, end, 300 * step):
        right = min(left + 300 * step, end)
        for attempt in range(3):
            try:
                data = public_get("BTC-EUR", "candles", {"start": left, "end": right,
                                  "granularity": "ONE_MINUTE" if step == 60 else "FIVE_MINUTE", "limit": 350})
                break
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(1 + attempt)
        for x in data["candles"]:
            stamp = int(x["start"])
            if start <= stamp < end:
                rows[stamp] = {"start": stamp, **{k: float(x[k]) for k in ("open", "high", "low", "close", "volume")}}
        print(f"Hentet {len(rows)} candles ({step}s)", flush=True)
        time.sleep(0.15)
    bars = sorted(rows.values(), key=lambda b: b["start"])
    validate(bars, start, end, step)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"start": start, "end": end, "step": step, "bars": bars}), encoding="utf-8")
    return bars


def validate(bars, start, end, step=60):
    stamps = [b["start"] for b in bars]
    if not stamps or stamps != sorted(set(stamps)) or any(t < start or t >= end or t % step for t in stamps):
        raise ValueError("Ugyldig tidsakse")
    import math
    for b in bars:
        if not all(math.isfinite(b[k]) for k in ("open", "high", "low", "close", "volume")) or not 0 < b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"] or b["volume"] < 0:
            raise ValueError("Ugyldige OHLCV-priser")
    return (end - start) // step - len(bars)


def simulate(bars, start, end, c, spread=0.0002, five_minute=None):
    if c.get("strategy") == "ai_v3":
        raise ValueError("V3 kræver AI-svar registreret på handelstidspunktet; denne backtest understøtter kun v1/v2")
    paper = Paper(c)
    previous, five, bucket = [], [], []
    curve, peak, drawdown = [], c["initial_cash"], 0
    skipped = 0
    filters, decisions, last_decision = {}, 0, None
    five_index = 0
    for bar in bars:
        if bar["start"] >= end:
            break
        if five_minute is not None:
            while five_index < len(five_minute) and five_minute[five_index]["start"] + 300 <= bar["start"]:
                five.append(five_minute[five_index])
                five = five[-300:]
                five_index += 1
        if bar["start"] >= start:
            closed = Candles(five[-300:]) if c.get("strategy") == "trend_v2" else Candles(previous[-300:])
            if c.get("strategy") != "trend_v2":
                recent = closed[-c["slow_window"]:]
                if any(b[0] - a[0] != 60 for a, b in zip(recent, recent[1:])) or not recent or bar["start"] - recent[-1][0] > 120:
                    closed = []
                    skipped += 1
            mid = bar["open"]
            paper.tick(closed, mid * (1 - spread / 2), mid * (1 + spread / 2), bar["start"])
            if c.get("strategy") == "trend_v2" and closed and closed[-1][0] != last_decision:
                last_decision = closed[-1][0]
                decision = paper.s.get("decision", {})
                if decision.get("ready"):
                    decisions += 1
                    for check in decision["checks"]:
                        filters[check["label"]] = filters.get(check["label"], 0) + int(check["ok"])
            value = paper.equity(mid * (1 - spread / 2))
            peak = max(peak, value)
            drawdown = max(drawdown, 1 - value / peak)
            curve.append([bar["start"], value])
        previous.append(bar)
        if len(previous) > 300:
            previous.pop(0)
        bucket.append(bar)
        if (bar["start"] + 60) % 300 == 0:
            if five_minute is None:
                five.extend(aggregate(bucket, 300, 60))
            five = five[-300:]
            bucket = []
    if not curve:
        raise ValueError("Tom testperiode")
    # Account equity values an open position after hypothetical selling costs.
    final = curve[-1][1]
    pairs, entry = [], None
    for t in paper.s["trades"]:
        if t["side"] == "BUY":
            entry = t
        else:
            net = (t["price"] - entry["price"]) * t["btc"] - entry["fee"] - t["fee"]
            pairs.append(net)
            entry = None
    positive = sum(x for x in pairs if x > 0)
    negative = -sum(x for x in pairs if x < 0)
    first = next(b for b in bars if b["start"] >= start)
    last = next(b for b in reversed(bars) if b["start"] < end)
    # Same 10% allocation for the passive benchmark; remainder stays in cash.
    budget = c["initial_cash"] * c["allocation"]
    btc = budget / (1 + c["fee_rate"]) / (first["open"] * (1 + spread / 2) * (1 + c["slippage"]))
    passive = c["initial_cash"] - budget + btc * last["open"] * (1 - spread / 2) * (1 - c["slippage"]) * (1 - c["fee_rate"])
    return {"strategy": c.get("strategy", "sma_v1"), "equity_dkk": final * c["dkk_per_eur"],
            "return_pct": (final / c["initial_cash"] - 1) * 100, "drawdown_pct": drawdown * 100,
            "trades": len(paper.s["trades"]), "closed_positions": len(pairs),
            "net_winners": sum(x > 0 for x in pairs), "fees_dkk": sum(t["fee"] for t in paper.s["trades"]) * c["dkk_per_eur"],
            "profit_factor": positive / negative if negative else None,
            "halted": paper.s["halted"], "open_position": paper.s["btc"] > 0,
            "legacy_signals_skipped_missing_data": skipped,
            "evaluated_v2_candles": decisions, "entry_filter_pass_counts": filters,
            "passive_10pct_equity_dkk": passive * c["dkk_per_eur"], "curve": curve,
            "trade_log": paper.s["trades"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-09-24")
    parser.add_argument("--end", default="2026-10-01")
    parser.add_argument("--data", type=Path, default=ROOT / "runs" / "backtest-minute.json")
    parser.add_argument("--output", type=Path, default=ROOT / "runs" / "backtest-results.json")
    args = parser.parse_args()
    start, end = timestamp(args.start), timestamp(args.end)
    if end <= start or end > time.time() // 60 * 60:
        raise ValueError("Testperioden skal være afsluttet")
    warmup = start - 1800 * 60
    def load(path, step):
        if not path.exists():
            return fetch(warmup, end, path, step)
        cached = json.loads(path.read_text())
        if [cached["start"], cached["end"], cached["step"]] != [warmup, end, step]:
            raise ValueError("Cache dækker en anden periode; brug en anden --data fil")
        return cached["bars"]
    bars = load(args.data, 60)
    five = load(args.data.with_name(args.data.stem + "-5min.json"), 300)
    gaps = validate(bars, warmup, end)
    gaps_five = validate(five, warmup, end, 300)
    split = (start + (end - start) // 2) // 60 * 60
    results = {"start_utc": args.start, "end_utc": args.end, "spread_assumption": 0.0002,
               "parameters_tuned": False, "missing_minute_candles": gaps,
               "missing_five_minute_candles": gaps_five, "periods": []}
    for label, left, right in [("første halvdel", start, split), ("anden halvdel", split, end)]:
        for filename in ("config.json", "config-v2.json"):
            result = simulate([b for b in bars if b["start"] >= left - 1800 * 60], left, right, config(ROOT / filename),
                              five_minute=[b for b in five if b["start"] >= left - 1800 * 60])
            result.update({"period": label, "start": left, "end": right})
            results["periods"].append(result)
            print(json.dumps({k: v for k, v in result.items() if k not in ("curve", "trade_log")}, ensure_ascii=False), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
