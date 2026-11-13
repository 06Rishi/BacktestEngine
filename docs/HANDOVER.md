# Handover: state of work and how to resume

Last updated 2026-11-13.

**Where things stand**
- Milestones 1–3 are complete.
- Gate A has been reviewed by the user and by three independent reviewers. Every approved fix is in; see `reports/gateA_report.md`, section 0.
- Milestone 3 (the strategy layer) was then verified by two more independent reviewers. Every confirmed issue is fixed, with a regression test. See `reports/phase45_progress.md`, line "M3 review fixes".
- **Next step: M4.1** (TRI fetch or approximate TRI, and baselines).

**Repository:** everything is committed and pushed to `origin/main` (private repo `github.com/06Rishi/BacktestEngine`). 365 of 365 tests pass.

## 1. Read first, in this order

1. `docs/phase45_amendments.md`: **the only authoritative spec for Phases 4–5.** It holds:
   - the working protocol and stop rules;
   - the corporate-action rules and cost constants;
   - the mini-checkpoint list (Part 4).
2. `reports/gateA_report.md`: engine validation, findings, the user's decisions, and the stated approximations.
3. `reports/phase45_progress.md`: one line per finished mini-checkpoint.
4. `reports/phase45_conversions.md`: every Nifty 500 member that stopped trading, with its outcome and source.
5. `plan.md`: the overall product spec (Phases 0–7). Update its status table as milestones finish.
6. `README.md`: setup, daily workflow, store layout.

## 2. Standing rules from the user

- **No references to code-generation tools or assistants** anywhere: code, docs, reports, commit messages.
  - No `Co-Authored-By` or "Generated with" lines.
  - Search the tree for such names before every commit.
- **Protocol for every mini-checkpoint** (amendments Part 1):
  1. Write the tests.
  2. Run the **whole** suite.
  3. Commit with a message naming the checkpoint.
  4. Append a line to `reports/phase45_progress.md` and print the same line in chat:
     `[M3.1 strategy base] 240/240 tests pass | +7 new | commit abc1234 | notes: …`
  5. Push at the end of every milestone.
  6. Continue without waiting unless a stop rule applies.
- **Stop rules S1–S7:** write `reports/structural/YYYY-MM-DD_<slug>.md`, say in one or two sentences what broke, and stop.
- **Gate B** (after M5.2) is a hard stop that needs the user's approval.
- **Config changes:** before each commit, print the cost constants and any config value that changed.
- **Scope:** nothing beyond the spec. Ideas go in an "Ideas, not done" section of the progress file.
- **Backups:** the user deferred the backup destination. Don't raise it again unless asked.
- **The user:**
  - A retail swing trader. Default account: ₹36,000, 3 positions, 33% cap.
  - Wants concise, professional documentation.
  - Wants data resolved from primary sources: NSE filings first, then company notices, then the press. Cite the source.

## 3. Environment

- **Python environment:** Python 3.12 venv via `uv` (`.venv/`). The system Python is 3.14, so always use `.venv/bin/python` / `.venv/bin/pytest`.
- **Tests:** `.venv/bin/pytest -q` (365 pass, about 35 s; the real-store strategy tests take most of it). Some tests read the real store and are skipped if it hasn't been built.
- **Data (not in git):**
  - `data/nse/raw`, including:
    - `announcements/`: NSE corporate filings per symbol, cached JSON;
    - `filings_text/`: text of the attached filing PDFs used to verify ratios and outcomes.
  - `data/nse/reference`, including `delisted_*.csv` and `suspension_*.csv`.
  - `data/store`, including `lifecycle.parquet`.
  - `dataextracted/DE`.
- **Commands:**

  | Command | What it does |
  |---|---|
  | `nsebt update` | Daily update, after 19:00 IST |
  | `nsebt account` | Set the account limits |
  | `nsebt mergers --seed --check` | Seed the conversion table and check ratios against prices |
  | `nsebt build` | Rebuild the store; applies `ca_overrides.csv` and rebuilds `lifecycle.parquet` |
  | `nsebt lifecycle [--fetch]` | Rebuild `lifecycle.parquet` alone; `--fetch` downloads the NSE lists first |
  | `nsebt reconcile` | Validation checks (a)–(d) plus timing; writes `reports/phase45_reconciliation*` |

- **Speed:** `market.from_store` loads in 5.1 s; one backtest takes 0.6 s, so 5.7 s including the load (target ≤ 30 s).

## 4. What exists (Phase 4–5 code: `nsebt/nsebt/backtest/`)

