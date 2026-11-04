"""Tests for strategies.py and research.py on hand-made 5-minute bars (no network).

Run: python -m unittest discover -s tests
The pack's timemachine.py is needed only for cents_away; a stand-in is used here."""

import datetime
import os
import random
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import research as rs  # noqa: E402
import strategies as st  # noqa: E402

TM = types.SimpleNamespace(cents_away=lambda x, long: math_floor_cents(x) if long else round(x, 2))


def math_floor_cents(x):
    return int(x * 100 + 1e-9) / 100


def bar(o, h, l, c, v=1000):
    return {"o": o, "h": h, "l": l, "c": c, "v": v}


def flat_day(price, n=78, v=1000):
    return {i: bar(price, price + 0.05, price - 0.05, price, v) for i in range(n)}


US = rs.MARKETS["us"]
K = rs.bar_closing(US, US["scan"])          # 8
LAST = rs.bar_closing(US, US["flat"])       # 76


class Clock(unittest.TestCase):
    def test_bar_indices(self):
        self.assertEqual(K, 8)
        self.assertEqual(LAST, 76)
        self.assertEqual(rs.clock(US, K), "10:15")
        self.assertEqual(rs.bar_closing(rs.MARKETS["in"], rs.MARKETS["in"]["scan"]), 8)
        self.assertEqual(rs.bar_closing(rs.MARKETS["in"], rs.MARKETS["in"]["flat"]), 71)


class TradeSim(unittest.TestCase):
    def day(self):
        D = flat_day(101.5)                         # quiet at 101.5 after the scan
        for i in range(3):                          # range 100 to 101
            D[i] = bar(100.5, 101, 100, 100.5)
        D[K] = bar(101, 101.6, 101, 101.5)          # entry 101.5, stop 100.5, risk 1
        return D

    def test_target_and_stop_order(self):
        D = self.day()
        D[K + 1] = bar(101.5, 102.6, 100.4, 102)    # both in one bar: stop first, as the forward test
        r = rs.result(US, D, K, LAST, 101.5, 100.5, 102.5)
        self.assertEqual((r["outcome"], r["r"], r["pnl"]), ("stop", -1.0, -49.0))   # 49 shares: $5,000 cap

    def test_breakeven_moves_stop_from_next_bar(self):
        D = self.day()
        D[K + 1] = bar(101.5, 102.1, 101.4, 102)     # +0.6R: stop moves to entry after this bar
        D[K + 2] = bar(102, 102, 101.4, 101.45)      # back through entry
        r = rs.result(US, D, K, LAST, 101.5, 100.5, 102.5, be_at=0.5)
        self.assertEqual((r["outcome"], r["pnl"]), ("breakeven", 0.0))
        r = rs.result(US, D, K, LAST, 101.5, 100.5, 102.5)
        self.assertEqual(r["outcome"], "closed at bell")

    def test_noon_exit(self):
        D = self.day()
        noon = rs.bar_closing(US, 12 * 60)
        D[noon] = bar(101.9, 102, 101.8, 101.9)
        r = rs.result(US, D, K, LAST, 101.5, 100.5, 102.5, exit_bar=noon)
        self.assertEqual((r["outcome"], r["exit"], r["pnl"]), ("time exit", "12:00", 19.6))

    def test_variants_pullback_and_confirm(self):
        D = self.day()
        D[K + 2] = bar(101.2, 101.3, 100.9, 101.1)   # dips to the range high 101: filled there
        D[K + 4] = bar(101.1, 101.6, 101.0, 101.5)   # +0.5 from 101, risk 0.5 -> target hit
        v, entry, stop, risk = rs.variants(US, TM, {"D": D, "k": K, "oh": 101.0, "ol": 100.0}, LAST)
        self.assertEqual((entry, stop, risk), (101.5, 100.5, 1.0))
        self.assertEqual(v["pullback"]["outcome"], "target")   # filled at 101, target 101.5 at the next bar
        self.assertEqual(v["pullback"]["pnl"], 24.5)      # 49 shares x 0.5
        self.assertEqual(v["confirm"]["outcome"], "closed at bell")
        self.assertEqual(v["stop_low"]["shares"], 33)     # risk 1.5: 50 / 1.5


def make_charts(n_days=25, seed=3):
    """Synthetic market: 12 stocks, an index, all with the same calendar."""
    rnd = random.Random(seed)
    start = datetime.date(2026, 9, 1)
    days = []
    d = start
    while len(days) < n_days:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += datetime.timedelta(days=1)
    charts = {}
    for s in range(12):
        sym = "S%02d" % s
        charts[sym] = {}
        p = 50 + 10 * s
        for iso in days:
            D, x = {}, p
            for i in range(78):
                step = rnd.gauss(0.0004 if i < 12 else 0, 0.003) * x
                o, c = x, x + step
                D[i] = bar(round(o, 2), round(max(o, c) * 1.001, 2), round(min(o, c) * 0.999, 2), round(c, 2),
                           v=rnd.randint(50000, 400000))
                x = c
            charts[sym][iso] = D
            p = x
    idx = {iso: flat_day(500.0) for iso in days}
    return charts, idx, days


