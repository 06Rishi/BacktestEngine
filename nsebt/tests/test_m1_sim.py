"""M1.3: simulator core on hand-built price series.

Next-open fills, sells before buys, rupee orders, whole shares, cap -> liquidity -> cash,
slot limit and the rejected-signal log.
"""
import numpy as np
import pandas as pd
import pytest

from nsebt.backtest.account import Account
from nsebt.backtest.costs import CostModel
from nsebt.backtest.sim import Market, Signals, SimConfig, Sizing, simulate

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


def test_fills_at_next_open_not_signal_close():
    m = market({"A": [100, (110, 112, 108, 111), 111, 111]})
    r = simulate(m, sig(m, entries=[(0, 0)]), cfg())
    assert r.open_positions.loc[0, "entry_date"] == m.dates[1]
    assert r.open_positions.loc[0, "entry_price"] == 110
    # equal sizing: 30000/3 = 10000, under the 33% cap (9900)? cap binds: 9900 -> 90 shares
    assert r.open_positions.loc[0, "shares"] == 90
    assert r.daily["cash"].iloc[1] == 30_000 - 90 * 110


def test_whole_shares_and_rupee_order_sized_at_fill():
    m = market({"A": [100, 333, 333]})
    r = simulate(m, sig(m, entries=[(0, 0)]), cfg(cap=100, n=1))
    # value 30000 decided on 100 close, shares computed at the 333 open: floor(30000/333) = 90
    assert r.open_positions.loc[0, "shares"] == 90


def test_open_gapping_above_order_value_rejects_as_gap():
    m = market({"A": [100, 20_000, 20_000]})
    r = simulate(m, sig(m, entries=[(0, 0)]), cfg(cap=33))
    assert r.open_positions.empty
    assert r.rejected["reason"].tolist() == ["gap"]


def test_share_price_above_cap_is_rejected_as_cap():
    m = market({"MRF": [130_000] * 3})
    r = simulate(m, sig(m, entries=[(0, 0)]), cfg())
    assert r.rejected.loc[0, "reason"] == "cap" and r.open_positions.empty


def test_slot_limit_ranks_and_logs_no_slot():
    m = market({k: [100] * 3 for k in "ABCDE"})
    score = np.tile([1.0, 5.0, 3.0, 4.0, 2.0], (3, 1))
    r = simulate(m, sig(m, entries=[(0, j) for j in range(5)], score=score), cfg())
    assert sorted(r.open_positions["SecurityID"]) == ["B", "C", "D"]
    rej = r.rejected[r.rejected["reason"] == "no slot"]
    assert sorted(rej["SecurityID"]) == ["A", "E"]


def test_cash_limit_shrinks_then_rejects():
    m = market({k: [100] * 3 for k in "ABC"})
    score = np.tile([3.0, 2.0, 1.0], (3, 1))
    s = sig(m, entries=[(0, 0), (0, 1), (0, 2)], score=score, sizing=Sizing("fixed", 20_000))
    r = simulate(m, s, cfg(cap=100))
    sh = r.open_positions.set_index("SecurityID")["shares"]
    assert sh["A"] == 200 and sh["B"] == 100          # B limited by remaining cash
    assert r.rejected.set_index("SecurityID").loc["C", "reason"] == "no cash"


def test_order_of_limits_cap_then_liquidity_then_cash():
    days = pd.bdate_range("2024-01-01", periods=3)
    tv = pd.DataFrame({"A": [100_000.0] * 3}, index=days)           # 5% participation -> ₹5,000
    m = market({"A": [100] * 3}, turnover=tv, days=days)
    r = simulate(m, sig(m, entries=[(0, 0)]), cfg(part=5))
    assert r.open_positions.loc[0, "shares"] == 50
    tv2 = pd.DataFrame({"A": [1_000.0] * 3}, index=days)            # 5% of 1,000 = ₹50 < one share
    m2 = market({"A": [100] * 3}, turnover=tv2, days=days)
    r2 = simulate(m2, sig(m2, entries=[(0, 0)]), cfg(part=5))
    assert r2.rejected.loc[0, "reason"] == "liquidity"


