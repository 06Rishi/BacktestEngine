# Data audit: Bloomberg Nifty 500 extract (Phase 0)

Audit date: 2026-11-12. Data audited: `dataextracted/DE/` (unzipped, 458 MB). Nothing in it was modified.

To reproduce (about 100 seconds):

```bash
cd nsebt
../.venv/bin/python -m nsebt audit --de-root ../dataextracted/DE --out ../reports/audit \
    --bhavcopy tests/fixtures/sec_bhavdata_full_12112026.csv
```

All numbers below come from `reports/audit/audit_summary.json` and the CSVs next to it (index at the end).

---

## 1. Summary

The extract is in good mechanical shape: 858 daily series, 1,798,332 rows, 2016-08-29 to 2026-08-27, with no duplicate dates, no OHLC inconsistencies, no non-positive prices and no unreadable files. The precomputed features file is an exact, look-ahead-free function of the prices.

Seven findings change how the data should be used:

| # | Finding | Impact |
|---|---|---|
| 1 | **Prices and volumes are already adjusted for splits and bonuses**, as of the extraction date. The plan assumed they were raw. Dividends are *not* adjusted. Some demergers are, some are not. | Phase 1's adjustment step must be inverted: we need raw prices for position sizing, not adjusted ones for indicators. |
| 2 | **162 series start with a synthetic row at the IPO offer price**, with a close but no open, high, low or volume. | Creates 71 fake "listing-day" moves above 20% (e.g. TATATECH +163%). Must be dropped. |
| 3 | **The 2018-09-01 membership snapshot is incomplete**: 338 members instead of about 500. 160 stocks that were members in both March 2018 and March 2019 are missing. | Distorts the point-in-time universe for six months. Repairable. |
| 4 | **28 current Nifty 500 members have no Bloomberg prices at all** (recent listings such as GROWW, LENSKART, MEESHO, TATACAP, LGEL, PINELABS, TMCV). | 5.6% of today's universe has no history; it has to come from NSE. |
| 5 | **The existing engine's 35% "corporate action" filter now deletes real crashes.** Since prices are already adjusted, 29 of the moves it removes are real, e.g. YES Bank −56% on 2020-03-06, CCAvenue −71%, DHFL −43%. | Biases every backtest in the strategy's favour. Must be removed or replaced. |
| 6 | **Bloomberg codes match NSE symbols for only 203 of 500 current members.** For former members it's 88 of 387. | Confirms the plan's estimate. ISIN mapping (`bbg_isin.csv`) is needed. |
| 7 | **NSE trades on some weekends** (Diwali Muhurat sessions, Budget days, special sessions): 10 such days are in the data. | The current downloader skips all weekends, so it would miss these days. |

**Recommendation:** recompute features rather than reuse the 390 MB file (section 5). The data can be used once the fixes in section 9 are applied.

---

## 2. What is in the folder

The audit walked **925 files**: 877 parquet, 32 CSV, 8 notebooks and 8 junk files (`.DS_Store`, `.ipynb_checkpoints`). All of them were readable. Full list with row counts, columns, date ranges, ticker counts and null rates: `audit/inventory_files.csv`. Per-column dtypes and null rates: `audit/inventory_columns.csv`.

Differences from what the plan describes:

| Plan says | Actually |
|---|---|
| `DE/data/raw/prices/*.parquet`: 858 securities | 859 parquet files. 858 hold prices; the 859th is not a price file. File names use the Bloomberg root with `/` replaced by `_` (`DBNK_F.parquet`). |
| `extraction_failed.parquet` | It is `extraction_failed.csv` with 3 rows (`360ONE IN`, `RAYMONDL IN`, `SANOFICO IN`). Those failed because their membership codes ended in ` IN` rather than ` IS`, so the request became `360ONE IN` instead of `360ONE IN Equity`. 360ONE and RAYMONDL were pulled anyway under their other code; **only SANOFICO has no prices**. |
| 3 quality reports in `prices/` | 11 CSVs (coverage, continuity, quarter-end availability, price-only securities). Their counts agree with this audit, e.g. `ohlc_missing_report.csv` has the same 162 rows. |
| `macro`, `index`, `corporate_actions` empty | Confirmed empty. Also empty: `data/logs`, a nested `data/raw/prices/data/...` tree (an accidental copy of the folder layout), a top-level `prices/` and `DE/data/raw/prices`. |
| — | `data/raw/prices_daily/` has two one-year RELIANCE test pulls. They match the main RELIANCE file on every day except 2026-08-27, where the side file shows a lower volume (8.19M vs 12.29M), consistent with a pull made during the session. |
| — | `data/processed/monthly_closes/` has two small leftovers from a name-search notebook. They found none of the real-estate companies they looked for, because the search used company names that are not in the data. |
| — | `constituents/` also has `nifty500_historical_membership.parquet` (the raw snapshots), `nifty500_current_snapshot.parquet`, a mapping audit CSV and a validation CSV. |

