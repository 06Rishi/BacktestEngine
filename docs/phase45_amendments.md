# Phase 4–5 amendments and working protocol

This file amends the Phase 4–5 plan. Where the two conflict, this file wins. Milestone 0 folds everything here into `plan.md`.

Sources: an external review, a second review, and findings from the Phase 0–3 code itself. Every item says what to build and how to test it. Nothing outside this list is added to scope.

---

## Part 1. Working protocol (how the work is run)

### 1.1 Mini-checkpoints

Every milestone is split into the mini-checkpoints listed in Part 4. Each mini-checkpoint is roughly one module, or one behaviour, plus its tests. For each one:

1. Write the tests first, or alongside the code. A behaviour without a test isn't done.
2. Run the **whole** test suite, not just the new tests.
3. When everything passes, commit with a message that names the mini-checkpoint (e.g. `M1.3 sim: fills, stops, targets`). Push at the end of every milestone.
4. Append one line to `reports/phase45_progress.md` and print the same line in chat:
   `[M1.3 sim fills] 131/131 tests pass | +14 new | commit abc1234 | notes: …`
5. Continue to the next mini-checkpoint without waiting for approval, unless a stop rule below applies.

Ordinary bugs (a wrong index, an off-by-one, a missing dtype cast) get fixed, tested and logged in the progress file's notes column. They aren't reported as problems.

### 1.2 Structural stop rule

**Stop all work, write a structural-issue report, and wait for the user** if any of these happens:

| # | Trigger |
|---|---|
| S1 | The same test, or the same root cause, still fails after **3 genuinely different** fix attempts. Re-running or tweaking the same idea doesn't count as a new attempt. |
| S2 | A fix would change a **frozen contract**: the store schema, `Panel` fields, `corporate_actions.parquet` semantics (`factor` direction, `applied`, `kind`), the `Strategy` interface after M3.1, the `Run`/ledger schema after M1.5, or the behaviour of any Phase 0–3 module (beyond a bug fix that comes with a test). |
| S3 | A fix would weaken or delete an existing test, loosen a tolerance by more than 10×, or add a special case for a named security, date or symbol. |
| S4 | A real-data invariant fails above its threshold. Example: more than 0.5% of securities fail the ledger-vs-adjusted reconciliation in M2.6. |
| S5 | Speed stays more than 3× over target after one optimisation pass. |
| S6 | A finding contradicts a decision recorded in `plan.md` or in this file. |
| S7 | Data the plan relies on turns out not to exist or not to be reachable (TRI endpoint, delisted/suspended lists), **and** the documented fallback also fails. |

The report goes in `reports/structural/YYYY-MM-DD_<slug>.md`:

```
# Structural issue: <one line>
Trigger: S1–S7 (which one)
Where: module / test / mini-checkpoint
Symptom: what fails, with the exact error or numbers
Attempts: each fix tried and why it failed (one line each)
Root-cause hypothesis: what is actually wrong, and the evidence
Options: 2–3 ways forward, each with cost, risk and what it changes
Recommendation: one option, and why
Blocked: what cannot proceed until this is decided; what can proceed safely
```

After writing it, say in chat in one or two sentences what broke and what decision is needed. Then stop. Work that the issue doesn't block may continue only if the report lists it under "can proceed safely" **and** the user agrees.

### 1.3 Planned hard stops (gates)

- **Gate A, after Milestone 2.** The engine is validated on synthetic data and reconciled against the real store. Show: the full test list, the cost worked examples, the M2.6 reconciliation across all securities, and the speed. Nothing in Milestones 3–5 starts until the user approves Gate A, because everything downstream trusts this engine.
- **Gate B, after Milestone 5.** The planned Phase 4–5 checkpoint with the full reports.

### 1.4 Rules that hold throughout

- No scope beyond this file and the plan. Ideas go in an "Ideas, not done" section of the progress file.
- No references to code-generation tools anywhere: code, docs, commit messages or reports.
- Every report states its assumptions and approximations explicitly. Nothing is approximated silently.
- Before each commit, print the cost constants and any other config value that changed.

---

## Part 2. Corporate actions and security lifecycle (textbook defaults)

### 2.1 Principle: two ledgers that never mix

This is the standard design (CRSP-style cumulative adjustment factors for research series; a share-and-cash position ledger for simulation, as in zipline and backtrader).

