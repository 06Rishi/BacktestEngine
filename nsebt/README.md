# nsebt: NSE backtesting engine + bhavcopy data pipeline

Backtests strategies on your Bloomberg Nifty 500 history (2016-08-29 to 2026-08-27) and
extends that history with NSE's free daily bhavcopy files.

## Setup

```bash
pip install -r requirements.txt
mkdir -p data/DE && cp -r /path/to/DE/data data/DE/data   # or pass --de-data
python -m pytest -q                                         # 14 tests
```

## Daily data pipeline

```bash
# 1. Download bhavcopy files. First run: start inside the Bloomberg period so the
#    two sources overlap and symbol matches can be checked.
python -m nsebt update --start 2026-08-20
# later runs just continue from where the store ends:
python -m nsebt update

# If NSE blocks the script, download sec_bhavdata_full_DDMMYYYY.csv from the NSE
# website yourself and add it:
python -m nsebt ingest ~/Downloads/sec_bhavdata_full_*.csv

# 2. Map Bloomberg IDs to NSE symbols and validate them on overlapping days
python -m nsebt map
```

Files land in `data/nse/raw` (raw CSVs, cached) and `data/nse/store` (one parquet per month).
Weekends are skipped, holidays come back as "no file", and re-running never duplicates rows.

### Symbol mapping (needs one manual step)

Bloomberg codes often differ from NSE symbols (`MM IN Equity` is `M&M`, `BANDHAN` is
`BANDHANBNK`). Only about 200 of the current 500 members match by name, so:

1. **Best:** on the Bloomberg terminal, pull `ID_ISIN` for every ticker in
   `DE/data/raw/prices` and save it as `bbg_isin.csv` with columns `Bloomberg_ID,ISIN`.
   Download NSE's equity list `EQUITY_L.csv`. Then:
   `python -m nsebt map --bbg-isin bbg_isin.csv --nse-list EQUITY_L.csv`
2. **Or by hand:** fill in `data/unmapped_current_members.csv` and pass it as
   `--overrides`.

Each match is checked against Bloomberg prices on the overlapping days
(`data/mapping_validation.csv`). Only matches with status `ok` are used to extend the
history, so a wrong match can't silently corrupt a series.

## Backtests

```bash
python -m nsebt backtest momentum            # top 20 by 12-1 momentum, monthly rebalance
python -m nsebt backtest momentum --top-n 10
python -m nsebt backtest breakout            # trade-level, with random-entry placebo
python -m nsebt signals momentum             # what to hold as of the latest data
```

Rules the engine enforces:
- Decisions use data up to a day's close; fills happen at the next day's open.
- Point-in-time Nifty 500 membership (a stock only counts while it was in the index).
- Costs: 0.15% per side for portfolios, 0.3% round trip for trades (`--cost` to change).
- Daily moves over 35% are treated as splits/bonuses, since the Bloomberg prices are
  not adjusted.

## Adding a strategy

Put a class in `nsebt/strategies/` and register it in `strategies/__init__.py`:
- `kind = "trades"` with `signals(df) -> DataFrame[entry, exit]` (per stock), or
- `kind = "portfolio"` with `scores(close, volume) -> DataFrame` (higher = better, NaN = skip).

Only use data up to each row's date (rolling windows, `shift`). The tests include
truncation checks you can copy to prove a new strategy has no look-ahead.
