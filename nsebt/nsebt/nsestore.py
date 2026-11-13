"""Build the canonical NSE store from the raw downloaded files.

    store/prices/year=YYYY/part.parquet   raw daily rows, one per (Date, SecurityID)
    store/securities.parquet              one row per security
    store/symbol_history.parquet          every (security, ISIN, symbol, series) run
    store/corporate_actions.parquet       price-adjustment events found in the data
    store/build_report.json               counts and checks

Security identity. NSE gives every row an ISIN. A company's ISIN changes when its
face value changes (a split), and its symbol changes on renames, so neither alone is
a stable key. ISINs are linked into one security when the same issuer (ISIN chars
4-7) continues under the same symbol (or a symbol NSE lists as renamed) within a few
trading days. SecurityID is the security's first ISIN.

Corporate actions come from NSE's own announcements (see corpactions.py): splits,
bonuses and consolidations are exact; rights and demergers are estimated from prices.
Adjusted prices are raw prices multiplied by the product of all later factors
(back-adjustment). Raw prices are stored unchanged; adjustment happens when loading.
Note: the legacy bhavcopy's PREVCLOSE is NOT re-based on ex-dates, so it cannot be
used to detect actions.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import corpactions, nseparse
from .nsefetch import UDIFF_START  # noqa: F401  (documents the format switch)

log = logging.getLogger(__name__)

LINK_GAP_DAYS = 10          # max trading days between an old ISIN's last row and the new one's first
PRICE_KINDS = ("split", "bonus", "consolidation", "rights", "arrangement",      # applied by default
               "special_dividend", "inferred_split")
JUMP = 0.35                 # raw overnight moves above this with no announcement are reported


# --------------------------------------------------------------------------- loading raw

def _parse_all(raw_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, list]:
    raw_dir = Path(raw_dir)
    prices, deliv, errors = [], [], []
    for src, fn in nseparse.PARSERS.items():
        for p in sorted((raw_dir / src).glob("*/*")):
            if p.suffix == ".part":
                continue
            try:
                prices.append(fn(p))
            except Exception as e:  # noqa: BLE001
                errors.append((str(p), str(e)))
    for p in sorted((raw_dir / "mto").glob("*/*.DAT")):
        try:
            deliv.append(nseparse.parse_mto(p))
        except Exception as e:  # noqa: BLE001
            errors.append((str(p), str(e)))
    px = pd.concat(prices, ignore_index=True)
    dv = pd.concat(deliv, ignore_index=True) if deliv else pd.DataFrame(
        columns=["Date", "Symbol", "Series", "QtyTraded", "DelivQty", "DelivPct"])
    return px, dv, errors


# --------------------------------------------------------------------------- identity

def issuer(isin: pd.Series) -> pd.Series:
    return isin.str[3:7]


def link_isins(px: pd.DataFrame, symbol_changes: pd.DataFrame | None = None) -> pd.Series:
    """Map ISIN -> SecurityID (first ISIN of its linked chain)."""
    cal = pd.Index(sorted(px["Date"].unique()))
    pos = pd.Series(np.arange(len(cal)), index=cal)
    g = px.groupby("ISIN")
    span = pd.DataFrame({"first": g["Date"].min(), "last": g["Date"].max()})
    span["first_sym"] = px.sort_values("Date").groupby("ISIN")["Symbol"].first()
    span["last_sym"] = px.sort_values("Date").groupby("ISIN")["Symbol"].last()
    span["issuer"] = issuer(span.index.to_series())
    span["kf"], span["kl"] = span["first"].map(pos), span["last"].map(pos)

    renames = set()
    if symbol_changes is not None and len(symbol_changes):
        renames = set(zip(symbol_changes["old"], symbol_changes["new"]))

    parent = {i: i for i in span.index}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _, grp in span.groupby("issuer"):
        if len(grp) < 2:
            continue
        ends = grp.sort_values("kl")
        starts = grp.sort_values("kf")
        for a, ra in ends.iterrows():
            for b, rb in starts.iterrows():
                if a == b or not (0 < rb["kf"] - ra["kl"] <= LINK_GAP_DAYS):
                    continue
                if ra["last_sym"] == rb["first_sym"] or (ra["last_sym"], rb["first_sym"]) in renames:
                    ra_root, rb_root = find(a), find(b)
                    if ra_root != rb_root:
                        parent[rb_root] = ra_root
    comp = pd.Series({i: find(i) for i in span.index})
    first_isin = span.assign(c=comp).sort_values("first").groupby("c").apply(lambda x: x.index[0])
    return comp.map(first_isin)


def read_symbol_changes(path: Path | None) -> pd.DataFrame:
    if not path or not Path(path).exists():
        return pd.DataFrame(columns=["old", "new", "date"])
    s = pd.read_csv(path, dtype=str, encoding="utf-8", encoding_errors="replace")
    s.columns = [c.strip().upper() for c in s.columns]
    return pd.DataFrame({"old": s["OLD SYMBOL"].str.strip(), "new": s["NEW SYMBOL"].str.strip(),
                         "date": pd.to_datetime(s["EFFECTIVE DATE"].str.strip(), format="%d-%b-%Y",
                                                errors="coerce")})


# --------------------------------------------------------------------------- corporate actions

def factor_series(px: pd.DataFrame, ratios: pd.DataFrame) -> pd.Series:
    """Cumulative back-adjustment factor for each row of px (aligned to px.index):
    product of `ratio` for every event of the same security dated after the row.
    ratios: SecurityID, Date (first trading day on which the event applies), ratio."""
    f = pd.Series(1.0, index=px.index)
    if ratios.empty:
        return f
    sids = px["SecurityID"].to_numpy()
    dates = px["Date"].to_numpy()
    order = np.argsort(sids, kind="stable")
    s_sorted = sids[order]
    for sid, ev in ratios.groupby("SecurityID"):
        lo, hi = np.searchsorted(s_sorted, sid, "left"), np.searchsorted(s_sorted, sid, "right")
        if lo == hi:
            continue
        rows = order[lo:hi]
        ev = ev.sort_values("Date")
        r = ev["ratio"].to_numpy()
        cum = np.cumprod(r[::-1])[::-1]               # cum[k] = product of events k..end
        k = np.searchsorted(ev["Date"].to_numpy(), dates[rows], side="right")
        f.iloc[rows] = np.where(k < len(r), cum[np.minimum(k, len(r) - 1)], 1.0)
    return f


def adjust(px: pd.DataFrame, actions: pd.DataFrame, kinds=PRICE_KINDS) -> pd.DataFrame:
    """px with AdjFactor and Adj* columns. kinds: which event kinds to apply
    (add 'dividend' for a total-return series)."""
    a = actions[actions["kind"].isin(kinds)] if len(actions) else actions
    f = factor_series(px, corpactions.combine_same_day(a) if len(a) else
                      pd.DataFrame(columns=["SecurityID", "Date", "ratio"]))
    out = px.copy()
    out["AdjFactor"] = f
    for c in ("Open", "High", "Low", "Close", "Last", "PrevClose", "VWAP"):
        if c in out:
            out[f"Adj{c}"] = out[c] * f
    out["AdjVolume"] = out["Volume"] / f
    if "DelivQty" in out:
        out["AdjDelivQty"] = out["DelivQty"] / f
    return out


def unexplained_jumps(px: pd.DataFrame, actions: pd.DataFrame, jump: float = JUMP) -> pd.DataFrame:
    """Raw overnight moves larger than `jump` with no announced price event on that
    day: candidates for missing corporate actions (or genuine crashes/spikes).
    Prices under Rs 2 are skipped (one tick is already a large % move there)."""
    d = px.sort_values(["SecurityID", "Date"])
    prior = d.groupby("SecurityID")["Close"].shift(1)
    gap = d["Open"] / prior
    big = d[(np.log(gap).abs() > np.log(1 + jump)) & (prior >= 2.0)].assign(prior_close=prior, gap=gap)
    ev = actions[actions["kind"].isin(PRICE_KINDS)][["SecurityID", "effective_date"]].dropna()
    known = set(zip(ev["SecurityID"], ev["effective_date"]))
    big = big[[(s, t) not in known for s, t in zip(big["SecurityID"], big["Date"])]]
    return big[["SecurityID", "Symbol", "ISIN", "Date", "prior_close", "Open", "Close", "gap"]].reset_index(drop=True)


# --------------------------------------------------------------------------- build

def build(raw_dir: Path, store_dir: Path, reference_dir: Path | None = None,
          ca_overrides: Path | None = None) -> dict:
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    log.info("parsing raw files ...")
    px, dv, errors = _parse_all(raw_dir)
    rep = {"built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "parse_errors": errors[:50], "n_parse_errors": len(errors),
           "price_rows_parsed": len(px), "delivery_rows_parsed": len(dv)}

    # duplicates (same day from two sources) - keep the first parsed
    n0 = len(px)
    px = px.drop_duplicates(["Date", "ISIN"]).reset_index(drop=True)
    rep["duplicate_rows_dropped"] = n0 - len(px)

    # delivery joins on the symbol and series NSE used that day
    dv = dv.drop_duplicates(["Date", "Symbol", "Series"])
    px = px.merge(dv[["Date", "Symbol", "Series", "DelivQty", "DelivPct"]],
                  on=["Date", "Symbol", "Series"], how="left")
    px["VWAP"] = (px["Turnover"] / px["Volume"]).where(px["Volume"] > 0)

    sc = None
    if reference_dir:
        files = sorted(Path(reference_dir).glob("symbolchange_*.csv"))
        sc = read_symbol_changes(files[-1] if files else None)
    log.info("linking ISINs into securities ...")
    sid = link_isins(px, sc)
    px["SecurityID"] = px["ISIN"].map(sid)

    # a security can still have two rows on one date if two linked ISINs overlap; keep EQ-first
    px = (px.assign(_r=px["Series"].map(nseparse.SERIES_RANK)).sort_values(["SecurityID", "Date", "_r"])
            .drop_duplicates(["SecurityID", "Date"]).drop(columns="_r").reset_index(drop=True))

    log.info("corporate actions ...")
    ann = corpactions.load_announcements(Path(raw_dir) / "corp_actions")
    ev = corpactions.price_factors(corpactions.events(ann), px) if len(ann) else pd.DataFrame(
        columns=["SecurityID", "effective_date", "kind", "factor"])
    inf = corpactions.infer_from_isin_changes(px, ev) if len(ev) else pd.DataFrame()
    if len(inf):
        ev = pd.concat([ev, inf], ignore_index=True)
    ev, ca_over = corpactions.apply_overrides(ev, ca_overrides)
    ev.to_parquet(store_dir / "corporate_actions.parquet", index=False)
    uj = unexplained_jumps(px, ev)
    uj.to_parquet(store_dir / "unexplained_jumps.parquet", index=False)

    # symbol history: contiguous runs of (SecurityID, ISIN, Symbol, Series)
    h = px.sort_values(["SecurityID", "Date"])
    key = h["SecurityID"] + "|" + h["ISIN"] + "|" + h["Symbol"] + "|" + h["Series"]
    run = (key != key.groupby(h["SecurityID"]).shift(1)).cumsum()
    hist = h.groupby(run).agg(SecurityID=("SecurityID", "first"), ISIN=("ISIN", "first"),
                              Symbol=("Symbol", "first"), Series=("Series", "first"),
                              first_date=("Date", "min"), last_date=("Date", "max"),
                              rows=("Date", "size")).reset_index(drop=True)
    hist.to_parquet(store_dir / "symbol_history.parquet", index=False)

    # securities
    last_day = px["Date"].max()
    g = px.sort_values("Date").groupby("SecurityID")
    sec = pd.DataFrame({
        "Symbol": g["Symbol"].last(), "ISIN": g["ISIN"].last(), "Series": g["Series"].last(),
        "first_date": g["Date"].min(), "last_date": g["Date"].max(), "rows": g.size(),
        "n_isins": g["ISIN"].nunique(), "n_symbols": g["Symbol"].nunique(),
    })
    if "Name" in px:
        sec["Name"] = g["Name"].last()
    sec["active"] = sec["last_date"] >= last_day - pd.Timedelta(days=7)
    sec["n_price_actions"] = (ev[ev["kind"].isin(PRICE_KINDS)].groupby("SecurityID").size()
                              .reindex(sec.index).fillna(0).astype(int))
    if reference_dir:
        eq = sorted(Path(reference_dir).glob("EQUITY_L_*.csv"))
        if eq:
            e = pd.read_csv(eq[-1], dtype=str)
            e.columns = [c.strip().upper() for c in e.columns]
            e = e.set_index(e["ISIN NUMBER"].str.strip())
            sec["Name"] = sec.get("Name", pd.Series(index=sec.index, dtype=object)).fillna(
                sec["ISIN"].map(e["NAME OF COMPANY"].str.strip()))
            sec["listing_date"] = pd.to_datetime(sec["ISIN"].map(e["DATE OF LISTING"].str.strip()),
                                                 format="%d-%b-%Y", errors="coerce")
            sec["face_value"] = pd.to_numeric(sec["ISIN"].map(e["FACE VALUE"].str.strip()), errors="coerce")
        n5 = sorted(Path(reference_dir).glob("ind_nifty500list_*.csv"))
        if n5:
            n = pd.read_csv(n5[-1], dtype=str)
            ind = dict(zip(n["ISIN Code"].str.strip(), n["Industry"].str.strip()))
            isin_to_sid = px.drop_duplicates("ISIN").set_index("ISIN")["SecurityID"]
            sid_ind = {isin_to_sid[i]: v for i, v in ind.items() if i in isin_to_sid}
            sec["Industry"] = sec.index.map(sid_ind)
    sec.index.name = "SecurityID"
    sec.reset_index().to_parquet(store_dir / "securities.parquet", index=False)

    # prices, partitioned by year
    pdir = store_dir / "prices"
    for y, part in px.groupby(px["Date"].dt.year):
        d = pdir / f"year={y}"
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "part.parquet.tmp"
        part.drop(columns=[c for c in ("Name",) if c in part]).to_parquet(tmp, index=False)
        tmp.replace(d / "part.parquet")

    ifiles = sorted((Path(raw_dir) / "indices").glob("*/*.csv"))
    if ifiles:
        ix = pd.concat([nseparse.parse_indices(f) for f in ifiles], ignore_index=True)
        ix["Index"] = ix["Index"].str.replace(r"\s+", " ", regex=True)
        ix = ix.drop_duplicates(["Date", "Index"]).sort_values(["Index", "Date"])
        ix.to_parquet(store_dir / "indices.parquet", index=False)
        rep["indices"] = {"rows": len(ix), "indices": int(ix["Index"].nunique()),
                          "nifty500_days": int((ix["Index"].str.upper() == "NIFTY 500").sum())}

    rep.update({
        "rows": len(px), "dates": int(px["Date"].nunique()),
        "first_date": str(px["Date"].min().date()), "last_date": str(last_day.date()),
        "securities": len(sec), "active_securities": int(sec["active"].sum()),
        "isins": int(px["ISIN"].nunique()),
        "securities_with_isin_change": int((sec["n_isins"] > 1).sum()),
        "securities_with_symbol_change": int((sec["n_symbols"] > 1).sum()),
        "rows_with_delivery": int(px["DelivQty"].notna().sum()),
        "announcements": int(len(ann)),
        "corporate_actions": int(len(ev)),
        "corporate_actions_by_kind": ev["kind"].value_counts().to_dict(),
        "exact_actions_price_confirmed": int((ev["price_confirms"] == True).sum()),  # noqa: E712
        "exact_actions_price_not_confirmed": int((ev["price_confirms"] == False).sum()),  # noqa: E712
        "exact_actions_not_applied": int((~ev["applied"].astype(bool)).sum()),
        "inferred_from_isin_change": int((ev["kind"] == "inferred_split").sum()),
        "ca_overrides_applied": ca_over,
        "unexplained_jumps": int(len(uj)),
    })
    (store_dir / "build_report.json").write_text(json.dumps(rep, indent=2, default=str))
    return rep


def load_prices(store_dir: Path, adjusted: bool = True, start=None, end=None,
                columns=None, kinds=PRICE_KINDS) -> pd.DataFrame:
    """Raw prices from the store; with adjusted=True adds Adj* columns using every
    detected corporate action."""
    df = pd.read_parquet(Path(store_dir) / "prices")
    if "year" in df:
        df = df.drop(columns="year")
    if start is not None:
        df = df[df["Date"] >= pd.Timestamp(start)]
    if end is not None:
        df = df[df["Date"] <= pd.Timestamp(end)]
    df = df.reset_index(drop=True)
    if adjusted:
        ev = pd.read_parquet(Path(store_dir) / "corporate_actions.parquet")
        df = adjust(df, ev, kinds)
    return df[columns] if columns else df