1. **Signals** use back-adjusted prices (`load_panel` adjusted fields). These already exist and are not changed.
2. **Execution and accounting** use raw prices plus a **position ledger**. The ledger applies each corporate action to share counts, cash and receivables on the ex-date.

Signals never read the ledger. So none of the accounting choices below can change a signal. They change only P&L, when cash becomes available, and therefore whether a later entry fits (the realistic "no cash" rejection).

**Entitlement under T+1:** a position held at the close of the day before the ex-date is entitled. A buy that fills at the ex-date's open is not.

### 2.2 Factor convention (already defined in `corpactions.py`; make the engine test it)

`factor` multiplies prices **before** the ex-date:

| Event | factor | Shares | Stop, target, trailing reference |
|---|---|---|---|
| Split, face value ₹10 → ₹2 | 0.2 | × 5 (÷ factor) | × 0.2 |
| Bonus 1:1 | 0.5 | × 2 | × 0.5 |
| Bonus 1:2 | 2/3 | × 1.5 | × 2/3 |
| Consolidation 10:1 | 10 | ÷ 10 | × 10 |

- **Fractional entitlements** (e.g. 3 shares × 1.5 = 4.5): keep the whole shares and credit the fraction as cash at the ex-date raw close (cash in lieu, as the company does).
- Apply only rows with `applied == True`. Include `inferred_split`.
- Rows with no `effective_date` (9 today) are skipped and flagged if a position is affected.
- Pending orders hold a rupee amount, so they need no adjustment. Their stop and target fractions are applied to the fill price, as in the plan.
- **Tests:** each row of the table, one fractional bonus, and a split on the same day a pending order fills.

### 2.3 Defaults per event kind

| Event | Default treatment | Config key and options | Why it's safe |
|---|---|---|---|
| Split / bonus / consolidation / inferred split | As in 2.2 | none | Exact |
| Ordinary dividend | **Receivable** on the ex-date: counts in equity, not in buying power. Paid as cash `dividend_pay_lag_days` later (default **30 calendar days**, the Companies Act limit for paying a declared dividend). | `dividends: receivable | instant | ignore` | Signal-neutral. Stops dividend cash being spent before it arrives. |
| Special dividend (≥5% or labelled special) | Same as ordinary: receivable, then cash. Already a price factor in the adjusted series. | as above | Not double-counted: raw price drop plus the cash receivable = the adjusted path |
| Rights | Holder receives entitlements, **assumed sold**. Receivable = shares × (a/b) × max(0, TERP − issue price). Realised `rights_realise_lag_days` later (default **15 trading days**, about the entitlement trading window). If the entitlement security (`-RE`) exists in the store, use its traded price instead and say so. Out-of-the-money rights → 0 (factor already 1). | `rights: sell_entitlement | subscribe | ignore` | Value-preserving: b·TERP + a·(TERP − I) = b·C. Uses no extra cash. |
| Demerger / scheme of arrangement / capital reduction | Textbook: receive child shares at the scheme ratio. The data has no ratio or child mapping, so the **default is a receivable** = shares × prior_close × (1 − factor). It's a non-cash asset, realised as cash `demerger_realise_lag_days` later (default **60 calendar days**). Flagged as an approximation in every report it touches. **Upgrade path:** `demergers.csv` (parent SecurityID, ex-date, child SecurityID, ratio). When a row exists, the child becomes a real holding, sold at its first listing open. It doesn't use a strategy slot. | `demerger: receivable | table_then_receivable` | Value-preserving with the price-implied factor. Never usable as cash early. |
| Merger / amalgamation (stock for stock), **missing from the plan** | Convert shares into the acquirer at the swap ratio on the effective date. Ratios come from `mergers.csv` (manual), seeded with the announcements whose subject mentions amalgamation or merger, plus every Nifty 500 member that stopped trading in 2016–2026. Strategy positions: convert, then sell at the acquirer's next open (`merger_exit`). Buy-and-hold baselines: keep holding the acquirer. | `merger: convert_then_sell | convert_and_hold` | Otherwise a merged stock (e.g. HDFC Ltd into HDFC Bank, 2023) is treated as delisted and hit with a 50% haircut. The random-3 baseline holds stocks for years and will run into this. |
| Buyback | Ignored (tender is optional). | none | Standard |
| ISIN or symbol change | Continuity through `SecurityID`. | none | Already in the store |

### 2.4 Securities that stop trading (replaces the 20-day delisting rule)