class Rebuild(unittest.TestCase):
    def test_candidates_score_learn_race(self):
        charts, idx, days = make_charts()
        names = {s: "Company " + s for s in charts}
        by_day = {}
        for iso in days:
            c = rs.candidates_for_day(US, TM, iso, charts, names, idx)
            if c:
                by_day[iso] = c
        self.assertTrue(by_day)
        for iso, cands in by_day.items():
            dv = [c["feat"]["dv"] for c in cands]
            self.assertEqual(dv, sorted(dv, reverse=True))            # ranked by money traded
            for c in cands:
                D = charts[c["symbol"]][iso]
                self.assertGreater(D[K]["c"], c["or_high"])           # still above the range at the scan
                self.assertEqual(c["entry"], round(D[K]["c"], 2))
                self.assertIn(c["var"]["base"]["outcome"], ("stop", "target", "closed at bell"))
        # The history round-trips through the CSV.
        path = os.path.join(HERE, "_hist.csv")
        try:
            rs.save_history(path, by_day)
            back = rs.load_history(path)
        finally:
            os.remove(path)
        self.assertEqual(sorted(back), sorted(by_day))
        d0 = sorted(by_day)[0]
        self.assertEqual(back[d0][0]["var"]["base"]["pnl"], by_day[d0][0]["var"]["base"]["pnl"])
        # Scoring: the base rule's P&L is the sum of the top 3 a day.
        s = rs.score(back, sorted(back))
        want = sum(c["var"]["base"]["pnl"] for d in back for c in back[d][:3])
        self.assertAlmostEqual(s["pnl"], round(want, 2), places=2)
        self.assertAlmostEqual(sum(s["halves"]), s["pnl"], places=1)
        exit_key, skips, evals = rs.learn(back, sorted(back))
        self.assertIn(exit_key, ["base"] + [k for k, _, _ in rs.EXITS])
        # Race on the last day, with the index as both ETFs.
        etfs = {"SPY": idx, "QQQ": idx}
        out = rs.race_day(US, days[-1], charts, names, etfs, by_day)
        self.assertIn("v2", out)
        self.assertIn("sip", out)
        self.assertEqual(out["vwap"][1], 0.0)          # a flat index makes nothing
        self.assertEqual(out["momentum"][1], 0.0)
        rows = rs.sheet_rows_race(US, {days[-1]: out}, [days[-1]], days[-1])
        self.assertTrue(any(r and r[0] == "Current rule" for r in rows))


class Evidence(unittest.TestCase):
    def test_t_stat_and_verdict(self):
        base = {"trades": 60, "pnl": 0.0, "halves": [0.0, 0.0], "daily": [0.0] * 20}
        same = dict(base)
        self.assertEqual(rs.t_stat(base, same), 0.0)
        steady = {"trades": 60, "pnl": 200.0, "halves": [100.0, 100.0], "daily": [10.0 + (i % 2) for i in range(20)]}
        self.assertGreater(rs.t_stat(base, steady), rs.PROVEN_T)
        self.assertTrue(rs.verdict(base, steady).startswith("Proven"))
        lucky = {"trades": 60, "pnl": 40.0, "halves": [20.0, 20.0], "daily": [100.0, -98.0] * 10}
        self.assertFalse(rs.verdict(base, lucky).startswith("Proven"))   # better in both halves, but noise
        few = dict(steady, trades=10)
        self.assertEqual(rs.verdict(base, few), "Too few trades to judge")


