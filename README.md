# AlgoTrading: ORB forward test (US and India)

A daily forward test of the [day-trader skill pack](https://github.com/Jakeschincariol/day-trader-skill)
for Claude. **Simulation only:** nothing in this repo places, stages or drafts an order.

The point is to see, before risking money or even paper money, whether the pack's
opening-range breakout setup and its Time Machine odds hold up on real sessions.

## What it does each weekday

After the US close, GitHub Actions runs `forward_test.py` for every one of the last 5 sessions
that isn't logged yet:

1. **Scan at 10:15 AM New York.** trader-scanner finds every stock whose 5-minute close broke above
   its opening range (the high of 9:30 to 9:45), with a price of at least $5 and at least 200,000
   shares traded by 10:15.
2. **Keep the ones still breaking out.** Only stocks whose 10:15 price is still above the range high.
3. **Pick.** The 3 with the most dollars traded from 9:30 to 10:15.
4. **Plan.** Entry at the 10:15 price, stop at the opening-range midpoint, target 1R above the
   entry (the same distance as the stop is below), flat by 3:55 PM.
5. **Time Machine.** trader-timemachine replays the rest of the day 10,000 times from the stock's
   own prior sessions (seed 19) and re-runs the rule on each of those sessions.
6. **News.** trader-news collects the Yahoo Finance headlines for each pick published in the
   18 hours before 10:15, tagged (earnings, analyst, deal, legal, macro...). Add a repo secret
   `SEC_USER_AGENT` set to `Your Name you@example.com` to include SEC 8-K filings as well.
7. **Score.** The real 5-minute bars after 10:15 decide what happened: stop first, target first, or
   the price at 3:55 PM. A bar that touches both counts as the stop, the same rule the Time Machine uses.
8. **Size.** $5,000 account, $50 max loss per trade: shares = the smaller of
   floor(50 / risk per share) and floor(5,000 / entry). P&L = shares x (exit - entry).

Every step uses only what was known at 10:15, so each trade is one a person could have placed.

### Rule versions

- **v2 (current):** as above. 27 to 30 Oct 2026 were re-run under it; 26 Oct was already past
  Yahoo's 6-day scan window, so it has no v2 result.
- **v1 (26 to 30 Oct 2026):** bought each stock at the close of its own breakout bar, often 9:50 to
  10:05, but chose the stocks using the 10:15 ranking. That is look-ahead: nobody could have known
  at 9:50 which stocks would rank in the top 3 at 10:15. Those results are kept in
  `forward-test-data/archive/` and are not counted.

## India (NSE)

`forward_test_india.py` runs the same method on India's NSE, moved to NSE hours (9:15 AM to
3:30 PM India time). `.github/workflows/forward-test-india.yml` runs it after each NSE close
(scheduled 10:43 UTC, 4:13 PM India, 8:43 PM Brisbane) and writes `forward-test-data-india/`.

| Step | US | India |
| --- | --- | --- |
| Stocks scanned | every US-listed stock (Nasdaq Trader list) | the Nifty 500 (NSE's list) |
| Opening range | 9:30 to 9:45 New York | 9:15 to 9:30 India |
| Decide and buy | 10:15 | 10:00 (also 45 minutes after the open) |
| Filters | price $5+, 200,000+ shares by 10:15 | price Rs 50+, 100,000+ shares by 10:00 |
| Ranked by | dollars traded since the open | rupee turnover since the open |
| Stop, target | range midpoint, 1R | range midpoint, 1R |
| Flat by | 3:55 PM | 3:15 PM (before brokers square off intraday positions) |
| Account, max loss | $5,000, $50 | Rs 5,00,000, Rs 5,000 |

The Time Machine for India is the skill pack's own `simulate` and `history_replay` code from
`trader-timemachine/timemachine.py`, loaded with the NSE session from 9:15 to 3:15 (72 five-minute
bars) in place of New York's 78. It stops at 3:15 because the test is out by then, and because
Yahoo leaves the 3:20 and 3:25 bars empty for many large NSE stocks, which would otherwise keep
their history out of the replays. The pack's scanner and chart tools only know US tickers and hours, so the India
script fetches each stock's 60 days of 5-minute bars from Yahoo (`SYMBOL.NS`) itself and scans
them. Because every chart holds 60 days, a backfill can cover the last 15 or so sessions at once.
News uses the pack's `news.py` with `.NS` symbols. Costs (brokerage, STT, exchange charges, GST,
stamp duty) are not included.

