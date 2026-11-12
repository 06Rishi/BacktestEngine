"""M1.4: stops, targets, trailing stop, gap rules and direction-aware locked bars (A6)."""
import numpy as np
import pytest

from _simkit import cfg, market, sig


def frac(m, j, v):
    a = np.full(m.close.shape, np.nan)
    a[:, j] = v
    return a


def one(m, **kw):
    from nsebt.backtest.sim import simulate
    return simulate(m, sig(m, entries=[(0, 0)], **kw), cfg())


def test_stop_hit_intraday_fills_at_stop():
    # entry 100, stop 10% -> 90; day 2 low 88
    m = market({"A": [100, (100, 101, 99, 100), (97, 98, 88, 95), 95]})
    t = one(m, stop=frac(m, 0, 0.10)).trades.iloc[0]
    assert t["exit_price"] == pytest.approx(90) and t["exit_reason"] == "stop" and t["exit_at"] == "stop"


def test_gap_down_through_stop_fills_at_open():
    m = market({"A": [100, (100, 101, 99, 100), (80, 82, 78, 81), 81]})
    t = one(m, stop=frac(m, 0, 0.10)).trades.iloc[0]
    assert t["exit_price"] == 80 and t["exit_at"] == "open (gap)"


def test_target_hit_intraday_and_gap_through_target():
    m = market({"A": [100, (100, 101, 99, 100), (105, 121, 104, 110), 110]})
    t = one(m, target=frac(m, 0, 0.20)).trades.iloc[0]
    assert t["exit_price"] == pytest.approx(120) and t["exit_reason"] == "target"
    m2 = market({"A": [100, (100, 101, 99, 100), (130, 131, 125, 128), 128]})
    t2 = one(m2, target=frac(m2, 0, 0.20)).trades.iloc[0]
    assert t2["exit_price"] == 130 and t2["exit_at"] == "open (gap)"


def test_stop_first_when_both_touched():
    m = market({"A": [100, (100, 101, 99, 100), (100, 125, 85, 100), 100]})
    t = one(m, stop=frac(m, 0, 0.10), target=frac(m, 0, 0.20)).trades.iloc[0]
    assert t["exit_reason"] == "stop" and t["exit_price"] == pytest.approx(90)


def test_entry_day_counts_for_stops():
    m = market({"A": [100, (100, 101, 85, 95), 95]})
    t = one(m, stop=frac(m, 0, 0.10)).trades.iloc[0]
    assert t["entry_date"] == t["exit_date"] == m.dates[1] and t["exit_price"] == pytest.approx(90)


def test_trailing_stop_uses_highest_close_and_activates_next_day():
    # closes 100 (entry day), 120, then a low of 107: trail 10% of 120 = 108 -> filled at 108
    m = market({"A": [100, (100, 101, 99, 100), (110, 125, 109, 120), (115, 116, 107, 110), 110]})
    t = one(m, trail=frac(m, 0, 0.10)).trades.iloc[0]
    assert t["exit_reason"] == "trailing stop" and t["exit_price"] == pytest.approx(108)
    assert t["exit_date"] == m.dates[3]
    # day 2's intraday high (125) does not move the reference: only closes do
    m2 = market({"A": [100, (100, 101, 99, 100), (110, 140, 109, 110), (110, 111, 101, 105), 105]})
    r2 = one(m2, trail=frac(m2, 0, 0.10))
    assert r2.trades.empty or r2.trades.iloc[0]["exit_price"] < 126


def test_trailing_never_below_fixed_stop():
    m = market({"A": [100, (100, 101, 99, 100), (100, 101, 91, 95), 95]})
    r = one(m, stop=frac(m, 0, 0.05), trail=frac(m, 0, 0.20))
    t = r.trades.iloc[0]
    assert t["exit_reason"] == "stop" and t["exit_price"] == pytest.approx(95)


def test_locked_upper_circuit_blocks_buy_then_expires():
    m = market({"A": [100, (110, 110, 110, 110), 112, 112]})
    r = one(m)
    assert r.open_positions.empty
    assert r.unfilled.iloc[0]["why"] == "locked" and r.rejected.iloc[0]["reason"] == "expired"


def test_locked_lower_circuit_blocks_sell_and_retries():
    m = market({"A": [100, 100, (90, 90, 90, 90), (81, 81, 81, 81), (80, 84, 79, 83), 83]})
    from nsebt.backtest.sim import simulate
    r = simulate(m, sig(m, entries=[(0, 0)], exits=[(1, 0)]), cfg())
    t = r.trades.iloc[0]
    assert t["exit_date"] == m.dates[4] and t["exit_price"] == 80
    assert (r.unfilled["why"] == "locked").sum() == 2
    # marked at the raw close while stuck
    assert r.daily["holdings"].iloc[3] == pytest.approx(t["shares"] * 81)


def test_lower_circuit_does_not_block_buys_and_upper_does_not_block_sells():
    m = market({"A": [100, (90, 90, 90, 90), 92]})
    assert len(one(m).open_positions) == 1
    m2 = market({"A": [100, 100, (110, 110, 110, 110), 110]})
    from nsebt.backtest.sim import simulate
    r2 = simulate(m2, sig(m2, entries=[(0, 0)], exits=[(1, 0)]), cfg())
    assert r2.trades.iloc[0]["exit_price"] == 110


def test_stop_on_locked_lower_day_sells_at_next_open():
    m = market({"A": [100, (100, 101, 99, 100), (85, 85, 85, 85), (84, 86, 83, 85), 85]})
    t = one(m, stop=frac(m, 0, 0.10)).trades.iloc[0]
    assert t["exit_date"] == m.dates[3] and t["exit_price"] == 84 and t["exit_reason"] == "stop"


def test_stops_skip_days_without_a_trade_and_gap_on_resume():
    m = market({"A": [100, (100, 101, 99, 100), None, None, (70, 72, 69, 71), 71]})
    t = one(m, stop=frac(m, 0, 0.10)).trades.iloc[0]
    assert t["exit_date"] == m.dates[4] and t["exit_price"] == 70
