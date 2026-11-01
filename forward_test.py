#!/usr/bin/env python3
"""Forward test for the day-trader skill pack. Simulation only: it never places an order.

For one US trading day it:
  1. runs trader-scanner's opening-range breakout scan as of 10:15 AM New York time,
  2. keeps the TOP most-traded breakouts (dollar volume 9:30 to 10:15),
  3. plans each one the pack's default way (entry = breakout close, stop = range midpoint,
     target = 1R, flat by 3:55 PM),
  4. runs trader-timemachine on it (10,000 replays from the stock's prior sessions, seed 19),
  5. checks what really happened that day after the entry (stop first, then target, bar by bar,
     else the price at 3:55 PM, the same rule the time machine uses),
  6. sizes it to the account and max loss and works out the P&L you would have had.

Rows go to results.csv (one per pick, or one "no setup" row for a day with no breakout).
Re-running a date replaces that date's rows.

Usage:
  python3 forward_test.py --repo PATH_TO_day-trader-skill --date 2026-10-30
  python3 forward_test.py --repo ... --backfill 5        # the last 5 completed sessions
  python3 forward_test.py --repo ... --last              # the most recent completed session
"""
import argparse
import csv
import datetime
import importlib.util
import json
import math
import os
import subprocess
import sys
import time


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

FIELDS = ["date", "symbol", "entry_time", "entry", "stop", "target", "risk_per_share", "shares",
          "position_usd", "max_loss_usd", "tm_target_pct", "tm_stop_pct", "tm_neither_pct", "tm_green_pct",
          "tm_avg_r", "hist_fired", "hist_target", "hist_stop", "hist_close", "hist_avg_r", "verdict",
          "actual_outcome", "exit_time", "exit_price", "r_multiple", "pnl_usd", "dollar_volume_to_1015",
          "scan_hits", "seed", "run_at", "note"]