## Where the results go

| Place | What |
| --- | --- |
| `forward-test-data/results.csv` | One row per pick, or one "no setup" row for a day without a breakout |
| `forward-test-data/sheet_rows.json` | Every row in the column order of the Google Sheet's Picks tab |
| `forward-test-data/last_run.json` | What the latest run did |
| `forward-test-data/news.json` | The headlines found before each pick's entry, by date and ticker |
| `forward-test-data/dashboard.json` | Everything a website needs in one file: settings, totals, P&L by session, every pick with its company name and news |
| `forward-test-data-india/` | The same files for the India test (`results.csv`, `news.json`, `dashboard.json`, `universe.csv`) |
| Google Sheet "Day Trader Forward Test" | Picks tab (raw log) and Summary tab (formulas). A Claude scheduled task copies new rows from `sheet_rows.json` each morning |
| Claude artifact "ORB Forward Test" | A dashboard that reads the Google Sheet |
| Trading tab on personal-dashboard-ebc.pages.dev | Reads `dashboard.json` from this repo on every page load |

## Showing it on a website

`dashboard.json` is public and can be fetched straight from the browser (GitHub serves raw files
with open CORS):

```js
const res = await fetch("https://raw.githubusercontent.com/Adit0396/AlgoTrading/main/forward-test-data/dashboard.json", { cache: "no-store" });
const data = await res.json();   // data.totals, data.daily, data.picks
```

GitHub caches raw files for up to about 5 minutes, so a new run can take that long to show.

## Schedule

`.github/workflows/forward-test.yml` is scheduled for 21:23 UTC Monday to Friday: 7:23 AM
Brisbane, Tuesday to Saturday, after the US close in both daylight and standard time. GitHub often
starts scheduled runs 2 to 4 hours late; each run fills in any of the last 5 sessions that are
missing, so a late or skipped run is picked up by the next one. Each run commits new results back
to this repo, and the Claude sync task copies them into the Google Sheet at 1:41 PM Brisbane.

To run it now: Actions tab, **ORB forward test**, **Run workflow**. Leave the mode as `catch-up`,
type `backfill` (or `backfill 3`) to redo the last 5 (or 3) sessions, or type one date such as `2026-10-30`.

## Run it on your own computer

```
git clone https://github.com/Adit0396/AlgoTrading.git
cd AlgoTrading
git clone https://github.com/Jakeschincariol/day-trader-skill.git
python3 forward_test.py --repo day-trader-skill --state forward-test-data --catch-up
```

Other modes: `--last` (latest session), `--date 2026-10-30`, `--backfill 5`.
Options: `--account 5000 --max-loss 50 --top 3 --min-price 5 --min-volume 200000 --target-r 1`.
If you change the account or max loss, change the inputs on the sheet's Summary tab too.

Needs Python 3.8 or newer and nothing to install. The workflow pins the skill pack to commit
`3d0f4ef`, so an update to the pack can't change the rules in the middle of the test.

## Reading the results

- At a 1R target, break-even means the target comes first in more than half of the trades
  that hit either side.
- Five sessions of three picks is 15 trades at most. That is enough to spot a broken setup,
  not enough to prove a good one.
- Compare the Time Machine's predicted "target first" with how often it really happened.
  If they stay far apart, the odds aren't telling you much.

## Limits

- Yahoo keeps only about 6 days of 5-minute data for the scan, so a session missed for longer
  than that can't be filled in.
- Yahoo's free endpoints are unofficial. They can lag, change, or rate-limit cloud servers.
- Yahoo's headline feed keeps only the latest items per ticker, so a busy stock's early-morning
  headlines may already be gone by the evening run, and older sessions in a backfill show few.
- Fills are modeled at the stop and target prices. Real fills can be worse, especially across
  a gap, and there are no commissions or slippage in the numbers.
- Most people who day trade lose money. This is a test harness, not financial advice.

## History

This repo previously held a momentum auto-trader for an IBKR paper account (IB Gateway in
Docker plus a Render dashboard). That code is in the git history up to commit `b5c2dae`.
