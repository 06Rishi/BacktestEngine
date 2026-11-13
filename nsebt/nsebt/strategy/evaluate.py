"""Evaluate parsed expressions on a research panel (M3.3).

Values are DataFrames (dates x securities) or scalars. Every evaluation also returns where the
value is defined; a strategy's warm-up (A1) starts at the first row where EVERY node of every
expression it uses is defined for that security, plus an optional `warmup_buffer`. This replaces
REGISTRY.lookback, which is the history a cache needs for exactness, not signal warm-up.

Names:
  adjusted fields  close open high low vwap volume deliv_qty   (signals; levels encode later splits)
  raw fields       raw_close raw_open raw_high raw_low raw_volume turnover deliv_pct
Functions:
  every registered indicator, e.g. sma(200), rsi(14), atr(14), macd_line(12, 26, 9), bollinger_upper(20, 2);
    a single-input indicator may take a series first: sma(volume, 20), highest(close, 50)
  cross_up(a, b), cross_down(a, b), macd_cross_up(f, s, sig), macd_cross_down(f, s, sig)
  shift(x, n) (n >= 1), prior_high(n) = shift(highest(high, n), 1), prior_low(n)
  rank(x): 1 = highest among point-in-time members that traded that day
  abs(x), min(a, b), max(a, b)
  rs_index(n): n-day return relative to the Nifty 500 index
  rs_sector(n): relative to the security's sector index by TODAY's industry tag (A5: look-ahead in
    classification; former members fall back to the Nifty 500; reported in the strategy notes)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .. import indicators as I
from .base import rank_desc
from .expr import Bin, Bool, BoolOp, Call, Cmp, ExprError, Name, Neg, Node, Not, Num, Param, Parsed, walk

ADJ_FIELDS = ("close", "open", "high", "low", "vwap", "volume", "deliv_qty")
ADJ_PRICES = ("close", "open", "high", "low", "vwap")
RAW_FIELDS = ("raw_close", "raw_open", "raw_high", "raw_low", "raw_volume", "turnover", "deliv_pct")
FIELDS = ADJ_FIELDS + RAW_FIELDS
SPECIAL = {"cross_up", "cross_down", "macd_cross_up", "macd_cross_down", "shift", "prior_high", "prior_low",
           "rank", "abs", "min", "max", "rs_index", "rs_sector"}


def _multi_outputs() -> dict[str, tuple[str, str]]:
    """'macd_line' -> ('macd', 'line'), ..."""
    return {f"{n}_{o}": (n, o) for n, s in I.REGISTRY.items() for o in s.outputs}


MULTI = _multi_outputs()


@dataclass
class Value:
    v: object                 # DataFrame or scalar
    ok: object = True         # DataFrame[bool] (defined) or True

    @property
    def frame(self) -> bool:
        return isinstance(self.v, pd.DataFrame)


def _is_cond(x) -> bool:
    v = x.v
    if isinstance(v, pd.DataFrame):
        return bool(len(v.columns)) and v.dtypes.apply(lambda d: d == bool).all()
    return isinstance(v, (bool, np.bool_))


def _and(a, b):
    if a is True:
        return b
    if b is True:
        return a
    return a & b


@dataclass
class Evaluator:
    ctx: object                               # strategy.base.Context
    params: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    # ------------------------------------------------------------------ public
    def evaluate(self, p: Parsed) -> Value:
        self._p = p
        return self._eval(p.tree)

    def number(self, p: Parsed) -> float:
        """A constant expression (e.g. a parameter value)."""
        self._p = p
        return self._const(p.tree, "a constant")

    # ------------------------------------------------------------------ helpers
    def _err(self, msg, node):
        return self._p.error(msg, node)

    @property
    def _panel(self):
        return self.ctx.panel

    def _field(self, name, node):
        p = self._panel
        try:
            f = p[name] if isinstance(p, dict) else getattr(p, name)
        except (AttributeError, KeyError):
            raise self._err(f"field '{name}' is not in the panel", node) from None
        return f

    def _const(self, node, what) -> float:
        if isinstance(node, Num):
            return node.value
        if isinstance(node, Bool):
            return bool(node.value)
        if isinstance(node, Param):
            if node.name not in self.params:
                raise self._err(f"unknown parameter ${node.name}; declared: {sorted(self.params)}", node)
            return self.params[node.name]
        if isinstance(node, Neg):
            return -self._const(node.x, what)
        if isinstance(node, Bin):
            a, b = self._const(node.a, what), self._const(node.b, what)
            if node.op == "/" and not b:
                raise self._err("division by zero in a constant expression", node)
            return {"+": a + b, "-": a - b, "*": a * b, "/": a / b if b else 0.0}[node.op]
        if isinstance(node, Call) and node.name in ("abs", "min", "max") and not node.kwargs:
            vals = [self._const(x, what) for x in node.args]
            if node.name == "abs" and len(vals) == 1:
                return abs(vals[0])
            if node.name in ("min", "max") and len(vals) == 2:
                return min(vals) if node.name == "min" else max(vals)
        raise self._err(f"expected {what} (a number or $param)", node)

    def _is_const(self, node) -> bool:
        try:
            self._const(node, "a constant")
            return True
        except ExprError:
            return False

    def _int(self, node, what, minimum=1) -> int:
        v = self._const(node, what)
        if isinstance(v, (bool, np.bool_)):
            raise self._err(f"{what} must be a number, not true/false", node)
        if not float(v).is_integer() or v < minimum:
            raise self._err(f"{what} must be a whole number >= {minimum}, got {v:g}", node)
        return int(v)

    # ------------------------------------------------------------------ evaluation
    def _eval(self, n: Node) -> Value:
        if isinstance(n, (Num, Bool)):
            return Value(n.value)
        if isinstance(n, Param):
            return Value(self._const(n, "a parameter"))
        if isinstance(n, Name):
            if n.name not in FIELDS:
                hint = " (did you mean a function call, e.g. sma(20)?)" if n.name in I.REGISTRY else ""
                raise self._err(f"unknown name '{n.name}'{hint}", n)
            f = self._field(n.name, n)
            return Value(f, f.notna())
        if isinstance(n, Neg):
            x = self._eval(n.x)
            if _is_cond(x):
                raise self._err("can't negate a condition with '-'; use 'not'", n)
            return Value(-x.v, x.ok)
        if isinstance(n, Bin):
            a, b = self._eval(n.a), self._eval(n.b)
            for side, node in ((a, n.a), (b, n.b)):
                if _is_cond(side):
                    raise self._err(f"arithmetic '{n.op}' on a condition (true/false); compare numbers instead", node)
            if n.op == "/" and not isinstance(b.v, pd.DataFrame) and not b.v:
                raise self._err("division by zero", n)
            if n.op == "/":
                v = a.v / b.v
                if isinstance(v, pd.DataFrame):
                    v = v.replace([np.inf, -np.inf], np.nan)
            else:
                v = {"+": lambda: a.v + b.v, "-": lambda: a.v - b.v, "*": lambda: a.v * b.v}[n.op]()
            ok = _and(a.ok, b.ok)
            if isinstance(v, pd.DataFrame):
                ok = _and(ok, v.notna())
            return Value(v, ok)
        if isinstance(n, Cmp):
            a, b = self._eval(n.a), self._eval(n.b)
            ops = {"<": lambda x, y: x < y, "<=": lambda x, y: x <= y, ">": lambda x, y: x > y,
                   ">=": lambda x, y: x >= y, "==": lambda x, y: x == y, "!=": lambda x, y: x != y}
            ok = _and(a.ok, b.ok)
            v = ops[n.op](a.v, b.v)
            if isinstance(v, pd.DataFrame):
                v = v & (ok if isinstance(ok, pd.DataFrame) else True)
            return Value(v, ok)
        if isinstance(n, BoolOp):
            vals = [self._eval(x) for x in n.items]
            for x, node in zip(vals, n.items):
                if not _is_cond(x):
                    raise self._err(f"'{n.op}' joins conditions; this is a number (compare it with something)", node)
            v, ok = self.truth(vals[0]), vals[0].ok
            for x in vals[1:]:
                v = (v & self.truth(x)) if n.op == "and" else (v | self.truth(x))
                ok = _and(ok, x.ok)
            return Value(v, ok)
        if isinstance(n, Not):
            # True only where the operand is defined and false: missing data never satisfies 'not'
            x = self._eval(n.x)
            if not _is_cond(x):
                raise self._err("'not' needs a condition; this is a number (compare it with something)", n.x)
            b = self._bool(x)
            if isinstance(b, pd.DataFrame):
                ok = x.ok if isinstance(x.ok, pd.DataFrame) else True
                return Value(~b & ok, x.ok)
            return Value(not b, x.ok)
        if isinstance(n, Call):
            return self._call(n)
        raise self._err("unsupported expression", n)        # pragma: no cover

    def truth(self, x: Value):
        """A value used as a condition: True only where it is defined and true."""
        b = self._bool(x)
        if isinstance(b, pd.DataFrame) and isinstance(x.ok, pd.DataFrame):
            return b & x.ok
        return b

    def _bool(self, x: Value):
        v = x.v
        if isinstance(v, pd.DataFrame):
            if v.dtypes.apply(lambda d: d == bool).all():
                return v
            return v.fillna(0).astype(bool)
        return bool(v)

    def _call(self, n: Call) -> Value:
        name = n.name
        if name in SPECIAL:
            return self._special(n)
        if name in MULTI:
            ind, part = MULTI[name]
            return self._indicator(n, ind, part)
        if name in I.REGISTRY:
            if I.REGISTRY[name].outputs:
                outs = ", ".join(f"{name}_{o}" for o in I.REGISTRY[name].outputs)
                raise self._err(f"'{name}' has several outputs; use one of {outs}", n)
            return self._indicator(n, name, None)
        raise self._err(f"unknown function '{name}'", n)

    def _indicator(self, n: Call, name: str, part: str | None) -> Value:
        spec = I.REGISTRY[name]
        names = list(spec.params)
        args = list(n.args)
        series = None
        if args and not self._is_const(args[0]):
            if len(spec.inputs) != 1:
                raise self._err(f"{name} reads {', '.join(spec.inputs)}; it can't take a series argument", args[0])
            series = self._eval(args[0])
            if not series.frame:
                raise self._err(f"{name}: the first argument must be a series", args[0])
            args = args[1:]
        if len(args) > len(names):
            raise self._err(f"{name} takes at most {len(names)} parameter(s): {', '.join(names)}", n)
        params = {}
        for k, a in zip(names, args):
            params[k] = self._const(a, f"{name} parameter '{k}'")
        for k, a in n.kwargs:
            if k not in names:
                raise self._err(f"{name} has no parameter '{k}'; parameters: {', '.join(names)}", a)
            if k in params:
                raise self._err(f"{name}: parameter '{k}' given twice", a)
            params[k] = self._const(a, f"{name} parameter '{k}'")
        for k, v in params.items():
            if isinstance(v, (bool, np.bool_)):
                raise self._err(f"{name} parameter '{k}' must be a number, not true/false", n)
            if isinstance(spec.params.get(k), float):
                params[k] = float(v)
            if isinstance(spec.params.get(k), int) and not isinstance(spec.params.get(k), bool):
                if not float(v).is_integer() or v < 1:
                    raise self._err(f"{name} parameter '{k}' must be a whole number >= 1", n)
                params[k] = int(v)
        if series is not None:
            res = spec.fn(series.v, **{**spec.params, **params})
        else:
            res = self.ctx.indicator(name, **params)
        v = getattr(res, part) if part else res
        v = v.astype(float) if v.dtypes.apply(lambda d: d == bool).any() else v
        return Value(v, v.notna())

    def _special(self, n: Call) -> Value:
        name, a = n.name, n.args
        need = {"cross_up": 2, "cross_down": 2, "macd_cross_up": 3, "macd_cross_down": 3, "shift": 2,
                "prior_high": 1, "prior_low": 1, "rank": 1, "abs": 1, "min": 2, "max": 2, "rs_index": 1,
                "rs_sector": 1}[name]
        if len(a) != need or n.kwargs:
            raise self._err(f"{name} takes {need} positional argument(s)", n)
        if name in ("cross_up", "cross_down"):
            x, y = self._eval(a[0]), self._eval(a[1])
            fn = I.cross_up if name == "cross_up" else I.cross_down
            v = fn(x.v, y.v)
            ok = _and(_and(x.ok, y.ok), _and(self._shift_ok(x.ok), self._shift_ok(y.ok)))
            return Value(v & (ok if isinstance(ok, pd.DataFrame) else True), ok)
        if name in ("macd_cross_up", "macd_cross_down"):
            p = dict(zip(("fast", "slow", "signal"), (self._int(x, f"{name} period") for x in a)))
            m = self.ctx.indicator("macd", **p)
            fn = I.cross_up if name == "macd_cross_up" else I.cross_down
            ok = m.line.notna() & m.signal.notna() & m.line.shift(1).notna() & m.signal.shift(1).notna()
            return Value(fn(m.line, m.signal) & ok, ok)
        if name == "shift":
            x = self._eval(a[0])
            k = self._int(a[1], "shift(x, n): n", 1)
            if not x.frame:
                return x
            ok = x.ok.shift(k).fillna(False).astype(bool) if isinstance(x.ok, pd.DataFrame) else True
            return Value(x.v.shift(k), ok)
        if name in ("prior_high", "prior_low"):
            k = self._int(a[0], f"{name}(n): n", 1)
            base = self.ctx.indicator("highest" if name == "prior_high" else "lowest", n=k)
            v = base.shift(1)
            return Value(v, v.notna())
        if name == "rank":
            x = self._eval(a[0])
            if not x.frame:
                raise self._err("rank() needs a series", a[0])
            mask = self.ctx.members & self._panel.close.notna()
            v = rank_desc(x.v, mask)
            return Value(v, x.ok)
        if name == "abs":
            x = self._eval(a[0])
            return Value(abs(x.v), x.ok)
        if name in ("min", "max"):
            x, y = self._eval(a[0]), self._eval(a[1])
            if x.frame or y.frame:
                xv = x.v if x.frame else (y.v * 0 + x.v)
                yv = y.v if y.frame else (x.v * 0 + y.v)
                v = np.fmin(xv, yv) if name == "min" else np.fmax(xv, yv)
            else:
                v = min(x.v, y.v) if name == "min" else max(x.v, y.v)
            return Value(v, _and(x.ok, y.ok))
        k = self._int(a[0], f"{name}(n): n", 1)
        close = self._panel.close
        if name == "rs_index":
            bench = self._panel.index("Nifty 500")
        else:
            bench = self._panel.sector_bench()
            note = "rs_sector() uses today's industry tag (look-ahead in classification); former members use the Nifty 500 (A5)"
            if note not in self.notes:
                self.notes.append(note)
        v = I.relative_strength(close, bench, k)
        return Value(v, v.notna())

    @staticmethod
    def _shift_ok(ok):
        return ok.shift(1).fillna(False).astype(bool) if isinstance(ok, pd.DataFrame) else True


# --------------------------------------------------------------------------- warm-up

def warmup(oks: list, index, columns, buffer: int = 0) -> pd.DataFrame:
    """A1: True from the first row where every node of every expression is defined (per security),
    plus `buffer` rows."""
    ok = pd.DataFrame(True, index=index, columns=columns)
    for o in oks:
        if isinstance(o, pd.DataFrame):
            ok &= o.reindex(index=index, columns=columns).fillna(False).astype(bool)
    w = ok.cummax()
    if buffer:
        w = w.shift(buffer).fillna(False).astype(bool)
    return w


# --------------------------------------------------------------------------- lints (A2, A3)

def _input_field(call: Call) -> str | None:
    """Field a highest/lowest call reads: its first argument if it's a field name, else the default."""
    if call.args and isinstance(call.args[0], Name):
        return call.args[0].name
    if call.args and not isinstance(call.args[0], (Num, Param)):
        return None
    return "high" if call.name == "highest" else "low"