---

## 3. Prices

### 3.1 Checks

| Check | Result | Evidence file |
|---|---|---|
| Series / rows / dates | 858 / 1,798,332 / 2016-08-29 to 2026-08-27 | `price_ticker_summary.csv` |
| Files holding more than one ticker | 0 | `price_files.csv` |
| Duplicate (ticker, date) rows | 0 | `price_duplicates.csv` |
| OHLC inconsistent (low > open/close, high < open/close, low > high) | 0 | `price_ohlc_violations.csv` |
| Non-positive prices or negative volume | 0 | `price_nonpositive.csv` |
| Zero volume | 0 | `price_zero_volume.csv` |
| Close present, open/high/low missing | 162 rows. **Every one is the first row of its series and has only a close (the IPO row, 3.3).** | `price_missing_ohl.csv` |
| Volume missing | 672 rows: the 162 IPO rows, 44 rows in the two `/F` artefact series, and the rest in the illiquid early history of AIIL, PTCIL and VORG | `price_missing_volume.csv` |
| Gaps over 5 trading days | 60 gaps in 30 series. Longest: SWANDEF (377 days, insolvency), GVKP (256), LANCI (223). Five series (1958850D, ABGS, BHAFIN, EXCC, SCTL) share the same 93-day gap from 2019-11-14 to 2020-03-27 and then end on 2020-04-13, which looks like a vendor artefact around suspensions. | `price_gaps.csv` |
| Stale bars (close unchanged, no volume) | 447, mostly AIIL (228), PTCIL (94), VORG (60) | `price_stale.csv` |
| "Locked" bars (open = high = low = close) | 2,329 (0.13% of rows), concentrated in illiquid names | `price_locked_bars.csv` |
| Series ending before the data ends | 76, matching the plan. By year of last trade: 2017: 6, 2018: 5, 2019: 3, 2020: 19, 2021: 6, 2022: 8, 2023: 10, 2024: 10, 2025: 6, 2026: 3 | `price_ticker_summary.csv` |
| Series starting after 2016-08-29 | 213 (listings during the period) | same |

### 3.2 Trading calendar

Treating any date on which at least 50 series traded as a market day gives **2,478 market days**. That includes 10 weekend sessions: Diwali Muhurat (2016-10-30, 2019-10-27, 2020-11-14, 2023-11-12), Union Budget days (2020-02-01, 2025-02-01, 2026-02-01) and special Saturday sessions (2024-01-20, 2024-03-02, 2024-05-18).

There are also 8 off-calendar dates on which exactly one series has a row. Seven of these are IPO rows dated on market holidays: SBFC on 2023-08-15, JSWINFRA on 2023-10-02, NEWGEN on 2018-01-26, and others. The eighth is IEX on 2017-10-20. Evidence: `price_calendar.csv`, `price_off_calendar_rows.csv`.

### 3.3 IPO offer-price rows

For 162 series the first row has a close equal to the IPO offer price and nothing else. Examples: TATATECH ₹500 on 2023-11-29 (listed next day at ₹1,199.95), IREDA ₹32, BAJAJHFL ₹70, NETWEB ₹500. A backtest would see these as a 1-day return and could "buy" a stock before it listed. **Fix: drop the first row of a series when it has a close and nothing else.** The existing `bloomberg.load_prices` already drops rows missing open, high or low, so it removes these by accident. A new loader must do it on purpose.

### 3.4 Adjustment basis: adjusted for splits and bonuses, not for dividends

Three independent tests agree.

**a) No split or bonus jumps in the data.** Over 10 years, 858 Indian stocks have had well over a hundred splits and bonuses. In unadjusted data each would show up as an overnight drop to 1/2, 1/5, 1/10 and so on. Of the 353 close-to-close moves above 20%, only 5 match a clean split or bonus ratio at the open. On inspection, none of those 5 is a split:

