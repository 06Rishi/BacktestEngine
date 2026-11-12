"""Map Bloomberg IDs (e.g. 'MM IN Equity') to NSE symbols (e.g. 'M&M').

Bloomberg's Indian codes often differ from NSE symbols, so matching is done in order:
  1. overrides CSV you maintain (Bloomberg_ID,NSE_Symbol)  - always wins
  2. ISIN match, if you supply both ISIN files              - reliable
  3. Bloomberg root == NSE symbol                           - often right, must be validated
Every match should then pass validate_overlap() before you trust it.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

SUFFIX = " IN Equity"


def root(bbg_id: str) -> str:
    return bbg_id[: -len(SUFFIX)] if bbg_id.endswith(SUFFIX) else bbg_id.split()[0]


def build(bbg_ids, nse_symbols, overrides_csv: Path | None = None,
          bbg_isin_csv: Path | None = None, nse_equity_list_csv: Path | None = None) -> pd.DataFrame:
    """Returns Bloomberg_ID, NSE_Symbol (NaN if unmapped), Method."""
    nse = set(nse_symbols)
    rows = {b: (np.nan, "unmapped") for b in sorted(set(bbg_ids))}

    for b in rows:
        if root(b) in nse:
            rows[b] = (root(b), "direct")

    if bbg_isin_csv and nse_equity_list_csv:
        bi = pd.read_csv(bbg_isin_csv, dtype=str)          # Bloomberg_ID, ISIN
        el = pd.read_csv(nse_equity_list_csv, dtype=str)   # NSE EQUITY_L.csv
        el.columns = [c.strip() for c in el.columns]
        isin_col = next(c for c in el.columns if "ISIN" in c.upper())
        isin_to_sym = dict(zip(el[isin_col].str.strip(), el["SYMBOL"].str.strip()))
        for b, isin in zip(bi["Bloomberg_ID"].str.strip(), bi["ISIN"].str.strip()):
            if b in rows and isin in isin_to_sym:
                rows[b] = (isin_to_sym[isin], "isin")

    if overrides_csv and Path(overrides_csv).exists():
        ov = pd.read_csv(overrides_csv, dtype=str)
        for b, s in zip(ov["Bloomberg_ID"].str.strip(), ov["NSE_Symbol"].str.strip()):
            if b in rows:
                rows[b] = (s, "override")

    out = pd.DataFrame([(b, s, m) for b, (s, m) in rows.items()],
                       columns=["Bloomberg_ID", "NSE_Symbol", "Method"])
    dup = out.dropna(subset=["NSE_Symbol"]).duplicated("NSE_Symbol", keep=False)
    if dup.any():
        bad = out.dropna(subset=["NSE_Symbol"])[dup]
        raise ValueError(f"NSE symbol mapped from several Bloomberg IDs:\n{bad.to_string()}")
    return out


def validate_overlap(mapping: pd.DataFrame, bbg: dict[str, pd.DataFrame], nse: pd.DataFrame,
                     tol: float = 0.01) -> pd.DataFrame:
    """For each mapped pair, compare closes on dates both sources cover.
    status: ok | price_mismatch | no_overlap | missing_data"""
    nse_close = nse.pivot_table(index="Date", columns="Symbol", values="Close")
    res = []
    for b, s, m in mapping.dropna(subset=["NSE_Symbol"]).itertuples(index=False):
        if b not in bbg or s not in nse_close:
            res.append((b, s, m, "missing_data", np.nan, 0))
            continue
        both = pd.concat([bbg[b]["Close"], nse_close[s]], axis=1, keys=["bbg", "nse"]).dropna()
        if both.empty:
            res.append((b, s, m, "no_overlap", np.nan, 0))
            continue
        err = float((both["nse"] / both["bbg"] - 1).abs().median())
        res.append((b, s, m, "ok" if err <= tol else "price_mismatch", err, len(both)))
    return pd.DataFrame(res, columns=["Bloomberg_ID", "NSE_Symbol", "Method", "Status",
                                      "Median_Abs_Diff", "Overlap_Days"])
