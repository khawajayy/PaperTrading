"""News sentiment, macro readings, the economic calendar and earnings dates.

Every source is best effort. A failed source leaves its score neutral and is
reported on the dashboard, so an outage can never cause a trade by itself.
"""
from __future__ import annotations

import email.utils
import json
import math
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .market import http_get, yahoo_daily

NEWS_WINDOW_H = 48
HALF_LIFE_H = 12
VETO_WINDOW_H = 12
VETO_SCORE = -3.0

# Phrase weights are checked against the lower-cased headline. Weights of 3 or more
# mark market-moving events; only those can veto a trade.
POSITIVE = {
    "raises guidance": 4, "raises forecast": 4, "raises outlook": 4, "boosts forecast": 4, "beats estimates": 3,
    "tops estimates": 3, "beat estimates": 3, "earnings beat": 3, "record revenue": 3, "record profit": 3,
    "blowout": 3, "upgrade": 2, "upgraded": 2, "price target raised": 2, "raises price target": 2,
    "major contract": 3, "wins contract": 3, "buyback": 2, "soars": 2, "surges": 2, "jumps": 2, "rallies": 2,
    "rally": 1, "record high": 2, "strong demand": 2, "gains": 1, "rises": 1, "climbs": 1, "bullish": 1,
    "outperform": 1, "partnership": 1, "rate cut": 1, "safe haven": 1, "tariff relief": 2, "export approval": 2,
}
NEGATIVE = {
    "cuts guidance": 4, "lowers guidance": 4, "cuts forecast": 4, "profit warning": 4, "weak guidance": 3,
    "misses estimates": 3, "missed estimates": 3, "earnings miss": 3, "bankruptcy": 5, "fraud": 4,
    "sec investigation": 4, "antitrust": 2, "lawsuit": 2, "export ban": 4, "export controls": 2, "export curbs": 3,
    "new tariffs": 3, "tariffs": 1, "sanctions": 2, "layoffs": 2, "recall": 2, "downgrade": 2, "downgraded": 2,
    "price target cut": 2, "cuts price target": 2, "plunges": 3, "tumbles": 2, "slumps": 2, "sinks": 2,
    "slides": 1, "falls": 1, "drops": 1, "selloff": 2, "sell-off": 2, "bearish": 1, "underperform": 1,
    "rate hike": 1, "crash": 3, "oversupply": 2, "weak demand": 2,
}


def _phrase_re(words) -> re.Pattern:
    alt = "|".join(sorted((re.escape(w) for w in words), key=len, reverse=True))
    return re.compile(rf"\b(?:{alt})\b")


_POS_RE, _NEG_RE = _phrase_re(POSITIVE), _phrase_re(NEGATIVE)


def score_headline(title: str) -> float:
    t = title.lower()
    return float(sum(POSITIVE[m] for m in _POS_RE.findall(t)) - sum(NEGATIVE[m] for m in _NEG_RE.findall(t)))


def _parse_rss(raw: bytes, default_source: str) -> list[dict]:
    items = []
    for it in ET.fromstring(raw).iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate") or ""
        try:
            ts = email.utils.parsedate_to_datetime(pub).timestamp()
        except (TypeError, ValueError):
            continue
        source = (it.findtext("source") or "").strip() or default_source
        suffix = f" - {source}"
        if title.endswith(suffix):
            title = title[: -len(suffix)]
        if title and link.startswith(("https://", "http://")):
            items.append({"title": title, "link": link, "source": source, "ts": ts})
    return items


