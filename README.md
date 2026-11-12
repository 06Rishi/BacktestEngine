# nsebt: NSE research and backtesting platform

A local tool for researching and backtesting swing-trading strategies on NSE equities.
It keeps a clean, survivorship-free daily history of every NSE stock since 2016, updated
from NSE's own files. Placing orders is out of scope.

Status: Phase 0 (data audit), Phase 1 (data layer), Phase 2 (daily pipeline) and Phase 3 (indicators) are done.
Indicators, strategies, the backtest engine, the app and the scanner follow (see `plan.md`).

## Setup

Needs [uv](https://docs.astral.sh/uv/) and git. Python 3.12 is installed by uv.

```bash
git clone <this repo> BacktestEngine && cd BacktestEngine
uv sync --extra dev --extra query      # exact library versions from uv.lock, into .venv/
.venv/bin/nsebt doctor                 # check everything
```

Data is not in git. On a new machine either restore a backup (below) or download it again
with `nsebt backfill` (about 1 hour) and `nsebt backfill --sources indices`.

## Daily workflow

```bash
.venv/bin/nsebt update
```

Run it after 19:00 IST, when NSE has published the day's files. It:

1. downloads every day since the store's last day (price, delivery and index files; weekends
   too, because NSE sometimes trades on them),
2. refreshes corporate actions, the listed-securities list and the Nifty 500 list,
3. rebuilds the store (about 2 minutes),
4. writes a report to `reports/runs/` and prints one line, e.g.
   `update 2026-11-13..2026-11-13: 1 day(s) fetched, 0 failed, 0 real gap(s); store to 2026-11-13`.

Missing files are explained in the report: weekend, NSE holiday (from NSE's holiday list),
not published yet, or a real gap. Sharp moves (>20%) with no known corporate action are
listed for review. The exit code is non-zero if anything failed.

If NSE blocks the script, download the files in a browser
(`cmDDMONYYYYbhav.csv.zip` / `BhavCopy_NSE_CM_…zip`, `MTO_DDMMYYYY.DAT`, `ind_close_all_DDMMYYYY.csv`) and run:

```bash
.venv/bin/nsebt ingest ~/Downloads/MTO_13112026.DAT ~/Downloads/BhavCopy_NSE_CM_0_0_0_20261113_F_0000.csv.zip
.venv/bin/nsebt build
```

### Scheduling

**Linux / macOS (cron).** `crontab -e`, then add (runs every day at 19:30 IST, including weekends):

```
CRON_TZ=Asia/Kolkata
30 19 * * * cd /path/to/BacktestEngine && .venv/bin/nsebt update >> logs/cron.log 2>&1
```

**Windows (Task Scheduler).** Create Basic Task → Daily, 19:30 → Start a program:
- Program: `C:\path\to\BacktestEngine\.venv\Scripts\nsebt.exe`
- Arguments: `update`
- Start in: `C:\path\to\BacktestEngine`

Tick "Run task as soon as possible after a scheduled start is missed", so a day the laptop was
off is caught up on the next start. Missed days are also caught by the next `update`, since it
always continues from the store's last day.

## Backups and moving to another machine

`nsebt backup` copies everything that cannot be rebuilt from code: the downloaded NSE files,
the Bloomberg extract, reference lists, `bbg_overrides.csv`, `config.yaml` and the trade
journal (once it exists). It also writes a git bundle of the full code history, so a backup
is complete even without GitHub. Only new or changed files are copied, and a checksum
manifest is written.

```bash
.venv/bin/nsebt backup --dest /run/media/me/drive     # or set backup.destinations in config.yaml
.venv/bin/nsebt doctor --backup /run/media/me/drive   # verifies every checksum
```

Moving to a new machine:

```bash
git clone <repo> BacktestEngine && cd BacktestEngine       # or: git clone /drive/nsebt-backup/repo.bundle BacktestEngine
uv sync --extra dev --extra query
.venv/bin/nsebt restore /run/media/me/drive                # dry run: shows what would be restored
.venv/bin/nsebt restore /run/media/me/drive --apply
.venv/bin/nsebt build                                      # rebuild the store (~2 min)
.venv/bin/nsebt doctor --tests
.venv/bin/nsebt update                                     # carry on
```

## What is in the store

`data/store/` (rebuilt by `nsebt build` from `data/nse/raw/`):

| File | Contents |
|---|---|
| `prices/year=YYYY/` | one row per security per day: raw OHLC, last, previous close, volume, turnover, trades, VWAP, delivery qty and %, ISIN, symbol, series |
| `securities.parquet` | one row per security: current symbol/ISIN, name, listing date, face value, industry (current Nifty 500), first/last trade |
| `symbol_history.parquet` | every (ISIN, symbol, series) a security has used, with dates |
| `corporate_actions.parquet` | splits, bonuses, rights, demergers, special and ordinary dividends, with factors and checks |
| `indices.parquet` | daily OHLC, P/E, P/B, dividend yield for 179 NSE indices (Nifty 50, Nifty 500, India VIX, sectors …) |
| `membership.parquet` | point-in-time Nifty 500, by security, on NSE's effective dates |
| `bbg_mapping.parquet` | Bloomberg ID → security |

Loading prices in Python:

```python
from nsebt import nsestore, universe
px = nsestore.load_prices("data/store")          # raw + Adj* columns (splits, bonuses, rights, demergers, special dividends)
n500 = universe.nifty500("data/store")           # n500.members_on("2020-03-23")
```

Raw prices are never changed. Adjusted columns are computed on load and are meant for indicators.
Fills and position sizing must use raw prices.

## Known limitations

- History starts on 2016-08-29. Signals that need 200–252 days of history start around September 2017.
- Membership comes from twice-yearly snapshots. Index changes between reconstitutions (mergers, replacements) are not captured.
- Industry tags are today's classification only.
- Rights and demerger adjustments are estimates (theoretical ex-rights price, and the ex-date opening gap). Splits and bonuses are exact and checked against the price.
- One Bloomberg code (`PRIF`) is deliberately left unmapped; see `bbg_overrides.csv`.
- No fundamentals (P/E, ROE …) or FII/DII flows yet.
- `backtest` and `signals` still run on the Bloomberg extract until the Phase 5 engine.

## Reports

- `reports/data_audit.md`: Phase 0 audit of the Bloomberg extract
- `reports/phase1_report.md`: Phase 1 data layer and validation
- `reports/runs/`: one report per `update` run
