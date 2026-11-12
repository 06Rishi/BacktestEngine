"""Zerodha equity-delivery costs, in rupees per order.

Rates live in the `costs:` block of config.yaml. Source: Zerodha charges page and the
support article "What are transaction charges and how do NSE, BSE, and MCX calculate it?",
checked 2026-11-13. The NSE transaction charge already includes the IPFT charge, so there
is no separate IPFT line.

Slippage is not a charge: it moves the fill price (`fill_price`).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields

CRORE = 1e7


@dataclass(frozen=True)
class Charges:
    brokerage: float
    stt: float
    transaction: float
    sebi: float
    gst: float
    stamp: float
    dp: float = 0.0

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.transaction + self.sebi + self.gst + self.stamp + self.dp

    def __add__(self, o: "Charges") -> "Charges":
        return Charges(*(getattr(self, f.name) + getattr(o, f.name) for f in fields(self)))


@dataclass(frozen=True)
class CostModel:
    brokerage: float = 0.0                   # ₹ per order (Zerodha delivery: 0)
    stt_pct: float = 0.1                     # buy and sell
    transaction_pct: float = 0.00307         # NSE, buy and sell; includes IPFT
    sebi_per_crore: float = 10.0             # buy and sell
    gst_pct: float = 18.0                    # on brokerage + transaction + SEBI
    stamp_pct_buy: float = 0.015             # buy only (uniform rate since July 2020; applied to all history)
    dp_per_scrip_sell_day: float = 15.34     # once per stock per day it is sold
    slippage_bps_buy: float = 10.0
    slippage_bps_sell: float = 10.0

    def __post_init__(self):
        for f in fields(self):
            v = getattr(self, f.name)
            if not isinstance(v, (int, float)) or v < 0:
                raise ValueError(f"costs.{f.name} must be a non-negative number, got {v!r}")

    @classmethod
    def from_config(cls, raw: dict) -> "CostModel":
        block = dict(raw.get("costs") or {})
        known = {f.name for f in fields(cls)}
        unknown = set(block) - known
        if unknown:
            raise ValueError(f"config.yaml costs: unknown key(s) {sorted(unknown)}")
        return cls(**block)

    def with_slippage(self, bps_buy: float, bps_sell: float | None = None) -> "CostModel":
        d = asdict(self)
        d.update(slippage_bps_buy=bps_buy, slippage_bps_sell=bps_buy if bps_sell is None else bps_sell)
        return CostModel(**d)

    def fill_price(self, side: str, price: float) -> float:
        if side == "buy":
            return price * (1 + self.slippage_bps_buy / 1e4)
        if side == "sell":
            return price * (1 - self.slippage_bps_sell / 1e4)
        raise ValueError(side)

    def order(self, side: str, value: float, dp: bool = False) -> Charges:
        """Charges on one order of `value` rupees. `dp=True` adds the DP charge (sells only)."""
        if side not in ("buy", "sell"):
            raise ValueError(side)
        if value < 0:
            raise ValueError("order value must be non-negative")
        txn = value * self.transaction_pct / 100
        sebi = value * self.sebi_per_crore / CRORE
        gst = (self.brokerage + txn + sebi) * self.gst_pct / 100
        return Charges(
            brokerage=self.brokerage if value > 0 else 0.0,
            stt=value * self.stt_pct / 100,
            transaction=txn,
            sebi=sebi,
            gst=gst,
            stamp=value * self.stamp_pct_buy / 100 if side == "buy" else 0.0,
            dp=self.dp_per_scrip_sell_day if (dp and side == "sell") else 0.0,
        )


class DPTracker:
    """DP is charged once per stock per sell day, however many sell orders that day."""

    def __init__(self):
        self._seen: set = set()

    def due(self, day, security) -> bool:
        key = (day, security)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True