Every security whose last trade is before the end of the data gets classified once, into `data/store/lifecycle.parquet`. This is a new store output, not a change to existing files.

| Class | How it's identified | Engine treatment |
|---|---|---|
| `merged` | Row in `mergers.csv` | Convert, as in 2.3 |
| `delisted` | On NSE's published delisted list (probe the endpoint; cache it like the reference files) | Exit under `delisting: haircut(50) | last_close | zero`, with every policy's result shown in the report |
| `suspended` | On NSE's suspended list, **or** the security resumes trading later in the data | Hold; marked at the last raw close; stops can't fill; exit at the first open after trading resumes |
| `truncated` | Last trade within 5 trading days of the end of the data | Marked at the last close. **Not** a delisting. |
| `unknown` | None of the above | Treated as suspended for up to `unknown_grace_days` (default 60 trading days), then as delisted. Always flagged. |

If the NSE lists can't be reached (S7 applies only if this fallback also fails): classify from the data (resumes later → `suspended`; otherwise `unknown`), and list every `unknown` Nifty 500 member in the M2 report so the user can fill `mergers.csv` or decide.

### 2.5 Index removal (separate from exits)

`on_universe_exit: hold | sell`, default **`hold`**. Universe membership decides **entry eligibility only**. Holdings exit through the strategy's rules. Ranking strategies that rebalance (momentum) drop removed stocks naturally at the next rebalance, because removed stocks aren't ranked.

### 2.6 The reconciliation invariant (the textbook validation, M2.6)

For **every** Nifty 500 security, a costless, slippage-free buy-and-hold through the engine (1 share-equivalent, dividends as receivables counted in equity) must reproduce the security's **total-return adjusted** path (`kinds = PRICE_KINDS + dividend`). Tolerance: 1e-6 per day, except:

- known fractional cash-in-lieu (bounded and reported),
- days flagged as approximations (rights at theoretical value, demerger receivables), which are listed.

**Threshold:** more than 0.5% of securities failing outside the flagged days → S4.

This one test catches factor-direction errors, double-counted dividends, missed events and ISIN-continuity breaks, all at once.

---

## Part 3. Other amendments

Each item lands in the milestone shown in brackets.

### Signals and the expression language

**A1. Warm-up must use first-valid rows, not cache exactness [M3].** `REGISTRY.lookback` is the history the **cache** needs for an exact restart: 25×n for exponential averages, so about 5,000 rows for EMA(200) and about 875 for MACD. Using it as signal warm-up would delay `macd_trend_pullback` by about 3.5 years and stop EMA(200) strategies trading at all.
- Signal warm-up = the first row where every node in the expression tree is non-NaN for that security, plus an optional `warmup_buffer` (default 0).
- **Test:** `macd_trend_pullback`'s first possible entry is in 2017 (SMA(200) needs 200 bars from 2016-08-29), not 2019–2020.

**A2. `highest`/`lowest` include today [M3].** `close > highest(high, 20)` can never be true.
- Add the helpers `prior_high(n)` = `shift(highest(high, n), 1)` and `prior_low(n)`.
- The parser **warns** whenever a field is compared with `highest`/`lowest` of the same or a containing field (close vs high) without a shift.
- `breakout.yaml` uses `close > prior_high(20)` and `close < prior_low(10)`, matching the legacy `.shift(1)`.

**A3. Adjusted vs raw price levels [M3].**
- Add `raw_close`, `raw_open`, `raw_high` and `raw_low` as fields.
- Warn when an adjusted price field is compared with a numeric constant (e.g. `close > 100`), because adjusted levels encode later splits.
- `min_price` filters always use raw prices.

**A4. Eligibility, defined once and shared [M1, used by M4].** A security is eligible on day t when all of these hold:
- it's a point-in-time member on t;
- it traded on t (series EQ/BE/BZ);
- the strategy's warm-up is satisfied (A1);
- raw close ≥ `min_price` (default ₹10);
- the 20-day average traded value up to t is at least `min_adv` (default ₹1 crore).

The same function feeds strategy entries, the selection and timing placebos, and the random-3 baseline.

**A5. `sector()` uses today's industry tag [M3].** Look-ahead in classification. Former members fall back to the Nifty 500. Every report that uses `sector()` says so.

### Execution

**A6. Execution rules the plan left open [M1].**

