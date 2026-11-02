#!/usr/bin/env python3
"""Forward test of the opening-range breakout on India's NSE. Simulation only: it never places an order.

The same method as forward_test.py (the US test), moved to NSE hours (9:15 to 15:30 India time):

  1. Universe: the Nifty 500 (NSE's own constituents list).
  2. At 10:00 AM India time (45 minutes after the open, like 10:15 in New York), keep the stocks
     whose 5-minute close broke above their opening range (the high of 9:15 to 9:30), with a price
     of at least Rs 50 and at least 100,000 shares traded, and still above that high at 10:00.
  3. Take the 3 with the most rupee turnover from 9:15 to 10:00.
  4. Buy at the 10:00 price. Stop at the opening-range midpoint, target 1R, flat by 15:15.
  5. The day-trader pack's Time Machine (its own simulate and history_replay code, with the NSE
     session in place of New York's) replays the rest of the day 10,000 times, seed 19.
  6. The real 5-minute bars after 10:00 decide what happened. Sized to an account and a max loss.

All of it comes from one Yahoo chart request per stock (60 days of 5-minute bars), so this test
can also backfill several weeks at once. Results go to forward-test-data-india/.

Usage:
  python3 forward_test_india.py --repo day-trader-skill --catch-up      # missing sessions (last 15)
  python3 forward_test_india.py --repo day-trader-skill --backfill 15
  python3 forward_test_india.py --repo day-trader-skill --date 2026-10-30
"""
import argparse
import concurrent.futures
import contextlib
import csv
import datetime
import importlib.util
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OPEN_MIN = 9 * 60 + 15
CLOSE_MIN = 15 * 60 + 30
SESSION_BARS = 75                 # 9:15 to 15:30 in 5-minute bars
IST = datetime.timedelta(hours=5, minutes=30)
SCAN_LABEL = "10:00 AM"           # when every pick is decided and bought (India time)
SCAN_BAR = 8                      # the bar 9:55 to 10:00 (9:15 is bar 0)
FLAT_BY = "15:15"
RULE = "v2"
UNIVERSE_URLS = (
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
)
# The skill pack's own User-Agent: it names the tool honestly, and Yahoo answers it.
UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
SETTINGS = {
    "market": "IN", "market_label": "India (NSE)", "currency": "INR", "timezone": "India",
    "universe": "the Nifty 500 (NSE's constituents list)",
    "opening_range": "9:15 to 9:30", "scan_time": "10:00 India", "flat_by": "15:15 India",
    "ranked_by": "rupee turnover 9:15 to 10:00",
    "eligible": "broke out above the range by 10:00 and still above it at 10:00",
    "entry": "the 10:00 price (close of the 9:55 bar)", "stop": "opening-range midpoint",
}


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ft = load_module(os.path.join(HERE, "forward_test.py"), "forward_test")


def nse_timemachine(repo):
    """A private copy of the pack's timemachine.py with NSE hours in place of New York's."""
    tm = load_module(os.path.join(repo, "trader-timemachine", "timemachine.py"), "timemachine_nse")
    tm.OPEN_MIN, tm.CLOSE_MIN, tm.SESSION_BARS = OPEN_MIN, CLOSE_MIN, SESSION_BARS
    tm.NYSE_EARLY_CLOSE = {}
    tm.eastern_offset = lambda utc: 5.5     # India has no daylight saving
    return tm