| Module | Role |
|---|---|
| `account.py` | User-set limits, validated. Capital is capped at ₹1,000,000,000. |
| `costs.py` | Zerodha delivery costs. A ₹12k round trip costs ₹42.0377. |
| `sim.py` | The simulator. See section 5. |
| `ledger.py` | Corporate-action events, `LedgerConfig`, and the pure helpers |
| `mergers.py` | The `mergers.csv` loader and seeding |
| `eligibility.py` | A4 eligibility, plus `rolling_adv`, shared with the simulator's liquidity limit |
| `reconcile.py` | The M2.6 checks: (a) reinvest-equivalent, (b) value preservation |
| `market.py` | **`from_store(cfg)`, the only way to build a market.** Loads the panel plus acquirers as non-members, the corporate actions, mergers and lifecycle (refused if stale), and hashes every input into `Market.sources`. |
| `run.py` | The Run record, **frozen at v2** (approved by the user). v2 adds `data_inputs` (input-file hashes), `signals_hash` and `strategy_source_hash`; the eligibility config and ADV window are in `sim`; untracked files count as dirty. Changing it again triggers S2. |

**Data layer additions:**
- `nsebt/nsebt/lifecycle.py` classifies securities that stopped trading and writes `lifecycle.parquet`.
- `nsefetch.fetch_announcements` and `LIFECYCLE_FILES` fetch NSE's filings and its delisted/suspension lists.
- `data.load_panel(extra=…)` loads further securities that are never members, such as merger acquirers.

**Manual data files** (tracked; every row cites a source):

| File | Contents |
|---|---|
| `mergers.csv` | 49 parents and 51 component rows: mergers, schemes and capital-reduction relistings. Multi-part rows are supported, plus a `cash` column. |
| `lifecycle_overrides.csv` | 22 delisted, 17 of them with a stated value (₹0 where the equity was extinguished), and 7 suspended |
| `ca_overrides.csv` | Sourced factors for 6 member events the automatic rules set to 1.0: 4 demergers under the 3% gap threshold, and 2 bonus-preference-share schemes. Applied by `nsebt build`. |

There are 0 `unknown` Nifty 500 members.

**Legacy code still present**, to be removed in M5.1:
- `nsebt/nsebt/engine/`
- `nsebt/nsebt/strategies/{breakout,momentum}.py`
- the `backtest`/`signals` CLI paths

`panel.py` and `bloomberg.py` stay.

## 5. Simulator facts that M3+ relies on

- **Inputs:**
  - `Market`: build it with `market.from_store(cfg)` for real runs. `from_frames`/`from_panel` are for tests.
  - `Signals`: entry, exit, score, stop, target, trail, max hold, sizing, an optional `warmup` mask, and optional `dates`/`ids` labels that must equal the market's.
  - `SimConfig.from_config(cfg.raw)`: account, costs, `ledger` (corporate_actions block), `eligibility` (A4 floors, `adv_days`, `on_universe_exit`).
