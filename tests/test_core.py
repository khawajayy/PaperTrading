import copy
import json
import math
import unittest
from datetime import datetime, timezone
from pathlib import Path

from paper_trader import events, indicators as ind
from paper_trader.indicators import Candle, HOUR_MS
from paper_trader.market import split_closed
from paper_trader.portfolio import Portfolio, reconcile, round_down
from paper_trader.run import is_weekend, premium_pct
from paper_trader.strategy import analyze

CFG = json.loads((Path(__file__).parent.parent / "config.json").read_text(encoding="utf-8"))
NOW = 1_790_000_000.0


def trend(n, start=100.0, step=0.2, wiggle=0.6):
    out, t = [], 1_700_000_000_000 // HOUR_MS * HOUR_MS
    price = start
    for i in range(n):
        o = price
        price = max(1.0, price + step + wiggle * math.sin(i / 3))
        out.append(Candle(t + i * HOUR_MS, o, max(o, price) + 0.2, min(o, price) - 0.2, price, 100.0))
    return out


def new_portfolio():
    state = Portfolio.fresh_state(CFG)
    pf = Portfolio(copy.deepcopy(CFG), state)
    pf.deposit(NOW, CFG["account"]["starting_cash"])
    state["started_at"] = "x"
    return pf


def ledger_of(pf):
    return [{k: str(v) for k, v in row.items()} for row in pf.new_ledger]


class Indicators(unittest.TestCase):
    def test_ema_seeds_with_average(self):
        self.assertEqual(ind.ema([1, 2, 3, 4], 3), [None, None, 2.0, 3.0])

    def test_rsi_extremes(self):
        self.assertEqual(ind.rsi([float(i) for i in range(30)])[-1], 100.0)
        self.assertEqual(ind.rsi([float(30 - i) for i in range(30)])[-1], 0.0)

    def test_atr_of_constant_range(self):
        c = [Candle(i, 10, 11, 9, 10, 1) for i in range(40)]
        self.assertAlmostEqual(ind.atr(c)[-1], 2.0)

    def test_adx_strong_in_steady_uptrend(self):
        c = [Candle(i, 10 + i, 11 + i, 9.5 + i, 10.5 + i, 1) for i in range(80)]
        a, plus, minus = ind.adx(c)
        self.assertGreater(a[-1], 50)
        self.assertGreater(plus[-1], minus[-1])

    def test_resample_drops_incomplete_bucket(self):
        c = trend(4 * 10 + 2)
        c = [x for x in c if x.t >= c[0].t - c[0].t % (4 * HOUR_MS) + 4 * HOUR_MS]
        out = ind.resample(c, 4 * HOUR_MS)
        self.assertTrue(all(x.t % (4 * HOUR_MS) == 0 for x in out))
        self.assertLessEqual(len(out) * 4, len(c))

    def test_split_closed_hides_forming_candle(self):
        c = trend(5)
        closed, forming = split_closed(c, HOUR_MS, c[-1].t + 10)
        self.assertEqual(len(closed), 4)
        self.assertEqual(forming, c[-1])
        self.assertEqual(split_closed(c, HOUR_MS, c[-1].t + HOUR_MS)[1], None)


class Strategy(unittest.TestCase):
    def test_uptrend_scores_above_downtrend(self):
        up = analyze(trend(300), [])
        down = analyze(trend(300, start=400, step=-0.6), [])
        self.assertGreater(up["score"], 0)
        self.assertLess(down["score"], 0)

    def test_needs_history(self):
        self.assertIsNone(analyze(trend(50), []))

    def test_no_lookahead(self):
        c = trend(200)
        first = analyze(c, [])
        spiked = c + [Candle(c[-1].t + HOUR_MS, 1, 9999, 1, 9999, 1e9)]
        self.assertEqual(analyze(spiked[:-1], [])["score"], first["score"])


