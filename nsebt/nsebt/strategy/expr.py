"""Expression language for YAML strategies: tokenizer and parser (no `eval`).

Grammar, lowest precedence first:

    expr    := or
    or      := and ("or" and)*
    and     := not ("and" not)*
    not     := "not" not | cmp
    cmp     := sum (CMP sum)?               CMP: < <= > >= == !=   (comparisons don't chain)
    sum     := term (("+" | "-") term)*
    term    := unary (("*" | "/") unary)*
    unary   := "-" unary | atom
    atom    := NUMBER | "true" | "false" | $PARAM | NAME | NAME "(" [arg ("," arg)*] ")" | "(" expr ")"
    arg     := expr | NAME "=" expr

Every node records where it starts, so errors point at file:line:col.
`shift(x, n)` must look back (n >= 1); a literal n is checked here, a $param when it is bound.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

KEYWORDS = {"and", "or", "not", "true", "false"}
CMP = {"<", "<=", ">", ">=", "==", "!="}
TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
  | (?P<param>\$[A-Za-z_][A-Za-z0-9_]*)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op><=|>=|==|!=|[<>+\-*/(),=])
""", re.X)


@dataclass(frozen=True)
class Where:
    file: str = "<expr>"
    line: int = 1            # 1-based line of the expression's first character in the file
    col: int = 1             # 1-based column of the expression's first character in the file
    exact: bool = True       # False for multi-line YAML values: positions point at the value's start

    def at(self, offset: int) -> tuple[int, int]:
        return (self.line, self.col + offset) if self.exact else (self.line, self.col)


class ExprError(ValueError):
    def __init__(self, msg: str, where: Where = Where(), offset: int = 0):
        self.msg, self.where, self.offset = msg, where, offset
        line, col = where.at(offset)
        self.line, self.col = line, col
        if not where.exact:
            msg = f"{msg} (in the multi-line value starting here)"
            self.msg = msg
        super().__init__(f"{where.file}:{line}:{col}: {msg}")


@dataclass(frozen=True)
class Tok:
    kind: str     # num | param | name | op | kw | end
    text: str
    pos: int


# --------------------------------------------------------------------------- AST

@dataclass(frozen=True)
class Node:
    pos: int


@dataclass(frozen=True)
class Num(Node):
    value: float


@dataclass(frozen=True)
class Bool(Node):
    value: bool


@dataclass(frozen=True)
class Param(Node):
    name: str


@dataclass(frozen=True)
class Name(Node):
    name: str


@dataclass(frozen=True)
class Call(Node):
    name: str
    args: tuple = ()
    kwargs: tuple = ()       # ((name, node), ...)


@dataclass(frozen=True)
class Neg(Node):
    x: Node


@dataclass(frozen=True)
class Bin(Node):
    op: str                  # + - * /
    a: Node
    b: Node


@dataclass(frozen=True)
class Cmp(Node):
    op: str
    a: Node
    b: Node


@dataclass(frozen=True)
class BoolOp(Node):
    op: str                  # and | or
    items: tuple


@dataclass(frozen=True)
class Not(Node):
    x: Node


@dataclass
class Parsed:
    text: str
    tree: Node
    where: Where = field(default_factory=Where)

    def error(self, msg: str, node: Node) -> ExprError:
        return ExprError(msg, self.where, node.pos)


# --------------------------------------------------------------------------- tokenizer

def tokenize(text: str, where: Where = Where()) -> list[Tok]:
    out, i = [], 0
    while i < len(text):
        m = TOKEN.match(text, i)
        if not m:
            raise ExprError(f"unexpected character {text[i]!r}", where, i)
        kind = m.lastgroup
        if kind != "ws":
            t = m.group()
            if kind == "name" and t in KEYWORDS:
                kind = "kw"
            out.append(Tok(kind, t, i))
        i = m.end()
    out.append(Tok("end", "", len(text)))
    return out


# --------------------------------------------------------------------------- parser

