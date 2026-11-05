"""Turns closed candles into a technical score from -100 to +100.

Only pure functions live here, so the live runner and the backtest make the
same decisions from the same data.
"""
from __future__ import annotations

from .indicators import Candle, HOUR_MS, adx, atr, ema, macd, resample, rsi

MIN_H1_BARS = 120
_MAX_POINTS = 94  # sum of the largest value each rule below can award


def analyze(h1: list[Candle], d1: list[Candle]) -> dict | None:
    """Score closed 1h candles, with closed daily candles for the larger trend."""
    if len(h1) < MIN_H1_BARS:
        return None
    h1 = h1[-300:]
    closes = [x.c for x in h1]
    last = h1[-1]
    points: dict[str, int] = {}

    e20, e50 = ema(closes, 20)[-1], ema(closes, 50)[-1]
    points["trend_1h"] = 12 if e20 > e50 else -12

    h4 = resample(h1, 4 * HOUR_MS)
    e4 = ema([x.c for x in h4], 50)[-1] if len(h4) >= 50 else None
    above_4h = None if e4 is None else last.c > e4
    points["trend_4h"] = 0 if e4 is None else (12 if above_4h else -12)

    dc = [x.c for x in d1]
    d20, d50 = (ema(dc, 20)[-1], ema(dc, 50)[-1]) if len(dc) >= 50 else (None, None)
    points["trend_1d"] = 0 if d50 is None else (12 if d20 > d50 else -12)

    r = rsi(closes)[-1]
    if r >= 78:
        points["rsi"] = -8
    elif r >= 68:
        points["rsi"] = 4
    elif r >= 52:
        points["rsi"] = 12
    elif r >= 45:
        points["rsi"] = 0
    elif r >= 35:
        points["rsi"] = -8
    else:
        points["rsi"] = -12

    _, _, hist = macd(closes)
    h_now, h_prev = hist[-1], hist[-2]
    points["macd_sign"] = 8 if h_now > 0 else -8
    points["macd_slope"] = 6 if h_now > h_prev else -6

    a, plus, minus = adx(h1)
    a_now = a[-1] or 0.0
    if a_now >= 20:
        points["adx"] = 12 if plus[-1] > minus[-1] else -12
    elif a_now < 15:
        points["adx"] = -6
    else:
        points["adx"] = 0

    prior = h1[-21:-1]
    if last.c >= max(x.h for x in prior):
        points["breakout"] = 10
    elif last.c <= min(x.l for x in prior):
        points["breakout"] = -10
    else:
        points["breakout"] = 0

    avg_vol = sum(x.v for x in prior) / len(prior)
    vol_ratio = last.v / avg_vol if avg_vol > 0 else 0.0
    if vol_ratio >= 1.3:
        points["volume"] = 10 if last.c >= last.o else -10
    else:
        points["volume"] = 0

    atr_now = atr(h1)[-1]
    score = max(-100.0, min(100.0, 100 * sum(points.values()) / _MAX_POINTS))
    return {
        "bar_time": last.t,
        "close": last.c,
        "score": round(score, 1),
        "points": points,
        "ema20": e20,
        "ema50": e50,
        "rsi": round(r, 1),
        "macd_hist": h_now,
        "adx": round(a_now, 1),
        "atr": atr_now,
        "atr_pct": round(100 * atr_now / last.c, 2),
        "vol_ratio": round(vol_ratio, 2),
        "extension_atr": round((last.c - e20) / atr_now, 2) if atr_now else 0.0,
        # Trend is broken only when both the fast and slow views agree.
        "trend_broken": e20 < e50 and above_4h is False,
    }