class Accounting(unittest.TestCase):
    def test_round_trip_reconciles_with_fees(self):
        pf = new_portfolio()
        pf.buy(NOW, "INTCB", 10, 50.0, 45.0, 2.0, 60, "test")
        reconcile(pf.state, ledger_of(pf), 10_000)
        self.assertAlmostEqual(pf.state["cash"], 10_000 - 500 - 0.5)
        trade = pf.sell(NOW + 7200, "INTCB", 55.0, "test exit")
        reconcile(pf.state, ledger_of(pf), 10_000)
        self.assertAlmostEqual(trade["pnl"], 50 - 0.5 - 0.55)
        self.assertAlmostEqual(pf.state["cash"], 10_000 + trade["pnl"])
        self.assertAlmostEqual(trade["r_multiple"], trade["pnl"] / 50, places=3)

    def test_reconcile_catches_tampering(self):
        pf = new_portfolio()
        pf.buy(NOW, "INTCB", 10, 50.0, 45.0, 2.0, 60, "test")
        rows = ledger_of(pf)
        rows[1]["amount"] = "-1"
        with self.assertRaises(ValueError):
            reconcile(pf.state, rows, 10_000)
        pf.state["positions"]["INTCB"]["qty"] = 11
        with self.assertRaises(ValueError):
            reconcile(pf.state, ledger_of(pf), 10_000)

    def test_stop_hit_before_high_and_gap_fills_at_open(self):
        pf = new_portfolio()
        pf.buy(NOW, "INTCB", 10, 50.0, 45.0, 2.0, 60, "test")
        t = int(NOW + 600) * 1000
        trade = pf.apply_bar("INTCB", Candle(t, 44.0, 70.0, 43.0, 60.0, 1), True, 0.05)
        self.assertLess(trade["exit_price"], 44.0)
        self.assertNotIn("INTCB", pf.state["positions"])
        self.assertIn("INTCB", pf.state["cooldown_until"])

    def test_trailing_stop_only_rises(self):
        pf = new_portfolio()
        pf.buy(NOW, "INTCB", 10, 50.0, 45.0, 2.0, 60, "test")
        t = int(NOW + 600) * 1000
        self.assertIsNone(pf.apply_bar("INTCB", Candle(t, 50, 60, 49, 59, 1), True, 0.05))
        self.assertEqual(pf.state["positions"]["INTCB"]["stop"], 54.0)
        self.assertIsNone(pf.apply_bar("INTCB", Candle(t + 300_000, 59, 59.5, 55, 56, 1), True, 0.05))
        self.assertEqual(pf.state["positions"]["INTCB"]["stop"], 54.0)
        trade = pf.apply_bar("INTCB", Candle(t + 600_000, 56, 56, 53, 54, 1), True, 0.05)
        self.assertEqual(trade["exit_reason"], "Trailing stop")

    def test_forming_candle_cannot_raise_stop(self):
        pf = new_portfolio()
        pf.buy(NOW, "INTCB", 10, 50.0, 45.0, 2.0, 60, "test")
        pf.apply_bar("INTCB", Candle(int(NOW + 600) * 1000, 50, 80, 49, 79, 1), False, 0.05)
        self.assertEqual(pf.state["positions"]["INTCB"]["stop"], 45.0)

    def test_round_down_respects_step(self):
        self.assertEqual(round_down(1.23456, 0.001), 1.234)
        self.assertEqual(round_down(0.3, 0.1), 0.3)


class RiskRules(unittest.TestCase):
    def test_bull_and_bear_semis_never_coexist(self):
        pf = new_portfolio()
        pf.buy(NOW, "SOXLB", 1, 60.0, 55.0, 1.0, 60, "t")
        self.assertIn("conflicts", pf.portfolio_block("SOXSB", 10_000, {}, NOW))

    def test_only_one_gold_token(self):
        pf = new_portfolio()
        pf.buy(NOW, "XAUT", 0.1, 4000.0, 3900.0, 10.0, 60, "t")
        self.assertIn("gold", pf.portfolio_block("PAXG", 10_000, {}, NOW))

    def test_max_positions(self):
        pf = new_portfolio()
        for s in ("INTCB", "SNDKB", "MUUB", "KORUB"):
            pf.buy(NOW, s, 1, 10.0, 9.0, 0.5, 60, "t")
        self.assertIn("max positions", pf.portfolio_block("XAUT", 10_000, {}, NOW))

    def test_daily_loss_halt(self):
        pf = new_portfolio()
        pf.state["day"] = {"date": "x", "start_equity": 10_000}
        self.assertIn("daily loss", pf.portfolio_block("XAUT", 9_600, {}, NOW))

    def test_size_risks_one_percent_when_uncapped(self):
        pf = new_portfolio()
        qty = pf.size("XAUT", 100.0, 95.0, 10_000, {}, None)
        self.assertAlmostEqual(qty * 5, 100.0)

    def test_size_capped_by_leverage_limit(self):
        pf = new_portfolio()
        qty = pf.size("SOXLB", 60.0, 59.9, 10_000, {}, None)
        self.assertAlmostEqual(qty * 60.0, 1000.0)

    def test_size_capped_by_group_exposure(self):
        pf = new_portfolio()
        pf.buy(NOW, "INTCB", 39, 100.0, 90.0, 1.0, 60, "t")
        qty = pf.size("SNDKB", 100.0, 99.0, 10_000, {"INTCB": 100.0}, None)
        self.assertAlmostEqual(qty * 100.0, 100.0)

    def test_drawdown_halves_risk(self):
        pf = new_portfolio()
        pf.state["peak_equity"] = 12_000
        qty = pf.size("XAUT", 100.0, 95.0, 10_000, {}, None)
        self.assertAlmostEqual(qty * 5, 50.0)

    def test_size_capped_by_liquidity(self):
        pf = new_portfolio()
        qty = pf.size("XAUT", 100.0, 95.0, 10_000, {}, 50_000)
        self.assertAlmostEqual(qty * 100.0, 500.0)