class _Parser:
    def __init__(self, text: str, where: Where):
        self.text, self.where = text, where
        self.toks = tokenize(text, where)
        self.i = 0

    @property
    def tok(self) -> Tok:
        return self.toks[self.i]

    def take(self) -> Tok:
        t = self.tok
        self.i += 1
        return t

    def err(self, msg: str, tok: Tok | None = None):
        t = tok or self.tok
        return ExprError(msg, self.where, t.pos)

    def expect(self, kind: str, text: str | None = None) -> Tok:
        t = self.tok
        if t.kind != kind or (text is not None and t.text != text):
            want = text or kind
            got = "end of expression" if t.kind == "end" else repr(t.text)
            raise self.err(f"expected {want!r}, found {got}")
        return self.take()

    def parse(self) -> Node:
        if self.tok.kind == "end":
            raise self.err("empty expression")
        n = self.or_()
        if self.tok.kind != "end":
            raise self.err(f"unexpected {self.tok.text!r}")
        return n

    def or_(self) -> Node:
        first = self.and_()
        items = [first]
        while self.tok.kind == "kw" and self.tok.text == "or":
            self.take()
            items.append(self.and_())
        return first if len(items) == 1 else BoolOp(first.pos, "or", tuple(items))

    def and_(self) -> Node:
        first = self.not_()
        items = [first]
        while self.tok.kind == "kw" and self.tok.text == "and":
            self.take()
            items.append(self.not_())
        return first if len(items) == 1 else BoolOp(first.pos, "and", tuple(items))

    def not_(self) -> Node:
        if self.tok.kind == "kw" and self.tok.text == "not":
            t = self.take()
            return Not(t.pos, self.not_())
        return self.cmp()

    def cmp(self) -> Node:
        a = self.sum_()
        if self.tok.kind == "op" and self.tok.text in CMP:
            op = self.take()
            b = self.sum_()
            if self.tok.kind == "op" and self.tok.text in CMP:
                raise self.err("comparisons can't be chained; use 'and'")
            return Cmp(op.pos, op.text, a, b)
        return a

    def sum_(self) -> Node:
        n = self.term()
        while self.tok.kind == "op" and self.tok.text in "+-":
            op = self.take()
            n = Bin(op.pos, op.text, n, self.term())
        return n

    def term(self) -> Node:
        n = self.unary()
        while self.tok.kind == "op" and self.tok.text in "*/":
            op = self.take()
            n = Bin(op.pos, op.text, n, self.unary())
        return n

    def unary(self) -> Node:
        if self.tok.kind == "op" and self.tok.text == "-":
            t = self.take()
            return Neg(t.pos, self.unary())
        return self.atom()

    def atom(self) -> Node:
        t = self.tok
        if t.kind == "num":
            self.take()
            return Num(t.pos, float(t.text))
        if t.kind == "kw" and t.text in ("true", "false"):
            self.take()
            return Bool(t.pos, t.text == "true")
        if t.kind == "param":
            self.take()
            return Param(t.pos, t.text[1:])
        if t.kind == "name":
            self.take()
            if self.tok.kind == "op" and self.tok.text == "(":
                return self.call(t)
            return Name(t.pos, t.text)
        if t.kind == "op" and t.text == "(":
            self.take()
            n = self.or_()
            self.expect("op", ")")
            return n
        if t.kind == "end":
            raise self.err("expression ends too early")
        raise self.err(f"unexpected {t.text!r}")

    def call(self, name: Tok) -> Node:
        self.expect("op", "(")
        args, kwargs = [], []
        if not (self.tok.kind == "op" and self.tok.text == ")"):
            while True:
                if self.tok.kind == "name" and self.toks[self.i + 1].kind == "op" and self.toks[self.i + 1].text == "=":
                    k = self.take()
                    self.take()
                    if any(n == k.text for n, _ in kwargs):
                        raise self.err(f"argument {k.text!r} given twice", k)
                    kwargs.append((k.text, self.or_()))
                else:
                    if kwargs:
                        raise self.err("positional argument after a keyword argument")
                    args.append(self.or_())
                if self.tok.kind == "op" and self.tok.text == ",":
                    self.take()
                    continue
                break
        self.expect("op", ")")
        node = Call(name.pos, name.text, tuple(args), tuple(kwargs))
        if name.text == "shift":
            if len(args) != 2 or kwargs:
                raise self.err("shift takes two arguments: shift(x, n)", name)
            n = args[1]
            if isinstance(n, Neg) or (isinstance(n, Num) and (n.value < 1 or n.value != int(n.value))):
                raise ExprError("shift(x, n) needs a whole number n >= 1: it may only look back", self.where, n.pos)
        return node


def parse(text: str, where: Where = Where()) -> Parsed:
    if not isinstance(text, str):
        raise ExprError(f"expected an expression string, found {type(text).__name__}", where)
    return Parsed(text, _Parser(text, where).parse(), where)


def walk(node: Node):
    """Every node in the tree, parents first."""
    yield node
    for v in vars(node).values():
        if isinstance(v, Node):
            yield from walk(v)
        elif isinstance(v, tuple):
            for x in v:
                if isinstance(x, Node):
                    yield from walk(x)
                elif isinstance(x, tuple) and len(x) == 2 and isinstance(x[1], Node):
                    yield from walk(x[1])
