"""Loss review and strategy race for the ORB forward test: what went wrong on each losing trade,
which changes would have turned it into a win, which of those changes hold up across every past
session, and how published intraday strategies compare with the current rule day by day.

Simulation only. This script never places, stages or drafts an order.

For each market it
  1. loads 60 days of 5-minute bars (US: the ~900 most traded stocks, which always contain the
     top 3 by dollars traded by 10:15; India: the Nifty 500) plus the index (SPY / Nifty 50),
  2. rebuilds the forward test's rule v2 for every session in those 60 days, keeping the top 12
     candidates of each day with what was known at the scan (how far above the range, how wide the
     stop, the market's direction, the gap, how late the breakout, volume) and how each one would
     have ended under the current exits and under each alternative,
  3. adds the new sessions to candidates.csv, so the history grows past Yahoo's 60 days,
  4. scores every "mistake" and every "fix" over all stored sessions, split into an earlier and a
     later half: a fix counts as proven only when it beats the current rule in both halves,
  5. keeps a "learned rule" (the current rule plus the fixes proven on the sessions before each
     day) and logs what it would have picked, walk-forward, so its record has no hindsight,
  6. races the current rule and the learned rule against four published strategies (strategies.py)
     on the same days, same data and same risk, and logs each one's daily P&L in race.csv,
  7. writes loss_review.json, lessons.json, learned.csv, race.csv and the Google Sheet rows.
"""

import argparse
import concurrent.futures
import csv
import datetime
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import forward_test as ft  # noqa: E402
import forward_test_india as fi  # noqa: E402
import strategies as st  # noqa: E402

TOP_KEEP = 12          # candidates kept per day, so a skipped pick can be replaced by the next one
MIN_HISTORY = 15       # sessions needed before the learned rule may change anything
MIN_TRADES = 30        # a fix needs this many trades in the history to be judged at all
PROVEN_T = 2.5         # t-statistic of the daily P&L difference needed to call a fix proven. About 15
                       # fixes are tested at once; 2.5 keeps the chance that pure luck "proves" one near 10%.

MARKETS = {
    "us": {"label": "US", "state": "forward-test-data", "open": 9 * 60 + 30, "scan": 10 * 60 + 15,
           "flat": 15 * 60 + 55, "index": "SPY", "index_name": "S&P 500 (SPY)", "account": 5000.0,
           "max_loss": 50.0, "min_price": 5.0, "min_volume": 200000, "cur": "$", "confirm_min": 15,
           "close": 16 * 60, "etf_trend": "QQQ", "etf_index": "SPY",
           "sip": {"min_price": 5.0, "min_avg_vol": 1000000, "min_atr": 0.5, "range_bars": 1, "vol_bars": (0,)}},
    "in": {"label": "India", "state": "forward-test-data-india", "open": 9 * 60 + 15, "scan": 10 * 60,
           "flat": 15 * 60 + 15, "index": "^NSEI", "index_name": "Nifty 50", "account": 500000.0,
           "max_loss": 5000.0, "min_price": 50.0, "min_volume": 100000, "cur": "₹", "confirm_min": 15,
           "close": 15 * 60 + 15, "etf_trend": "NIFTYBEES", "etf_index": "NIFTYBEES",
           "sip": {"min_price": 50.0, "min_avg_vol": 500000, "min_atr": 10.0, "range_bars": 3, "vol_bars": (1, 2)}},
}
N_RANGE = 3            # 15-minute opening range = three 5-minute bars


def bar_closing(m, hhmm_min):
    """Index of the bar that closes at this minute of the day."""
    return (hhmm_min - m["open"]) // 5 - 1


