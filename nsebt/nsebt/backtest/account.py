"""Account limits chosen by the user: capital, slots, position cap, liquidity participation.

Stored in the `account:` block of config.yaml. `nsebt account` asks for each value
(Enter keeps the current one); `nsebt backtest` flags override them for one run.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Callable

MAX_CAPITAL = 1_000_000_000          # ₹100 crore


class AccountError(ValueError):
    pass


# field -> (label, type, lower bound, lower inclusive, upper bound)
LIMITS = {
    "capital": ("Starting capital (₹)", float, 0.0, False, MAX_CAPITAL),
    "max_positions": ("Maximum open positions", int, 1, True, 100),
    "max_position_pct": ("Maximum position size (% of equity)", float, 1.0, True, 100.0),
    "max_participation_pct": ("Maximum order size (% of 20-day average traded value)", float, 0.1, True, 100.0),
}


def parse(name: str, value) -> float | int:
    """Validate one setting. Accepts '36,000', '₹36000', '1e5', '5%'."""
    if name not in LIMITS:
        raise AccountError(f"unknown account setting '{name}'")
    label, typ, lo, lo_inc, hi = LIMITS[name]
    raw = value
    if isinstance(value, str):
        s = re.sub(r"[₹,_\s%]", "", value).removeprefix("Rs").removeprefix("INR")
        try:
            value = float(s)
        except ValueError:
            raise AccountError(f"{label}: '{raw}' is not a number") from None
    value = float(value)
    if value != value:  # NaN
        raise AccountError(f"{label}: not a number")
    if typ is int:
        if value != int(value):
            raise AccountError(f"{label}: must be a whole number, got {raw}")
        value = int(value)
    ok_lo = value >= lo if lo_inc else value > lo
    if not ok_lo or value > hi:
        lo_txt = f"at least {lo:g}" if lo_inc else f"more than {lo:g}"
        hi_txt = f"{hi:,.0f}" if float(hi).is_integer() else f"{hi:g}"
        raise AccountError(f"{label}: must be {lo_txt} and at most {hi_txt}, got {raw}")
    return value


@dataclass(frozen=True)
class Account:
    capital: float = 36_000.0
    max_positions: int = 3
    max_position_pct: float = 33.0
    max_participation_pct: float = 5.0

    def __post_init__(self):
        for f in fields(self):
            object.__setattr__(self, f.name, parse(f.name, getattr(self, f.name)))

    def with_overrides(self, **kw) -> "Account":
        return replace(self, **{k: v for k, v in kw.items() if v is not None})

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        return (f"capital ₹{self.capital:,.0f}, up to {self.max_positions} positions, "
                f"≤{self.max_position_pct:g}% each, ≤{self.max_participation_pct:g}% of 20-day traded value")


def from_config(raw: dict) -> Account | None:
    """The saved account, or None if none has been saved yet."""
    block = raw.get("account")
    if not block:
        return None
    unknown = set(block) - set(LIMITS)
    if unknown:
        raise AccountError(f"config.yaml account: unknown setting(s) {sorted(unknown)}")
    return Account(**block)


def prompt(current: Account | None = None, ask: Callable[[str], str] | None = None,
           say: Callable[[str], None] | None = None) -> Account:
    """Ask for each value; Enter keeps the current one. Re-asks until valid."""
    ask = ask or (lambda q: input(q))
    say = say or print
    cur = current or Account()
    vals = {}
    for name, (label, typ, *_rest) in LIMITS.items():
        default = getattr(cur, name)
        shown = f"{default:,.0f}" if name == "capital" else f"{default:g}"
        while True:
            ans = ask(f"{label} [{shown}]: ").strip()
            if not ans:
                vals[name] = default
                break
            try:
                vals[name] = parse(name, ans)
                break
            except AccountError as e:
                say(f"  {e}")
    return Account(**vals)


def _yaml_block(acc: Account) -> str:
    d = acc.to_dict()
    cap = int(d["capital"]) if float(d["capital"]).is_integer() else d["capital"]
    return ("account:\n"
            "  # set with `nsebt account`; per-run flags on `nsebt backtest` override these\n"
            f"  capital: {cap}                  # ₹, at most {MAX_CAPITAL:,}\n"
            f"  max_positions: {d['max_positions']}\n"
            f"  max_position_pct: {d['max_position_pct']:g}           # % of equity\n"
            f"  max_participation_pct: {d['max_participation_pct']:g}       # % of 20-day average traded value\n")


def save(config_path: Path, acc: Account) -> None:
    """Write the account block into config.yaml, keeping every other line and comment."""
    path = Path(config_path)
    lines = path.read_text().splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if l.startswith("account:")), None)
    block = _yaml_block(acc)
    if start is None:
        text = "".join(lines)
        if text and not text.endswith("\n"):
            text += "\n"
        path.write_text(text + "\n" + block)
        return
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t#"):
        end += 1
    # keep trailing blank lines that separate the next block
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    path.write_text("".join(lines[:start]) + block + "".join(lines[end:]))
