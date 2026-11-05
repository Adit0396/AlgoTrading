"""Cross-market arbitrage tracker: the same company listed in two markets, priced in one currency.

Simulation only. This script never places, stages or drafts an order.

Pairs: five Indian companies with US depositary shares (NSE vs NYSE) and two Australian companies
listed in New York (ASX vs NYSE). For every day both markets traded:

  parity (US$) = local close x shares per US share / FX         (FX = local currency per US$)
  premium      = US close / parity - 1

A premium is normal and can last for years (Indian US shares usually trade above their NSE price,
because shares can't be moved freely between the two). What can be traded is the premium straying
far from its own recent level and then coming back:

  z = (premium - its 60-day average) / its 60-day standard deviation, from data up to that day.
  |z| >= 2: sell the expensive side and buy the cheap one, US$5,000 each, at the next close of each
            market (the two markets never trade at the same time, so the signal waits for both).
  Exit at the next closes once |z| <= 0.5, after 20 trading days, or if the gap widens to 2 more
  standard deviations than at the signal (stop).

A one-day premium jump of more than 20 points is treated as a corporate action (split, bonus,
ratio change), not an opportunity: the 60-day history restarts and any open trade is closed.
Moves of a few points are normal: New York often moves 3-5% after the NSE and ASX have closed,
and the local market catches up the next day.

Costs, per side: 5 bps on US trades, 15 bps on NSE, 10 bps on ASX, 10 bps currency conversion on
the local leg, and 3% a year for carrying the short leg. P&L is in US$ and converted to A$ at each
day's rate. Each pair has a US$5,000 sleeve, so the fund is US$35,000; monthly returns are on that.
The benchmark is an Indian arbitrage mutual fund (cash-futures arbitrage) at 6.55% a year, the
category's 1-year average on 3 Nov 2026 (sharpely.in).

Real-world limits the numbers don't capture: Indian rules restrict moving shares between the NSE
and US listings, NRIs can't short NSE shares (an NSE short means stock futures, in large lots),
shorting needs borrowable stock, and the markets' closes are hours apart.
"""

import argparse
import csv
import datetime
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"

# key, company, region, US symbol, local symbol, local shares per US share, FX symbol, FX is local per US$
PAIRS = [
    ("INFY", "Infosys", "India ↔ US", "INFY", "INFY.NS", 1, "INR=X", True),
    ("WIT", "Wipro", "India ↔ US", "WIT", "WIPRO.NS", 1, "INR=X", True),
    ("HDB", "HDFC Bank", "India ↔ US", "HDB", "HDFCBANK.NS", 3, "INR=X", True),
    ("IBN", "ICICI Bank", "India ↔ US", "IBN", "ICICIBANK.NS", 2, "INR=X", True),
    ("RDY", "Dr. Reddy's Laboratories", "India ↔ US", "RDY", "DRREDDY.NS", 1, "INR=X", True),
    ("BHP", "BHP Group", "Australia ↔ US", "BHP", "BHP.AX", 2, "AUDUSD=X", False),
    ("RMD", "ResMed", "Australia ↔ US", "RMD", "RMD.AX", 10, "AUDUSD=X", False),
]
LOCAL_COST = {".NS": 0.0015, ".AX": 0.0010}
US_COST = 0.0005
FX_COST = 0.0010
SHORT_CARRY = 0.03          # a year, on the short leg
LEG_USD = 5000.0
WINDOW, ENTRY_Z, EXIT_Z, STOP_WIDEN, MAX_DAYS, BREAK = 60, 2.0, 0.5, 2.0, 20, 0.20
BENCH_YEAR = 0.0655         # Indian arbitrage funds, category 1-year average, 3 Nov 2026
LIVE_FROM = "2026-11-05"    # trades entered from this day on are tracked live; earlier ones are backtest


# ---------------------------------------------------------------- data

