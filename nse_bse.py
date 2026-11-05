"""NSE vs BSE price gaps: the same share on India's two exchanges, minute by minute.

Simulation only. This script never places, stages or drafts an order.

Every Nifty 500 share trades on both the NSE and the BSE, in rupees, at the same hours, held in the
same demat account. If it is dearer on one exchange than the other at the same moment, someone who
already holds the share can sell it on the dearer exchange and buy the same number back on the
cheaper one: the holding is unchanged and the difference is kept, minus costs.

For each of the last trading days (Yahoo keeps about a week of 1-minute bars), for every minute in
which the share traded on both exchanges:

    gap = (higher price / lower price) - 1

A minute counts as an opportunity when the gap beats the round-trip cost. Consecutive opportunity
minutes in one share are one episode, traded once, at the first minute's prices. The trade size is
the smaller of Rs 1,00,000 and the shares traded on each exchange in that minute (a thin BSE book
can't absorb more), so the profit is a rough upper bound: the last trade of a minute is not a price
you can still get, and the bid-ask spread is not in the data.

Costs (Zerodha's published charges):
  holdings route (CNC): STT 0.1% on the sale and 0.1% on the purchase, stamp duty 0.015% on the
      purchase, exchange charges 0.00307% (NSE) / 0.00375% (BSE), SEBI Rs 10 a crore, 18% GST on
      exchange and SEBI charges, and Rs 15.34 DP charge per sale.
  intraday route (MIS, only if your broker lets the two legs offset): brokerage 0.03% or Rs 20 a
      leg, STT 0.025% on the sale, stamp duty 0.003% on the purchase, exchange and SEBI charges,
      18% GST on brokerage and charges.
"""

import argparse
import concurrent.futures
import csv
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
IST = datetime.timedelta(hours=5, minutes=30)
TRADE_CAP = 100000.0       # rupees per episode
TXN = {"NSE": 0.0000307, "BSE": 0.0000375}
SEBI = 10 / 1e7


def cost(route, buy_ex, sell_ex, value):
    """Rupee cost of buying `value` on buy_ex and selling `value` on sell_ex."""
    charges = value * (TXN[buy_ex] + TXN[sell_ex] + 2 * SEBI)
    if route == "holdings":
        return value * 0.001 * 2 + value * 0.00015 + charges * 1.18 + 15.34
    brokerage = 2 * min(20.0, value * 0.0003)
    return brokerage * 1.18 + value * 0.00025 + value * 0.00003 + charges * 1.18


def breakeven(route, value=TRADE_CAP):
    """The gap (as a fraction) a Rs `value` trade needs just to cover its costs."""
    return cost(route, "NSE", "BSE", value) / value


# ---------------------------------------------------------------- data

def fetch_minutes(sym, pause=0.3):
    """{(date, 'HH:MM'): (close, volume)} for a Yahoo symbol's last ~7 days of 1-minute bars."""
    last = None
    for host in ("query2", "query1"):
        for attempt in (0, 1):
            url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=7d&interval=1m" % (host, urllib.parse.quote(sym))
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    doc = json.loads(r.read().decode("utf-8", "replace"))
                time.sleep(pause)
                return parse_minutes(doc)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                if e.code == 429 and attempt == 0:
                    time.sleep(5)
                    continue
                break
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = str(e)
                break
    raise RuntimeError("Yahoo 1-minute chart for %s failed (%s)" % (sym, last))


def parse_minutes(doc):
    res = (doc.get("chart") or {}).get("result") or []
    if not res:
        raise RuntimeError("no data")
    r = res[0]
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    out = {}
    for ts, c, v in zip(r.get("timestamp") or [], q.get("close") or [], q.get("volume") or []):
        if c is None or not v:
            continue                      # no trade that minute: its price would be stale
        t = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).replace(tzinfo=None) + IST
        if not (datetime.time(9, 15) <= t.time() < datetime.time(15, 30)):
            continue
        out[(t.date().isoformat(), t.strftime("%H:%M"))] = (float(c), int(v))
    return out


# ---------------------------------------------------------------- the measurement

