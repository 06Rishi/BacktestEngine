"""The Strategy interface (M3.1, frozen).

A strategy turns a research panel (adjusted prices for signals; raw fields for price filters)
into decision frames. YAML strategies compile to this interface; Python strategies implement it.

    class MyStrategy(Strategy):
        name = "my_strategy"
        defaults = {"n": 20}
        def generate(self, ctx: Context) -> Output: ...

Contract (changing it needs the user's approval, stop rule S2):
  - Every frame in `Output` is indexed like `ctx.panel` (dates x SecurityID). Decisions are taken
    at the close of each row's date and filled at the next open by the engine.
  - `entry`, `exit`, `warmup`: bool. `score`: float, higher ranks first. Missing = False / NaN.
  - `stop`, `target`, `trail`: distances as fractions of the decision close (0.05 = 5%), fixed at
    entry (the engine sets the stop and target at the fill price; the trailing stop follows the
    highest raw close). Distances are scale-free, so adjusted prices give the right fraction.
  - `warmup`: True from the first row where everything the strategy reads is defined (A1). The
    engine ANDs it with A4 eligibility (member, traded, raw close >= min price, ADV >= min ADV).
  - A strategy must use data up to and including each row only (no look-ahead; tested).
  - `source()` returns the text that defines the strategy (YAML text or Python source); its hash is
    stored in the run record.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..backtest.sim import Signals, Sizing


@dataclass
class Context:
    """What a strategy may read."""
    panel: object                       # data.Panel: adjusted fields, raw_* fields, members, indices
    params: dict
    features: object | None = None      # features.FeatureCache over the same panel (optional)

    def indicator(self, name: str, **params):
        """A registered indicator on the panel's default inputs, cached when a FeatureCache is given."""
        from .. import indicators as I
        if self.features is not None:
            return self.features.get(name, **params)
        return I.compute(name, self.panel, **params)

    @property
    def members(self) -> pd.DataFrame:
        p = self.panel
        if p.members is None:
            return p.close.notna()
        return p.members.reindex(index=p.close.index, columns=p.close.columns).fillna(False).astype(bool)


@dataclass
class Output:
    entry: pd.DataFrame
    exit: pd.DataFrame | None = None
    score: pd.DataFrame | None = None
    stop: pd.DataFrame | None = None
    target: pd.DataFrame | None = None
    trail: pd.DataFrame | None = None
    warmup: pd.DataFrame | None = None
    max_hold_days: int | None = None
    sizing: Sizing = field(default_factory=Sizing)
    max_positions: int | None = None
    notes: tuple = ()                   # e.g. lint warnings, "uses today's industry tag" (A5)


class Strategy:
    """Base class. Subclasses set `name`, `defaults` and implement `generate`."""
    name: str = "unnamed"
    defaults: dict = {}
    universe: str = "nifty500"

    def __init__(self, **params):
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"{self.name}: unknown parameter(s) {sorted(unknown)}; "
                             f"declared: {sorted(self.defaults)}")
        self.params = {**self.defaults, **params}

    def generate(self, ctx: Context) -> Output:          # pragma: no cover - interface
        raise NotImplementedError

    def source(self) -> str:
        return inspect.getsource(type(self))


def rank_desc(x: pd.DataFrame, mask: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional rank on each date, 1 = highest value, among securities where `mask` is
    True and the value is defined. Ties share the best rank ('min')."""
    return x.where(mask.reindex_like(x).fillna(False).astype(bool)).rank(axis=1, ascending=False, method="min")


def rebalance_days(index: pd.DatetimeIndex, every: str) -> pd.Series:
    """True on the last trading day of each period: 'daily' | 'weekly' | 'monthly'. The last row
    counts only if the next business day starts a new period, so truncating the data never turns
    an ordinary day into a period end."""
    idx = pd.DatetimeIndex(index)
    if every == "daily":
        return pd.Series(True, index=idx)
    if every not in ("weekly", "monthly"):
        raise ValueError(f"rebalance must be daily, weekly or monthly, not '{every}'")
    freq = "W" if every == "weekly" else "M"
    # the next trading day; for the last row, the next business day (known in advance, no look-ahead)
    nxt = pd.DatetimeIndex(list(idx[1:]) + [idx[-1] + pd.offsets.BDay(1)])
    return pd.Series(idx.to_period(freq) != nxt.to_period(freq), index=idx)


def to_signals(out: Output, market) -> Signals:
    """Align strategy frames onto the market (same dates and securities, by label) and attach the
    labels so the engine can verify them."""
    idx, cols = market.dates, market.ids

    def arr(f, fill, dtype):
        if f is None:
            return None
        return f.reindex(index=idx, columns=cols).fillna(fill).to_numpy(dtype)

    def frac(f):
        if f is None:
            return None
        a = f.reindex(index=idx, columns=cols).to_numpy(float)
        return np.where(np.isfinite(a) & (a > 0), a, np.nan)

    return Signals(entry=arr(out.entry, False, bool), exit=arr(out.exit, False, bool),
                   score=None if out.score is None else out.score.reindex(index=idx, columns=cols).to_numpy(float),
                   stop=frac(out.stop), target=frac(out.target), trail=frac(out.trail),
                   max_hold_days=out.max_hold_days, sizing=out.sizing, max_positions=out.max_positions,
                   warmup=arr(out.warmup, False, bool), dates=idx, ids=cols)