- ASTERDM 2024-04-23 (−22%) is its ₹118 special dividend.
- BSOFT 2019-01-24 (−40%) is the KPIT/Birlasoft demerger.
- PATANJAL 2020-01-27 (×5) is the relisting after its insolvency capital reduction.
- SUDARCOL 2020-07-16 and TVTN 2023-02-13 (both −25% at the open) are unexplained and go on the Phase 1 corporate-action reconciliation list.

**b) Known events show no jump.** These are publicly announced events. Ex-dates were taken from memory as approximate and checked in a ±5-day window:

| Stock (Bloomberg code) | Event | Raw data would show | Largest close-to-close ratio in window | Volume after / before |
|---|---|---|---|---|
| RELIANCE | bonus 1:1, Sep 2017 | 0.50 | 1.03 | 1.21 |
| RELIANCE | bonus 1:1, Oct 2024 | 0.50 | 0.97 | 0.80 |
| TCS | bonus 1:1, May 2018 | 0.50 | 1.03 | 1.14 |
| INFY (`INFO`) | bonus 1:1, Sep 2018 | 0.50 | 1.03 | 1.12 |
| IRCTC | split 1:5, Oct 2021 | 0.20 | 0.87 | 0.32 |
| WIPRO (`WPRO`) | bonus 1:1, Dec 2024 | 0.50 | 1.02 | 0.77 |
| BAJFINANCE (`BAF`) | split 1:2 + bonus 4:1, Jun 2025 | 0.10 | 1.03 | 0.81 |
| HDFCBANK (`HDFCB`) | bonus 1:1, Aug 2025 | 0.50 | 0.98 | 1.15 |

None shows the jump. Volume is level too: with raw volume it would rise by 1/ratio (×2, ×5, ×10) after each event. **Volume is adjusted as well.** The absolute levels agree: HDFCB shows ₹431 at the end of March 2020 and BAF shows ₹222, about half and a tenth of what those stocks traded at then. Evidence: `adjustment_reference_events.csv`.

**c) Dividends are not adjusted.** For 763 securities with Bloomberg's gross total-return index, the index grows faster than `PX_LAST` by a median of **0.63% a year** (interquartile range 0.09% to 1.77%), which is about the dividend yield. If prices were dividend-adjusted the gap would be zero. ASTERDM's −22% on its special-dividend ex-date points the same way. Evidence: `adjustment_dividend_check.csv`.

**Demergers and other events are adjusted inconsistently.** RELIANCE's price is about 10% below its market cap ÷ shares before 2023, which fits adjustment for the 2023 Jio Financial demerger and the 2020 rights issue. But several overnight gap-downs of 35–75% look like unadjusted demergers or capital reductions: ARVND −65% on 2018-11-28 (Arvind Fashions and Anup demerger), BSOFT −40% on 2019-01-24 (KPIT/Birlasoft), ABREL −55%, AGIG −74%, TTCH −57%, ARCP −44%. In each, 89–100% of the move happens at the open. These need checking against NSE corporate-action data in Phase 1.

**Why this matters:**

- *Indicators* can use these prices almost as they are. Only the unadjusted demergers need fixing.
- *Position sizing and the whole-share rule* need **raw** prices: in 2020 one BAF share really cost about ₹2,200, not ₹222. With ₹36,000 and a 33% cap, these two prices lead to very different trades. The adjustment factors therefore have to be rebuilt in order to *un-adjust*.
- *Appending NSE data* (raw) to an adjusted history is only seamless when no split or bonus has happened since 2026-08-27. Any event after that date requires re-basing.
- *Volume and delivery* comparisons between NSE (raw) and Bloomberg (adjusted) need the same factors.

### 3.5 Classification of all 353 moves above 20%

| Label | Count | Meaning |
|---|---|---|
| `real_move` | 255 | Genuine market moves: listing pops, COVID (Mar 2020), YES Bank, Adani Enterprises (Feb 2023), Zee, IndusInd (Mar 2025), plus the handful of unadjusted demergers above |
| `ipo_reference_row` | 71 | Move from the synthetic IPO row to the first real close |
| `after_trading_gap` | 12 | First print after a suspension longer than 5 days (SWANDEF, GVKP, BCG, CHOLAHLD, ...) |
| `spike_reversed` | 10 | Undone the next day: probable bad prints (FCON, SITINET, YES 2019-10-01, ...) |
| `ca_ratio_only` / `ca_ratio_with_volume_shift` | 5 | Clean ratio at the open; on inspection none is a split (3.4a) |

