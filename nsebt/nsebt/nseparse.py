"""Parse NSE's raw daily files into one tidy schema.

Output columns (one row per Date, Symbol, Series):
  Date, Symbol, Series, ISIN, Open, High, Low, Close, Last, PrevClose,
  Volume (shares), Turnover (rupees), Trades, Source
Delivery files give: Date, Symbol, Series, DelivQty, DelivPct.
"""
from __future__ import annotations

import io
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# Equity series. EQ normal; BE/BZ trade-for-trade (surveillance); SM/ST/SZ SME platform.
EQUITY_SERIES = ("EQ", "BE", "BZ", "SM", "ST", "SZ")
SERIES_RANK = {s: i for i, s in enumerate(EQUITY_SERIES)}
COLUMNS = ["Date", "Symbol", "Series", "ISIN", "Open", "High", "Low", "Close", "Last",
           "PrevClose", "Volume", "Turnover", "Trades", "Source"]
NUMERIC = ["Open", "High", "Low", "Close", "Last", "PrevClose", "Volume", "Turnover", "Trades"]


def _read_zip_csv(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as z:
        name = z.namelist()[0]
        return pd.read_csv(io.BytesIO(z.read(name)), dtype=str, skipinitialspace=True)


def _finish(df: pd.DataFrame, source: str, series=EQUITY_SERIES) -> pd.DataFrame:
    for c in ("Symbol", "Series", "ISIN"):
        df[c] = df[c].astype(str).str.strip()
    df = df[df["Series"].isin(series) & df["ISIN"].str.startswith("INE")].copy()
    for c in NUMERIC:
        df[c] = pd.to_numeric(df[c].replace({"-": np.nan, "": np.nan}), errors="coerce")
    df["Source"] = source
    df = df[COLUMNS]
    # one row per (Date, ISIN): keep the highest-priority series if a stock appears twice
    df = (df.assign(_r=df["Series"].map(SERIES_RANK)).sort_values(["ISIN", "_r"])
            .drop_duplicates(["Date", "ISIN"]).drop(columns="_r"))
    return df.sort_values("Symbol").reset_index(drop=True)


def parse_cm_legacy(path: Path) -> pd.DataFrame:
    df = _read_zip_csv(path)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns={"SYMBOL": "Symbol", "SERIES": "Series", "OPEN": "Open", "HIGH": "High",
                            "LOW": "Low", "CLOSE": "Close", "LAST": "Last", "PREVCLOSE": "PrevClose",
                            "TOTTRDQTY": "Volume", "TOTTRDVAL": "Turnover", "TOTALTRADES": "Trades"})
    df["Date"] = pd.to_datetime(df["TIMESTAMP"].str.strip(), format="%d-%b-%Y")
    if "Trades" not in df:
        df["Trades"] = np.nan
    return _finish(df, "cm_legacy")


def parse_cm_udiff(path: Path) -> pd.DataFrame:
    df = _read_zip_csv(path)
    df.columns = [c.strip() for c in df.columns]
    df = df[df["Sgmt"].str.strip() == "CM"]
    df = df.rename(columns={"TckrSymb": "Symbol", "SctySrs": "Series", "OpnPric": "Open",
                            "HghPric": "High", "LwPric": "Low", "ClsPric": "Close", "LastPric": "Last",
                            "PrvsClsgPric": "PrevClose", "TtlTradgVol": "Volume",
                            "TtlTrfVal": "Turnover", "TtlNbOfTxsExctd": "Trades"})
    df["Date"] = pd.to_datetime(df["TradDt"].str.strip(), format="%Y-%m-%d")
    out = _finish(df, "cm_udiff")
    names = df.drop_duplicates("ISIN").set_index("ISIN")["FinInstrmNm"].str.strip()
    out["Name"] = out["ISIN"].map(names)
    return out


def parse_mto(path: Path) -> pd.DataFrame:
    """Security-wise delivery position. Data rows start with record type 20:
    20,SrNo,Symbol,Series,QtyTraded,DeliverableQty,DelivPct"""
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    date = None
    for ln in lines[:5]:
        if ln.startswith("10,"):
            date = datetime.strptime(ln.split(",")[2].strip(), "%d%m%Y")
            break
        if "Trade Date <" in ln:
            date = datetime.strptime(ln.split("<")[1].split(">")[0], "%d-%b-%Y")
    rows = [ln.split(",") for ln in lines if ln.startswith("20,")]
    df = pd.DataFrame([r[2:7] for r in rows if len(r) >= 7],
                      columns=["Symbol", "Series", "QtyTraded", "DelivQty", "DelivPct"])
    for c in ("Symbol", "Series"):
        df[c] = df[c].str.strip()
    for c in ("QtyTraded", "DelivQty", "DelivPct"):
        df[c] = pd.to_numeric(df[c].str.strip(), errors="coerce")
    df["Date"] = pd.Timestamp(date) if date else pd.NaT
    df = df[df["Series"].isin(EQUITY_SERIES)]
    return df[["Date", "Symbol", "Series", "QtyTraded", "DelivQty", "DelivPct"]].reset_index(drop=True)


PARSERS = {"cm_legacy": parse_cm_legacy, "cm_udiff": parse_cm_udiff}


def parse_indices(path: Path) -> pd.DataFrame:
    """Daily closing values of all NSE indices (ind_close_all_DDMMYYYY.csv)."""
    df = pd.read_csv(path, dtype=str, skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    out = pd.DataFrame({
        "Date": pd.to_datetime(df["Index Date"].str.strip(), format="%d-%m-%Y", errors="coerce"),
        "Index": df["Index Name"].str.strip(),
    })
    cols = {"Open Index Value": "Open", "High Index Value": "High", "Low Index Value": "Low",
            "Closing Index Value": "Close", "Volume": "Volume", "Turnover (Rs. Cr.)": "TurnoverCr",
            "P/E": "PE", "P/B": "PB", "Div Yield": "DivYield"}
    for a, b in cols.items():
        out[b] = pd.to_numeric(df[a].str.strip().replace({"-": None}), errors="coerce") if a in df else np.nan
    return out.dropna(subset=["Date", "Close"])