- **Equity for sizing:** marked at the decision-day raw close, cash + holdings + receivables. Buying power = cash only.
- **Locked bars, direction-aware.**
  - Locked at the upper circuit (open = high = low = close, gap up): buys can't fill; sells can.
  - Locked at the lower circuit (the same, with a gap down): sells can't fill; buys can.
  - Unfilled orders retry daily, are logged, and the position is marked at the raw close.
  - `max_unfilled_days` defaults to unlimited for sells. Buys are cancelled after 1 day (`buy_order_ttl_days: 1`).
- **Gap through target:** if the open is at or above the target, fill at the open (a limit sell gets the better price).
- **Gap through stop:** fill at the open.
- **Stop and target both touched intrabar:** the stop fills first.
- **Trailing stop:** the reference is the highest **raw close** since entry, updated at the close and active from the next day. Using closes avoids guessing the order of prices within the bar. It's adjusted by `factor` on corporate-action days.
- **Slippage:** basis points **per side**. Default **10 bps** buy and 10 bps sell. Sensitivity runs at 0, 10 and 25 bps per side.

**A7. Cost constants [M1].** All in config, with the source and date checked in a comment. Source: Zerodha support, "What are transaction charges and how do NSE, BSE, and MCX calculate it?", checked 2026-11-13.

| Charge | Default | Note |
|---|---|---|
| STT | 0.1% buy and sell | |
| NSE transaction charge | **0.00307%** (₹3.07 per lakh), buy and sell | Equity cash segment. **Includes NSE's IPFT charge**, so there is no separate IPFT line. Don't add IPFT on top of this. |
| SEBI | ₹10/crore | |
| Stamp duty | 0.015%, buy side only | The uniform rate dates from July 2020. Apply it to the whole history and document that. |
| GST | 18% on brokerage + transaction + SEBI | |
| DP charge | ₹15.34 per scrip per sell day | |

**Worked-example test:** a ₹12,000 buy then a ₹12,000 sell costs **₹42.04**: STT ₹24.00, transaction ₹0.74, SEBI ₹0.02, GST ₹0.14, stamp ₹1.80, DP ₹15.34. That is 0.350%, and the DP charge accounts for about 0.13% of it. The test asserts ₹42.04 ± ₹0.01, and the config has no `ipft` key.

### Research framework

**A8. Holdout status is already partly spent [M4, recorded in M0].** Reports already in the project (`backtests/momentum-nifty500-2016-2026.md`, `backtests/breakout-nifty500-2016-2026.md`) show 2022–2026 results for momentum (6 variants) and breakout, on the legacy Bloomberg engine.
- Seed the ledger with those views: `oos_viewed = true`, `source = legacy_bloomberg`, date 2026-11-12. Reports for these two strategies must state "2022+ is not an untouched holdout."
- `macd_trend_pullback` has never been run, so its 2022+ period is still clean.
- The genuinely unseen period for all three is **forward data after the Gate B code freeze**, i.e. Phase 7 paper trading. Record the freeze commit and date in the ledger.
- Once `--final` has been used for a strategy, later parameter changes to that strategy are flagged "selected after holdout viewed".

**A9. Fixing the sanity check [M4].** Delete "random-entry placebo ≈ random-3 baseline minus costs."
- Engine correctness is proved by the synthetic tests and the reconciliation (2.6).
- Each placebo is checked against its own construction: same slots, eligibility (A4) and holding-length distribution.
- Report the placebo vs baseline gap; don't treat it as a failure.

**A10. Undefined choices, now defined [M4].**
- **Walk-forward:** select by **net-of-cost Sharpe** in each 3-year window. Ties go to the parameter set closest to the centre of the profitable region.
- **Sweep "profitable cell":** net CAGR above the Nifty 500 TRI CAGR over the same window, not just above zero.
- **Island warning:** fewer than 30% of the neighbouring cells profitable.

**A11. Multiple testing [M4, small].** Alongside the variant count, show the **deflated Sharpe ratio** (Bailey & López de Prado), using the ledger's variant count for that strategy family and the skew and kurtosis of the strategy's returns. One function, one test, one report line. Nothing more.

**A12. Baseline costs [M4].**
- The TRI index lines are **theoretical** and shown without trading costs.
- Executable baselines (random-3, first picks held, the member-based equal-weight reconstruction) carry the full Zerodha costs.
- **Random-3 speed:** compute the 1,000 draws through a vectorised buy-and-hold path (the adjusted total-return path, plus entry costs, lifecycle events and exits). Check it against the full engine on 20 draws: the results must match to 1e-6.