def test_liquidity_uses_only_past_traded_value():
    days = pd.bdate_range("2024-01-01", periods=4)
    tv = pd.DataFrame({"A": [100_000.0] * 4}, index=days)
    m = market({"A": [100] * 4}, turnover=tv, days=days)
    base = simulate(m, sig(m, entries=[(0, 0)]), cfg(part=5)).open_positions
    tv.iloc[1:] = 1e15                                               # fill day and later: huge volume
    m2 = market({"A": [100] * 4}, turnover=tv, days=days)
    assert simulate(m2, sig(m2, entries=[(0, 0)]), cfg(part=5)).open_positions.equals(base)


def test_sells_fill_before_buys_and_free_the_slot():
    m = market({"A": [100, 100, 120, 120], "B": [50, 50, 50, 50]})
    s = sig(m, entries=[(0, 0), (1, 1)], exits=[(1, 0)])
    r = simulate(m, s, cfg(n=1, cap=100))
    assert r.trades.loc[0, "SecurityID"] == "A" and r.trades.loc[0, "exit_price"] == 120
    pos = r.open_positions.iloc[0]
    assert pos["SecurityID"] == "B" and pos["entry_date"] == m.dates[2]
    # the order value was set at the decision close (equity ₹30,000), filled from A's proceeds
    assert pos["shares"] == 30_000 // 50
    assert r.daily["cash"].iloc[2] == pytest.approx(300 * 120 - 600 * 50)


def test_max_hold_days_exit():
    m = market({"A": list(range(100, 110))})
    r = simulate(m, sig(m, entries=[(0, 0)], max_hold_days=3), cfg())
    t = r.trades.iloc[0]
    assert t["entry_date"] == m.dates[1] and t["exit_date"] == m.dates[4] and t["exit_reason"] == "max hold"
    assert t["days_held"] == 3


def test_buy_order_expires_when_not_traded():
    m = market({"A": [100, None, 100, 100]})
    r = simulate(m, sig(m, entries=[(0, 0)]), cfg())
    assert r.open_positions.empty and r.rejected.loc[0, "reason"] == "expired"
    assert r.unfilled.loc[0, "why"] == "not traded"


def test_already_held_not_bought_again():
    m = market({"A": [100] * 5})
    r = simulate(m, sig(m, entries=[(t, 0) for t in range(4)]), cfg())
    assert len(r.open_positions) == 1 and r.rejected.empty


def test_strategy_positions_clamped_to_account():
    m = market({k: [100] * 3 for k in "ABCDE"})
    r = simulate(m, sig(m, entries=[(0, j) for j in range(5)], max_positions=5), cfg())
    assert len(r.open_positions) == 3 and r.config["warnings"]


def test_cash_reconciles_with_costs():
    m = market({"A": [100, 101, 105, 103, 108, 110], "B": [50, 49, 52, 55, 54, 56]})
    s = sig(m, entries=[(0, 0), (0, 1)], exits=[(3, 0), (3, 1)])
    r = simulate(m, s, cfg(costs=CostModel()))
    assert r.open_positions.empty and len(r.trades) == 2
    assert r.daily["equity"].iloc[-1] == pytest.approx(30_000 + r.trades["net_pnl"].sum())
    assert r.daily["costs"].sum() == pytest.approx(r.trades["costs"].sum())
    # one DP charge per stock per sell day
    assert (r.trades["costs"] > 15.34).all()


def test_same_seed_same_result_and_tie_break_is_seeded():
    m = market({k: [100] * 3 for k in "ABCDEF"})
    s = sig(m, entries=[(0, j) for j in range(6)])
    a = simulate(m, s, cfg(seed=1)).open_positions
    b = simulate(m, s, cfg(seed=1)).open_positions
    assert a.equals(b)
    picks = {tuple(sorted(simulate(m, s, cfg(seed=k)).open_positions["SecurityID"])) for k in range(10)}
    assert len(picks) > 1
