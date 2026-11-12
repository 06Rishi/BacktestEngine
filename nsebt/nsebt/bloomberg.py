"""Readers for the Bloomberg extract (the DE/data folder)."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

OHLCV = ["Open", "High", "Low", "Close", "Volume"]


def load_prices(de_data: Path, min_rows: int = 1) -> dict[str, pd.DataFrame]:
    """{Bloomberg ID: OHLCV frame indexed by Date} from DE/data/raw/prices/*.parquet."""
    folder = Path(de_data) / "raw" / "prices"
    files = sorted(folder.glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no price parquet files in {folder}")
    need = {"Date", "Ticker", "PX_OPEN", "PX_HIGH", "PX_LOW", "PX_LAST", "PX_VOLUME"}
    out = {}
    for f in files:
        df = pd.read_parquet(f)
        if df.empty or not need <= set(df.columns):
            continue
        t = str(df["Ticker"].iloc[0])
        df = df.assign(Date=pd.to_datetime(df["Date"]))
        df = df.drop_duplicates("Date").set_index("Date").sort_index()
        df = df[["PX_OPEN", "PX_HIGH", "PX_LOW", "PX_LAST", "PX_VOLUME"]]
        df.columns = OHLCV
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        df = df[(df[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
        if len(df) >= min_rows:
            out[t] = df
    return out


def load_membership(de_data: Path) -> pd.DataFrame:
    """Point-in-time Nifty 500 membership snapshots: columns Date, Bloomberg_ID.
    Historical snapshots plus the current security master as the latest snapshot."""
    c = Path(de_data) / "raw" / "constituents"
    m = pd.read_csv(c / "historical_membership_canonical_mapping.csv", parse_dates=["Date"])
    m = m[m["Mapping_Status"].astype(str).str.strip() == "Mapped"]
    m = m.rename(columns={"Price_Ticker": "Bloomberg_ID"})[["Date", "Bloomberg_ID"]]
    sm_path = c / "security_master.parquet"
    if sm_path.exists():
        sm = pd.read_parquet(sm_path)
        cur = pd.DataFrame({"Date": pd.to_datetime(sm["Snapshot_Date"]),
                            "Bloomberg_ID": sm["Bloomberg_ID"].astype(str)})
        if cur["Date"].max() > m["Date"].max():
            m = pd.concat([m, cur], ignore_index=True)
    return m.drop_duplicates().reset_index(drop=True)
