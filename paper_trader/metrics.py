"""Performance statistics from the equity history and closed trades."""
from __future__ import annotations

import math

MIN_DAYS_FOR_SHARPE = 30


def trade_stats(trades: list[dict]) -> dict:
    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    rs = [float(t["r_multiple"]) for t in trades]
    gross_loss = -sum(losses)
    return {
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(pnls), 1) if pnls else None,
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss > 0 else None,
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "total_pnl": round(sum(pnls), 2),
        "total_fees": round(sum(float(t["fees"]) for t in trades), 2),
        "avg_hold_hours": round(sum(float(t["hold_hours"]) for t in trades) / len(pnls), 1) if pnls else None,
    }


def per_symbol(trades: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for t in trades:
        x = out.setdefault(t["symbol"], {"symbol": t["symbol"], "trades": 0, "wins": 0, "pnl": 0.0})
        x["trades"] += 1
        x["wins"] += float(t["pnl"]) > 0
        x["pnl"] = round(x["pnl"] + float(t["pnl"]), 2)
    return sorted(out.values(), key=lambda x: -x["pnl"])


def equity_stats(rows: list[dict], start: float) -> dict:
    if not rows:
        return {"return_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe": None, "benchmark_return_pct": 0.0, "days": 0}
    daily: dict[str, float] = {}
    peak, worst = start, 0.0
    for r in rows:
        e = float(r["equity"])
        daily[r["time"][:10]] = e
        peak = max(peak, e)
        worst = min(worst, e / peak - 1)
    closes = list(daily.values())
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    sharpe = None
    if len(rets) >= MIN_DAYS_FOR_SHARPE:
        mean = sum(rets) / len(rets)
        sd = (sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
        # Gold and these tokens trade every day of the year.
        sharpe = round(mean / sd * math.sqrt(365), 2) if sd > 0 else None
    last = rows[-1]
    return {
        "return_pct": round(100 * (float(last["equity"]) / start - 1), 2),
        "max_drawdown_pct": round(100 * worst, 2),
        "sharpe": sharpe,
        "benchmark_return_pct": round(100 * (float(last["benchmark"]) / start - 1), 2),
        "days": len(daily),
    }
