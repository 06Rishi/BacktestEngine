"""Map Bloomberg IDs to NSE securities, and reconcile corporate actions between them.

Matching uses prices, not names: daily returns of the same stock agree in Bloomberg's
adjusted data and NSE's adjusted data on almost every day, whatever the two sources
call it. For each Bloomberg series:
  1. candidates = ISIN match (if a Bloomberg->ISIN file is given), name match
     (Bloomberg root == any NSE symbol the security ever used), and the five NSE
     securities whose returns correlate best over the overlap;
  2. each candidate is scored by `agreement`: the share of overlapping days on which
     the two daily returns differ by less than 0.2%;
  3. the best candidate is accepted if agreement >= 0.90 over at least 20 days and
     no other Bloomberg ID claims the same security with a better score.

Reconciliation: for a matched pair, Bloomberg close / NSE raw close is Bloomberg's
own cumulative adjustment factor. Dividing it by NSE's factor (from PrevClose
re-bases) leaves a residual that is flat when both sources adjust for the same
events; its steps show events only one source adjusted for.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .mapping import root

AGREE_TOL = 0.002
MIN_AGREEMENT = 0.90
MIN_OVERLAP = 20


def _returns(m: pd.DataFrame) -> pd.DataFrame:
    return np.log(m).diff()


def _corr_topk(rb: pd.DataFrame, rn: pd.DataFrame, k: int = 5) -> dict[str, list[str]]:
    """Top-k NSE columns by return correlation for each Bloomberg column (pairwise
    overlap handled by masking)."""
    B, N = rb.to_numpy(), rn.to_numpy()
    mb, mn = ~np.isnan(B), ~np.isnan(N)
    Bz = np.where(mb, B, 0.0)
    Nz = np.where(mn, N, 0.0)
    cnt = mb.T.astype(np.float32) @ mn.astype(np.float32)
    sb = Bz.T @ mn          # sum of b over overlap
    sn = mb.T.astype(float) @ Nz
    sbb = (Bz ** 2).T @ mn
    snn = mb.T.astype(float) @ (Nz ** 2)
    sbn = Bz.T @ Nz
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = sbn - sb * sn / cnt
        var = (sbb - sb ** 2 / cnt) * (snn - sn ** 2 / cnt)
        corr = cov / np.sqrt(var)
    corr[cnt < MIN_OVERLAP] = -np.inf
    corr = np.nan_to_num(corr, nan=-np.inf)
    top = np.argsort(-corr, axis=1)[:, :k]
    return {b: [rn.columns[j] for j in top[i] if np.isfinite(corr[i, j])] for i, b in enumerate(rb.columns)}


def _agreement(b: pd.Series, n: pd.Series, tol: pd.Series | None = None) -> tuple[float, int]:
    """Share of overlapping days where the two daily log returns agree within tol
    (per day; defaults to AGREE_TOL)."""
    both = pd.concat([b, n], axis=1).dropna()
    if len(both) == 0:
        return 0.0, 0
    t = AGREE_TOL if tol is None else tol.reindex(both.index).fillna(AGREE_TOL)
    return float(((both.iloc[:, 0] - both.iloc[:, 1]).abs() < t).mean()), len(both)


def tick_tolerance(close: pd.Series, tick: float = 0.01) -> pd.Series:
    """Bloomberg rounds to 0.01, so a stock at Rs 0.50 can show a 2% 'move' from rounding
    alone. Tolerance per day = max(AGREE_TOL, 1.5 ticks relative to the lower close)."""
    lo = pd.concat([close, close.shift(1)], axis=1).min(axis=1)
    return np.maximum(AGREE_TOL, 1.5 * tick / lo)


def match(bbg: dict[str, pd.DataFrame], nse_adj: pd.DataFrame, history: pd.DataFrame,
          bbg_isin: pd.DataFrame | None = None) -> pd.DataFrame:
    """bbg: {Bloomberg_ID: frame with Close}. nse_adj: long frame with Date, SecurityID,
    AdjClose. history: symbol_history (SecurityID, ISIN, Symbol)."""
    B = pd.DataFrame({k: v["Close"] for k, v in bbg.items()}).sort_index()
    N = nse_adj.pivot_table(index="Date", columns="SecurityID", values="AdjClose")
    idx = B.index.intersection(N.index)
    rb, rn = _returns(B).reindex(idx), _returns(N).reindex(idx)
    rb = rb.loc[:, rb.notna().sum() > 0]

    by_name: dict[str, set] = {}
    for s, sid in zip(history["Symbol"], history["SecurityID"]):
        by_name.setdefault(s, set()).add(sid)
    by_isin = dict(zip(history["ISIN"], history["SecurityID"]))
    isin_of = {}
    if bbg_isin is not None:
        isin_of = dict(zip(bbg_isin["Bloomberg_ID"].str.strip(), bbg_isin["ISIN"].str.strip()))

    top = _corr_topk(rb, rn)
    tol = {b: tick_tolerance(B[b]).reindex(idx) for b in rb.columns}
    rows = []
    for b in B.columns:
        cands: dict[str, str] = {}
        if b in isin_of and isin_of[b] in by_isin:
            cands[by_isin[isin_of[b]]] = "isin"
        for sid in by_name.get(root(b), ()):
            cands.setdefault(sid, "name")
        for sid in top.get(b, []):
            cands.setdefault(sid, "returns")
        scored = []
        for sid, how in cands.items():
            if b not in rb or sid not in rn:
                continue
            a, n = _agreement(rb[b], rn[sid], tol[b])
            scored.append((a, n, sid, how))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        bdays = int(B[b].notna().sum())
        if not scored:
            rows.append({"Bloomberg_ID": b, "SecurityID": None, "method": "", "agreement": np.nan,
                         "overlap_days": 0, "bbg_days": bdays, "runner_up_agreement": np.nan})
            continue
        a, n, sid, how = scored[0]
        rows.append({"Bloomberg_ID": b, "SecurityID": sid, "method": how, "agreement": a,
                     "overlap_days": n, "bbg_days": bdays,
                     "runner_up_agreement": scored[1][0] if len(scored) > 1 else np.nan})
    out = pd.DataFrame(rows)
    ok = (out["agreement"] >= MIN_AGREEMENT) & (out["overlap_days"] >= np.minimum(MIN_OVERLAP, out["bbg_days"] - 1))
    out["status"] = np.where(out["SecurityID"].isna(), "no_overlap",
                             np.where(ok, "ok", "low_agreement"))
    # one security, several Bloomberg IDs: keep the best, mark the rest
    okm = out[out["status"] == "ok"].sort_values(["agreement", "overlap_days"], ascending=False)
    dup = okm[okm.duplicated("SecurityID")].index
    out.loc[dup, "status"] = "duplicate_target"
    return out


def reconcile(bbg: dict[str, pd.DataFrame], nse: pd.DataFrame, mapping: pd.DataFrame,
              step: float = 0.01) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per matched pair: residual = (Bloomberg close / NSE raw close) / NSE factor,
    normalised to 1 on the last common day. Returns (summary per pair, list of steps
    where the residual jumps by more than `step`)."""
    pairs = mapping[mapping["status"] == "ok"]
    summ, steps = [], []
    g = {k: v for k, v in nse.groupby("SecurityID")}
    for b, sid in zip(pairs["Bloomberg_ID"], pairs["SecurityID"]):
        n = g[sid].set_index("Date")
        both = pd.concat([bbg[b]["Close"].rename("bbg"), n["Close"].rename("raw"),
                          n["AdjFactor"].rename("f")], axis=1).dropna()
        if len(both) < 2:
            continue
        res = both["bbg"] / both["raw"] / both["f"]
        res = res / res.iloc[-1]
        jump = np.log(res).diff()
        big = jump[jump.abs() > np.log(1 + step)]
        for d, j in big.items():
            steps.append({"Bloomberg_ID": b, "SecurityID": sid, "Date": d, "residual_step": float(np.exp(j))})
        summ.append({"Bloomberg_ID": b, "SecurityID": sid, "days": len(both),
                     "residual_min": float(res.min()), "residual_max": float(res.max()),
                     "residual_first": float(res.iloc[0]), "n_steps": len(big),
                     "bbg_factor_first": float((both["bbg"] / both["raw"]).iloc[0] /
                                               (both["bbg"] / both["raw"]).iloc[-1])})
    return pd.DataFrame(summ), pd.DataFrame(steps)