def episodes(sym, nse, bse, route):
    """Opportunity episodes for one share: [dict]. Minutes need a trade on both exchanges."""
    be = breakeven(route)
    out, cur = [], None
    for key in sorted(set(nse) & set(bse)):
        (pn, vn), (pb, vb) = nse[key], bse[key]
        hi, lo = max(pn, pb), min(pn, pb)
        gap = hi / lo - 1
        if gap <= be:
            cur = None
            continue
        if cur and cur["date"] == key[0] and cur["dir"] == (pn > pb) and _next_minute(cur["last"], key[1]):
            cur["last"], cur["minutes"] = key[1], cur["minutes"] + 1
            cur["max_gap"] = max(cur["max_gap"], gap)
            continue
        qty = int(min(TRADE_CAP / hi, vn, vb))
        sell_ex, buy_ex = ("NSE", "BSE") if pn > pb else ("BSE", "NSE")
        value = qty * lo
        profit = qty * (hi - lo) - (cost(route, buy_ex, sell_ex, value) if qty else 0)
        cur = {"date": key[0], "time": key[1], "last": key[1], "symbol": sym, "dir": pn > pb,
               "sell_on": sell_ex, "buy_on": buy_ex, "sell_px": hi, "buy_px": lo, "gap": gap, "max_gap": gap,
               "qty": qty, "value": round(value, 2), "profit": round(profit, 2), "minutes": 1}
        out.append(cur)
    return out


def _next_minute(a, b):
    ha, ma = map(int, a.split(":"))
    hb, mb = map(int, b.split(":"))
    return hb * 60 + mb - (ha * 60 + ma) <= 2      # allow one minute without a trade


