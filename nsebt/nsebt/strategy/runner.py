"""Run a strategy end to end: store -> market and panel -> strategy frames -> engine -> run record."""
from __future__ import annotations

import pandas as pd

from ..backtest import market as mk
from ..backtest.run import Run, run
from ..backtest.sim import SimConfig
from .base import Context, Strategy, to_signals


def holdout_start(cfg) -> pd.Timestamp:
    return pd.Timestamp(cfg.raw.get("research", {}).get("holdout_start", "2022-01-01"))


def backtest(cfg, strategy: Strategy, start=None, end=None, seed: int = 0, sim: SimConfig | None = None,
             loaded=None, use_cache: bool = True, allow_holdout: bool = False) -> Run:
    """Signals are computed on the full history (warm-up intact); the engine runs on [start, end].
    The holdout (config research.holdout_start onward) is refused until M4.3's `--final` lock exists,
    unless `allow_holdout`. `loaded` = (market, panel) from market.from_store, to reuse a load."""
    m, panel = loaded or mk.from_store(cfg, universe=strategy.universe)
    end = pd.Timestamp(end) if end is not None else m.dates[-1]
    if end >= holdout_start(cfg) and not allow_holdout:
        raise PermissionError(f"the holdout from {holdout_start(cfg).date()} is locked; pass an earlier end")
    features = None
    if use_cache:
        from ..features import FeatureCache
        features = FeatureCache(cfg.root / cfg.raw["paths"].get("features", "data/features"), panel)
    out = strategy.generate(Context(panel, dict(strategy.params), features))
    mw = m.slice(start, end)
    sc = sim or SimConfig.from_config(cfg.raw, seed=seed)
    return run(mw, to_signals(out, mw), sc, strategy=strategy.name, params=strategy.params,
               universe=str(strategy.universe), store=cfg.path("store"), strategy_source=strategy.source())
