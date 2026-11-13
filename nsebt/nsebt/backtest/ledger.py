"""Position ledger for corporate actions (amendments 2.2, 2.3).

Signals use back-adjusted prices; execution uses raw prices plus this ledger, which applies
each corporate action to share counts, cash and receivables on its effective date (the
security's first trading day on or after the ex-date).

`factor` (from corporate_actions.parquet) multiplies prices before the ex-date:
  share events (split, bonus, consolidation, inferred split): shares ÷ factor, price levels × factor
  value events (dividends, rights, demergers): value moved out of the share, per pre-event share
    = prior_close × (1 − factor). For dividends this is the dividend D; for rights it is
    (a/b)(TERP − I), the value of the entitlement; for demergers it is the price-implied child value.

Entitlement under T+1: events are applied at the start of the effective day, before fills, so a
position held at the previous close is entitled and a buy filled at the ex-date open is not.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, fields

import numpy as np
import pandas as pd

SHARE_KINDS = ("split", "bonus", "consolidation", "inferred_split")
VALUE_KINDS = ("dividend", "special_dividend", "rights", "arrangement")
APPROX_KINDS = ("rights", "arrangement")      # theoretical / price-implied values

RE_RS = re.compile(r"r[se]\.?\s*(\d+(?:\.\d+)?)", re.I)

OPTIONS = {
    "dividends": ("receivable", "instant", "ignore"),
    "rights": ("sell_entitlement", "subscribe", "ignore"),
    "demerger": ("receivable", "table_then_receivable"),
    "merger": ("convert_then_sell", "convert_and_hold"),
}


@dataclass(frozen=True)
class LedgerConfig:
    dividends: str = "receivable"
    dividend_pay_lag_days: int = 30          # calendar days (Companies Act limit for paying a declared dividend)
    rights: str = "sell_entitlement"
    rights_realise_lag_days: int = 15        # trading days (about the entitlement trading window)
    demerger: str = "receivable"
    demerger_realise_lag_days: int = 60      # calendar days
    merger: str = "convert_then_sell"

    def __post_init__(self):
        for key, allowed in OPTIONS.items():
            v = getattr(self, key)
            if v not in allowed:
                raise ValueError(f"corporate_actions.{key}: '{v}' is not one of {list(allowed)}")
        for key in ("dividend_pay_lag_days", "rights_realise_lag_days", "demerger_realise_lag_days"):
            v = getattr(self, key)
            if not isinstance(v, (int, np.integer)) or isinstance(v, bool) or v < 0:
                raise ValueError(f"corporate_actions.{key}: must be a whole number ≥ 0, got {v!r}")

    @classmethod
    def from_config(cls, raw: dict) -> "LedgerConfig":
        block = dict(raw.get("corporate_actions") or {})
        unknown = set(block) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"config.yaml corporate_actions: unknown key(s) {sorted(unknown)}")
        return cls(**block)


@dataclass(frozen=True)
class Event:
    t: int                  # index of the effective date in the market calendar
    j: int                  # security column
    kind: str
    factor: float           # price multiplier before the ex-date (NaN for some dividends)
    amount_ps: float        # ₹ of value per pre-event share (value events); 0 for share events
    ex_date: pd.Timestamp
    detail: str
    approx: bool            # value is theoretical or price-implied, not a known cash amount

    @property
    def is_share(self) -> bool:
        return self.kind in SHARE_KINDS


# --------------------------------------------------------------------------- pure helpers

def dividend_amount(detail) -> float:
    """₹ per share from a detail such as 'Rs 2.10'; NaN when absent."""
    m = RE_RS.search(str(detail or ""))
    return float(m.group(1)) if m else float("nan")


def value_per_share(kind: str, factor: float, prior_close: float, detail=None) -> float:
    """₹ moved out of one pre-event share by a value event: prior_close × (1 − factor).
    Dividends with no factor fall back to the declared amount in `detail`."""
    if np.isfinite(factor) and np.isfinite(prior_close):
        return max(prior_close * (1.0 - factor), 0.0)
    if kind in ("dividend", "special_dividend"):
        return dividend_amount(detail)
    return float("nan")


RE_RIGHTS = re.compile(r"(\d+)\s*:\s*(\d+)\s*@\s*(\d+(?:\.\d+)?)")


def parse_rights(detail) -> tuple[int, int, float] | None:
    """(a, b, issue price) from '1:15 @ 1257': a new shares for every b held at the issue price."""
    m = RE_RIGHTS.search(str(detail or ""))
    if not m or int(m.group(1)) <= 0 or int(m.group(2)) <= 0 or float(m.group(3)) <= 0:
        return None
    return int(m.group(1)), int(m.group(2)), float(m.group(3))


def split_shares(shares: int, factor: float) -> tuple[int, float]:
    """(whole shares, fractional entitlement) after a share event: shares ÷ factor."""
    new = shares / factor
    whole = math.floor(new + 1e-9)
    frac = new - whole
    return whole, (frac if frac > 1e-9 else 0.0)


# --------------------------------------------------------------------------- scheduling

def schedule(dates: pd.DatetimeIndex, ids: pd.Index, ca: pd.DataFrame | None):
    """Map corporate-action rows onto the market calendar.

    Returns (events, unscheduled):
      events: {t: [Event, ...]} with value events before share events on the same day
      unscheduled: [(j, ex_date, kind, detail)] for applied rows with no effective_date
    Rows with applied == False, unknown securities, or effective dates outside the calendar are dropped.
    """
    events: dict[int, list[Event]] = {}
    unscheduled: list[tuple] = []
    if ca is None or not len(ca):
        return events, unscheduled
    col = pd.Index(ids).get_indexer(ca["SecurityID"])
    keep = (col >= 0) & ca["applied"].astype(bool).to_numpy()
    ca, col = ca[keep], col[keep]
    kinds = set(SHARE_KINDS) | set(VALUE_KINDS)
    first, last = dates[0], dates[-1]
    for r, j in zip(ca.itertuples(index=False), col):
        if r.kind not in kinds:
            continue
        if pd.isna(r.effective_date):
            unscheduled.append((int(j), pd.Timestamp(r.ex_date), r.kind, r.detail))
            continue
        eff = pd.Timestamp(r.effective_date)
        if eff <= first or eff > last:
            continue        # before the first day nothing can be held; after the end it never happens
        t = int(dates.searchsorted(eff, side="left"))
        f = float(r.factor) if pd.notna(r.factor) else float("nan")
        if r.kind in SHARE_KINDS:
            if not np.isfinite(f) or f <= 0:
                continue
            amt = 0.0
        else:
            amt = value_per_share(r.kind, f, float(r.prior_close) if pd.notna(r.prior_close) else np.nan,
                                  r.detail)
            if not np.isfinite(amt):
                continue
        events.setdefault(t, []).append(Event(t, int(j), r.kind, f, amt, pd.Timestamp(r.ex_date),
                                              str(r.detail), r.kind in APPROX_KINDS))
    for t in events:
        events[t].sort(key=lambda e: (e.j, e.is_share))     # value events first, per security
    return events, unscheduled
