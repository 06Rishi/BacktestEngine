# Gate A: engine validation (Milestones 1–2)

Date 2026-11-13. Spec: `docs/phase45_amendments.md`. Progress log: `reports/phase45_progress.md`.

**Status.** M1.1–M2.6 are complete and pushed. 233 of 233 tests pass in about 5 s.

**Gate A follow-up (2026-11-13):**
- Approved: the reconciliation method (section 6, item 1).
- Resolved from NSE filings: all merger ratios and every stopped Nifty 500 member (items 2 and 3). Details are in `reports/phase45_conversions.md`.
- Milestones 3–5 can start once you confirm the resolutions below.

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

## 3. Reconciliation (amendment 2.6)

Universe: all 903 securities that were Nifty 500 members in 2016–2026, over 2,530 days (store version `f74312a5eb8204af`). Run it with `nsebt reconcile`. The full output is in `reports/phase45_reconciliation.md` and its CSVs.

| Check | Result |
|---|---|
| (a) Reinvest-equivalent engine vs total-return adjusted close: securities with any day outside 1e-6 | **0 of 903** (the S4 threshold is 0.5%) |
| (a) Security-days compared; largest daily error | 1,853,369; 4.4e-16 |
| (b) Receivable mode: value preserved on each ex-date to 1e-6 | **8,128 of 8,128** events (7,517 dividends, 471 special dividends, 75 demergers, 65 rights) |
| Value events with no factor | 0 affect a Nifty 500 holding (of 306 such rows, 271 predate the data, 32 have no effective date, and 2 are other securities) |

The checks are proven to detect errors. Mutation tests fail as expected when an event is dropped, a factor is reversed, an effective date moves by one day, or a prior close is wrong.

## 4. Speed

| Step | Seconds |
|---|---|
| Load the Nifty 500 panel (raw prices plus the total-return adjusted series) | 5.3 |
| One backtest: 3 positions, random signals, full costs, 903 securities × 2,530 days | 0.7 |
| **Single backtest including the load** (target ≤ 30) | **6.0** |
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
- **Stopped securities:** classified in `data/store/lifecycle.parquet` with `nsebt lifecycle [--fetch]`.

  | Class | Treatment |
  |---|---|
  | Delisted | Exit at 50% of the last close (`haircut(50)`). Also available: `last_close`, `zero`. |
  | Unknown | Held for 60 trading days, then treated as delisted. |
  | Suspended, truncated | Held at the last close. |

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
| `corporate_actions.unknown_grace_days` | 60 trading days |
| `eligibility.min_price` | ₹10 |
| `eligibility.min_adv` | ₹1 crore |
| `eligibility.adv_days` | 20 |
| `eligibility.on_universe_exit` | `hold` |
| `paths.mergers` | `mergers.csv` (also added to the backup list) |
| `paths.lifecycle_overrides` | `lifecycle_overrides.csv` (also added to the backup list) |