Each move is listed with its overnight share, the nearest CA ratio and the volume shift in `price_big_moves.csv`.

### 3.6 Two artefact series

`DBNK/F IN Equity` and `POL/F IN Equity` each have 22 rows (2017-06 to 2018-06). All 22 are locked bars with no volume. The real `DBNK IN Equity` (Dena Bank) has 626 rows. **Exclude both.** `TTMT/A IN Equity` (Tata Motors DVR, ended 2024-08-29) is a genuine series.

---

## 4. Membership and mapping

| Check | Result |
|---|---|
| Snapshots | 20, every 1 March and 1 September from 2016-09-01 to 2026-03-01, plus the current master (500, as of 2026-08-27) |
| Members per snapshot | 500–502, **except 2018-09-01: 338** |
| 2018-09-01 detail | 186 dropped and 195 re-added six months later. 160 were members both before and after. A partial extraction, not real index changes. |
| Usual turnover per snapshot | 18–38 additions and the same number of deletions |
| Ever-members without a price file | 1 (SANOFICO, member on 2024-09-01 only) |
| Current members without a price file | **28**: ABDL, ACUTAAS, ANTHEM, BELRISE, CANHLIFE, CARTRADE, CPPLUS, EMMVEE, GABR, GALL, GROWW, HDBFS, ICICIAMC, JAINREC, LENSKART, LGEL, MEESHO, PARADEEP, PINELABS, PIRAMALF, PRIF, PWL, TATACAP, TEGA, TENNIND, TMCV, TRAVELFO, URBANCO. Prices were pulled only for historical-snapshot tickers, and these joined after 2026-03-01 (or under new codes). |
| Price files never in any membership | 0 |
| Member rows with no price near the snapshot date | 6: the two `/F` artefacts (4 rows), SANOFICO, and ADVENTHT, a member on 2025-09-01 whose prices start on 2025-11-13 |
| `Mapping_Status` in the canonical mapping | 9,856 mapped, 1 unmapped (SANOFICO) |
| Bloomberg root equals an NSE symbol (bhavcopy of 2026-11-12) | Current members: **203 of 500**. Former members: 88 of 387. Matches like `BATA` → `BATAINDIA` and `MM` → `M&M` are not caught by name. |

How membership was pulled: `INDX_MWEIGHT_HIST` on `NSE500 Index` with an `END_DATE_OVERRIDE` on each snapshot date. NSE reconstitutes the index at the end of March and the end of September. A 1 September snapshot therefore reflects membership from about 31 March to 30 September, and a 1 March snapshot from about 30 September to 31 March. The current `Universe` switches membership on the snapshot date. As a result, additions and removals take effect about 1 month *before* NSE's effective date, and changes between reconstitutions (mergers, replacements) are invisible. This is a small, known bias; section 9 lists the fix.

Evidence: `membership_snapshots.csv`, `membership_price_crosscheck.csv`, `membership_unpriced_on_snapshot.csv`, `mapping_name_match.csv`.

---

## 5. The 390 MB features file

`data/processed/nifty500_historical_stock_features.parquet` has 1,798,332 rows and 37 columns: `Date`, `Ticker`, the 5 raw `PX_*` fields and 30 features.

| Check | Result |
|---|---|
| Rows and OHLCV vs the raw price files | Identical: same rows, 0 differing values in all 5 fields |
| Recomputation from prices (43 tickers including RELIANCE, TCS, HDFCB; 2.4M values) | **All 30 features match exactly** (max difference 0.0, identical missing-value pattern) |
| Values appearing before the window allows (would indicate rows leaking across tickers) | 0 |
| Infinite values | 0 |
| Anything not derivable from prices (fundamentals, index, macro) | **None** |

What each feature is (from the feature notebook, confirmed by recomputation):

