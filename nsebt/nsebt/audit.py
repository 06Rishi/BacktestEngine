"""Phase 0 audit of the Bloomberg extract (the DE/ folder).

Reads every file under the root, checks the daily price files in depth, inspects the
precomputed features file, and writes CSV evidence plus a machine summary:

    python -m nsebt audit --de-root path/to/DE --out reports/audit

Nothing under the DE root is modified.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

JUNK_PARTS = ("__MACOSX", ".ipynb_checkpoints")
JUNK_NAMES = (".DS_Store",)
TICKER_COLS = ("Ticker", "Bloomberg_ID", "Bloomberg_Security", "Price_Ticker", "security", "Security")
PRICE_COLS = ["PX_OPEN", "PX_HIGH", "PX_LOW", "PX_LAST", "PX_VOLUME"]

# Price ratios (new / old) that a split, bonus or consolidation produces.
# Split 1:k -> 1/k. Bonus a:b (a new shares per b held) -> b/(a+b).
CA_RATIOS = {
    "split 1:2 / bonus 1:1": 1 / 2, "bonus 2:1": 1 / 3, "bonus 1:2": 2 / 3,
    "split 1:4 / bonus 3:1": 1 / 4, "bonus 1:3": 3 / 4, "split 1:5 / bonus 4:1": 1 / 5,
    "bonus 3:2": 2 / 5, "bonus 2:3": 3 / 5, "split 1:10": 1 / 10, "split 1:2 + bonus 4:1": 1 / 10,
    "split 1:20": 1 / 20, "consolidation 2:1": 2.0, "consolidation 5:1": 5.0,
    "consolidation 10:1": 10.0,
}

# Publicly announced splits/bonuses used as spot checks of the adjustment basis.
# Ex-dates are approximate; the check scans a +/-5 trading-day window.
REFERENCE_EVENTS = [
    ("RELIANCE IN Equity", "2017-09-07", "bonus 1:1", 0.5),
    ("RELIANCE IN Equity", "2024-10-28", "bonus 1:1", 0.5),
    ("TCS IN Equity", "2018-05-31", "bonus 1:1", 0.5),
    ("INFO IN Equity", "2018-09-04", "bonus 1:1", 0.5),
    ("IRCTC IN Equity", "2021-10-28", "split 1:5", 0.2),
    ("WPRO IN Equity", "2024-12-03", "bonus 1:1", 0.5),
    ("BAF IN Equity", "2025-06-16", "split 1:2 + bonus 4:1", 0.1),
    ("HDFCB IN Equity", "2025-08-26", "bonus 1:1", 0.5),
]

# What each column of the precomputed features file means, from the feature notebook.
FEATURE_DEFS = {
    "Return_1M": "PX_LAST pct change over 21 rows",
    "Return_3M": "PX_LAST pct change over 63 rows",
    "Return_6M": "PX_LAST pct change over 126 rows",
    "Return_12M": "PX_LAST pct change over 252 rows",
    "DMA_20": "20-row SMA of PX_LAST", "DMA_50": "50-row SMA of PX_LAST",
    "DMA_100": "100-row SMA of PX_LAST", "DMA_200": "200-row SMA of PX_LAST",
    "Price_20DMA": "PX_LAST / DMA_20 (ratio, not %)", "Price_50DMA": "PX_LAST / DMA_50",
    "Price_100DMA": "PX_LAST / DMA_100", "Price_200DMA": "PX_LAST / DMA_200",
    "52W_High": "252-row max of PX_LAST (close, not high)",
    "52W_Low": "252-row min of PX_LAST (close, not low)",
    "Pct_Below_52W_High": "(52W_High - PX_LAST) / 52W_High",
    "Pct_Above_52W_Low": "(PX_LAST - 52W_Low) / 52W_Low",
    "RSI": "RSI(14), Wilder smoothing (ewm alpha=1/14, adjust=False, min_periods=14)",
    "ROC": "PX_LAST pct change over 20 rows",
    "MACD": "EMA12 - EMA26 of PX_LAST (line only, no signal/histogram, no warm-up mask)",
    "Daily_Return": "PX_LAST pct change over 1 row",
    "Volatility_20D": "std of Daily_Return over 20 rows (daily, NOT annualised)",
    "Volatility_60D": "std of Daily_Return over 60 rows (daily)",
    "Volatility_120D": "std of Daily_Return over 120 rows (daily)",
    "Volatility_252D": "std of Daily_Return over 252 rows (daily)",
    "Current_Drawdown": "PX_LAST / running max since series start - 1",
    "Max_Drawdown_3M": "63-row min of (PX_LAST / 63-row max - 1)",
    "Max_Drawdown_12M": "252-row min of (PX_LAST / 252-row max - 1)",
    "Volume_Change": "PX_VOLUME pct change over 1 row",
    "Volume_20D_Avg": "20-row mean of PX_VOLUME",
    "Volume_to_20D_Avg": "PX_VOLUME / Volume_20D_Avg",
}


@dataclass
class AuditConfig:
    big_move: float = 0.20          # close-to-close moves above this are classified
    gap_days: int = 5               # flag gaps longer than this many trading days
    ratio_tol: float = 0.04         # tolerance when matching a move to a CA ratio
    market_day_min: int = 50        # a date is a market day if this many series trade
    feature_sample: int = 40        # tickers to recompute features for
    seed: int = 0


def is_junk(rel: Path) -> bool:
    return (any(p in JUNK_PARTS for p in rel.parts) or rel.name in JUNK_NAMES
            or rel.name.startswith("._"))


# --------------------------------------------------------------------------- inventory

def _describe_frame(df: pd.DataFrame) -> dict:
    out = {"rows": len(df), "n_columns": df.shape[1]}
    date_cols = [c for c in df.columns
                 if "date" in str(c).lower() or pd.api.types.is_datetime64_any_dtype(df[c])]
    lo, hi = None, None
    for c in date_cols:
        d = pd.to_datetime(df[c], errors="coerce")
        if d.notna().any():
            lo = min(lo, d.min()) if lo is not None else d.min()
            hi = max(hi, d.max()) if hi is not None else d.max()
    out["date_columns"] = ";".join(map(str, date_cols))
    out["date_min"] = lo.date().isoformat() if lo is not None else ""
    out["date_max"] = hi.date().isoformat() if hi is not None else ""
    tcol = next((c for c in TICKER_COLS if c in df.columns), None)
    out["ticker_column"] = tcol or ""
    out["n_tickers"] = int(df[tcol].nunique()) if tcol else np.nan
    out["max_null_rate"] = float(df.isna().mean().max()) if df.shape[1] and len(df) else np.nan
    return out


def inventory(root: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One row per file (every file, junk marked) and one row per (data file, column)."""
    files, cols = [], []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        row = {"path": str(rel), "ext": p.suffix.lower(), "size_bytes": p.stat().st_size,
               "junk": is_junk(rel), "readable": True, "error": ""}
        if row["junk"]:
            files.append(row)
            continue
        try:
            if p.suffix.lower() == ".parquet":
                meta = pq.ParquetFile(p).metadata
                df = pd.read_parquet(p)
                row.update(_describe_frame(df), row_groups=meta.num_row_groups)
            elif p.suffix.lower() == ".csv":
                df = pd.read_csv(p, low_memory=False)
                row.update(_describe_frame(df))
            elif p.suffix.lower() == ".ipynb":
                nb = json.loads(p.read_text(encoding="utf-8"))
                cells = nb.get("cells", [])
                code = ["".join(c.get("source", [])) for c in cells if c.get("cell_type") == "code"]
                row.update(rows=len(cells), n_code_cells=len(code),
                           code_lines=sum(len(c.splitlines()) for c in code),
                           uses_blpapi=any("blpapi" in c for c in code))
                df = None
            else:
                df = None
        except Exception as e:      # noqa: BLE001 - every failure is recorded, not raised
            row.update(readable=False, error=f"{type(e).__name__}: {e}"[:300])
            df = None
        files.append(row)
        if df is not None:
            nulls = df.isna().mean()
            for c in df.columns:
                cols.append({"path": str(rel), "column": str(c), "dtype": str(df[c].dtype),
                             "null_rate": float(nulls[c]), "n_unique": int(df[c].nunique())
                             if len(df) <= 200_000 else np.nan})
    return pd.DataFrame(files), pd.DataFrame(cols)


