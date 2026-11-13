"""M3.3: evaluation through the feature cache, A1 warm-up, A2/A3 lints, no look-ahead."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nsebt import indicators as I
from nsebt.data import Panel
from nsebt.features import FeatureCache
from nsebt.strategy import Context
from nsebt.strategy.evaluate import Evaluator, lint, warmup
from nsebt.strategy.expr import ExprError, Where, parse

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "store"


def panel(n=80, ids=("A", "B", "C"), seed=1):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2020-01-01", periods=n)
    c = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.02, (n, len(ids))), axis=0)), index=days, columns=list(ids))
    v = pd.DataFrame(rng.uniform(1e5, 2e5, c.shape), index=days, columns=list(ids))
    f = {"close": c, "open": c.shift(1).fillna(c), "high": c * 1.02, "low": c * 0.98, "volume": v,
         "raw_close": c * 2, "raw_open": c, "raw_high": c, "raw_low": c, "turnover": v * c}
    mem = pd.DataFrame(True, index=days, columns=list(ids))
    mem.iloc[:, 2] = False
    return Panel(f, mem, pd.DataFrame({"SecurityID": list(ids)}), pd.DataFrame(columns=["Date", "Index", "Close"]))


def ev(expr, p=None, params=None, features=None):
    p = p or panel()
    e = Evaluator(Context(p, params or {}, features), params or {})
    return e.evaluate(parse(expr)), p


def test_arithmetic_and_comparison_match_hand_computation():
    v, p = ev("close > sma(3) and volume > 1.2 * sma(volume, 5)")
    c, vol = p.close, p.volume
    want = (c > c.rolling(3).mean()) & (vol > 1.2 * vol.rolling(5).mean())
    pd.testing.assert_frame_equal(v.v, want, check_names=False)
    v, _ = ev("(close - open) / open * 100", p)
    pd.testing.assert_frame_equal(v.v, (c - p.open) / p.open * 100, check_names=False)


def test_indicator_forms_positional_keyword_and_multi_output():
    p = panel()
    a, _ = ev("macd_line(12, 26, 9)", p)
    b, _ = ev("macd_line(fast=12, slow=26, signal=9)", p)
    pd.testing.assert_frame_equal(a.v, I.macd(p.close, 12, 26, 9).line)
    pd.testing.assert_frame_equal(a.v, b.v)
    u, _ = ev("bollinger_upper(20, 2)", p)
    pd.testing.assert_frame_equal(u.v, I.bollinger(p.close, 20, 2.0).upper)
    s, _ = ev("rsi(14)", p, params={})
    pd.testing.assert_frame_equal(s.v, I.rsi(p.close, 14))


def test_params_are_bound():
    v, p = ev("rsi($n) < $hi", params={"n": 10, "hi": 60})
    pd.testing.assert_frame_equal(v.v, (I.rsi(p.close, 10) < 60) & I.rsi(p.close, 10).notna(), check_names=False)


@pytest.mark.parametrize("expr,msg", [
    ("closee > 1", "unknown name 'closee'"),
    ("sma > 1", "did you mean a function call"),
    ("smaa(20)", "unknown function 'smaa'"),
    ("rsi($missing)", "unknown parameter $missing"),
    ("sma(20, 30)", "at most 1 parameter"),
    ("macd(12, 26, 9)", "several outputs"),
    ("atr(close, 14)", "can't take a series"),
    ("sma(n=2.5)", "whole number"),
    ("shift(close, $lag)", "whole number >= 1"),
    ("sma(window=20)", "no parameter 'window'"),
])
def test_errors_point_at_the_problem(expr, msg):
    import re
    with pytest.raises(ExprError, match=re.escape(msg)) as e:
        Evaluator(Context(panel(), {"lag": 0}), {"lag": 0}).evaluate(parse(expr, Where("s.yaml", 3, 5)))
    assert e.value.line == 3 and e.value.col >= 5


def test_warmup_starts_where_every_node_is_defined():
    p = panel()
    a, _ = ev("close > sma(10)", p)
    b, _ = ev("rsi(14) < 70", p)
    w = warmup([a.ok, b.ok], p.close.index, p.close.columns)
    first = w.idxmax()
    assert (first == p.close.index[14]).all()                      # rsi(14) needs 15 closes
    w2 = warmup([a.ok, b.ok], p.close.index, p.close.columns, buffer=3)
    assert (w2.idxmax() == p.close.index[17]).all()


def test_cross_shift_prior_high_rank():
    p = panel()
    v, _ = ev("cross_up(close, sma(5))", p)
    s = p.close.rolling(5).mean()
    pd.testing.assert_frame_equal(v.v, I.cross_up(p.close, s) & s.notna() & s.shift(1).notna(), check_names=False)
    ph, _ = ev("prior_high(20)", p)
    pd.testing.assert_frame_equal(ph.v, p.high.rolling(20).max().shift(1), check_names=False)
    sh, _ = ev("shift(close, 2)", p)
    pd.testing.assert_frame_equal(sh.v, p.close.shift(2))
    r, _ = ev("rank(close)", p)
    assert r.v["C"].isna().all()                                   # not a member: not ranked
    assert ((r.v[["A", "B"]] >= 1) & (r.v[["A", "B"]] <= 2)).all().all()


def test_lints_a2_and_a3():
    w = lint(parse("close > highest(high, 20)", Where("s.yaml", 4, 3)))
    assert len(w) == 1 and "can never be true" in w[0] and w[0].startswith("s.yaml:4:9:")
    assert lint(parse("close > highest(20)"))                       # default input is high
    assert lint(parse("lowest(20) > low"))                          # same thing written the other way
    assert lint(parse("close > highest(close, 50)"))
    assert not lint(parse("close > prior_high(20)"))
    assert not lint(parse("close > shift(highest(high, 20), 1)"))
    assert not lint(parse("close >= highest(close, 50)"))           # equality can be true
    a3 = lint(parse("close > 100"))
    assert len(a3) == 1 and "raw_close" in a3[0]
    assert not lint(parse("raw_close >= 10"))
    assert lint(parse("$floor < close"))


def test_evaluation_goes_through_the_feature_cache(tmp_path):
    p = panel()
    fc = FeatureCache(tmp_path, p)
    ev("rsi(14) < 70", p, features=fc)
    assert fc.last_mode == "full"
    ev("rsi(14) > 30", p, features=fc)
    assert fc.last_mode == "cached"


def test_no_look_ahead_in_the_rule_engine():
    exprs = ["close > sma(20) and macd_cross_up(12, 26, 9)", "cross_down(close, sma(10)) or rsi(14) > 70",
             "close > prior_high(20)", "rank(roc(10)) <= 2", "atr(14) / close", "shift(close, 3) < close"]
    full = panel(n=150)
    cut = panel(n=150)
    for k, v in cut.fields.items():
        cut.fields[k] = v.iloc[:100]
    cut.members = cut.members.iloc[:100]
    for e in exprs:
        a, _ = ev(e, full)
        b, _ = ev(e, cut)
        pd.testing.assert_frame_equal(b.v, a.v.iloc[:100], check_dtype=False, obj=e)


@pytest.mark.skipif(not (STORE / "prices").exists(), reason="store not built")
def test_a1_macd_trend_pullback_first_entry_is_in_2017():
    from nsebt.data import load_panel
    p = load_panel(STORE, ["INE002A01018", "INE009A01021", "INE467B01029"], end="2018-12-31")
    ctx = Context(p, {})
    e = Evaluator(ctx, {"rsi_max": 60, "vol_mult": 1.5})
    exprs = ["close > sma(200)", "macd_cross_up(12, 26, 9)", "rsi(14) < $rsi_max", "volume > $vol_mult * sma(volume, 20)",
             "macd_cross_down(12, 26, 9)", "close < sma(50)", "roc(63)", "2 * atr(14)", "3 * atr(14)"]
    w = warmup([e.evaluate(parse(x)).ok for x in exprs], p.close.index, p.close.columns)
    first = w.idxmax()
    assert (first.dt.year == 2017).all()                           # SMA(200) from 2016-08-29: mid-2017
    assert (first <= pd.Timestamp("2017-07-31")).all()
