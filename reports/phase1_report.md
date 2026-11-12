# Phase 1 checkpoint: canonical NSE data layer

Built 2026-11-13. Covers 2016-08-29 to 2026-11-12 (2,530 trading days).

## 1. Summary

The store is now **NSE-first**. Every row comes from NSE's official daily files, keyed by ISIN, and raw prices are kept unchanged. Adjustment factors come from NSE's own corporate-action announcements. The Bloomberg extract is now used only to map Bloomberg IDs to NSE securities (so the historical membership can be used) and to validate the result.

| | |
|---|---|
| Price rows | **5,001,751** (0 parse errors, 0 duplicates) |
| Securities | **3,935** (3,214 still trading), 4,341 ISINs |
| Delivery data | on 4.49M rows; **100% of EQ-series rows** |
| Corporate actions | 22,627 announcements → 15,201 events; **787 of 788 splits/bonuses confirmed by the price move** |
| Index levels | **179 NSE indices**, every day (Nifty 50, Nifty 500, India VIX, all sector and factor indices) |
| Bloomberg mapping | **856 of 856 Bloomberg price series** matched by returns; median daily-return agreement 99.96% |
| Membership | 22 snapshots, **498–502 members mapped per snapshot**; 2018-09-01 repaired (338 → 498) |
| Validation vs Bloomberg | daily returns agree within 0.2% on **99.78%** of 1.77M stock-days |
| Disk | 479 MB raw downloads, 223 MB store |
| Tests | 48 passing |

## 2. What was built

| Module | Does |
|---|---|
| `nsefetch.py` | Polite, resumable downloads. Requests are spaced (0.5 s + jitter), retried with exponential backoff, and paused for 10 minutes after 6 consecutive failures (stopping after 3 pauses). HTML block pages served with status 200 are rejected. 404s for old dates are remembered so they are never asked for again. Files are written atomically. |
| `nseparse.py` | Parsers for the legacy bhavcopy (to 2024-07-05), the UDiFF bhavcopy (from 2024-07-08), delivery (MTO) files and index closes. ISIN is kept on every row. |
| `nsestore.py` | Links ISINs into stable security IDs across splits and renames; symbol history; raw prices partitioned by year; adjustment on load. |
| `corpactions.py` | NSE announcements → price factors. Fallbacks: matching by symbol and issuer when NSE's announcement carries a different ISIN, and inferring unannounced splits from ISIN changes. |
| `bbgmap.py` | Matches Bloomberg IDs to NSE securities by daily returns (tick-aware), plus name and override matches, and reconciles corporate actions between the two sources. |
| `membership.py` | Point-in-time Nifty 500 by SecurityID, on NSE's effective dates. |
| `universe.py` | Nifty 500 (point-in-time), all NSE equities, and custom watchlists. |
| `config.yaml` | All paths and politeness settings; no hard-coded paths. |

Commands:

```bash
python -m nsebt backfill                    # download (resumes; skips what it has)
python -m nsebt backfill --sources indices  # index closes
python -m nsebt build                       # raw files -> store, mapping, membership (~2 min)
```

## 3. Data sources (all checked by hand)

| Data | NSE file | Period |
|---|---|---|
| Daily OHLC, last, previous close, volume, turnover, trades, ISIN | `cm{DD}{MON}{YYYY}bhav.csv.zip` | to 2024-07-05 |
| Same, plus company name | `BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip` | from 2024-07-08 |
| Deliverable quantity and % | `MTO_{DDMMYYYY}.DAT` | whole period |
| Closing levels, P/E, P/B, dividend yield of every index | `ind_close_all_{DDMMYYYY}.csv` | whole period |
| Corporate actions | `www.nseindia.com/api/corporates-corporateActions` (JSON, 3-month chunks) | whole period |
| Listed securities, symbol changes, current Nifty 500 with industry | `EQUITY_L.csv`, `symbolchange.csv`, `ind_nifty500list.csv` | snapshot 2026-11-12 |