class Events(unittest.TestCase):
    def test_headline_scores(self):
        self.assertGreater(events.score_headline("Micron raises guidance on strong demand"), 3)
        self.assertLess(events.score_headline("Intel plunges after profit warning"), -3)
        self.assertEqual(events.score_headline("Intel to present at a conference"), 0)
        self.assertEqual(events.score_headline("Fraudulent claims"), 0)

    def _item(self, title, source, age_h=1):
        return {"title": title, "link": "https://x", "source": source, "ts": NOW - age_h * 3600}

    def test_veto_needs_two_publishers(self):
        one = [self._item("Intel plunges", "A"), self._item("Intel plunges again", "A")]
        two = [self._item("Intel plunges", "A"), self._item("Intel plunges today", "B")]
        self.assertFalse(events.summarize_topic(one, NOW)["veto"])
        self.assertTrue(events.summarize_topic(two, NOW)["veto"])

    def test_old_news_ignored(self):
        s = events.summarize_topic([self._item("Intel plunges", "A", age_h=100)], NOW)
        self.assertEqual((s["count"], s["sentiment"]), (0, 0.0))

    def test_bear_token_flips_news(self):
        topics = {"semis": {"sentiment": -0.5, "veto": True}}
        adj, veto = events.news_adjustment(CFG["assets"]["SOXSB"], topics, 15)
        self.assertEqual(adj, 7.5)
        self.assertFalse(veto)
        adj, veto = events.news_adjustment(CFG["assets"]["SOXLB"], topics, 15)
        self.assertEqual(adj, -7.5)
        self.assertTrue(veto)

    def test_missing_news_is_neutral(self):
        self.assertEqual(events.news_adjustment(CFG["assets"]["XAUT"], {}, 15), (0.0, False))

    def test_macro_opposite_for_bear_semis_and_neutral_when_missing(self):
        m = {"vix": {"last": 15, "chg_5d_pct": -1}, "smh": {"last": 110, "ma20": 100, "chg_5d_pct": 2}}
        v = events.macro_points(m)
        self.assertGreater(v["semis_long"], 0)
        self.assertEqual(v["semis_short"], -v["semis_long"])
        self.assertEqual(events.macro_points({}), {"semis_long": 0.0, "semis_short": 0.0, "gold": 0.0})

    def test_blackout_window(self):
        ev = [{"title": "CPI", "ts": NOW + 1800}]
        self.assertEqual(events.macro_blackout(ev, NOW, 60, 30), "CPI")
        self.assertIsNone(events.macro_blackout(ev, NOW - 7200, 60, 30))
        self.assertEqual(events.macro_blackout(ev, NOW + 3000, 60, 30), "CPI")
        self.assertIsNone(events.macro_blackout(ev, NOW + 3700, 60, 30))

    def test_rss_parse_strips_publisher_and_rejects_bad_links(self):
        raw = b"""<rss><channel>
        <item><title>Intel soars - Reuters</title><link>https://r/1</link>
          <pubDate>Thu, 05 Nov 2026 10:00:00 GMT</pubDate><source>Reuters</source></item>
        <item><title>Bad link</title><link>javascript:alert(1)</link>
          <pubDate>Thu, 05 Nov 2026 10:00:00 GMT</pubDate></item>
        </channel></rss>"""
        items = events._parse_rss(raw, "Feed")
        self.assertEqual([x["title"] for x in items], ["Intel soars"])
        self.assertEqual(items[0]["source"], "Reuters")


class Sessions(unittest.TestCase):
    def test_weekend_window(self):
        at = lambda day, hour: datetime(2026, 11, day, hour, tzinfo=timezone.utc).timestamp()
        self.assertFalse(is_weekend(at(5, 12)))  # Thursday
        self.assertTrue(is_weekend(at(6, 22)))  # Friday evening
        self.assertTrue(is_weekend(at(7, 12)))  # Saturday
        self.assertTrue(is_weekend(at(8, 12)))  # Sunday
        self.assertFalse(is_weekend(at(8, 23)))  # Sunday night

    def test_premium_only_during_session(self):
        ref = {"price": 100.0, "price_time": NOW - 60, "session_start": NOW - 3600, "session_end": NOW + 3600}
        self.assertEqual(premium_pct(102.0, ref, NOW), 2.0)
        self.assertIsNone(premium_pct(102.0, ref, NOW + 7200))
        self.assertIsNone(premium_pct(102.0, {**ref, "price_time": NOW - 4000}, NOW))
        self.assertIsNone(premium_pct(102.0, None, NOW))


if __name__ == "__main__":
    unittest.main()
