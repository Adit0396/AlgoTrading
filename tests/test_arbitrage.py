"""Tests for arbitrage.py on hand-made daily prices (no network)."""

import datetime
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import arbitrage as ab  # noqa: E402


def days(n, start="2025-01-01"):
    d, out = datetime.date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


class Premium(unittest.TestCase):
    def test_parity_india_and_australia(self):
        # HDB: 1 US share = 3 NSE shares. NSE 900 INR, 90 INR per US$ -> parity US$30; US at 33 -> +10%.
        s = ab.premium_series({"d": 33.0}, {"d": 900.0}, {"d": 90.0}, 3, True)
        self.assertAlmostEqual(s[0][4], 0.10)
        # BHP: 1 US share = 2 ASX shares. ASX A$40, 0.65 US$ per A$ -> parity US$52; US at 52 -> 0%.
        s = ab.premium_series({"d": 52.0}, {"d": 40.0}, {"d": 0.65}, 2, False)
        self.assertAlmostEqual(s[0][4], 0.0)

    def test_fx_carried_forward(self):
        s = ab.premium_series({"a": 10.0, "b": 10.0}, {"a": 900.0, "b": 900.0}, {"a": 90.0}, 1, True)
        self.assertEqual(len(s), 2)
        self.assertAlmostEqual(s[1][4], 0.0)


class Strategy(unittest.TestCase):
    def series(self, prem):
        ds = days(len(prem))
        fx, loc = 90.0, 900.0
        return [(d, 10.0 * (1 + p), loc, fx, p) for d, p in zip(ds, prem)]

    def test_z_needs_window_and_resets_on_break(self):
        prem = [0.02 + 0.001 * math.sin(i) for i in range(70)]
        zs = ab.zscores(self.series(prem))
        self.assertIsNone(zs[ab.WINDOW - 1][2])
        self.assertIsNotNone(zs[ab.WINDOW][2])
        prem2 = prem[:65] + [0.2] + [0.2 + 0.001 * math.sin(i) for i in range(10)]
        zs = ab.zscores(self.series(prem2))
        self.assertTrue(zs[65][3])
        self.assertTrue(all(z[2] is None for z in zs[65:]))   # history restarted

    def test_convergence_trade_makes_money_after_costs(self):
        prem = [0.02 + 0.002 * math.sin(i) for i in range(65)]
        prem += [0.035, 0.036, 0.03, 0.022, 0.02, 0.02, 0.02]   # strays up, then comes back
        ser = self.series(prem)
        pair = ("X", "X Ltd", "India ↔ US", "X", "X.NS", 1, "INR=X", True)
        ts = ab.simulate(pair, ser, ab.zscores(ser))
        self.assertEqual(len(ts), 1)
        t = ts[0]
        self.assertEqual(t["trade"], "sell US, buy local")
        self.assertEqual(t["entry_date"], ser[66][0])                 # the day after the signal
        self.assertEqual(t["why"], "back to normal")
        # US share fell from 10.36 to ~10.2 while the local leg was flat: ~US$5000 x 1.5%.
        self.assertGreater(t["gross_usd"], 60)
        self.assertLess(t["pnl_usd"], t["gross_usd"])                 # costs taken off
        want_costs = 2 * 5000 * (0.0005 + 0.0015 + 0.0010)
        self.assertAlmostEqual(t["costs_usd"], round(want_costs + 5000 * 0.03 * (
            (datetime.date.fromisoformat(t["exit_date"]) - datetime.date.fromisoformat(t["entry_date"])).days) / 365, 2))

    def test_corporate_action_closes_at_clean_prices(self):
        prem = [0.02 + 0.002 * math.sin(i) for i in range(65)] + [0.035, 0.036, 0.30, 0.30]
        ser = self.series(prem)
        pair = ("X", "X Ltd", "India ↔ US", "X", "X.NS", 1, "INR=X", True)
        t = ab.simulate(pair, ser, ab.zscores(ser))[0]
        self.assertEqual(t["why"], "corporate action")
        self.assertEqual(t["exit_date"], ser[66][0])                  # last clean day, not the jump day
        self.assertLess(abs(t["gross_usd"]), 10)


class EndToEnd(unittest.TestCase):
    def test_run_with_fake_data(self):
        ds = days(300, "2025-01-01")
        data = {}
        for k, name, region, us, loc, ratio, fx, inv in ab.PAIRS:
            rate = 85.0 if inv else 0.66
            data[fx] = {d: rate for d in ds}
            base = 1000.0
            data[loc] = {d: base * (1 + 0.01 * math.sin(i / 7)) for i, d in enumerate(ds)}
            par = [(base * (1 + 0.01 * math.sin(i / 7))) * ratio / rate if inv else
                   (base * (1 + 0.01 * math.sin(i / 7))) * ratio * rate for i in range(len(ds))]
            data[us] = {d: par[i] * (1.03 + 0.004 * math.sin(i * 1.7) + (0.03 if i % 50 == 49 else 0))
                        for i, d in enumerate(ds)}
        data["AUDUSD=X"] = {d: 0.66 for d in ds}
        with tempfile.TemporaryDirectory() as out:
            rc = ab.run(out, fetch=lambda s: data[s])
            self.assertEqual(rc, 0)
            import json
            rows = json.load(open(os.path.join(out, "sheet_arbitrage.json")))
            self.assertTrue(any(r and r[0] == "MONTHLY P&L (A$)" for r in rows))
            summ = json.load(open(os.path.join(out, "summary.json")))
            self.assertEqual(len(summ["latest"]), len(ab.PAIRS))
            self.assertTrue(summ["monthly_aud"])


if __name__ == "__main__":
    unittest.main()
