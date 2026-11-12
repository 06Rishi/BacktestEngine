# NSE Research and Backtesting Platform: Specification

## 1. Purpose and scope

A local research platform for swing trading NSE equities on daily data. It:

1. maintains a clean, survivorship-free daily history of all NSE equities, updated from NSE's published files;
2. computes a parameterised library of technical indicators;
3. lets strategies be defined in YAML rule files or Python and backtested under realistic account limits and costs;
4. measures every result against baselines and statistical controls designed to expose strategies with no edge;
5. scans the latest data each day for stocks that meet a strategy's conditions, and explains each signal.

**Target user:** a retail delivery trader. Holding periods run from days to weeks. Account limits are user-defined (defaults: ₹36,000 capital, at most 3 open positions, at most 33% of equity per position).

**Out of scope:** order placement, intraday or tick data, derivatives, automated trading.

**Guiding principle:** the platform succeeds if it reliably shows when a strategy is **not** profitable. Producing profitable backtests is not the measure of success.

## 2. Status

| Phase | Subject | Status | Report |
|---|---|---|---|
| 0 | Audit of the Bloomberg extract | Done | `reports/data_audit.md` |
| 1 | Canonical data layer | Done | `reports/phase1_report.md` |
| 2 | Daily NSE pipeline | Done | `README.md`, `reports/runs/` |
| 3 | Indicator and feature library | Done | `reports/phase3_report.md` |
| 4 | Strategy definitions | In progress | — |
| 5 | Backtesting engine | In progress | — |
| 6 | Interactive app | Planned | — |
| 7 | Daily scanner | Planned | — |

**Authoritative Phase 4–5 specification:** `docs/phase45_amendments.md`. It contains the working protocol, the corporate-action rules, execution details, cost constants and the mini-checkpoint list. Where it is more detailed than this document, it governs.

## 3. Core principles

- **Timing:** decisions use the close of day t; orders fill at the open of day t+1. No calculation may use data after the decision date.
- **Two price views, never mixed:**
  - **Signals:** back-adjusted prices (splits, bonuses, rights, demergers, special dividends).
  - **Execution and accounting:** raw prices plus a position ledger that applies each corporate action to shares, cash and receivables on the ex-date.
- **Survivorship-free:** delisted, merged and suspended securities stay in the data and are handled explicitly.
- **Explicit approximations:** every assumption and approximation is stated in the report it affects.
- **Reproducibility:** every run records the code commit, data version, resolved configuration, its hash, and the random seed.

## 4. Data

| Source | Use |
|---|---|
| NSE bhavcopy (legacy format to 2024-07-05, UDiFF format from 2024-07-08) | Daily OHLCV, ISIN, series |
| NSE `MTO` files | Delivery quantity and % |
| NSE `ind_close_all` | 179 index series with P/E, P/B, dividend yield |
| NSE corporate-actions API | Splits, bonuses, rights, dividends, arrangements |
| NSE reference lists (`EQUITY_L`, symbol changes, holidays, Nifty 500 list) | Security master, calendar, industry |
| Bloomberg extract (`dataextracted/DE`) | Validation and point-in-time Nifty 500 membership |

History starts on 2016-08-29. The store is described in `README.md`.

## 5. Phases 0–3 (complete)

- **Phase 0, audit:** inventory, price checks and feature-file review of the Bloomberg extract.
- **Phase 1, data layer:**
  - Partitioned parquet store keyed by a stable `SecurityID` (ISIN-linked).
  - Raw prices and adjustment factors stored separately.
  - Corporate-action factors from NSE announcements, confirmed against prices.
  - Bloomberg-to-NSE mapping validated on daily returns.
  - Point-in-time Nifty 500 membership.
- **Phase 2, pipeline:**
  - `nsebt update`: polite downloader, holiday-aware gap classification, run reports.
  - `ingest` for manually downloaded files.
  - `backup`, `restore` and `doctor`.
- **Phase 3, indicators:**
  - Every planned indicator, parameterised and vectorised; all match TA-Lib.
  - No-look-ahead tests and an incremental feature cache.

## 6. Phase 4: Strategy definitions

**YAML rule files**, for example:

```yaml
name: macd_trend_pullback
universe: nifty500
params: {rsi_max: 60, vol_mult: 1.5}
entry:
  all:
    - close > sma(200)
    - macd_cross_up(12, 26, 9)
    - rsi(14) < $rsi_max
    - volume > $vol_mult * sma(volume, 20)
exit:
  any:
    - macd_cross_down(12, 26, 9)
    - close < sma(50)
rank: roc(63)
stop_loss: 2 * atr(14)
take_profit: 3 * atr(14)
max_hold_days: 20
sizing: risk_pct(1.0)
```

**Expression language:**
- Parsed by a dedicated parser; `eval` is never used.
- **Supports:** arithmetic, comparisons, `and`/`or`/`not`, crossovers, `shift(x, n)` (n ≥ 1, past values only), `rank()` across the universe, `prior_high(n)`/`prior_low(n)`, raw price fields, and every Phase 3 indicator with parameters.
- **Errors:** report file, line and column.
- **Lints:** warn on comparisons that can never be true (e.g. `close > highest(high, n)`) and on adjusted prices compared with constants.

