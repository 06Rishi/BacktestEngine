"""NSE corporate actions -> price adjustment factors.

Source: NSE's corporate-action announcements (fetched by nsefetch.fetch_corporate_actions).
Each announcement has an ISIN, an ex-date and a free-text subject. The subject is
parsed into zero or more events; each event has a `factor` that multiplies every price
dated before the ex-date (volume is divided by it):

  split          "Face Value Split ... From Rs A ... To Rs B"   factor = B / A
  consolidation  "Consolidation ... From Rs A To Rs B"           factor = B / A
  bonus          "Bonus a:b"  (a new shares for every b held)    factor = b / (a + b)
  rights         "Rights a:b @ Premium Rs P"                     theoretical ex-rights:
                 issue price I = face value + P, prior close C:
                 factor = (b*C + a*I) / ((a+b)*C), only when I < C
  arrangement    demerger / scheme of arrangement / capital reduction: no ratio in the
                 text, so the factor is implied by prices: ex-date open / prior close,
                 used only when that gap exceeds 3%
  dividend       amount per share; factor 1 - D / C, applied only for total-return series

Split, consolidation and bonus factors are exact. Rights and arrangement factors are
estimates and are flagged as such.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

NUM = r"(\d+(?:\.\d+)?)"
RE_SPLIT = re.compile(r"(?:split|splt|sub-?division).*?(?:from|frm)\s*r[se]\.?\s*" + NUM + r".*?to\s*r[se]\.?\s*" + NUM, re.I)
RE_CONSOL = re.compile(r"consolidation.*?from\s*r[se]\.?\s*" + NUM + r".*?to\s*r[se]\.?\s*" + NUM, re.I)
RE_BONUS = re.compile(r"(?<![a-z])bonus\s*[-:]?\s*" + NUM + r"\s*:\s*" + NUM, re.I)
RE_RIGHTS = re.compile(r"rights?(?:\s*issue)?\s*" + NUM + r"\s*:\s*" + NUM + r"(?:\s*@\s*prem(?:ium)?\s*r[se]\.?\s*" + NUM + r")?", re.I)
RE_DIV = re.compile(r"dividend\s*-?\s*r[se]\.?\s*" + NUM, re.I)
RE_ARRANGE = re.compile(r"demerg|arrangement|capital reduction", re.I)
ARRANGE_MIN_GAP = 0.03
SPECIAL_DIV = 0.05          # a dividend of 5%+ of the price is treated as a price event


def load_announcements(folder: Path) -> pd.DataFrame:
    rows = [r for f in sorted(Path(folder).glob("ca_*.json")) for r in json.loads(f.read_text())]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["subject"] = df["subject"].astype(str).str.strip()
    df["exDate"] = pd.to_datetime(df["exDate"], format="%d-%b-%Y", errors="coerce")
    df["faceVal"] = pd.to_numeric(df["faceVal"], errors="coerce")
    df = df[df["series"].isin(["EQ", "BE", "BZ", "SM", "ST", "SZ"]) & df["exDate"].notna()]
    return df.drop_duplicates(["isin", "exDate", "subject"]).reset_index(drop=True)


def parse_subject(subject: str, face_value: float = np.nan) -> list[dict]:
    """Events in one announcement. Price-dependent factors (rights, arrangement,
    dividend) are left for `price_factors` to fill in."""
    s = subject
    ev = []
    for m in RE_BONUS.finditer(s):
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0 and b > 0:
            ev.append({"kind": "bonus", "factor": b / (a + b), "detail": f"{a:g}:{b:g}"})
    m = RE_SPLIT.search(s)
    if m and not re.search(r"consolidation", s, re.I):
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0 and b > 0 and b < a:
            ev.append({"kind": "split", "factor": b / a, "detail": f"FV {a:g}->{b:g}"})
    m = RE_CONSOL.search(s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0 and b > a:
            ev.append({"kind": "consolidation", "factor": b / a, "detail": f"FV {a:g}->{b:g}"})
    m = RE_RIGHTS.search(s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        prem = float(m.group(3)) if m.group(3) else np.nan
        issue = (face_value if np.isfinite(face_value) else 0.0) + (prem if np.isfinite(prem) else 0.0)
        ev.append({"kind": "rights", "factor": np.nan, "detail": f"{a:g}:{b:g} @ {issue:g}",
                   "_a": a, "_b": b, "_issue": issue if np.isfinite(prem) else np.nan})
    if RE_ARRANGE.search(s) and not any(e["kind"] in ("bonus", "split") for e in ev):
        ev.append({"kind": "arrangement", "factor": np.nan, "detail": s[:60]})
    for m in RE_DIV.finditer(s):
        ev.append({"kind": "dividend", "factor": np.nan, "detail": f"Rs {m.group(1)}",
                   "_div": float(m.group(1))})
    return ev


def events(ann: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for r in ann.itertuples(index=False):
        for e in parse_subject(r.subject, r.faceVal):
            rows.append({"ISIN": r.isin, "Symbol": r.symbol, "ex_date": r.exDate, "subject": r.subject, **e})
    ev = pd.DataFrame(rows)
    if ev.empty:
        return ev
    # the same event announced twice (revisions) -> keep one per ISIN, date, kind, detail
    return ev.drop_duplicates(["ISIN", "ex_date", "kind", "detail"]).reset_index(drop=True)


def match_security(ev: pd.DataFrame, px: pd.DataFrame, window_days: int = 7):
    """SecurityID for each announcement. NSE's announcement feed sometimes carries a
    different ISIN of the same company than the price files, so: exact ISIN first,
    then the symbol that was trading around the ex-date, then the issuer code
    (ISIN chars 4-7) if exactly one security of that issuer was trading then."""
    isin_sid = px.drop_duplicates("ISIN").set_index("ISIN")["SecurityID"]
    runs = px.groupby(["SecurityID", "Symbol"])["Date"].agg(["min", "max"]).reset_index()
    px_iss = px.assign(iss=px["ISIN"].str[3:7])
    iruns = px_iss.groupby(["SecurityID", "iss"])["Date"].agg(["min", "max"]).reset_index()
    by_sym = {k: g for k, g in runs.groupby("Symbol")}
    by_iss = {k: g for k, g in iruns.groupby("iss")}
    w = pd.Timedelta(days=window_days)
    sid_out, how = [], []
    for isin, sym, d in zip(ev["ISIN"], ev["Symbol"], ev["ex_date"]):
        if isin in isin_sid.index:
            sid_out.append(isin_sid[isin]); how.append("isin"); continue
        g = by_sym.get(str(sym).strip())
        if g is not None:
            hit = g[(g["min"] <= d + w) & (g["max"] >= d - w)]
            if len(hit["SecurityID"].unique()) == 1:
                sid_out.append(hit["SecurityID"].iloc[0]); how.append("symbol"); continue
        g = by_iss.get(str(isin)[3:7])
        if g is not None:
            hit = g[(g["min"] <= d + w) & (g["max"] >= d - w)]
            if len(hit["SecurityID"].unique()) == 1:
                sid_out.append(hit["SecurityID"].iloc[0]); how.append("issuer"); continue
        sid_out.append(None); how.append("")
    return sid_out, how


def price_factors(ev: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    """Attach SecurityID and the ex-date trading day, and fill price-dependent factors.
    px: store rows with Date, ISIN, SecurityID, Open, Close."""
    ev = ev.copy()
    ev["SecurityID"], ev["matched_by"] = match_security(ev, px)
    ev = ev[ev["SecurityID"].notna()].copy()
    by = {sid: g.sort_values("Date").reset_index(drop=True) for sid, g in px.groupby("SecurityID")}
    eff, prior_close, ex_open, ex_close = [], [], [], []
    for sid, d in zip(ev["SecurityID"], ev["ex_date"]):
        g = by[sid]
        i = int(g["Date"].searchsorted(d))
        if i >= len(g) or i == 0:
            eff.append(pd.NaT if i >= len(g) else g["Date"].iloc[i])
            prior_close.append(np.nan)
            ex_open.append(np.nan)
            ex_close.append(np.nan)
            continue
        eff.append(g["Date"].iloc[i])
        prior_close.append(g["Close"].iloc[i - 1])
        ex_open.append(g["Open"].iloc[i])
        ex_close.append(g["Close"].iloc[i])
    ev["effective_date"], ev["prior_close"], ev["ex_open"], ev["ex_close"] = eff, prior_close, ex_open, ex_close
    C = ev["prior_close"]

    r = ev["kind"] == "rights"
    if r.any():
        a, b, iss = ev.loc[r, "_a"], ev.loc[r, "_b"], ev.loc[r, "_issue"]
        terp = (b * C[r] + a * iss) / ((a + b) * C[r])
        ev.loc[r, "factor"] = np.where(iss.notna() & (iss < C[r]), terp, 1.0)
    ev["exact"] = ev["kind"].isin(["split", "bonus", "consolidation"])
    exact_comb = (ev.assign(f=ev["factor"].where(ev["exact"], 1.0))
                    .groupby(["SecurityID", "effective_date"])["f"].transform("prod"))
    # price-implied factors exclude same-day exact events (e.g. bonus + demerger together)
    ar = ev["kind"] == "arrangement"
    gap = ev["ex_open"] / C / exact_comb
    ev.loc[ar, "factor"] = np.where((np.log(gap[ar]).abs() > np.log(1 + ARRANGE_MIN_GAP)) & (gap[ar] < 1),
                                    gap[ar], 1.0)
    dv = ev["kind"] == "dividend"
    if dv.any():
        ev.loc[dv, "factor"] = (1 - ev.loc[dv, "_div"] / (C[dv] * exact_comb[dv])).clip(lower=0.01)
        special = dv & ((ev["factor"] <= 1 - SPECIAL_DIV) | ev["subject"].str.contains("special", case=False))
        ev.loc[special, "kind"] = "special_dividend"

    # check: did the raw price move by about the combined exact factor on the ex-date?
    move = ev["ex_close"] / C
    ev["price_move"] = move
    # with a same-day demerger the price falls by more than the exact factor alone
    with_arr = ev.assign(a=ev["kind"].eq("arrangement")).groupby(["SecurityID", "effective_date"])["a"].transform("any")
    ok = (np.log(move / exact_comb).abs() < np.log(1.25)) | (with_arr & (move < exact_comb * 1.25))
    ev["price_confirms"] = np.where(ev["exact"] & move.notna(), ok, np.nan)
    # an announced split/bonus the price did not show is not applied (announcement error,
    # cancelled, or wrong ex-date); it stays in the table, flagged
    ev["applied"] = ~(ev["exact"] & (ev["price_confirms"] == 0))
    keep = ["SecurityID", "matched_by", "ISIN", "Symbol", "ex_date", "effective_date", "kind", "factor", "exact",
            "detail", "subject", "prior_close", "ex_open", "ex_close", "price_move", "price_confirms", "applied"]
    return ev[keep].sort_values(["SecurityID", "effective_date"]).reset_index(drop=True)


def infer_from_isin_changes(px: pd.DataFrame, ev: pd.DataFrame, tol: float = 0.04) -> pd.DataFrame:
    """Fallback for splits/consolidations NSE did not announce: the ISIN changes (face
    value change) and the overnight gap matches a clean ratio, with no announced exact
    event within 3 trading days."""
    from .audit import nearest_ca
    d = px.sort_values(["SecurityID", "Date"])
    g = d.groupby("SecurityID")
    prev_isin, prior = g["ISIN"].shift(1), g["Close"].shift(1)
    ch = d[(d["ISIN"] != prev_isin) & prev_isin.notna()].assign(prior_close=prior, gap=d["Open"] / prior)
    known = ev[ev["exact"]][["SecurityID", "effective_date"]].dropna()
    rows = []
    for r in ch.itertuples(index=False):
        k = known[known["SecurityID"] == r.SecurityID]["effective_date"]
        if len(k) and (abs((k - r.Date).dt.days) <= 5).any():
            continue
        name, f = nearest_ca(r.gap, tol)
        if not name or abs(np.log(r.gap)) < np.log(1.35):
            continue
        rows.append({"SecurityID": r.SecurityID, "matched_by": "isin_change", "ISIN": r.ISIN, "Symbol": r.Symbol,
                     "ex_date": r.Date, "effective_date": r.Date, "kind": "inferred_split", "factor": f,
                     "exact": False, "detail": name, "subject": "inferred: ISIN change + clean price ratio",
                     "prior_close": r.prior_close, "ex_open": r.Open, "ex_close": r.Close,
                     "price_move": r.Close / r.prior_close, "price_confirms": np.nan, "applied": True})
    return pd.DataFrame(rows)


def combine_same_day(ev: pd.DataFrame) -> pd.DataFrame:
    """One factor per security per effective date (e.g. a bonus and a split together)."""
    ev = ev[ev["effective_date"].notna() & ev["factor"].notna()]
    if "applied" in ev:
        ev = ev[ev["applied"].astype(bool)]
    return (ev.groupby(["SecurityID", "effective_date"])["factor"].prod().rename("ratio").reset_index()
              .rename(columns={"effective_date": "Date"}))
