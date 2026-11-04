"""Published intraday strategies, simulated on 5-minute bars for the strategy race.

Simulation only: nothing here places, stages or drafts an order.

Every function takes one session's bars, as {bar index: {"o","h","l","c","v"}} with bar 0 the first
5 minutes after the open, and returns a list of trades: dicts with symbol, side ("long"/"short"),
entry, exit, entry_time, exit_time, shares, pnl and outcome. Prices are filled at bar closes or at a
stop/trigger level, the same way the forward test fills; costs are not deducted.

Sources (rules as published; adaptations noted in each docstring):
  Zarattini, Barbon & Aziz (2024), "A Profitable Day Trading Strategy For The U.S. Equity Market",
    Swiss Finance Institute Research Paper 24-98.
  Zarattini & Aziz (2023), "Volume Weighted Average Price (VWAP): The Holy Grail for Day Trading Systems".
  Gao, Han, Li & Zhou (2018), "Market intraday momentum", Journal of Financial Economics 129(2).
  Zarattini, Aziz & Barbon (2024), "Beat the Market: An Effective Intraday Momentum Strategy for
    S&P500 ETF (SPY)", Swiss Finance Institute Research Paper 24-97.
"""

import math


def _clock(open_min, i):
    t = open_min + 5 * (i + 1)
    return "%02d:%02d" % (t // 60, t % 60)


def session_ohlcv(D):
    idx = sorted(D)
    return {"o": D[idx[0]]["o"], "h": max(D[i]["h"] for i in idx), "l": min(D[i]["l"] for i in idx),
            "c": D[idx[-1]]["c"], "v": sum(D[i]["v"] for i in idx)}


def prior_stats(days, iso, n=14):
    """ATR, average daily volume, average first-bar volume and previous close over the n sessions
    before iso. None if fewer than n sessions."""
    prev = sorted(d for d in days if d < iso and days[d])
    if len(prev) < n + 1:
        return None
    daily = [session_ohlcv(days[d]) for d in prev[-(n + 1):]]
    trs = [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])) for a, b in zip(daily, daily[1:])]
    first = [days[d][0]["v"] for d in prev[-n:] if 0 in days[d]]
    return {"atr": sum(trs) / len(trs), "avg_vol": sum(b["v"] for b in daily[1:]) / n,
            "avg_first_vol": (sum(first) / len(first)) if first else 0, "prev_close": daily[-1]["c"]}


# ---------------------------------------------------------------- 1. Stocks in Play ORB

def stocks_in_play_picks(charts, iso, min_price, min_avg_vol, min_atr, names, one_per_company, top=3):
    """Zarattini, Barbon & Aziz (2024): stocks opening above min_price, averaging min_avg_vol shares a
    day and an ATR(14) above min_atr, whose first-5-minute volume is at least their 14-day average for
    those minutes (relative volume >= 100%), ranked by relative volume. The paper trades the top 20;
    the race takes the top `top` so it risks the same as the current rule. -> [(rel_vol, sym, D, stats)]"""
    rows = []
    for sym, days in charts.items():
        D = days.get(iso)
        if not D or 0 not in D:
            continue
        st = prior_stats(days, iso)
        if st is None or not st["avg_first_vol"]:
            continue
        b0 = D[0]
        if b0["o"] < min_price or st["avg_vol"] < min_avg_vol or st["atr"] < min_atr:
            continue
        rel = b0["v"] / st["avg_first_vol"]
        if rel < 1 or b0["c"] == b0["o"]:
            continue
        rows.append((rel, sym, D, st))
    rows.sort(key=lambda r: -r[0])
    return one_per_company(rows, lambda r: r[1], names, top)


def stocks_in_play_trade(sym, D, st, last, account, max_loss, open_min):
    """Buy stop at the first 5-minute bar's high if that bar closed up, sell stop at its low if it
    closed down; stop 10% of ATR from the fill; otherwise held to the flat time. Sized to lose
    max_loss at the stop, capped at the account. In the bar that triggers the entry, the stop counts
    only if that bar also closes beyond it (5-minute bars cannot show which came first)."""
    b0 = D[0]
    side = 1 if b0["c"] > b0["o"] else -1
    trigger = b0["h"] if side > 0 else b0["l"]
    dist = 0.1 * st["atr"]
    for i in range(1, last + 1):
        b = D.get(i)
        if b is None:
            return None
        hit = b["h"] >= trigger if side > 0 else b["l"] <= trigger
        if not hit:
            continue
        fill = max(trigger, b["o"]) if side > 0 else min(trigger, b["o"])
        stop = fill - side * dist
        shares = min(int(math.floor(max_loss / dist + 1e-9)), int(math.floor(account / fill)))
        trade = {"symbol": sym, "side": "long" if side > 0 else "short", "entry": round(fill, 2),
                 "entry_time": _clock(open_min, i), "shares": shares}
        if (b["c"] <= stop) if side > 0 else (b["c"] >= stop):
            return dict(trade, exit=round(stop, 2), exit_time=_clock(open_min, i), outcome="stop",
                        pnl=round(shares * side * (stop - fill), 2))
        for j in range(i + 1, last + 1):
            bj = D.get(j)
            if bj is None:
                return None
            if (bj["l"] <= stop) if side > 0 else (bj["h"] >= stop):
                return dict(trade, exit=round(stop, 2), exit_time=_clock(open_min, j), outcome="stop",
                            pnl=round(shares * side * (stop - fill), 2))
        px = D[last]["c"]
        return dict(trade, exit=round(px, 2), exit_time=_clock(open_min, last), outcome="closed at bell",
                    pnl=round(shares * side * (px - fill), 2))
    return {"symbol": sym, "side": "long" if side > 0 else "short", "outcome": "not triggered", "pnl": 0.0,
            "shares": 0}