def fetch_daily(sym, rng="2y", pause=0.5):
    """{date: close} for a Yahoo symbol, dated in the exchange's own time zone."""
    last = None
    for host in ("query2", "query1"):
        for attempt in (0, 1):
            url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d" % (
                host, urllib.parse.quote(sym), rng)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    doc = json.loads(r.read().decode("utf-8", "replace"))
                time.sleep(pause)
                return parse_daily(doc)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                if e.code == 429 and attempt == 0:
                    time.sleep(5)
                    continue
                break
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = str(e)
                break
    raise RuntimeError("Yahoo daily chart for %s failed (%s)" % (sym, last))


def parse_daily(doc):
    res = (doc.get("chart") or {}).get("result") or []
    if not res:
        raise RuntimeError("no data")
    r = res[0]
    off = (r.get("meta") or {}).get("gmtoffset") or 0
    closes = ((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    out = {}
    for ts, c in zip(r.get("timestamp") or [], closes):
        if c is None:
            continue
        d = datetime.datetime.fromtimestamp(ts + off, datetime.timezone.utc).date().isoformat()
        out[d] = float(c)
    return out


# ---------------------------------------------------------------- the strategy

def premium_series(us, local, fx, ratio, fx_local_per_usd):
    """[(date, us_close, local_close, fx, premium)] for days with all three prices (FX carried
    forward from the latest earlier day if that day has none)."""
    out = []
    fx_days = sorted(fx)
    j, last_fx = 0, None
    for d in sorted(set(us) & set(local)):
        while j < len(fx_days) and fx_days[j] <= d:
            last_fx = fx[fx_days[j]]
            j += 1
        if not last_fx:
            continue
        parity = local[d] * ratio / last_fx if fx_local_per_usd else local[d] * ratio * last_fx
        out.append((d, us[d], local[d], last_fx, us[d] / parity - 1))
    return out


def zscores(series):
    """[(date, premium, z or None, break?)]: z from the previous WINDOW premiums (since the last break)."""
    out, hist, prev = [], [], None
    for d, _, _, _, p in series:
        brk = prev is not None and abs(p - prev) > BREAK
        if brk:
            hist = []
        z = None
        if len(hist) >= WINDOW:
            w = hist[-WINDOW:]
            m = sum(w) / len(w)
            sd = math.sqrt(sum((x - m) ** 2 for x in w) / (len(w) - 1))
            z = (p - m) / sd if sd > 0 else None
        out.append((d, p, z, brk))
        hist.append(p)
        prev = p
    return out


def usd_value(local_px, fx, fx_local_per_usd):
    return local_px / fx if fx_local_per_usd else local_px * fx


def simulate(pair, series, zs):
    """Trades for one pair -> list of dicts. Signals at day i act at day i+1's closes."""
    key, name, region, us_sym, loc_sym, ratio, _, inv = pair
    lc = LOCAL_COST[loc_sym[loc_sym.rindex("."):]]
    trades, open_t = [], None
    for i in range(len(series) - 1):
        d, p, z, brk = zs[i]
        nd, us_px, loc_px, fx, _ = series[i + 1]
        if open_t:
            open_t["days"] += 1
            why = None
            if zs[i + 1][3]:
                why = "corporate action"
            elif z is not None and abs(z) <= EXIT_Z:
                why = "back to normal"
            elif z is not None and z * open_t["signal_z"] > 0 and abs(z) >= abs(open_t["signal_z"]) + STOP_WIDEN:
                why = "stop"
            elif open_t["days"] >= MAX_DAYS:
                why = "20 days"
            if why == "corporate action":
                # The jump is a data artefact (split, bonus, ratio change): close at the last clean prices.
                cd, cu, cl, cf, cp = series[i]
                trades.append(close_trade(open_t, cd, cu, cl, cf, inv, lc, why, cp))
                open_t = None
            elif why:
                trades.append(close_trade(open_t, nd, us_px, loc_px, fx, inv, lc, why, series[i + 1][4]))
                open_t = None
            continue
        if z is None or brk or abs(z) < ENTRY_Z or zs[i + 1][3]:
            continue
        side = -1 if z > 0 else 1          # +1: long the US share, short the local one
        us_sh = LEG_USD / us_px
        loc_sh = LEG_USD / usd_value(loc_px, fx, inv)
        open_t = {"pair": key, "name": name, "region": region, "side": side, "signal_date": d, "signal_z": z,
                  "entry_date": nd, "entry_premium": series[i + 1][4], "us_in": us_px, "loc_in": loc_px,
                  "fx_in": fx, "us_sh": us_sh, "loc_sh": loc_sh, "days": 0}
    if open_t:
        nd, us_px, loc_px, fx, prem = series[-1]
        t = close_trade(open_t, nd, us_px, loc_px, fx, inv, lc, "open", prem)
        t["open"] = True
        trades.append(t)
    return trades


def close_trade(t, d, us_px, loc_px, fx, inv, lc, why, prem):
    side = t["side"]
    us_pnl = side * t["us_sh"] * (us_px - t["us_in"])
    loc_pnl = -side * t["loc_sh"] * (usd_value(loc_px, fx, inv) - usd_value(t["loc_in"], t["fx_in"], inv))
    gross = us_pnl + loc_pnl
    days = (datetime.date.fromisoformat(d) - datetime.date.fromisoformat(t["entry_date"])).days
    costs = 2 * LEG_USD * (US_COST + lc + FX_COST) + LEG_USD * SHORT_CARRY * days / 365
    return dict(t, exit_date=d, exit_premium=prem, why=why, gross_usd=round(gross, 2), costs_usd=round(costs, 2),
                pnl_usd=round(gross - costs, 2), open=False,
                trade=("buy US, sell local" if side > 0 else "sell US, buy local"))


# ---------------------------------------------------------------- reporting

def audusd_on(audusd, d):
    days = [x for x in audusd if x <= d]
    return audusd[max(days)] if days else None


def monthly(trades, audusd, months):
    """{month: {pair: A$ P&L}} by the month each trade closed (open trades count in the latest month)."""
    out = {m: {} for m in months}
    for t in trades:
        m = t["exit_date"][:7]
        if m not in out:
            continue
        rate = audusd_on(audusd, t["exit_date"])
        aud = t["pnl_usd"] / rate if rate else 0.0
        out[m][t["pair"]] = out[m].get(t["pair"], 0.0) + aud
    return out


def sheet_rows(trades, latest, mon, months, audusd):
    """(main tab rows, trades tab rows). The main tab keeps fixed rows at the top (pairs, totals)
    and the monthly table last, so it grows downward without moving anything."""
    capital_usd = LEG_USD * len(PAIRS)
    rate = audusd_on(audusd, max(audusd)) if audusd else None
    capital_aud = capital_usd / rate if rate else 0.0
    bench_m = (1 + BENCH_YEAR) ** (1 / 12) - 1
    rows = [["Cross-market arbitrage (simulated): the same company in two markets, priced in US$. Sell the expensive "
             "side and buy the cheap one when the price gap strays far from normal; close when it comes back. "
             "No orders are placed."],
            ["Fund: US$5,000 per pair x %d pairs = US$%s (about A$%s). Entry when the gap is 2+ standard deviations "
             "from its 60-day average; exit near average, after 20 days, or if it widens 2 more (stop). Costs "
             "included. Benchmark: an Indian arbitrage fund at %.2f%% a year on the same money."
             % (len(PAIRS), "{:,.0f}".format(capital_usd), "{:,.0f}".format(capital_aud), BENCH_YEAR * 100)],
            ["Real-world limits: Indian rules restrict moving shares between the NSE and US listings; NRIs can't "
             "short NSE shares (that means stock futures, in large lots); shorting needs borrowable stock; the "
             "markets' closes are hours apart. This measures how often the gaps stray and come back, not money "
             "that could be collected."],
            ["PAIRS TODAY", "Company", "Markets", "US share = local shares", "Latest day", "Premium of US share",
             "60-day average", "Gap now (z)", "Signal", "Position"]]
    for key, name, region, us_sym, loc_sym, ratio, *_ in PAIRS:
        L = latest.get(key) or {}
        rows.append([key + " / " + loc_sym, name, region, ratio, "'" + L.get("date", "") if L else "",
                     L.get("premium", ""), L.get("mean", ""), L.get("z", ""), L.get("signal", "no data"),
                     L.get("position", "")])
    closed = [t for t in trades if not t.get("open")]
    wins = sum(1 for t in closed if t["pnl_usd"] > 0)
    pnl_aud = round(sum(t["pnl_aud"] for t in closed), 2)
    costs_aud = round(sum(t["costs_aud"] for t in closed), 2)
    bench_total = round(bench_m * capital_aud * len(months), 2)
    rows += [[],
             ["TOTALS", "Closed trades", "Winners", "Win rate", "P&L (A$)", "Costs paid (A$)",
              "Benchmark, same %d months (A$)" % len(months), "vs benchmark (A$)", "Open trades now (A$)"],
             ["", len(closed), wins, round(wins / len(closed), 3) if closed else "", pnl_aud, costs_aud, bench_total,
              round(pnl_aud - bench_total, 2), round(sum(t["pnl_aud"] for t in trades if t.get("open")), 2)],
             [],
             ["MONTHLY P&L (A$)", "Month"] + [p[0] for p in PAIRS] +
             ["India ↔ US", "Australia ↔ US", "Total", "Return on fund", "Arbitrage-fund benchmark", "Running total",
              "Benchmark running total"]]
    run_t = run_b = 0.0
    for m in months:
        vals = [round(mon[m].get(p[0], 0.0), 2) for p in PAIRS]
        india = round(sum(v for v, p in zip(vals, PAIRS) if p[2].startswith("India")), 2)
        aus = round(sum(v for v, p in zip(vals, PAIRS) if p[2].startswith("Australia")), 2)
        total = round(india + aus, 2)
        bench = round(bench_m * capital_aud, 2)
        run_t += total
        run_b += bench
        rows.append(["", "'" + m] + vals + [india, aus, total, round(total / capital_aud, 4) if capital_aud else "",
                                            bench, round(run_t, 2), round(run_b, 2)])
    trade_rows = [["Pair", "Company", "Markets", "Trade", "Signal day", "Gap at signal (z)", "Entry day", "Exit day",
                   "Why closed", "Premium in", "Premium out", "P&L before costs (US$)", "Costs (US$)", "P&L (US$)",
                   "P&L (A$)", "Tracking"]]
    for t in sorted(trades, key=lambda t: t["entry_date"], reverse=True):
        trade_rows.append([t["pair"], t["name"], t["region"], t["trade"], "'" + t["signal_date"], round(t["signal_z"], 2),
                           "'" + t["entry_date"], "still open" if t.get("open") else "'" + t["exit_date"], t["why"],
                           round(t["entry_premium"], 4), round(t["exit_premium"], 4), t["gross_usd"], t["costs_usd"],
                           t["pnl_usd"], t["pnl_aud"], "live" if t["entry_date"] >= LIVE_FROM else "backtest"])
    return rows, trade_rows


def run(out_dir, fetch=fetch_daily):
    os.makedirs(out_dir, exist_ok=True)
    cache, failed = {}, []
    syms = sorted({s for p in PAIRS for s in (p[3], p[4], p[6])} | {"AUDUSD=X"})
    for s in syms:
        try:
            cache[s] = fetch(s)
        except Exception as e:  # noqa: BLE001
            failed.append("%s: %s" % (s, e))
    if failed:
        print("failed: " + "; ".join(failed), file=sys.stderr)
    audusd = cache.get("AUDUSD=X") or {}
    trades, latest, report = [], {}, []
    for pair in PAIRS:
        key, name, region, us_sym, loc_sym, ratio, fx_sym, inv = pair
        if not all(s in cache for s in (us_sym, loc_sym, fx_sym)):
            report.append("%s: missing data" % key)
            continue
        series = premium_series(cache[us_sym], cache[loc_sym], cache[fx_sym], ratio, inv)
        if len(series) < WINDOW + 5:
            report.append("%s: only %d days" % (key, len(series)))
            continue
        med = sorted(x[4] for x in series)[len(series) // 2]
        if abs(med) > 0.25:
            # A typical premium this far off means the share ratio is wrong for this data: skip.
            report.append("%s: typical premium %.0f%% - check the share ratio; skipped" % (key, med * 100))
            continue
        zs = zscores(series)
        ts = simulate(pair, series, zs)
        trades += ts
        d, p, z, brk = zs[-1]
        hist = [x[1] for x in zs[-WINDOW - 1:-1]]
        pos = next((t for t in ts if t.get("open")), None)
        latest[key] = {"date": d, "premium": round(p, 4), "mean": round(sum(hist) / len(hist), 4),
                       "z": "" if z is None else round(z, 2),
                       "signal": "" if z is None else ("US share expensive: sell US, buy local" if z >= ENTRY_Z else
                                                       "US share cheap: buy US, sell local" if z <= -ENTRY_Z else "none"),
                       "position": ("%s since %s (%+.2f US$ so far)" % (pos["trade"], pos["entry_date"], pos["pnl_usd"])
                                    if pos else "none")}
        report.append("%s: %d days %s to %s, typical premium %+.1f%%, %d trades" % (
            key, len(series), series[0][0], series[-1][0], med * 100, len(ts)))
    first = min((t["entry_date"] for t in trades), default=None)
    months = []
    if first:
        m = first[:7]
        end = max(max(t["exit_date"] for t in trades), max(audusd) if audusd else first)[:7]
        while m <= end:
            months.append(m)
            y, mo = int(m[:4]), int(m[5:])
            m = "%04d-%02d" % (y + (mo == 12), 1 if mo == 12 else mo + 1)
    for t in trades:
        rate = audusd_on(audusd, t["exit_date"])
        t["pnl_aud"] = round(t["pnl_usd"] / rate, 2) if rate else 0.0
        t["costs_aud"] = round(t["costs_usd"] / rate, 2) if rate else 0.0
    mon = monthly(trades, audusd, months)
    with open(os.path.join(out_dir, "trades.csv"), "w", newline="") as fh:
        cols = ["pair", "name", "region", "trade", "signal_date", "signal_z", "entry_date", "exit_date", "why",
                "entry_premium", "exit_premium", "gross_usd", "costs_usd", "pnl_usd", "pnl_aud", "open"]
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for t in sorted(trades, key=lambda t: t["entry_date"]):
            w.writerow(t)
    main_rows, trade_rows = sheet_rows(trades, latest, mon, months, audusd)
    with open(os.path.join(out_dir, "sheet_arbitrage.json"), "w") as fh:
        json.dump(main_rows, fh, ensure_ascii=False)
    with open(os.path.join(out_dir, "sheet_arbitrage_trades.json"), "w") as fh:
        json.dump(trade_rows, fh, ensure_ascii=False)
    summary = {"generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
               "pairs": report, "latest": latest, "failed": failed,
               "monthly_aud": {m: round(sum(mon[m].values()), 2) for m in months}}
    with open(os.path.join(out_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1, ensure_ascii=False)
    for line in report:
        print(line)
    return 0 if len(latest) >= len(PAIRS) - 2 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "arbitrage-data"))
    args = ap.parse_args(argv)
    return run(args.out)


if __name__ == "__main__":
    sys.exit(main())