Average price is computed as turnover ÷ volume, so the `sec_bhavdata_full` file isn't needed for the history. Download totals: 2,530 price files, 2,530 delivery files and 2,530 index files, with **0 failures**. 332 + 167 dates returned 404, all of them holidays or weekends.

## 4. Corporate actions

**Finding: NSE's legacy files do not re-base `PrevClose` on ex-dates** (JSWSTEEL's 1:10 split shows a previous close of ₹1,644.55 against a close of ₹163.05). So `PrevClose` can't be used to detect events, and NSE's announcement feed was used instead.

| Kind | Events | How the factor is set | Applied by default |
|---|---|---|---|
| split | 383 | exact: new face value / old face value | yes |
| bonus | 413 | exact: b / (a + b) | yes |
| consolidation | 1 | exact | yes |
| rights | 272 | theoretical ex-rights price from issue price and prior close | yes |
| arrangement (demerger, scheme, capital reduction) | 131 | price-implied (ex-date open / prior close), only if the gap is over 3%, excluding same-day exact events | yes |
| special dividend (≥5% of price or labelled special) | 683 | 1 − dividend / prior close | yes |
| inferred split (ISIN change + clean ratio, no announcement) | 7 | the clean ratio | yes |
| dividend (ordinary) | 13,311 | 1 − dividend / prior close | **no**; available for total-return series |

Checks:
- **787 of 788** splits and bonuses inside the data period are confirmed by the actual price move on the ex-date. The exception is DVL's 2021-08-05 "Bonus 1:2": the price never moved, so it is flagged and not applied.
- 1,729 announcements (mostly dividends) carried a different ISIN from the price files: 1,728 were matched by the symbol trading at the time and 1 by issuer code. Without that fallback they would have been lost silently.
- 7 unannounced splits and consolidations were found from ISIN changes (e.g. CCAVENUE 10:1 consolidation on 2026-10-19, JSLL 1:5 in 2025).
- **Unexplained overnight jumps over 35%: 277, of which only 3 are Nifty 500 members** (GVKPIL, COFFEEDAY and BCG). All 3 are first prints after long suspensions, so they are genuine. The rest are small and SME stocks. Listed in `data/store/unexplained_jumps.parquet`.

**Answer to the plan's question "How complete is NSE's corporate-actions data back to 2016?"** It is close to complete for splits and bonuses: every one of the eight reference events from the Phase 0 audit is present and confirmed, and only 7 were missing (all caught by the ISIN fallback). Gaps are rare and specific: wrong or alternate ISINs (fixed by the fallback), one announced bonus that did not happen, and the occasional missing special dividend.

## 5. Bloomberg mapping and validation

| Method | Count |
|---|---|
| Matched by returns (all 856 Bloomberg price series) | 856: 576 found by correlation search, 280 also matching by name |
| Name-only (members with no Bloomberg prices, i.e. recent listings) | 22 |
| Manual override (`bbg_overrides.csv`) | 6: GABR→GABRIEL, GALL→GALLANTT, LGEL→LGEINDIA, PIRAMALF→PIRAMALFIN, SANOFICO→SANOFICONR, TRAVELFO→TRAVELFOOD |
| Unmatched | **1: PRIF** (decision needed, section 8) |

Fixes made along the way: Tata Motors DVR uses an `IN9` ISIN (now included); REITs and InvITs (series RR/IV) are now kept because three are Nifty 500 members; the match scoring allows for Bloomberg's ₹0.01 rounding on very low-priced stocks.

Validation over the full overlap (2016-08-29 to 2026-08-27):

| Check | Result |
|---|---|
| Daily returns agree within 0.2% | **99.78%** of 1,774,977 stock-days |
| Price level within 1%, applying only splits, bonuses and rights (events both sources treat alike) | **95.2%** of stock-days |
| Price level within 1%, applying all default events | 87.2%. The difference is demergers and special dividends, which NSE's data adjusts for and Bloomberg mostly does not. |
| Reference splits and bonuses | raw moves of −49% to −90% become at most +3% after adjustment (IRCTC: 10.6%, a genuine move that week) |
| Bloomberg vs NSE close on 2026-08-26 | 98.8% identical |
| Bloomberg vs NSE close on **2026-08-27** | **43.7% identical**: Bloomberg's last day was pulled during the session, as the Phase 0 audit suspected. The store uses NSE's official close. |