# ---------------------------------------------------------------- 2. VWAP trend

def vwap_trend(sym, D, last, account, open_min):
    """Zarattini & Aziz (2023): long while price is above the session VWAP, short while below,
    flipping at each cross, flat at the close. Checked on 5-minute closes (the paper uses 1-minute
    bars); each position is the whole account."""
    cum_pv = cum_v = 0.0
    pos, shares, entry, entry_i = 0, 0, None, None
    trades = []
    for i in range(0, last + 1):
        b = D.get(i)
        if b is None:
            return None
        cum_pv += (b["h"] + b["l"] + b["c"]) / 3 * b["v"]
        cum_v += b["v"]
        vwap = cum_pv / cum_v if cum_v else b["c"]
        want = pos if i == last else (1 if b["c"] > vwap else -1 if b["c"] < vwap else pos)
        if pos and (want != pos or i == last):
            trades.append({"symbol": sym, "side": "long" if pos > 0 else "short", "entry": round(entry, 2),
                           "exit": round(b["c"], 2), "entry_time": _clock(open_min, entry_i),
                           "exit_time": _clock(open_min, i), "shares": shares,
                           "outcome": "closed at bell" if i == last else "VWAP cross",
                           "pnl": round(shares * pos * (b["c"] - entry), 2)})
            pos = 0
        if i < last and want and want != pos:
            pos, entry, entry_i = want, b["c"], i
            shares = int(math.floor(account / entry))
    return trades


# ---------------------------------------------------------------- 3. Intraday momentum

def intraday_momentum(sym, D, prev_close, signal_bar, entry_bar, last, account, open_min):
    """Gao, Han, Li & Zhou (2018): the return from the previous close to 30 minutes after the open
    sets the direction of the last half hour, bought or sold 30 minutes before the close and held to
    the close. Whole account."""
    if prev_close is None or any(D.get(i) is None for i in (signal_bar, entry_bar, last)):
        return None
    r1 = D[signal_bar]["c"] / prev_close - 1
    if r1 == 0:
        return []
    side = 1 if r1 > 0 else -1
    entry, px = D[entry_bar]["c"], D[last]["c"]
    shares = int(math.floor(account / entry))
    return [{"symbol": sym, "side": "long" if side > 0 else "short", "entry": round(entry, 2), "exit": round(px, 2),
             "entry_time": _clock(open_min, entry_bar), "exit_time": _clock(open_min, last), "shares": shares,
             "outcome": "closed at bell", "pnl": round(shares * side * (px - entry), 2),
             "signal": round(r1 * 100, 2)}]


# ---------------------------------------------------------------- 4. Noise-area momentum

def noise_area(sym, days, iso, last, account, open_min, lookback=14, every=6):
    """Zarattini, Aziz & Barbon (2024): the 'noise area' around today's open is the average absolute
    move from the open at the same time of day over the last 14 sessions, widened by an overnight
    gap. Every half hour, go long above the upper edge or short below the lower edge; exit at the
    next check when price is back past the edge or the VWAP (whichever is tighter); flat at the
    close. Whole account (the paper's volatility-targeted sizing is left out)."""
    D = days.get(iso)
    prev = sorted(d for d in days if d < iso and days[d])
    if not D or len(prev) < lookback or 0 not in D:
        return None
    hist = [days[d] for d in prev[-lookback:]]
    pd = days[prev[-1]]
    prev_close = pd[max(pd)]["c"]
    o = D[0]["o"]
    cum_pv = cum_v = 0.0
    pos, shares, entry, entry_i = 0, 0, None, None
    trades = []

    def close(i, px, why):
        trades.append({"symbol": sym, "side": "long" if pos > 0 else "short", "entry": round(entry, 2),
                       "exit": round(px, 2), "entry_time": _clock(open_min, entry_i), "exit_time": _clock(open_min, i),
                       "shares": shares, "outcome": why, "pnl": round(shares * pos * (px - entry), 2)})
    for i in range(0, last + 1):
        b = D.get(i)
        if b is None:
            return None
        cum_pv += (b["h"] + b["l"] + b["c"]) / 3 * b["v"]
        cum_v += b["v"]
        vwap = cum_pv / cum_v if cum_v else b["c"]
        if i == last:
            if pos:
                close(i, b["c"], "closed at bell")
            break
        if (i + 1) % every or i < every - 1:
            continue
        moves = [abs(h[i]["c"] / h[0]["o"] - 1) for h in hist if i in h and 0 in h]
        if len(moves) < lookback // 2:
            continue
        sigma = sum(moves) / len(moves)
        ub, lb = max(o, prev_close) * (1 + sigma), min(o, prev_close) * (1 - sigma)
        c = b["c"]
        if pos > 0 and c < max(ub, vwap):
            close(i, c, "back inside")
            pos = 0
        elif pos < 0 and c > min(lb, vwap):
            close(i, c, "back inside")
            pos = 0
        if pos == 0 and (c > ub or c < lb):
            pos, entry, entry_i = (1 if c > ub else -1), c, i
            shares = int(math.floor(account / c))
    return trades
