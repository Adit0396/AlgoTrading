#!/usr/bin/env python3
"""
Momentum Auto-Trader — hosted dashboard (Render + MongoDB Atlas)
==================================================================
Receives trade/scan events pushed from momentum_autotrader.py (running on
your own machine, next to IB Gateway) over HTTPS, stores them in MongoDB
Atlas (free M0 tier — no 30-day expiry, unlike Render's free Postgres),
and serves a password-gated status page.

Env vars (set these in the Render dashboard, never in code):
  MONGODB_URI    - your Atlas connection string, e.g.
                    mongodb+srv://user:pass@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
  API_KEY        - shared secret the bot uses to push events (X-API-Key header)
  DASH_PASSWORD  - password for viewing the dashboard in a browser
  FLASK_SECRET   - random string for session cookie signing (optional, auto-generated if unset)
"""

import os
import datetime as dt
from functools import wraps

from flask import Flask, request, Response, session, redirect, url_for
from pymongo import MongoClient, DESCENDING

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(24))

API_KEY = os.environ.get("API_KEY", "")
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")
MONGODB_URI = os.environ.get("MONGODB_URI", "")

_client = MongoClient(MONGODB_URI) if MONGODB_URI else None
_db = _client["momentum_autotrader"] if _client is not None else None
_events = _db["events"] if _db is not None else None


# --- Ingestion (called by the bot) ------------------------------------------
@app.route("/api/events", methods=["POST"])
def ingest_event():
    if not API_KEY or request.headers.get("X-API-Key") != API_KEY:
        return Response("unauthorized", status=401)
    if _events is None:
        return Response("database not configured", status=500)
    body = request.get_json(force=True, silent=True) or {}
    body.setdefault("ts", dt.datetime.utcnow().isoformat(timespec="seconds"))
    _events.insert_one(body)
    return {"ok": True}


# --- Browser login gate -------------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if request.form.get("password") == DASH_PASSWORD and DASH_PASSWORD:
            session["authed"] = True
            return redirect(url_for("index"))
        error = "Wrong password."
    return f"""<!doctype html>
    <html><head><meta charset="utf-8"><title>Login</title>
    <style>
      body {{ font-family:-apple-system,sans-serif; background:#0f1115; color:#e6e6e6;
             display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
      form {{ background:#161a22; padding:32px; border-radius:12px; width:280px; }}
      input {{ width:100%; padding:10px; margin-top:12px; border-radius:6px; border:1px solid #333;
               background:#0f1115; color:white; box-sizing:border-box; }}
      button {{ width:100%; padding:10px; margin-top:16px; border-radius:6px; border:none;
                background:#2f7a4f; color:white; font-weight:600; }}
      .err {{ color:#e07070; font-size:13px; margin-top:8px; }}
    </style></head><body>
      <form method="post">
        <h2 style="margin-top:0;">Momentum Auto-Trader</h2>
        <input type="password" name="password" placeholder="Password" autofocus>
        <button type="submit">View dashboard</button>
        <div class="err">{error}</div>
      </form>
    </body></html>"""


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- Dashboard -----------------------------------------------------------------
@app.route("/")
@login_required
def index():
    if _events is None:
        return "Database not configured — set MONGODB_URI in Render env vars."

    entries = list(_events.find({"kind": "ENTRY"}).sort("ts", DESCENDING).limit(50))
    scans = list(_events.find({"kind": "SCAN_COMPLETE"}).sort("ts", DESCENDING).limit(20))
    refused = list(_events.find({"kind": "REFUSED"}).sort("ts", DESCENDING).limit(1))

    last_scan = scans[0] if scans else None
    status_color = "#2f7a4f" if last_scan else "#a8681e"
    status_text = "Running" if last_scan else "No activity logged yet"

    stale_warning = ""
    if last_scan:
        last_ts = dt.datetime.fromisoformat(last_scan["ts"])
        if dt.datetime.utcnow() - last_ts > dt.timedelta(minutes=15):
            stale_warning = (
                f'<div class="warn">No scan received in the last 15 minutes '
                f'(last: {last_ts.strftime("%Y-%m-%d %H:%M UTC")}). '
                f'Market may be closed, or the bot / Gateway on your machine may have stopped.</div>'
            )

    refused_html = ""
    if refused:
        r = refused[0]
        refused_html = (
            f'<div class="danger">Bot refused to run at {r["ts"]} — account "{r.get("account")}" '
            f'did not look like a paper account. Nothing was traded.</div>'
        )

    rows = ""
    for e in entries:
        rows += f"""<tr>
          <td>{e.get('ts')}</td>
          <td><b>{e.get('symbol')}</b></td><td>{e.get('qty')}</td>
          <td>${e.get('entry_price', 0):.2f}</td>
          <td>T1 {e.get('t1_qty')}@${e.get('t1_price')} · T2 {e.get('t2_qty')}@${e.get('t2_price')} · T3 {e.get('t3_qty')}@${e.get('t3_price')}</td>
          <td>${e.get('stop_price')}</td></tr>"""
    if not rows:
        rows = '<tr><td colspan="6" style="text-align:center;color:#888;padding:20px;">No entries yet</td></tr>'

    scan_rows = ""
    for e in scans:
        q = ", ".join(e.get("qualified", [])) or "—"
        scan_rows += f"""<tr>
          <td>{e.get('ts')}</td><td>{e.get('scanned')}</td>
          <td>{q}</td><td>{e.get('errors')}</td>
          <td>{", ".join(e.get('open_positions', [])) or "—"}</td></tr>"""
    if not scan_rows:
        scan_rows = '<tr><td colspan="5" style="text-align:center;color:#888;padding:20px;">No scans logged yet</td></tr>'

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="30">
<title>Momentum Auto-Trader</title>
<style>
  body {{ font-family:-apple-system,sans-serif; background:#0f1115; color:#e6e6e6; margin:0; padding:24px; }}
  h1 {{ font-size:20px; margin-bottom:4px; }}
  .sub {{ color:#999; font-size:13px; margin-bottom:20px; }}
  .badge {{ display:inline-block; padding:4px 10px; border-radius:6px; color:white; font-size:13px; background:{status_color}; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:32px; font-size:13px; }}
  th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #262b36; }}
  th {{ color:#999; font-weight:600; text-transform:uppercase; font-size:11px; }}
  .card {{ background:#161a22; border-radius:10px; padding:16px 20px; margin-bottom:24px; }}
  .warn {{ background:#a8681e;color:white;padding:10px 14px;border-radius:8px;margin-bottom:16px; }}
  .danger {{ background:#b23b3b;color:white;padding:10px 14px;border-radius:8px;margin-bottom:16px; }}
  a {{ color:#8fb8ff; }}
</style></head><body>
  <h1>Momentum Auto-Trader — Paper Account</h1>
  <div class="sub">Auto-refreshes every 30s · <a href="/logout">log out</a></div>
  <span class="badge">{status_text}</span>
  <div style="margin-top:20px"></div>
  {stale_warning}{refused_html}
  <div class="card"><h2 style="font-size:15px;margin-top:0;">Entries (last 50)</h2>
    <table><tr><th>Time</th><th>Symbol</th><th>Qty</th><th>Entry</th><th>Exit ladder</th><th>Stop</th></tr>{rows}</table>
  </div>
  <div class="card"><h2 style="font-size:15px;margin-top:0;">Scan history (last 20)</h2>
    <table><tr><th>Time</th><th>Scanned</th><th>Qualified</th><th>Errors</th><th>Open positions</th></tr>{scan_rows}</table>
  </div>
</body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8787)))
