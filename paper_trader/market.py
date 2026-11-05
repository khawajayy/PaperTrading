"""Public market data: Binance prices and Yahoo Finance reference data. No API keys."""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from .indicators import Candle

# Binance's market-data host comes first because api.binance.com refuses US IP
# addresses, which is where GitHub Actions runners usually are.
BINANCE_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com", "https://api1.binance.com"]
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
USER_AGENT = "Mozilla/5.0 (compatible; PaperTradingBot/1.0)"


def http_get(url: str, timeout=20, retries=2, headers: dict | None = None) -> bytes:
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # network, HTTP and timeout errors all retry the same way
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def _binance(path: str, **params):
    query = urllib.parse.urlencode(params)
    last = None
    for host in BINANCE_HOSTS:
        try:
            return json.loads(http_get(f"{host}{path}?{query}", retries=1))
        except Exception as e:
            last = e
    raise RuntimeError(f"all Binance hosts failed for {path}: {last}")


def klines(pair: str, interval: str, limit: int = 1000, start_ms: int | None = None) -> list[Candle]:
    params = {"symbol": pair, "interval": interval, "limit": limit}
    if start_ms is not None:
        params["startTime"] = start_ms
    rows = _binance("/api/v3/klines", **params)
    return [Candle(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]


def split_closed(candles: list[Candle], interval_ms: int, now_ms: float) -> tuple[list[Candle], Candle | None]:
    """Binance's newest candle may still be forming; indicators must never see it."""
    if candles and candles[-1].t + interval_ms > now_ms:
        return candles[:-1], candles[-1]
    return candles, None


def tickers_24h(pairs: list[str]) -> dict[str, dict]:
    rows = _binance("/api/v3/ticker/24hr", symbols=json.dumps(pairs, separators=(",", ":")))
    return {r["symbol"]: r for r in rows}


def exchange_info(pairs: list[str]) -> dict[str, dict]:
    data = _binance("/api/v3/exchangeInfo", symbols=json.dumps(pairs, separators=(",", ":")))
    out = {}
    for s in data["symbols"]:
        f = {x["filterType"]: x for x in s["filters"]}
        out[s["symbol"]] = {
            "status": s["status"],
            "step": float(f["LOT_SIZE"]["stepSize"]),
            "min_notional": float(f.get("NOTIONAL", {}).get("minNotional", 5)),
        }
    return out


def yahoo_daily(symbol: str, range_: str = "3mo") -> dict:
    """Returns {closes, price, price_time, market_state_open} for a Yahoo symbol."""
    url = YAHOO + urllib.parse.quote(symbol, safe="") + f"?range={range_}&interval=1d"
    data = json.loads(http_get(url))["chart"]["result"][0]
    meta = data["meta"]
    closes = [c for c in data["indicators"]["quote"][0]["close"] if c is not None]
    period = meta.get("currentTradingPeriod", {}).get("regular", {})
    return {
        "closes": closes,
        "price": meta.get("regularMarketPrice"),
        "price_time": meta.get("regularMarketTime"),
        "session_start": period.get("start"),
        "session_end": period.get("end"),
    }
