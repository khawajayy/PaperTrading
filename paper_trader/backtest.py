"""Replays the technical rules and risk limits over each token's Binance history.

    python -m paper_trader.backtest

News, macro and event filters are not replayed because their history isn't available,
so this is a sanity check of the mechanics, not a forecast. Nothing here touches the
live record in docs/data.
"""
from __future__ import annotations

import bisect
import time

from . import market, metrics, storage
from .indicators import DAY_MS, HOUR_MS, Candle, resample
from .portfolio import Portfolio, iso, round_down
from .strategy import MIN_H1_BARS, analyze


def history(pair: str, days: int = 180) -> list[Candle]:
    out: list[Candle] = []
    cursor = int(time.time() * 1000) - days * DAY_MS
    while True:
        rows = market.klines(pair, "1h", 1000, start_ms=cursor)
        out.extend(rows)
        if len(rows) < 1000:
            return out
        cursor = rows[-1].t + 1


def replay(cfg: dict, data: dict[str, list[Candle]], start_ms: int) -> dict:
    """Decide at each hour's close, then fill at the next hour's open."""
    state = Portfolio.fresh_state(cfg)
    pf = Portfolio(cfg, state)
    start_cash = cfg["account"]["starting_cash"]
    pf.deposit(start_ms / 1000, start_cash)
    index = {s: {c.t: i for i, c in enumerate(b)} for s, b in data.items()}
    daily = {s: resample(b, DAY_MS) for s, b in data.items()}
    daily_t = {s: [d.t for d in b] for s, b in daily.items()}
    times = sorted({c.t for b in data.values() for c in b if c.t >= start_ms})
    r, sig_cfg, assets = cfg["risk"], cfg["signals"], cfg["assets"]
    equity_rows: list[dict] = []
    prices: dict[str, float] = {}
    first_price: dict[str, float] = {}
    share = start_cash / len(data)

    for t in times:
        ts = t / 1000
        bars = {s: data[s][index[s][t]] for s in data if t in index[s]}
        for s, bar in bars.items():
            first_price.setdefault(s, bar.o)
            prices[s] = bar.c
            if s in state["positions"]:
                pf.apply_bar(s, bar, True, assets[s]["slippage_pct"])
        equity = pf.equity(prices)
        pf.update_marks(equity, ts, prices)

        signals = {}
        for s in bars:
            i = index[s][t]
            if i + 1 < MIN_H1_BARS:
                continue
            # Only days that finished before this hour closed.
            n = bisect.bisect_right(daily_t[s], t + HOUR_MS - DAY_MS)
            sig = analyze(data[s][max(0, i - 299):i + 1], daily[s][max(0, n - 200):n])
            if sig:
                signals[s] = sig

        next_t = t + HOUR_MS
        for s in list(state["positions"]):
            sig, pos = signals.get(s), state["positions"][s]
            nxt = data[s][index[s][next_t]] if next_t in index[s] else None
            if not sig or not nxt:
                continue
            held_h = (next_t / 1000 - pos["entry_time"]) / 3600
            reason = None
            if assets[s]["leverage"] > 1 and held_h >= r["max_hold_days_leveraged"] * 24:
                reason = "Leveraged token time limit"
            elif held_h >= r["min_hold_hours_for_signal_exit"]:
                if sig["score"] <= sig_cfg["exit_score"]:
                    reason = "Score fell"
                elif sig["trend_broken"]:
                    reason = "Trend broken"
            if reason:
                pf.sell(next_t / 1000, s, nxt.o * (1 - assets[s]["slippage_pct"] / 100), reason)

        for s in sorted(signals, key=lambda x: -signals[x]["score"]):
            sig = signals[s]
            nxt = data[s][index[s][next_t]] if next_t in index[s] else None
            if not nxt or sig["score"] < sig_cfg["entry_score"] or sig["extension_atr"] > sig_cfg["max_extension_atr"]:
                continue
            if pf.portfolio_block(s, equity, prices, next_t / 1000):
                continue
            fill = nxt.o * (1 + assets[s]["slippage_pct"] / 100)
            stop = fill - r["atr_stop_mult"] * sig["atr"]
            qty = round_down(pf.size(s, fill, stop, equity, prices, None), 0.01)
            if qty * fill >= 5:
                pf.buy(next_t / 1000, s, qty, fill, stop, sig["atr"], sig["score"], "backtest")

        bench = sum(share * prices[s] / first_price[s] if s in first_price else share for s in data)
        equity_rows.append({"time": iso(ts), "equity": pf.equity(prices), "benchmark": bench})

    return {
        "stats": metrics.equity_stats(equity_rows, start_cash),
        "trades": metrics.trade_stats(pf.new_trades),
        "per_symbol": metrics.per_symbol(pf.new_trades),
        "final_equity": round(pf.equity(prices), 2),
        "first_bar": iso(times[0] / 1000) if times else None,
        "last_bar": iso(times[-1] / 1000) if times else None,
    }


def main() -> None:
    cfg = storage.load_config()
    now_ms = time.time() * 1000
    data = {}
    for sym, a in cfg["assets"].items():
        data[sym] = market.split_closed(history(a["pair"]), HOUR_MS, now_ms)[0]
        print(f"{sym}: {len(data[sym])} hours")
    # Start once every token is listed and has enough history to score.
    start = max(b[MIN_H1_BARS].t for b in data.values())
    result = replay(cfg, data, start)
    print(f"\n{result['first_bar']} to {result['last_bar']} (technical rules only, net of fees and slippage)")
    print("Equity stats:", result["stats"])
    print("Trade stats: ", result["trades"])
    print("Final equity:", result["final_equity"])
    for row in result["per_symbol"]:
        print(" ", row)


if __name__ == "__main__":
    main()
