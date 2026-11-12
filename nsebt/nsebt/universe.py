"""Universes: which securities a strategy may trade on each date.

  Universe            point-in-time membership: a security is a member on date t if it
                      is in the most recent snapshot effective on or before t
  nifty500(store)     Nifty 500 by SecurityID, from store/membership.parquet
  AllEquities         every security that traded on NSE that day in the chosen series
  watchlist(...)      a fixed list of symbols, resolved to SecurityIDs

All expose members_on(date) -> set and mask(index, columns) -> boolean DataFrame.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd


class Universe:
    def __init__(self, membership: pd.DataFrame, id_col: str = "Bloomberg_ID", date_col: str = "Date"):
        m = membership[[date_col, id_col]].dropna().rename(columns={date_col: "Date", id_col: "id"}).copy()
        m["Date"] = pd.to_datetime(m["Date"])
        self.snapshots = sorted(m["Date"].unique())
        self._sets = {d: set(g["id"]) for d, g in m.groupby("Date")}
        self._table = (m.assign(v=True).pivot_table(index="Date", columns="id", values="v", aggfunc="any")
                         .fillna(False).astype(bool).sort_index())

    def members_on(self, date) -> set[str]:
        date = pd.Timestamp(date)
        prior = [d for d in self.snapshots if d <= date]
        return self._sets[prior[-1]] if prior else set()

    def mask(self, index: pd.DatetimeIndex, columns) -> pd.DataFrame:
        """Dates x ids boolean membership matrix (False before the first snapshot)."""
        t = self._table.reindex(columns=list(columns), fill_value=False)
        full = t.reindex(t.index.union(index)).ffill()
        return full.reindex(index).fillna(False).astype(bool)


def nifty500(store_dir: Path) -> Universe:
    """Point-in-time Nifty 500 keyed by SecurityID, switching on NSE's effective dates."""
    m = pd.read_parquet(Path(store_dir) / "membership.parquet")
    return Universe(m, id_col="SecurityID", date_col="effective_from")


class AllEquities:
    """Every security with a row in the store on that date, in the given series."""

    def __init__(self, prices: pd.DataFrame, series=("EQ", "BE", "BZ")):
        p = prices[prices["Series"].isin(series)][["Date", "SecurityID"]]
        self._table = (p.assign(v=True).pivot_table(index="Date", columns="SecurityID", values="v",
                                                    aggfunc="any").fillna(False).astype(bool))

    def members_on(self, date) -> set[str]:
        date = pd.Timestamp(date)
        if date not in self._table.index:
            return set()
        row = self._table.loc[date]
        return set(row.index[row.to_numpy()])

    def mask(self, index, columns) -> pd.DataFrame:
        return self._table.reindex(index=index, columns=list(columns), fill_value=False).fillna(False).astype(bool)


class Watchlist:
    """A fixed set of securities, member on every date."""

    def __init__(self, ids):
        self.ids = set(ids)

    def members_on(self, date) -> set[str]:
        return set(self.ids)

    def mask(self, index, columns) -> pd.DataFrame:
        cols = list(columns)
        return pd.DataFrame({c: c in self.ids for c in cols}, index=index)


def watchlist(store_dir: Path, symbols) -> tuple[Watchlist, list[str]]:
    """Resolve NSE symbols (current or former) to SecurityIDs. Returns the watchlist and
    the symbols that could not be resolved."""
    h = pd.read_parquet(Path(store_dir) / "symbol_history.parquet")
    latest = h.sort_values("last_date").groupby("Symbol")["SecurityID"].last()
    ids, missing = [], []
    for s in symbols:
        s = str(s).strip().upper()
        (ids.append(latest[s]) if s in latest.index else missing.append(s))
    return Watchlist(ids), missing
