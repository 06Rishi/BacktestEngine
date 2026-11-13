"""M3.2: tokenizer and parser: precedence, errors with file:line:col, no eval, shift sign rule."""
import pytest

from nsebt.strategy.expr import (Bin, BoolOp, Call, Cmp, ExprError, Name, Neg, Not, Num, Param, Where,
                                 parse, tokenize, walk)


def tree(s):
    return parse(s).tree


def test_arithmetic_precedence_and_associativity():
    t = tree("1 + 2 * 3 - 4 / 2")
    # ((1 + (2 * 3)) - (4 / 2))
    assert isinstance(t, Bin) and t.op == "-"
    assert t.a.op == "+" and t.a.b.op == "*" and t.b.op == "/"
    t = tree("10 - 3 - 2")                               # left-associative
    assert t.op == "-" and t.a.op == "-" and t.b.value == 2
    t = tree("-(close - 1) * 2")
    assert t.op == "*" and isinstance(t.a, Neg)


def test_boolean_precedence():
    t = tree("a > 1 or b > 2 and not c > 3")
    assert isinstance(t, BoolOp) and t.op == "or"
    assert isinstance(t.items[1], BoolOp) and t.items[1].op == "and"
    assert isinstance(t.items[1].items[1], Not) and isinstance(t.items[1].items[1].x, Cmp)
    t = tree("close > sma(200) and rsi(14) < $rsi_max")
    assert t.op == "and" and t.items[1].b == Param(t.items[1].b.pos, "rsi_max")


def test_calls_and_keywords():
    t = tree("macd_cross_up(12, 26, signal=9)")
    assert isinstance(t, Call) and len(t.args) == 2 and t.kwargs[0][0] == "signal"
    t = tree("volume > $vol_mult * sma(volume, 20)")
    assert isinstance(t.b, Bin) and isinstance(t.b.b, Call) and isinstance(t.b.b.args[0], Name)
    assert tree("prior_high(20)").args[0].value == 20


def test_positions_point_at_the_token():
    p = parse("close > sma(200)")
    cmp = p.tree
    assert cmp.pos == 6 and cmp.b.pos == 8


@pytest.mark.parametrize("src,col,msg", [
    ("close >", 8, "ends too early"),
    ("close > > 3", 9, "unexpected"),
    ("sma(20", 7, "expected ')'"),
    ("1 < 2 < 3", 7, "chained"),
    ("close @ 3", 7, "unexpected character"),
    ("f(a=1, 2)", 8, "positional argument after a keyword"),
    ("f(a=1, a=2)", 8, "given twice"),
    ("", 1, "empty expression"),
    ("(close", 7, "expected ')'"),
])
def test_errors_report_file_line_and_column(src, col, msg):
    import re
    with pytest.raises(ExprError, match=re.escape(msg)) as e:
        parse(src, Where("strategies/x.yaml", 12, 1))
    assert e.value.line == 12 and e.value.col == col
    assert str(e.value).startswith(f"strategies/x.yaml:12:{col}: ")


def test_column_offset_from_yaml_position():
    with pytest.raises(ExprError) as e:
        parse("close >> 1", Where("s.yaml", 7, 9))
    assert (e.value.line, e.value.col) == (7, 9 + 7)


@pytest.mark.parametrize("src", ["shift(close, 0)", "shift(close, -1)", "shift(close, 1.5)", "shift(close)"])
def test_shift_must_look_back(src):
    with pytest.raises(ExprError, match="shift"):
        parse(src)


def test_shift_accepts_a_positive_literal_or_param():
    assert tree("shift(close, 1)").args[1].value == 1
    assert isinstance(tree("shift(close, $lag)").args[1], Param)


def test_no_eval_python_is_not_an_expression():
    for src in ("__import__('os')", "close.__class__", "[1, 2]", "lambda: 1", "close; 1"):
        with pytest.raises(ExprError):
            parse(src)


def test_walk_visits_every_node():
    t = tree("a > shift(b, 1) and not c(x=d)")
    names = sorted(n.name for n in walk(t) if isinstance(n, (Name, Call)))
    assert names == ["a", "b", "c", "d", "shift"]


def test_tokenizer_numbers():
    toks = tokenize("1 2.5 .5 1e3 3.")
    assert [t.text for t in toks if t.kind == "num"] == ["1", "2.5", ".5", "1e3", "3."]
    assert tree("1e-2").value == 0.01 and isinstance(tree("2"), Num)
