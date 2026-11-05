"""One trading cycle. GitHub Actions runs this every hour: python -m paper_trader.run"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

from . import events, market, metrics, storage
from .indicators import DAY_MS, HOUR_MS
from .portfolio import LEDGER_FIELDS, TRADE_FIELDS, Portfolio, iso, reconcile, round_down
from .strategy import analyze

M5_MS = 300_000
MAX_HEADLINES_PUBLISHED = 6


def stale_closed(closed, interval_ms, now_ms) -> bool:
    return not closed or now_ms - (closed[-1].t + interval_ms) > 2 * interval_ms


def is_weekend(now: float) -> bool:
    """US equities have no price discovery from Friday's close to Sunday evening (ET)."""
    d = datetime.fromtimestamp(now, timezone.utc)
    return d.weekday() == 5 or (d.weekday() == 4 and d.hour >= 21) or (d.weekday() == 6 and d.hour < 22)


def premium_pct(mid: float, ref: dict | None, now: float) -> float | None:
    """Token vs its real-world share. Only meaningful while the US market is open."""
    if not ref or not ref.get("price") or not ref.get("session_start"):
        return None
    if not ref["session_start"] <= now <= ref["session_end"]:
        return None
    if now - (ref.get("price_time") or 0) > 1800:
        return None
    return round(100 * (mid / ref["price"] - 1), 2)


def collect_context(cfg: dict, now: float, health: dict) -> dict:
    topics = {}
    ok = failed = 0
    for name, topic in cfg["news_topics"].items():
        items, o, f = events.fetch_topic(topic)
        ok, failed = ok + o, failed + f
        topics[name] = events.summarize_topic(items, now)
    health["news"] = {"ok": failed == 0, "detail": f"{ok} feeds ok, {failed} failed"}

    macro, errors = events.fetch_macro(cfg["macro_symbols"])
    health["macro"] = {"ok": not errors, "detail": "; ".join(errors) or f"{len(macro)} readings"}

    try:
        calendar = events.fetch_calendar()
        storage.write_json("calendar.json", calendar)
        health["calendar"] = {"ok": True, "detail": f"{len(calendar)} high-impact US events this week"}
    except Exception as e:
        calendar = storage.read_json("calendar.json", [])
        health["calendar"] = {"ok": False, "detail": f"using saved copy ({e})"}

    tickers = sorted({a["earnings_of"] for a in cfg["assets"].values() if a["earnings_of"]})
    earnings, errors = events.fetch_earnings(tickers)
    saved = storage.read_json("earnings.json", {})
    if errors:
        earnings = {**saved, **earnings}
    storage.write_json("earnings.json", earnings)
    health["earnings"] = {"ok": not errors, "detail": "; ".join(errors)[:160] or f"{len(earnings)} dates announced"}

    refs = {}
    ref_errors = []
    for a in cfg["assets"].values():
        if a["underlying"]:
            try:
                refs[a["underlying"]] = market.yahoo_daily(a["underlying"], "5d")
            except Exception as e:
                ref_errors.append(f"{a['underlying']}: {e}")
    health["underlying"] = {"ok": not ref_errors, "detail": "; ".join(ref_errors)[:160] or f"{len(refs)} reference prices"}
    return {"topics": topics, "macro": macro, "calendar": calendar, "earnings": earnings, "refs": refs}


def entry_gates(cfg, sym, a, sig, tick, ctx, final, news_veto, now) -> list[str]:
    """Every reason not to buy this token now, ignoring what the portfolio holds."""
    g, s = cfg["gates"], cfg["signals"]
    out = []
    if final < s["entry_score"]:
        out.append(f"score {final:.1f} is below {s['entry_score']}")
    if sig["extension_atr"] > s["max_extension_atr"]:
        out.append(f"overextended ({sig['extension_atr']:.1f} ATR above EMA20)")
    if tick["spread_pct"] > g["max_spread_pct"]:
        out.append(f"spread {tick['spread_pct']:.2f}% too wide")
    if tick["quote_volume"] < g["min_24h_quote_volume"]:
        out.append("24h volume too low")
    if tick["premium_pct"] is not None and tick["premium_pct"] > g["max_premium_pct"]:
        out.append(f"token at {tick['premium_pct']:.1f}% premium to {a['underlying']}")
    if news_veto:
        out.append("high-impact bad news")
    event = events.macro_blackout(ctx["calendar"], now, g["macro_blackout_minutes_before"], g["macro_blackout_minutes_after"])
    if event:
        out.append(f"macro event window: {event}")
    earn = ctx["earnings"].get(a["earnings_of"]) if a["earnings_of"] else None
    if earn and -DAY_MS / 1000 < earn - now < g["earnings_no_entry_hours"] * 3600:
        out.append(f"{a['earnings_of']} earnings soon")
    vix = ctx["macro"].get("vix", {}).get("last")
    if vix and vix >= g["vix_halt_level"] and a["class"] == "equity" and a["macro"] != "semis_short":
        out.append(f"VIX {vix:.0f} is above the halt level")
    if a["class"] == "equity" and not g["equity_tokens_trade_weekends"] and is_weekend(now):
        out.append("US market closed for the weekend")
    return out