| Columns | Definition | Coverage |
|---|---|---|
| `Return_1M/3M/6M/12M` | `PX_LAST` % change over 21/63/126/252 rows | 99.0 / 97.0 / 94.0 / 88.0% |
| `DMA_20/50/100/200` | SMA of `PX_LAST` | 99.1 / 97.7 / 95.3 / 90.5% |
| `Price_nDMA` | `PX_LAST / DMA_n` (a ratio, median about 1.0, not a %) | as DMA |
| `52W_High`, `52W_Low` | 252-row max/min of the **close** (not the high/low) | 88.1% |
| `Pct_Below_52W_High`, `Pct_Above_52W_Low` | distance from those | 88.1% |
| `RSI` | RSI(14), Wilder smoothing (`ewm(alpha=1/14, adjust=False, min_periods=14)`) | 99.3% |
| `ROC` | 20-row % change | 99.1% |
| `MACD` | EMA12 − EMA26 line only. **No signal or histogram, and no warm-up mask**: values exist from row 1. | 100% |
| `Daily_Return` | 1-row % change | 99.95% |
| `Volatility_20D/60D/120D/252D` | std of daily returns. **Daily, not annualised** (median 0.021) | 99.1–88.0% |
| `Current_Drawdown` | vs running max **since the series starts** (depends on where the extract begins) | 100% |
| `Max_Drawdown_3M/12M` | worst drawdown from a rolling 63/252-row peak, over 63/252 rows | 94.1 / 76.4% |
| `Volume_Change`, `Volume_20D_Avg`, `Volume_to_20D_Avg` | volume change, 20-row mean and ratio | 99.9 / 98.9 / 98.9% |

**Decision: recompute; don't reuse.** The file is correct for what it is, but:

1. Every value is cheap to recompute (seconds per feature over 1.8M rows).
2. It's computed over the IPO rows (3.3) and the unadjusted demergers (3.4), so returns, volatility and drawdowns around those dates are wrong.
3. It can't be extended with NSE data. It has fixed parameters, uses closes where highs and lows are expected (52-week high), and lacks MACD signal/histogram, ATR, Bollinger, ADX, delivery and the rest of the Phase 3 list.
4. It's 390 MB of redundancy.

It will stay useful as a regression check: Phase 3's `sma`, `rsi` and `macd` must reproduce its columns on the same input.

Evidence: `features_coverage.csv`, `features_raw_compare.csv`, `features_recompute.csv`, `features_early_values.csv`.

---

## 6. Fundamentals

`nifty500_historical_fundamentals.parquet`: 15,534 rows, 856 securities, quarterly from 2020-01-28 to 2024-12-31, 13 fields. `nifty500_fundamentals.parquet` is empty (0 rows). The other 8 parquet files there are extraction-failure and error logs.

| Field | Non-null | Field | Non-null |
|---|---|---|---|
| SALES_REV_TURN | 96.4% | CUR_MKT_CAP | 96.6% |
| NET_INCOME | 96.4% | EQY_SH_OUT | 98.3% |
| EBITDA | 90.8% | PE_RATIO | 79.0% |
| EBIT | 91.0% | PX_TO_BOOK_RATIO | 89.9% |
| BOOK_VAL_PER_SH | **51.8%** | PX_TO_SALES_RATIO | 91.9% |
| IS_EPS | 96.2% | DVD_SH_12M | 68.1% |
| | | TOT_RETURN_INDEX_GROSS_DVDS | 96.4% |

Problems for any future use:

- **Rows are dated at quarter end, not when results were published.** Indian companies report 30–60 days later, so using a quarter's numbers on its quarter-end date is look-ahead.
- Share counts are split-adjusted like the prices.
- 18 securities have fundamentals but no price file; 20 price series have no fundamentals.

Fundamentals are outside the plan's scope. The one useful thing here is `TOT_RETURN_INDEX_GROSS_DVDS`, which settled the dividend question. Evidence: `fundamentals_fields.csv`, `fundamentals_quarters.csv`.

---

## 7. What the notebooks did

