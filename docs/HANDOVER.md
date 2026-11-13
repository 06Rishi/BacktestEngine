# Handover: state of work and how to resume

Last updated 2026-11-13.

**Where things stand**
- Milestones 1 and 2 are complete, and Gate A has been reviewed.
- The user approved the reconciliation method.
- The open Gate A items (merger ratios, stopped Nifty 500 members) were resolved from NSE filings.
- **Next step: M3.1**, once the user confirms the Gate A resolutions in `reports/gateA_report.md`, section 6.

**Repository:** everything is committed and pushed to `origin/main` (private repo `github.com/06Rishi/BacktestEngine`). 233 of 233 tests pass.

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
- **Tests:** `.venv/bin/pytest -q` (233 pass, about 5 s). Some tests read the real store and are skipped if it hasn't been built.
- **Data (not in git):**
  - `data/nse/raw`, including `announcements/`: NSE corporate filings per symbol, cached JSON.
  - `data/nse/reference`, including `delisted_*.csv` and `suspension_*.csv`.
  - `data/store`, including `lifecycle.parquet`.
  - `dataextracted/DE`.
- **Commands:**

  | Command | What it does |
  |---|---|
  | `nsebt update` | Daily update, after 19:00 IST |
  | `nsebt account` | Set the account limits |
  | `nsebt mergers --seed --check` | Seed the conversion table and check ratios against prices |
  | `nsebt lifecycle [--fetch]` | Rebuild `lifecycle.parquet`; `--fetch` downloads the NSE lists first |
  | `nsebt reconcile` | Full M2.6 reconciliation plus timing; writes `reports/phase45_reconciliation*` |

- **Speed:** the Nifty 500 panel loads in 5.4 s; one backtest takes 0.7 s, so 6.1 s including the load (target ≤ 30 s).

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
| `run.py` | The Run record. Its schema is **frozen at v1**; changing it triggers S2. |

**Data layer additions:**
- `nsebt/nsebt/lifecycle.py` classifies securities that stopped trading and writes `lifecycle.parquet`.
- `nsefetch.fetch_announcements` and `LIFECYCLE_FILES` fetch NSE's filings and its delisted/suspension lists.
- `data.load_panel(extra=…)` loads further securities that are never members, such as merger acquirers.

**Manual data files** (tracked; every row cites a source):

| File | Contents |
|---|---|
| `mergers.csv` | 49 parents and 51 component rows: mergers, schemes and capital-reduction relistings. Multi-part rows are supported, plus a `cash` column. |
| `lifecycle_overrides.csv` | 22 delisted, 17 of them with a stated value (₹0 where the equity was extinguished), and 7 suspended |

There are 0 `unknown` Nifty 500 members.

**Legacy code still present**, to be removed in M5.1:
- `nsebt/nsebt/engine/`
- `nsebt/nsebt/strategies/{breakout,momentum}.py`
- the `backtest`/`signals` CLI paths

`panel.py` and `bloomberg.py` stay.

## 5. Simulator facts that M3+ relies on

- **Inputs:**
  - `Market`: raw arrays, plus optional `actions`, `mergers`, `lifecycle` and `members`. Build it with `Market.from_panel(panel, actions=…, mergers=…, lifecycle=…)`.
  - `Signals`: entry, exit, score, stop, target, trail, max hold, sizing.
  - `SimConfig`: account, costs, seed, TTL, `ledger: LedgerConfig`, `on_universe_exit`, `reconcile` (validation only).
- **Each day:**
  1. Corporate actions: receivables due become cash; then value events, then share events, then delistings; **mergers last**, so converted shares are not entitled to the acquirer's same-day events.
  2. Sells, then buys, at the raw open.
  3. Stops and targets.
  4. Mark at the close: pay cash in lieu, apply injections.
  5. Decide exits and entries.
- **Equity** = cash + holdings + receivables. Buying power is cash only.
- **Trade P&L** uses `cost_basis` and `income`.
- **Entry eligibility:** the simulator doesn't apply it. Strategies AND their entries with `eligibility.eligible(...)`. Acquirers loaded via `extra` are never members, so they're never eligible.
- **Outputs:** `Result.ledger` logs every corporate action applied to holdings; `daily` has a `receivables` column.
- **Test gotcha:** in `_simkit.market()`, a bare number c becomes the bar (c, 1.01c, 0.99c, c). A flat bar that gaps from the previous close is read as a locked circuit.

## 6. Gate A outcome (details in the report)

1. **Reconciliation: approved by the user.**
   - (a) Reinvest-equivalent: 903 of 903 securities, 1.85 million security-days, max error 4.4e-16.
   - (b) Receivable mode: 8,128 of 8,128 value events preserve value.
2. **Merger ratios:** all verified against filings. ANDHRABANK corrected to 325/1000; the 10 REVIEW rows resolved.
3. **Stopped members:** all 54 previously unknown resolved: 25 conversions, 22 delisted, 7 suspended.
4. **Still open, to report again at Gate B:**
   - 40 corporate-action rows have no effective date (the spec says 9); none is applied.
   - The A8 legacy reports are missing.
   - There is no TRI in `indices.parquet`. M4.1 will probe niftyindices.com and fall back to an "approximate TRI".

## 7. Next: Milestone 3 (amendments Part 4)

| Checkpoint | Scope | Notes |
|---|---|---|
| M3.1 | `base.py` Strategy interface (**freezes it**) + registry | A Python strategy runs end to end. Use `eligibility.eligible()` for entries. Build the market with acquirers as `extra` and pass `mergers`/`lifecycle`. |
| M3.2 | `expr.py` tokenizer and parser | Errors give file:line:col. No `eval`. |
| M3.3 | Evaluation through FeatureCache | A1 warm-up from first-valid rows; A2 `prior_high`/`prior_low` lint; A3 raw vs adjusted lint. |
| M3.4 | `spec.py` | YAML schema, `params`, `--set` overrides |
| M3.5 | The three example strategies | YAML and Python momentum must produce identical trades. |

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
- **Reconciliation:** method (a) + (b), approved at Gate A.
- **Conversions:** primary sources only. A ratio that disagrees with prices is kept when the filing says so, and the gap is explained in the report.