def walk_stops(pf: Portfolio, cfg: dict, now: float, health: dict) -> None:
    """Replay 5-minute candles since the last run, so a late run still honours stops."""
    now_ms = now * 1000
    for sym in list(pf.state["positions"]):
        a = cfg["assets"][sym]
        pos = pf.state["positions"][sym]
        try:
            bars = market.klines(a["pair"], "5m", 1000, start_ms=pos["last_checked_bar"])
        except Exception as e:
            health["stops"] = {"ok": False, "detail": f"{sym}: {e}"}
            continue
        for bar in bars:
            closed = bar.t + M5_MS <= now_ms
            if pf.apply_bar(sym, bar, closed, a["slippage_pct"]):
                print(f"  {sym}: stop filled in candle {iso(bar.t / 1000)}")
                break
            if closed:
                pos["last_checked_bar"] = bar.t + M5_MS


def cycle(cfg: dict, now: float) -> dict:
    state = storage.read_json("state.json") or Portfolio.fresh_state(cfg)
    pf = Portfolio(cfg, state)
    start_cash = cfg["account"]["starting_cash"]
    if state["ledger_seq"] == 0:
        pf.deposit(now, start_cash)
        state["started_at"] = iso(now)

    health: dict = {}
    assets = cfg["assets"]
    pairs = [a["pair"] for a in assets.values()]

    # Prices are the one thing we can't do without, so a failure here aborts the run.
    tick_raw = market.tickers_24h(pairs)
    info = market.exchange_info(pairs)
    health["binance"] = {"ok": True, "detail": f"{len(tick_raw)} tokens"}

    ctx = collect_context(cfg, now, health)
    now_ms = now * 1000
    macro_view = events.macro_points(ctx["macro"])

    ticks = {}
    for sym, a in assets.items():
        r = tick_raw[a["pair"]]
        bid, ask = float(r["bidPrice"]), float(r["askPrice"])
        mid = (bid + ask) / 2
        ref = ctx["refs"].get(a["underlying"]) if a["underlying"] else None
        ticks[sym] = {
            "bid": bid, "ask": ask, "mid": mid, "last": float(r["lastPrice"]),
            "spread_pct": round(100 * (ask - bid) / ask, 3) if ask else 99.0,
            "quote_volume": float(r["quoteVolume"]),
            "change_24h_pct": float(r["priceChangePercent"]),
            "premium_pct": premium_pct(mid, ref, now),
            "status": info[a["pair"]]["status"],
        }

    if not state["benchmark_start"]:
        state["benchmark_start"] = {s: t["ask"] for s, t in ticks.items()}
    walk_stops(pf, cfg, now, health)
    health.setdefault("stops", {"ok": True, "detail": "all open positions checked"})

    # ---- signals ----------------------------------------------------------------
    signals = {}
    for sym, a in assets.items():
        try:
            h1, _ = market.split_closed(market.klines(a["pair"], "1h", 1000), HOUR_MS, now_ms)
            d1, _ = market.split_closed(market.klines(a["pair"], "1d", 200), DAY_MS, now_ms)
        except Exception as e:
            signals[sym] = {"error": str(e)}
            continue
        sig = analyze(h1, d1)
        if sig is None or stale_closed(h1, HOUR_MS, now_ms):
            signals[sym] = {"error": "not enough fresh candles"}
            continue
        news_adj, veto = events.news_adjustment(a, ctx["topics"], cfg["signals"]["max_news_adjust"])
        macro_adj = round(macro_view[a["macro"]] * cfg["signals"]["max_macro_adjust"], 1)
        sig.update(news_adj=news_adj, macro_adj=macro_adj, news_veto=veto,
                   final=round(max(-100, min(100, sig["score"] + news_adj + macro_adj)), 1))
        signals[sym] = sig
    errors = [f"{s}: {x['error']}" for s, x in signals.items() if "error" in x]
    health["signals"] = {"ok": not errors, "detail": "; ".join(errors)[:160] or f"{len(signals)} tokens scored"}

    bids = {s: t["bid"] for s, t in ticks.items()}
    actions: dict[str, str] = {}

    # ---- exits ------------------------------------------------------------------
    r = cfg["risk"]
    for sym in list(state["positions"]):
        a, pos, sig = assets[sym], state["positions"][sym], signals.get(sym, {})
        held_h = (now - pos["entry_time"]) / 3600
        reason = None
        earn = ctx["earnings"].get(a["earnings_of"]) if a["earnings_of"] else None
        flatten_s = cfg["gates"]["earnings_flatten_hours"] * 3600
        if earn and -flatten_s < earn - now < flatten_s:
            reason = f"{a['earnings_of']} earnings within {cfg['gates']['earnings_flatten_hours']}h"
        elif a["leverage"] > 1 and held_h >= r["max_hold_days_leveraged"] * 24:
            reason = "Leveraged token time limit"
        elif sig.get("news_veto") and held_h >= r["min_hold_hours_for_signal_exit"]:
            reason = "High-impact bad news"
        elif "final" in sig and held_h >= r["min_hold_hours_for_signal_exit"]:
            if sig["final"] <= cfg["signals"]["exit_score"]:
                reason = f"Score fell to {sig['final']:.0f}"
            elif sig["trend_broken"]:
                reason = "Trend broken"
        if reason:
            pf.sell(now, sym, ticks[sym]["bid"] * (1 - a["slippage_pct"] / 100), reason)
            state["last_signal_bar"][sym] = sig.get("bar_time", 0)
            actions[sym] = f"SOLD: {reason}"

    # ---- entries, strongest signal first -------------------------------------------
    equity = pf.equity(bids)
    pf.update_marks(equity, now, bids)
    ranked = sorted((s for s in signals if "final" in signals[s]), key=lambda s: -signals[s]["final"])
    for sym in ranked:
        a, sig, tick = assets[sym], signals[sym], ticks[sym]
        sig["gates"] = entry_gates(cfg, sym, a, sig, tick, ctx, sig["final"], sig["news_veto"], now)
        if tick["status"] != "TRADING":
            sig["gates"].append(f"exchange status {tick['status']}")
        block = pf.portfolio_block(sym, equity, bids, now)
        if block:
            sig["gates"].append(block)
        if sig["gates"] or sym in actions:
            continue
        if state["last_signal_bar"].get(sym) == sig["bar_time"]:
            sig["gates"].append("this candle was already acted on")
            continue
        fill = tick["ask"] * (1 + a["slippage_pct"] / 100)
        stop = fill - r["atr_stop_mult"] * sig["atr"]
        qty = pf.size(sym, fill, stop, equity, bids, tick["quote_volume"])
        qty = round_down(qty, info[a["pair"]]["step"])
        if qty * fill < info[a["pair"]]["min_notional"]:
            sig["gates"].append("position size below the exchange minimum")
            continue
        note = (f"score {sig['final']} (tech {sig['score']}, news {sig['news_adj']:+}, macro {sig['macro_adj']:+}); "
                f"RSI {sig['rsi']}, ADX {sig['adx']}")
        pf.buy(now, sym, qty, fill, stop, sig["atr"], sig["final"], note)
        state["last_signal_bar"][sym] = sig["bar_time"]
        actions[sym] = f"BOUGHT at {fill:.4f}"

    # ---- record -------------------------------------------------------------------
    equity = pf.equity(bids)
    pf.update_marks(equity, now, bids)
    all_ledger = storage.read_csv("ledger.csv") + [{k: str(v) for k, v in x.items()} for x in pf.new_ledger]
    reconcile(state, all_ledger, start_cash)  # raises before anything is saved

    exposure = equity - state["cash"]
    bench_each = start_cash / len(assets)
    benchmark = sum(bench_each * bids[s] / p for s, p in state["benchmark_start"].items())
    row = {
        "time": iso(now), "equity": round(equity, 2), "cash": round(state["cash"], 2),
        "exposure": round(exposure, 2),
        "drawdown_pct": round(100 * (equity / state["peak_equity"] - 1), 3),
        "benchmark": round(benchmark, 2), "open_positions": len(state["positions"]),
    }
    storage.append_csv("ledger.csv", LEDGER_FIELDS, pf.new_ledger)
    storage.append_csv("trades.csv", TRADE_FIELDS, pf.new_trades)
    storage.append_csv("equity.csv", storage.EQUITY_FIELDS, [row])
    storage.write_json("state.json", state)
    storage.write_json("dashboard.json", build_dashboard(cfg, state, ctx, signals, ticks, actions, health, now))
    return {"equity": equity, "actions": actions, "health": health}