| Notebook | What it does | Worth reusing |
|---|---|---|
| `DE/nifty500_bloomberg_extraction.ipynb.ipynb` (87 cells) | Bloomberg API session; current members from `INDX_MEMBERS`; historical members via `INDX_MWEIGHT_HIST` with `END_DATE_OVERRIDE` every 6 months; daily OHLCV via `HistoricalDataRequest` (2016-08-27 to 2026-08-27) per security; failure retries; quarterly fundamentals. **The price request sets no adjustment flags,** so the terminal's default settings (split/bonus-adjusted) were applied. That is why the data is adjusted. The ` IS` → ` IN Equity` code conversion missed codes ending in ` IN`, which caused the 3 failures. | The request pattern, if we ever re-pull. A re-pull should set `adjustmentSplit=False`, `adjustmentAbnormal=False`, `adjustmentNormal=False` explicitly, or pull both adjusted and raw. |
| `DE/NIFTY500_Historical_Feature_Pipeline_BLOOMBERG_VALIDATED.ipynb` | Computes the 30 features (section 5) per security and concatenates them into the 390 MB file; tests and runs the fundamentals extraction; leaves an FII/DII placeholder ("do not invent a Bloomberg mnemonic"). | Formulas only, as a regression reference. |
| `DE/NIFTY500_FINAL_INTEGRATION.ipynb.ipynb` | Writes the quality CSVs in `raw/prices` (OHLC missing, volume gaps, date continuity, coverage buckets, quarter-end availability) and the fundamentals audits; builds `project_data_inventory.csv`. | Nothing that this audit doesn't cover; its counts agree. |
| `DE/validation.ipynb` | Recomputes features for one stock and compares them; checks one stock's fundamentals; scans price files for a few company names to build monthly closes. | No. |
| `DE/test.ipynb` | Bloomberg connection test and a one-year RELIANCE pull (the `prices_daily` files). | No. |
| `DE/DE/Untitled.ipynb`, `prices/Untitled*.ipynb` | Zips the folder; empty notebooks. | No. |
| `notebooks/Nifty500_Audit_V1.ipynb` | An earlier audit. Its ticker matching compared `X IS` with `X IN EQUITY` and concluded that no member had prices, which is wrong. | No. |
| `notebooks/Nifty500_Comprehensive_Audit_V2.ipynb`, `Nifty500_Final_Validation.ipynb` | Generic audits (outputs in `audit_v2_output/`, `final_validation_output/`): 47 moves over 50%, 155 gaps over 7 calendar days. They marked adjustment basis, membership effective dates and corporate actions as "unverified blockers". | Their numbers are consistent with this audit at their thresholds. Two of their blockers are now resolved (adjustment basis, feature definitions). Membership effective dates are characterised above. |

---

## 8. Consequences for the existing `nsebt` code

All 14 existing tests pass, and 6 new audit tests have been added (20 in total). Applying this audit to the existing code turns up these problems:

| Where | Problem |
|---|---|
| `engine/portfolio.py`, `engine/trades.py`, `strategies/momentum.py`: `ca_filter=0.35` | Assumes raw prices. With adjusted data it removes **29 real moves** (crashes and spikes such as YES Bank, CCAvenue, DHFL, Jet Airways) and keeps most of the unadjusted demergers below 35% anyway. This favours strategies by hiding losses. |
| `bloomberg.load_prices` | Drops rows missing O/H/L (this happens to remove the IPO rows, which is correct), but keeps the `/F` artefacts and doesn't know the series are adjusted. |
| `universe.py` | Uses snapshot dates as effective dates, and uses the broken 2018-09-01 snapshot as it is. |
| `store.update` | Skips every Saturday and Sunday, so it would miss Muhurat and Budget sessions. |
| `panel.combine` | Appends raw NSE rows to adjusted Bloomberg rows without checking for corporate actions after 2026-08-27. |

None of this changes the decision to extend `nsebt` rather than rewrite it. The structure is sound, and each item is a contained fix in Phases 1–2.

---

## 9. Problems and recommended fixes