Reconciliation: 637 of 856 pairs show no difference in adjustment at all. In the other 219, the largest differences are exactly the **unadjusted Bloomberg demergers found in Phase 0** (AGIG, ARVND, TTCH, ABREL, ARCP, TCOM, BSOFT), which the new store adjusts. Most small differences come from special dividends and from penny-stock rounding.

## 6. Membership

| Snapshot (source) | Effective from | Members | Mapped |
|---|---|---|---|
| 2016-09-01 … 2026-03-01 (Bloomberg, 20 snapshots) | NSE's semi-annual effective date (first trading day of April or October) | 498–502 | 498–502 |
| 2018-09-01 (repaired) | 2018-04-02 | 338 + 160 = 498 | 498 |
| 2026-08-27 (Bloomberg current) | 2026-04-01 | 500 | 499 |
| 2026-11-12 (NSE's own list, by ISIN) | 2026-10-01 | 501 | 500 (DUMMYHEG is NSE's placeholder line, not a stock) |

The 28 current members that had no Bloomberg history now have full NSE history from their listing date.

## 7. Status of the Phase 0 recommendations

| # | Phase 0 problem | Status |
|---|---|---|
| 1 | History adjusted; sizing needs raw prices | **Done**: raw is stored, adjustment happens on load |
| 2 | Synthetic IPO rows | **Done**: NSE data has none; Bloomberg rows dropped in mapping |
| 3 | 35% filter deletes crashes | Adjusted series no longer need it; the engine change is in Phase 5 |
| 4 | 2018-09-01 snapshot | **Done** |
| 5 | 28 current members without history | **Done**: full NSE history |
| 6 | Unadjusted demergers | **Done**: NSE-based factors |
| 7 | Name matching only 203/500 | **Done**: 884 of 885 mapped |
| 8 | Membership effective dates | **Done**: NSE's dates |
| 9 | Weekend sessions | **Done** for the backfill (listed in `config.yaml`); Phase 2 adds the holiday calendar |
| 10 | Bloomberg 2026-08-27 intraday | **Confirmed and avoided** (NSE close used) |
| 11 | `/F` artefacts | **Done**: excluded |
| 12–14 | Gaps, stale bars, features file | Phases 3 and 5 |

## 8. Open items and decisions

1. **PRIF IN Equity** (Bloomberg current list, 2026-08-27) is either Prime Focus (`PFOCUS`) or Privi Speciality Chemicals (`PRIVISCL`). If you know which, add one line to `bbg_overrides.csv`. It affects one stock's membership from April to September 2026.
2. **Special dividends** (≥5% of price) are adjusted by default so indicators don't see false breakdowns. Bloomberg doesn't do this. The setting is `PRICE_KINDS` in `nsestore.py`, if you prefer Bloomberg's convention.
3. **Back-adjusted price levels encode later splits** (2016 RELIANCE shows about ₹237, not ₹1,057). Returns, ratios and indicators are unaffected. Rules that compare an absolute price level (e.g. "price > ₹100") or position sizing must use raw prices. The store keeps both, and the Phase 5 engine will enforce this.
4. Phase 2 next: the daily `update` command (new days only, run report, holiday calendar), plus `backup`, `restore` and `doctor`, and a pinned environment. I still need your answers on **where backups should go** and **whether you have a GitHub account**.

## Files

- Store: `data/store/` (`prices/year=YYYY/`, `securities`, `symbol_history`, `corporate_actions`, `membership`, `indices`, `bbg_mapping`, `unexplained_jumps`)
- Reports: `reports/phase1/` (`phase1_summary.json`, `validation.json`, `bbg_mapping.csv`, `ca_reconciliation.csv`, `ca_residual_steps.csv`)
- Manual decisions: `bbg_overrides.csv`