def day_stats(sym, nse, bse):
    """{date: (minutes compared, median gap, share of minutes above each break-even)}."""
    by = {}
    for key in set(nse) & set(bse):
        (pn, _), (pb, _) = nse[key], bse[key]
        by.setdefault(key[0], []).append(max(pn, pb) / min(pn, pb) - 1)
    out = {}
    for d, gaps in by.items():
        gaps.sort()
        out[d] = (len(gaps), gaps[len(gaps) // 2], sum(g > breakeven("holdings") for g in gaps),
                  sum(g > breakeven("intraday") for g in gaps))
    return out


# ---------------------------------------------------------------- run

def load_universe():
    path = os.path.join(HERE, "forward-test-data-india", "universe.csv")
    out = []
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            sym = (r.get("Symbol") or "").strip().upper()
            if sym and (r.get("Series") or "EQ").strip() == "EQ":
                out.append((sym, (r.get("Company Name") or "").strip()))
    return out


def run(out_dir, universe=None, fetch=fetch_minutes, workers=3):
    os.makedirs(out_dir, exist_ok=True)
    universe = universe or load_universe()
    names = dict(universe)
    data, failed = {}, []

    def one(sym):
        return sym, fetch(sym + ".NS"), fetch(sym + ".BO")
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in concurrent.futures.as_completed([pool.submit(one, s) for s, _ in universe]):
            try:
                sym, n, b = fut.result()
                data[sym] = (n, b)
            except Exception as e:  # noqa: BLE001
                failed.append(str(e)[:80])
    print("loaded %d of %d shares on both exchanges (%d failed)" % (len(data), len(universe), len(failed)), flush=True)

    # Only whole sessions that have finished, and only days not stored before (the record never changes).
    ep_path = os.path.join(out_dir, "episodes.csv")
    day_path = os.path.join(out_dir, "days.csv")
    done = set()
    if os.path.exists(day_path):
        with open(day_path, newline="") as fh:
            done = {r["date"] for r in csv.DictReader(fh)}
    now_ist = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + IST
    last_complete = now_ist.date() if now_ist.time() >= datetime.time(15, 45) else now_ist.date() - datetime.timedelta(days=1)
    dates = sorted({k[0] for n, b in data.values() for k in n} - done)
    dates = [d for d in dates if d <= last_complete.isoformat()]

    new_eps, new_days = [], []
    for d in dates:
        eps = {"holdings": [], "intraday": []}
        compared = above_h = above_i = 0
        medians = []
        for sym, (n, b) in data.items():
            nd = {k: v for k, v in n.items() if k[0] == d}
            bd = {k: v for k, v in b.items() if k[0] == d}
            if not nd or not bd:
                continue
            st = day_stats(sym, nd, bd).get(d)
            if st:
                compared += st[0]
                medians.append(st[1])
                above_h += st[2]
                above_i += st[3]
            for route in eps:
                for e in episodes(sym, nd, bd, route):
                    e["route"], e["name"] = route, names.get(sym, "")
                    eps[route].append(e)
        if not compared:
            continue
        medians.sort()
        new_days.append({"date": d, "shares": len(medians), "minutes_compared": compared,
                         "median_gap_pct": round(100 * medians[len(medians) // 2], 4),
                         "minutes_above_holdings_cost": above_h, "minutes_above_intraday_cost": above_i,
                         "episodes_holdings": len(eps["holdings"]),
                         "profit_holdings": round(sum(e["profit"] for e in eps["holdings"]), 2),
                         "episodes_intraday": len(eps["intraday"]),
                         "profit_intraday": round(sum(e["profit"] for e in eps["intraday"]), 2)})
        new_eps += eps["holdings"] + eps["intraday"]
    cols = ["date", "time", "minutes", "route", "symbol", "name", "sell_on", "buy_on", "sell_px", "buy_px", "gap",
            "max_gap", "qty", "value", "profit"]
    write_rows(ep_path, cols, new_eps)
    dcols = list(new_days[0]) if new_days else []
    if new_days:
        write_rows(day_path, dcols, new_days)
    all_days = []
    if os.path.exists(day_path):
        with open(day_path, newline="") as fh:
            all_days = list(csv.DictReader(fh))
    all_eps = []
    if os.path.exists(ep_path):
        with open(ep_path, newline="") as fh:
            all_eps = list(csv.DictReader(fh))
    with open(os.path.join(out_dir, "sheet_nse_bse.json"), "w") as fh:
        json.dump(sheet_rows(all_days, all_eps), fh, ensure_ascii=False)
    print("new days: %s" % (", ".join(d["date"] for d in new_days) or "none"))
    for d in new_days:
        print("  %s: %d shares, median gap %.3f%%, %d holdings episodes Rs %.0f, %d intraday episodes Rs %.0f" % (
            d["date"], d["shares"], d["median_gap_pct"], d["episodes_holdings"], d["profit_holdings"],
            d["episodes_intraday"], d["profit_intraday"]))
    return 0 if data else 1


def write_rows(path, cols, rows):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items() if k in cols})


def sheet_rows(days, eps):
    bh, bi = breakeven("holdings"), breakeven("intraday")
    rows = [["NSE vs BSE (simulated): the same share on India's two exchanges, minute by minute. If you already "
             "hold a share, sell it where it's dearer and buy the same number back where it's cheaper; your "
             "holding is unchanged and you keep the gap, minus costs. No orders are placed."],
            ["Costs for a Rs 1,00,000 trade: holdings route (sell from demat, buy back, STT both sides) %.3f%%; "
             "intraday route (only if your broker lets the legs offset) %.3f%%. A gap smaller than that loses "
             "money. Trade size: Rs 1,00,000 or what traded on each exchange that minute, whichever is smaller. "
             "Profits are an upper bound: the last trade of a minute may no longer be available." % (100 * bh, 100 * bi)],
            [],
            ["BY DAY", "Date", "Shares compared", "Minutes compared", "Median gap", "Minutes above holdings cost",
             "Minutes above intraday cost", "Holdings route: trades", "Holdings route: profit (Rs)",
             "Intraday route: trades", "Intraday route: profit (Rs)"]]
    for d in sorted(days, key=lambda d: d["date"], reverse=True):
        rows.append(["", "'" + d["date"], int(d["shares"]), int(d["minutes_compared"]),
                     float(d["median_gap_pct"]) / 100, int(d["minutes_above_holdings_cost"]),
                     int(d["minutes_above_intraday_cost"]), int(d["episodes_holdings"]), float(d["profit_holdings"]),
                     int(d["episodes_intraday"]), float(d["profit_intraday"])])
    months = sorted({d["date"][:7] for d in days})
    rows += [[], ["BY MONTH", "Month", "Days", "Holdings route: trades", "Holdings route: profit (Rs)",
                  "Intraday route: trades", "Intraday route: profit (Rs)"]]
    for m in reversed(months):
        ds = [d for d in days if d["date"].startswith(m)]
        rows.append(["", "'" + m, len(ds), sum(int(d["episodes_holdings"]) for d in ds),
                     round(sum(float(d["profit_holdings"]) for d in ds), 2),
                     sum(int(d["episodes_intraday"]) for d in ds), round(sum(float(d["profit_intraday"]) for d in ds), 2)])
    rows += [[], ["BIGGEST GAPS (holdings route, newest first, up to 100)", "Date", "Time", "Share", "Company",
                  "Sell on", "Buy on", "Sell price", "Buy price", "Gap", "Lasted (min)", "Shares", "Value (Rs)",
                  "Profit after costs (Rs)"]]
    hold = [e for e in eps if e["route"] == "holdings"]
    hold.sort(key=lambda e: (e["date"], float(e["profit"])), reverse=True)
    for e in hold[:100]:
        rows.append(["", "'" + e["date"], "'" + e["time"], e["symbol"], e["name"], e["sell_on"], e["buy_on"],
                     float(e["sell_px"]), float(e["buy_px"]), float(e["gap"]), int(e["minutes"]), int(e["qty"]),
                     float(e["value"]), float(e["profit"])])
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(HERE, "nse-bse-data"))
    ap.add_argument("--limit", type=int, default=0, help="only the first N shares (testing)")
    args = ap.parse_args(argv)
    uni = load_universe()
    if args.limit:
        uni = uni[:args.limit]
    return run(args.out, uni)


if __name__ == "__main__":
    sys.exit(main())