def nse_news(repo):
    """The pack's news.py, keeping '.NS' in Yahoo symbols (it turns '.' into '-' for US share classes)."""
    news = load_module(os.path.join(repo, "trader-news", "news.py"), "news_nse")
    news.yahoo_symbol = lambda t: t.upper()
    return news


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def load_universe(state, offline=None):
    """[(symbol, company name)] for the Nifty 500, cached in state/universe.csv."""
    cache = os.path.join(state, "universe.csv")
    text = None
    if offline:
        with open(offline, encoding="utf-8") as fh:
            text = fh.read()
    else:
        fresh = os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 7 * 86400
        if not fresh:
            for url in UNIVERSE_URLS:
                try:
                    text = http_get(url, timeout=15)
                    if "Symbol" in text.splitlines()[0]:
                        break
                except (urllib.error.URLError, OSError, IndexError) as e:
                    print("universe: %s: %s" % (url, e), file=sys.stderr)
                text = None
        if text:
            with open(cache, "w", encoding="utf-8") as fh:
                fh.write(text)
        elif os.path.exists(cache):
            with open(cache, encoding="utf-8") as fh:
                text = fh.read()
    if not text:
        raise RuntimeError("could not get the Nifty 500 list from NSE and there is no cached copy")
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        sym = (r.get("Symbol") or "").strip().upper()
        if sym and (r.get("Series") or "EQ").strip() == "EQ":
            out.append((sym, (r.get("Company Name") or "").strip()))
    return out


def fetch_chart(tm, sym, offline=None):
    """60 days of 5-minute bars for SYM.NS from Yahoo, one retry after a 429, then the other host."""
    if offline:
        return tm.read_offline(offline, sym)
    last = None
    for host in ("query2", "query1"):
        for attempt in (0, 1):
            url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=60d&interval=5m" % (
                host, urllib.parse.quote(sym + ".NS"))
            try:
                return tm._get(url, timeout=15)  # the pack's request code, same as its Time Machine
            except urllib.error.HTTPError as e:
                last = e.code
                if e.code == 404:
                    raise RuntimeError("Yahoo has no chart for %s.NS" % sym)
                if e.code == 429 and attempt == 0:
                    time.sleep(tm.BACKOFF)
                    continue
                break
            except (urllib.error.URLError, OSError) as e:
                last = str(e)
                break
    raise RuntimeError("Yahoo chart for %s.NS failed (%s)" % (sym, last))


def to_utc(day, label):
    t = datetime.datetime.strptime(label, "%I:%M %p").time()
    return datetime.datetime.combine(day, t) - IST


def news_before(news, sym, when_utc, hours, state):
    """Yahoo headlines for SYM.NS in the hours before the entry: (summary, items)."""
    argv = ["--tickers", sym + ".NS", "--now", when_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "--hours", str(hours),
            "--all", "--dry-run", "--no-moves", "--no-sec", "--json", "--state", os.path.join(state, "news-state.json")]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        try:
            news.main(argv)
        except SystemExit:
            pass
    try:
        doc = json.loads(buf.getvalue())
    except ValueError:
        return "news check failed", []
    items = [{"time": i.get("time"), "source": i.get("source"), "kind": i.get("kind"), "title": i.get("title"),
              "link": i.get("link"), "tags": i.get("tags") or [], "important": bool(i.get("important"))}
             for i in doc.get("items") or []]
    if not items:
        notes = [n for n in doc.get("notes") or [] if "skipped" not in n]
        return ("news check failed: %s" % notes[0][:120]) if notes else \
            "no headlines in the %d hours before entry" % hours, []
    tags = sorted({t for i in items for t in i["tags"]})
    return "%d item%s%s; latest: %s" % (len(items), "" if len(items) == 1 else "s",
                                        (" (" + ", ".join(tags) + ")") if tags else "",
                                        (items[0]["title"] or "")[:140]), items[:8]


