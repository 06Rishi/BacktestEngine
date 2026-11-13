# Gate A: engine validation (Milestones 1–2)

Date 2026-11-13. Spec: `docs/phase45_amendments.md`. Progress log: `reports/phase45_progress.md`.

**Status.** M1.1–M2.6 are complete. 257 of 257 tests pass in about 7 s. After the independent review (section 0), every finding the user approved fixing is fixed, and the validation passes again.

---

## 0. Independent review and fixes (2026-11-13)

Three independent reviewers looked at the work: architecture, correctness and data. Their findings, and what was done about each:

**Engine bugs, all reproduced and fixed, each with a regression test (`test_m2_review_fixes.py`):**

| # | Bug | Fix |
|---|---|---|
| 1 | A buy filled on the day merged shares arrived overwrote them | The fill is rejected as "held" |
| 2 | Merged shares awaiting sale missed the acquirer's split, dividend, delisting or merger | Events reach every holding of the security |
| 3 | Shares converted into an acquirer delisted the same day never exited | Day order: value and share events, then mergers, then delistings |
| 4 | A merger component that listed later was marked at up to 4× its value | Valued from the rest of the parent's last close |
| 5 | An unloaded acquirer with a cash component lost the share value | Credited as cash at the parent's close, with no trading costs |
| 6 | A flat bar after a split was read as a locked circuit | The circuit test uses the previous close adjusted for the day's events |
| 7 | `on_universe_exit: sell` did nothing without membership data | The simulator raises |

The reviewer's random test (3,000 scenarios with real costs and every event kind) now fails 0 times; it failed 20 before. An accounting-identity test over random scenarios is in the suite.

**Corporate-action data errors the reconciliation could not see, now fixed:**
- **Rights premiums written as "Premium Of Rs", "Prm Rs", "Premium 91" or "Partly Paid @"** were not parsed, so the factor was 1.0. The parser is fixed, with a test.
  - IDEA 2019 is now 0.604 (the price fell to 0.629); before, it showed a phantom −37%.
  - ABFRL 2020, VHLTD 2024 and RELTD 2026 are also fixed.