**A13. Tax [M4].** STCG 15% before 2024-07-23 and 20% from then on. LTCG (holding over 12 months) 10% before and 12.5% after, above an annual exemption of ₹1 lakh (₹1.25 lakh from FY 2024-25). Plus:
- 4% cess;
- FIFO lot matching;
- short-term losses offset short- and long-term gains; long-term losses offset long-term gains only; unused losses carry forward 8 years.

Config allows `business_income` at a slab rate instead. It's always labelled an estimate and never changes engine results.

**A14. Legacy results are not comparable [M5].** The legacy momentum report held the top 20 stocks on Bloomberg prices. The new momentum strategy holds the top 3 under the account limits. The Phase 4–5 report says so, and doesn't present the old 27% CAGR as a reference point.

**A15. Full-report runtime [M5].**
- A single backtest takes 30 seconds or less.
- A full report (200 placebo runs, 2,000 bootstrap resamples, 3 slippage reruns, baselines, walk-forward) targets **10 minutes or less** per strategy.
- All the counts are in config.

---

## Part 4. Milestones as mini-checkpoints

| ID | Mini-checkpoint | Done when |
|---|---|---|
| **M0** | Rewrite `plan.md` (plan + this file); record holdout status (A8) | `plan.md` committed; this file kept as `docs/phase45_amendments.md` |
| M1.1 | `account.py` + `nsebt account` prompt | Validation and mocked-input tests pass |
| M1.2 | `costs.py` (A7) | The worked example gives ₹42.04 ± ₹0.01; IPFT isn't charged separately; DP charged once per scrip per sell day |
| M1.3 | `sim.py` core: next-open fills, sells before buys, rupee orders, whole shares, cap → liquidity → cash order, slot limit, rejected log | Hand-built series tests pass |
| M1.4 | Stops, targets, trailing stop, gap rules, locked bars (A6) | Tests for each rule pass |
| M1.5 | `run.py`: Run record (commit, store version, config hash, seed); **freezes the Run schema** | Same config + seed gives an identical run |
| M2.1 | Corporate-action ledger (2.2, 2.3) with config switches | Every row of the 2.2 table, fractional cash, split on fill day |
| M2.2 | Dividend, rights and demerger receivables | Value preserved on the ex-date; cash appears only after the lag |
| M2.3 | Mergers: `mergers.csv` seeding + conversion | A synthetic merger, plus a real HDFC → HDFC Bank check |
| M2.4 | `lifecycle.parquet` + suspension/delisting/truncation handling (2.4) | One test per class; every policy |
| M2.5 | Eligibility (A4), universe exit (2.5), liquidity limit using past data only | Changing the fill-day volume doesn't change results |
| M2.6 | **Reconciliation across all securities (2.6)** + no-look-ahead on the engine + speed | Under 0.5% of securities fail; truncation test passes; ≤30 s |
| **Gate A** | **Stop and report** | User approves |
| M3.1 | `base.py` Strategy interface (**freezes it**) + registry | A Python strategy runs end to end |
| M3.2 | `expr.py` tokenizer and parser, errors with file:line:col | Precedence, errors, no `eval`, shift sign rule |
| M3.3 | Evaluation through FeatureCache, A1 warm-up, A2/A3 lints | No look-ahead in the rule engine; first-entry date test |
| M3.4 | `spec.py` YAML schema, params, `--set` overrides | Bad overrides rejected, with clear messages |
| M3.5 | The three example strategies (momentum as YAML and Python) | YAML and Python momentum produce identical trades |
| M4.1 | TRI fetch (or approximate TRI) + `baselines.py` (A12) | Synthetic hand-computed checks; vectorised path matches engine |
| M4.2 | `metrics.py` + `ranges.py` | Hand-computed metrics; range slicing tests |
| M4.3 | `ledger.py` + holdout lock + seed rows (A8) | `--final` recorded; view counts correct |
| M4.4 | Placebos (A9), block bootstrap, slippage sensitivity | Placebo construction tests |
| M4.5 | Walk-forward + sweep + island warning (A10) + deflated Sharpe (A11) | Synthetic tests with known answers |
| M4.6 | `tax.py` (A13) | Worked FIFO and set-off examples |
| M5.1 | Run folders, charts, CLI, legacy removal | CLI smoke tests |
| M5.2 | Full runs of the three strategies + `reports/phase45_report.md` + README section | Reports complete, A14 note included |
| **Gate B** | **Stop and report** | — |
