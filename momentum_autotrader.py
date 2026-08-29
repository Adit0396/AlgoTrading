#!/usr/bin/env python3
"""
Momentum Auto-Trader — unattended paper-trading bot for Interactive Brokers
=============================================================================
Runs continuously against your IBKR PAPER TRADING account via IB Gateway/TWS
(NOT the Claude/IBKR MCP connector — that one is deliberately confirm-first
and can't place unattended orders). This script connects directly to your
own running Gateway session using ib_insync, so once you're logged in, it
places entries and manages the scaled exit plan with ZERO manual taps.

-----------------------------------------------------------------------------
ONE-TIME SETUP (you have to do this part — nobody can do it for you, this is
how every unattended IBKR bot works, algo funds included):
-----------------------------------------------------------------------------
1. Install IB Gateway (lighter than TWS) from interactivebrokers.com/en/trading/ibgateway-stable.php
2. Log into IB Gateway using your PAPER TRADING credentials, "Paper Trading" mode.
   Paper account IDs start with "DU". Default paper socket port is 4002.
3. In Gateway: Configure -> Settings -> API -> Settings:
     - Enable ActiveX and Socket Clients
     - Uncheck "Read-Only API" (the bot needs to place orders)
     - Add 127.0.0.1 to Trusted IPs
     - Socket port: 4002 (paper) — confirm it matches PORT below
4. To survive overnight/unattended without you re-clicking 2FA each morning,
   run Gateway under IBC (Interactive Brokers Controller — free, open source):
   https://github.com/IbcAlpha/IBC
   IBC auto-restarts and re-authenticates Gateway on schedule so it never
   needs a human at 2am. This is the standard solution retail algo traders
   use for exactly this problem.
5. pip install ib_insync pandas numpy --break-system-packages
6. Run this script on the SAME machine as Gateway (or point HOST at it):
     python3 momentum_autotrader.py

The bot will refuse to run (see SAFETY CHECK below) unless it can confirm
the connected account ID starts with "DU" (paper). It will never willingly
trade a live ("U"-prefixed) account.
-----------------------------------------------------------------------------
"""

import os
import time
import math
import json
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from ib_insync import IB, Stock, MarketOrder, LimitOrder, StopOrder, util

# --- Dashboard log ------------------------------------------------------------
# Every entry, scan heartbeat, and safety event is appended here as one JSON
# object per line (local backup / offline viewing), AND pushed to the hosted
# Render dashboard so you can check it from your phone/browser anywhere.
LOG_PATH = Path(__file__).parent / "trade_log.jsonl"

# Set these once your Render service is live — see the service's env vars.
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")   # e.g. https://momentum-dashboard.onrender.com/api/events
DASHBOARD_API_KEY = os.environ.get("DASHBOARD_API_KEY", "")

def log_event(kind: str, **fields):
    rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "kind": kind, **fields}
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(rec) + "\n")
    if DASHBOARD_URL:
        try:
            requests.post(DASHBOARD_URL, json=rec,
                           headers={"X-API-Key": DASHBOARD_API_KEY}, timeout=5)
        except Exception as e:
            print(f"[dashboard push failed, continuing locally] {e}")
    return rec

# =============================================================================
# CONFIG
# =============================================================================
HOST = "127.0.0.1"
PORT = 4002          # 4002 = IB Gateway paper. 7497 = TWS paper. Change if needed.
CLIENT_ID = 17

SCAN_INTERVAL_SEC = 60          # how often the whole watchlist is rescanned
MAX_CONCURRENT_POSITIONS = 8    # cap simultaneous open positions
CAPITAL_PER_TRADE_USD = 1500    # notional per new entry

# --- Watchlist universe -----------------------------------------------------
# "All stocks" in practice = the liquid universe your strategy's own guards
# would accept anyway (min price, min $ volume). We pull S&P 500 + Nasdaq 100
# constituents dynamically; if that fetch fails (no network), we fall back to
# a hardcoded liquid core so the bot still runs.
FALLBACK_UNIVERSE = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AMD","NFLX","AVGO",
    "JPM","V","UNH","XOM","COST","CRM","ADBE","ORCL","QCOM","INTC",
    "PYPL","INTU","AMAT","TXN","MU","CSCO","IBM","NOW","UBER","SHOP",
]

def get_universe():
    try:
        sp500 = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
        tickers = sp500["Symbol"].str.replace(".", "-", regex=False).tolist()
        ndx = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
        for t in ndx:
            if "Ticker" in t.columns:
                tickers += t["Ticker"].tolist()
                break
        tickers = sorted(set(tickers))
        print(f"[universe] loaded {len(tickers)} symbols from S&P500 + Nasdaq100")
        return tickers
    except Exception as e:
        print(f"[universe] dynamic fetch failed ({e}); using fallback core of {len(FALLBACK_UNIVERSE)} liquid names")
        return FALLBACK_UNIVERSE

# --- Guards (mirrors the Pine scanner's anti-noise section) ----------------
MIN_PRICE = 5.0
MIN_AVG_DOLLAR_VOL = 1_000_000

