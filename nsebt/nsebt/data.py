"""Load the store as wide panels (rows = dates, columns = SecurityID) for research.

    p = load_panel(store, universe="nifty500")
    p.close, p.high, p.volume ...   adjusted (for indicators)
    p.raw_close, p.raw_open ...     as traded (for fills and position sizing)
    p.members                       point-in-time membership mask
    p.index("Nifty 500")            benchmark index closes
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import nsestore, universe as uni

ADJ = {"open": "AdjOpen", "high": "AdjHigh", "low": "AdjLow", "close": "AdjClose", "vwap": "AdjVWAP",
       "volume": "AdjVolume", "deliv_qty": "AdjDelivQty"}
RAW = {"raw_open": "Open", "raw_high": "High", "raw_low": "Low", "raw_close": "Close",
       "raw_volume": "Volume", "turnover": "Turnover", "trades": "Trades", "deliv_pct": "DelivPct",
       "factor": "AdjFactor"}

# NSE sector index for each industry in the Nifty 500 list (fallback: Nifty 500)
SECTOR_INDEX = {
    "Financial Services": "Nifty Financial Services", "Information Technology": "Nifty IT",
    "Healthcare": "Nifty Healthcare Index", "Automobile and Auto Components": "Nifty Auto",
    "Fast Moving Consumer Goods": "Nifty FMCG", "Metals & Mining": "Nifty Metal",
    "Oil Gas & Consumable Fuels": "Nifty Oil & Gas", "Realty": "Nifty Realty",
    "Media Entertainment & Publication": "Nifty Media", "Consumer Durables": "Nifty Consumer Durables",
    "Capital Goods": "Nifty Capital Goods", "Chemicals": "Nifty Chemicals", "Power": "Nifty Power",
    "Construction": "Nifty Construction", "Construction Materials": "Nifty Cement",
    "Telecommunication": "Nifty Telecommunications", "Consumer Services": "Nifty Consumer Services",
    "Services": "Nifty Services Sector",
}


@dataclass
class Panel:
    fields: dict
    members: pd.DataFrame | None
    securities: pd.DataFrame
    indices: pd.DataFrame
    meta: dict = field(default_factory=dict)

    def __getattr__(self, k):
        f = self.__dict__.get("fields", {})
        if k in f:
            return f[k]
        raise AttributeError(k)

    def __getitem__(self, k):
        return self.fields[k]

    @property
    def dates(self):
        return self.fields["close"].index

    @property
    def ids(self):
        return self.fields["close"].columns

    def index(self, name: str = "Nifty 500") -> pd.Series:
        s = self.indices[self.indices["Index"].str.lower() == name.lower()].set_index("Date")["Close"]
        return s.reindex(self.dates)

    def sector_bench(self) -> pd.DataFrame:
        """Each security's sector index close (by today's industry tag; Nifty 500 if none)."""
        ind = self.securities.set_index("SecurityID")["Industry"].reindex(self.ids)
        cols = {}
        cache = {}
        for sid, i in ind.items():
            name = SECTOR_INDEX.get(i, "Nifty 500") if isinstance(i, str) else "Nifty 500"
            if name not in cache:
                s = self.index(name)
                cache[name] = s if s.notna().any() else self.index("Nifty 500")
            cols[sid] = cache[name]
        return pd.DataFrame(cols, index=self.dates)

    def symbols(self) -> pd.Series:
        return self.securities.set_index("SecurityID")["Symbol"].reindex(self.ids)


def load_panel(store: Path, universe: str | list = "nifty500", start=None, end=None,
               series=("EQ", "BE", "BZ"), kinds=nsestore.PRICE_KINDS) -> Panel:
    """universe: 'nifty500' (every security that was ever a member, with a membership
    mask), 'all' (all securities in `series`), or a list of SecurityIDs."""
    store = Path(store)
    px = nsestore.load_prices(store, adjusted=True, kinds=kinds)
    px = px[px["Series"].isin(series)]
    members = None
    if universe == "nifty500":
        u = uni.nifty500(store)
        ids = sorted(set(pd.read_parquet(store / "membership.parquet")["SecurityID"].dropna()))
    elif universe == "all":
        ids = sorted(px["SecurityID"].unique())
        u = None
    else:
        ids, u = sorted(universe), None
    px = px[px["SecurityID"].isin(ids)]
    if start is not None:
        px = px[px["Date"] >= pd.Timestamp(start)]
    if end is not None:
        px = px[px["Date"] <= pd.Timestamp(end)]
    fields = {}
    for name, col in {**ADJ, **RAW}.items():
        fields[name] = px.pivot(index="Date", columns="SecurityID", values=col).sort_index()
    if u is not None:
        members = u.mask(fields["close"].index, fields["close"].columns)
    sec = pd.read_parquet(store / "securities.parquet")
    ix = pd.read_parquet(store / "indices.parquet") if (store / "indices.parquet").exists() else pd.DataFrame(
        columns=["Date", "Index", "Close"])
    return Panel(fields, members, sec, ix, {"universe": universe, "kinds": kinds})