def clock(m, i):
    """Close time of bar i, 'HH:MM'."""
    t = m["open"] + 5 * (i + 1)
    return "%02d:%02d" % (t // 60, t % 60)


# ---------------------------------------------------------------- trade simulation

def shares_for(m, entry, risk):
    if risk <= 0:
        return 0
    return min(int(math.floor(m["max_loss"] / risk + 1e-9)), int(math.floor(m["account"] / entry)))


def run_trade(D, k, last, entry, stop, target, be_at=None, exit_bar=None):
    """Bars after k: stop checked before target in each bar (as the forward test does). be_at moves
    the stop to entry once a bar's high has reached entry + be_at x risk (from the next bar on).
    exit_bar closes the trade at that bar's close. -> (outcome, exit bar index, exit price) or None."""
    risk = entry - stop
    end = min(last, exit_bar) if exit_bar is not None else last
    for i in range(k + 1, end + 1):
        b = D.get(i)
        if b is None:
            return None
        if b["l"] <= stop:
            return ("stop" if stop < entry else "breakeven"), i, stop
        if b["h"] >= target:
            return "target", i, target
        if be_at is not None and b["h"] >= entry + be_at * risk:
            stop = max(stop, entry)
    b = D.get(end)
    if b is None:
        return None
    return ("time exit" if end < last else "closed at bell"), end, round(b["c"], 2)


def result(m, D, k, last, entry, stop, target, **kw):
    """-> dict(outcome, exit, pnl, r, shares) for one trade, or None if bars are missing."""
    risk = round(entry - stop, 2)
    if risk <= 0:
        return None
    out = run_trade(D, k, last, entry, stop, target, **kw)
    if out is None:
        return None
    oc, i, px = out
    sh = shares_for(m, entry, risk)
    return {"outcome": oc, "exit": clock(m, i), "exit_bar": i, "pnl": round(sh * (px - entry), 2),
            "r": round((px - entry) / risk, 2), "shares": sh}


def variants(m, tm, c, last):
    """Every exit/entry alternative for one candidate -> {name: result or None}. c has D, k, oh, ol."""
    D, k, oh, ol = c["D"], c["k"], c["oh"], c["ol"]
    entry = round(D[k]["c"], 2)
    mid = tm.cents_away((oh + ol) / 2, True)
    low = tm.cents_away(ol, True)
    risk = round(entry - mid, 2)
    out = {}

    def std(target_r, stop=mid, **kw):
        r = round(entry - stop, 2)
        return result(m, D, k, last, entry, stop, round(entry + target_r * r, 2), **kw)
    out["base"] = std(1.0)
    out["t05"] = std(0.5)
    out["t15"] = std(1.5)
    out["t2"] = std(2.0)
    out["stop_low"] = std(1.0, stop=low)
    out["breakeven"] = std(1.0, be_at=0.5)
    out["noon"] = std(1.0, exit_bar=bar_closing(m, 12 * 60))
    # Wait for a pullback to the range high (a limit order there, good until 11:00).
    pb = None
    for i in range(k + 1, bar_closing(m, 11 * 60) + 1):
        b = D.get(i)
        if b is None:
            break
        if b["l"] <= oh:
            fill = round(min(oh, b["o"]), 2)
            r = round(fill - mid, 2)
            if r <= 0:
                break
            tgt = round(fill + r, 2)
            if b["l"] <= mid:      # filled and stopped in the same bar
                sh = shares_for(m, fill, r)
                pb = {"outcome": "stop", "exit": clock(m, i), "exit_bar": i, "pnl": round(-sh * r, 2),
                      "r": -1.0, "shares": sh}
            else:
                pb = result(m, D, i, last, fill, mid, tgt)
            break
    out["pullback"] = pb if pb is not None else {"outcome": "no fill", "exit": "", "exit_bar": None,
                                                 "pnl": 0.0, "r": 0.0, "shares": 0}
    # Wait 15 more minutes and buy only if it is still above the range.
    k2 = k + m["confirm_min"] // 5
    b2 = D.get(k2)
    if b2 is None:
        out["confirm"] = None
    elif b2["c"] <= oh:
        out["confirm"] = {"outcome": "skipped", "exit": "", "exit_bar": None, "pnl": 0.0, "r": 0.0, "shares": 0}
    else:
        e2 = round(b2["c"], 2)
        r2 = round(e2 - mid, 2)
        out["confirm"] = result(m, D, k2, last, e2, mid, round(e2 + r2, 2)) if r2 > 0 else None
    return out, entry, mid, risk


EXITS = [  # (key, short name, what changes)
    ("t05", "Target 0.5R", "take profit at half the risk instead of 1R"),
    ("t15", "Target 1.5R", "take profit at 1.5x the risk"),
    ("t2", "Target 2R", "take profit at 2x the risk"),
    ("stop_low", "Stop at range low", "stop at the bottom of the opening range instead of its middle (fewer shares)"),
    ("breakeven", "Breakeven after +0.5R", "move the stop to the entry price once the trade is up half the risk"),
    ("noon", "Exit at 12:00", "close the trade at noon if neither stop nor target was hit"),
    ("pullback", "Buy the pullback", "wait for a dip back to the range high (by 11:00) instead of buying at the scan"),
    ("confirm", "Wait 15 minutes", "buy 15 minutes after the scan, only if still above the range"),
]
EXIT_NAME = dict((k, n) for k, n, _ in EXITS)

# Mistakes: things known at the scan. Thresholds are fixed in advance, not tuned to the losses.
MISTAKES = [
    ("mkt_down", "Market falling at entry", "the index was below its open at the scan",
     lambda f: f["mkt_ret"] is not None and f["mkt_ret"] < 0),
    ("chased", "Chased the breakout", "bought more than 0.5% above the range high",
     lambda f: f["ext_pct"] > 0.5),
    ("wide", "Wide stop", "risk more than 1.5% of the price",
     lambda f: f["risk_pct"] > 1.5),
    ("gap", "Big gap up", "opened more than 2% above the previous close",
     lambda f: f["gap_pct"] is not None and f["gap_pct"] > 2),
    ("late", "Fresh breakout", "first broke out in the 10 minutes before the scan",
     lambda f: f["late"]),
    ("fading", "Already fading", "more than half the risk below its high since the breakout",
     lambda f: f["faded_r"] > 0.5),
    ("quiet", "Ordinary volume", "dollars traded by the scan below its own 10-day average for that time",
     lambda f: f["rel_vol"] is not None and f["rel_vol"] < 1),
]
MISTAKE = {k: (n, d, fn) for k, n, d, fn in MISTAKES}


def features(m, sym, days, iso, D, k, oh, ol, first, dv, index_days):
    entry = D[k]["c"]
    mid = (oh + ol) / 2
    risk = max(entry - mid, 1e-9)
    prev = [d for d in sorted(days) if d < iso]
    prev_close = None
    if prev:
        pd = days[prev[-1]]
        prev_close = pd[max(pd)]["c"]
    hist = []
    for d in reversed(prev):
        P = days[d]
        if all(i in P for i in range(k + 1)):
            hist.append(sum(P[i]["c"] * P[i]["v"] for i in range(k + 1)))
        if len(hist) == 10:
            break
    I = (index_days or {}).get(iso, {})
    mkt_ret = mkt_above = None
    if 0 in I and k in I:
        mkt_ret = (I[k]["c"] / I[0]["o"] - 1) * 100
        if all(i in I for i in range(N_RANGE)):
            mkt_above = I[k]["c"] > max(I[i]["h"] for i in range(N_RANGE))
    hi = max(D[i]["h"] for i in range(first, k + 1))
    return {"ext_pct": (entry / oh - 1) * 100, "risk_pct": risk / entry * 100, "range_pct": (oh - ol) / entry * 100,
            "gap_pct": None if not prev_close else (D[0]["o"] / prev_close - 1) * 100,
            "mkt_ret": mkt_ret, "mkt_above_or": mkt_above, "first_bar": first, "late": first >= k - 1,
            "faded_r": (hi - entry) / risk, "rel_vol": (dv / (sum(hist) / len(hist))) if len(hist) >= 5 else None,
            "dv": dv}


def candidates_for_day(m, tm, iso, charts, names, index_days):
    """Rule v2's candidates for one session, best first (one per company), with features and variants."""
    k = bar_closing(m, m["scan"])
    last = bar_closing(m, m["flat"])
    cands = []
    for sym, days in charts.items():
        D = days.get(iso)
        if not D or any(i not in D for i in range(k + 1)):
            continue
        oh = max(D[i]["h"] for i in range(N_RANGE))
        ol = min(D[i]["l"] for i in range(N_RANGE))
        first = next((i for i in range(N_RANGE, k + 1) if D[i]["c"] > oh), None)
        if first is None:
            continue
        if D[k]["c"] < m["min_price"] or sum(D[i]["v"] for i in range(k + 1)) < m["min_volume"]:
            continue
        if D[k]["c"] <= oh:
            continue
        dv = sum(D[i]["c"] * D[i]["v"] for i in range(k + 1))
        cands.append((dv, sym, D, oh, ol, first))
    cands.sort(key=lambda x: -x[0])
    out = []
    for rank, (dv, sym, D, oh, ol, first) in enumerate(ft.one_per_company(cands, lambda c: c[1], names, TOP_KEEP), 1):
        c = {"D": D, "k": k, "oh": oh, "ol": ol}
        v, entry, stop, risk = variants(m, tm, c, last)
        if v.get("base") is None:
            continue
        f = features(m, sym, charts[sym], iso, D, k, oh, ol, first, dv, index_days)
        base = v["base"]
        hi = max([D[i]["h"] for i in range(k + 1, base["exit_bar"] + 1)] or [entry])
        f["mfe_r"] = (hi - entry) / risk if risk > 0 else 0
        f["mins_held"] = 5 * (base["exit_bar"] - k)
        out.append({"date": iso, "rank": rank, "symbol": sym, "name": names.get(sym, ""), "entry": entry,
                    "stop": stop, "risk": risk, "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "breakout": clock(m, first), "feat": f, "var": v})
    return out


# ---------------------------------------------------------------- stored history

def flat_row(c):
    r = {"date": c["date"], "rank": c["rank"], "symbol": c["symbol"], "name": c["name"], "entry": c["entry"],
         "stop": c["stop"], "risk": c["risk"], "or_high": c["or_high"], "or_low": c["or_low"],
         "breakout": c["breakout"]}
    for key, val in c["feat"].items():
        r["f_" + key] = "" if val is None else (round(val, 4) if isinstance(val, float) else val)
    for key, res in c["var"].items():
        r["v_%s_pnl" % key] = "" if res is None else res["pnl"]
        r["v_%s_r" % key] = "" if res is None else res["r"]
        r["v_%s_out" % key] = "" if res is None else res["outcome"]
        r["v_%s_exit" % key] = "" if res is None else res["exit"]
    return r


def unflat(r):
    def num(x):
        if x in ("", None):
            return None
        if x in ("True", "False"):
            return x == "True"
        try:
            return float(x)
        except ValueError:
            return x
    feat = {k[2:]: num(v) for k, v in r.items() if k.startswith("f_")}
    var = {}
    for key in ["base"] + [k for k, _, _ in EXITS]:
        if r.get("v_%s_pnl" % key, "") == "":
            var[key] = None
        else:
            var[key] = {"pnl": float(r["v_%s_pnl" % key]), "r": float(r["v_%s_r" % key]),
                        "outcome": r["v_%s_out" % key], "exit": r["v_%s_exit" % key]}
    return {"date": r["date"], "rank": int(float(r["rank"])), "symbol": r["symbol"], "name": r["name"],
            "entry": float(r["entry"]), "stop": float(r["stop"]), "risk": float(r["risk"]),
            "or_high": float(r["or_high"]), "or_low": float(r["or_low"]), "breakout": r["breakout"],
            "feat": feat, "var": var}


def load_history(path):
    if not os.path.exists(path):
        return {}
    by_day = {}
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            by_day.setdefault(r["date"], []).append(unflat(r))
    for d in by_day:
        by_day[d].sort(key=lambda c: c["rank"])
    return by_day


def save_history(path, by_day):
    rows = [flat_row(c) for d in sorted(by_day) for c in by_day[d]]
    if not rows:
        return
    cols = list(rows[0])
    for r in rows:
        for c in r:
            if c not in cols:
                cols.append(c)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


# ---------------------------------------------------------------- scoring

def picks(day_cands, skip=(), top=3):
    """The top 3 of the day that none of the `skip` mistakes flag."""
    out = []
    for c in day_cands:
        if any(MISTAKE[s][2](c["feat"]) for s in skip):
            continue
        out.append(c)
        if len(out) == top:
            break
    return out


def score(by_day, days, exit_key="base", skip=()):
    """P&L of a rule over these days -> dict(trades, wins, pnl, r, halves=[pnl1, pnl2])."""
    res = {"trades": 0, "wins": 0, "pnl": 0.0, "r": 0.0, "halves": [0.0, 0.0], "days": len(days), "daily": []}
    half = len(days) // 2
    for n, d in enumerate(days):
        day_pnl = 0.0
        for c in picks(by_day[d], skip):
            v = c["var"].get(exit_key)
            if v is None or v["outcome"] in ("no fill", "skipped"):
                continue
            res["trades"] += 1
            res["wins"] += v["pnl"] > 0
            res["pnl"] += v["pnl"]
            res["r"] += v["r"]
            res["halves"][0 if n < half else 1] += v["pnl"]
            day_pnl += v["pnl"]
        res["daily"].append(day_pnl)
    res["pnl"] = round(res["pnl"], 2)
    res["halves"] = [round(x, 2) for x in res["halves"]]
    return res


def t_stat(base, alt):
    """t-statistic of the day-by-day P&L difference (alt minus base); 0 when there is no difference."""
    diff = [a - b for a, b in zip(alt["daily"], base["daily"])]
    n = len(diff)
    if n < 2:
        return 0.0
    mean = sum(diff) / n
    var = sum((x - mean) ** 2 for x in diff) / (n - 1)
    if var == 0:
        return 0.0
    return mean / math.sqrt(var / n)


def verdict(base, alt):
    if alt["trades"] < MIN_TRADES:
        return "Too few trades to judge"
    t = t_stat(base, alt)
    better = [alt["halves"][i] > base["halves"][i] for i in (0, 1)]
    if alt["pnl"] > base["pnl"] and all(better) and t >= PROVEN_T:
        return "Proven: better in both halves, unlikely to be luck"
    if alt["pnl"] > base["pnl"] and t >= 1:
        return "Promising: better, but could still be luck"
    if alt["pnl"] > base["pnl"]:
        return "No clear difference"
    return "Does not help"


def learn(by_day, days):
    """The learned rule from these days: proven skips, plus the best proven exit, kept only if the
    combination still beats the current rule in both halves. -> (exit_key, skips, evaluations)."""
    base = score(by_day, days)
    evals = {"base": base}
    skips = []
    for key, *_ in MISTAKES:
        s = score(by_day, days, skip=(key,))
        evals["skip_" + key] = s
        if verdict(base, s).startswith("Proven"):
            skips.append(key)
    best_exit, best_pnl = "base", base["pnl"]
    for key, *_ in EXITS:
        s = score(by_day, days, exit_key=key)
        evals["exit_" + key] = s
        if verdict(base, s).startswith("Proven") and s["pnl"] > best_pnl:
            best_exit, best_pnl = key, s["pnl"]
    combo = score(by_day, days, exit_key=best_exit, skip=tuple(skips))
    if (best_exit, tuple(skips)) != ("base", ()) and not verdict(base, combo).startswith("Proven"):
        # The pieces were each proven but not together: keep the single best piece.
        options = [(evals["exit_" + best_exit]["pnl"], best_exit, ())] if best_exit != "base" else []
        options += [(evals["skip_" + s]["pnl"], "base", (s,)) for s in skips]
        _, best_exit, skips = max(options) if options else (0, "base", ())
        skips = list(skips)
    return best_exit, skips, evals


def rule_text(m, exit_key, skips):
    parts = []
    if exit_key != "base":
        parts.append(EXIT_NAME[exit_key])
    parts += ["skip if %s" % MISTAKE[s][0].lower() for s in skips]
    return "Current rule" if not parts else "Current rule + " + "; ".join(parts)


# ---------------------------------------------------------------- data loading

def load_charts(m, tm, syms, fetch, workers, pause, minutes):
    now = ft.utcnow()
    charts, failed = {}, []
    deadline = time.time() + minutes * 60

    def one(sym):
        if time.time() > deadline:
            raise RuntimeError("time limit")
        text = fetch(sym)
        time.sleep(pause)
        return text
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(one, s): s for s in syms}
        for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            sym = futs[fut]
            try:
                ysym = (sym + ".NS") if m is MARKETS["in"] and not sym.startswith("^") else sym
                _, bars, _ = tm.parse_bars(tm.chart_result(fut.result(), ysym), now)
                charts[sym] = tm.split_sessions(bars)
            except Exception as e:  # noqa: BLE001
                failed.append("%s: %s" % (sym, str(e)[:60]))
            if i % 100 == 0:
                print("  %d/%d charts, %d failed, %.0fs" % (i, len(syms), len(failed), time.time() - t0), flush=True)
    print("charts: %d of %d in %.0fs" % (len(charts), len(syms), time.time() - t0), flush=True)
    return charts, failed


def us_liquid(repo, state, n, pause):
    """The n US stocks with the most dollars traded per day over the last month (Yahoo spark)."""
    scan = ft.load_module(os.path.join(repo, "trader-scanner", "scan.py"), "scan_lr")
    syms = []
    with open(os.path.join(state, "universe.txt"), encoding="utf-8") as fh:
        for line in fh:
            s = line.split("|")[0].strip().upper()
            if s:
                syms.append(s)
    data, failed = scan.fetch_spark(syms, "1mo", "1d", pause)
    print("spark: %d of %d symbols (%d failed)" % (len(data), len(syms), len(failed)), flush=True)
    dollars = {}
    for s, ser in data.items():
        cs, vs = ser.get("c") or [], ser.get("v") or []
        pairs = [(c, v) for c, v in zip(cs, vs) if c and v]
        if pairs:
            dollars[s] = sum(c * v for c, v in pairs) / len(pairs)
        else:
            meta = ser.get("meta") or {}
            if meta.get("regularMarketPrice") and meta.get("regularMarketVolume"):
                dollars[s] = meta["regularMarketPrice"] * meta["regularMarketVolume"]
    ranked = sorted(dollars, key=lambda s: -dollars[s])
    if len(ranked) < n // 2:
        raise RuntimeError("spark gave liquidity for only %d symbols" % len(ranked))
    return ranked[:n]


# ---------------------------------------------------------------- outputs

def mistakes_of(f):
    return [k for k, (_, _, fn) in MISTAKE.items() if fn(f)]


def fmt_money(m, x):
    if x is None:
        return ""
    return ("+" if x > 0 else "-" if x < 0 else "") + m["cur"] + "{:,.2f}".format(abs(x))


def review_losses(m, tm, state, charts, by_day, names, index_days):
    """One row per losing forward-test trade (rule v2)."""
    with open(os.path.join(state, "results.csv"), newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("rule") == "v2" and r.get("pnl_usd")]
    out = []
    for r in rows:
        if float(r["pnl_usd"]) >= 0:
            continue
        iso, sym = r["date"], r["symbol"]
        c = next((c for c in by_day.get(iso, []) if c["symbol"] == sym), None)
        v = None
        if c is None and sym in charts and iso in charts[sym] and all(i in charts[sym][iso] for i in range(9)):
            # The rebuilt list missed it (e.g. not in the liquid set): rebuild this one trade.
            D = charts[sym][iso]
            k = bar_closing(m, m["scan"])
            oh, ol = float(r["or_high"]), float(r["or_low"])
            first = next((i for i in range(N_RANGE, k + 1) if D.get(i) and D[i]["c"] > oh), k)
            v, entry, stop, risk = variants(m, tm, {"D": D, "k": k, "oh": oh, "ol": ol}, bar_closing(m, m["flat"]))
            if v.get("base") is None:
                v = None
        if c is None and v is not None:
            dv = sum(D[i]["c"] * D[i]["v"] for i in range(k + 1))
            f = features(m, sym, charts[sym], iso, D, k, oh, ol, first, dv, index_days)
            base = v["base"]
            hi = max([D[i]["h"] for i in range(k + 1, base["exit_bar"] + 1)] or [entry])
            f["mfe_r"] = (hi - entry) / risk if risk > 0 else 0
            f["mins_held"] = 5 * (base["exit_bar"] - k)
            c = {"feat": f, "var": v}
        if c is None:
            gone = sym not in charts or iso not in charts[sym]
            out.append({"date": iso, "symbol": sym, "name": r.get("name", ""), "outcome": r["actual_outcome"],
                        "pnl": float(r["pnl_usd"]), "fixes": [], "best": "", "best_pnl": None,
                        "wrong": "older than Yahoo's 60 days of 5-minute bars" if gone else "could not replay this trade"})
            continue
        f, v = c["feat"], c["var"]
        wrong = [MISTAKE[k][0] + " (" + detail(k, f, m) + ")" for k in mistakes_of(f)]
        after = []
        if r["actual_outcome"] == "stop":
            if f.get("mfe_r") is not None and f["mfe_r"] >= 0.5:
                after.append("was up %.1fR before turning down" % (math.floor(f["mfe_r"] * 10) / 10))
            if f.get("mins_held") is not None and f["mins_held"] <= 30:
                after.append("stopped within %d minutes" % f["mins_held"])
        fixes = [(EXIT_NAME[k], v[k]["pnl"]) for k, _, _ in EXITS if v.get(k) and v[k]["pnl"] > 0]
        avoided = [(k, v[k]) for k, _, _ in EXITS if v.get(k) and v[k]["outcome"] in ("no fill", "skipped")]
        skip_fix = ["skip if " + MISTAKE[k][0].lower() for k in mistakes_of(f)]
        best = max(((EXIT_NAME[k] + (" (no trade)" if v[k]["outcome"] in ("no fill", "skipped") else ""), v[k]["pnl"])
                    for k, _, _ in EXITS if v.get(k)), key=lambda x: x[1], default=None)
        out.append({"date": iso, "symbol": sym, "name": r.get("name", "") or names.get(sym, ""),
                    "outcome": r["actual_outcome"], "exit_time": r.get("exit_time", ""), "pnl": float(r["pnl_usd"]),
                    "wrong": "; ".join(wrong) or "nothing flagged at entry", "after": "; ".join(after),
                    "fixes": fixes, "avoided": [EXIT_NAME[k] for k, _ in avoided], "skips": skip_fix,
                    "best": best[0] if best else "", "best_pnl": best[1] if best else None})
    return out


def detail(key, f, m):
    if key == "mkt_down":
        return "index %+.2f%% since the open" % f["mkt_ret"]
    if key == "chased":
        return "%.2f%% above the range" % f["ext_pct"]
    if key == "wide":
        return "risk %.2f%% of price" % f["risk_pct"]
    if key == "gap":
        return "gapped up %.1f%%" % f["gap_pct"]
    if key == "late":
        return "broke out at %s" % clock(m, int(f["first_bar"]))
    if key == "fading":
        return "%.1fR below its high" % f["faded_r"]
    if key == "quiet":
        return "%.1fx usual volume" % f["rel_vol"]
    return ""


def sheet_rows_review(m, review):
    head = ["Date", "Stock", "Company", "Outcome", "P&L", "What went wrong (known at entry)", "After entry",
            "Would have been a win with", "Would have been skipped by", "Best alternative", "Best alternative P&L"]
    rows = [head]
    for r in sorted(review, key=lambda r: (r["date"], r["symbol"]), reverse=True):
        wins = ", ".join("%s (%s)" % (n, fmt_money(m, p)) for n, p in r["fixes"]) or "none of the tested changes"
        skipped = ", ".join(r.get("skips", []) + ["%s (no trade)" % a for a in r.get("avoided", [])])
        when = " at " + r["exit_time"] if r.get("exit_time") and r["outcome"] in ("stop", "target") else ""
        rows.append(["'" + r["date"], r["symbol"], r["name"], r["outcome"] + when,
                     r["pnl"], r["wrong"], r.get("after", ""), wins, skipped or "", r["best"],
                     "" if r["best_pnl"] is None else r["best_pnl"]])
    return rows


def sheet_rows_lessons(m, by_day, days, fwd_trades, learned_now, learned_log, v2_log):
    base = score(by_day, days)
    rows = [["Lessons from %d sessions (%s to %s): %d trades under the current rule, %s"
             % (len(days), days[0], days[-1], base["trades"], fmt_money(m, base["pnl"]))] if days else ["No history yet"],
            ["A change counts as proven only if it beats the current rule in both the earlier and the later half of "
             "these sessions AND its day-by-day gain is large next to its noise (t of 2.5 or more), because many "
             "changes are tested at once. Thresholds were fixed in advance, not tuned to the losses. Simulation only."],
            [],
            ["MISTAKES (known at entry)", "What it means", "Forward-test losses with it", "Forward-test wins with it",
             "History: trades with it", "History: win rate with it", "History: win rate without", "History: avg R with",
             "History: avg R without", "Skip these picks: P&L", "vs current rule", "Earlier half", "Later half",
             "Evidence (t, 2.5+ = proven)", "Verdict"]]
    all_trades = [c for d in days for c in picks(by_day[d])]
    for key, name, desc, fn in MISTAKES:
        w = [c for c in all_trades if fn(c["feat"])]
        wo = [c for c in all_trades if not fn(c["feat"])]
        fl = [t for t in fwd_trades if t["loss"] and t["feat"] is not None and fn(t["feat"])]
        fw = [t for t in fwd_trades if not t["loss"] and t["feat"] is not None and fn(t["feat"])]
        nl = sum(1 for t in fwd_trades if t["loss"] and t["feat"] is not None)
        nw = sum(1 for t in fwd_trades if not t["loss"] and t["feat"] is not None)
        s = score(by_day, days, skip=(key,))

        def wr(g):
            return "" if not g else round(sum(1 for c in g if c["var"]["base"]["pnl"] > 0) / len(g), 3)

        def ar(g):
            return "" if not g else round(sum(c["var"]["base"]["r"] for c in g) / len(g), 3)
        rows.append([name, desc, "%d of %d" % (len(fl), nl), "%d of %d" % (len(fw), nw), len(w), wr(w), wr(wo), ar(w), ar(wo),
                     s["pnl"], round(s["pnl"] - base["pnl"], 2), s["halves"][0], s["halves"][1],
                     round(t_stat(base, s), 2), verdict(base, s)])
    rows += [[], ["FIXES TESTED", "What changes", "Trades", "Win rate", "Total P&L", "vs current rule", "Avg R",
                  "Earlier half", "Later half", "Evidence (t, 2.5+ = proven)", "Verdict"]]
    rows.append(["Current rule (v2)", "stop at range middle, target 1R, close at the bell", base["trades"],
                 "" if not base["trades"] else round(base["wins"] / base["trades"], 3), base["pnl"], 0,
                 "" if not base["trades"] else round(base["r"] / base["trades"], 3), base["halves"][0], base["halves"][1], "", ""])
    for key, name, desc in EXITS:
        s = score(by_day, days, exit_key=key)
        rows.append([name, desc, s["trades"], "" if not s["trades"] else round(s["wins"] / s["trades"], 3), s["pnl"],
                     round(s["pnl"] - base["pnl"], 2), "" if not s["trades"] else round(s["r"] / s["trades"], 3),
                     s["halves"][0], s["halves"][1], round(t_stat(base, s), 2), verdict(base, s)])
    exit_key, skips = learned_now
    combo = score(by_day, days, exit_key=exit_key, skip=tuple(skips))
    rows += [[], ["LEARNED RULE", rule_text(m, exit_key, skips)],
             ["History P&L", combo["pnl"], "vs current rule", round(combo["pnl"] - base["pnl"], 2)],
             ["The learned rule is rebuilt every day from the sessions before it, then used on that day only, so its "
              "forward record below has no hindsight."],
             [], ["Date", "Learned rule", "Picks", "Day P&L", "Running P&L", "Current rule running P&L (same rebuild)"]]
    run = 0.0
    for r in learned_log:
        run += float(r["pnl"])
        rows.append(["'" + r["date"], r["rule"], r["picks"], float(r["pnl"]), round(run, 2), v2_log.get(r["date"], "")])
    return rows


# ---------------------------------------------------------------- strategy race

RACE = [  # key, name, what it trades, source
    ("v2", "Current rule", "15-min opening-range breakout, top 3 by money traded at the scan, long only",
     "the day-trader skill pack, forward test rule v2"),
    ("learned", "Learned rule", "current rule plus the fixes proven on earlier sessions (rebuilt each day)",
     "this project's loss review"),
    ("sip", "Stocks in Play ORB", "opening-range breakout (US 5 min; India 15 min), long or short, top 3 stocks by "
     "relative volume, stop 10% of ATR, held to the close", "Zarattini, Barbon & Aziz 2024 (SFI 24-98)"),
    ("vwap", "VWAP trend", "long above VWAP, short below, on the index ETF", "Zarattini & Aziz 2023"),
    ("momentum", "Intraday momentum", "last half hour in the direction of the first half hour, on the index ETF",
     "Gao, Han, Li & Zhou 2018 (JFE)"),
    ("noise", "Noise-area momentum", "index ETF breakouts from its usual intraday range, checked every 30 minutes",
     "Zarattini, Aziz & Barbon 2024 (SFI 24-97)"),
]
RACE_NAME = {k: n for k, n, _, _ in RACE}


def trade_text(m, t):
    if t.get("outcome") == "not triggered":
        return "%s %s not triggered" % (t["symbol"], t["side"])
    return "%s %s %s" % (t["symbol"], t["side"], fmt_money(m, t["pnl"]))


def race_day(m, iso, charts, names, etfs, by_day):
    """{strategy: (trades, pnl, detail)} for one session; None for a strategy whose data is missing."""
    last = bar_closing(m, m["flat"])
    out = {}
    if iso in by_day:
        ts = [c for c in picks(by_day[iso]) if c["var"].get("base")]
        out["v2"] = (len(ts), round(sum(c["var"]["base"]["pnl"] for c in ts), 2),
                     "; ".join("%s %s" % (c["symbol"], fmt_money(m, c["var"]["base"]["pnl"])) for c in ts))
    else:
        out["v2"] = (0, 0.0, "no breakout still above its range at the scan")
    sp = m["sip"]
    ref = etfs.get(m["etf_index"]) or {}
    if sum(1 for d in ref if d < iso) >= 15:   # the ATR and volume averages need 14 earlier sessions
        funnel = {}
        chosen = st.stocks_in_play_picks(charts, iso, sp["min_price"], sp["min_avg_vol"], sp["min_atr"], names,
                                         ft.one_per_company, funnel=funnel, range_bars=sp["range_bars"],
                                         vol_bars=sp["vol_bars"])
        print("  %s %s stocks in play: %s" % (m["label"], iso, ", ".join("%s %d" % kv for kv in sorted(funnel.items()))),
              flush=True)
        trades = [t for t in (st.stocks_in_play_trade(sym, D, s, last, m["account"], m["max_loss"], m["open"],
                                                      range_bars=sp["range_bars"]) for _, sym, D, s in chosen) if t]
        out["sip"] = (sum(1 for t in trades if t["outcome"] != "not triggered"),
                      round(sum(t["pnl"] for t in trades), 2),
                      "; ".join(trade_text(m, t) for t in trades) or "no stock in play")
    tr = etfs.get(m["etf_trend"], {}).get(iso)
    if tr:
        ts = st.vwap_trend(m["etf_trend"], tr, last, m["account"], m["open"])
        if ts is not None:
            out["vwap"] = (len(ts), round(sum(t["pnl"] for t in ts), 2),
                           "%d flips; %s" % (len(ts), fmt_money(m, sum(t["pnl"] for t in ts))))
    idx_days = etfs.get(m["etf_index"], {})
    D = idx_days.get(iso)
    if D:
        prev = sorted(d for d in idx_days if d < iso and idx_days[d])
        pc = idx_days[prev[-1]][max(idx_days[prev[-1]])]["c"] if prev else None
        end = bar_closing(m, m["close"])
        ts = st.intraday_momentum(m["etf_index"], D, pc, bar_closing(m, m["open"] + 30),
                                  bar_closing(m, m["close"] - 30), end, m["account"], m["open"])
        if ts is not None:
            out["momentum"] = (len(ts), round(sum(t["pnl"] for t in ts), 2),
                               "; ".join("%s (first half hour %+.2f%%)" % (trade_text(m, t), t["signal"]) for t in ts)
                               or "no move in the first half hour")
        ts = st.noise_area(m["etf_index"], idx_days, iso, last, m["account"], m["open"])
        if ts is not None:
            out["noise"] = (len(ts), round(sum(t["pnl"] for t in ts), 2),
                            "; ".join(trade_text(m, t) for t in ts) or "stayed inside the noise area")
    return out


def load_race(path):
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            out.setdefault(r["date"], {})[r["strategy"]] = (int(r["trades"]), float(r["pnl"]), r["detail"])
    return out


def save_race(path, race):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "strategy", "trades", "pnl", "detail"])
        for d in sorted(race):
            for k, _, _, _ in RACE:
                if k in race[d]:
                    w.writerow([d, k] + list(race[d][k]))