**Parameters:**
- Declared under `params`.
- Overridden per run with `--set name=value`, and validated against the declarations.

**Warm-up:** the first row where every term of the expression has a value.

**Python strategies:** implement the same interface, returning entry, exit and score frames. YAML strategies compile to it.

**Reference strategies:**
- `macd_trend_pullback`
- `breakout` (20-day breakout above the 50-day average on heavy volume)
- 12-1 `momentum` with a monthly rebalance, written both as YAML and as Python

## 7. Phase 5: Backtesting engine

### 7.1 Account (user-defined)

| Setting | Allowed range | Default |
|---|---|---|
| Capital | above 0, at most ₹1,000,000,000 | ₹36,000 |
| Maximum open positions | 1–100 | 3 |
| Maximum position size | 1–100% of equity | 33% |
| Maximum participation | % of 20-day average traded value up to the decision day | 5% |

- `nsebt account` prompts for these values and saves them to `config.yaml`. Per-run flags override them.
- Account limits always take precedence over a strategy's own settings.

### 7.2 Sizing and order handling
- **Sizing modes:** equal slots, fixed rupee amount, and `risk_pct` (the loss at the stop equals x% of equity).
- **Limits after sizing, in this order:** position cap, then liquidity, then available cash, then rounding down to whole shares. A stock whose single share exceeds the limit is skipped and recorded.
- **Orders hold a rupee amount.** Shares are calculated at the fill price.
- **Too many signals:** when more candidates signal than there are free slots, they are ranked by the strategy's `rank`, with a seeded tie-break. Rejected candidates are logged with a reason (`no slot`, `no cash`, `cap`, `liquidity`).
- **No trimming:** portfolio strategies don't trim or top up positions they keep holding. Only new entries and full exits trade. Trimming is an option, off by default.

### 7.3 Execution rules
- **Fills:** at the next open; sells before buys.
- **Stops and targets:** checked against the day's raw high and low.
  - A gap through either fills at the open.
  - When both are touched in one bar, the stop fills first.
- **Trailing stop:** referenced to the highest raw close since entry.
- **Locked circuit bars:** handled by direction. A bar locked at the upper limit blocks buys; a bar locked at the lower limit blocks sells. Blocked sells retry daily; blocked buys expire after one day.

### 7.4 Corporate actions and security lifecycle
- **Splits, bonuses and consolidations:** adjust shares, stops and targets on the ex-date. Fractional shares are paid as cash.
- **Dividends, rights entitlements and demerger value:** become receivables that count toward equity and turn into usable cash after a configured lag.
- **Mergers:** converted into the acquirer using `mergers.csv`.
- **Securities that stop trading:** classified as merged, delisted, suspended, truncated (data end) or unknown. Each class has a stated treatment. Delisting is valued under a configurable policy (default: a 50% haircut), and the report shows the result under every policy.
- **Leaving the index:** a holding that drops out of the index is kept. Universe membership controls entries only.
- **Engine check:** a costless buy-and-hold of every Nifty 500 security, run through the engine, must reproduce its total-return adjusted price path.

### 7.5 Costs (Zerodha equity delivery, all configurable)

| Charge | Rate | Side |
|---|---|---|
| Brokerage | ₹0 | — |
| STT | 0.1% | buy and sell |
| NSE transaction charge (includes IPFT) | 0.00307% | buy and sell |
| SEBI fee | ₹10 per crore | buy and sell |
| GST | 18% of transaction charge + SEBI fee | buy and sell |
| Stamp duty | 0.015% | buy |
| DP charge | ₹15.34 per stock per sell day | sell |
| Slippage | 10 bps per side (sensitivity at 0, 10 and 25) | buy and sell |

**Reference:** a ₹12,000 round trip costs ₹42.04 (0.350%).

Reports show total costs in rupees and as a % of capital per year.

**Tax:** a separate after-tax estimate. Short- and long-term capital gains at the rates in force on each date, FIFO lots, loss set-off and carry-forward. A business-income mode is also available. Tax never changes the engine's results.

### 7.6 Outputs
- **Charts:** equity curve against the baselines, drawdowns, and a monthly-returns heatmap.
- **Metrics:** CAGR, volatility, Sharpe and Sortino (risk-free rate 6.5% by default), maximum drawdown, Calmar, win rate, average win and loss, profit factor, expectancy, exposure, turnover, and costs.
- **Trade logs:** the full trade log with entry and exit reasons and the indicator values at entry; the rejected-signal log.
- **Trailing returns** over the ranges 1D, 5D, 1W, 2W, 1M, 3M, 5M, 6M, 12M, 2Y, 5Y and All. The same ranges drive the app's chart dropdown.

### 7.7 Baselines (in every report)

