# Plan: NSE research and backtesting platform

## Who this is for and what it should do

I'm a retail swing trader in India (₹36,000 capital, at most 3 open positions, no position larger than 33% of capital, delivery trades, holding days to weeks, no intraday). I want a local tool that:

1. Thoroughly audits and cleans my Bloomberg Nifty 500 dataset.
2. Keeps the dataset up to date automatically from NSE's daily bhavcopy files.
3. Computes a full library of technical indicators (MACD, DMAs, RSI, etc.) on all of it.
4. Lets me define my own strategies, from rules or Python, and backtest them interactively.
5. Scans the latest data each day and tells me which stocks currently meet a strategy's conditions, and why.

**Not in scope:** placing orders, intraday or tick data, options, anything that trades automatically.

## Starting point

There is an existing project, `nsebt` (from `nsebt.zip`), that already has:
- a bhavcopy downloader and parser (`bhavcopy.py`, `store.py`)
- Bloomberg-to-NSE symbol mapping with price-overlap validation (`mapping.py`)
- a point-in-time Nifty 500 universe (`universe.py`)
- a trade-level engine with a random-entry placebo test, and a monthly portfolio engine (`engine/`)
- two strategies (breakout, 12-1 momentum) and 14 passing tests

Read it first. Extend and refactor it rather than starting over, unless you find a concrete reason not to, and tell me what that reason is. Keep its timing rules: decisions use a day's close, fills happen at the next day's open, and there is no look-ahead.

## The data

The Bloomberg extract is a folder `DE/` (about 400 MB zipped). Known contents:

| Path | What it is |
|---|---|
| `DE/data/raw/prices/*.parquet` | 858 securities, daily, 2016-08-29 to 2026-08-27. Columns: `Date, Ticker, PX_OPEN, PX_HIGH, PX_LOW, PX_LAST, PX_VOLUME`. Ticker format: `RELIANCE IN Equity` |
| `DE/data/raw/prices/*.csv` | quality reports: `price_quality_report.csv`, `ohlc_missing_report.csv`, `volume_missing_summary.csv` |
| `DE/data/raw/prices/extraction_failed.parquet` | failed extractions (no Date or PX_LAST) |
| `DE/data/raw/constituents/` | point-in-time Nifty 500 membership: 20 snapshots 2016-09-01 to 2026-03-01 (`historical_membership_canonical_mapping.csv`), plus the current 500 (`security_master.parquet`, snapshot 2026-08-27) |
| `DE/data/raw/fundamentals/` | quarterly fundamentals 2020-01 to 2024-12 for about 856 securities, plus missingness and validity audits |
| `DE/data/processed/nifty500_historical_stock_features.parquet` | 390 MB precomputed feature file. Contents not yet inspected |
| `DE/*.ipynb` | extraction, validation and feature-pipeline notebooks |
| `DE/data/raw/project_data_inventory.csv` | inventory summary |
| `DE/data/raw/index`, `macro`, `corporate_actions` | empty folders |

Known problems:
- **Prices are not adjusted for splits or bonuses.** A 1:5 split looks like an 80% crash.
- **Bloomberg codes often differ from NSE symbols.** Only about 200 of the current 500 match by name (`MM` = `M&M`, `BANDHAN` = `BANDHANBNK`).
- 76 series end before 2026-08 (mergers and delistings). Keep them: they prevent survivorship bias.
- A few rows have a close but no open, high or low. Some volumes are missing.
- The data ends 2026-08-27 and must be extended with NSE files.
- macOS junk (`__MACOSX/`, `.DS_Store`, `._*`) is mixed into the zip. Ignore it.

## Phase 0: Audit the dataset (do this first, then stop and report)

"Thoroughly" means: open every file, not just the ones listed above.

1. Write a script that walks all of `DE/` and records, for every data file: path, format, size, row count, columns and dtypes, date range, number of tickers, and the null rate per column.
2. For the price files, check:
   - duplicate dates
   - OHLC consistency (low ≤ open/close ≤ high)
   - non-positive prices or volume
   - gaps longer than 5 trading days
   - single-day moves over 20%, each flagged as a likely split/bonus or a real move (use the ratio: 1/2, 1/5, 1/10 or 2× patterns suggest corporate actions)
   - whether every ticker in the membership files has a price file, and the reverse
