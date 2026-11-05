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
3. **Pick.** The 3 with the most dollars traded from 9:30 to 10:15, one per company (when both
   GOOGL and GOOG qualify, only the more traded one is taken: two share classes are the same bet).
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

## Loss review, lessons and strategy race

`research.py` runs every weekday as soon as the US forward test finishes (`.github/workflows/research.yml`;
India's run is earlier the same day), or from the Actions tab with **Loss review and strategy race**,
**Run workflow**. For each market it loads 60 days of 5-minute bars (US: the ~900
most traded stocks plus SPY and QQQ; India: the Nifty 500 plus the Nifty 50 index and NIFTYBEES)
and:

1. **Rebuilds rule v2 for every session in those 60 days** and stores each day's top 12
   candidates in `review/candidates.csv`, so the history keeps growing past Yahoo's 60 days.
   The log prints how many forward-test picks the rebuild matches.
2. **Reviews every losing forward-test trade**: which "mistakes" were visible at entry (market
   falling, chased more than 0.5% above the range, stop wider than 1.5% of the price, gap up over
   2%, breakout in the last 10 minutes, already fading by more than half the risk, ordinary
   volume), what happened after entry, and which of 8 changes would have made it a win
   (targets of 0.5R, 1.5R or 2R, stop at the range low, breakeven after +0.5R, exit at noon,
   buy the pullback to the range high, wait 15 minutes).
3. **Scores every mistake and every change over all stored sessions.** A change is "proven"
   only if it beats the current rule in both the earlier and the later half of the history
   and its day-by-day gain has a t-statistic of 2.5 or more. About 15 changes are tested at
   once, so a weaker test would "prove" something by luck most weeks. The thresholds were
   fixed in advance, not tuned to the losses.
4. **Keeps a learned rule**: the current rule plus the changes proven on the sessions *before*
   each day, used on that day only. Its record in `review/learned.csv` is walk-forward, with no
   hindsight. The forward test's own rule (v2) is not changed, so its record stays clean.
5. **Races strategies** on the same days, data and account, logging each one's daily P&L in
   `review/race.csv`:

| Strategy | Rules | Source |
| --- | --- | --- |
| Current rule | 15-min ORB, top 3 by money traded at the scan, long only | this repo, rule v2 |
| Learned rule | current rule + proven changes (walk-forward) | this repo |
| Stocks in Play ORB | first 5-min bar's direction, stop order at its high/low, stop 10% of ATR(14), held to the close; stocks with relative volume of 100%+ (top 3 here, top 20 in the paper), price > $5, 1M+ shares a day, ATR > $0.50. India: ₹50, 500k shares, ATR > ₹10, and a 15-minute range with relative volume from 9:20 to 9:30, because Yahoo often reports no volume for the NSE's 9:15 bar | Zarattini, Barbon & Aziz (2024), SFI Research Paper 24-98 |
| VWAP trend | long above the session VWAP, short below, on QQQ (India: NIFTYBEES), checked on 5-minute closes | Zarattini & Aziz (2023) |
| Intraday momentum | the previous close to the first half hour's return sets the direction of the last half hour on SPY (India: NIFTYBEES) | Gao, Han, Li & Zhou (2018), Journal of Financial Economics 129(2) |
| Noise-area momentum | SPY (India: NIFTYBEES) breaking out of its usual 14-day move from the open, checked every 30 minutes, exit back inside the band or VWAP | Zarattini, Aziz & Barbon (2024), SFI Research Paper 24-97 |

Stock strategies risk $50 (₹5,000) a trade, up to 3 a day; index-ETF strategies put the whole
$5,000 (₹5,00,000) account in the ETF. Fills are at bar closes or stop/trigger levels, with no
costs. The papers use 1-minute bars, more stocks and leverage, so these are faithful to their
rules, not to their reported returns.

Outputs in `forward-test-data/review/` and `forward-test-data-india/review/`: `loss_review.json`,
`lessons.json`, `learned.csv`, `race.csv`, `candidates.csv`, and the Google Sheet rows
`sheet_loss_review.json`, `sheet_lessons.json` and `sheet_race.json` (tabs US/India Loss review,
Lessons and Strategy race).

Each session is scored once and then kept as stored (`rebuild` in the workflow redoes them).
That matters for India: Yahoo returns slightly different NSE volumes from one download to the
next, which can change a day's third pick or the VWAP, so rescoring old days would make the
record drift. US data has been stable between downloads.

Tests: `python -m unittest discover -s tests`.

## Cross-market arbitrage tracker

`arbitrage.py` (`.github/workflows/arbitrage.yml`, weekdays 21:47 UTC after the New York close)
follows seven companies listed in two markets: Infosys, Wipro, HDFC Bank, ICICI Bank and
Dr. Reddy's (NSE vs their New York shares) and BHP and ResMed (ASX vs New York). Each day it prices
the local shares in US$ (local close x shares per US share / exchange rate) and measures the US
share's premium over that. Shares per US share: Infosys, Wipro, Dr. Reddy's 1; ICICI Bank 2;
HDFC Bank 3; BHP 2; ResMed 10 ASX units.

The simulated trade: when the premium is 2 standard deviations from its 60-day average, sell the
expensive side and buy the cheap one (US$5,000 each, at each market's next close), and close when
it is back within 0.5, after 20 trading days, or if it widens 2 more (stop). A one-day jump of
more than 20 points is treated as a corporate action: the history restarts and an open trade closes
at the last clean prices. Costs: 5 bps a side in the US, 15 bps on the NSE, 10 bps on the ASX,
10 bps currency conversion, and 3% a year to carry the short leg.

Results go to `arbitrage-data/` (`trades.csv`, `summary.json`, `sheet_arbitrage.json` for the
sheet's Arbitrage tab): monthly P&L in A$ per pair, per market pair and in total, against an
Indian arbitrage mutual fund at 6.55% a year on the same US$35,000. Trades entered before
5 Nov 2026 are a backtest; later ones are tracked live.

This measures how often the gaps stray and come back; it is not money that could be collected.
Indian rules restrict moving shares between the NSE and US listings, NRIs can't short NSE shares
(that means stock futures, in large lots), shorting needs borrowable stock, and the markets'
closes are hours apart.

## Where the results go

| Place | What |
| --- | --- |
| `forward-test-data/results.csv` | One row per pick with its company name, or one "no setup" row for a day without a breakout |
| `forward-test-data/sheet_rows.json` | Every pick in the column order of the Google Sheet's US Picks tab |
| `forward-test-data/daily.csv` | One line per session: the picks with company names and outcomes, counts, day P&L, running total |
| `forward-test-data/daily_rows.json` | The same lines in the column order of the sheet's US Daily tab |
| `forward-test-data/last_run.json` | What the latest run did |
| `forward-test-data/news.json` | The headlines found before each pick's entry, by date and ticker |
| `forward-test-data/dashboard.json` | Everything a website needs in one file: settings, totals, P&L by session, every pick with its company name and news |
| `forward-test-data-india/` | The same files for the India test, feeding the sheet's India Picks and India Daily tabs |
| Google Sheet "Day Trader Forward Test" | US Picks, US Daily, US Summary, India Picks, India Daily, India Summary. A Claude scheduled task copies new rows from each market's `sheet_rows.json` and `daily_rows.json` at 1:41 PM Brisbane on weekdays |
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