def sheet_rows_race(m, race, fwd_days, today):
    days = [d for d in sorted(race) if d >= fwd_days[0]] if fwd_days else []
    hist = sorted(race)
    rows = [["Strategy race (%s): every strategy on the same days and the same %s%s account. Stock strategies risk "
             "%s%s per trade (3 trades a day at most); index-ETF strategies put the whole account in the ETF. "
             "Simulated only, no costs." % (m["label"], m["cur"], "{:,.0f}".format(m["account"]), m["cur"],
                                            "{:,.0f}".format(m["max_loss"]))],
            ["Race since %s (the forward test's first day), %d sessions. Backtest column: every stored session "
             "(%s to %s)." % (days[0] if days else "-", len(days), hist[0] if hist else "-", hist[-1] if hist else "-")],
            [],
            ["Strategy", "How it trades", "Source", "Race P&L", "Trades", "Winning days", "Losing days", "Best day",
             "Worst day", "Latest day (%s)" % (today or "-"), "Backtest P&L", "Backtest days"]]
    board = []
    for k, name, how, src in RACE:
        vals = [race[d][k][1] for d in days if k in race[d]]
        tr = sum(race[d][k][0] for d in days if k in race[d])
        bt = [race[d][k][1] for d in hist if k in race[d]] if k != "learned" else []
        board.append([name, how, src, round(sum(vals), 2) if vals else "", tr, sum(1 for v in vals if v > 0),
                      sum(1 for v in vals if v < 0), max(vals) if vals else "", min(vals) if vals else "",
                      race.get(today, {}).get(k, ("", "", ""))[1] if today else "",
                      round(sum(bt), 2) if bt else ("walk-forward only" if k == "learned" else ""), len(bt) or ""])
    board.sort(key=lambda r: -(r[3] if isinstance(r[3], (int, float)) else -1e18))
    rows += board
    rows += [[], ["Day by day", ""] + [n for _, n, _, _ in RACE] + ["Leader of the day"]]
    for d in sorted(days, reverse=True):
        vals = [race[d].get(k, (None, None, None))[1] for k, _, _, _ in RACE]
        best = max(((v, n) for v, (k, n, _, _) in zip(vals, RACE) if v is not None), default=None)
        rows.append(["'" + d, ""] + ["" if v is None else v for v in vals] + [best[1] if best else ""])
    rows += [[], ["Trades on the latest day", ""]]
    for k, name, _, _ in RACE:
        if today and k in race.get(today, {}):
            rows.append([name, race[today][k][2]])
    return rows