3. Inspect the 390 MB features file. List every column, what it seems to be, its coverage, and whether it can be trusted (spot-check a few features by recomputing them from prices). Decide whether to reuse it or recompute.
4. Read the notebooks and summarise what each one did, especially any adjustment, cleaning or validation logic worth reusing.
5. Output: `reports/data_audit.md`, with a summary table, the list of problems found, and recommended fixes, plus the CSVs behind it.

**Checkpoint:** stop here and show me the audit before building on the data.

## Phase 1: Clean, canonical data layer

1. **One store for everything.** Daily OHLCV for every security from both sources, keyed by a stable security ID, stored as partitioned parquet. Use DuckDB for querying if it makes things simpler.
2. **Security master:** Bloomberg ID, NSE symbol, ISIN, company name, listing and delisting dates, sector or industry if available, plus a history of symbol changes.
   - Mapping order: ISIN first, then a manual overrides file, then a name match. Every match must be validated against prices on days both sources cover; keep the existing `validate_overlap` logic.
   - I will provide `bbg_isin.csv` (`Bloomberg_ID, ISIN`, pulled from the terminal via `ID_ISIN`) and NSE's `EQUITY_L.csv`. Until then, use what matches and report what doesn't.
3. **Corporate-action adjustment.**
   - Build an adjustment-factor table per security from NSE corporate-actions data (splits, bonuses; dividends optional).
   - Find the official NSE source for corporate actions. Don't hard-code URLs you haven't verified, and fall back to the split-detection heuristic from Phase 0 where data is missing.
   - Store raw prices and adjustment factors separately. Indicators and backtests use adjusted prices; trade fills and position sizing use raw prices on the day.
4. **Point-in-time universe:** keep the Nifty 500 membership logic. Also allow "all NSE equities" and custom watchlists as universes.
5. **Benchmark series:** Nifty 50 and Nifty 500 index levels if obtainable from NSE; otherwise an equal-weight member index, clearly labelled.

## Phase 2: Automatic NSE data pipeline

1. Daily download of `https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv`. Start from the existing `bhavcopy.py`.
   - Browser-like headers, retries with backoff, a pause between requests, and a raw-file cache so nothing is downloaded twice.
   - Detect when NSE returns an HTML or bot page with status 200, and treat it as a failure.
   - 404 means a holiday or not yet published. Use NSE's official trading-holiday list to tell these apart and report real gaps.
2. Keep all columns, including `AVG_PRICE`, `NO_OF_TRADES`, `DELIV_QTY` and `DELIV_PER`; delivery % is a useful feature.
3. **Backfill:** start the first run at 2026-08-20 so it overlaps the Bloomberg data; use the overlap to validate the mapping.
4. **Manual fallback:** an `ingest` command for files I download myself in a browser.
5. **Scheduling:** a single command I can run daily after about 7 pm IST, with instructions for Windows Task Scheduler and for cron.
6. **Run report** after each update: days fetched, holidays, failures, new symbols, symbols that disappeared, and any price that disagrees sharply with the previous close without a known corporate action.

## Phase 3: Indicator and feature library

All features must be vectorised, computed per security on adjusted prices, and use only data up to each row's date.

| Group | Features |
|---|---|
| Trend | SMA and EMA (5, 10, 20, 50, 100, 200 "DMA"), price vs each DMA, DMA crossovers (golden/death cross), slope of the 50 and 200 DMA |
| Momentum | MACD (12/26/9) line, signal and histogram, plus crossovers; RSI (14); Stochastic (14,3); Rate of Change; 1/3/6/12-month returns; 12-1 momentum |
| Volatility | ATR (14), Bollinger Bands (20, 2σ) and %B and bandwidth, historical volatility (20/60 day), Keltner channels |
| Trend strength | ADX/DMI (14), Aroon |
| Volume and delivery | volume vs its 20-day average, OBV, delivery % and its 20-day average, delivery-volume spikes, turnover in ₹ |
| Price levels | 52-week high and low, distance from each, N-day highs and lows, gap up and gap down |
| Relative | relative strength vs Nifty 500 and vs sector, cross-sectional rank of any feature on each date |

Requirements:
- **Parameterised:** `macd(fast=12, slow=26, signal=9)`, `sma(n)` and so on, so strategies can use any setting.
- **Checked against a reference:** compare results with `pandas-ta` or TA-Lib on sample stocks. Document any differences, such as how the first values are seeded.
- **No look-ahead test:** for every indicator, computing on data truncated at day t must give identical values up to t.
- **Caching:** cache features to disk keyed by parameters, and recompute only for new dates.

