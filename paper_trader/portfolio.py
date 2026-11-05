"""Paper account: positions, simulated fills, ledger, closed trades, risk rules.

Every change to cash is one ledger row, so the ledger alone can rebuild the cash
balance and every position. `reconcile` checks exactly that before anything is saved.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

HOUR_S = 3600

LEDGER_FIELDS = ["seq", "time", "type", "symbol", "qty", "price", "amount", "cash_after", "trade_id", "note"]
TRADE_FIELDS = [
    "trade_id", "symbol", "entry_time", "entry_price", "exit_time", "exit_price", "qty",
    "fees", "pnl", "pnl_pct", "r_multiple", "hold_hours", "entry_score", "exit_reason",
]


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def round_down(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    decimals = max(0, round(-math.log10(step)))
    return round(math.floor(qty / step + 1e-9) * step, decimals)


@dataclass
class Portfolio:
    cfg: dict
    state: dict
    new_ledger: list = field(default_factory=list)
    new_trades: list = field(default_factory=list)

    @classmethod
    def fresh_state(cls, cfg: dict) -> dict:
        return {
            "cash": 0.0,
            "positions": {},
            "ledger_seq": 0,
            "trade_seq": 0,
            "peak_equity": cfg["account"]["starting_cash"],
            "day": {"date": None, "start_equity": cfg["account"]["starting_cash"]},
            "cooldown_until": {},
            "last_signal_bar": {},
            "started_at": None,
            "benchmark_start": {},
        }

    def _post(self, ts, kind, symbol, qty, price, amount, trade_id, note=""):
        self.state["ledger_seq"] += 1
        self.state["cash"] = round(self.state["cash"] + round(amount, 8), 8)
        self.new_ledger.append({
            "seq": self.state["ledger_seq"], "time": iso(ts), "type": kind, "symbol": symbol,
            "qty": qty, "price": price, "amount": round(amount, 8),
            "cash_after": self.state["cash"], "trade_id": trade_id, "note": note,
        })

    def deposit(self, ts: float, amount: float):
        self._post(ts, "DEPOSIT", "", 0, 0, amount, "", "Starting paper balance")

    # ---- valuation ----------------------------------------------------------
    def position_value(self, symbol: str, price: float) -> float:
        return self.state["positions"][symbol]["qty"] * price

    def equity(self, prices: dict[str, float]) -> float:
        total = self.state["cash"]
        for sym, pos in self.state["positions"].items():
            total += pos["qty"] * prices.get(sym, pos["last_price"])
        return total

    # ---- risk checks -----------------------------------------------------------
    def portfolio_block(self, symbol: str, equity: float, prices: dict, now: float) -> str | None:
        """Why this symbol can't be opened right now, based on what is already held."""
        s, r = self.state, self.cfg["risk"]
        asset = self.cfg["assets"][symbol]
        if symbol in s["positions"]:
            return "already holding"
        if now < s["cooldown_until"].get(symbol, 0):
            return "cooling down after a loss"
        if len(s["positions"]) >= r["max_positions"]:
            return "max positions reached"
        if s["day"]["start_equity"] and equity <= s["day"]["start_equity"] * (1 - r["daily_loss_halt_pct"] / 100):
            return "daily loss limit hit"
        group = asset["group"]
        rule = self.cfg["groups"][group]
        held = [p for p in s["positions"] if self.cfg["assets"][p]["group"] == group]
        if rule.get("max_positions") is not None and len(held) >= rule["max_positions"]:
            return f"{group} group already has a position"
        for other in s["positions"]:
            if self.cfg["assets"][other]["group"] in rule.get("conflicts_with", []):
                return f"conflicts with open {other}"
        return None

    def size(self, symbol: str, price: float, stop: float, equity: float, prices: dict, volume_24h: float | None) -> float:
        """Quantity allowed after applying every sizing limit. 0 means skip."""
        s, r = self.state, self.cfg["risk"]
        asset = self.cfg["assets"][symbol]
        per_unit_risk = price - stop
        if per_unit_risk <= 0:
            return 0.0
        risk_pct = r["risk_per_trade_pct"]
        if s["peak_equity"] and equity <= s["peak_equity"] * (1 - r["drawdown_half_risk_pct"] / 100):
            risk_pct /= 2
        notional = equity * risk_pct / 100 / per_unit_risk * price
        notional = min(notional, equity * asset["max_position_pct"] / 100)

        gross = sum(self.position_value(p, prices.get(p, price)) for p in s["positions"])
        notional = min(notional, equity * r["max_gross_exposure_pct"] / 100 - gross)

        group = asset["group"]
        in_group = sum(
            self.position_value(p, prices.get(p, price))
            for p in s["positions"] if self.cfg["assets"][p]["group"] == group
        )
        notional = min(notional, equity * self.cfg["groups"][group]["max_exposure_pct"] / 100 - in_group)

        if volume_24h is not None:
            notional = min(notional, volume_24h * r["max_pct_of_24h_volume"] / 100)

        fee_rate = self.cfg["account"]["fee_pct"] / 100
        notional = min(notional, s["cash"] / (1 + fee_rate))
        return max(notional, 0.0) / price

    # ---- fills -----------------------------------------------------------------
    def buy(self, ts, symbol, qty, price, stop, atr_value, score, note) -> None:
        s = self.state
        s["trade_seq"] += 1
        tid = f"T{s['trade_seq']:05d}"
        fee = qty * price * self.cfg["account"]["fee_pct"] / 100
        self._post(ts, "BUY", symbol, qty, price, -qty * price, tid, note)
        self._post(ts, "FEE", symbol, 0, 0, -fee, tid, "Entry fee")
        s["positions"][symbol] = {
            "trade_id": tid, "qty": qty, "entry_price": price, "entry_time": ts,
            "initial_stop": stop, "stop": stop, "atr": atr_value, "high_water": price,
            "entry_fee": fee, "entry_score": score, "last_price": price,
            # Start after the entry candle, which can include prices from before the fill.
            "last_checked_bar": (int(ts // 300) + 1) * 300_000,
        }

    def sell(self, ts, symbol, price, reason) -> dict:
        s = self.state
        pos = s["positions"].pop(symbol)
        qty, tid = pos["qty"], pos["trade_id"]
        gross = qty * price
        fee = gross * self.cfg["account"]["fee_pct"] / 100
        self._post(ts, "SELL", symbol, qty, price, gross, tid, reason)
        self._post(ts, "FEE", symbol, 0, 0, -fee, tid, "Exit fee")
        fees = pos["entry_fee"] + fee
        cost = qty * pos["entry_price"]
        pnl = gross - cost - fees
        risk = qty * (pos["entry_price"] - pos["initial_stop"])
        trade = {
            "trade_id": tid, "symbol": symbol, "entry_time": iso(pos["entry_time"]),
            "entry_price": pos["entry_price"], "exit_time": iso(ts), "exit_price": price,
            "qty": qty, "fees": round(fees, 6), "pnl": round(pnl, 6),
            "pnl_pct": round(100 * pnl / cost, 3) if cost else 0,
            "r_multiple": round(pnl / risk, 3) if risk > 0 else 0,
            "hold_hours": round((ts - pos["entry_time"]) / HOUR_S, 2),
            "entry_score": pos["entry_score"], "exit_reason": reason,
        }
        self.new_trades.append(trade)
        if pnl < 0:
            s["cooldown_until"][symbol] = ts + self.cfg["risk"]["cooldown_hours_after_loss"] * HOUR_S
        return trade

    def apply_bar(self, symbol: str, bar, closed: bool, slippage_pct: float) -> dict | None:
        """Check one candle against the open stop. Assumes the stop is hit before any
        new high in that candle, and only raises the stop after the candle closes."""
        pos = self.state["positions"][symbol]
        if bar.l <= pos["stop"]:
            level = min(pos["stop"], bar.o)  # a gap through the stop fills at the open
            fill = level * (1 - 3 * slippage_pct / 100)
            label = "Trailing stop" if pos["stop"] > pos["initial_stop"] else "Stop loss"
            return self.sell(bar.t / 1000 + 60, symbol, fill, label)
        if closed:
            pos["high_water"] = max(pos["high_water"], bar.h)
            r = self.cfg["risk"]
            risk = pos["entry_price"] - pos["initial_stop"]
            if pos["high_water"] >= pos["entry_price"] + r["trail_after_r"] * risk:
                pos["stop"] = max(pos["stop"], pos["high_water"] - r["atr_trail_mult"] * pos["atr"])
        return None

    def update_marks(self, equity: float, now: float, prices: dict):
        s = self.state
        for sym, p in s["positions"].items():
            p["last_price"] = prices.get(sym, p["last_price"])
        s["peak_equity"] = max(s["peak_equity"], equity)
        s["last_equity"] = equity
        day = iso(now)[:10]
        if s["day"]["date"] != day:
            s["day"] = {"date": day, "start_equity": equity}


def reconcile(state: dict, ledger: list[dict], starting_cash: float) -> None:
    """Rebuild cash and positions from the ledger; raise if they differ from state."""
    cash, qty, deposits = 0.0, {}, 0.0
    for i, row in enumerate(ledger, 1):
        if int(row["seq"]) != i:
            raise ValueError(f"ledger sequence broken at row {i}")
        amount = float(row["amount"])
        cash = round(cash + amount, 8)
        if abs(cash - float(row["cash_after"])) > 1e-6:
            raise ValueError(f"cash_after mismatch at ledger row {i}")
        if row["type"] == "DEPOSIT":
            deposits += amount
        elif row["type"] == "BUY":
            qty[row["symbol"]] = qty.get(row["symbol"], 0.0) + float(row["qty"])
        elif row["type"] == "SELL":
            qty[row["symbol"]] = qty.get(row["symbol"], 0.0) - float(row["qty"])
    if abs(deposits - starting_cash) > 1e-6:
        raise ValueError("ledger deposits differ from configured starting cash")
    if abs(cash - state["cash"]) > 1e-6:
        raise ValueError(f"cash mismatch: ledger {cash} vs state {state['cash']}")
    if cash < -1e-6:
        raise ValueError("cash balance went negative")
    open_qty = {k: v for k, v in qty.items() if abs(v) > 1e-9}
    held = {k: v["qty"] for k, v in state["positions"].items()}
    if set(open_qty) != set(held) or any(abs(open_qty[k] - held[k]) > 1e-9 for k in held):
        raise ValueError(f"position mismatch: ledger {open_qty} vs state {held}")