| # | Severity | Problem | Recommended fix (phase) |
|---|---|---|---|
| 1 | High | History is split/bonus-adjusted; sizing needs raw prices | Build a factor table per security (NSE corporate actions, with the split-detection heuristic as fallback). Store adjusted Bloomberg prices, factors, and derived raw prices. For appended NSE data, store raw and adjust with the same table (1) |
| 2 | High | 162 synthetic IPO rows | Drop the first row when it has a close and nothing else (1) |
| 3 | High | 35% CA filter deletes real crashes | Remove it. Handle the residual unadjusted events through the factor table, and flag unexplained >35% overnight moves for review rather than zeroing them (1, 5) |
| 4 | High | 2018-09-01 snapshot incomplete | Add back the stocks that were members both before and after the snapshot (160), and mark the snapshot as repaired (1) |
| 5 | High | 28 current members and SANOFICO have no history | Backfill from NSE bhavcopy archives. Without that, mark them as "history starts 2026-08-20" and keep them out of lookback-dependent signals until enough history exists (1, 2) |
| 6 | Medium | Some demergers / capital reductions unadjusted (ARVND, BSOFT, ABREL, AGIG, TTCH, ARCP, PATANJAL, ...) | Reconcile against NSE corporate actions. Apply factors where NSE confirms an event (1) |
| 7 | Medium | Name match covers only 203/500 current members | ISIN mapping from `bbg_isin.csv` + `EQUITY_L.csv`, then overrides, then name. Validate every match against prices (1) |
| 8 | Medium | Membership effective dates lag NSE's by about 1 month | Shift each snapshot's effective date to NSE's semi-annual effective date (last trading day of March/September) and document the remaining between-reconstitution blind spot (1) |
| 9 | Medium | Weekend trading sessions | Use NSE's holiday list *and* special-session announcements; don't skip weekends blindly (2) |
| 10 | Medium | Last Bloomberg day (2026-08-27) may be an intraday pull for some securities | Compare with the NSE bhavcopy for 2026-08-20 to 2026-08-27. Prefer NSE wherever they disagree (2) |
| 11 | Low | `DBNK/F`, `POL/F` artefact series | Exclude (1) |
| 12 | Low | 60 gaps over 5 days; 5 series share a suspicious 93-day gap | Keep the data. Don't fill gaps; don't let trades open or close across a gap of more than 5 days (5) |
| 13 | Low | 447 stale bars, 2,329 locked bars in illiquid early history | Treat locked bars as unfillable (already in the plan). A liquidity filter will exclude most of these names (5) |
| 14 | Low | Features file can't be extended and inherits problems 2 and 6 | Recompute in Phase 3; keep it as a regression reference |
| 15 | Info | Fundamentals dated at period end | Not in scope. If used later, lag them by a publication delay |

---

## 10. The plan's open questions

- **Does the features file contain anything that can't be recomputed from prices?** No. All 30 features are exact functions of `PX_LAST` and `PX_VOLUME`, and nothing fundamental or external is joined in.
- **How complete is NSE's corporate-actions data back to 2016?** Not answerable from this folder. Phase 1. The audit does give a test set: the 8 reference events above, the gap-down demergers, and the `ca_ratio` hits.
- **Can Nifty 500 index levels be downloaded from NSE?** Not answerable from this folder (`raw/index` is empty). Phase 1.

---

## 11. Decisions needed

1. **Raw price history.** To size positions on true historical share prices, we need raw prices for 2016–2026. Option A: un-adjust Bloomberg using NSE corporate-action data, which depends on that data being complete (unknown). Option B: backfill NSE bhavcopy files for the whole period, keep the store raw, and use Bloomberg only to validate. B also fills in the 28 missing current members, at the cost of about 2,500 polite downloads. **Recommended: B, with A as a cross-check.**
2. **2018-09-01 snapshot repair** as described in fix 4. Alternatively, re-pull that one date on the terminal.
3. Optional: if you are at the terminal again, pull `bbg_isin.csv` (plan Phase 1), raw unadjusted OHLCV (set the three `adjustment*` flags to false), and the 2018-09-01 membership.

---

## Evidence files (`reports/audit/`)

| File | Contents |
|---|---|
| `audit_summary.json` | All headline numbers |
| `inventory_files.csv`, `inventory_columns.csv` | Every file, every column, dtypes, null rates, date ranges, ticker counts |
| `price_files.csv`, `price_ticker_summary.csv`, `price_calendar.csv` | Per file, per series, per date |
| `price_duplicates.csv`, `price_ohlc_violations.csv`, `price_nonpositive.csv`, `price_zero_volume.csv`, `price_missing_close.csv` | Empty: no problems found |
| `price_missing_ohl.csv`, `price_missing_volume.csv`, `price_gaps.csv`, `price_stale.csv`, `price_locked_bars.csv`, `price_off_calendar_rows.csv` | Row-level problems |
| `price_big_moves.csv` | All 353 moves above 20%, with labels |
| `adjustment_reference_events.csv`, `adjustment_dividend_check.csv` | Adjustment-basis evidence |
| `membership_snapshots.csv`, `membership_price_crosscheck.csv`, `membership_unpriced_on_snapshot.csv`, `mapping_name_match.csv` | Membership and mapping |
| `features_coverage.csv`, `features_raw_compare.csv`, `features_recompute.csv`, `features_early_values.csv` | Features file |
| `fundamentals_fields.csv`, `fundamentals_quarters.csv` | Fundamentals |