def session_days(charts, tm):
    """Dates where the reference stocks have a full 75-bar session, oldest first."""
    counts = {}
    for days in charts.values():
        for d, bars in days.items():
            if len(bars) == SESSION_BARS:
                counts[d] = counts.get(d, 0) + 1
    need = max(1, len(charts) // 4)
    return sorted(d for d, c in counts.items() if c >= need)


def pick_day(tm, news, day, charts, texts, names, args):
    """The picks for one session, using only what was known at 10:00."""
    iso = day.isoformat()
    k, n = SCAN_BAR, args.or_minutes // 5
    last = (15 * 60 + 15 - OPEN_MIN) // 5 - 1            # the bar that closes at 15:15
    run_at = ft.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    broke, cands = 0, []
    for sym, days in charts.items():
        D = days.get(iso, {})
        if any(i not in D for i in range(k + 1)):
            continue
        oh = max(D[i]["h"] for i in range(n))
        ol = min(D[i]["l"] for i in range(n))
        first = next((i for i in range(n, k + 1) if D[i]["c"] > oh), None)
        if first is None:
            continue
        shares_traded = sum(D[i]["v"] for i in range(k + 1))
        if D[k]["c"] < args.min_price or shares_traded < args.min_volume:
            continue
        broke += 1
        if D[k]["c"] <= oh:
            continue                                       # back inside its range by 10:00
        turnover = sum(D[i]["c"] * D[i]["v"] for i in range(k + 1))
        cands.append((turnover, sym, D, oh, ol, first))
    cands.sort(key=lambda x: -x[0])
    base = {"date": iso, "scan_hits": broke, "candidates": len(cands), "seed": args.seed, "run_at": run_at,
            "rule": RULE}
    if not cands:
        return [dict(base, symbol="", actual_outcome="no setup", note="no breakout still above its range at 10:00")]

    rows = []
    for rank, (turnover, sym, D, oh, ol, first) in enumerate(cands[:args.top], 1):
        entry = round(D[k]["c"], 2)
        stop = tm.cents_away((oh + ol) / 2, True)
        risk = round(entry - stop, 2)
        target = round(entry + args.target_r * risk, 2)
        shares = min(int(math.floor(args.max_loss / risk + 1e-9)), int(math.floor(args.account / entry)))
        row = dict(base, symbol=sym, name=names.get(sym, ""), rank=rank, dollar_volume_to_1015=round(turnover),
                   breakout_time=tm.clock12(OPEN_MIN + 5 * (first + 1)), breakout_price=round(D[first]["c"], 2),
                   or_high=round(oh, 2), or_low=round(ol, 2), breakout_pct=round((entry / oh - 1) * 100, 2),
                   entry_time=SCAN_LABEL, entry=entry, stop=stop, target=target, risk_per_share=risk,
                   shares=shares, position_usd=round(shares * entry, 2), max_loss_usd=round(shares * risk, 2))
        # Time Machine: the pack's own replay and history code on this stock's earlier NSE sessions
        days = charts[sym]
        sessions, _ = tm.full_sessions(days, iso)
        if len(sessions) < tm.MIN_SESSIONS:
            row["note"] = "time machine: only %d full sessions before %s" % (len(sessions), iso)
        else:
            pool = tm.build_pool(sessions)
            mc = tm.simulate(pool, entry, stop, target, k, last, args.replays, args.seed, 2, "buy")
            mc["until"] = FLAT_BY
            hist = tm.history_replay(sessions, {"setup": "orb", "or_minutes": args.or_minutes, "stop_rule": "or_mid",
                                                "target_r": args.target_r}, last, "buy")
            row.update(tm_target_pct=round(100 * mc["p_target"], 1), tm_stop_pct=round(100 * mc["p_stop"], 1),
                       tm_neither_pct=round(100 * mc["p_neither"], 1), tm_green_pct=round(100 * mc["p_green"], 1),
                       tm_avg_r=mc["avg_R"], hist_fired=hist["triggered"], hist_target=hist["target"],
                       hist_stop=hist["stop"], hist_close=hist["close"], hist_avg_r=hist["avg_R"],
                       verdict=tm.verdict(mc))
        if news is not None:
            row["news_before_entry"], row["_news"] = news_before(news, sym, to_utc(day, SCAN_LABEL),
                                                                 args.news_hours, args.state)
        if shares < 1:
            row["note"] = (row.get("note", "") + "; " if row.get("note") else "") + \
                "0 shares: one share risks more than the max loss"
        oc, t, px, why = ft.actual_outcome(D, k, last, entry, stop, target)
        if oc is None:
            row["note"] = (row.get("note", "") + "; " if row.get("note") else "") + why
        else:
            row.update(actual_outcome="flat at 3:15" if oc == "flat at 3:55" else oc, exit_time=t, exit_price=px,
                       r_multiple=round((px - entry) / risk, 2), pnl_usd=round(shares * (px - entry), 2))
        rows.append(row)
    return rows


def diagnose(tm, charts, syms, now, offline=None):
    """Print how Yahoo's NSE bars line up, to see why sessions are or aren't 75 bars."""
    for sym in syms:
        days = charts.get(sym)
        if not days:
            print(sym, "no chart")
            continue
        print("==", sym)
        for d in sorted(days)[-12:]:
            slots = sorted(days[d])
            missing = [i for i in range(SESSION_BARS) if i not in days[d]]
            print("  %s: %d bars, first %s last %s, missing slots %s" % (
                d, len(slots), days[d][slots[0]]["start"], days[d][slots[-1]]["start"], missing[:12]))
    text = fetch_chart(tm, syms[0], offline)
    res = tm.chart_result(text, syms[0] + ".NS")
    meta, bars, dropped = tm.parse_bars(res, now)
    stamps = res.get("timestamp") or []
    print("raw: %d stamps, kept %d, dropped %s, meta tz %s gmtoffset %s" % (
        len(stamps), len(bars), dropped, meta.get("exchangeTimezoneName"), meta.get("gmtoffset")))
    tail = [datetime.datetime.utcfromtimestamp(t).strftime("%m-%d %H:%M:%S") for t in stamps[-80:-70]]
    print("raw stamps near a day's end (UTC):", tail)
    for d in ("2026-10-23", "2026-10-22"):
        full = sum(1 for days in charts.values() if all(i in days.get(d, {}) for i in range(SCAN_BAR + 1)))
        vols = sorted(sum(days[d][i]["v"] for i in range(SCAN_BAR + 1)) for days in charts.values()
                      if all(i in days.get(d, {}) for i in range(SCAN_BAR + 1)))
        print("%s: %d stocks with bars to 10:00; median shares to 10:00 %s" % (d, full, vols[len(vols) // 2] if vols else None))
    return 0


FIELDS = ft.FIELDS + ["name"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True, help="folder holding the trader-* skill folders")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--date", help="one session, YYYY-MM-DD (India)")
    g.add_argument("--backfill", type=int, help="redo the last N complete sessions")
    g.add_argument("--catch-up", action="store_true", help="the last 15 complete sessions not logged yet")
    ap.add_argument("--state", default="forward-test-data-india")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--account", type=float, default=500000.0, help="rupees")
    ap.add_argument("--max-loss", type=float, default=5000.0, help="rupees per trade")
    ap.add_argument("--target-r", type=float, default=1.0)
    ap.add_argument("--or-minutes", type=int, default=15)
    ap.add_argument("--min-price", type=float, default=50.0, help="rupees")
    ap.add_argument("--min-volume", type=int, default=100000, help="shares traded 9:15 to 10:00")
    ap.add_argument("--replays", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=19)
    ap.add_argument("--news-hours", type=int, default=18)
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--pause", type=float, default=0.3, help="seconds each worker waits between Yahoo requests")
    ap.add_argument("--workers", type=int, default=3, help="charts downloaded at once")
    ap.add_argument("--chart-minutes", type=float, default=35, help="stop downloading charts after this long")
    ap.add_argument("--diagnose", help=argparse.SUPPRESS)
    ap.add_argument("--universe-offline", help=argparse.SUPPRESS)
    ap.add_argument("--chart-offline", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    repo = os.path.abspath(args.repo)
    args.state = os.path.abspath(args.state)
    os.makedirs(args.state, exist_ok=True)
    tm = nse_timemachine(repo)
    news = None if args.no_news else nse_news(repo)

    universe = load_universe(args.state, args.universe_offline)
    names = {s: n for s, n in universe}
    now = ft.utcnow()
    charts, texts, failed = {}, {}, []
    t0 = time.time()
    print("universe: %d stocks" % len(universe), flush=True)

    deadline = t0 + args.chart_minutes * 60

    def one(sym):
        if time.time() > deadline:
            raise RuntimeError("skipped: chart time limit reached")
        text = fetch_chart(tm, sym, args.chart_offline)
        if not args.chart_offline and args.pause:
            time.sleep(args.pause)
        return text

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(one, sym): sym for sym, _ in universe}
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            sym = futures[fut]
            try:
                _, bars, _ = tm.parse_bars(tm.chart_result(fut.result(), sym + ".NS"), now)
                charts[sym] = tm.split_sessions(bars)
            except Exception as e:  # noqa: BLE001 - one bad chart should not stop the run
                failed.append("%s: %s" % (sym, str(e)[:80]))
            if i % 50 == 0:
                print("  %d/%d charts, %d failed, %.0fs" % (i, len(universe), len(failed), time.time() - t0), flush=True)
    print("charts: %d of %d loaded in %.0fs" % (len(charts), len(universe), time.time() - t0), flush=True)
    if failed:
        print("  failed: %s%s" % ("; ".join(failed[:5]), " ..." if len(failed) > 5 else ""), file=sys.stderr)
    if len(charts) < len(universe) * 0.9:
        # Ranking a partial list would pick from the wrong set of stocks, so write nothing.
        print("Only %d of %d charts loaded; not writing results. First failures: %s"
              % (len(charts), len(universe), "; ".join(failed[:8])), file=sys.stderr, flush=True)
        return 1

    if args.diagnose:
        return diagnose(tm, charts, args.diagnose.split(","), now, args.chart_offline)
    complete = session_days(charts, tm)
    out = os.path.join(args.state, "results.csv")
    done = set()
    if os.path.exists(out):
        with open(out, newline="") as fh:
            done = {r.get("date") for r in csv.DictReader(fh)}
    if args.date:
        days = [datetime.date.fromisoformat(args.date)]
    elif args.backfill:
        days = [datetime.date.fromisoformat(d) for d in complete[-args.backfill:]]
    else:
        days = [datetime.date.fromisoformat(d) for d in complete[-15:] if d not in done]
        if not days:
            print("Nothing to catch up.")

    summary, news_store = [], {}
    news_path = os.path.join(args.state, "news.json")
    if os.path.exists(news_path):
        with open(news_path) as fh:
            news_store = json.load(fh)
    for d in days:
        t1 = time.time()
        rows = pick_day(tm, news, d, charts, texts, names, args)
        save(rows, out)
        for r in rows:
            if r.get("symbol") and "_news" in r:
                news_store.setdefault(d.isoformat(), {})[r["symbol"]] = r["_news"]
        pnl = sum(r["pnl_usd"] for r in rows if r.get("pnl_usd") is not None)
        summary.append({"date": d.isoformat(), "picks": [r.get("symbol") for r in rows if r.get("symbol")],
                        "pnl_inr": round(pnl, 2)})
        print("%s: %s -> P&L Rs %.2f (%.0fs)" % (d, ", ".join("%s %s" % (r["symbol"], r.get("actual_outcome", "?"))
                                                              for r in rows if r.get("symbol")) or "no setup", pnl,
                                                 time.time() - t1), flush=True)
    with open(news_path, "w") as fh:
        json.dump(news_store, fh, indent=1, sort_keys=True)
    with open(os.path.join(args.state, "last_run.json"), "w") as fh:
        json.dump({"days": summary, "charts_loaded": len(charts), "charts_failed": len(failed)}, fh, indent=2)
    ft.write_dashboard(out, news_store, os.path.join(args.state, "dashboard.json"), args, settings=SETTINGS,
                       names=names)
    print("Results: %s" % out)
    return 0


def save(rows, path):
    old = []
    if os.path.exists(path):
        with open(path, newline="") as fh:
            old = list(csv.DictReader(fh))
    dates = {r["date"] for r in rows}
    allrows = sorted([r for r in old if r.get("date") not in dates] + rows,
                     key=lambda r: (r["date"], r.get("symbol") or ""))
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in allrows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


if __name__ == "__main__":
    sys.exit(main())