class Strategies(unittest.TestCase):
    def test_stocks_in_play_long_stop_and_close(self):
        st0 = {"atr": 2.0, "avg_vol": 2e6, "avg_first_vol": 1e5, "prev_close": 100}
        D = flat_day(100.5)
        D[0] = bar(100, 101, 99.8, 100.8)                 # up bar: buy stop at 101
        D[3] = bar(100.9, 101.3, 100.9, 101.2)            # triggers at 101; stop 100.8
        t = st.stocks_in_play_trade("X", D, st0, LAST, 5000, 50, US["open"])
        self.assertEqual((t["side"], t["entry"], t["entry_time"]), ("long", 101.0, "09:50"))
        self.assertEqual(t["shares"], 49)                 # min(50/0.2=250, 5000/101=49)
        self.assertEqual(t["outcome"], "stop")            # flat 100.5 day afterwards falls through 100.8
        self.assertEqual(t["pnl"], round(49 * -0.2, 2))
        D2 = dict(D)
        for i in range(4, 78):
            D2[i] = bar(101.5, 101.6, 101.4, 101.5)
        t = st.stocks_in_play_trade("X", D2, st0, LAST, 5000, 50, US["open"])
        self.assertEqual((t["outcome"], t["pnl"]), ("closed at bell", round(49 * 0.5, 2)))

    def test_stocks_in_play_picks_relative_volume(self):
        days = {}
        for n in range(16):
            iso = "2026-09-%02d" % (n + 1)
            days[iso] = {i: bar(20, 21, 19, 20, v=100000) for i in range(78)}
        hot = dict(days)
        today = "2026-09-17"
        hot[today] = {i: bar(20, 21, 19, 20.5 if i == 0 else 20, v=300000 if i == 0 else 100000) for i in range(78)}
        cold = dict(days)
        cold[today] = {i: bar(20, 21, 19, 20.5 if i == 0 else 20, v=50000) for i in range(78)}
        picks = st.stocks_in_play_picks({"HOT": hot, "COLD": cold}, today, 5, 1e6, 0.5, {}, rs.ft.one_per_company)
        self.assertEqual([p[1] for p in picks], ["HOT"])
        self.assertAlmostEqual(picks[0][0], 3.0)

    def test_stocks_in_play_fifteen_minute_range(self):
        st0 = {"atr": 20.0, "avg_vol": 2e6, "avg_first_vol": 1e5, "prev_close": 1000}
        D = {i: bar(1010, 1011, 1009, 1010) for i in range(72)}
        D[0] = bar(1000, 1004, 999, 1003)
        D[1] = bar(1003, 1006, 1002, 1005)
        D[2] = bar(1005, 1007, 1004, 1006)                 # range 999-1007, closed up: buy stop 1007
        t = st.stocks_in_play_trade("X", D, st0, 71, 500000, 5000, rs.MARKETS["in"]["open"], range_bars=3)
        self.assertEqual((t["side"], t["entry"], t["entry_time"]), ("long", 1010.0, "09:35"))  # opened above 1007
        self.assertEqual(t["shares"], 495)                 # min(5000 / 2.0, 500000 / 1010)
        self.assertEqual((t["outcome"], t["pnl"]), ("closed at bell", 0.0))

    def test_vwap_trend_flips(self):
        D = {}
        prices = [100, 101, 102, 101, 99, 98, 99, 100]
        for i, p in enumerate(prices):
            D[i] = bar(p, p, p, p, v=100)
        ts = st.vwap_trend("Q", D, len(prices) - 1, 1000, US["open"])
        self.assertEqual([t["side"] for t in ts], ["long", "short"])
        self.assertEqual(ts[0]["entry"], 101)            # bar 0 equals its own VWAP: no position yet
        self.assertEqual(ts[-1]["outcome"], "closed at bell")
        total = sum(t["pnl"] for t in ts)
        self.assertAlmostEqual(total, ts[0]["pnl"] + ts[1]["pnl"])

    def test_intraday_momentum(self):
        D = flat_day(100.0)
        D[5] = bar(100, 101, 100, 101)                    # +1% from the previous close of 100
        D[71] = bar(102, 102, 102, 102)
        D[77] = bar(103, 103, 103, 103)
        t = st.intraday_momentum("SPY", D, 100.0, 5, 71, 77, 5000, US["open"])[0]
        self.assertEqual((t["side"], t["entry"], t["exit"], t["shares"]), ("long", 102, 103, 49))
        self.assertEqual(t["pnl"], 49.0)
        self.assertEqual((t["entry_time"], t["exit_time"]), ("15:30", "16:00"))

    def test_noise_area_breakout_and_exit(self):
        days = {}
        for n in range(14):
            days["2026-09-%02d" % (n + 1)] = {i: bar(100, 100.6, 99.4, 100.5 if i else 100, v=10) for i in range(78)}
        today = "2026-09-20"
        D = {i: bar(100, 100, 100, 100, v=10) for i in range(78)}
        D[0] = bar(100, 100, 100, 100, v=10)
        for i in range(11, 30):
            D[i] = bar(101.5, 101.5, 101.5, 101.5, v=10)  # above 100.5 x 1.005 (gap-adjusted) at 10:30
        days[today] = D
        ts = st.noise_area("SPY", days, today, LAST, 5000, US["open"])
        self.assertEqual(ts[0]["side"], "long")
        self.assertEqual(ts[0]["entry_time"], "10:30")
        self.assertEqual((ts[0]["entry"], ts[0]["exit"]), (101.5, 100))   # back to 100 at the 12:30 check
        self.assertEqual(ts[0]["exit_time"], "12:30")
        self.assertEqual(ts[0]["outcome"], "back inside")


if __name__ == "__main__":
    unittest.main()
