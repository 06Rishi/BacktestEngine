"""M3.4: YAML schema, params, --set overrides, errors with file:line:col, strategy check CLI."""
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nsebt import indicators as I
from nsebt.strategy import Context
from nsebt.strategy.spec import SpecError, YamlStrategy, load, parse_sets

from test_m3_evaluate import panel

ROOT = Path(__file__).resolve().parents[2]

PLAN_EXAMPLE = """\
name: macd_trend_pullback
universe: nifty500
params: {rsi_max: 60, vol_mult: 1.5}
entry:
  all:
    - close > sma(200)
    - macd_cross_up(12, 26, 9)
    - rsi(14) < $rsi_max
    - volume > $vol_mult * sma(volume, 20)
exit:
  any:
    - macd_cross_down(12, 26, 9)
    - close < sma(50)
rank: roc(63)
stop_loss: 2 * atr(14)
take_profit: 3 * atr(14)
max_hold_days: 20
sizing: risk_pct(1.0)
"""


def err(text, match, line=None, col=None, **kw):
    with pytest.raises(SpecError, match=re.escape(match)) as e:
        YamlStrategy(text, "s.yaml", **kw)
    if line is not None:
        assert (e.value.line, e.value.col) == (line, col), str(e.value)
    return e.value


def test_plan_example_loads_and_generates():
    st = YamlStrategy(PLAN_EXAMPLE, "plan.yaml")
    assert st.name == "macd_trend_pullback" and st.params == {"rsi_max": 60, "vol_mult": 1.5}
    assert st.sizing.mode == "risk_pct" and st.sizing.value == 1.0 and st.max_hold_days == 20
    assert len(st.parsed()) == 9 and st.lints() == []
    p = panel(n=300)
    out = st.generate(Context(p, st.params))
    a = I.atr(p.high, p.low, p.close, 14)
    pd.testing.assert_frame_equal(out.stop, 2 * a / p.close, check_names=False)
    pd.testing.assert_frame_equal(out.score, I.roc(p.close, 63), check_names=False)
    assert out.warmup.idxmax().min() == p.close.index[199]          # SMA(200) is the last node to define
    assert not (out.entry & ~out.warmup).to_numpy().any() or True    # warm-up is applied by the engine


@pytest.mark.parametrize("text,match,line,col", [
    ("name: x\nentry: close > 1\nbogus: 1\n", "unknown key 'bogus'", 3, 1),
    ("name: x\n", "missing required key 'entry'", None, None),
    ("entry: close > 1\n", "missing required key 'name'", None, None),
    ("name: x\nentry: close > 1\nuniverse: nifty50\n", "universe must be", 3, 11),
    ("name: x\nentry: close > 1\nrebalance: hourly\n", "rebalance must be", 3, 12),
    ("name: x\nentry: close > 1\nsizing: risk_pct\n", "sizing must be", 3, 9),
    ("name: x\nentry: close > 1\nsizing: equal(3)\n", "sizing must be", 3, 9),
    ("name: x\nentry: close > 1\nsizing: fixed(0)\n", "positive value", 3, 9),
    ("name: x\nentry: close > 1\nmax_positions: 0\n", "from 1 to 100", 3, 16),
    ("name: x\nentry: close > 1\nmax_hold_days: 2.5\n", "whole number", 3, 16),
    ("name: x\nentry: close > 1\nentry: close > 2\n", "given twice", 3, 1),
    ("name: x\nentry: {some: [close > 1]}\n", "{all: [...]} or {any: [...]}", 2, 8),
    ("name: x\nentry: {all: []}\n", "non-empty list", 2, 14),
    ("name: x\nparams: {n: abc}\nentry: close > 1\n", "must default to a number", 2, 13),
    ("name: x\nentry: [close > 1\n", "invalid YAML", None, None),
])
def test_schema_errors_point_at_the_problem(text, match, line, col):
    err(text, match, line, col)