def build_dashboard(cfg, state, ctx, signals, ticks, actions, health, now) -> dict:
    trades = storage.read_csv("trades.csv")
    ledger = storage.read_csv("ledger.csv")
    equity_rows = storage.read_csv("equity.csv")
    start = cfg["account"]["starting_cash"]
    positions = []
    for sym, p in state["positions"].items():
        bid = ticks[sym]["bid"]
        cost = p["qty"] * p["entry_price"]
        risk = p["qty"] * (p["entry_price"] - p["initial_stop"])
        pnl = p["qty"] * bid - cost - p["entry_fee"]
        positions.append({
            "symbol": sym, "qty": p["qty"], "entry_price": p["entry_price"], "price": bid,
            "stop": p["stop"], "value": round(p["qty"] * bid, 2), "pnl": round(pnl, 2),
            "pnl_pct": round(100 * pnl / cost, 2), "r": round(pnl / risk, 2) if risk > 0 else 0,
            "entry_time": iso(p["entry_time"]), "entry_score": p["entry_score"],
        })
    table = []
    for sym, a in cfg["assets"].items():
        s, t = signals.get(sym, {}), ticks[sym]
        table.append({
            "symbol": sym, "name": a["name"], "leverage": a["leverage"], "group": a["group"],
            "price": t["mid"], "change_24h_pct": t["change_24h_pct"], "spread_pct": t["spread_pct"],
            "quote_volume": round(t["quote_volume"]), "premium_pct": t["premium_pct"],
            "final": s.get("final"), "tech": s.get("score"), "news": s.get("news_adj"), "macro": s.get("macro_adj"),
            "rsi": s.get("rsi"), "adx": s.get("adx"), "atr_pct": s.get("atr_pct"), "vol_ratio": s.get("vol_ratio"),
            "points": s.get("points"), "gates": s.get("gates", []), "error": s.get("error"),
            "action": actions.get(sym), "held": sym in state["positions"],
        })
    table.sort(key=lambda x: -(x["final"] if x["final"] is not None else -999))
    stride = max(1, len(equity_rows) // 1500)
    curve = equity_rows[::stride]
    if equity_rows and curve[-1] is not equity_rows[-1]:
        curve.append(equity_rows[-1])
    news = {
        k: {"sentiment": v["sentiment"], "count": v["count"], "veto": v["veto"], "top": v["top"][:MAX_HEADLINES_PUBLISHED]}
        for k, v in ctx["topics"].items()
    }
    upcoming = [e for e in ctx["calendar"] if e["ts"] > now - 3600]
    return {
        "generated_at": iso(now),
        "started_at": state["started_at"],
        "starting_cash": start,
        "cash": round(state["cash"], 2),
        "equity": float(equity_rows[-1]["equity"]) if equity_rows else start,
        "exposure": float(equity_rows[-1]["exposure"]) if equity_rows else 0,
        "stats": metrics.equity_stats(equity_rows, start),
        "trade_stats": metrics.trade_stats(trades),
        "per_symbol": metrics.per_symbol(trades),
        "positions": positions,
        "signals": table,
        "entry_score": cfg["signals"]["entry_score"],
        "macro": ctx["macro"],
        "calendar": upcoming,
        "earnings": ctx["earnings"],
        "news": news,
        "health": health,
        "equity_curve": curve,
        "trades": trades[-200:][::-1],
        "ledger": ledger[-200:][::-1],
        "ledger_rows": len(ledger),
    }


def main() -> int:
    cfg = storage.load_config()
    now = time.time()
    result = cycle(cfg, now)
    print(f"{iso(now)} equity {result['equity']:.2f}")
    for sym, action in result["actions"].items():
        print(f"  {sym}: {action}")
    for name, h in result["health"].items():
        print(f"  [{'ok' if h['ok'] else 'WARN'}] {name}: {h['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
