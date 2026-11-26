"""Tests for rule v3 (learned.py): which learned rule a session gets, and the India forward test
trading it (skips pass a stock over, a learned exit replaces v2's). Hand-made bars, no network.

Run: python -m unittest discover -s tests"""

import csv
import datetime
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import forward_test_india as fi  # noqa: E402
import learned as lr  # noqa: E402
import research as rs  # noqa: E402

TM = types.SimpleNamespace(
    cents_away=lambda x, long: int(x * 100 + 1e-9) / 100 if long else round(x, 2),
    clock12=lambda mins: "%d:%02d %s" % ((mins // 60 - 1) % 12 + 1, mins % 60, "AM" if mins < 720 else "PM"),
    OPEN_MIN=fi.OPEN_MIN, MIN_SESSIONS=20, full_sessions=lambda days, iso: ([], None))
DAY = "2026-11-25"
PRIOR = ["2026-11-%02d" % d for d in (17, 18, 19, 20, 21, 24)]


def bar(o, h, l, c, v, i=0):
    t = fi.OPEN_MIN + 5 * i
    return {"o": o, "h": h, "l": l, "c": c, "v": v, "start": "%02d:%02d" % (t // 60, t % 60)}


def session(vol, late=False, up=True):
    """72 bars: a 99-101 range for 15 minutes, a breakout (at 9:30, or at 9:50 when late), then a climb."""
    D = {}
    for i in range(72):
        if i < 3:
            D[i] = bar(100, 101, 99, 100, vol, i)
        elif i <= 8:
            c = 100.5 if (late and i < 7) else 101.5
            D[i] = bar(c, c + 0.05, c - 0.05, c, vol, i)
        else:
            c = min(101.5 + 0.4 * (i - 8), 107) if up else 101.5
            D[i] = bar(c - 0.1, c + 0.05, c - 0.15, c, vol, i)
    return D


def charts():
    out = {}
    for sym, vol, late in (("AAA", 50000, True), ("BBB", 40000, False), ("CCC", 30000, False), ("DDD", 25000, False)):
        days = {d: {i: bar(100, 100.2, 99.8, 100, vol, i) for i in range(72)} for d in PRIOR}
        days[DAY] = session(vol, late)
        out[sym] = days
    return out


def args(state):
    return types.SimpleNamespace(or_minutes=15, min_price=50.0, min_volume=100000, seed=19, top=3, target_r=1.0,
                                 account=500000.0, max_loss=5000.0, replays=10, news_hours=18, state=state)


class RuleFor(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.state, "review"))

    def tearDown(self):
        shutil.rmtree(self.state)

    def write_lessons(self, last, exit_key, skips, text):
        with open(os.path.join(self.state, "review", "lessons.json"), "w") as fh:
            json.dump({"last": last, "learned_exit": exit_key, "learned_skips": skips, "learned_rule": text}, fh)

    def test_no_review_is_current_rule(self):
        self.assertEqual(lr.rule_for(self.state, DAY)[:3], ("base", [], "Current rule"))

    def test_next_session_uses_newest_lessons(self):
        self.write_lessons("2026-11-24", "t2", ["late"], "Current rule + Target 2R; skip if fresh breakout")
        exit_key, skips, text, src = lr.rule_for(self.state, DAY)
        self.assertEqual((exit_key, skips), ("t2", ["late"]))
        self.assertIn("2026-11-24", src)

    def test_lessons_never_used_on_their_own_sessions(self):
        # Lessons that already include the day would be hindsight: fall back to the current rule.
        self.write_lessons(DAY, "t2", ["late"], "x")
        self.assertEqual(lr.rule_for(self.state, DAY)[:2], ("base", []))

    def test_rerun_of_a_logged_day_gets_the_rule_it_had(self):
        self.write_lessons("2026-11-30", "t05", [], "Current rule + Target 0.5R")
        with open(os.path.join(self.state, "review", "learned.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["date", "rule", "exit", "skips", "picks", "pnl"])
            w.writeheader()
            w.writerow({"date": DAY, "rule": "Current rule + skip if fresh breakout", "exit": "base", "skips": "late",
                        "picks": "BBB CCC DDD", "pnl": 0})
        self.assertEqual(lr.rule_for(self.state, DAY)[:2], ("base", ["late"]))

    def test_unknown_keys_are_ignored(self):
        self.write_lessons("2026-11-24", "moon", ["nope", "gap"], "x")
        self.assertEqual(lr.rule_for(self.state, DAY)[:2], ("base", ["gap"]))


class IndiaPicks(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.state, "review"))
        self.charts = charts()
        self.names = {s: "Company " + s for s in self.charts}

    def tearDown(self):
        shutil.rmtree(self.state)

    def run_day(self, exit_key="base", skips=(), text="Current rule"):
        with open(os.path.join(self.state, "review", "lessons.json"), "w") as fh:
            json.dump({"last": PRIOR[-1], "learned_exit": exit_key, "learned_skips": list(skips),
                       "learned_rule": text}, fh)
        return fi.pick_day(TM, None, datetime.date.fromisoformat(DAY), self.charts, {}, self.names,
                           args(self.state))

    def test_current_rule_is_v2(self):
        rows = self.run_day()
        self.assertEqual([r["symbol"] for r in rows], ["AAA", "BBB", "CCC"])   # by turnover
        self.assertTrue(all(r["rule"] == "v3" for r in rows))
        self.assertTrue(all(r["actual_outcome"] == "target" for r in rows))
        r = rows[1]
        self.assertAlmostEqual(r["target"], round(r["entry"] + r["risk_per_share"], 2))
        self.assertNotIn("learned rule", r.get("note", ""))

    def test_skip_passes_over_and_next_takes_its_place(self):
        rows = self.run_day(skips=["late"], text="Current rule + skip if fresh breakout")
        self.assertEqual([r["symbol"] for r in rows], ["BBB", "CCC", "DDD"])
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3])
        self.assertIn("learned rule: Current rule + skip if fresh breakout", rows[0]["note"])
        self.assertIn("passed over AAA (fresh breakout)", rows[0]["note"])

    def test_learned_exit_replaces_v2_exit(self):
        rows = self.run_day(exit_key="t2", text="Current rule + Target 2R")
        m = lr.market("in", 500000.0, 5000.0)
        for r in rows:
            D = self.charts[r["symbol"]][DAY]
            want = rs.variants(m, TM, {"D": D, "k": 8, "oh": 101, "ol": 99}, 71)[0]["t2"]
            self.assertAlmostEqual(r["target"], round(r["entry"] + 2 * r["risk_per_share"], 2))
            self.assertEqual(r["actual_outcome"], "target")
            self.assertEqual(r["pnl_usd"], want["pnl"])
            self.assertEqual(r["exit_time"], want["exit"])

    def test_pullback_without_fill_is_no_trade(self):
        rows = self.run_day(exit_key="pullback", text="Current rule + Buy the pullback")
        for r in rows:
            self.assertEqual(r["actual_outcome"], "no fill")
            self.assertNotIn("pnl_usd", r)
            self.assertIn("no pullback", r["note"])

    def test_everything_passed_over_is_no_setup(self):
        for sym in self.charts:
            self.charts[sym][DAY] = session(30000, late=True)
        rows = self.run_day(skips=["late"], text="Current rule + skip if fresh breakout")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actual_outcome"], "no setup")
        self.assertIn("passed over", rows[0]["note"])


if __name__ == "__main__":
    unittest.main()