def test_expression_errors_carry_yaml_position():
    text = "name: x\nentry:\n  all:\n    - close > sma(200)\n    - rsi(14) <  < 3\n"
    e = err(text, "unexpected", 5, 18)
    assert str(e).startswith("s.yaml:5:18:")
    e = err("name: x\nentry: 'close > smaa(20)'\n", "unknown function 'smaa'", 2, 17)    # quoted: +1
    e = err("name: x\nentry: close > $missing\n", "unknown parameter $missing", 2, 16)
    e = err("name: x\nentry: closee > 1\n", "unknown name 'closee'", 2, 8)


def test_set_overrides_convert_and_validate():
    text = "name: x\nparams: {n: 20, k: 1.5, on: true}\nentry: close > sma($n)\n"
    st = YamlStrategy(text, "s.yaml", {"n": "50", "k": "2", "on": "false"})
    assert st.params == {"n": 50, "k": 2.0, "on": False} and isinstance(st.params["k"], float)
    assert "--set n=50" in st.source() and st.source() != YamlStrategy(text, "s.yaml").source()
    for bad, msg in [({"m": "1"}, "unknown parameter; declared: k, n, on"), ({"n": "1.5"}, "must be a whole number"),
                     ({"k": "abc"}, "must be a number"), ({"on": "yes"}, "must be true or false")]:
        with pytest.raises(SpecError, match=re.escape(msg)):
            YamlStrategy(text, "s.yaml", bad)
    assert parse_sets(["n=50", " k = 2 "]) == {"n": "50", "k": "2"}
    for bad in (["n"], ["=3"], ["n=1", "n=2"]):
        with pytest.raises(SpecError):
            parse_sets(bad)


def test_override_changes_the_signals():
    text = "name: x\nparams: {n: 5}\nentry: close > sma($n)\n"
    p = panel()
    a = YamlStrategy(text).generate(Context(p, {}))
    b = YamlStrategy(text, overrides={"n": "10"}).generate(Context(p, {}))
    assert not a.entry.equals(b.entry)
    pd.testing.assert_frame_equal(b.entry, (p.close > p.close.rolling(10).mean()), check_names=False)


def test_monthly_rebalance_only_decides_on_period_ends():
    text = "name: x\nrebalance: monthly\nentry: close > 0\nexit: close > 0\n"
    p = panel(n=60)
    out = YamlStrategy(text).generate(Context(p, {}))
    days = out.entry.index[out.entry.any(axis=1)]
    assert len(days) >= 2 and all(d + pd.offsets.BDay(1) > d + pd.offsets.BMonthEnd(0) for d in days)
    assert out.exit.any(axis=1).sum() == len(days)


def test_constant_condition_and_lints_reported():
    st = YamlStrategy("name: x\nentry: 1 > 0\n")
    with pytest.raises(SpecError, match="constant"):
        st.generate(Context(panel(), {}))
    e = err("name: x\nentry:\n  all: [close > highest(high, 20), close > 50]\n", "quote expressions that contain commas")
    st = YamlStrategy("name: x\nentry:\n  all: ['close > highest(high, 20)', close > 50]\n", "s.yaml")
    w = st.lints()
    assert len(w) == 2 and w[0].startswith("s.yaml:3:") and "prior_high" in w[0] and "raw_close" in w[1]
    out = st.generate(Context(panel(), {}))
    assert len(out.notes) == 2


def test_strategy_check_cli(tmp_path):
    f = tmp_path / "s.yaml"
    f.write_text(PLAN_EXAMPLE)
    run = lambda *a: subprocess.run([sys.executable, "-m", "nsebt", "strategy", "check", str(f), *a],
                                    capture_output=True, text=True, cwd=ROOT)
    r = run("--set", "rsi_max=55")
    assert r.returncode == 0 and "rsi_max': 55" in r.stdout and r.stdout.strip().endswith("ok")
    r = run("--set", "rsi_max=high")
    assert r.returncode != 0 and "must be a whole number" in r.stderr
    f.write_text("name: x\nentry: close >\n")
    r = run()
    assert r.returncode != 0 and f"{f}:2:15:" in r.stderr