| Baseline | Costs | Status |
|---|---|---|
| Nifty 500 TRI and Nifty 50 TRI, buy and hold | None | Theoretical benchmark |
| Random 3 stocks, buy and hold (1,000 draws) | Full | Executable; median and 5–95% band |
| The strategy's first picks, held | Full | Executable |
| Official Nifty500 Equal Weight index | None | Price-index reference (excludes dividends) |
| Member-based equal-weight reconstruction, rebalanced monthly | Full | Reference only; not executable with 3 positions |

**TRI source:** the official total return index where available. Otherwise an approximate TRI built from the price index and dividend yield, clearly labelled. A price-only index is never the primary benchmark.

### 7.8 Statistical controls (in every report)
- **Placebos:** run through the same engine, with the same slots, eligibility and holding lengths.
  - **Timing placebo:** the same stocks on random dates.
  - **Selection placebo:** the same dates with random stocks.
- **Holdout:** in-sample 2016–2021, out-of-sample 2022 onwards.
  - The out-of-sample period is hidden unless a run passes `--final`, and every viewing is recorded.
  - Momentum and breakout results for 2022 onwards have already been viewed (legacy runs), so that period is not a clean holdout for them.
- **Walk-forward:** parameters chosen on rolling 3-year windows by net Sharpe.
- **Parameter sweeps:** heatmaps, with a warning when profitable settings form an isolated island.
- **Block bootstrap:** confidence intervals for CAGR and Sharpe from daily portfolio returns.
- **Experiment ledger:** a global record of every variant tried, with the deflated Sharpe ratio.
- **Slippage sensitivity:** results repeated at 0, 10 and 25 bps per side.

### 7.9 Performance
- A single backtest on the Nifty 500 over 10 years takes 30 seconds or less.
- A full report with every control takes 10 minutes or less.

## 8. Phase 6: Interactive app (Streamlit and Plotly)

1. **Strategy editor:** YAML with validation as it is typed; Python strategies can also be loaded.
2. **Run settings:** dates, universe, account values, costs, sizing and holdout options.
3. **Results:**
   - Equity against the baselines, drawdown, monthly heatmap, trade log.
   - Metrics alongside the placebo and out-of-sample results.
4. **Charts:** every time-series chart has a range dropdown (1D, 5D, 1W, 2W, 1M, 3M, 5M, 6M, 12M, 2Y, 5Y, All), which sets the visible window over daily bars.
5. **Stock chart:** candlesticks with chosen indicators and the strategy's buy and sell markers.
6. **Run comparison:** saved runs side by side.
7. **Data status:** last update, failed days, unmapped symbols, corporate actions applied.

## 9. Phase 7: Daily scanner

1. **Ranked signal list:** after each update, run the selected strategies on the latest day. List the stocks that meet the entry conditions, ranked, with the indicator values that triggered each signal.
2. **Open positions:** current positions for each strategy, with their exit and stop levels.
3. **Strategy credibility:** each strategy's out-of-sample statistics shown next to its signals.
4. **Exports:** CSV and Markdown.
5. **Future extension:** cross-reference signals with news briefs. Not in scope now.

## 10. Engineering requirements

- **Packaging:** Python 3.11+ (3.12 pinned with `uv`), with a single `pyproject.toml`.
- **CLI:** `nsebt update | build | audit | account | strategy check | backtest | sweep | scan | app | backup | restore | doctor`.
- **Configuration:** all paths, costs, account values and defaults in `config.yaml`. No hard-coded paths.
- **Logging:** detailed logs to file, plus a one-line summary on the console.
- **Tests:**
  - parsers on real NSE files; pipeline behaviour under mocked network faults
  - indicators against TA-Lib; no look-ahead in indicators, the rule engine and the engine
  - cost worked examples; engine fills on hand-built price series; corporate-action and lifecycle cases
  - end-to-end runs of the reference strategies
- **Documentation:** `README.md` covers setup, the daily workflow, writing a strategy, and known limitations.
- **Attribution:** no references to code-generation tools in code, documentation or commit history.

## 11. Delivery protocol

Detailed in `docs/phase45_amendments.md`, Part 1:

- **Mini-checkpoints:** each is tested, committed, and logged in `reports/phase45_progress.md`.
- **Structural stop rules:** S1–S7, with reports filed in `reports/structural/`.
- **Gate A,** after the engine is validated: the user must approve before strategy and research work begins.
- **Gate B,** after full reports for the three reference strategies.

## 12. Resolved questions

| Question | Answer |
|---|---|
| Does the Bloomberg features file contain anything that can't be recomputed? | No. Every column is reproducible from prices (Phase 3, `reports/phase3/old_features_regression.csv`). |
| Is NSE's corporate-action data complete back to 2016? | Nearly. 787 of 788 exact events are confirmed by price; 7 unannounced splits were inferred from ISIN changes; 3 unexplained jumps remain among Nifty 500 stocks, all relistings. |
| Are Nifty 500 index levels available from NSE? | Yes, daily in `ind_close_all`, as price indices. The total return index (TRI) needs a separate source (§7.7). |