def fetch_topic(topic: dict) -> tuple[list[dict], int, int]:
    """Returns (items, sources_ok, sources_failed) for one news topic."""
    urls = []
    for q in topic["queries"]:
        query = urllib.parse.quote(f"{q} when:2d")
        urls.append((f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en", "Google News"))
    for t in topic["tickers"]:
        urls.append((f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={t}&region=US&lang=en-US", "Yahoo Finance"))
    items, ok, failed = {}, 0, 0
    # Search feeds return loosely related stories, so the headline must name the topic.
    relevant = _phrase_re(topic["match"])
    for url, source in urls:
        try:
            for item in _parse_rss(http_get(url, retries=1), source):
                key = re.sub(r"[^a-z0-9]+", " ", item["title"].lower()).strip()
                if relevant.search(item["title"].lower()):
                    items.setdefault(key, item)
            ok += 1
        except Exception:
            failed += 1
    return list(items.values()), ok, failed


def summarize_topic(items: list[dict], now: float) -> dict:
    """Recency-weighted sentiment from -1 to +1, plus any high-impact bad news."""
    fresh = [dict(x) for x in items if 0 <= now - x["ts"] <= NEWS_WINDOW_H * 3600]
    weighted = 0.0
    bad_sources: set[str] = set()
    for x in fresh:
        x["score"] = score_headline(x["title"])
        age_h = (now - x["ts"]) / 3600
        weighted += x["score"] * 0.5 ** (age_h / HALF_LIFE_H)
        if age_h <= VETO_WINDOW_H and x["score"] <= VETO_SCORE:
            bad_sources.add(x["source"])
    scored = [x for x in fresh if x["score"]]
    sentiment = math.tanh(weighted / max(math.sqrt(len(scored)), 1) / 3) if scored else 0.0
    fresh.sort(key=lambda x: (-abs(x["score"]), -x["ts"]))
    return {
        "sentiment": round(sentiment, 3),
        "count": len(fresh),
        "scored": len(scored),
        # One alarming headline can be noise; two separate publishers rarely are.
        "veto": len(bad_sources) >= 2,
        "bad_sources": sorted(bad_sources),
        "top": [{k: x[k] for k in ("title", "link", "source", "ts", "score")} for x in fresh[:6]],
    }


def news_adjustment(asset: dict, topics: dict[str, dict], cap: float) -> tuple[float, bool]:
    """Returns (score adjustment, blocked by bad news) for one asset."""
    total_w, value = 0.0, 0.0
    veto = False
    for name, weight in asset["topics"].items():
        t = topics.get(name)
        if not t:
            continue
        total_w += weight
        value += weight * t["sentiment"]
        if weight >= 1 and t["veto"] and asset["news_sign"] > 0:
            veto = True
    sentiment = value / total_w if total_w else 0.0
    return round(sentiment * asset["news_sign"] * cap, 1), veto


# ---- macro --------------------------------------------------------------------
def fetch_macro(symbols: dict[str, str]) -> tuple[dict, list[str]]:
    out, errors = {}, []
    for key, sym in symbols.items():
        try:
            d = yahoo_daily(sym)
            c = d["closes"]
            if len(c) < 25:
                raise ValueError("not enough history")
            ma20 = sum(c[-20:]) / 20
            out[key] = {
                "symbol": sym, "last": round(c[-1], 3), "ma20": round(ma20, 3),
                "chg_5d_pct": round(100 * (c[-1] / c[-6] - 1), 2),
            }
        except Exception as e:
            errors.append(f"{sym}: {e}")
    return out, errors


def macro_points(macro: dict) -> dict[str, float]:
    """Risk view per macro profile, from -1 to +1. Missing readings count as neutral."""
    risk = 0.0
    if "vix" in macro:
        v = macro["vix"]["last"]
        risk += 0.4 if v < 18 else 0.15 if v < 22 else -0.15 if v < 28 else -0.5
    if "smh" in macro:
        s = macro["smh"]
        risk += 0.4 if s["last"] > s["ma20"] else -0.4
        risk += 0.2 if s["chg_5d_pct"] > 0 else -0.2
    gold = 0.0
    if "dxy" in macro:
        gold += 0.4 if macro["dxy"]["chg_5d_pct"] < 0 else -0.4
    if "us10y" in macro:
        gold += 0.4 if macro["us10y"]["chg_5d_pct"] < 0 else -0.4
    if "vix" in macro:
        gold += 0.2 if macro["vix"]["chg_5d_pct"] > 0 else -0.2
    clamp = lambda x: max(-1.0, min(1.0, x))
    return {"semis_long": clamp(risk), "semis_short": clamp(-risk), "gold": clamp(gold)}


# ---- calendars ----------------------------------------------------------------
def fetch_calendar() -> list[dict]:
    """This week's high-impact US events."""
    raw = http_get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", retries=1)
    out = []
    for e in json.loads(raw):
        if e.get("country") == "USD" and e.get("impact") == "High":
            ts = datetime.fromisoformat(e["date"]).timestamp()
            out.append({"title": e["title"], "ts": ts})
    return sorted(out, key=lambda x: x["ts"])


def macro_blackout(events: list[dict], now: float, before_min: int, after_min: int) -> str | None:
    for e in events:
        if -after_min * 60 <= e["ts"] - now <= before_min * 60:
            return e["title"]
    return None


def fetch_earnings(tickers: list[str]) -> tuple[dict[str, float], list[str]]:
    """Next announced earnings date per ticker. Blank means none has been announced."""
    out, errors = {}, []
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    for t in tickers:
        try:
            raw = http_get(f"https://api.nasdaq.com/api/analyst/{t}/earnings-date", retries=1, headers=headers, timeout=15)
            text = json.loads(raw)["data"].get("announcement") or ""
            m = re.search(r"([A-Z][a-z]{2} \d{1,2}, \d{4})", text)
            if m:
                d = datetime.strptime(m.group(1), "%b %d, %Y").replace(tzinfo=timezone.utc)
                out[t] = d.timestamp()
        except Exception as e:
            errors.append(f"{t}: {e}")
    return out, errors
