"""M3 review: regression tests for the strategy-layer issues found by the independent review."""
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nsebt.features import FeatureCache
from nsebt.strategy import Context
from nsebt.strategy.evaluate import Evaluator, lint
from nsebt.strategy.expr import ExprError, parse
from nsebt.strategy.spec import SpecError, YamlStrategy

from test_m3_evaluate import panel

ROOT = Path(__file__).resolve().parents[2]


def gen(text, p=None, **over):
    st = YamlStrategy(text, "s.yaml", over or None)
    return st.generate(Context(p or panel(), st.params))


# --------------------------------------------------------------------------- `not`

@pytest.mark.parametrize("flag,expected", [("true", 0), ("false", 240)])
def test_not_on_a_bool_param(flag, expected):
    out = gen("name: x\nparams: {use: true}\nentry: close > 0 and not $use\n", use=flag)
    assert int(out.entry.to_numpy().sum()) == expected


def test_not_on_constants():
    e = Evaluator(Context(panel(), {}), {})
    assert e.evaluate(parse("not true")).v is False and e.evaluate(parse("not false")).v is True


def test_not_and_or_never_fire_on_missing_data():
    p = panel()
    p.fields["deliv_pct"] = p.close * 0 + 50
    p.fields["deliv_pct"].iloc[40:45, 0] = np.nan
    for expr in ("not deliv_pct > 30", "not deliv_pct < 30 or close > 1e9", "deliv_pct > 30 or not deliv_pct > 30"):
        out = gen(f"name: x\nentry: {expr}\nexit: {expr}\n", p)
        assert not out.entry.iloc[40:45, 0].any() and not out.exit.iloc[40:45, 0].any(), expr


# --------------------------------------------------------------------------- clean errors

def test_empty_yaml_values():
    st = YamlStrategy("name: x\ndescription:\nentry: close > 1\n")
    assert st.description == ""
    with pytest.raises(SpecError, match="empty value"):
        YamlStrategy("name: x\nentry: close > 1\nexit:\n")
    with pytest.raises(SpecError, match="must default to a number"):
        YamlStrategy("name: x\nparams: {n: }\nentry: close > 1\n")


@pytest.mark.parametrize("expr,msg", [("close > 1/0", "division by zero"), ("close > $a/$b", "division by zero"),
                                      ("-(close > 1) < 0", "use 'not'"), ("(close > 1) + 1 > 0", "on a condition")])
def test_bad_arithmetic_is_an_error_with_position(expr, msg):
    with pytest.raises(SpecError, match=re.escape(msg)) as e:
        gen(f"name: x\nparams: {{a: 1, b: 0}}\nentry: {expr}\n")
    assert e.value.line == 3


def test_constant_first_argument_is_a_parameter():
    e = Evaluator(Context(panel(), {"a": 10, "b": 20}), {"a": 10, "b": 20})
    a = e.evaluate(parse("sma(max($a, $b))")).v
    pd.testing.assert_frame_equal(a, e.evaluate(parse("sma(20)")).v)
    pd.testing.assert_frame_equal(e.evaluate(parse("sma(abs(-5))")).v, e.evaluate(parse("sma(5)")).v)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1_0", "0x10", ""])
def test_set_rejects_non_finite_and_odd_numbers(value):
    with pytest.raises(SpecError):
        YamlStrategy("name: x\nparams: {k: 1.5}\nentry: close > 0\n", overrides={"k": value})


def test_set_accepts_signed_and_exponent_numbers():
    st = YamlStrategy("name: x\nparams: {k: 1.5, n: 5}\nentry: close > 0\n", overrides={"k": "-2.5e1", "n": "+7"})
    assert st.params == {"k": -25.0, "n": 7}


# --------------------------------------------------------------------------- lints and notes

def test_lints_cover_multipliers_and_price_levels():
    assert lint(parse("close > 1.02 * highest(high, 20)"))
    assert lint(parse("sma(200) > 100")) and lint(parse("volume > 100000")) and lint(parse("close * 1 > 100"))
    assert lint(parse("close > 2 * $x")) and lint(parse("close > -5"))
    assert not lint(parse("close / sma(200) > 1.05"))
    assert not lint(parse("close > 0.98 * highest(high, 20)"))
    assert not lint(parse("rsi(14) < 70"))
    st = YamlStrategy("name: x\nentry: close > sma(5)\nstop_loss: 0.05\n", "s.yaml")
    assert any("constant price distance" in w for w in st.lints())


def test_non_positive_stop_distances_are_reported():
    out = gen("name: x\nentry: close > sma(5)\ntrailing_stop: -1 * atr(14)\n")
    assert any("trailing_stop is zero or negative" in n for n in out.notes)


def test_exit_lookback_does_not_delay_entries():
    p = panel(n=320)
    a = gen("name: x\nentry: close > sma(5)\n", p)
    b = gen("name: x\nentry: close > sma(5)\nexit: close < sma(300)\n", p)
    pd.testing.assert_frame_equal(a.warmup, b.warmup)


def test_multi_line_value_errors_point_at_its_start():
    text = "name: x\nentry: >\n  close > sma(20) and\n  closee > 1\n"
    with pytest.raises(SpecError, match="multi-line value") as e:
        YamlStrategy(text, "s.yaml")
    assert (e.value.line, e.value.col) == (2, 8)


def test_float_indicator_params_share_one_cache_entry(tmp_path):
    p = panel()
    fc = FeatureCache(tmp_path, p)
    e = Evaluator(Context(p, {"k": 2}, fc), {"k": 2})
    e.evaluate(parse("bollinger_upper(20, $k)"))
    e.evaluate(parse("bollinger_upper(20, 2.0)"))
    assert fc.last_mode == "cached" and len(list(tmp_path.glob("bollinger*.json"))) == 1


def test_check_cli_dry_runs_without_sample(tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("name: x\nparams: {lag: 0}\nentry: shift(close, $lag) < close\n")
    r = subprocess.run([sys.executable, "-m", "nsebt", "strategy", "check", str(f)], capture_output=True,
                       text=True, cwd=ROOT)
    assert r.returncode != 0 and "whole number >= 1" in r.stderr and f"{f}:3:" in r.stderr


@pytest.mark.parametrize("expr,msg", [("close and volume > 1", "joins conditions"), ("not close", "'not' needs a condition"),
                                      ("sma(true) > 1", "not true/false"), ("shift(close, true) > 1", "not true/false")])
def test_conditions_and_numbers_are_not_mixed(expr, msg):
    with pytest.raises(SpecError, match=re.escape(msg)):
        gen(f"name: x\nentry: {expr}\n")


def test_a_bare_number_is_not_a_condition():
    with pytest.raises(SpecError, match="must be true/false"):
        gen("name: x\nentry: rsi(14)\n")