## Phase 4: Defining strategies

Two ways to write a strategy:

1. **Rule files (YAML)**, so I can write strategies without touching code:

```yaml
name: macd_trend_pullback
universe: nifty500
entry:
  all:
    - close > sma(200)
    - macd_cross_up(12, 26, 9)
    - rsi(14) < 60
    - volume > 1.5 * sma(volume, 20)
exit:
  any:
    - macd_cross_down(12, 26, 9)
    - close < sma(50)
stop_loss: 2 * atr(14)        # below entry
take_profit: 3 * atr(14)
max_hold_days: 20
sizing: risk_pct(1.0)         # risk 1% of equity per trade
max_positions: 5
```

   - Parse expressions with a safe parser. Never `eval`.
   - Support comparisons, `and`/`or`/`not`, arithmetic, crossovers, `shift(n)` for past values, `rank()` across stocks, and every Phase 3 feature with parameters.
   - Give clear errors that point to the bad line.
2. **Python classes** for anything the rule format can't express. Use the existing `signals(df)` and `scores(close, volume)` interfaces, extended as needed.

## Phase 5: Backtesting engine

1. **Realistic account, matching how I trade.** Defaults: ₹36,000 starting capital, **max 3 open positions**, **no position above 33% of current equity**. All configurable.
   - Whole shares only. A stock whose single share costs more than the position limit (e.g. MRF) can't be bought; record it as skipped.
   - Cash constraint: positions can't exceed available cash.
   - Position-sizing modes, each capped at the 33% limit: fixed ₹, % of equity, ATR-based risk.
   - When more than 3 stocks signal on the same day, the strategy must rank them (e.g. by signal strength or momentum), and the engine takes the top ones that fit. Log the rest as "signal skipped: no slot", so I can see how many opportunities were missed.
   - Skip a trade and record why when cash is short.
   - With only 3 positions, results depend heavily on which few trades happened. Every report must show the distribution of outcomes from bootstrapping or random tie-breaking among same-day signals, not just one equity curve.
