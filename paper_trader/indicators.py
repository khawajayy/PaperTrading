"""Technical indicators on plain Python lists. Every function returns a list the
same length as its input, with None where there is not yet enough history."""
from __future__ import annotations

from typing import NamedTuple


class Candle(NamedTuple):
    t: int  # open time, epoch ms
    o: float
    h: float
    l: float
    c: float
    v: float  # base volume


HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _wilder(values: list[float], period: int, first: int) -> list[float | None]:
    """Wilder smoothing of values[first:], seeded by a simple average."""
    out: list[float | None] = [None] * len(values)
    if len(values) < first + period:
        return out
    prev = sum(values[first:first + period]) / period
    out[first + period - 1] = prev
    for i in range(first + period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [0.0] + [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [0.0] + [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_gain = _wilder(gains, period, 1)
    avg_loss = _wilder(losses, period, 1)
    for i in range(len(closes)):
        g, l = avg_gain[i], avg_loss[i]
        if g is None or l is None:
            continue
        out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l) if g > 0 else 0.0
    return out


def macd(closes: list[float], fast=12, slow=26, signal=9):
    """Returns (macd_line, signal_line, histogram)."""
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [a - b if a is not None and b is not None else None for a, b in zip(ef, es)]
    start = next((i for i, v in enumerate(line) if v is not None), None)
    sig: list[float | None] = [None] * len(closes)
    if start is not None:
        tail = ema([v for v in line[start:]], signal)
        sig[start:] = tail
    hist = [a - b if a is not None and b is not None else None for a, b in zip(line, sig)]
    return line, sig, hist


def true_range(c: list[Candle]) -> list[float]:
    out = []
    for i, x in enumerate(c):
        if i == 0:
            out.append(x.h - x.l)
        else:
            p = c[i - 1].c
            out.append(max(x.h - x.l, abs(x.h - p), abs(x.l - p)))
    return out


def atr(c: list[Candle], period: int = 14) -> list[float | None]:
    return _wilder(true_range(c), period, 0)


def adx(c: list[Candle], period: int = 14):
    """Returns (adx, plus_di, minus_di)."""
    n = len(c)
    plus_dm, minus_dm = [0.0] * n, [0.0] * n
    for i in range(1, n):
        up, down = c[i].h - c[i - 1].h, c[i - 1].l - c[i].l
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
    tr_s = _wilder(true_range(c), period, 1)
    p_s = _wilder(plus_dm, period, 1)
    m_s = _wilder(minus_dm, period, 1)
    plus_di: list[float | None] = [None] * n
    minus_di: list[float | None] = [None] * n
    dx = [0.0] * n
    first = None
    for i in range(n):
        if tr_s[i] is None:
            continue
        if first is None:
            first = i
        if tr_s[i] > 0:
            plus_di[i] = 100 * p_s[i] / tr_s[i]
            minus_di[i] = 100 * m_s[i] / tr_s[i]
            total = plus_di[i] + minus_di[i]
            dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / total if total else 0.0
        else:
            plus_di[i] = minus_di[i] = 0.0
    adx_line = _wilder(dx, period, first) if first is not None else [None] * n
    return adx_line, plus_di, minus_di


def resample(c: list[Candle], ms: int) -> list[Candle]:
    """Aggregate candles into a larger timeframe, keeping only complete buckets."""
    if not c:
        return []
    step = c[1].t - c[0].t if len(c) > 1 else ms
    need = max(ms // step, 1)
    buckets: dict[int, list[Candle]] = {}
    for x in c:
        buckets.setdefault(x.t - x.t % ms, []).append(x)
    out = []
    for t in sorted(buckets):
        b = buckets[t]
        if len(b) == need:
            out.append(Candle(t, b[0].o, max(x.h for x in b), min(x.l for x in b), b[-1].c, sum(x.v for x in b)))
    return out