def map_by_name(ids, history: pd.DataFrame, around: dict | None = None) -> pd.DataFrame:
    """For Bloomberg IDs with no Bloomberg prices (so no returns to compare): the NSE
    security whose symbol equals the Bloomberg root, preferring one trading around the
    given date. Unvalidated by prices; marked method 'name_only'."""
    rows = []
    for b in ids:
        r = root(b)
        h = history[history["Symbol"] == r]
        if around and b in around and len(h):
            d = pd.Timestamp(around[b])
            near = h[(h["first_date"] <= d + pd.Timedelta(days=120)) & (h["last_date"] >= d - pd.Timedelta(days=120))]
            h = near if len(near) else h
        sids = h["SecurityID"].unique()
        rows.append({"Bloomberg_ID": b, "SecurityID": sids[0] if len(sids) == 1 else None,
                     "method": "name_only" if len(sids) == 1 else "", "status":
                     "ok_name_only" if len(sids) == 1 else ("ambiguous_name" if len(sids) > 1 else "no_match")})
    return pd.DataFrame(rows)


def apply_overrides(mp: pd.DataFrame, ov: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    """Manual Bloomberg_ID -> NSE symbol decisions (bbg_overrides.csv). They win over
    automatic matches; a symbol is resolved to the latest security that used it."""
    latest = history.sort_values("last_date").groupby("Symbol")["SecurityID"].last()
    mp = mp.copy()
    for b, sym in zip(ov["Bloomberg_ID"].str.strip(), ov["NSE_Symbol"].str.strip()):
        sid = latest.get(sym)
        if sid is None:
            continue
        row = {"Bloomberg_ID": b, "SecurityID": sid, "method": "override", "status": "ok_override"}
        if (mp["Bloomberg_ID"] == b).any():
            for k, v in row.items():
                mp.loc[mp["Bloomberg_ID"] == b, k] = v
        else:
            mp = pd.concat([mp, pd.DataFrame([row])], ignore_index=True)
    return mp