# --------------------------------------------------------------------------- prices

def load_price_files(prices_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """All raw rows from every price parquet, untouched, plus a per-file table.
    Returns (long frame with File, Ticker, Date + PX columns, file table)."""
    frames, meta = [], []
    for f in sorted(prices_dir.glob("*.parquet")):
        df = pd.read_parquet(f)
        ok = {"Date", "Ticker", *PRICE_COLS} <= set(df.columns)
        tick = sorted(df["Ticker"].dropna().astype(str).unique()) if "Ticker" in df else []
        meta.append({"file": f.name, "rows": len(df), "has_price_schema": ok,
                     "tickers_in_file": ";".join(tick), "n_tickers_in_file": len(tick)})
        if not ok:
            continue
        df = df[["Ticker", "Date", *PRICE_COLS]].copy()
        df.insert(0, "File", f.name)
        frames.append(df)
    px = pd.concat(frames, ignore_index=True)
    px["Ticker"] = px["Ticker"].astype(str)
    for c in PRICE_COLS:
        px[c] = pd.to_numeric(px[c], errors="coerce").astype("float64")
    px["Date"] = pd.to_datetime(px["Date"]).astype("datetime64[ns]")
    return px.sort_values(["Ticker", "Date"], kind="stable").reset_index(drop=True), pd.DataFrame(meta)


def market_calendar(px: pd.DataFrame, min_series: int) -> pd.DataFrame:
    c = px.groupby("Date")["Ticker"].nunique().rename("n_series").reset_index()
    c["weekday"] = c["Date"].dt.day_name()
    c["market_day"] = c["n_series"] >= min_series
    return c


def nearest_ca(ratio: float, tol: float) -> tuple[str, float]:
    """Closest corporate-action ratio within tol (relative), else ('', nan)."""
    if not np.isfinite(ratio) or ratio <= 0:
        return "", np.nan
    best = min(CA_RATIOS.items(), key=lambda kv: abs(np.log(ratio / kv[1])))
    return (best[0], best[1]) if abs(ratio / best[1] - 1) <= tol else ("", np.nan)


def classify_moves(px: pd.DataFrame, cfg: AuditConfig, cal: pd.DataFrame | None = None) -> pd.DataFrame:
    """Every close-to-close move above cfg.big_move, labelled (first rule that applies):
      ipo_reference_row           previous row is the series' first row with a close only
                                  (Bloomberg's IPO offer-price row), so this is the listing-day move
      after_trading_gap           more than cfg.gap_days market days since the previous bar
      spike_reversed              move undone the next day (likely a bad print)
      ca_ratio_with_volume_shift  clean CA ratio, overnight gap and volume level changes by ~1/ratio
      ca_ratio_only               clean CA ratio and overnight gap, volume level unchanged
      spike_reversed              move undone the next day (likely a bad print)
      real_move                   none of the above
    """
    d = px[["Ticker", "Date", "PX_OPEN", "PX_HIGH", "PX_LOW", "PX_LAST", "PX_VOLUME"]].copy()
    g = d.groupby("Ticker", sort=False)
    close_only = d[["PX_OPEN", "PX_HIGH", "PX_LOW"]].isna().all(axis=1) & d["PX_VOLUME"].isna()
    d["prev_is_ipo_row"] = (g.cumcount() == 1) & close_only.groupby(d["Ticker"]).shift(1, fill_value=False)
    if cal is not None:
        mdays = pd.DatetimeIndex(cal.loc[cal["market_day"], "Date"].sort_values())
        k = pd.Series(mdays.searchsorted(d["Date"]), index=d.index)
        d["market_days_since_prev"] = k - k.groupby(d["Ticker"]).shift(1)
    else:
        d["market_days_since_prev"] = 1.0
    d["prev_close"] = g["PX_LAST"].shift(1)
    d["prev_date"] = g["Date"].shift(1)
    d["next_close"] = g["PX_LAST"].shift(-1)
    d["ratio"] = d["PX_LAST"] / d["prev_close"]
    d["prev_ratio"] = d.groupby("Ticker", sort=False)["ratio"].shift(1)
    d["gap_ratio"] = d["PX_OPEN"] / d["prev_close"]
    v = d["PX_VOLUME"]
    d["vol_before"] = g["PX_VOLUME"].transform(lambda s: s.shift(1).rolling(20, min_periods=5).median())
    d["vol_after"] = g["PX_VOLUME"].transform(lambda s: s[::-1].rolling(20, min_periods=5).median()[::-1])
    big = d[(d["ratio"] - 1).abs() > cfg.big_move].copy()
    lab, name, fac = [], [], []
    for r in big.itertuples(index=False):
        n, f = nearest_ca(r.ratio, cfg.ratio_tol)
        undone_next = (np.isfinite(r.next_close) and r.PX_LAST > 0
                       and abs((r.next_close / r.PX_LAST) * r.ratio - 1) < 0.05)
        undoes_prev = (np.isfinite(r.prev_ratio) and abs(r.prev_ratio - 1) > cfg.big_move
                       and abs(r.prev_ratio * r.ratio - 1) < 0.05)
        reversed_ = undone_next or undoes_prev
        if r.prev_is_ipo_row:
            label = "ipo_reference_row"
        elif r.market_days_since_prev > cfg.gap_days + 1:
            label = "after_trading_gap"
        elif n:
            gap_ok = np.isfinite(r.gap_ratio) and abs(r.gap_ratio / f - 1) <= 2 * cfg.ratio_tol
            vshift = r.vol_after / r.vol_before if r.vol_before and r.vol_before > 0 else np.nan
            vol_ok = np.isfinite(vshift) and 0.5 <= vshift * f <= 2.0 and abs(np.log(vshift)) > np.log(1.5)
            label = ("spike_reversed" if reversed_ else
                     "ca_ratio_with_volume_shift" if gap_ok and vol_ok else
                     "ca_ratio_only" if gap_ok else "real_move")
        else:
            label = "spike_reversed" if reversed_ else "real_move"
        lab.append(label)
        name.append(n)
        fac.append(f)
    big["ca_pattern"] = pd.Series(name, index=big.index, dtype="object")
    big["ca_ratio"] = fac
    big["volume_shift"] = big["vol_after"] / big["vol_before"]
    big["label"] = pd.Series(lab, index=big.index, dtype="object")
    big["move"] = big["ratio"] - 1
    # share of the move that happened overnight (open vs previous close); ~1 means the
    # price re-based at the open, typical of an ex-date (demerger, special dividend, CA)
    big["overnight_share"] = np.log(big["gap_ratio"]) / np.log(big["ratio"])
    return big[["Ticker", "Date", "prev_date", "market_days_since_prev", "prev_close", "PX_OPEN", "PX_LAST", "next_close",
                "move", "gap_ratio", "overnight_share", "ca_pattern", "ca_ratio", "vol_before", "vol_after",
                "volume_shift", "label"]].reset_index(drop=True)


def price_checks(px: pd.DataFrame, cal: pd.DataFrame, cfg: AuditConfig, data_end: pd.Timestamp):
    """Row-level problem tables and a per-ticker summary."""
    o, h, l, c, v = (px[k] for k in PRICE_COLS)
    eps = 1e-9
    out = {}

    dup = px[px.duplicated(["Ticker", "Date"], keep=False)].copy()
    if len(dup):
        dup["conflicting"] = dup.groupby(["Ticker", "Date"])[PRICE_COLS].transform("nunique").max(axis=1) > 1
    out["duplicates"] = dup

    has_all = o.notna() & h.notna() & l.notna() & c.notna()
    viol = has_all & ((l > o + eps) | (l > c + eps) | (h < o - eps) | (h < c - eps) | (l > h + eps))
    pv = px[viol].copy()
    pv["issue"] = np.select(
        [pv["PX_LOW"] > pv["PX_HIGH"], pv["PX_HIGH"] < pv[["PX_OPEN", "PX_LAST"]].max(axis=1)],
        ["low>high", "high<max(open,close)"], "low>min(open,close)")
    pv["worst_breach_pct"] = np.maximum.reduce([
        (pv["PX_LOW"] - pv[["PX_OPEN", "PX_LAST"]].min(axis=1)) / pv["PX_LAST"],
        (pv[["PX_OPEN", "PX_LAST"]].max(axis=1) - pv["PX_HIGH"]) / pv["PX_LAST"]])
    out["ohlc_violations"] = pv

    first = px.groupby("Ticker").cumcount() == 0
    mo = px[c.notna() & (o.isna() | h.isna() | l.isna())].copy()
    mo["first_row_of_series"] = first[mo.index]
    mo["close_only"] = mo[["PX_OPEN", "PX_HIGH", "PX_LOW", "PX_VOLUME"]].isna().all(axis=1)
    out["missing_ohl"] = mo
    out["missing_close"] = px[c.isna()]
    out["nonpositive"] = px[(px[["PX_OPEN", "PX_HIGH", "PX_LOW", "PX_LAST"]] <= 0).any(axis=1) | (v < 0)]
    out["zero_volume"] = px[v == 0]
    out["missing_volume"] = px[v.isna() & c.notna()]

    # gaps measured in market days
    mdays = pd.DatetimeIndex(cal.loc[cal["market_day"], "Date"].sort_values())
    pos = pd.Series(np.arange(len(mdays)), index=mdays)
    k = px["Date"].map(pos)
    tmp = pd.DataFrame({"Ticker": px["Ticker"], "Date": px["Date"], "k": k})
    tmp = tmp.dropna(subset=["k"])
    tmp["prev_date"] = tmp.groupby("Ticker")["Date"].shift(1)
    tmp["missing_days"] = tmp["k"] - tmp.groupby("Ticker")["k"].shift(1) - 1
    gaps = tmp[tmp["missing_days"] > cfg.gap_days][["Ticker", "prev_date", "Date", "missing_days"]]
    out["gaps"] = gaps.rename(columns={"prev_date": "last_before_gap", "Date": "first_after_gap"})

    # days absent from the market calendar (series traded when almost nothing else did)
    off = cal.loc[~cal["market_day"], "Date"]
    out["off_calendar_rows"] = px[px["Date"].isin(off)]

    # stale: close unchanged and no volume
    prevc = px.groupby("Ticker")["PX_LAST"].shift(1)
    out["stale"] = px[(c == prevc) & (v.isna() | (v == 0))]

    locked = has_all & (o == h) & (h == l) & (l == c)
    out["locked_bars"] = px[locked]

    moves = classify_moves(px, cfg, cal)
    out["big_moves"] = moves

    g = px.groupby("Ticker")
    s = pd.DataFrame({
        "file": g["File"].first(), "rows": g.size(),
        "first_date": g["Date"].min(), "last_date": g["Date"].max(),
        "duplicate_rows": out["duplicates"].groupby("Ticker").size() if len(dup) else 0,
        "ohlc_violations": out["ohlc_violations"].groupby("Ticker").size(),
        "missing_ohl": out["missing_ohl"].groupby("Ticker").size(),
        "missing_close": out["missing_close"].groupby("Ticker").size(),
        "nonpositive": out["nonpositive"].groupby("Ticker").size(),
        "missing_volume": out["missing_volume"].groupby("Ticker").size(),
        "zero_volume": out["zero_volume"].groupby("Ticker").size(),
        "gaps_gt_limit": out["gaps"].groupby("Ticker").size(),
        "max_gap_days": out["gaps"].groupby("Ticker")["missing_days"].max(),
        "stale_bars": out["stale"].groupby("Ticker").size(),
        "locked_bars": out["locked_bars"].groupby("Ticker").size(),
        "big_moves": moves.groupby("Ticker").size(),
        "ca_like_moves": moves[moves["label"].astype(str).str.startswith("ca_")].groupby("Ticker").size(),
    })
    cnt = [c for c in s.columns if c not in ("file", "first_date", "last_date", "max_gap_days")]
    s[cnt] = s[cnt].fillna(0).astype(int)
    s["ends_early"] = s["last_date"] < data_end - pd.Timedelta(days=7)
    first_market = cal.loc[cal["market_day"], "Date"].min()
    s["starts_late"] = s["first_date"] > first_market + pd.Timedelta(days=7)
    return out, s.reset_index()


def reference_event_check(px: pd.DataFrame) -> pd.DataFrame:
    """For each known split/bonus, the largest overnight move near the ex-date. If
    prices were raw, a move close to the event ratio would appear."""
    rows = []
    for t, ex, ev, ratio in REFERENCE_EVENTS:
        d = px[px["Ticker"] == t].set_index("Date").sort_index()
        if d.empty:
            rows.append({"Ticker": t, "approx_ex_date": ex, "event": ev, "raw_ratio_expected": ratio,
                         "status": "ticker not in data"})
            continue
        gap = d["PX_OPEN"] / d["PX_LAST"].shift(1)
        cc = d["PX_LAST"] / d["PX_LAST"].shift(1)
        i = d.index.searchsorted(pd.Timestamp(ex))
        w = slice(max(i - 5, 1), min(i + 6, len(d)))
        win_cc, win_gap = cc.iloc[w], gap.iloc[w]
        j = int(np.argmax(np.abs(np.log(win_cc.to_numpy()))))
        vb = d["PX_VOLUME"].iloc[max(i - 25, 0):max(i - 5, 1)].median()
        va = d["PX_VOLUME"].iloc[i + 5:i + 25].median()
        rows.append({
            "Ticker": t, "approx_ex_date": ex, "event": ev, "raw_ratio_expected": ratio,
            "largest_close_ratio_in_window": float(win_cc.iloc[j]),
            "on_date": win_cc.index[j].date().isoformat(),
            "largest_gap_ratio_in_window": float(win_gap.iloc[np.argmax(np.abs(np.log(win_gap.to_numpy())))]),
            "volume_after_over_before": float(va / vb) if vb else np.nan,
            "close_before_window": float(d["PX_LAST"].iloc[w.start - 1]),
            "status": ("jump present (raw prices)" if abs(np.log(win_cc.iloc[j] / ratio)) < 0.08
                       else "no jump (adjusted)"),
        })
    return pd.DataFrame(rows)


def dividend_basis_check(fund: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    """Compare growth of Bloomberg's gross total-return index with growth of PX_LAST
    over each security's fundamentals window. If PX_LAST were dividend-adjusted the two
    would match; if not, the TRI grows faster by roughly the dividend yield."""
    f = fund[["Bloomberg_Security", "Date", "TOT_RETURN_INDEX_GROSS_DVDS"]].dropna().copy()
    f["Date"] = pd.to_datetime(f["Date"]).astype("datetime64[ns]")
    close = px.set_index(["Ticker", "Date"])["PX_LAST"]
    rows = []
    for t, g in f.groupby("Bloomberg_Security"):
        g = g.sort_values("Date")
        if len(g) < 8 or t not in close.index.get_level_values(0):
            continue
        p = close.loc[t]
        a, b = g.iloc[0], g.iloc[-1]
        pa = p[:a["Date"]].iloc[-1] if len(p[:a["Date"]]) else np.nan
        pb = p[:b["Date"]].iloc[-1] if len(p[:b["Date"]]) else np.nan
        yrs = (b["Date"] - a["Date"]).days / 365.25
        if not (np.isfinite(pa) and np.isfinite(pb)) or yrs < 1 or a["TOT_RETURN_INDEX_GROSS_DVDS"] <= 0:
            continue
        tri = (b["TOT_RETURN_INDEX_GROSS_DVDS"] / a["TOT_RETURN_INDEX_GROSS_DVDS"]) ** (1 / yrs) - 1
        pr = (pb / pa) ** (1 / yrs) - 1
        rows.append({"Ticker": t, "from": a["Date"].date(), "to": b["Date"].date(), "years": yrs,
                     "tri_cagr": tri, "price_cagr": pr, "tri_minus_price": tri - pr})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- membership

def normalise_member_id(t: pd.Series) -> pd.Series:
    """'RELIANCE IS' (index-member code) and 'RELIANCE IN' both -> 'RELIANCE IN Equity'."""
    return t.astype(str).str.strip().str.replace(r" (IS|IN)$", " IN Equity", regex=True)


def membership_checks(cons_dir: Path, px_tickers: set[str]) -> dict[str, pd.DataFrame]:
    m = pd.read_parquet(cons_dir / "nifty500_historical_membership.parquet")
    m["Date"] = pd.to_datetime(m["Date"])
    m["Bloomberg_ID"] = normalise_member_id(m["Ticker"])
    canon = pd.read_csv(cons_dir / "historical_membership_canonical_mapping.csv", parse_dates=["Date"])
    sm = pd.read_parquet(cons_dir / "security_master.parquet")
    cur = set(sm["Bloomberg_ID"].astype(str))

    counts = m.groupby("Date").agg(members=("Ticker", "nunique"), rows=("Ticker", "size")).reset_index()
    prev = None
    adds, drops = [], []
    for d, gset in m.groupby("Date")["Bloomberg_ID"]:
        s = set(gset)
        adds.append(len(s - prev) if prev is not None else np.nan)
        drops.append(len(prev - s) if prev is not None else np.nan)
        prev = s
    counts["added_vs_prev"] = adds
    counts["dropped_vs_prev"] = drops
    counts["with_price_file"] = m.groupby("Date")["Bloomberg_ID"].apply(lambda x: x.isin(px_tickers).sum()).values

    ids = sorted(set(m["Bloomberg_ID"]) | cur | px_tickers)
    snaps = m.groupby("Bloomberg_ID")["Date"].agg(["count", "min", "max"])
    cross = pd.DataFrame({"Bloomberg_ID": ids})
    cross["in_historical_membership"] = cross["Bloomberg_ID"].isin(set(m["Bloomberg_ID"]))
    cross["in_current_master"] = cross["Bloomberg_ID"].isin(cur)
    cross["has_price_file"] = cross["Bloomberg_ID"].isin(px_tickers)
    cross = cross.merge(snaps.rename(columns={"count": "n_snapshots", "min": "first_snapshot",
                                              "max": "last_snapshot"}),
                        left_on="Bloomberg_ID", right_index=True, how="left")
    cmap = canon["Mapping_Status"].value_counts().rename_axis("Mapping_Status").reset_index(name="rows")
    return {"snapshot_counts": counts, "cross": cross, "canonical_status": cmap, "membership": m}


def members_priced_on_snapshot(m: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    """For each (snapshot, member): did the member have a price within 5 days of the snapshot?"""
    last = px.groupby("Ticker")["Date"].agg(["min", "max"])
    j = m.merge(last, left_on="Bloomberg_ID", right_index=True, how="left")
    j["priced_around_snapshot"] = (j["min"] <= j["Date"] + pd.Timedelta(days=5)) & (j["max"] >= j["Date"] - pd.Timedelta(days=5))
    bad = j[~j["priced_around_snapshot"]]
    return bad[["Date", "Ticker", "Bloomberg_ID", "min", "max"]].rename(
        columns={"min": "price_first", "max": "price_last"})


# --------------------------------------------------------------------------- features

def recompute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Re-implementation of the feature notebook's formulas, used only for the spot check."""
    df = df.sort_values("Date").drop_duplicates("Date").reset_index(drop=True)
    p, v = df["PX_LAST"], df["PX_VOLUME"]
    f = pd.DataFrame({"Date": df["Date"]})
    for k, n in (("1M", 21), ("3M", 63), ("6M", 126), ("12M", 252)):
        f[f"Return_{k}"] = p.pct_change(n)
    for n in (20, 50, 100, 200):
        f[f"DMA_{n}"] = p.rolling(n, min_periods=n).mean()
        f[f"Price_{n}DMA"] = p / f[f"DMA_{n}"]
    f["52W_High"] = p.rolling(252, min_periods=252).max()
    f["52W_Low"] = p.rolling(252, min_periods=252).min()
    f["Pct_Below_52W_High"] = (f["52W_High"] - p) / f["52W_High"]
    f["Pct_Above_52W_Low"] = (p - f["52W_Low"]) / f["52W_Low"]
    dlt = p.diff()
    ag = dlt.clip(lower=0).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    al = (-dlt.clip(upper=0)).ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    f["RSI"] = 100 - 100 / (1 + ag / al)
    f["ROC"] = p.pct_change(20)
    f["MACD"] = p.ewm(span=12, adjust=False).mean() - p.ewm(span=26, adjust=False).mean()
    f["Daily_Return"] = p.pct_change()
    for n in (20, 60, 120, 252):
        f[f"Volatility_{n}D"] = f["Daily_Return"].rolling(n, min_periods=n).std()
    f["Current_Drawdown"] = p / p.cummax() - 1
    for k, n in (("3M", 63), ("12M", 252)):
        f[f"Max_Drawdown_{k}"] = (p / p.rolling(n, min_periods=n).max() - 1).rolling(n, min_periods=n).min()
    f["Volume_Change"] = v.pct_change()
    f["Volume_20D_Avg"] = v.rolling(20, min_periods=20).mean()
    f["Volume_to_20D_Avg"] = v / f["Volume_20D_Avg"]
    return f


def feature_checks(feat_path: Path, px: pd.DataFrame, cfg: AuditConfig) -> dict[str, pd.DataFrame]:
    F = pd.read_parquet(feat_path)
    F["Date"] = pd.to_datetime(F["Date"]).astype("datetime64[ns]")
    F["Ticker"] = F["Ticker"].astype(str)
    obj = [c for c in F.columns if c not in ("Date", "Ticker") and F[c].dtype == object]
    for c in obj:
        F[c] = pd.to_numeric(F[c], errors="coerce").astype("float64")
    cols = [c for c in F.columns if c not in ("Date", "Ticker")]

    first_valid = {}
    for c in cols:
        nn = F[c].notna()
        first_valid[c] = F[nn].groupby("Ticker")["Date"].min()
    cov = pd.DataFrame({
        "column": cols, "dtype": [str(F[c].dtype) for c in cols],
        "non_null_rate": [float(F[c].notna().mean()) for c in cols],
        "inf_count": [int(np.isinf(F[c]).sum()) if F[c].dtype.kind == "f" else 0 for c in cols],
        "min": [float(F[c].min()) for c in cols], "median": [float(F[c].median()) for c in cols],
        "max": [float(F[c].max()) for c in cols],
        "meaning": [FEATURE_DEFS.get(c, "raw Bloomberg field" if c in PRICE_COLS else "") for c in cols],
    })

    # 1) OHLCV in the features file equals the raw price files?
    keys = ["Ticker", "Date"]
    j = F[keys + PRICE_COLS].merge(px[keys + PRICE_COLS], on=keys, how="outer",
                                   suffixes=("_feat", "_raw"), indicator=True)
    raw_cmp = {"rows_features": len(F), "rows_raw": len(px),
               "only_in_features": int((j["_merge"] == "left_only").sum()),
               "only_in_raw": int((j["_merge"] == "right_only").sum())}
    both = j[j["_merge"] == "both"]
    for c in PRICE_COLS:
        a, b = both[f"{c}_feat"], both[f"{c}_raw"]
        raw_cmp[f"{c}_mismatch_rows"] = int(((a - b).abs() > 1e-9 * b.abs().clip(lower=1)).sum()
                                            + (a.isna() != b.isna()).sum())

    # 2) leakage across ticker boundaries: a value present before the window could exist
    lead = []
    F = F.sort_values(["Ticker", "Date"]).reset_index(drop=True)
    F["_row"] = F.groupby("Ticker").cumcount()
    for c, n in (("Return_1M", 21), ("Return_12M", 252), ("DMA_200", 199), ("Volatility_20D", 20),
                 ("RSI", 14), ("Daily_Return", 1)):
        if c in F:
            lead.append({"column": c, "first_possible_row": n,
                         "rows_with_value_too_early": int((F[c].notna() & (F["_row"] < n)).sum())})

    # 3) recompute on a sample of tickers
    rng = np.random.default_rng(cfg.seed)
    tick = sorted(F["Ticker"].unique())
    must = [t for t in ("RELIANCE IN Equity", "TCS IN Equity", "HDFCB IN Equity") if t in tick]
    sample = list(dict.fromkeys(must + list(rng.choice(tick, size=min(cfg.feature_sample, len(tick)),
                                                       replace=False))))
    feat_cols = [c for c in FEATURE_DEFS if c in F.columns]
    agg = {c: {"compared": 0, "mismatch": 0, "max_abs_diff": 0.0, "nan_pattern_diff": 0} for c in feat_cols}
    for t in sample:
        mine = recompute_features(px[px["Ticker"] == t][["Date", "PX_LAST", "PX_VOLUME"]])
        theirs = F[F["Ticker"] == t].set_index("Date")
        mine = mine.set_index("Date").reindex(theirs.index)
        for c in feat_cols:
            a, b = theirs[c].replace([np.inf, -np.inf], np.nan), mine[c].replace([np.inf, -np.inf], np.nan)
            both_ = a.notna() & b.notna()
            d = (a[both_] - b[both_]).abs()
            tol = 1e-6 * b[both_].abs().clip(lower=1)
            agg[c]["compared"] += int(both_.sum())
            agg[c]["mismatch"] += int((d > tol).sum())
            agg[c]["max_abs_diff"] = max(agg[c]["max_abs_diff"], float(d.max()) if len(d) else 0.0)
            agg[c]["nan_pattern_diff"] += int((a.isna() != b.isna()).sum())
    rec = pd.DataFrame([{"column": c, **v} for c, v in agg.items()])
    rec["match_rate"] = 1 - rec["mismatch"] / rec["compared"].clip(lower=1)
    rec["tickers_sampled"] = len(sample)
    return {"coverage": cov, "raw_compare": pd.DataFrame([raw_cmp]), "early_values": pd.DataFrame(lead),
            "recompute": rec}


# --------------------------------------------------------------------------- fundamentals

def fundamentals_checks(fdir: Path, px_tickers: set[str]) -> dict[str, pd.DataFrame]:
    f = pd.read_parquet(fdir / "nifty500_historical_fundamentals.parquet")
    f["Date"] = pd.to_datetime(f["Date"])
    fields = [c for c in f.columns if c not in ("Bloomberg_Security", "Date")]
    by_field = pd.DataFrame({"field": fields, "non_null_rate": [float(f[c].notna().mean()) for c in fields],
                             "securities_with_any": [int(f.groupby("Bloomberg_Security")[c].apply(
                                 lambda s: s.notna().any()).sum()) for c in fields]})
    q = f["Date"].dt.to_period("Q")
    dates = f.groupby(q)["Date"].agg(["min", "max", "count"]).reset_index().rename(columns={"Date": "quarter"})
    dates["off_quarter_end_dates"] = f.groupby(q)["Date"].apply(lambda s: int((~s.dt.is_quarter_end).sum())).values
    secs = set(f["Bloomberg_Security"])
    summ = pd.DataFrame([{"rows": len(f), "securities": len(secs),
                          "securities_without_price_file": len(secs - px_tickers),
                          "price_tickers_without_fundamentals": len(px_tickers - secs),
                          "date_min": f["Date"].min().date(), "date_max": f["Date"].max().date()}])
    return {"by_field": by_field, "by_quarter": dates, "summary": summ, "frame": f}


# --------------------------------------------------------------------------- NSE name match

def nse_name_match(bhavcopy_csv: Path, cross: pd.DataFrame) -> pd.DataFrame:
    """Does the Bloomberg root equal an NSE symbol in a bhavcopy file? Name matches are
    candidates only; Phase 1 validates them against overlapping prices."""
    from . import bhavcopy
    from .mapping import root
    syms = set(bhavcopy.parse(bhavcopy_csv)["Symbol"])
    c = cross.copy()
    c["bbg_root"] = c["Bloomberg_ID"].map(root)
    c["root_is_nse_symbol"] = c["bbg_root"].isin(syms)
    return c


# --------------------------------------------------------------------------- driver

def run(de_root: Path, out: Path, cfg: AuditConfig = AuditConfig(), log=print,
        nse_bhavcopy: Path | None = None) -> dict:
    de_root, out = Path(de_root), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    data = de_root / "data"
    summary: dict = {"generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "de_root": str(de_root), "config": cfg.__dict__}

    log("inventory: walking every file ...")
    inv, invcols = inventory(de_root)
    inv.to_csv(out / "inventory_files.csv", index=False)
    invcols.to_csv(out / "inventory_columns.csv", index=False)
    summary["files"] = {"total": len(inv), "junk": int(inv["junk"].sum()),
                        "unreadable": int((~inv["readable"]).sum()),
                        "by_ext": inv[~inv["junk"]]["ext"].value_counts().to_dict(),
                        "empty_dirs": sorted(str(p.relative_to(de_root)) for p in de_root.rglob("*")
                                             if p.is_dir() and not any(p.iterdir()))}

    log("prices: loading every price parquet ...")
    px, files = load_price_files(data / "raw" / "prices")
    files.to_csv(out / "price_files.csv", index=False)
    data_end = px["Date"].max()
    cal = market_calendar(px, cfg.market_day_min)
    cal.to_csv(out / "price_calendar.csv", index=False)
    log("prices: running checks ...")
    tabs, per = price_checks(px, cal, cfg, data_end)
    per.to_csv(out / "price_ticker_summary.csv", index=False)
    for k, t in tabs.items():
        t.to_csv(out / f"price_{k}.csv", index=False)
    ref = reference_event_check(px)
    ref.to_csv(out / "adjustment_reference_events.csv", index=False)
    mv = tabs["big_moves"]
    summary["prices"] = {
        "files": len(files), "files_with_price_schema": int(files["has_price_schema"].sum()),
        "files_with_multiple_tickers": int((files["n_tickers_in_file"] > 1).sum()),
        "rows": len(px), "tickers": int(px["Ticker"].nunique()),
        "date_min": str(px["Date"].min().date()), "date_max": str(data_end.date()),
        "market_days": int(cal["market_day"].sum()), "off_calendar_dates": int((~cal["market_day"]).sum()),
        "weekend_market_days": int((cal["market_day"] & cal["Date"].dt.weekday.ge(5)).sum()),
        **{k: int(len(v)) for k, v in tabs.items() if k != "big_moves"},
        "conflicting_duplicates": int(tabs["duplicates"]["conflicting"].sum()) if len(tabs["duplicates"]) else 0,
        "missing_ohl_first_row_close_only": int((tabs["missing_ohl"]["first_row_of_series"]
                                                 & tabs["missing_ohl"]["close_only"]).sum()),
        "tickers_ending_early": int(per["ends_early"].sum()),
        "tickers_starting_late": int(per["starts_late"].sum()),
        "big_moves": int(len(mv)), "big_move_labels": mv["label"].value_counts().to_dict(),
        "reference_events": ref["status"].value_counts().to_dict(),
    }

    log("membership ...")
    tickers = set(px["Ticker"])
    mem = membership_checks(data / "raw" / "constituents", tickers)
    mem["snapshot_counts"].to_csv(out / "membership_snapshots.csv", index=False)
    mem["cross"].to_csv(out / "membership_price_crosscheck.csv", index=False)
    unpriced = members_priced_on_snapshot(mem["membership"], px)
    unpriced.to_csv(out / "membership_unpriced_on_snapshot.csv", index=False)
    cr = mem["cross"]
    summary["membership"] = {
        "snapshots": int(len(mem["snapshot_counts"])),
        "members_min": int(mem["snapshot_counts"]["members"].min()),
        "members_max": int(mem["snapshot_counts"]["members"].max()),
        "ids_ever_member": int(cr["in_historical_membership"].sum()),
        "members_without_price_file": int((cr["in_historical_membership"] & ~cr["has_price_file"]).sum()),
        "current_without_price_file": int((cr["in_current_master"] & ~cr["has_price_file"]).sum()),
        "price_files_never_member": int((cr["has_price_file"] & ~cr["in_historical_membership"]
                                         & ~cr["in_current_master"]).sum()),
        "member_snapshot_rows_without_price_nearby": int(len(unpriced)),
        "canonical_mapping_status": dict(zip(mem["canonical_status"]["Mapping_Status"],
                                             mem["canonical_status"]["rows"].astype(int))),
    }

    if nse_bhavcopy:
        nm = nse_name_match(Path(nse_bhavcopy), cr)
        nm.to_csv(out / "mapping_name_match.csv", index=False)
        cur = nm[nm["in_current_master"]]
        hist = nm[nm["in_historical_membership"] & ~nm["in_current_master"]]
        summary["name_match"] = {"bhavcopy": Path(nse_bhavcopy).name,
                                 "current_members": int(len(cur)),
                                 "current_root_is_nse_symbol": int(cur["root_is_nse_symbol"].sum()),
                                 "former_members": int(len(hist)),
                                 "former_root_is_nse_symbol": int(hist["root_is_nse_symbol"].sum())}

    log("fundamentals ...")
    fu = fundamentals_checks(data / "raw" / "fundamentals", tickers)
    fu["by_field"].to_csv(out / "fundamentals_fields.csv", index=False)
    fu["by_quarter"].to_csv(out / "fundamentals_quarters.csv", index=False)
    div = dividend_basis_check(fu["frame"], px)
    div.to_csv(out / "adjustment_dividend_check.csv", index=False)
    summary["fundamentals"] = {k: (str(v) if isinstance(v, (pd.Timestamp,)) else v)
                               for k, v in fu["summary"].iloc[0].to_dict().items()}
    summary["fundamentals"] = {k: (v.isoformat() if hasattr(v, "isoformat") else
                                   int(v) if isinstance(v, (np.integer,)) else v)
                               for k, v in summary["fundamentals"].items()}
    summary["dividend_check"] = {"securities": len(div),
                                 "median_tri_minus_price_cagr": float(div["tri_minus_price"].median()) if len(div) else None,
                                 "share_with_gap_over_0.5pct": float((div["tri_minus_price"] > 0.005).mean()) if len(div) else None}

    feat_path = data / "processed" / "nifty500_historical_stock_features.parquet"
    if feat_path.exists():
        log("features: reading the features file and recomputing a sample ...")
        fc = feature_checks(feat_path, px, cfg)
        for k, t in fc.items():
            t.to_csv(out / f"features_{k}.csv", index=False)
        rec = fc["recompute"]
        summary["features"] = {"columns": int(len(fc["coverage"])),
                               **{k: int(v) for k, v in fc["raw_compare"].iloc[0].items()},
                               "recompute_tickers": int(rec["tickers_sampled"].iloc[0]),
                               "features_fully_matching": int((rec["mismatch"] == 0).sum()),
                               "features_with_mismatch": rec.loc[rec["mismatch"] > 0, "column"].tolist(),
                               "early_values": int(fc["early_values"]["rows_with_value_too_early"].sum())}

    (out / "audit_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    log(f"audit written to {out}")
    return summary
