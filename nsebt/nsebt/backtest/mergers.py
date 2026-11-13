"""Share conversions (amendments 2.3): the manual `mergers.csv` table.

Covers stock-for-stock mergers and schemes that end a listing by issuing other listed shares,
including capital reductions that relist the same company under a new ISIN the store could not
link (e.g. Ruchi Soya -> Ruchi, 1 new share per 100).

One row per (parent, acquirer). A parent with several rows receives every component: e.g. Max
India -> 99/100 Max Healthcare + 1/5 Max India (new). Only rows with `acquirer` and `ratio`
filled are applied. `ratio` = acquirer shares received per parent share (HDFC Ltd -> HDFC Bank:
42/25 = 1.68). `cash` = ₹ received per parent share besides shares (e.g. Cairn India: four ₹10
redeemable preference shares with a cash exit at par = ₹40); put it on one row of the parent.
`effective_date` = the first trading day after the parent's last trade: the holding converts at
the start of that day. Every filled row cites its source.

`seed` lists candidates: every Nifty 500 member that stopped trading before the end of the data,
plus announcements whose subject mentions amalgamation or merger (not demerger). It never
overwrites a filled row. `implied_ratio` checks a filled ratio against prices: the parent's last
close ÷ the acquirer's close on the same day (a small discount is normal before the record date).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

COLUMNS = ["parent", "parent_symbol", "last_trade", "effective_date", "acquirer", "acquirer_symbol",
           "ratio", "cash", "implied_ratio", "source", "note"]
TRUNCATION_DAYS = 5          # stopped within this many trading days of the data end: truncated, not a candidate
RE_MERGER = r"amalgamat|merger"
RE_DEMERGER = r"de-?merg"


def read(path: Path) -> pd.DataFrame:
    """The whole table, filled and unfilled rows."""
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = set(COLUMNS[:7]) - set(df.columns)
    if missing:
        raise ValueError(f"{path.name}: missing column(s) {sorted(missing)}")
    for c in COLUMNS:
        if c not in df:
            df[c] = ""
    return df[COLUMNS + [c for c in df.columns if c not in COLUMNS]]


def load(path: Path, ids=None) -> pd.DataFrame:
    """Filled rows as (parent, effective_date, acquirer, ratio, cash), validated."""
    df = read(path)
    df = df[(df["acquirer"].str.strip() != "") & (df["ratio"].str.strip() != "")].copy()
    cash = df["cash"].str.strip().replace("", "0")
    out = pd.DataFrame({
        "parent": df["parent"].str.strip(),
        "effective_date": pd.to_datetime(df["effective_date"], errors="coerce"),
        "acquirer": df["acquirer"].str.strip(),
        "ratio": pd.to_numeric(df["ratio"].map(_ratio), errors="coerce"),
        "cash": pd.to_numeric(cash, errors="coerce"),
    }).reset_index(drop=True)
    bad = out[out["effective_date"].isna() | ~(out["ratio"] > 0) | (out["parent"] == out["acquirer"])
              | ~(out["cash"] >= 0)]
    if len(bad):
        raise ValueError(f"{Path(path).name}: invalid row(s) for {bad['parent'].tolist()} "
                         f"(need a date, a positive ratio, cash ≥ 0 and a different acquirer)")
    dup = out[out.duplicated(["parent", "acquirer"])]["parent"]
    if len(dup):
        raise ValueError(f"{Path(path).name}: parent listed twice: {dup.tolist()}")
    dates = out.groupby("parent")["effective_date"].nunique()
    if (dates > 1).any():
        raise ValueError(f"{Path(path).name}: components with different dates: {dates[dates > 1].index.tolist()}")
    return out


def _ratio(s: str) -> float:
    """'1.68', '42/25' or '42:25' -> 1.68."""
    s = str(s).strip()
    for sep in ("/", ":"):
        if sep in s:
            a, b = s.split(sep, 1)
            try:
                return float(a) / float(b)
            except (ValueError, ZeroDivisionError):
                return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


# --------------------------------------------------------------------------- seeding

def candidates(securities: pd.DataFrame, member_ids, calendar: pd.DatetimeIndex,
               announcements: pd.DataFrame | None = None) -> pd.DataFrame:
    """Members that stopped trading (not within TRUNCATION_DAYS of the end) + merger announcements."""
    cal = pd.DatetimeIndex(sorted(calendar))
    cutoff = cal[-TRUNCATION_DAYS] if len(cal) >= TRUNCATION_DAYS else cal[0]
    sec = securities.set_index("SecurityID")
    stopped = sec[sec.index.isin(set(member_ids)) & (pd.to_datetime(sec["last_date"]) < cutoff)]
    rows = {sid: "stopped trading (Nifty 500 member)" for sid in stopped.index}
    if announcements is not None and len(announcements):
        a = announcements
        hit = a["subject"].str.contains(RE_MERGER, case=False) & ~a["subject"].str.contains(RE_DEMERGER, case=False)
        by_isin = securities.set_index("ISIN")["SecurityID"]
        for isin, subj in a.loc[hit, ["isin", "subject"]].itertuples(index=False):
            sid = by_isin.get(isin)
            if sid is not None and sid in sec.index:
                rows[sid] = (rows.get(sid, "") + "; " if sid in rows else "") + f"announcement: {subj}"
    out = []
    for sid, why in rows.items():
        last = pd.Timestamp(sec.at[sid, "last_date"])
        k = cal.searchsorted(last, side="right")
        eff = cal[k] if k < len(cal) else pd.NaT
        out.append({"parent": sid, "parent_symbol": sec.at[sid, "Symbol"], "last_trade": last.date().isoformat(),
                    "effective_date": "" if pd.isna(eff) else eff.date().isoformat(), "acquirer": "",
                    "acquirer_symbol": "", "ratio": "", "implied_ratio": "", "source": "", "note": why})
    return pd.DataFrame(out, columns=COLUMNS).sort_values(["last_trade", "parent"]).reset_index(drop=True)


def implied_ratio(close: pd.DataFrame, parent: str, acquirer: str, last_trade) -> float:
    """Parent's last close ÷ acquirer's close on or before that day. `close`: wide raw closes."""
    if parent not in close or acquirer not in close:
        return float("nan")
    p = close[parent].loc[:pd.Timestamp(last_trade)].dropna()
    a = close[acquirer].loc[:pd.Timestamp(last_trade)].dropna()
    if p.empty or a.empty:
        return float("nan")
    return float(p.iloc[-1] / a.iloc[-1])


def merge_seed(existing: pd.DataFrame, seed: pd.DataFrame) -> pd.DataFrame:
    """Add seed rows for parents not yet in the table; existing rows are kept unchanged."""
    new = seed[~seed["parent"].isin(set(existing["parent"]))]
    out = pd.concat([existing, new], ignore_index=True) if len(existing) else new
    for c in COLUMNS:
        if c not in out:
            out[c] = ""
    out = out[COLUMNS + [c for c in out.columns if c not in COLUMNS]].astype(object)
    return out.where(out.notna(), "")


def refresh_implied(table: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    t = table.copy()
    multi = t[t["acquirer"].str.strip() != ""]["parent"].value_counts()
    for i, r in t.iterrows():
        if multi.get(r["parent"], 0) > 1 or str(r.get("cash", "")).strip() not in ("", "0"):
            t.at[i, "implied_ratio"] = ""          # one component of several: no single-price check
            continue
        if str(r["acquirer"]).strip():
            v = implied_ratio(close, r["parent"], r["acquirer"].strip(), r["last_trade"] or r["effective_date"])
            t.at[i, "implied_ratio"] = "" if not np.isfinite(v) else f"{v:.4f}"
    return t