LEVEL_FUNCS = {"sma", "ema", "highest", "lowest", "prior_high", "prior_low", "atr", "vwap",
               "bollinger_mid", "bollinger_upper", "bollinger_lower", "keltner_mid", "keltner_upper", "keltner_lower"}


def _constant(node) -> bool:
    return all(isinstance(x, (Num, Param, Neg, Bin)) or (isinstance(x, Call) and x.name in ("abs", "min", "max"))
               for x in walk(node))


def _level(node) -> str | None:
    """The adjusted price or volume level an expression measures, if it is one (not a ratio)."""
    if isinstance(node, Name) and node.name in ADJ_PRICES + ("volume", "deliv_qty"):
        return node.name
    if isinstance(node, Call) and node.name in LEVEL_FUNCS:
        return node.name + "()"
    if isinstance(node, Neg):
        return _level(node.x)
    if isinstance(node, Bin) and node.op in "+-":
        return _level(node.a) or _level(node.b)
    if isinstance(node, Bin) and node.op == "*":
        if _constant(node.a):
            return _level(node.b)
        if _constant(node.b):
            return _level(node.a)
    if isinstance(node, Bin) and node.op == "/" and _constant(node.b):
        return _level(node.a)
    return None


def _scaled(node):
    """(multiplier, call) for highest/lowest(...) optionally times a numeric literal."""
    if isinstance(node, Call) and node.name in ("highest", "lowest"):
        return 1.0, node
    if isinstance(node, Bin) and node.op == "*":
        for k, c in ((node.a, node.b), (node.b, node.a)):
            if isinstance(k, Num) and isinstance(c, Call) and c.name in ("highest", "lowest"):
                return k.value, c
    return None