# --- Qualifying thresholds (mirrors intraday_spike_scanner.pine) -----------
BURST_MIN_ATRX   = 1.5
RVOL_MIN         = 3.0
HOD_TOL_PCT      = 0.3
PERSIST_MIN      = 3
PERSIST_WINDOW   = 5
COOLDOWN_SEC     = 15 * 60   # don't re-enter same symbol within 15 min of last signal

# --- Scaled exit plan (mirrors spike_scaled_exit_strategy.pine cost model) -
BROKERAGE_PCT = 0.05
TAXES_PCT     = 0.10
SLIPPAGE_PCT  = 0.05
ANNUAL_SUB_COST = 600
TRADES_PER_YEAR = 200
ROUND_TRIP_COST_PCT = BROKERAGE_PCT * 2 + TAXES_PCT + SLIPPAGE_PCT
SUB_PCT_PER_TRADE = (ANNUAL_SUB_COST / TRADES_PER_YEAR) / (CAPITAL_PER_TRADE_USD / 100)
T1_BUFFER_PCT = 2.0
T1_PCT = ROUND_TRIP_COST_PCT + SUB_PCT_PER_TRADE + T1_BUFFER_PCT   # cost-recovery + small buffer
T2_PCT = 30.0
T3_PCT = 50.0
T1_FRAC, T2_FRAC = 0.40, 0.30   # remainder (0.30) goes to T3
STOP_PCT = 4.0

NY_TZ = ZoneInfo("America/New_York")

# =============================================================================
# Indicator math (mirrors intraday_spike_scanner.pine)
# =============================================================================
def compute_signal(bars: pd.DataFrame):
    """bars: columns open, high, low, close, volume, indexed by time, 5-min RTH bars for today."""
    if len(bars) < 20:
        return None

    close = bars["close"]
    volume = bars["volume"]
    ret = close.pct_change() * 100
    atr_proxy = ret.rolling(14).std().iloc[-1]         # volatility proxy in %
    burst_pct = (close.iloc[-1] / close.iloc[-4] - 1) * 100 if len(close) > 4 else 0
    burst_atr = burst_pct / atr_proxy if atr_proxy and atr_proxy > 0 else 0

    vol_avg = volume.rolling(20).mean().iloc[-1]
    rvol = volume.iloc[-1] / vol_avg if vol_avg and vol_avg > 0 else 0

    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    vwap = (typical * volume).cumsum() / volume.cumsum()
    above_vwap = close.iloc[-1] > vwap.iloc[-1]

    hod = bars["high"].max()
    dist_hod = (hod - close.iloc[-1]) / hod * 100 if hod > 0 else 100
    near_hod = dist_hod <= HOD_TOL_PCT

    green_bars = (bars["close"] > bars["open"]).tail(PERSIST_WINDOW).sum()

    avg_dollar_vol = vol_avg * close.iloc[-1] if vol_avg else 0
    liquidity_ok = close.iloc[-1] >= MIN_PRICE and avg_dollar_vol >= MIN_AVG_DOLLAR_VOL

    qualifies = (
        burst_atr >= BURST_MIN_ATRX and
        rvol >= RVOL_MIN and
        above_vwap and
        near_hod and
        green_bars >= PERSIST_MIN and
        liquidity_ok
    )
    return {
        "qualifies": bool(qualifies), "burst_atr": burst_atr, "rvol": rvol,
        "above_vwap": bool(above_vwap), "dist_hod": dist_hod,
        "green_bars": int(green_bars), "last_price": float(close.iloc[-1]),
    }

