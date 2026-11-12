"""Download and parse NSE's daily security-wise bhavcopy (sec_bhavdata_full).

One file per trading day, every listed security: OHLC, volume, delivery %.
Files are cached raw on disk so re-runs never re-download.
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d:%d%m%Y}.csv"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/csv,text/plain,*/*",
    "Referer": "https://www.nseindia.com/",
}
# Main-board equity. EQ = normal; BE/BZ = trade-for-trade segment (stocks move here
# under surveillance, so dropping them would create gaps in a stock's history).
EQUITY_SERIES = ("EQ", "BE", "BZ")

COLUMNS = {
    "SYMBOL": "Symbol", "SERIES": "Series", "DATE1": "Date",
    "OPEN_PRICE": "Open", "HIGH_PRICE": "High", "LOW_PRICE": "Low",
    "CLOSE_PRICE": "Close", "PREV_CLOSE": "PrevClose", "TTL_TRD_QNTY": "Volume",
    "TURNOVER_LACS": "TurnoverLacs", "NO_OF_TRADES": "Trades",
    "DELIV_QTY": "DelivQty", "DELIV_PER": "DelivPct",
}


def raw_name(d: date) -> str:
    return f"sec_bhavdata_full_{d:%d%m%Y}.csv"


def fetch(d: date, raw_dir: Path, session=None, retries: int = 3, pause: float = 2.0) -> Path | None:
    """Download one day's file into raw_dir. Returns the path, or None if NSE has no
    file for that day (holiday, weekend, or not published yet)."""
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / raw_name(d)
    if path.exists() and path.stat().st_size > 0:
        return path
    if session is None:
        import requests
        session = requests.Session()
        session.headers.update(HEADERS)
    last_err = None
    for attempt in range(retries):
        try:
            r = session.get(URL.format(d=d), timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            text = r.content.decode("utf-8", errors="replace")
            if not text.lstrip().upper().startswith("SYMBOL"):
                # NSE sometimes serves an HTML error/bot page with status 200
                raise ValueError(f"unexpected content for {d}: {text[:80]!r}")
            tmp = path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
            return path
        except Exception as e:  # network errors, bad content
            last_err = e
            time.sleep(pause * (attempt + 1))
    raise RuntimeError(f"failed to fetch {d}: {last_err}")


def parse(path: Path, series: tuple[str, ...] = EQUITY_SERIES) -> pd.DataFrame:
    """Parse a raw file into a tidy frame: one row per (Date, Symbol)."""
    df = pd.read_csv(path, dtype=str, skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    missing = set(COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    df = df[list(COLUMNS)].rename(columns=COLUMNS)
    for c in df.columns:
        df[c] = df[c].str.strip()
    df = df[df["Series"].isin(series)].copy()
    df["Date"] = pd.to_datetime(df["Date"], format="%d-%b-%Y")
    num = ["Open", "High", "Low", "Close", "PrevClose", "Volume",
           "TurnoverLacs", "Trades", "DelivQty", "DelivPct"]
    for c in num:
        df[c] = pd.to_numeric(df[c].replace("-", np.nan), errors="coerce")
    # If a symbol shows up in more than one series on a day, keep EQ first.
    order = {s: i for i, s in enumerate(series)}
    df["_o"] = df["Series"].map(order)
    df = (df.sort_values(["Symbol", "_o"]).drop_duplicates(["Date", "Symbol"])
            .drop(columns="_o").reset_index(drop=True))
    return df