- **Six member events** that the automatic rules set to 1.0 now carry sourced factors in `ca_overrides.csv`, applied by `nsebt build`:
  - demergers under the 3% gap threshold: HINDUNILVR 2025 (Kwality Wall's), GREENPLY 2019, CCAVENUE 2020, IIFL 2017 (5paisa), each at the ex-date open gap, the method used for every demerger;
  - bonus preference shares: TVSMOTOR 2025 (4 × ₹10) and TVSHLTD 2026 (46 × ₹10, approximate).
- **CAIRN's cash** is ₹57.70: ₹40 of preference shares plus the ₹17.70 Vedanta interim dividend paid to Cairn holders.
- **Cross-check against Bloomberg's independently adjusted prices** (`reports/phase1/ca_reconciliation.csv`, rebuilt):
  - IDEA's agreement went from 0.605 to 0.999. The ABFRL, HINDUNILVR 2025 and IIFL steps disappeared, and CCAVENUE improved.
  - For TVSMOTOR's preference-share bonus and GREENPLY, Bloomberg made no adjustment, so a step of 1.2% and 0.9% now appears there. This is a methodology difference: the preference shares and the demerged shares carry real value.

**Validation, restated honestly:**
- Check (a) proves the engine applies the corporate-action table correctly. Both sides read the same table, so it cannot see errors in the table itself; the earlier wording that it catches "missed events and ISIN breaks" overstated it.
- Check (b) is a code-path check.
- **New (c):** every member factor is compared with the actual ex-date price move.
- **New (d):** a full-ledger run holds every member through its conversions and lifecycle exits.

**Architecture and decisions approved by the user:**
- **Run schema v2:**
  - `data_inputs`: hashes of corporate actions, membership, lifecycle, `mergers.csv`, `lifecycle_overrides.csv`, `ca_overrides.csv` and the build report;
  - `signals_hash`;
  - `strategy_source_hash`;
  - the eligibility config and ADV window in the `sim` block;
  - untracked files now count as "dirty".
- **One way in:** `backtest/market.py: from_store(cfg)` builds the market every time, with the corporate actions, mergers, lifecycle (refused if stale) and acquirers loaded as non-members. `SimConfig.from_config` reads every block, including `eligibility`.
- **Eligibility (A4) is enforced by the engine:** member, traded, raw close ≥ ₹10, ADV ≥ ₹1 crore, and the strategy's warm-up mask. Signals may carry `dates`/`ids` labels that must equal the market's. The ADV window must match the eligibility config.
- **Stopped securities:** delisted, suspended-without-resuming and unknown are all held for 60 trading days (`grace_days`), then exit at the stated value or under the delisting policy. Before, known delistings exited at once, which used hindsight to free cash early, and suspended stocks were held forever.
- **`lifecycle.parquet`** is rebuilt on every `nsebt build` and daily update, records its data end, and `from_store` refuses a stale copy.

**Reviewed and left as is:**
- The stops, target and trailing reference are scaled on dividend days. A6 says the trailing reference is "adjusted by factor on corporate-action days".
- The 5 event-days flagged by check (c) are all explained:
  - EASEMYTRIP, NAZARA and VSTIND rallied on their bonus ex-dates;
  - ONGC's ex-date was the 23-Mar-2020 crash;
  - IGARASHI's 25:202 bonus is confirmed by the BSE notice (ex 27-Sep-2018); the price simply didn't fall that day.

**Open, for M3–M4:**
- Variable stop and trail distances (ATR exits) and a `Market.slice` for walk-forward windows, to settle at the M3.1 interface freeze.
- The A12 vectorised random-3 baseline should reuse the engine's per-security hold paths rather than a second ledger.

---

## 1. Tests

| File | Tests | Covers |
|---|---|---|
| Phases 0–3 (`test_audit`, `test_data`, `test_engine`, `test_phase1–3`) | 79 | Unchanged |
| `test_m1_account.py` | 28 | Account limits, validation, prompt, config round trip, CLI |
| `test_m1_costs.py` | 8 | Worked example, buy-only stamp, sell-only DP, GST base, no IPFT key, DP once per scrip per day, slippage |
| `test_m1_sim.py` | 15 | Next-open fills, whole shares, gap and cap rejections, slots, cash limit, order of limits (cap → liquidity → cash), liquidity from past data, sells before buys, max hold, TTL, seeded tie-break |
| `test_m1_execution.py` | 12 | Stops, targets, gaps through either, stop first when both touched, trailing stop, locked circuits by direction, resumption gaps |
| `test_m1_run.py` | 5 | Identical runs for the same config and seed, config hash, frozen Run schema, store version |
| `test_m2_ledger.py` | 20 | Every row of the 2.2 table, fractional bonus, consolidation to cash, split on fill day, T+1 entitlement, cost-basis P&L, trailing stop across a split, unscheduled rows, config |
| `test_m2_receivables.py` | 13 | Dividend receivable and its lag, receivables in sizing but not buying power, instant and ignore modes, rights sold or subscribed, demerger receivable, value events before share events |
| `test_m2_mergers.py` | 19 | Convert then sell, convert and hold, merging into an existing holding, acquirer missing, multi-part conversion with cash, no same-day entitlement to the acquirer's events, table validation, seeding, real HDFC → HDFC Bank, no unknown or unsourced rows |
| `test_m2_lifecycle.py` | 18 | One test per class, every delisting policy, stated delisting value, sourced overrides and their validation, unknown grace period, suspension, truncation, NSE list parsing |
| `test_m2_eligibility.py` | 7 | Each A4 criterion, no future data, fill-day volume has no effect, universe exit hold/sell |
| `test_m2_reconcile.py` | 9 | Reconciliation of every event kind; detection of a missed event, reversed factor, wrong date, wrong prior close; future truncation (3 seeds); real-store sample and speed |

Test names: `.venv/bin/pytest --collect-only -q`.

## 2. Cost worked example (₹12,000 buy, then ₹12,000 sell)

| Charge | Buy | Sell |
|---|---|---|
| STT 0.1% | 12.0000 | 12.0000 |
| NSE transaction 0.00307% (includes IPFT) | 0.3684 | 0.3684 |
| SEBI ₹10/crore | 0.0120 | 0.0120 |
| GST 18% on transaction + SEBI | 0.0685 | 0.0685 |
| Stamp 0.015% (buy only) | 1.8000 | — |
| DP ₹15.34 (sell, once per scrip per day) | — | 15.3400 |
| **Total** | **14.2489** | **27.7889** |

The round trip costs **₹42.0377**, or 0.350% (spec: ₹42.04 ± 0.01). Slippage of 10 bps per side is applied to the fill price, separately from these charges.

## 3. Validation (amendment 2.6)

Run with `nsebt reconcile`. Output: `reports/phase45_reconciliation.md` and its CSVs. Store rebuilt after the data fixes.

| Check | What it proves | Result |
|---|---|---|
| (a) Reinvest-equivalent engine vs total-return adjusted close, every day, 1e-6 | The engine applies the table correctly (not the table itself) | **903 of 903** securities (S4 threshold 0.5%); 1,853,369 security-days; max error 4.4e-16 |
| (b) Receivable mode: value preserved on each ex-date | Receivable code path; table prior close agrees with prices | **8,128 of 8,128** events |
| (c) Table vs prices: combined factor vs ex-date open and close | The table itself, independently | 5 of 784 member event-days flagged, all explained (section 0) |
| (d) Full ledger run with conversions and lifecycle exits | Accounting identity, no negative cash | **Pass**: error 0; 49 conversions, 32 lifecycle exits, 0 warnings |

Membership has 906 securities. Three REITs (EMBASSY, BIRET, BAGMANE; members from October 2026) are not in the EQ/BE/BZ panel.

Mutation tests confirm (a) fails when an event is dropped, a factor is reversed or an effective date moves. A table test confirms (c) flags an IDEA-like rights row.

## 4. Speed

| Step | Seconds |
|---|---|
| Load the Nifty 500 panel (raw prices plus the total-return adjusted series) | 5.3 |
| One backtest: 3 positions, random signals, full costs, 903 securities × 2,530 days | 0.7 |
| **Single backtest including the load via `market.from_store`** (target ≤ 30) | **5.7** |
| Both reconciliation runs, every security held at once | 3.1 + 3.3 |

## 5. What the engine now does with corporate actions

- **Share events (split, bonus, consolidation, inferred split):**
  - Shares are divided by the factor.
  - Stop, target, trailing reference and entry price are multiplied by the factor.
  - Fractions are paid as cash at the ex-date raw close.
  - Events apply at the start of the effective day. A position held at the previous close is entitled; a buy filled at the ex-date open is not.
- **Dividends, rights and demergers:**
  - Value per share = prior close × (1 − factor). It is credited as a receivable: counted in equity and sizing, but not in buying power.
  - The receivable turns into cash after 30 calendar days (dividends), 15 trading days (rights) or 60 calendar days (demergers).
  - Value events are applied before share events on the same day, on the pre-event share count.
- **Mergers:** conversion at the scheme ratio from `mergers.csv` on the first trading day after the parent's last trade. The default is to sell at the acquirer's open that day; `convert_and_hold` is the alternative.
- **Stopped securities:** classified in `data/store/lifecycle.parquet`, rebuilt by `nsebt build` and the daily update (updated after the review).

  | Class | Treatment |
  |---|---|
  | Delisted, suspended (never resumed), unknown | Held 60 trading days (`grace_days`), then exit at the stated value or under the delisting policy (`haircut(50)`; also `last_close`, `zero`) |
  | Truncated (stopped within 5 days of the data end) | Held at the last close |

- **Eligibility (A4):** one shared function covering membership, a trade on the day, warm-up, raw close ≥ ₹10 and 20-session average traded value ≥ ₹1 crore. The simulator's liquidity limit uses the same average. By default (`hold`), removal from the index does not force an exit.

## 6. Findings and decisions needed

1. **Reconciliation method: approved.** Check (a) reinvests dividends to match the total-return adjusted series exactly. Check (b) confirms the receivable engine preserves value on every ex-date. Both are re-run after the changes below: 903 of 903 securities and 8,128 of 8,128 events pass.

2. **Merger ratios: verified and corrected.** Every filled row in `mergers.csv` now cites its source, an NSE filing or, for 6 rows, the company's or press record-date notice.
   - **One error fixed:** ANDHRABANK is 325/1000 (Union Bank's gazette scheme), not 324/1000.
   - **The 10 REVIEW rows resolved from filings:**

     | Parent | Acquirer | Ratio |
     |---|---|---|
     | ABIRLANUVO | Grasim | 15/10 |
     | GRUH | Bandhan | 568/1000 |
     | ALBK | Indian Bank | 115/1000 |
     | EQUITAS | Equitas SFB | 231/100 |
     | UJJIVAN | Ujjivan SFB | 116/10 |
     | TV18BRDCST | Network18 | 100/172 |
     | CAIRN | Vedanta | 1 + ₹40 cash (four ₹10 preference shares, cash exit at par) |
     | PEL | Piramal Finance | 1 |
     | DISHMAN | DCAL | 1 |
     | DALMIABHA | Dalmia Bharat | 2 |

   - **Price checks:** the price-implied ratios differ by more than 4% for some rows. Each case is explained by a listing delay, a holding-company discount, or the acquirer's same-day demerger (ACLGATI → Allcargo).
   - **Engine changes:**
     - A conversion can have several components plus cash: Max India → Max Healthcare + new Max India; Tube Investments → TI Financial Holdings + new Tube Investments.
     - Converted shares are not entitled to the acquirer's own corporate actions on the conversion day.
     - Acquirers outside the Nifty 500 are loaded as non-members, so converted holdings can be held and sold (`load_panel(extra=…)`).

3. **The 54 unexplained members: resolved, 0 unknown remain.**
   - **25 are conversions.** These include 5 capital-reduction relistings under a new ISIN that the store hadn't linked: RUCHISOYA → RUCHI (100:1), ALOKTEXT → ALOKINDS (1:1), SUPPETRO → SPLPETRO (1:1 + ₹6 refund), RNAVAL → SWANDEF (275:1), TUBEINVEST → TIFIN.
   - **22 delisted,** recorded in `lifecycle_overrides.csv` with sources:
     - 17 at a stated value: ₹0 where the equity was extinguished, as with DHFL, SINTEX, RELCAPITAL, SREINFRA, JPASSOCIAT, LAKSHVILAS and the liquidations; ₹0.10 for AMTEKAUTO.
     - 5 under the delisting policy, because no payout is stated: COX&KINGS, ESSDEE, MANPASAND, GLOBOFFS, SPICEJET.
   - **7 suspended, still in insolvency** at the data end: VIDEOIND, JBFIND, BRFL, FRETAIL, FCONSUMER, EROSMEDIA, BALLARPUR. These are held at the last close, as amendment 2.4 requires.
   - **Assumptions:**
     - Liquidations, and ROLTA (shares extinguished, no payment stated), are valued at ₹0.
     - AMTEKAUTO's ₹0.10 is a ₹2.5 crore aggregate divided by an approximate public share count.
     - SPICEJET kept trading on BSE.

4. **40 corporate-action rows have no effective date**, not the 9 the spec states. All are ex-dates after the data ends, except a few that predate it. None is applied. If a held position passes one of these ex-dates, the run logs a warning.

5. **The legacy reports named in A8 are not in the repo.** The holdout seed relies on your statement that momentum and breakout 2022+ results were viewed.

6. **The index file has no TRI.** `indices.parquet` has the price index and a daily dividend yield. M4.1 will try to fetch TRI from niftyindices.com; if that fails, it will use the price index plus the yield, labelled "approximate TRI".

## 7. Approximations, stated

- **Rights:** valued at the theoretical entitlement, (a/b)(TERP − issue price), realised after 15 trading days. There are no `-RE` entitlement prices in the store. `subscribe` mode pays cash and adds the shares on the ex-date.
- **Demergers:** a receivable at the price-implied value, realised after 60 days. There is no child-security table. `table_then_receivable` falls back to the receivable and logs a warning.
- **Mergers:**
  - `convert_then_sell` sells at the acquirer's open on the conversion day.
  - If the acquirer is not in the loaded universe, the holding closes at the parent's last close, with a warning.
- **Delisting:** the policy's share of the last close is credited as cash, with no trading charges.
- **Lifecycle classes are known in hindsight.** "Resumes later" and "unknown → delisted" are decided from the full history, as the spec defines them. This affects only how a stopped holding is valued, never a signal.
- **Same-day events:** a dividend and a bonus on the same ex-date are applied with the dividend on the pre-bonus share count.

## 8. Config added in Milestone 2 (cost constants unchanged)

| Key | Value |
|---|---|
| `corporate_actions.dividends` | `receivable` |
| `corporate_actions.dividend_pay_lag_days` | 30 calendar days |
| `corporate_actions.rights` | `sell_entitlement` |
| `corporate_actions.rights_realise_lag_days` | 15 trading days |
| `corporate_actions.demerger` | `receivable` |
| `corporate_actions.demerger_realise_lag_days` | 60 calendar days |
| `corporate_actions.merger` | `convert_then_sell` |
| `corporate_actions.delisting` | `haircut(50)` |
| `corporate_actions.grace_days` (was `unknown_grace_days`) | 60 trading days, for delisted, suspended and unknown |
| `paths.ca_overrides` | `ca_overrides.csv` (also added to the backup list) |
| `eligibility.min_price` | ₹10 |
| `eligibility.min_adv` | ₹1 crore |
| `eligibility.adv_days` | 20 |
| `eligibility.on_universe_exit` | `hold` |
| `paths.mergers` | `mergers.csv` (also added to the backup list) |
| `paths.lifecycle_overrides` | `lifecycle_overrides.csv` (also added to the backup list) |
