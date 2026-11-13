"""Securities that stop trading (amendments 2.4): `data/store/lifecycle.parquet`.

Every security whose last trade is before the end of the data is classified once:

  merged     filled row in mergers.csv                          convert (mergers)
  truncated  last trade within 5 trading days of the data end   marked at the last close; not a delisting
  delisted   on NSE's delisted list (delisted.csv)              held `grace_days`, then the stated value or policy
  suspended  on NSE's suspension list (suspension.csv)          held `grace_days`, then the stated value or policy
  unknown    none of the above                                  held for `grace_days`, then the delisting policy

`lifecycle_overrides.csv` (manual, every row citing a source) sets the class from NSE notices and
filings where NSE's lists are silent: the NSE lists end in 2020 (delisted) and 2012 (suspension).
Its optional `value` is the ₹ per share a delisted holder received when a source states it (0 when
the equity was cancelled or extinguished); otherwise the delisting policy applies.

NSE's lists are matched by symbol: the security that last traded under that symbol, with its
last trade no later than 30 days after the listed date. A security that stops and later resumes
inside the data is not in this table: the engine simply holds it through the gap (suspended).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

CLASSES = ("merged", "truncated", "delisted", "suspended", "unknown")
TRUNCATION_DAYS = 5
MATCH_SLACK = pd.Timedelta(days=30)
COLUMNS = ["SecurityID", "Symbol", "last_trade", "effective_date", "cls", "source", "list_date", "detail", "value"]
OVERRIDE_COLUMNS = ["SecurityID", "Symbol", "cls", "value", "source", "note"]


def read_overrides(path: Path | None) -> pd.DataFrame:
    """lifecycle_overrides.csv, validated: cls must be delisted or suspended; value ≥ 0 or blank."""
    if path is None or not Path(path).exists():
        return pd.DataFrame(columns=OVERRIDE_COLUMNS)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = set(OVERRIDE_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{Path(path).name}: missing column(s) {sorted(missing)}")
    df["value"] = pd.to_numeric(df["value"].str.strip().replace("", "nan"), errors="coerce")
    bad = df[~df["cls"].isin(["delisted", "suspended"]) | (df["value"] < 0) | (df["source"].str.strip() == "")]
    if len(bad):
        raise ValueError(f"{Path(path).name}: invalid row(s) {bad['Symbol'].tolist()} "
                         f"(cls delisted|suspended, value ≥ 0 or blank, a source)")
    if df["SecurityID"].duplicated().any():
        raise ValueError(f"{Path(path).name}: SecurityID listed twice")
    return df


def is_truncated(last_trade, calendar: pd.DatetimeIndex, days: int = TRUNCATION_DAYS) -> bool:
    """Last trade within `days` trading days of the end of the data."""
    k = calendar.searchsorted(pd.Timestamp(last_trade), side="left")
    return (len(calendar) - 1 - k) <= days


def read_list(path: Path | None, date_col: str) -> pd.DataFrame:
    """NSE list -> (Symbol, list_date, detail). Missing file -> empty."""
    if path is None or not Path(path).exists():
        return pd.DataFrame(columns=["Symbol", "list_date", "detail"])
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="latin-1")   # NSE files use cp1252 spaces
    df.columns = [c.strip() for c in df.columns]
    df = df.apply(lambda c: c.str.replace("\xa0", " ").str.strip())
    detail = df["Type of Delisting"].str.strip() if "Type of Delisting" in df else ""
    out = pd.DataFrame({"Symbol": df["Symbol"].str.strip().str.upper(),
                        "list_date": pd.to_datetime(df[date_col].str.strip(), format="%d-%b-%y", errors="coerce"),
                        "detail": detail})
    return out[(out["Symbol"] != "") & out["list_date"].notna()].reset_index(drop=True)


def latest(ref_dir: Path, stem: str) -> Path | None:
    files = sorted(Path(ref_dir).glob(f"{stem}_*.csv"))
    return files[-1] if files else None


def _match(lst: pd.DataFrame, symbols: pd.DataFrame, stopped: set) -> dict:
    """SecurityID -> (list_date, detail) for list rows matched by symbol."""
    out = {}
    sh = symbols[symbols["SecurityID"].isin(stopped)].copy()
    sh["Symbol"] = sh["Symbol"].str.upper()
    sh["last_date"] = pd.to_datetime(sh["last_date"])
    by_sym = {s: g for s, g in sh.groupby("Symbol")}
    for r in lst.itertuples(index=False):
        g = by_sym.get(r.Symbol)
        if g is None:
            continue
        g = g[g["last_date"] <= r.list_date + MATCH_SLACK]
        if len(g):
            sid = g.sort_values("last_date")["SecurityID"].iloc[-1]
            out.setdefault(sid, (r.list_date, r.detail))
    return out


def classify(securities: pd.DataFrame, calendar, symbols: pd.DataFrame | None = None,
             mergers: pd.DataFrame | None = None, delisted: pd.DataFrame | None = None,
             suspended: pd.DataFrame | None = None, overrides: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per security whose last trade is before the end of `calendar`.

    securities: SecurityID, Symbol, last_date      symbols: symbol_history (SecurityID, Symbol, last_date)
    mergers: mergers.load() rows                   delisted / suspended: read_list() frames
    """
    cal = pd.DatetimeIndex(sorted(pd.DatetimeIndex(calendar).unique()))
    sec = securities.copy()
    sec["last_date"] = pd.to_datetime(sec["last_date"])
    sec = sec[sec["last_date"] < cal[-1]]
    stopped = set(sec["SecurityID"])
    symbols = symbols if symbols is not None else sec[["SecurityID", "Symbol", "last_date"]]
    empty = pd.DataFrame(columns=["Symbol", "list_date", "detail"])
    dl = _match(delisted if delisted is not None else empty, symbols, stopped)
    sp = _match(suspended if suspended is not None else empty, symbols, stopped)
    mg = {} if mergers is None or not len(mergers) else mergers.drop_duplicates("parent").set_index("parent")
    ov = {} if overrides is None or not len(overrides) else overrides.set_index("SecurityID")
    rows = []
    for r in sec.itertuples(index=False):
        sid, last = r.SecurityID, r.last_date
        k = cal.searchsorted(last, side="right")
        eff = cal[k] if k < len(cal) else pd.NaT
        row = {"SecurityID": sid, "Symbol": r.Symbol, "last_trade": last, "effective_date": eff,
               "list_date": pd.NaT, "detail": "", "value": float("nan")}
        if len(mg) and sid in mg.index:
            m = mergers[mergers["parent"] == sid]
            row.update(cls="merged", source="mergers.csv",
                       detail=" + ".join(f"{x:g} {a}" for a, x in zip(m["acquirer"], m["ratio"])) + " per share")
        elif len(ov) and sid in ov.index:
            o = ov.loc[sid]
            row.update(cls=o["cls"], source="lifecycle_overrides.csv", detail=f"{o['source']}; {o['note']}".strip("; "),
                       value=float(o["value"]))
        elif is_truncated(last, cal):
            row.update(cls="truncated", source="data")
        elif sid in dl:
            row.update(cls="delisted", source="NSE delisted list", list_date=dl[sid][0], detail=dl[sid][1])
        elif sid in sp:
            row.update(cls="suspended", source="NSE suspension list", list_date=sp[sid][0])
        else:
            row.update(cls="unknown", source="data")
        rows.append(row)
    out = pd.DataFrame(rows, columns=COLUMNS)
    out["data_end"] = cal[-1]            # the store's last day when classified (stale if the store moves on)
    return out.sort_values(["last_trade", "SecurityID"]).reset_index(drop=True)


def build(store: Path, ref_dir: Path, mergers_csv: Path, overrides_csv: Path | None = None) -> pd.DataFrame:
    """Classify from the store and the cached NSE lists; write lifecycle.parquet."""
    from .backtest import mergers as mg
    store = Path(store)
    sec = pd.read_parquet(store / "securities.parquet")
    sym = pd.read_parquet(store / "symbol_history.parquet")
    cal = pd.DatetimeIndex(pd.read_parquet(store / "prices", columns=["Date"])["Date"].unique())
    out = classify(sec, cal, sym, mg.load(mergers_csv),
                   read_list(latest(ref_dir, "delisted"), "Delisted Date"),
                   read_list(latest(ref_dir, "suspension"), "Date of Suspension"), read_overrides(overrides_csv))
    out.to_parquet(store / "lifecycle.parquet", index=False)
    return out
