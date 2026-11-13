"""Entry eligibility (amendment A4), defined once and shared by strategy entries, placebos and
the random-3 baseline.

A security is eligible on day t when all of these hold:
  - it is a point-in-time index member on t;
  - it traded on t (series EQ/BE/BZ: the panel holds only these, so a raw close exists);
  - the strategy's warm-up is satisfied (A1; passed in as a mask);
  - raw close ≥ min_price (default ₹10);
  - the average traded value over the last `adv_days` sessions up to t is ≥ min_adv (default ₹1 crore).

Everything uses data up to and including t's close, when the decision is made. Nothing from t+1.
Index removal affects entry only; holdings exit by the strategy's rules unless
`on_universe_exit: sell` (amendment 2.5).
"""
from __future__ import annotations

from dataclasses import dataclass, fields

import numpy as np
import pandas as pd

UNIVERSE_EXIT = ("hold", "sell")


@dataclass(frozen=True)
class EligibilityConfig:
    min_price: float = 10.0          # ₹, raw close
    min_adv: float = 1e7             # ₹ (1 crore), average traded value
    adv_days: int = 20               # sessions in the average
    on_universe_exit: str = "hold"   # hold | sell

    def __post_init__(self):
        if self.on_universe_exit not in UNIVERSE_EXIT:
            raise ValueError(f"eligibility.on_universe_exit: '{self.on_universe_exit}' is not one of "
                             f"{list(UNIVERSE_EXIT)}")
        if not (self.min_price >= 0 and self.min_adv >= 0):
            raise ValueError("eligibility: min_price and min_adv must be ≥ 0")
        if not isinstance(self.adv_days, (int, np.integer)) or self.adv_days < 1:
            raise ValueError("eligibility.adv_days must be a whole number ≥ 1")

    @classmethod
    def from_config(cls, raw: dict) -> "EligibilityConfig":
        block = dict(raw.get("eligibility") or {})
        unknown = set(block) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"config.yaml eligibility: unknown key(s) {sorted(unknown)}")
        return cls(**block)


def rolling_adv(turnover: pd.DataFrame, days: int) -> pd.DataFrame:
    """Average traded value (₹) over the last `days` sessions up to and including t.
    A session with no trade counts as zero. NaN until `days` sessions after the first trade."""
    sessions = turnover.notna().cumsum().gt(0).cumsum()
    return turnover.fillna(0.0).rolling(days, min_periods=days).mean().where(sessions >= days)


def eligible(raw_close: pd.DataFrame, turnover: pd.DataFrame, members: pd.DataFrame | None = None,
             warmup: pd.DataFrame | None = None, cfg: EligibilityConfig = EligibilityConfig()) -> pd.DataFrame:
    """Boolean (dates × securities) mask of A4 eligibility."""
    idx, cols = raw_close.index, raw_close.columns
    ok = raw_close.notna() & (raw_close >= cfg.min_price)
    adv = rolling_adv(turnover.reindex(index=idx, columns=cols), cfg.adv_days)
    ok &= adv.ge(cfg.min_adv).fillna(False).astype(bool)
    if members is not None:
        ok &= members.reindex(index=idx, columns=cols).fillna(False).astype(bool)
    if warmup is not None:
        ok &= warmup.reindex(index=idx, columns=cols).fillna(False).astype(bool)
    return ok


def from_panel(panel, warmup: pd.DataFrame | None = None,
               cfg: EligibilityConfig = EligibilityConfig()) -> pd.DataFrame:
    return eligible(panel.raw_close, panel.turnover, panel.members, warmup, cfg)
