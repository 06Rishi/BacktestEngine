import numpy as np
import pandas as pd
import pytest

from nsebt.engine import portfolio, trades
from nsebt.strategies import Breakout, Momentum
from nsebt.universe import Universe

COLS = ["Open", "High", "Low", "Close", "Volume"]


def frame(opens, closes=None, idx=None):
    n = len(opens)
    idx = idx if idx is not None else pd.bdate_range("2020-01-01", periods=n)
    o = np.asarray(opens, float)
    c = np.asarray(closes if closes is not None else opens, float)
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c), "Low": np.minimum(o, c),
                         "Close": c, "Volume": np.full(n, 1e6)}, index=idx)


class Scripted:
    """Entry on given bar indices, exit on given bar indices."""
    kind = "trades"

    def __init__(self, entries, exits=()):
        self.entries, self.exits = set(entries), set(exits)

    def signals(self, d):
        n = len(d)
        return pd.DataFrame({"entry": [i in self.entries for i in range(n)],
                             "exit": [i in self.exits for i in range(n)]}, index=d.index)


def test_trade_fill_timing_and_cost():
    opens = np.arange(100, 200, dtype=float)             # strictly rising opens
    d = frame(opens)
    cfg = trades.TradeConfig(cost=0.003, warmup=5, max_hold=15)
    T = trades.run({"A": d}, Scripted(entries=[10], exits=[13]), cfg)
    assert len(T) == 1
    t = T.iloc[0]
    # signal on bar 10 close -> buy bar 11 open; exit signal bar 13 close -> sell bar 14 open
    assert t.entry_date == d.index[11] and t.exit_date == d.index[14]
    assert t.ret == pytest.approx(opens[14] / opens[11] - 1 - 0.003)
    assert t.days == 3


def test_max_hold_and_open_trade_at_end_dropped():
    d = frame(np.linspace(100, 150, 80))
    cfg = trades.TradeConfig(warmup=5, max_hold=15)
    T = trades.run({"A": d}, Scripted(entries=[10, 70]), cfg)
    assert list(T["days"]) == [15]          # capped at 15; the bar-70 trade can't close -> dropped


def test_corporate_action_window_skipped():
    o = np.full(60, 100.0)
    o[30:] = 20.0                            # 1:5 split
    d = frame(o)
    T = trades.run({"A": d}, Scripted(entries=[25]), trades.TradeConfig(warmup=5))
    assert T.empty


def test_membership_blocks_entries():
    d = frame(np.linspace(100, 120, 60))
    uni = Universe(pd.DataFrame({"Date": [d.index[0], d.index[30]], "Bloomberg_ID": ["A", "B"]}))
    T = trades.run({"A": d, "B": d.copy()}, Scripted(entries=[10, 40]),
                   trades.TradeConfig(warmup=5), universe=uni)
    got = set(zip(T.ticker, T.entry_date))
    assert got == {("A", d.index[11]), ("B", d.index[41])}


def test_breakout_has_no_lookahead():
    rng = np.random.default_rng(1)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 400)))
    d = frame(c * (1 + rng.normal(0, 0.003, 400)), c)
    d["High"] = d[["Open", "Close"]].max(axis=1) * 1.01
    d["Low"] = d[["Open", "Close"]].min(axis=1) * 0.99
    d["Volume"] = rng.uniform(5e5, 2e6, 400)
    full = Breakout().signals(d)
    cut = 250
    trunc = Breakout().signals(d.iloc[:cut])
    pd.testing.assert_frame_equal(full.iloc[:cut], trunc)


def test_momentum_has_no_lookahead_and_ignores_splits():
    rng = np.random.default_rng(2)
    idx = pd.bdate_range("2018-01-01", periods=600)
    C = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, (600, 4)), axis=0)),
                     index=idx, columns=list("ABCD"))
    V = pd.DataFrame(1e6, index=idx, columns=C.columns)
    m = Momentum(min_traded_value=0)
    full = m.scores(C, V)
    pd.testing.assert_frame_equal(full.iloc[:400], m.scores(C.iloc[:400], V.iloc[:400]))
    C2 = C.copy()
    C2.iloc[450:, 0] /= 5                     # split on A
    s1, s2 = full.iloc[-1], m.scores(C2, V).iloc[-1]
    assert s2["A"] == pytest.approx(s1["A"], rel=0.05)


class Fixed:
    """Always prefers tickers in a fixed order."""
    kind = "portfolio"

    def __init__(self, order):
        self.order = order

    def scores(self, C, V):
        return pd.DataFrame({t: float(len(self.order) - i) for i, t in enumerate(self.order)},
                            index=C.index).reindex(columns=C.columns)


def test_portfolio_returns_costs_and_timing():
    idx = pd.bdate_range("2021-01-01", periods=120)
    up = frame(100 * 1.01 ** np.arange(120), idx=idx)      # +1%/day open-to-open
    flat = frame(np.full(120, 50.0), idx=idx)
    data = {"UP": up, "FLAT": flat}
    cfg = portfolio.PortfolioConfig(top_n=1, cost_per_side=0.001)
    res = portfolio.run(data, Fixed(["UP", "FLAT"]), cfg)["daily"]
    first_reb = portfolio.rebalance_days(idx)[0]
    # nothing is held on or before the first rebalance day; trading starts next open
    assert res.index[0] == idx[idx.get_loc(first_reb) + 1]
    assert res["port"].iloc[0] == pytest.approx(0.01 - 0.001)    # buy cost on first day
    assert res["port"].iloc[1:].round(10).eq(0.01).all()         # no further turnover
    assert res["turnover"].sum() == pytest.approx(1.0)


def test_portfolio_switch_costs_two_sides():
    idx = pd.bdate_range("2021-01-01", periods=80)
    data = {"A": frame(np.full(80, 10.0), idx=idx), "B": frame(np.full(80, 20.0), idx=idx)}

    class Flip:
        kind = "portfolio"

        def scores(self, C, V):
            s = pd.DataFrame(0.0, index=C.index, columns=C.columns)
            s.loc[s.index.month % 2 == 1, "A"] = 1.0
            s.loc[s.index.month % 2 == 0, "B"] = 1.0
            return s

    res = portfolio.run(data, Flip(), portfolio.PortfolioConfig(top_n=1, cost_per_side=0.001))["daily"]
    switch = res[res["turnover"] > 1.5]
    assert len(switch) >= 1
    assert switch["port"].iloc[0] == pytest.approx(-0.002)       # sell A + buy B