- **Each day:**
  1. Corporate actions, applied to every holding including merged shares awaiting sale: receivables due become cash; value events, then share events; then **mergers** (converted shares are not entitled to the acquirer's same-day events); then **delistings** (which also reach shares converted that day).
  2. Sells, then buys, at the raw open.
  3. Stops and targets.
  4. Mark at the close: pay cash in lieu, apply injections.
  5. Decide exits and entries.
- **Equity** = cash + holdings + receivables. Buying power is cash only.
- **Trade P&L** uses `cost_basis` and `income`.
- **Entry eligibility (A4) is enforced by the simulator:** member, traded, raw close ≥ `min_price`, ADV ≥ `min_adv`, plus the strategy's `warmup` mask. Acquirers are never members, so they're never eligible. A buy never overwrites an existing holding; it is rejected as "held".
- **Stopped securities:** delisted, suspended-without-resuming and unknown are held for `grace_days` (60 trading days), then exit at the stated value or under the delisting policy (user decision at the Gate A review).
- **The locked-circuit test** uses the previous close adjusted for the day's corporate actions.
- **Outputs:** `Result.ledger` logs every corporate action applied to holdings; `daily` has a `receivables` column.
- **Test gotchas:**
  - In `_simkit.market()`, a bare number c becomes the bar (c, 1.01c, 0.99c, c). A flat bar that gaps from the previous close is read as a locked circuit.
  - `_simkit.cfg()` turns the A4 floors off and uses a 1-day ADV window, to match `market()`.

## 6. Gate A outcome (details in the report)

1. **Validation** (`nsebt reconcile`; the method was approved by the user):

   | Check | What it proves | Result |
   |---|---|---|
   | (a) | The engine applies the table correctly (not the table itself) | 903 of 903 securities |
   | (b) | The receivable code path | 8,128 of 8,128 events |
   | (c) | The table against prices | 5 of 784 member event-days flagged, all explained |
   | (d) | The full ledger with conversions and lifecycle | Identity exact, 0 warnings |

2. **Merger ratios:** all verified against filings. ANDHRABANK corrected to 325/1000; the 10 REVIEW rows resolved.
3. **Stopped members:** all 54 previously unknown resolved: 25 conversions, 22 delisted, 7 suspended.
4. **Independent review:** 7 engine bugs and the data errors fixed (IDEA rights, 4 sub-3% demergers, 2 bonus-preference schemes, CAIRN ₹17.70). Bloomberg's adjusted prices confirm the fixes.
5. **Settle at the M3.1 freeze:** variable stop and trail distances (ATR exits), and `Market.slice` for walk-forward windows (keeping the ADV warm-up). For A12, reuse the engine's per-security hold paths instead of writing a second ledger.
6. **Still open, to report again at Gate B:**
   - 40 corporate-action rows have no effective date (the spec says 9); none is applied.
   - The A8 legacy reports are missing.
   - There is no TRI in `indices.parquet`. M4.1 will probe niftyindices.com and fall back to an "approximate TRI".

## 7. The strategy layer (Milestone 3, `nsebt/nsebt/strategy/`)

| Module | Role |
|---|---|
| `base.py` | **Frozen interface.** `Strategy.generate(Context) -> Output` (entry, exit, score, stop/target/trail as fractions of the close fixed at entry, warmup, max hold, sizing, notes). `to_signals` aligns by label. `rank_desc`, `rebalance_days` (the last row counts only if the next business day starts a new period). |
| `expr.py` | Tokenizer and recursive-descent parser, no `eval`. Errors carry file:line:col. `shift(x, n)` needs n ≥ 1. |
| `evaluate.py` | Evaluation through the FeatureCache, the A1 warm-up, and the A2/A3 lints. See its docstring for names and functions. |
| `spec.py` | The YAML schema, `params` and `--set`, `rebalance`, sizing, and a dry run on a synthetic panel. |
| `library.py` | Python reference strategies (`momentum`). |
| `runner.py` | `backtest(cfg, strategy, start, end)`: computes signals on the full history and runs a `Market.slice` window. **It refuses to include the holdout (`research.holdout_start`, 2022-01-01) until M4.3's `--final` exists.** |

**Reference strategies** are in `strategies/`: `macd_trend_pullback.yaml`, `breakout.yaml` and `momentum.yaml`, plus Python `momentum`. The YAML and Python momentum give identical trades. Check a strategy with `nsebt strategy check <file> [--set k=v] [--sample N]`.

**Language rules** (settled by the review):
- `and`, `or` and `not` take conditions only, never numbers.
- A condition is true only where its data is defined. Missing data never satisfies `not` or `or`.
- Arithmetic on conditions is an error.
- Warm-up comes from entry, rank and stop/target/trail expressions; exits don't delay entries.
- Stop, target and trail are price distances; a constant one is linted.
- A YAML `[a, b]` list splits at commas, so quote expressions that contain them, or use a `- ` list.

**Known and documented, not changed:**
- Cash can dip slightly below zero when a tiny residual order (1 share) is sold below its fixed ₹15.34 DP charge. A broker debits the same, and no buy is placed without cash.
- Ties at the `top_n` cut-off are settled by the engine's seeded tie-break.
- Momentum sells a held stock that is no longer ranked: removed from the index (2.5), or not traded on the rebalance day while its momentum is still defined.

## 7b. Next: Milestone 4

| Checkpoint | Scope |
|---|---|
| M4.1 | TRI fetch (or an approximate TRI) and `baselines.py` (A12). For random-3, reuse the engine's per-security hold paths instead of writing a second ledger. |
| M4.2 | `metrics.py` and `ranges.py` |
| M4.3 | `ledger.py`, the holdout lock and the seed rows (A8). Replaces the runner's interim refusal. |
| M4.4 | Placebos (A9), block bootstrap, slippage sensitivity |
| M4.5 | Walk-forward (use `Market.slice`), sweep, island warning, deflated Sharpe |
| M4.6 | `tax.py` (A13) |

## 8. Decisions recorded with the user

- **Strategies:** YAML files with `params` and `--set k=v` overrides, plus Python classes behind the same interface.
- **Chart range dropdown** (1D … All): sets how far back the chart looks; each bar is one trading day. `ranges.py` comes in M4.2.
- **Baselines:**

  | Baseline | Costs |
  |---|---|
  | Nifty 500 / Nifty 50 TRI | none |
  | Random 3 held (1,000 draws, vectorised, checked against the engine on 20) | full |
  | First picks held | full |
  | Official Nifty500 Equal Weight (labelled as a price index) | none |
  | Member-based equal-weight reconstruction | full |

- **Out-of-sample:** 2022 onwards stays locked until a run passes `--final`. Views are recorded, and the deflated Sharpe ratio is shown.
- **Tax:** a separate estimate (A13); it never changes engine results.
- **Reconciliation:** method (a) + (b), approved at Gate A; (c) and (d) added after the review.
- **Run schema v2, corporate-action fixes, 60-day grace for every stopped class, and the delisting policy for suspended stocks:** approved by the user at the Gate A review.
- **Conversions:** primary sources only. A ratio that disagrees with prices is kept when the filing says so, and the gap is explained in the report.