2. **Indian cost model** for Zerodha equity delivery, computed in rupees per order. These rates come from Zerodha's charges page (Nov 2026). Keep each one as a config value:

   | Charge | Rate | Side |
   |---|---|---|
   | Brokerage | ₹0 | – |
   | STT | 0.1% of value | buy and sell |
   | Exchange transaction charge (NSE) | 0.00307% of value | buy and sell |
   | SEBI fee | ₹10 per crore (0.0001%) | buy and sell |
   | GST | 18% of (brokerage + exchange charge + SEBI fee) | buy and sell |
   | Stamp duty | 0.015% of value (₹1,500 per crore) | buy only |
   | DP charge | flat per stock per day it's sold. Not on the charges table above; check Zerodha's current amount, about ₹15–16 incl. GST | sell only |
   | Slippage | configurable, larger for illiquid stocks (scale it with turnover) | buy and sell |

   Percentage charges come to about 0.22% per round trip. The DP charge is ₹15.34 per stock per sell day (₹3.50 CDSL + ₹9.50 Zerodha + ₹2.34 GST, per Zerodha's charges page). On my typical ₹12,000 position, that's about 0.13%, so a full round trip costs about 0.35%. So:
   - Apply DP once per stock per sell day, however many shares are sold.
   - **Portfolio strategies must not trim or top up positions they keep holding.** Rebalancing back to equal weight sells a little of every stock each month, and each of those sells pays a full DP charge. Only buy new names and sell names that drop out. Make trimming an option that's off by default.
   - Every report shows total costs in rupees and as a % of capital per year.
3. **Optional tax mode:** short-term vs long-term capital gains at current rates, so returns can be shown before and after tax.
4. **Orders:**
   - Next-open market fills by default.
   - Stop-loss, trailing stop and take-profit, checked against the day's high and low. When a bar hits both, assume the stop was hit first (conservative). Gap-downs below a stop fill at the open, not at the stop price.
   - Circuit-limit days, where open = high = low (likely locked), are treated as unfillable.
5. **Outputs:**
   - Equity curve and drawdowns
   - CAGR, volatility, Sharpe and Sortino (risk-free rate configurable, default 6.5%), max drawdown, Calmar
   - Win rate, average win and loss, profit factor, expectancy per trade, exposure %, turnover, total costs paid
   - Full trade log with entry and exit reasons
6. **Built-in honesty checks:** these should appear in every report, not as optional extras.
   - Benchmark comparison: Nifty 500 or equal-weight, over the same period.
   - Random-entry placebo: same stocks and holding lengths, random dates (keep the existing one).
   - In-sample / out-of-sample split, with 2016–2021 in-sample and 2022 onwards out-of-sample by default. Also walk-forward testing.
   - Parameter sweep with a heatmap. Warn when only a narrow parameter range works.
   - Count the number of strategy variants tried in a session, and display it next to results to show the risk of data-mining.
   - Bootstrap confidence interval on CAGR and Sharpe.
7. **Speed:** a single-strategy backtest over 500 stocks and 10 years should take under about 30 seconds on a laptop. Precompute features to get there.

## Phase 6: Interactive app

A local Streamlit app (unless you have a strong reason for something else):

1. **Strategy builder:** pick or edit a YAML strategy in the browser, with validation as I type. Also load Python strategies.
2. **Settings:** date range, universe, capital, costs, sizing, in/out-of-sample split.
3. **Results page:**
   - Equity curve vs benchmark, and drawdown chart
   - Metrics table, with the placebo and out-of-sample results next to the main ones
   - Monthly returns heatmap
   - Trade log
4. **Stock chart:** candlestick with selected indicators (DMAs, Bollinger, MACD and RSI panels) and buy/sell markers for that strategy's trades. Plotly.
5. **Compare runs:** save every run with its config, then compare several runs side by side.
6. **Data status page:** last update date, failed days, unmapped symbols, and corporate actions applied.

## Phase 7: Daily scanner (inference on the latest data)

1. After each data update, run selected strategies on the latest day and output a ranked list of stocks whose entry conditions are met. For each one, show the indicator values behind the signal (e.g. "MACD crossed up, close 4% above 200 DMA, RSI 52, volume 2.1× average").
2. Show each stock's current open positions per strategy, and the exit or stop levels to watch.
3. Show the strategy's out-of-sample stats next to its signals, so a signal from a strategy with no edge is obviously labelled as such.
4. Export the results as CSV and Markdown.
5. Optional later step: tag stocks or sectors mentioned in my daily DFB news briefs, which I keep as Google Docs, so scanner hits can be cross-referenced with news. Design the scanner so this can plug in later; don't build it now.

## Engineering requirements

- Python 3.11+, pandas or Polars, pyarrow, DuckDB (optional), Streamlit, Plotly, pytest.
- One `pyproject.toml`; installable with `pip install -e .`; a CLI (`nsebt update | audit | backtest | scan | app`).
- Config in one YAML file: paths, cost rates, defaults. No hard-coded paths.
- Tests:
  - parsing, using a real bhavcopy file as a fixture
  - pipeline behaviour on 404s, block pages and repeat runs (mocked network)
  - every indicator against the reference library
  - no look-ahead for every indicator and the rule engine
  - cost model on worked examples
  - engine fills on hand-built price series, including stop-loss gap-downs and cash limits
  - the end-to-end example strategies
- Logging to a file, with a clear one-line summary on the console.
- A README with setup, daily workflow, how to write a strategy, and known limitations.

## Order of work and checkpoints

Stop and report to me after each of these:
1. **Phase 0 audit**, before changing any data.
2. **Phases 1–2:** the clean store plus the pipeline backfilled from 2026-08-20 to today, the mapping coverage numbers, and the overlap validation results.
3. **Phase 3:** indicators with the reference-check results.
4. **Phases 4–5:** run three example strategies through the engine and show the full reports: the existing momentum strategy, the breakout strategy, and the `macd_trend_pullback` YAML example above.
5. **Phases 6–7:** the app and the scanner.

At each checkpoint, list what was done, the test results, anything that didn't work or that you're unsure about, and any decision you need from me.

## Questions to answer during the work, not to guess

- Does the 390 MB features file contain anything that can't be recomputed from prices (e.g. fundamentals joined by date)?
- How complete is NSE's corporate-actions data back to 2016? If it's not complete, what does the split-detection fallback miss?
- Can Nifty 500 index levels be downloaded from NSE, or does the benchmark have to be the equal-weight proxy?
