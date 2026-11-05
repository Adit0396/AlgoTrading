"""Tests for nse_bse.py on hand-made 1-minute prices (no network)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nse_bse as nb  # noqa: E402


class Costs(unittest.TestCase):
    def test_breakevens(self):
        # Holdings route on Rs 1,00,000: STT 200 + stamp 15 + charges ~8 + DP 15.34 ~ Rs 238.4.
        self.assertAlmostEqual(nb.cost("holdings", "NSE", "BSE", 100000), 200 + 15 + 100000 * (0.0000307 + 0.0000375 + 2e-6) * 1.18 + 15.34, 2)
        self.assertAlmostEqual(nb.breakeven("holdings"), 0.002384, 5)
        # Intraday: brokerage Rs 20 x 2 + GST, STT 25, stamp 3, charges ~8.
        self.assertAlmostEqual(nb.breakeven("intraday"), (40 * 1.18 + 25 + 3 + 100000 * (0.0000307 + 0.0000375 + 2e-6) * 1.18) / 100000, 6)


class Episodes(unittest.TestCase):
    def test_one_episode_sized_by_thin_bse(self):
        d = "2026-11-04"
        nse = {(d, "10:00"): (100.0, 50000), (d, "10:01"): (100.0, 50000), (d, "10:02"): (100.0, 50000),
               (d, "10:03"): (100.0, 50000)}
        bse = {(d, "10:00"): (100.05, 900), (d, "10:01"): (100.5, 300), (d, "10:02"): (100.6, 300),
               (d, "10:03"): (100.1, 300)}
        eps = nb.episodes("X", nse, bse, "holdings")
        self.assertEqual(len(eps), 1)                 # 10:01-10:02 above 0.24%, one episode
        e = eps[0]
        self.assertEqual((e["time"], e["minutes"], e["sell_on"], e["buy_on"]), ("10:01", 2, "BSE", "NSE"))
        self.assertEqual(e["qty"], 300)               # capped by what traded on the BSE that minute
        want = 300 * 0.5 - nb.cost("holdings", "NSE", "BSE", 300 * 100.0)
        self.assertAlmostEqual(e["profit"], round(want, 2))
        self.assertAlmostEqual(e["profit"], 67.67, 2)  # Rs 150 gap - Rs 64.50 STT and stamp - Rs 15.34 DP - charges
        # A minute later (10:02) the BSE is still 0.6% dearer: the next-minute trade works too.
        want2 = 300 * 0.6 - nb.cost("holdings", "NSE", "BSE", 300 * 100.0)
        self.assertAlmostEqual(e["next_profit"], round(want2, 2))

    def test_gap_gone_next_minute(self):
        d = "2026-11-04"
        nse = {(d, "10:00"): (100.0, 5000), (d, "10:01"): (100.0, 5000)}
        bse = {(d, "10:00"): (100.5, 5000), (d, "10:01"): (100.0, 5000)}
        e = nb.episodes("X", nse, bse, "holdings")[0]
        self.assertGreater(e["profit"], 0)
        self.assertLess(e["next_profit"], 0)           # only the costs are left

    def test_stale_minutes_ignored(self):
        d = "2026-11-04"
        nse = {(d, "10:00"): (100.0, 1000)}
        bse = {(d, "10:01"): (105.0, 1000)}            # different minutes: never compared
        self.assertEqual(nb.episodes("X", nse, bse, "holdings"), [])


class EndToEnd(unittest.TestCase):
    def test_run_fake(self):
        d = "2026-11-03"
        def fetch(sym):
            base = 200.0 if sym.startswith("A") else 50.0
            bump = 0.006 if sym.endswith(".BO") and sym.startswith("A") else 0.0
            return {(d, "%02d:%02d" % (10 + i // 60, i % 60)): (base * (1 + (bump if i == 30 else 0)), 5000)
                    for i in range(120)}
        with tempfile.TemporaryDirectory() as out:
            self.assertEqual(nb.run(out, [("AAA", "A Ltd"), ("BBB", "B Ltd")], fetch=fetch, workers=1), 0)
            import csv
            import json
            days = list(csv.DictReader(open(os.path.join(out, "days.csv"))))
            self.assertEqual([x["date"] for x in days], [d])
            self.assertEqual(days[0]["episodes_holdings"], "1")
            rows = json.load(open(os.path.join(out, "sheet_nse_bse.json")))
            self.assertTrue(any(r and r[0] == "BY MONTH" for r in rows))
            # A second run adds nothing: stored days are never recomputed.
            nb.run(out, [("AAA", "A Ltd"), ("BBB", "B Ltd")], fetch=fetch, workers=1)
            self.assertEqual(len(list(csv.DictReader(open(os.path.join(out, "days.csv"))))), 1)


if __name__ == "__main__":
    unittest.main()
