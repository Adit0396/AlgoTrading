"""Rule v3: the forward test trades the learned rule. Simulation only: nothing here places an order.

research.py (the loss review) keeps a walk-forward "learned rule": rule v2 plus the changes that were
proven on the sessions before each day. Under v3 the forward tests read that rule before each session
and apply it to that session's picks:

  - skips: a candidate that one of the learned rule's mistakes flags at the scan is passed over and the
    next one by money traded takes its place (still one per company, still 3 a day);
  - exit: the learned exit or entry change (target 0.5R / 1.5R / 2R, stop at the range low, breakeven
    after +0.5R, exit at 12:00, buy the pullback, wait 15 minutes) replaces v2's, using the same trade
    code the review scores it with.

Which rule a session gets, using only sessions before it:
  1. review/learned.csv, if the review has already logged that date (a re-run of an old day gets the
     rule it had then);
  2. otherwise review/lessons.json, if its newest session is before the date (the normal case: the
     review ran after the previous session);
  3. otherwise rule v2 unchanged ("Current rule").
While nothing is proven the learned rule is "Current rule", and v3 picks and trades exactly like v2.
"""

import csv
import json
import os

import research as rs

CURRENT = "Current rule"


def rule_for(state, iso):
    """-> (exit_key, [skip keys], rule text, where it came from) for session iso (YYYY-MM-DD)."""
    review = os.path.join(state, "review")
    found = None
    path = os.path.join(review, "learned.csv")
    if os.path.exists(path):
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("date") == iso and r.get("rule") not in (None, "", "no setup"):
                    found = (r.get("exit") or "base", (r.get("skips") or "").split("|"), r["rule"], "learned.csv")
    if found is None:
        path = os.path.join(review, "lessons.json")
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    lessons = json.load(fh)
            except ValueError:
                lessons = {}
            if lessons.get("last") and lessons["last"] < iso:
                found = (lessons.get("learned_exit") or "base", lessons.get("learned_skips") or [],
                         lessons.get("learned_rule") or CURRENT, "lessons.json (sessions to %s)" % lessons["last"])
    if found is None:
        return "base", [], CURRENT, "no review yet"
    exit_key, skips, text, src = found
    if exit_key not in rs.EXIT_NAME:
        exit_key = "base"
    skips = [s for s in skips if s in rs.MISTAKE]
    return exit_key, skips, text, src


def market(key, account, max_loss):
    """research.py's market settings with the forward test's own account and max loss."""
    m = dict(rs.MARKETS[key])
    m["account"], m["max_loss"] = float(account), float(max_loss)
    return m


def flagged(m, sym, days, iso, D, k, oh, ol, dv, index_days, skips):
    """The skip mistakes that flag this candidate at the scan -> [mistake names]."""
    if not skips:
        return []
    first = next((i for i in range(rs.N_RANGE, k + 1) if D.get(i) and D[i]["c"] > oh), k)
    f = rs.features(m, sym, days, iso, D, k, oh, ol, first, dv, index_days)
    return [rs.MISTAKE[s][0] for s in skips if rs.MISTAKE[s][2](f)]


def choose(ordered, flags_of, top):
    """The first `top` of `ordered` that no skip flags -> (chosen, [(candidate, [flags])] passed over)."""
    chosen, passed = [], []
    for c in ordered:
        fl = flags_of(c)
        if fl:
            passed.append((c, fl))
            continue
        chosen.append(c)
        if len(chosen) == top:
            break
    return chosen, passed


def learned_trade(m, tm, D, k, last, oh, ol, exit_key):
    """The trade the learned exit makes -> research.variants result dict, or None if bars are missing."""
    v, _, _, _ = rs.variants(m, tm, {"D": D, "k": k, "oh": oh, "ol": ol}, last)
    return v.get(exit_key)


def outcome_label(v, flat_label):
    """research.py's outcome words -> the forward test's ('flat at 3:55' or 'flat at 3:15' for the bell)."""
    oc = v["outcome"]
    return {"closed at bell": flat_label, "time exit": "exit at 12:00", "skipped": "not confirmed"}.get(oc, oc)


def apply_trade(row, v, tm, flat_label):
    """Fill a forward-test row from the learned exit's trade (entry, stop, target, size, outcome, P&L)."""
    if v.get("entry") is None:                       # pullback never filled, or not still above the range
        row["actual_outcome"] = outcome_label(v, flat_label)
        row["note"] = add_note(row.get("note"), "no trade: %s" % (
            "no pullback to the range high by 11:00" if v["outcome"] == "no fill"
            else "back inside the range 15 minutes after the scan"))
        return row
    entry, stop, target, sh = v["entry"], v["stop"], v["target"], v["shares"]
    risk = round(entry - stop, 2)
    row.update(entry_time=tm.clock12(tm.OPEN_MIN + 5 * (v["entry_bar"] + 1)), entry=entry, stop=stop,
               target=target, risk_per_share=risk, shares=sh, position_usd=round(sh * entry, 2),
               max_loss_usd=round(sh * risk, 2), actual_outcome=outcome_label(v, flat_label), exit_time=v["exit"],
               exit_price=v["exit_price"], r_multiple=v["r"], pnl_usd=v["pnl"])
    return row


def add_note(note, text):
    return (note + "; " + text) if note else text


def rule_note(text, passed, names):
    """The note on a session's rows saying which learned rule was used and what it passed over."""
    parts = []
    if text != CURRENT:
        parts.append("learned rule: " + text)
    if passed:
        parts.append("passed over " + ", ".join("%s (%s)" % (sym, ", ".join(f.lower() for f in fl))
                                                 for sym, fl in passed))
    return "; ".join(parts)