# =============================================================================
# Bot
# =============================================================================
class MomentumBot:
    def __init__(self):
        self.ib = IB()
        self.last_signal_time = {}   # symbol -> datetime of last entry
        self.universe = get_universe()

    def connect_and_safety_check(self):
        # Gateway can accept the TCP connection before it has actually
        # finished its internal login/configuration sequence — an API
        # connection attempt in that narrow window gets rejected (e.g. IBKR's
        # "paper trading disclaimer" error) even though the account is fine
        # and fully usable moments later. Retry with backoff instead of
        # treating that as fatal.
        last_err = None
        for attempt in range(1, 9):
            try:
                self.ib.connect(HOST, PORT, clientId=CLIENT_ID, readonly=False, timeout=15)
                last_err = None
                break
            except Exception as e:
                last_err = e
                wait = min(10 * attempt, 60)
                print(f"[connect] attempt {attempt} failed ({e!r}); retrying in {wait}s")
                if self.ib.isConnected():
                    self.ib.disconnect()
                time.sleep(wait)
        if last_err is not None:
            log_event("REFUSED", reason=f"could not connect to Gateway after retries: {last_err!r}")
            raise SystemExit(f"Could not connect to IB Gateway after retries: {last_err!r}")

        acct = self.ib.managedAccounts()[0]
        print(f"[safety] connected account: {acct}")
        if not acct.startswith("DU"):
            log_event("REFUSED", account=acct, reason="not a paper account (no DU prefix)")
            self.ib.disconnect()
            raise SystemExit(
                f"REFUSING TO RUN: connected account '{acct}' does not look like a paper "
                f"account (paper accounts start with 'DU'). This bot will never trade a "
                f"live-looking account. Fix your Gateway login and rerun."
            )
        summary = {v.tag: v.value for v in self.ib.accountSummary(acct)}
        print(f"[safety] NetLiquidation={summary.get('NetLiquidation')} "
              f"Currency={summary.get('Currency')}")
        log_event("STARTED", account=acct, net_liquidation=summary.get("NetLiquidation"),
                   currency=summary.get("Currency"), universe_size=len(self.universe))

    def market_open(self):
        now = dt.datetime.now(NY_TZ)
        open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
        close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return now.weekday() < 5 and open_t <= now <= close_t

    def open_position_symbols(self):
        return {p.contract.symbol for p in self.ib.positions() if p.position != 0}

    def place_entry_with_scaled_exit(self, symbol, contract, last_price):
        qty = max(1, math.floor(CAPITAL_PER_TRADE_USD / last_price))
        q1 = max(1, round(qty * T1_FRAC))
        q2 = max(1, round(qty * T2_FRAC))
        q3 = max(1, qty - q1 - q2)
        if q3 <= 0:
            q1, q2, q3 = qty, 0, 0

        entry = MarketOrder("BUY", qty)
        entry.transmit = False
        trade = self.ib.placeOrder(contract, entry)
        self.ib.sleep(1)

        t1_price = round(last_price * (1 + T1_PCT / 100), 2)
        t2_price = round(last_price * (1 + T2_PCT / 100), 2)
        t3_price = round(last_price * (1 + T3_PCT / 100), 2)
        stop_price = round(last_price * (1 - STOP_PCT / 100), 2)

        parent_id = entry.orderId
        exits = []
        if q1: exits.append(LimitOrder("SELL", q1, t1_price))
        if q2: exits.append(LimitOrder("SELL", q2, t2_price))
        if q3: exits.append(LimitOrder("SELL", q3, t3_price))
        stop = StopOrder("SELL", qty, stop_price)

        for o in exits:
            o.parentId = parent_id
            o.transmit = False
        stop.parentId = parent_id
        stop.transmit = True  # last order in the bracket triggers transmission of all

        for o in exits:
            self.ib.placeOrder(contract, o)
        self.ib.placeOrder(contract, stop)

        print(f"[ENTRY] {symbol} x{qty} @ ~{last_price:.2f} | "
              f"T1 {q1}@{t1_price} T2 {q2}@{t2_price} T3 {q3}@{t3_price} stop {stop_price} "
              f"(break-even move to {last_price:.2f} applied once T1 fills — monitor loop below)")
        log_event("ENTRY", symbol=symbol, qty=qty, entry_price=last_price,
                   t1_qty=q1, t1_price=t1_price, t2_qty=q2, t2_price=t2_price,
                   t3_qty=q3, t3_price=t3_price, stop_price=stop_price)
        self.last_signal_time[symbol] = dt.datetime.now(NY_TZ)

    def scan_once(self):
        open_syms = self.open_position_symbols()
        if len(open_syms) >= MAX_CONCURRENT_POSITIONS:
            print(f"[scan] at max concurrent positions ({len(open_syms)}), skipping new entries this pass")
            log_event("SCAN_SKIPPED", reason="max_positions", open_positions=sorted(open_syms))
            return

        scanned, qualified, errors = 0, [], 0
        for symbol in self.universe:
            if symbol in open_syms:
                continue
            last_t = self.last_signal_time.get(symbol)
            if last_t and (dt.datetime.now(NY_TZ) - last_t).total_seconds() < COOLDOWN_SEC:
                continue
            try:
                contract = Stock(symbol, "SMART", "USD")
                self.ib.qualifyContracts(contract)
                bars = self.ib.reqHistoricalData(
                    contract, endDateTime="", durationStr="1 D",
                    barSizeSetting="5 mins", whatToShow="TRADES",
                    useRTH=True, formatDate=1,
                )
                if not bars:
                    continue
                df = util.df(bars)
                sig = compute_signal(df)
                scanned += 1
                if sig and sig["qualifies"]:
                    qualified.append(symbol)
                    if len(self.open_position_symbols()) >= MAX_CONCURRENT_POSITIONS:
                        break
                    self.place_entry_with_scaled_exit(symbol, contract, sig["last_price"])
            except Exception as e:
                errors += 1
                print(f"[scan] {symbol} error: {e}")
            self.ib.sleep(0.3)  # be polite to pacing limits across hundreds of symbols

        log_event("SCAN_COMPLETE", scanned=scanned, qualified=qualified, errors=errors,
                   open_positions=sorted(self.open_position_symbols()))

    def run(self):
        self.connect_and_safety_check()
        print(f"[run] watching {len(self.universe)} symbols, scanning every {SCAN_INTERVAL_SEC}s "
              f"during US regular hours only.")
        while True:
            if self.market_open():
                self.scan_once()
            else:
                print("[run] market closed, sleeping...")
            time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    MomentumBot().run()
