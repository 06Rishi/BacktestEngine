"""Day-by-day execution simulator on raw prices.

Timing: decisions at the close of day t, fills at the open of day t+1.
Order of events on each day t:
  1. corporate actions on the ex-date (position ledger; see M2)
  2. fills at the raw open: sells first, then buys
  3. stops and targets against the raw high/low (stop first when both are touched)
  4. at the close: mark positions, update trailing references, decide exits and entries

Signals arrive as (dates x securities) arrays aligned with `Market`. They are computed on
adjusted prices elsewhere; the simulator never reads adjusted prices.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .account import Account
from .costs import Charges, CostModel, DPTracker

REASONS_REJECT = ("no slot", "no cash", "cap", "liquidity", "gap", "no stop", "expired")


# --------------------------------------------------------------------------- inputs

@dataclass
class Market:
    """Raw prices as traded. NaN = no trade that day."""
    dates: pd.DatetimeIndex
    ids: pd.Index
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    adv: np.ndarray            # average daily traded value (₹) over the last `adv_days`, up to and including t
    prev_close: np.ndarray     # last traded close before t

    @classmethod
    def from_frames(cls, open_, high, low, close, turnover=None, adv_days: int = 20) -> "Market":
        close = close.sort_index()
        idx, cols = close.index, close.columns
        al = lambda f: f.reindex(index=idx, columns=cols).to_numpy(float)
        tv = turnover if turnover is not None else close * 0 + np.inf
        adv = tv.reindex(index=idx, columns=cols).rolling(adv_days, min_periods=adv_days).mean()
        prev = close.ffill().shift(1)
        return cls(idx, cols, al(open_), al(high), al(low), close.to_numpy(float),
                   adv.to_numpy(float), al(prev))

    @classmethod
    def from_panel(cls, panel, adv_days: int = 20) -> "Market":
        return cls.from_frames(panel.raw_open, panel.raw_high, panel.raw_low, panel.raw_close,
                               panel.turnover, adv_days)

    def locked(self, t: int, j: int) -> str | None:
        """'up' / 'down' when the bar is a single price (open = high = low = close) away from
        the previous close: likely locked at a circuit limit."""
        o, h, l, c, p = self.open[t, j], self.high[t, j], self.low[t, j], self.close[t, j], self.prev_close[t, j]
        if not (o == h == l == c) or not np.isfinite(p) or o == p:
            return None
        return "up" if o > p else "down"


@dataclass
class Sizing:
    mode: str = "equal"        # equal | fixed | risk_pct
    value: float | None = None  # fixed: ₹; risk_pct: % of equity lost if the stop is hit

    def __post_init__(self):
        if self.mode not in ("equal", "fixed", "risk_pct"):
            raise ValueError(f"unknown sizing mode '{self.mode}'")
        if self.mode != "equal" and (self.value is None or self.value <= 0):
            raise ValueError(f"sizing {self.mode} needs a positive value")


@dataclass
class Signals:
    entry: np.ndarray                       # bool (D, N): candidate to buy at the next open
    exit: np.ndarray | None = None          # bool (D, N): sell at the next open
    score: np.ndarray | None = None         # float (D, N): higher is better
    stop: np.ndarray | None = None          # float (D, N): stop distance as a fraction of the decision close
    target: np.ndarray | None = None        # float (D, N): target distance as a fraction
    trail: np.ndarray | None = None         # float (D, N): trailing distance from the highest close since entry
    max_hold_days: int | None = None
    sizing: Sizing = field(default_factory=Sizing)
    max_positions: int | None = None        # strategy's own limit (the account's limit wins)


@dataclass
class SimConfig:
    account: Account = field(default_factory=Account)
    costs: CostModel = field(default_factory=CostModel)
    seed: int = 0
    buy_order_ttl_days: int = 1                 # unfilled buys are cancelled after this many days
    # unfilled sells (not traded, locked at the lower circuit) retry every day until filled


# --------------------------------------------------------------------------- state

@dataclass
class Position:
    j: int
    shares: int
    entry_t: int
    signal_t: int
    entry_price: float          # fill price including slippage
    buy_charges: Charges
    stop: float | None = None
    target: float | None = None
    trail_frac: float | None = None
    trail_ref: float | None = None   # highest raw close since entry (active from the next day)
    last_close: float = float("nan")
    exit_reason: str | None = None   # set when a sell is pending
    exit_signal_t: int | None = None


@dataclass
class BuyOrder:
    j: int
    value: float
    signal_t: int
    limit_reason: str            # which limit set `value`: sizing | cap | liquidity | no cash
    stop_frac: float | None
    target_frac: float | None
    trail_frac: float | None
    age: int = 0


@dataclass
class Result:
    daily: pd.DataFrame
    trades: pd.DataFrame
    rejected: pd.DataFrame
    unfilled: pd.DataFrame
    open_positions: pd.DataFrame
    config: dict


# --------------------------------------------------------------------------- simulator

class Simulator:
    def __init__(self, market: Market, signals: Signals, cfg: SimConfig = SimConfig()):
        self.m, self.s, self.cfg = market, signals, cfg
        D, N = market.close.shape
        for name in ("entry", "exit", "score", "stop", "target", "trail"):
            a = getattr(signals, name)
            if a is not None and a.shape != (D, N):
                raise ValueError(f"signals.{name} has shape {a.shape}, market is {(D, N)}")
        if signals.sizing.mode == "risk_pct" and signals.stop is None:
            raise ValueError("risk_pct sizing needs a stop")
        acc = cfg.account
        self.max_pos = min(acc.max_positions, signals.max_positions or acc.max_positions)
        self.warnings = []
        if signals.max_positions and signals.max_positions > acc.max_positions:
            self.warnings.append(f"strategy asks for {signals.max_positions} positions; "
                                 f"account limit is {acc.max_positions}")
        self.rng = np.random.default_rng(cfg.seed)
        self.cash = float(acc.capital)
        self.positions: dict[int, Position] = {}
        self.buys: list[BuyOrder] = []
        self.dp = DPTracker()
        self.trades, self.rejected, self.unfilled, self.daily = [], [], [], []
        self.costs_today = 0.0

    # ----------------------------------------------------------------- helpers
    def _date(self, t):
        return self.m.dates[t]

    def _id(self, j):
        return self.m.ids[j]

    def holdings_value(self) -> float:
        return sum(p.shares * p.last_close for p in self.positions.values())

    def equity(self) -> float:
        return self.cash + self.holdings_value()

    def _reject(self, t, j, reason, **kw):
        self.rejected.append({"date": self._date(t), "SecurityID": self._id(j), "reason": reason, **kw})

    # ----------------------------------------------------------------- selling
    def _sell(self, t, pos: Position, price_raw: float, reason: str, at: str):
        c = self.cfg.costs
        px = c.fill_price("sell", price_raw)
        value = pos.shares * px
        ch = c.order("sell", value, dp=self.dp.due(t, pos.j))
        self.cash += value - ch.total
        self.costs_today += ch.total
        cost = pos.shares * pos.entry_price
        net = value - ch.total - cost - pos.buy_charges.total
        self.trades.append({
            "SecurityID": self._id(pos.j), "signal_date": self._date(pos.signal_t),
            "entry_date": self._date(pos.entry_t), "entry_price": pos.entry_price, "shares": pos.shares,
            "exit_date": self._date(t), "exit_price": px, "exit_at": at, "exit_reason": reason,
            "days_held": t - pos.entry_t, "gross_pnl": value - cost,
            "costs": ch.total + pos.buy_charges.total, "net_pnl": net,
            "return": net / (cost + pos.buy_charges.total),
        })
        del self.positions[pos.j]

    def _fill_sells(self, t):
        for pos in [p for p in self.positions.values() if p.exit_reason]:
            o = self.m.open[t, pos.j]
            if not np.isfinite(o):
                self._log_unfilled(t, pos.j, "sell", "not traded")
                continue
            if self.m.locked(t, pos.j) == "down":
                self._log_unfilled(t, pos.j, "sell", "locked")
                continue
            self._sell(t, pos, o, pos.exit_reason, "open")

    def _log_unfilled(self, t, j, side, why):
        self.unfilled.append({"date": self._date(t), "SecurityID": self._id(j), "side": side, "why": why})

    # ----------------------------------------------------------------- buying
    def _fill_buys(self, t):
        c = self.cfg.costs
        keep = []
        for o in self.buys:
            j = o.j
            op = self.m.open[t, j]
            why = None
            if not np.isfinite(op):
                why = "not traded"
            elif self.m.locked(t, j) == "up":
                why = "locked"
            if why:
                o.age += 1
                self._log_unfilled(t, j, "buy", why)
                if o.age >= self.cfg.buy_order_ttl_days:
                    self._reject(t, j, "expired", detail=why)
                else:
                    keep.append(o)
                continue
            px = c.fill_price("buy", op)
            per_share = px * (1 + (c.stt_pct + c.transaction_pct * (1 + c.gst_pct / 100)
                                   + c.stamp_pct_buy) / 100 + c.sebi_per_crore / 1e7 * (1 + c.gst_pct / 100))
            want = math.floor(o.value / px)
            afford = math.floor(max(self.cash - c.brokerage * (1 + c.gst_pct / 100), 0) / per_share)
            shares = min(want, afford)
            while shares > 0 and shares * px + c.order("buy", shares * px).total > self.cash + 1e-9:
                shares -= 1
            if shares <= 0:
                # want == 0: the open gapped above the order value; otherwise cash ran short
                self._reject(t, j, "gap" if want <= 0 else "no cash", detail="at fill", open=op, value=o.value)
                continue
            ch = c.order("buy", shares * px)
            self.cash -= shares * px + ch.total
            self.costs_today += ch.total
            pos = Position(j=j, shares=shares, entry_t=t, signal_t=o.signal_t, entry_price=px, buy_charges=ch,
                           stop=px * (1 - o.stop_frac) if o.stop_frac is not None else None,
                           target=px * (1 + o.target_frac) if o.target_frac is not None else None,
                           trail_frac=o.trail_frac, last_close=op)
            self.positions[j] = pos
        self.buys = keep

    # ----------------------------------------------------------------- stops and targets
    def _active_stop(self, p: Position) -> tuple[float | None, bool]:
        """(stop price, True if the trailing stop is the binding one)."""
        s, trailing = p.stop, False
        if p.trail_frac is not None and p.trail_ref is not None:
            ts = p.trail_ref * (1 - p.trail_frac)
            if s is None or ts > s:
                s, trailing = ts, True
        return s, trailing

    def _check_stops(self, t):
        for p in list(self.positions.values()):
            if p.exit_reason:
                continue
            o, h, l = self.m.open[t, p.j], self.m.high[t, p.j], self.m.low[t, p.j]
            if not np.isfinite(o):
                continue
            (stop, trailing), tgt = self._active_stop(p), p.target
            name = "trailing stop" if trailing else "stop"
            fill = None
            if stop is not None and o <= stop:
                fill = (o, name, "open (gap)")
            elif tgt is not None and o >= tgt:
                fill = (o, "target", "open (gap)")
            elif stop is not None and l <= stop:
                fill = (stop, name, "stop")
            elif tgt is not None and h >= tgt:
                fill = (tgt, "target", "target")
            if fill is None:
                continue
            if self.m.locked(t, p.j) == "down":
                # locked at the lower circuit: cannot sell today, sell at the next possible open
                p.exit_reason, p.exit_signal_t = fill[1], t
                self._log_unfilled(t, p.j, "sell", "locked")
                continue
            self._sell(t, p, fill[0], fill[1], fill[2])

    # ----------------------------------------------------------------- close
    def _mark(self, t):
        for p in self.positions.values():
            c = self.m.close[t, p.j]
            if np.isfinite(c):
                p.last_close = c
                if p.trail_frac is not None:
                    p.trail_ref = c if p.trail_ref is None else max(p.trail_ref, c)

    def _decide_exits(self, t):
        ex, mh = self.s.exit, self.s.max_hold_days
        for p in self.positions.values():
            if p.exit_reason:
                continue
            if ex is not None and ex[t, p.j]:
                p.exit_reason, p.exit_signal_t = "exit rule", t
            elif mh is not None and t - p.entry_t + 1 >= mh:
                p.exit_reason, p.exit_signal_t = "max hold", t

    def _size(self, t, j, equity) -> float:
        s, acc = self.s.sizing, self.cfg.account
        if s.mode == "equal":
            return equity / self.max_pos
        if s.mode == "fixed":
            return float(s.value)
        frac = self.s.stop[t, j]
        if not np.isfinite(frac) or frac <= 0:
            return float("nan")
        return equity * s.value / 100 / frac

    def _decide_entries(self, t, equity):
        ent = self.s.entry[t]
        held = set(self.positions) | {o.j for o in self.buys}
        cand = np.flatnonzero(ent & np.isfinite(self.m.close[t]))
        cand = np.array([j for j in cand if j not in held], dtype=int)
        if not len(cand):
            return
        score = self.s.score[t, cand] if self.s.score is not None else np.zeros(len(cand))
        score = np.where(np.isfinite(score), score, -np.inf)
        order = cand[np.lexsort((self.rng.random(len(cand)), -score))]
        staying = sum(1 for p in self.positions.values() if not p.exit_reason)
        slots = self.max_pos - staying - len(self.buys)
        acc, c = self.cfg.account, self.cfg.costs
        proceeds = 0.0
        for p in self.positions.values():
            if p.exit_reason:
                v = p.shares * c.fill_price("sell", p.last_close)
                proceeds += v - c.order("sell", v, dp=True).total
        budget = self.cash + proceeds - sum(o.value for o in self.buys)
        for j in order:
            if slots <= 0:
                self._reject(t, j, "no slot")
                continue
            px = c.fill_price("buy", self.m.close[t, j])
            value, reason = self._size(t, j, equity), "sizing"
            if not np.isfinite(value) or value <= 0:
                self._reject(t, j, "no stop")
                continue
            cap = equity * acc.max_position_pct / 100
            if cap < value:
                value, reason = cap, "cap"
            if value < px:
                self._reject(t, j, "cap", price=self.m.close[t, j], limit=value)
                continue
            liq = self.m.adv[t, j] * acc.max_participation_pct / 100 if np.isfinite(self.m.adv[t, j]) else 0.0
            if liq < value:
                value, reason = liq, "liquidity"
            if value < px:
                self._reject(t, j, "liquidity", price=self.m.close[t, j], limit=value)
                continue
            if budget < value:
                value, reason = budget, "no cash"
            if value < px:
                self._reject(t, j, "no cash", price=self.m.close[t, j], limit=value)
                continue
            fr = lambda a: (float(a[t, j]) if a is not None and np.isfinite(a[t, j]) else None)
            self.buys.append(BuyOrder(j=j, value=value, signal_t=t, limit_reason=reason,
                                      stop_frac=fr(self.s.stop), target_frac=fr(self.s.target),
                                      trail_frac=fr(self.s.trail)))
            budget -= value
            slots -= 1

    # ----------------------------------------------------------------- hooks (M2)
    def _corporate_actions(self, t):
        pass

    # ----------------------------------------------------------------- run
    def run(self) -> Result:
        D = len(self.m.dates)
        for t in range(D):
            self.costs_today = 0.0
            self._corporate_actions(t)
            self._fill_sells(t)
            self._fill_buys(t)
            self._check_stops(t)
            self._mark(t)
            eq = self.equity()
            if t < D - 1:
                self._decide_exits(t)
                self._decide_entries(t, eq)
            self.daily.append({"date": self._date(t), "equity": eq, "cash": self.cash,
                               "holdings": eq - self.cash, "positions": len(self.positions),
                               "costs": self.costs_today})
        return self._result()

    def _result(self) -> Result:
        daily = pd.DataFrame(self.daily).set_index("date")
        trades = pd.DataFrame(self.trades)
        op = pd.DataFrame([{"SecurityID": self._id(p.j), "entry_date": self._date(p.entry_t),
                            "entry_price": p.entry_price, "shares": p.shares, "last_close": p.last_close,
                            "stop": self._active_stop(p)[0], "target": p.target, "pending_exit": p.exit_reason}
                           for p in self.positions.values()])
        return Result(daily, trades, pd.DataFrame(self.rejected), pd.DataFrame(self.unfilled), op,
                      {"max_positions": self.max_pos, "warnings": self.warnings})


def simulate(market: Market, signals: Signals, cfg: SimConfig = SimConfig()) -> Result:
    return Simulator(market, signals, cfg).run()
