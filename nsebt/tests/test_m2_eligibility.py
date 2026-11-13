"""M2.5: eligibility (A4), index removal (2.5), liquidity from past data only."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from _simkit import cfg, market, sig
from nsebt.backtest.eligibility import EligibilityConfig, eligible, rolling_adv
from nsebt.backtest.sim import Market, SimConfig, Simulator, Sizing, simulate

DAYS = pd.bdate_range("2024-01-01", periods=30)
CR = 1e7
SMALL = EligibilityConfig(adv_days=3)


def frame(v, cols=("A",)):
    return pd.DataFrame({c: v for c in cols}, index=DAYS[:len(v)] if isinstance(v, list) else DAYS, dtype=float)


def test_each_criterion_excludes():
    close = frame([100.0] * 6)
    tv = frame([2 * CR] * 6)
    base = eligible(close, tv, cfg=SMALL)["A"]
    assert list(base) == [False, False, True, True, True, True]            # needs 3 sessions of history
    # not traded on t
    c2 = close.copy(); c2.iloc[4] = np.nan
    assert not eligible(c2, tv, cfg=SMALL)["A"].iloc[4]
    # raw close below ₹10
    c3 = close.copy(); c3.iloc[4] = 9.99
    assert not eligible(c3, tv, cfg=SMALL)["A"].iloc[4]
    # average traded value below ₹1 crore
    t2 = tv.copy(); t2.iloc[2:5] = 0.5 * CR
    assert not eligible(close, t2, cfg=SMALL)["A"].iloc[4]
    # not a member on t
    mem = frame([True] * 6).astype(bool); mem.iloc[4] = False
    assert not eligible(close, tv, members=mem, cfg=SMALL)["A"].iloc[4]
    # warm-up not satisfied
    wu = frame([False] * 3 + [True] * 3).astype(bool)
    assert list(eligible(close, tv, warmup=wu, cfg=SMALL)["A"]) == [False] * 3 + [True] * 3


def test_eligibility_uses_no_future_data():
    close, tv = frame([100.0] * 8), frame([2 * CR] * 8)
    e1 = eligible(close, tv, cfg=SMALL)
    c2, t2 = close.copy(), tv.copy()
    c2.iloc[5:] = 1.0                       # every value after day 4 changes
    t2.iloc[5:] = 0.0
    e2 = eligible(c2, t2, cfg=SMALL)
    pd.testing.assert_frame_equal(e1.iloc[:5], e2.iloc[:5])


def test_adv_counts_no_trade_sessions_as_zero():
    tv = frame([3.0, 3.0, np.nan, 3.0, 3.0])
    adv = rolling_adv(tv, 3)["A"]
    assert np.isnan(adv.iloc[1])
    assert adv.iloc[2] == pytest.approx(2.0) and adv.iloc[3] == pytest.approx(2.0)
    assert adv.iloc[4] == pytest.approx(2.0)
    late = frame([np.nan, np.nan, 3.0, 3.0, 3.0])          # listed on day 2: 3 sessions by day 4
    assert rolling_adv(late, 3)["A"].isna().iloc[:4].all() and rolling_adv(late, 3)["A"].iloc[4] == 3.0


def test_fill_day_volume_does_not_change_results():
    # order decided at the day-2 close; the fill-day (day 3) turnover is changed by 1000x
    m_tv = pd.DataFrame({"A": [1e6] * 6}, index=DAYS[:6])
    a = market({"A": [100] * 6}, turnover=m_tv, days=DAYS[:6])
    m_tv2 = m_tv.copy(); m_tv2.iloc[3:] = 1e9
    b = market({"A": [100] * 6}, turnover=m_tv2, days=DAYS[:6])
    ra = simulate(a, sig(a, entries=[(2, 0)]), cfg(capital=1_000_000, cap=100.0, part=10.0))
    rb = simulate(b, sig(b, entries=[(2, 0)]), cfg(capital=1_000_000, cap=100.0, part=10.0))
    pd.testing.assert_frame_equal(ra.daily, rb.daily)
    assert ra.open_positions["shares"].iloc[0] == 1000         # ₹1,00,000 = 10% of ₹10 lakh ADV


# --------------------------------------------------------------------------- index removal

def removal_market():
    m = market({"A": [100] * 8})
    mem = np.ones(m.close.shape, bool)
    mem[4:, 0] = False                                         # removed from the index on day 4
    m.members = mem
    return m


def test_universe_exit_hold_keeps_the_position():
    m = removal_market()
    sim = Simulator(m, sig(m, entries=[(0, 0)], sizing=Sizing("fixed", 1000.0)), cfg())
    r = sim.run()
    assert r.trades.empty and 0 in sim.positions


def test_universe_exit_sell_exits_at_the_next_open():
    m = removal_market()
    c = cfg(on_universe_exit="sell")
    r = simulate(m, sig(m, entries=[(0, 0)], sizing=Sizing("fixed", 1000.0)), c)
    t = r.trades.iloc[0]
    assert t["exit_reason"] == "universe exit" and t["exit_date"] == m.dates[5]


def test_config_validation_and_repo_block():
    with pytest.raises(ValueError, match="on_universe_exit"):
        EligibilityConfig(on_universe_exit="drop")
    with pytest.raises(ValueError, match="unknown"):
        EligibilityConfig.from_config({"eligibility": {"min_adv_cr": 1}})
    raw = yaml.safe_load((Path(__file__).resolve().parents[2] / "config.yaml").read_text())
    assert EligibilityConfig.from_config(raw) == EligibilityConfig()
