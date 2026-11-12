"""Point-in-time universe: a stock is a member on date t if it was in the most
recent membership snapshot on or before t."""
from __future__ import annotations

import pandas as pd


class Universe:
    def __init__(self, membership: pd.DataFrame):
        m = membership[["Date", "Bloomberg_ID"]].copy()
        m["Date"] = pd.to_datetime(m["Date"])
        self.snapshots = sorted(m["Date"].unique())
        self._sets = {d: set(g["Bloomberg_ID"]) for d, g in m.groupby("Date")}
        # snapshot x ticker boolean table
        self._table = (m.assign(v=True).pivot_table(index="Date", columns="Bloomberg_ID",
                                                   values="v", aggfunc="any")
                         .fillna(False).astype(bool).sort_index())

    def members_on(self, date) -> set[str]:
        date = pd.Timestamp(date)
        prior = [d for d in self.snapshots if d <= date]
        return self._sets[prior[-1]] if prior else set()

    def mask(self, index: pd.DatetimeIndex, columns) -> pd.DataFrame:
        """Dates x tickers boolean membership matrix (False before the first snapshot)."""
        t = self._table.reindex(columns=list(columns), fill_value=False)
        full = t.reindex(t.index.union(index)).ffill()
        return full.reindex(index).fillna(False).astype(bool)
