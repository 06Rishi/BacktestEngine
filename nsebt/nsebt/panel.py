"""Build one price panel: Bloomberg history, extended with NSE bhavcopy data."""
from __future__ import annotations

import pandas as pd

from .bloomberg import OHLCV


def combine(bbg: dict[str, pd.DataFrame], nse: pd.DataFrame, mapping: pd.DataFrame,
            trusted: set[str] | None = None) -> dict[str, pd.DataFrame]:
    """Append NSE rows dated after each stock's last Bloomberg date.
    `trusted`: if given, only extend these Bloomberg IDs (e.g. those that passed
    validate_overlap). Others keep Bloomberg history only."""
    out = {k: v.copy() for k, v in bbg.items()}
    if nse.empty:
        return out
    m = mapping.dropna(subset=["NSE_Symbol"])
    by_sym = {s: g.set_index("Date")[OHLCV].sort_index() for s, g in nse.groupby("Symbol")}
    for b, s in zip(m["Bloomberg_ID"], m["NSE_Symbol"]):
        if trusted is not None and b not in trusted:
            continue
        if s not in by_sym:
            continue
        new = by_sym[s]
        if b in out:
            new = new[new.index > out[b].index.max()]
            out[b] = pd.concat([out[b], new])
        else:
            out[b] = new
    return out


def matrix(panel: dict[str, pd.DataFrame], field: str) -> pd.DataFrame:
    """Dates x tickers matrix for one field."""
    return pd.DataFrame({t: d[field] for t, d in panel.items()}).sort_index()
