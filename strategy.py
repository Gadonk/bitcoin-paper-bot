"""Versioned, deterministic signal rules. Indicators only use closed candles."""
import math


class Candles(list):
    def __init__(self, bars):
        self.bars = bars
        super().__init__((b["start"], b["close"]) for b in bars)


def aggregate(bars, seconds, source_seconds):
    groups = {}
    for b in bars:
        groups.setdefault(b["start"] // seconds * seconds, []).append(b)
    result = []
    for stamp, group in sorted(groups.items()):
        group = sorted(group, key=lambda b: b["start"])
        if [b["start"] for b in group] != list(range(stamp, stamp + seconds, source_seconds)):
            continue
        result.append({"start": stamp, "open": group[0]["open"],
                       "high": max(b["high"] for b in group), "low": min(b["low"] for b in group),
                       "close": group[-1]["close"], "volume": sum(b["volume"] for b in group)})
    return result


def ema(values, n):
    result, value = [], values[0]
    for x in values:
        value += (x - value) * 2 / (n + 1)
        result.append(value)
    return result


def indicators(bars):
    closes = [b["close"] for b in bars]
    fast, slow = ema(closes, 12), ema(closes, 26)
    # Wilder smoothing for RSI and ATR, seeded with 14 observations.
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains, losses = [max(x, 0) for x in changes], [max(-x, 0) for x in changes]
    trs = [max(b["high"] - b["low"], abs(b["high"] - a["close"]),
               abs(b["low"] - a["close"])) for a, b in zip(bars, bars[1:])]
    def wilder(xs):
        value = sum(xs[:14]) / 14
        for x in xs[14:]:
            value = (value * 13 + x) / 14
        return value
    gain, loss = wilder(gains), wilder(losses)
    rsi = 50 if gain == loss == 0 else 100 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return fast, slow, rsi, wilder(trs)


def evaluate(candles, c, bid, ask):
    bars = getattr(candles, "bars", [])
    result = {"name": "Trend + breakout v2", "timeframe": 300, "checks": [], "ready": False}
    if len(bars) < 120:
        result["message"] = "Venter på mindst 120 afsluttede 5-minutters candles"
        return result
    if any(b["start"] - a["start"] != 300 for a, b in zip(bars, bars[1:])):
        result["message"] = "Huller i markedsdata; køb springes over"
        return result
    hours = aggregate(bars, 3600, 300)
    if len(hours) < 9:
        result["message"] = "Venter på 9 komplette time-candles"
        return result
    fast, slow, rsi, atr = indicators(bars)
    hour_closes = [b["close"] for b in hours]
    hour_ema = ema(hour_closes, 8)
    price = ask * (1 + c["slippage"])
    cost = ((1 + c["fee_rate"]) * (1 + c["slippage"]) /
            ((1 - c["fee_rate"]) * (1 - c["slippage"]))) - 1 + (ask - bid) / bid
    stop_fraction = max(0.008, 3 * atr / price)
    target_fraction = 8 * atr / price
    volume_avg = sum(b["volume"] for b in bars[-21:-1]) / 20
    volume_ratio = bars[-1]["volume"] / volume_avg if volume_avg else 0
    breakout = max(b["high"] for b in bars[-13:-1])
    checks = [
        ("Stigende trend på timegrafen", hour_closes[-1] > hour_ema[-1] and hour_ema[-1] > hour_ema[-2]),
        ("EMA 12 over EMA 26 og stigende", fast[-1] > slow[-1] and slow[-1] > slow[-4]),
        ("RSI mellem 50 og 70", 50 <= rsi <= 70),
        ("Lukkepris over sidste 12 candles højeste pris", bars[-1]["close"] > breakout),
        ("Volumen mindst 1,05 × normalt", volume_ratio >= 1.05),
        ("ATR-mål mindst 1,5 × handelsomkostninger", target_fraction >= cost * 1.5),
        ("Stopafstand højst 2% og mål mindst 2 × stop", stop_fraction <= 0.02 and target_fraction >= 2 * stop_fraction),
        ("Spread højst 0,10%", (ask - bid) / bid <= 0.001),
    ]
    result.update({"ready": True, "entry_ok": all(ok for _, ok in checks),
                   "checks": [{"label": label, "ok": bool(ok)} for label, ok in checks],
                   "fast": fast[-1], "slow": slow[-1], "rsi": rsi, "atr": atr,
                   "volume_ratio": volume_ratio, "breakout": breakout, "cost_fraction": cost,
                   "stop_fraction": stop_fraction, "target_fraction": target_fraction,
                   "bearish": fast[-1] < slow[-1], "message": "Alle købskrav opfyldt" if all(ok for _, ok in checks) else "Venter på et stærkere signal"})
    return result
