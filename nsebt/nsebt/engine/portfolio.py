"""Portfolio backtest: rank stocks, hold the top N, rebalance monthly.

Timing (no look-ahead):
  scores use data up to the close of the last trading day of the month
  -> trades happen at the next day's open
  daily return for day t = open(t) -> open(t+1), earned by weights held at open(t)
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..panel import matrix
from ..universe import Universe


@dataclass
class PortfolioConfig:
    top_n: int = 20
    cost_per_side: float = 0.0015   # STT 0.1% + exchange/stamp/GST + slippage
    ca_filter: float = 0.35         # daily open-to-open moves bigger than this treated as corporate actions
    split: str = "2022-01-01"


def rebalance_days(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Last trading day of each month in the data."""
    s = pd.Series(index, index=index)
    last = s.groupby([index.year, index.month]).max()
    return pd.DatetimeIndex(last.values)


def run(panel: dict[str, pd.DataFrame], strategy, cfg: PortfolioConfig = PortfolioConfig(),
        universe: Universe | None = None) -> dict:
    """strategy.scores(close, volume) -> dates x tickers matrix, higher = better,
    NaN = not eligible. Must only use data up to each row's date."""
    O, C, V = matrix(panel, "Open"), matrix(panel, "Close"), matrix(panel, "Volume")
    tickers, dates = list(O.columns), O.index
    r = (O.shift(-1) / O - 1)
    ca_hits = int((r.abs() > cfg.ca_filter).sum().sum())
    r = r.where(r.abs() <= cfg.ca_filter)          # NaN = no usable return that day

    S = strategy.scores(C, V).reindex(index=dates, columns=tickers)
    members = None
    if universe is not None:
        members = universe.mask(dates, tickers)
        S = S.where(members)

    reb = set(rebalance_days(dates))
    R, Sv = r.to_numpy(), S.to_numpy()
    w = np.zeros(len(tickers))
    pending = None
    port, cost_s, turn_s, nhold = [], [], [], []
    for i, t in enumerate(dates[:-1]):
        cost = turn = 0.0
        if pending is not None:
            turn = float(np.abs(pending - w).sum())
            cost = turn * cfg.cost_per_side
            w, pending = pending, None
        rt = np.nan_to_num(R[i], nan=0.0)            # missing data -> flat for that day
        gross = float(w @ rt)
        port.append(gross - cost)
        cost_s.append(cost)
        turn_s.append(turn)
        nhold.append(int((w > 0).sum()))
        if w.sum() > 0:
            w = w * (1 + rt) / (1 + gross)
        if t in reb:
            sc = Sv[i]
            ok = np.where(~np.isnan(sc))[0]
            target = np.zeros(len(tickers))
            if len(ok):
                top = ok[np.argsort(-sc[ok], kind="stable")[: cfg.top_n]]
                target[top] = 1.0 / len(top)
            pending = target

    idx = dates[:-1]
    bench_r = r if members is None else r.where(members)
    res = pd.DataFrame({"port": port, "bench": bench_r.mean(axis=1).iloc[:-1].fillna(0).to_numpy(),
                        "cost": cost_s, "turnover": turn_s, "holdings": nhold}, index=idx)
    start = res.index[np.argmax(res["holdings"].to_numpy() > 0)] if (res["holdings"] > 0).any() else res.index[0]
    res = res.loc[start:]
    return {"daily": res, "ca_hits": ca_hits}


def stats(daily: pd.DataFrame, split: str = "2022-01-01") -> pd.DataFrame:
    def s(x):
        yrs = len(x) / 252
        out = {}
        for col in ("port", "bench"):
            eq = (1 + x[col]).cumprod()
            out[f"{col}_CAGR"] = eq.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else np.nan
            out[f"{col}_vol"] = x[col].std() * np.sqrt(252)
            out[f"{col}_maxDD"] = (eq / eq.cummax() - 1).min()
        out["sharpe_rf0"] = x["port"].mean() / x["port"].std() * np.sqrt(252)
        out["excess_CAGR"] = out["port_CAGR"] - out["bench_CAGR"]
        out["annual_turnover"] = x["turnover"].sum() / yrs if yrs > 0 else np.nan
        out["annual_cost"] = x["cost"].sum() / yrs if yrs > 0 else np.nan
        out["avg_holdings"] = x["holdings"].mean()
        out["years"] = yrs
        return pd.Series(out)
    sp = pd.Timestamp(split)
    parts = {"all": daily, "in-sample": daily[daily.index < sp], "out-of-sample": daily[daily.index >= sp]}
    return pd.DataFrame({k: s(v) for k, v in parts.items() if len(v) > 20}).T