US_HOLIDAYS = {  # NYSE full closures, 2026-2027
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
    "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def is_session(d):
    return d.weekday() < 5 and d.isoformat() not in US_HOLIDAYS


def completed_sessions(tm, n):
    """The last n trading days whose regular session has finished (New York time)."""
    now_et = tm.to_eastern(utcnow())
    d = now_et.date()
    if now_et.hour * 60 + now_et.minute < 16 * 60 + 15 or not is_session(d):
        d -= datetime.timedelta(days=1)
    out = []
    while len(out) < n:
        if is_session(d):
            out.append(d)
        d -= datetime.timedelta(days=1)
    return sorted(out)


def run_scan(repo, workdir, day, args):
    universe = os.path.join(args.state, "universe.txt")
    scan = os.path.join(repo, "trader-scanner", "scan.py")
    if args.scan_offline:
        cmd = [sys.executable, "-I", scan, "orb", "--offline", args.scan_offline]
    else:
        stale = (not os.path.exists(universe)) or time.time() - os.path.getmtime(universe) > 7 * 86400
        if stale:
            subprocess.run([sys.executable, "-I", scan, "universe", "--out", universe], cwd=workdir,
                           check=True, capture_output=True, text=True)
        cmd = [sys.executable, "-I", scan, "orb", "--universe", universe]
    cmd += ["--now", "%s 10:15" % day.isoformat(), "--json", "--no-write", "--quiet",
            "--min-price", str(args.min_price), "--min-volume", str(args.min_volume)]
    p = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
    out = p.stdout.strip()
    if not out.startswith("{"):
        raise RuntimeError("scanner: " + (p.stderr.strip() or out or "no output")[-400:])
    return json.loads(out)


def chart_text(tm, sym, args):
    if args.chart_offline:
        return tm.read_offline(args.chart_offline, sym)
    text = tm.fetch_chart(sym)
    time.sleep(0.3)
    return text


def day_bars(tm, text, sym, day):
    result = tm.chart_result(text, sym)
    _, bars, _ = tm.parse_bars(result, utcnow())
    return tm.split_sessions(bars).get(day.isoformat(), {})


def bar_close(b):
    """The time a 5-minute bar closes, e.g. the 09:55 bar -> 10:00."""
    h, m = map(int, b["start"].split(":"))
    m += 5
    return "%02d:%02d" % (h + m // 60, m % 60)


def actual_outcome(D, k, last, entry, stop, target):
    """Same rule as the time machine's history: stop checked before target in each bar."""
    for i in range(k + 1, last + 1):
        b = D.get(i)
        if b is None:
            return None, None, None, "missing the %s bar" % i
        if b["l"] <= stop:
            return "stop", bar_close(b), stop, ""
        if b["h"] >= target:
            return "target", bar_close(b), target, ""
    b = D.get(last)
    return "flat at 3:55", "15:55", round(b["c"], 2), ""


def pick_day(tm, repo, day, args):
    run_at = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    workdir = os.path.join(args.state, "work")
    os.makedirs(workdir, exist_ok=True)
    doc = run_scan(repo, workdir, day, args)
    hits = doc.get("hits") or []
    base = {"date": day.isoformat(), "scan_hits": len(hits), "seed": args.seed, "run_at": run_at}
    if not hits:
        return [dict(base, symbol="", actual_outcome="no setup", note="no opening-range breakout by 10:15 AM")]

    n = args.or_minutes // 5
    last = (15 * 60 + 55 - tm.OPEN_MIN) // 5 - 1          # the bar that closes at 3:55 PM
    cands = []
    for h in (hits[:args.max_charts] if args.max_charts else hits):
        try:
            text = chart_text(tm, h["symbol"], args)
            D = day_bars(tm, text, h["symbol"], day)
        except Exception as e:  # noqa: BLE001 - one bad chart should not stop the day
            cands.append((0, h, None, None, "chart: %s" % e))
            continue
        dv = sum(D[i]["c"] * D[i]["v"] for i in range(h["bar_index"] + 1) if i in D)
        cands.append((dv, h, text, D, ""))
    cands.sort(key=lambda x: (bool(x[4]), -x[0]))  # charts that loaded first, most traded first

    rows = []
    for dv, h, text, D, err in cands[:args.top]:
        sym = h["symbol"]
        row = dict(base, symbol=sym, dollar_volume_to_1015=round(dv))
        if err:
            rows.append(dict(row, note=err))
            continue
        k = h["bar_index"]
        oh, ol = h["or_high"], h["or_low"]
        entry = h["price"]
        stop = tm.cents_away((oh + ol) / 2, True)
        risk = round(entry - stop, 2)
        if risk <= 0:
            rows.append(dict(row, note="stop not below entry"))
            continue
        target = round(entry + args.target_r * risk, 2)
        shares = min(int(math.floor(args.max_loss / risk + 1e-9)), int(math.floor(args.account / entry)))
        row.update(entry_time=h["at"], entry=entry, stop=stop, target=target, risk_per_share=risk,
                   shares=shares, position_usd=round(shares * entry, 2), max_loss_usd=round(shares * risk, 2))
        try:
            plan = tm.normalize_plan({"symbol": sym, "date": day.isoformat(), "entry": entry, "stop": stop,
                                      "target": target, "entry_bar": k, "setup": "orb", "stop_rule": "or_mid",
                                      "opening_range": {"minutes": args.or_minutes}, "side": "buy"})
            res = tm.time_machine(text, plan, utcnow(), replays=args.replays, seed=args.seed,
                                  until="15:55")
            mc, hist = res["mc"], res["history"]
            row.update(tm_target_pct=round(100 * mc["p_target"], 1), tm_stop_pct=round(100 * mc["p_stop"], 1),
                       tm_neither_pct=round(100 * mc["p_neither"], 1), tm_green_pct=round(100 * mc["p_green"], 1),
                       tm_avg_r=mc["avg_R"], hist_fired=hist.get("triggered"), hist_target=hist.get("target"),
                       hist_stop=hist.get("stop"), hist_close=hist.get("close"), hist_avg_r=hist.get("avg_R"),
                       verdict=res["verdict"])
        except Exception as e:  # noqa: BLE001
            row["note"] = "time machine: %s" % e
        if shares < 1:
            row["note"] = (row.get("note", "") + "; " if row.get("note") else "") + \
                "0 shares: one share risks more than the max loss"
        oc, t, px, why = actual_outcome(D, k, last, entry, stop, target)
        if oc is None:
            row["note"] = (row.get("note", "") + "; " if row.get("note") else "") + why
        else:
            r = round((px - entry) / risk, 2)
            row.update(actual_outcome=oc, exit_time=t, exit_price=px, r_multiple=r,
                       pnl_usd=round(shares * (px - entry), 2))
        rows.append(row)
    return rows


TEXT_FIELDS = ("date", "symbol", "entry_time", "exit_time", "run_at", "verdict", "note", "actual_outcome")


def sheet_row(r):
    """One row for the Google Sheet, in FIELDS order. Text that looks like a date or time gets a
    leading apostrophe so Sheets keeps it as text."""
    out = []
    for k in FIELDS:
        v = r.get(k, "")
        if v is None:
            v = ""
        if k in TEXT_FIELDS and v != "":
            v = "'" + str(v)
        out.append(v)
    return out


def save(rows, path):
    old = []
    if os.path.exists(path):
        with open(path, newline="") as fh:
            old = list(csv.DictReader(fh))
    dates = {r["date"] for r in rows}
    keep = [r for r in old if r.get("date") not in dates]
    allrows = sorted(keep + rows, key=lambda r: (r["date"], r.get("symbol") or ""))
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in allrows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True, help="folder holding the trader-* skill folders")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--date", help="one session, YYYY-MM-DD (New York)")
    g.add_argument("--backfill", type=int, help="the last N completed sessions (Yahoo keeps about 5)")
    g.add_argument("--last", action="store_true", help="the most recent completed session")
    g.add_argument("--catch-up", action="store_true",
                   help="every one of the last 5 completed sessions not yet in results.csv")
    ap.add_argument("--state", default="forward-test-data", help="where results.csv and the universe live")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--account", type=float, default=5000.0)
    ap.add_argument("--max-loss", type=float, default=50.0)
    ap.add_argument("--target-r", type=float, default=1.0)
    ap.add_argument("--or-minutes", type=int, default=15)
    ap.add_argument("--min-price", type=float, default=5.0)
    ap.add_argument("--min-volume", type=int, default=200000, help="shares traded 9:30 to 10:15")
    ap.add_argument("--max-charts", type=int, default=0,
                    help="rank only the first N breakouts by dollar volume (0 = all of them)")
    ap.add_argument("--replays", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=19)
    ap.add_argument("--scan-offline", help=argparse.SUPPRESS)
    ap.add_argument("--chart-offline", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    repo = os.path.abspath(args.repo)
    args.state = os.path.abspath(args.state)
    os.makedirs(args.state, exist_ok=True)
    tm = load_module(os.path.join(repo, "trader-timemachine", "timemachine.py"), "timemachine")

    out = os.path.join(args.state, "results.csv")
    if args.date:
        days = [datetime.date.fromisoformat(args.date)]
    elif args.catch_up:
        done = set()
        if os.path.exists(out):
            with open(out, newline="") as fh:
                done = {r.get("date") for r in csv.DictReader(fh)}
        days = [d for d in completed_sessions(tm, 5) if d.isoformat() not in done]
        if not days:
            print("Nothing to catch up: the last 5 sessions are already in %s" % out)
    else:
        days = completed_sessions(tm, args.backfill or 1)

    status = 0
    summary = []
    for d in days:
        try:
            rows = pick_day(tm, repo, d, args)
        except Exception as e:  # noqa: BLE001
            print("%s: FAILED: %s" % (d, e), file=sys.stderr)
            status = 1
            continue
        save(rows, out)
        pnl = sum(float(r["pnl_usd"]) for r in rows if r.get("pnl_usd") not in (None, ""))
        summary.append({"date": d.isoformat(), "picks": [r.get("symbol") for r in rows if r.get("symbol")],
                        "pnl_usd": round(pnl, 2), "rows": rows,
                        "sheet_rows": [sheet_row(r) for r in rows]})
        print("%s: %s -> P&L $%.2f" % (d, ", ".join(
            "%s %s" % (r["symbol"], r.get("actual_outcome", "?")) for r in rows if r.get("symbol")) or "no setup",
            pnl))
    with open(os.path.join(args.state, "last_run.json"), "w") as fh:
        json.dump({"fields": FIELDS, "days": summary}, fh, indent=2)
    # every logged row, in the Google Sheet's column order, so a sync can add whatever is missing
    allrows = []
    if os.path.exists(out):
        with open(out, newline="") as fh:
            for r in csv.DictReader(fh):
                for k in FIELDS:
                    v = r.get(k, "")
                    if k not in TEXT_FIELDS and v not in ("", None):
                        try:
                            f = float(v)
                            r[k] = int(f) if f.is_integer() and k in ("shares", "hist_fired", "hist_target",
                                                                       "hist_stop", "hist_close", "scan_hits",
                                                                       "seed") else f
                        except ValueError:
                            pass
                allrows.append(sheet_row(r))
    with open(os.path.join(args.state, "sheet_rows.json"), "w") as fh:
        json.dump({"fields": FIELDS, "rows": allrows}, fh, indent=1)
    print("Results: %s" % out)
    return status


if __name__ == "__main__":
    sys.exit(main())