def lint(p: Parsed) -> list[str]:
    """Warnings (not errors), with file:line:col."""
    out = []

    def warn(msg, node):
        line, col = p.where.at(node.pos)
        out.append(f"{p.where.file}:{line}:{col}: warning: {msg}")

    flip = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}
    for n in walk(p.tree):
        if not isinstance(n, Cmp):
            continue
        for f, other, op in ((n.a, n.b, n.op), (n.b, n.a, flip.get(n.op, n.op))):
            # A2: a price compared with an unshifted highest/lowest of the same or a containing field
            sc = _scaled(other)
            if isinstance(f, Name) and f.name in ("open", "high", "low", "close") and sc:
                k, call = sc
                x = _input_field(call)
                if call.name == "highest" and op in (">", ">=") and k >= 1 and x in ("high", f.name) \
                        and (op == ">" or k > 1):
                    warn(f"'{f.name} > {'' if k == 1 else f'{k:g} * '}highest({x}, n)' can never be true: "
                         f"highest() includes today; use prior_high(n)", n)
                if call.name == "lowest" and op in ("<", "<=") and k <= 1 and x in ("low", f.name) \
                        and (op == "<" or k < 1):
                    warn(f"'{f.name} < {'' if k == 1 else f'{k:g} * '}lowest({x}, n)' can never be true: "
                         f"lowest() includes today; use prior_low(n)", n)
        # A3: an adjusted price or volume level compared with a constant
        for side, other in ((n.a, n.b), (n.b, n.a)):
            lvl = _level(side)
            if lvl and _constant(other):
                raw = f"raw_{lvl}" if lvl in ADJ_PRICES else "a ratio or raw field"
                warn(f"adjusted level '{lvl}' compared with a constant: adjusted levels change with later "
                     f"splits and bonuses; use {raw}", n)
                break
    return out
