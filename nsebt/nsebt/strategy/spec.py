"""YAML strategies (M3.4): schema, parameters, `--set` overrides; compiled to the Strategy interface.

    name: macd_trend_pullback            # required
    description: ...
    universe: nifty500                   # nifty500 (default) | all
    params: {rsi_max: 60, vol_mult: 1.5} # types come from the defaults: int, float or bool
    rebalance: daily                     # daily (default) | weekly | monthly: entries and exits are
                                         #   only decided on the last trading day of each period
    entry:                               # required: an expression, or {all: [...]} / {any: [...]}, nestable
      all: [close > sma(200), rsi(14) < $rsi_max]
    exit: {any: [close < sma(50)]}
    rank: roc(63)                        # higher ranks first when there are more candidates than slots
    stop_loss: 2 * atr(14)               # price distance below the entry (adjusted units)
    take_profit: 3 * atr(14)
    trailing_stop: 0.1 * close           # distance below the highest close since entry
    max_hold_days: 20
    max_positions: 3                     # the account limit wins when it is lower
    sizing: risk_pct(1.0)                # equal (default) | fixed(25000) | risk_pct(1.0)
    warmup_buffer: 0                     # extra rows after the A1 warm-up

Errors (unknown keys, bad types, bad expressions, bad overrides) report file:line:col.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

from ..backtest.sim import Sizing
from .base import Context, Output, Strategy, rebalance_days
from .evaluate import FIELDS, MULTI, SPECIAL, Evaluator, lint, warmup
from .expr import Call, ExprError, Name, Param, Parsed, Where, parse, walk
from .. import indicators as I

KEYS = {"name", "description", "universe", "params", "rebalance", "entry", "exit", "rank", "stop_loss",
        "take_profit", "trailing_stop", "max_hold_days", "max_positions", "sizing", "warmup_buffer"}
RE_SIZING = re.compile(r"^\s*(equal|fixed|risk_pct)\s*(?:\(\s*([0-9]*\.?[0-9]+)\s*\))?\s*$")


class SpecError(ValueError):
    def __init__(self, msg, file="<spec>", line=1, col=1):
        self.msg, self.file, self.line, self.col = msg, file, line, col
        super().__init__(f"{file}:{line}:{col}: {msg}")


# --------------------------------------------------------------------------- YAML with positions

@dataclass
class Item:
    value: object
    line: int
    col: int
    quoted: bool = False
    flow: bool = False        # an item of a [a, b] flow list (YAML splits it at commas)
    exact: bool = True        # single-line value: columns inside it map to the file


def _plain(node, file):
    """YAML node -> python, with every scalar wrapped in Item (1-based line/col)."""
    if isinstance(node, yaml.ScalarNode):
        if node.tag == "tag:yaml.org,2002:str":
            v = node.value
        elif node.tag == "tag:yaml.org,2002:null":
            v = None
        else:
            v = yaml.constructor.SafeConstructor().construct_object(node)
        quoted = node.style in ("'", '"')
        exact = node.start_mark.line == node.end_mark.line and node.style not in ("|", ">", '"')
        return Item(v, node.start_mark.line + 1, node.start_mark.column + 1 + (1 if quoted else 0), quoted,
                    exact=exact)
    if isinstance(node, yaml.SequenceNode):
        items = [_plain(x, file) for x in node.value]
        if node.flow_style:
            for x in items:
                x.flow = True
        return Item(items, node.start_mark.line + 1, node.start_mark.column + 1)
    if isinstance(node, yaml.MappingNode):
        out = {}
        for k, v in node.value:
            key = k.value
            if key in out:
                raise SpecError(f"key '{key}' given twice", file, k.start_mark.line + 1, k.start_mark.column + 1)
            out[key] = (Item(key, k.start_mark.line + 1, k.start_mark.column + 1), _plain(v, file))
        return Item(out, node.start_mark.line + 1, node.start_mark.column + 1)
    raise SpecError("unsupported YAML", file)          # pragma: no cover


def _read(text: str, file: str) -> Item:
    try:
        node = yaml.compose(text)
    except yaml.YAMLError as e:
        m = getattr(e, "problem_mark", None)
        raise SpecError(f"invalid YAML: {getattr(e, 'problem', e)}", file,
                        m.line + 1 if m else 1, m.column + 1 if m else 1) from None
    if node is None or not isinstance(node, yaml.MappingNode):
        raise SpecError("a strategy file must be a mapping of keys", file)
    return _plain(node, file)


# --------------------------------------------------------------------------- the strategy

@dataclass
class Cond:
    kind: str                 # expr | all | any
    expr: Parsed | None = None
    items: tuple = ()


class YamlStrategy(Strategy):
    def __init__(self, text: str, file: str = "<spec>", overrides: dict | None = None):
        self.text, self.file = text, file
        root = _read(text, file).value
        for k, (ki, _) in root.items():
            if k not in KEYS:
                raise SpecError(f"unknown key '{k}'; allowed: {', '.join(sorted(KEYS))}", file, ki.line, ki.col)
        self._root = root
        self.name = self._str("name", required=True)
        self.description = self._str("description") or ""
        self.universe = self._str("universe") or "nifty500"
        if self.universe not in ("nifty500", "all"):
            self._fail("universe must be nifty500 or all", "universe")
        self.defaults = self._params()
        self.params = dict(self.defaults)
        self.overrides = {}
        for k, v in (overrides or {}).items():
            self.set(k, v)
        self.rebalance = self._str("rebalance") or "daily"
        if self.rebalance not in ("daily", "weekly", "monthly"):
            self._fail("rebalance must be daily, weekly or monthly", "rebalance")
        if "entry" not in root:
            raise SpecError("missing required key 'entry'", file)
        self.entry = self._cond("entry")
        self.exit = self._cond("exit") if "exit" in root else None
        self.exprs = {k: self._expr(k) for k in ("rank", "stop_loss", "take_profit", "trailing_stop") if k in root}
        self.max_hold_days = self._int("max_hold_days", 1, None)
        self.max_positions = self._int("max_positions", 1, 100)
        self.warmup_buffer = self._int("warmup_buffer", 0, None) or 0
        self.sizing = self._sizing()
        self._check_names()

    # ------------------------------------------------------------------ parameters
    def set(self, name: str, raw):
        """Override a declared parameter; the value is converted to the declared type."""
        if name not in self.defaults:
            raise SpecError(f"--set {name}: unknown parameter; declared: {', '.join(sorted(self.defaults)) or 'none'}",
                            self.file)
        d = self.defaults[name]
        s = str(raw).strip()
        try:
            if isinstance(d, bool):
                if s.lower() not in ("true", "false"):
                    raise ValueError
                v = s.lower() == "true"
            elif isinstance(d, int):
                if not re.fullmatch(r"[+-]?\d+", s):
                    raise ValueError
                v = int(s)
            else:
                if not re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", s):
                    raise ValueError
                v = float(s)
        except ValueError:
            kind = "true or false" if isinstance(d, bool) else "a whole number" if isinstance(d, int) else "a number"
            raise SpecError(f"--set {name}={raw}: must be {kind} (default {d!r})", self.file) from None
        self.params[name] = v
        self.overrides[name] = v

    def _params(self) -> dict:
        if "params" not in self._root:
            return {}
        _, it = self._root["params"]
        if not isinstance(it.value, dict):
            raise SpecError("params must be a mapping of name: default", self.file, it.line, it.col)
        out = {}
        for k, (ki, vi) in it.value.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                raise SpecError(f"parameter name '{k}' is not a valid name", self.file, ki.line, ki.col)
            v = vi.value
            if v is None or not isinstance(v, (int, float, bool)) or isinstance(v, str):
                raise SpecError(f"parameter '{k}' must default to a number or true/false", self.file, vi.line, vi.col)
            out[k] = v
        return out

    # ------------------------------------------------------------------ schema helpers
    def _fail(self, msg, key):
        ki, vi = self._root[key]
        raise SpecError(f"{key}: {msg}", self.file, vi.line, vi.col)

    def _str(self, key, required=False):
        if key not in self._root:
            if required:
                raise SpecError(f"missing required key '{key}'", self.file)
            return None
        _, vi = self._root[key]
        if vi.value is None and not required:
            return None
        if not isinstance(vi.value, str) or not vi.value.strip():
            self._fail("must be text", key)
        return vi.value.strip()

    def _int(self, key, lo, hi):
        if key not in self._root:
            return None
        _, vi = self._root[key]
        v = vi.value
        if isinstance(v, bool) or not isinstance(v, int) or v < lo or (hi is not None and v > hi):
            self._fail(f"must be a whole number{f' from {lo} to {hi}' if hi else f' >= {lo}'}", key)
        return v

    def _parse(self, item: Item) -> Parsed:
        if item.value is None:
            raise SpecError("empty value: expected an expression", self.file, item.line, item.col)
        if not isinstance(item.value, (str, int, float)) or isinstance(item.value, bool):
            raise SpecError("expected an expression", self.file, item.line, item.col)
        try:
            return parse(str(item.value), Where(self.file, item.line, item.col, item.exact))
        except ExprError as e:
            hint = ("; in a [a, b] list YAML splits at commas, so quote expressions that contain commas "
                    "or use a '- ' list" if item.flow and not item.quoted else "")
            raise SpecError(e.msg + hint, self.file, e.line, e.col) from None

    def _expr(self, key) -> Parsed:
        _, vi = self._root[key]
        return self._parse(vi)

    def _cond(self, key) -> Cond:
        _, vi = self._root[key]
        return self._cond_item(vi)

    def _cond_item(self, it: Item) -> Cond:
        if isinstance(it.value, dict):
            if len(it.value) != 1 or next(iter(it.value)) not in ("all", "any"):
                raise SpecError("a condition block must be {all: [...]} or {any: [...]}", self.file, it.line, it.col)
            kind, (_, lst) = next(iter(it.value.items()))
            if not isinstance(lst.value, list) or not lst.value:
                raise SpecError(f"'{kind}' needs a non-empty list", self.file, lst.line, lst.col)
            return Cond(kind, items=tuple(self._cond_item(x) for x in lst.value))
        return Cond("expr", self._parse(it))

    def _sizing(self) -> Sizing:
        if "sizing" not in self._root:
            return Sizing()
        _, vi = self._root["sizing"]
        m = RE_SIZING.match(str(vi.value))
        if not m or (m.group(1) == "equal") != (m.group(2) is None):
            raise SpecError("sizing must be equal, fixed(amount) or risk_pct(percent)", self.file, vi.line, vi.col)
        try:
            return Sizing(m.group(1), float(m.group(2)) if m.group(2) else None)
        except ValueError as e:
            raise SpecError(str(e), self.file, vi.line, vi.col) from None

    # ------------------------------------------------------------------ static checks
    def parsed(self) -> list[Parsed]:
        out = []

        def conds(c):
            if c is None:
                return
            if c.kind == "expr":
                out.append(c.expr)
            for x in c.items:
                conds(x)
        conds(self.entry)
        conds(self.exit)
        out += list(self.exprs.values())
        return out

    def _check_names(self):
        known = set(SPECIAL) | set(MULTI) | {n for n, s in I.REGISTRY.items() if not s.outputs}
        for p in self.parsed():
            for n in walk(p.tree):
                bad = None
                if isinstance(n, Name) and n.name not in FIELDS:
                    bad = f"unknown name '{n.name}'"
                elif isinstance(n, Call) and n.name not in known:
                    bad = f"unknown function '{n.name}'"
                elif isinstance(n, Param) and n.name not in self.params:
                    bad = f"unknown parameter ${n.name}; declared: {', '.join(sorted(self.params)) or 'none'}"
                if bad:
                    line, col = p.where.at(n.pos)
                    if not p.where.exact:
                        bad += " (in the multi-line value starting here)"
                    raise SpecError(bad, self.file, line, col)

    def lints(self) -> list[str]:
        out = [w for p in self.parsed() for w in lint(p)]
        for k in ("stop_loss", "take_profit", "trailing_stop"):
            p = self.exprs.get(k)
            if p is not None and not any(isinstance(n, (Name, Call)) for n in walk(p.tree)):
                line, col = p.where.at(0)
                out.append(f"{self.file}:{line}:{col}: warning: {k} is a constant price distance in rupees of "
                           f"adjusted price, not a fraction; for a percentage write e.g. 0.05 * close")
        return out

    # ------------------------------------------------------------------ the interface
    def generate(self, ctx: Context) -> Output:
        e = Evaluator(ctx, self.params)
        oks = []                  # A1 warm-up: what an entry decision reads (entry, rank, stop distances)

        def evaluate(p, warm=True):
            try:
                v = e.evaluate(p)
            except ExprError as x:
                raise SpecError(x.msg, self.file, x.line, x.col) from None
            if warm:
                oks.append(v.ok)
            return v

        def cond(c, warm=True):
            if c.kind == "expr":
                v = evaluate(c.expr, warm)
                if not isinstance(v.v, pd.DataFrame):
                    line, col = c.expr.where.at(c.expr.tree.pos)
                    raise SpecError("a condition must depend on the data (it is a constant)", self.file, line, col)
                if not v.v.dtypes.apply(lambda d: d == bool).all():
                    line, col = c.expr.where.at(c.expr.tree.pos)
                    raise SpecError("a condition must be true/false (e.g. rsi(14) < 30), not a number",
                                    self.file, line, col)
                return e.truth(v)
            parts = [cond(x, warm) for x in c.items]
            out = parts[0]
            for x in parts[1:]:
                out = (out & x) if c.kind == "all" else (out | x)
            return out

        close = ctx.panel.close
        idx, cols = close.index, close.columns
        entry = cond(self.entry)
        exit_ = cond(self.exit, warm=False) if self.exit else None
        frames = {k: evaluate(p).v for k, p in self.exprs.items()}
        reb = rebalance_days(idx, self.rebalance)
        on = reb.to_numpy()[:, None]
        entry = entry.reindex(index=idx, columns=cols).fillna(False).astype(bool) & on
        if exit_ is not None:
            exit_ = exit_.reindex(index=idx, columns=cols).fillna(False).astype(bool) & on

        notes = []

        def dist(k):
            if k not in frames:
                return None
            v = frames[k]
            f = (v / close) if isinstance(v, pd.DataFrame) else close * 0 + v / close
            bad = int(((f <= 0) & f.notna()).to_numpy().sum())
            if bad:
                notes.append(f"{k} is zero or negative on {bad:,} security-days; those entries get no {k}")
            return f
        score = frames.get("rank")
        if score is not None and not isinstance(score, pd.DataFrame):
            score = None                                   # a constant rank orders nothing
        return Output(entry=entry, exit=exit_, score=score, stop=dist("stop_loss"), target=dist("take_profit"),
                      trail=dist("trailing_stop"), warmup=warmup(oks, idx, cols, self.warmup_buffer),
                      max_hold_days=self.max_hold_days, sizing=self.sizing, max_positions=self.max_positions,
                      notes=tuple(self.lints()) + tuple(e.notes) + tuple(notes))

    def dry_run(self) -> Output:
        """Evaluate every expression on a small synthetic panel: catches errors only data reveals
        (bad indicator parameters, shift by a $param < 1, rank of a constant)."""
        return self.generate(Context(synthetic_panel(), self.params))

    def source(self) -> str:
        over = "".join(f"\n# --set {k}={v}" for k, v in sorted(self.overrides.items()))
        return self.text + over


def load(path, overrides: dict | None = None) -> YamlStrategy:
    path = Path(path)
    return YamlStrategy(path.read_text(), str(path), overrides)


def parse_sets(items) -> dict:
    """['a=1', 'b=2'] -> {'a': '1', 'b': '2'}, rejecting malformed items."""
    out = {}
    for s in items or ():
        if "=" not in s or not s.split("=", 1)[0].strip():
            raise SpecError(f"--set {s}: expected name=value")
        k, v = s.split("=", 1)
        if k.strip() in out:
            raise SpecError(f"--set {k.strip()}: given twice")
        out[k.strip()] = v.strip()
    return out


def synthetic_panel(n: int = 320, k: int = 4, seed: int = 0):
    """A small random panel with every field the language can read (for dry runs)."""
    import numpy as np
    from ..data import Panel
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2015-01-01", periods=n)
    ids = [f"S{i}" for i in range(k)]
    c = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.02, (n, k)), axis=0)), index=days, columns=ids)
    v = pd.DataFrame(rng.uniform(1e5, 1e6, (n, k)), index=days, columns=ids)
    f = {"close": c, "open": c.shift(1).fillna(c), "high": c * 1.01, "low": c * 0.99, "vwap": c, "volume": v,
         "deliv_qty": v * 0.4, "raw_close": c, "raw_open": c, "raw_high": c * 1.01, "raw_low": c * 0.99,
         "raw_volume": v, "turnover": v * c, "deliv_pct": c * 0 + 40.0}
    sec = pd.DataFrame({"SecurityID": ids, "Industry": [None] * k})
    idx = pd.DataFrame({"Date": days, "Index": "Nifty 500", "Close": c.mean(axis=1).to_numpy()})
    return Panel(f, pd.DataFrame(True, index=days, columns=ids), sec, idx)
