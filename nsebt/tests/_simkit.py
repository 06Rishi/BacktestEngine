"""Shared helpers for engine tests: hand-built markets, signals and configs."""
import numpy as np
import pandas as pd

from nsebt.backtest.account import Account
from nsebt.backtest.costs import CostModel
from nsebt.backtest.sim import Market, Signals, SimConfig

FREE = CostModel(stt_pct=0, transaction_pct=0, sebi_per_crore=0, gst_pct=0, stamp_pct_buy=0,
                 dp_per_scrip_sell_day=0, slippage_bps_buy=0, slippage_bps_sell=0)
BIG = 1e12


def market(bars: dict, turnover=None, days=None):
    """bars: {id: [(o, h, l, c) or c or None, ...]}. A bare number c is the bar
    (c, 1.01c, 0.99c, c): a normal day, not a locked circuit."""
    n = len(next(iter(bars.values())))
    idx = pd.bdate_range("2024-01-01", periods=n) if days is None else days
    f = {k: {} for k in ("o", "h", "l", "c")}
    for sid, rows in bars.items():
        for key in f:
            f[key][sid] = []
        for r in rows:
            if r is None:
                r = (np.nan,) * 4
            elif not isinstance(r, tuple):
                r = (r, r * 1.01, r * 0.99, r)
            for key, v in zip(("o", "h", "l", "c"), r):
                f[key][sid].append(v)
    fr = {k: pd.DataFrame(v, index=idx) for k, v in f.items()}
    tv = pd.DataFrame(BIG, index=idx, columns=fr["c"].columns) if turnover is None else turnover
    return Market.from_frames(fr["o"], fr["h"], fr["l"], fr["c"], tv, adv_days=1)


def sig(m, entries=(), exits=(), score=None, **kw):
    D, N = m.close.shape
    e, x = np.zeros((D, N), bool), np.zeros((D, N), bool)
    for t, j in entries:
        e[t, j] = True
    for t, j in exits:
        x[t, j] = True
    return Signals(entry=e, exit=x, score=score, **kw)


def cfg(capital=30_000, n=3, cap=33.0, part=100.0, costs=FREE, **kw):
    return SimConfig(account=Account(capital=capital, max_positions=n, max_position_pct=cap,
                                     max_participation_pct=part), costs=costs, **kw)