# ---------------------------------------------------------------- main

def run_market(key, args):
    m = MARKETS[key]
    state = os.path.abspath(os.path.join(HERE, m["state"]))
    repo = os.path.abspath(args.repo)
    if key == "us":
        tm = ft.load_module(os.path.join(repo, "trader-timemachine", "timemachine.py"), "timemachine_lr")
        names = ft.universe_names(os.path.join(state, "universe.txt"))
        syms = us_liquid(repo, state, args.us_stocks, args.pause)
        with open(os.path.join(state, "results.csv"), newline="") as fh:
            syms += [r["symbol"] for r in csv.DictReader(fh) if r.get("symbol") and r["symbol"] not in syms]
        fetch = tm.fetch_chart
    else:
        tm = fi.nse_timemachine(repo)
        universe = fi.load_universe(state)
        names = dict(universe)
        syms = [s for s, _ in universe]

        def fetch(sym):
            if sym.startswith("^"):
                return tm._get("https://query2.finance.yahoo.com/v8/finance/chart/%5ENSEI?range=60d&interval=5m")
            return fi.fetch_chart(tm, sym)
    print("%s: %d stocks" % (m["label"], len(syms)), flush=True)
    extra = [m["index"]] + sorted({m["etf_trend"], m["etf_index"]} - {m["index"]})
    charts, failed = load_charts(m, tm, syms + extra, fetch, args.workers, args.pause, args.chart_minutes)
    index_days = charts.pop(m["index"], None)
    etfs = {s: charts.pop(s) for s in extra[1:] if s in charts}
    if m["index"] in (m["etf_trend"], m["etf_index"]) and index_days is not None:
        etfs[m["index"]] = index_days
    if index_days is None:
        print("  index chart failed: market-direction checks will be blank", file=sys.stderr)
    if len(charts) < len(syms) * 0.9:
        print("Only %d of %d charts loaded; not writing results. %s" % (len(charts), len(syms), "; ".join(failed[:5])),
              file=sys.stderr)
        return 1

    last = bar_closing(m, m["flat"])
    # Complete sessions: the index has every bar to the flat time (an unfinished day never does).
    if index_days:
        complete = sorted(d for d, D in index_days.items() if all(i in D for i in range(last + 1)))
    else:
        counts = {}
        for days in charts.values():
            for d, D in days.items():
                counts[d] = counts.get(d, 0) + all(i in D for i in range(last + 1))
        complete = sorted(d for d, c in counts.items() if c >= len(charts) // 4)

    out_dir = os.path.join(state, "review")
    os.makedirs(out_dir, exist_ok=True)
    hist_path = os.path.join(out_dir, "candidates.csv")
    by_day = load_history(hist_path)
    new = 0
    for iso in complete:
        if iso in by_day and not args.rebuild:
            continue
        cands = candidates_for_day(m, tm, iso, charts, names, index_days)
        if cands:
            by_day[iso] = cands
            new += 1
    save_history(hist_path, by_day)
    days = sorted(by_day)
    print("%s: %d sessions stored (%d new), %s to %s" % (m["label"], len(days), new, days[0], days[-1]), flush=True)

    # How well the rebuild matches the forward test's own picks.
    with open(os.path.join(state, "results.csv"), newline="") as fh:
        fwd = [r for r in csv.DictReader(fh) if r.get("rule") == "v2" and r.get("symbol")]
    match = [(r["date"], r["symbol"]) for r in fwd if r["date"] in by_day]
    hit = sum(1 for d, s in match if s in [c["symbol"] for c in picks(by_day[d])])
    print("%s: rebuilt picks match the forward test on %d of %d trades" % (m["label"], hit, len(match)), flush=True)

    review = review_losses(m, tm, state, charts, by_day, names, index_days)
    fwd_trades = []
    for r in fwd:
        if not r.get("pnl_usd"):
            continue
        c = next((c for c in by_day.get(r["date"], []) if c["symbol"] == r["symbol"]), None)
        fwd_trades.append({"loss": float(r["pnl_usd"]) < 0, "feat": c["feat"] if c else None})

    # Walk-forward learned rule: for each forward-test day, learn from the sessions before it.
    learned_path = os.path.join(out_dir, "learned.csv")
    log = []
    if os.path.exists(learned_path):
        with open(learned_path, newline="") as fh:
            log = list(csv.DictReader(fh))
    logged = {r["date"] for r in log}
    with open(os.path.join(state, "results.csv"), newline="") as fh:
        fwd_days = sorted({r["date"] for r in csv.DictReader(fh) if r.get("rule") == "v2"})
    for d in fwd_days:
        if d in logged or (d not in by_day and d not in complete):
            continue
        before = [x for x in days if x < d]
        if len(before) < MIN_HISTORY:
            continue
        if d not in by_day:
            log.append({"date": d, "rule": "no setup", "exit": "", "skips": "", "picks": "", "pnl": 0.0})
            continue
        exit_key, skips, _ = learn(by_day, before)
        chosen = picks(by_day[d], skips)
        pnl = sum((c["var"].get(exit_key) or {"pnl": 0})["pnl"] for c in chosen
                  if c["var"].get(exit_key) and c["var"][exit_key]["outcome"] not in ("no fill", "skipped"))
        log.append({"date": d, "rule": rule_text(m, exit_key, skips), "exit": exit_key, "skips": "|".join(skips),
                    "picks": " ".join(c["symbol"] for c in chosen), "pnl": round(pnl, 2)})
    log.sort(key=lambda r: r["date"])
    with open(learned_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["date", "rule", "exit", "skips", "picks", "pnl"])
        w.writeheader()
        w.writerows(log)
    # The learned rule's record next to the rebuilt current rule on the same days, so the only
    # difference between the two columns is what was learned.
    v2_log, run = {}, 0.0
    for r in log:
        d = r["date"]
        run += sum(c["var"]["base"]["pnl"] for c in picks(by_day.get(d, [])) if c["var"].get("base"))
        v2_log[d] = round(run, 2)
    race_path = os.path.join(out_dir, "race.csv")
    race = load_race(race_path)
    for iso in complete:
        if iso in race and not args.rebuild:
            continue
        race[iso] = race_day(m, iso, charts, names, etfs, by_day)
    for r in log:
        race.setdefault(r["date"], {})["learned"] = (len(r["picks"].split()), float(r["pnl"]), "%s: %s" % (r["rule"], r["picks"]))
    save_race(race_path, race)
    fwd_in = [d for d in fwd_days if d in race]
    today = max(race) if race else None
    with open(os.path.join(out_dir, "sheet_race.json"), "w") as fh:
        json.dump(sheet_rows_race(m, race, fwd_in, today), fh, ensure_ascii=False)

    exit_key, skips, evals = learn(by_day, days) if len(days) >= MIN_HISTORY else ("base", [], {})
    lessons = {"market": m["label"], "sessions": len(days), "first": days[0], "last": days[-1],
               "rebuild_match": [hit, len(match)], "learned_rule": rule_text(m, exit_key, skips),
               "learned_exit": exit_key, "learned_skips": skips,
               "evaluations": evals, "generated_at": ft.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
    with open(os.path.join(out_dir, "lessons.json"), "w") as fh:
        json.dump(lessons, fh, indent=1)
    with open(os.path.join(out_dir, "loss_review.json"), "w") as fh:
        json.dump(review, fh, indent=1)
    with open(os.path.join(out_dir, "sheet_loss_review.json"), "w") as fh:
        json.dump(sheet_rows_review(m, review), fh, ensure_ascii=False)
    with open(os.path.join(out_dir, "sheet_lessons.json"), "w") as fh:
        json.dump(sheet_rows_lessons(m, by_day, days, fwd_trades, (exit_key, skips), log, v2_log), fh, ensure_ascii=False)
    print("%s: %d losses reviewed; learned rule now: %s" % (m["label"], len(review), rule_text(m, exit_key, skips)),
          flush=True)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True, help="folder holding the trader-* skill folders")
    ap.add_argument("--market", choices=["us", "in", "both"], default="both")
    ap.add_argument("--us-stocks", type=int, default=900, help="most traded US stocks to load")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--pause", type=float, default=0.3)
    ap.add_argument("--chart-minutes", type=float, default=40)
    ap.add_argument("--rebuild", action="store_true", help="recompute stored sessions that are still in Yahoo's 60 days")
    args = ap.parse_args(argv)
    rc = 0
    for key in (["us", "in"] if args.market == "both" else [args.market]):
        try:
            rc |= run_market(key, args)
        except Exception as e:  # noqa: BLE001 - one market failing should not stop the other
            print("%s failed: %s" % (key, e), file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
