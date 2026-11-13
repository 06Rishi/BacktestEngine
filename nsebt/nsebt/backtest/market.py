"""The one way to build a backtest market from the store.

Loads, every time and together:
  - the panel (adjusted fields for signals, raw fields for execution) for the universe, plus every
    merger acquirer as a non-member (so converted holdings can be held and sold, never bought);
  - corporate_actions.parquet, mergers.csv, lifecycle.parquet (checked against the store's data end);
  - the hashes of every input file, recorded in `Market.sources` and in the run record.

Signals are computed on the returned panel, whose dates and columns equal the market's.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd

from ..data import load_panel
from ..nsestore import PRICE_KINDS
from . import mergers as mg
from .sim import Market


def file_hash(p: Path) -> str:
    p = Path(p)
    if not p.exists():
        return "missing"
    h = hashlib.sha256()
    if p.is_dir():
        for f in sorted(p.rglob("*")):
            if f.is_file():
                h.update(f.relative_to(p).as_posix().encode())
                h.update(f.read_bytes())
    else:
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def paths(cfg) -> dict[str, Path]:
    """Every input file a backtest depends on, from config.yaml."""
    store = cfg.path("store")
    p = cfg.raw["paths"]
    return {
        "build_report": store / "build_report.json",
        "corporate_actions": store / "corporate_actions.parquet",
        "membership": store / "membership.parquet",
        "lifecycle": store / "lifecycle.parquet",
        "mergers_csv": cfg.root / p.get("mergers", "mergers.csv"),
        "lifecycle_overrides_csv": cfg.root / p.get("lifecycle_overrides", "lifecycle_overrides.csv"),
        "ca_overrides_csv": cfg.root / p.get("ca_overrides", "ca_overrides.csv"),
    }


def from_store(cfg, universe: str | list = "nifty500", start=None, end=None, kinds=PRICE_KINDS,
               adv_days: int | None = None):
    """(Market, Panel). `adv_days` defaults to the eligibility block of config.yaml."""
    from .eligibility import EligibilityConfig
    store = cfg.path("store")
    files = paths(cfg)
    adv_days = adv_days or EligibilityConfig.from_config(cfg.raw).adv_days
    table = mg.load(files["mergers_csv"])
    panel = load_panel(store, universe, start, end, kinds=kinds, extra=tuple(table["acquirer"].unique()))
    lc = pd.read_parquet(files["lifecycle"])
    store_end = pd.read_parquet(store / "prices", columns=["Date"])["Date"].max()
    if "data_end" not in lc or pd.Timestamp(lc["data_end"].iloc[0]) != store_end:
        raise ValueError(f"lifecycle.parquet was built for a different data end than the store ({store_end.date()}); "
                         f"run `nsebt lifecycle` or `nsebt build`")
    m = Market.from_panel(panel, adv_days=adv_days, actions=pd.read_parquet(files["corporate_actions"]),
                          mergers=table, lifecycle=lc)
    m.sources = {k: file_hash(v) for k, v in files.items()}
    return m, panel
