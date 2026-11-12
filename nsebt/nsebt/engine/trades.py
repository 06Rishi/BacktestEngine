"""Trade-level backtest: per-stock entry/exit signals, filled at the next day's open.

Timing (no look-ahead):
  signal on day i-1 close  -> buy at day i open
  exit signal on day j close -> sell at day j+1 open, or after max_hold days
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..panel import matrix
from ..universe import Universe


@dataclass
class TradeConfig:
    cost: float = 0.003          # round trip: STT 0.1%/side + charges + slippage
    max_hold: int = 15           # trading days, entry open -> exit open
    ca_filter: float = 0.35      # skip trades spanning a daily move this big (split/bonus)
    warmup: int = 60             # bars before the first allowed signal
    split: str = "2022-01-01"    # in-sample / out-of-sample boundary


def simulate(open_: np.ndarray, ret: np.ndarray, entry: np.ndarray, exit_: np.ndarray,
             allowed: np.ndarray, cfg: TradeConfig) -> list[tuple[int, int, float]]:
    """Returns (entry_index, exit_index, net_return) per trade."""
    out, n, i = [], len(open_), max(cfg.warmup, 1)
    while i < n - 1:
        if not (entry[i - 1] and allowed[i - 1]):
            i += 1
            continue
        e, j = i, i
        while j < min(e + cfg.max_hold - 1, n - 1) and not exit_[j]:
            j += 1
        x = j + 1
        if x >= n:                              # still open at end of data
            break
        if np.nanmax(np.abs(ret[e:x + 1])) > cfg.ca_filter:
            i = x + 1
            continue
        out.append((e, x, open_[x] / open_[e] - 1 - cfg.cost))
        i = x + 1
    return out


def _ew_open_to_open(panel, ca, members: pd.DataFrame | None) -> pd.Series:
    o = matrix(panel, "Open")
    r = o.shift(-1) / o - 1
    r = r.where(r.abs() < ca)
    if members is not None:
        r = r.where(members.reindex(index=r.index, columns=r.columns, fill_value=False))
    return r.mean(axis=1)


def run(panel: dict[str, pd.DataFrame], strategy, cfg: TradeConfig = TradeConfig(),
        universe: Universe | None = None) -> pd.DataFrame:
    """strategy.signals(df) must return a frame with boolean 'entry' and 'exit' columns,
    using only data up to each row's date."""
    members = None
    if universe is not None:
        idx = matrix(panel, "Close").index
        members = universe.mask(idx, panel.keys())
    ew = _ew_open_to_open(panel, cfg.ca_filter, members)
    rows = []
    for t, d in panel.items():
        if len(d) <= cfg.warmup + 2:
            continue
        sig = strategy.signals(d)
        ret = d["Close"].pct_change().to_numpy()
        allowed = (members[t].reindex(d.index, fill_value=False).to_numpy()
                   if members is not None else np.ones(len(d), bool))
        for e, x, r in simulate(d["Open"].to_numpy(), ret, sig["entry"].fillna(False).to_numpy(bool),
                                sig["exit"].fillna(False).to_numpy(bool), allowed, cfg):
            ed, xd = d.index[e], d.index[x]
            w = ew.loc[ed:xd].iloc[:-1]
            rows.append({"ticker": t, "entry_date": ed, "exit_date": xd, "days": x - e,
                         "ret": r, "bench": float((1 + w.fillna(0)).prod() - 1)})
    T = pd.DataFrame(rows, columns=["ticker", "entry_date", "exit_date", "days", "ret", "bench"])
    T["excess"] = T["ret"] - T["bench"]
    T["period"] = np.where(T["entry_date"] < pd.Timestamp(cfg.split), "in-sample", "out-of-sample")
    return T


def placebo(panel: dict[str, pd.DataFrame], trades: pd.DataFrame, cfg: TradeConfig = TradeConfig(),
            per_trade: int = 3, seed: int = 0) -> pd.DataFrame:
    """Random entries on the same stocks with the same hold lengths and filters."""
    rng = np.random.default_rng(seed)
    rows = []
    for t, sub in trades.groupby("ticker"):
        d = panel[t]
        op, ret = d["Open"].to_numpy(), d["Close"].pct_change().to_numpy()
        for days, period in zip(sub["days"], sub["period"]):
            for _ in range(per_trade):
                hi = len(d) - days - 1
                if hi <= cfg.warmup:
                    continue
                k = int(rng.integers(cfg.warmup, hi))
                if np.nanmax(np.abs(ret[k:k + days + 1])) > cfg.ca_filter:
                    continue
                rows.append({"ticker": t, "ret": op[k + days] / op[k] - 1 - cfg.cost,
                             "period": "in-sample" if d.index[k] < pd.Timestamp(cfg.split)
                             else "out-of-sample"})
    return pd.DataFrame(rows, columns=["ticker", "ret", "period"])


def summarize(trades: pd.DataFrame, placebo_trades: pd.DataFrame | None = None) -> pd.DataFrame:
    def s(x):
        return pd.Series({"trades": len(x), "win_rate": (x["ret"] > 0).mean(),
                          "avg_ret": x["ret"].mean(), "median_ret": x["ret"].median(),
                          "avg_excess": x["excess"].mean(), "avg_days": x["days"].mean()})
    out = pd.concat({"all": s(trades), **{p: s(g) for p, g in trades.groupby("period")}}, axis=1).T
    if placebo_trades is not None and len(placebo_trades):
        pa = {"all": placebo_trades["ret"].mean(),
              **placebo_trades.groupby("period")["ret"].mean().to_dict()}
        out["placebo_avg_ret"] = pd.Series(pa)
        out["edge_vs_placebo"] = out["avg_ret"] - out["placebo_avg_ret"]
    return out
