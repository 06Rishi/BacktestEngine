"""Technical indicators on wide panels (rows = dates, columns = securities).

Every function is vectorised across securities and uses only data up to each row's
date. Inputs are adjusted prices (see nsestore.load_prices / data.load_panel).

Conventions (chosen to match TA-Lib, the reference library):
  * exponential averages (EMA, Wilder) start from the simple average of the first n
    values, then recurse; nothing is reported before the first full window
  * Wilder smoothing uses alpha = 1/n (RSI, ATR, ADX)
  * Bollinger bands use the population standard deviation (ddof=0)
  * returns, ROC, slopes and distances are fractions (0.05 = 5%), not percentages;
    RSI, stochastic, ADX/DI and Aroon are on their usual 0-100 scale

Gaps: if a security has missing days inside its history (a suspension), its
indicators are computed on its own trading days only, so a 20-day window always
holds 20 trading days of that security.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

Frame = pd.DataFrame


# --------------------------------------------------------------------------- gap handling

def _gappy_columns(x: Frame) -> list:
    v = x.notna().to_numpy()
    cnt = v.sum(axis=0)
    first = np.where(cnt > 0, v.argmax(axis=0), 0)
    last = np.where(cnt > 0, len(v) - 1 - v[::-1].argmax(axis=0), -1)
    span = last - first + 1
    return list(x.columns[(cnt > 0) & (cnt < span)])


def per_security(fn: Callable) -> Callable:
    """Run fn on the whole panel for gap-free columns, and column by column on each
    gappy column's own trading days (rows where the FIRST input is present)."""
    def wrapped(*frames, **params):
        x0 = frames[0]
        if not isinstance(x0, pd.DataFrame):
            return fn(*frames, **params)
        gappy = _gappy_columns(x0)
        if not gappy:
            return fn(*frames, **params)
        clean = [c for c in x0.columns if c not in set(gappy)]
        out = fn(*[f[clean] if isinstance(f, pd.DataFrame) else f for f in frames], **params)
        single = isinstance(out, pd.DataFrame)
        outs = [out] if single else list(out)
        parts = [[o] for o in outs]
        for c in gappy:
            rows = x0[c].notna()
            sub = [f.loc[rows, [c]] if isinstance(f, pd.DataFrame) else f for f in frames]
            r = fn(*sub, **params)
            for k, o in enumerate([r] if single else list(r)):
                parts[k].append(o.reindex(x0.index))
        res = [pd.concat(p, axis=1)[list(x0.columns)] for p in parts]
        return res[0] if single else type(out)(*res) if hasattr(out, "_fields") else tuple(res)
    wrapped.__name__, wrapped.__doc__ = fn.__name__, fn.__doc__
    wrapped.__wrapped__ = fn
    return wrapped


def _started(x: Frame) -> Frame:
    return x.notna().cummax()


def _seeded_ewm(x: Frame, n: int, alpha: float, seed_len: int | None = None, seed_div: int | None = None) -> Frame:
    """Recursive average y_t = alpha*x_t + (1-alpha)*y_{t-1}, seeded with
    sum(first seed_len values) / seed_div (default: the simple mean of the first n)."""
    seed_len = seed_len or n
    seed_div = seed_div or seed_len
    seed = x.rolling(seed_len, min_periods=seed_len).sum() / seed_div
    on = _started(seed)
    first = on & ~on.shift(1, fill_value=False)
    y = x.where(on & ~first).mask(first, seed)
    return y.ewm(alpha=alpha, adjust=False).mean().where(on)


# --------------------------------------------------------------------------- trend

@per_security
def sma(x: Frame, n: int = 20) -> Frame:
    return x.rolling(n, min_periods=n).mean()


@per_security
def ema(x: Frame, n: int = 20) -> Frame:
    return _seeded_ewm(x, n, 2.0 / (n + 1))


@per_security
def wilder(x: Frame, n: int = 14) -> Frame:
    return _seeded_ewm(x, n, 1.0 / n)


def price_vs(x: Frame, ref: Frame) -> Frame:
    """x / ref - 1, e.g. close vs its 200-day average."""
    return x / ref - 1


def cross_up(a, b) -> Frame:
    """True on the day a closes above b after being at or below it the day before."""
    a1 = a.shift(1)
    b1 = b.shift(1) if isinstance(b, (pd.DataFrame, pd.Series)) else b
    return (a > b) & (a1 <= b1)


def cross_down(a, b) -> Frame:
    a1 = a.shift(1)
    b1 = b.shift(1) if isinstance(b, (pd.DataFrame, pd.Series)) else b
    return (a < b) & (a1 >= b1)


@per_security
def slope(x: Frame, n: int = 10) -> Frame:
    """Fractional change of x over n days (e.g. slope of a moving average)."""
    return x / x.shift(n) - 1


# --------------------------------------------------------------------------- momentum

@dataclass
class MACD:
    line: Frame
    signal: Frame
    hist: Frame
    _fields = ("line", "signal", "hist")

    def __iter__(self):
        return iter((self.line, self.signal, self.hist))


@per_security
def macd(close: Frame, fast: int = 12, slow: int = 26, signal: int = 9) -> MACD:
    """MACD with standard EMAs (alpha = 2/(n+1)). As in TA-Lib, the fast EMA's seed
    window ends on the same day as the slow one's, so both start together. (TA-Lib's
    plain MACD function uses rounded constants 0.15/0.075 for 12/26; its MACDEXT uses
    the standard ones and is the reference here.)"""
    age = _started(close).cumsum() - 1                    # rows since the first price
    lag = max(slow - fast, 0)
    line = ema.__wrapped__(close.where(age >= lag), fast) - ema.__wrapped__(close, slow)
    sig = ema.__wrapped__(line, signal)
    line = line.where(sig.notna())          # report line and signal from the same day
    return MACD(line, sig, line - sig)


@per_security
def rsi(close: Frame, n: int = 14) -> Frame:
    d = close.diff()
    up = _seeded_ewm(d.clip(lower=0), n, 1.0 / n)
    dn = _seeded_ewm((-d).clip(lower=0), n, 1.0 / n)
    r = 100 - 100 / (1 + up / dn)
    return r.where(dn != 0, 100.0).where(up.notna())


@dataclass
class Stoch:
    k: Frame
    d: Frame
    _fields = ("k", "d")

    def __iter__(self):
        return iter((self.k, self.d))


@per_security
def stochastic(high: Frame, low: Frame, close: Frame, n: int = 14, d: int = 3, smooth: int = 1) -> Stoch:
    """%K over n days (optionally smoothed), %D = SMA(d) of %K. Defaults = fast (14,3)."""
    hh = high.rolling(n, min_periods=n).max()
    ll = low.rolling(n, min_periods=n).min()
    rng = hh - ll
    k = (100 * (close - ll) / rng).where(rng != 0, 0.0).where(rng.notna())   # flat window -> 0, as TA-Lib
    if smooth > 1:
        k = k.rolling(smooth, min_periods=smooth).mean()
    return Stoch(k, k.rolling(d, min_periods=d).mean())


@per_security
def roc(x: Frame, n: int = 20) -> Frame:
    return x / x.shift(n) - 1


returns = roc


@per_security
def momentum_12_1(close: Frame, lookback: int = 252, skip: int = 21) -> Frame:
    """Return from `lookback` days ago to `skip` days ago (skips the latest month)."""
    return close.shift(skip) / close.shift(lookback) - 1


# --------------------------------------------------------------------------- volatility

@per_security
def true_range(high: Frame, low: Frame, close: Frame) -> Frame:
    pc = close.shift(1)
    hl, hc, lc = high - low, (high - pc).abs(), (low - pc).abs()
    tr = hl.where(hl >= hc, hc)
    tr = tr.where(tr >= lc, lc)
    return tr.where(pc.notna())


@per_security
def atr(high: Frame, low: Frame, close: Frame, n: int = 14) -> Frame:
    return _seeded_ewm(true_range.__wrapped__(high, low, close), n, 1.0 / n)


@dataclass
class Bands:
    mid: Frame
    upper: Frame
    lower: Frame
    pct_b: Frame
    width: Frame
    _fields = ("mid", "upper", "lower", "pct_b", "width")

    def __iter__(self):
        return iter((self.mid, self.upper, self.lower, self.pct_b, self.width))


@per_security
def bollinger(close: Frame, n: int = 20, k: float = 2.0) -> Bands:
    mid = close.rolling(n, min_periods=n).mean()
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    up, lo = mid + k * sd, mid - k * sd
    return Bands(mid, up, lo, (close - lo) / (up - lo), (up - lo) / mid)


@per_security
def keltner(high: Frame, low: Frame, close: Frame, n: int = 20, atr_n: int = 10, mult: float = 2.0) -> Bands:
    mid = ema.__wrapped__(close, n)
    a = atr.__wrapped__(high, low, close, atr_n)
    up, lo = mid + mult * a, mid - mult * a
    return Bands(mid, up, lo, (close - lo) / (up - lo), (up - lo) / mid)


@per_security
def hist_vol(close: Frame, n: int = 20, periods: int = 252) -> Frame:
    """Annualised standard deviation of daily log returns over n days."""
    return np.log(close).diff().rolling(n, min_periods=n).std() * np.sqrt(periods)


# --------------------------------------------------------------------------- trend strength

@dataclass
class ADX:
    adx: Frame
    plus_di: Frame
    minus_di: Frame
    _fields = ("adx", "plus_di", "minus_di")

    def __iter__(self):
        return iter((self.adx, self.plus_di, self.minus_di))


@per_security
def adx(high: Frame, low: Frame, close: Frame, n: int = 14) -> ADX:
    """Wilder's ADX/DMI. Smoothed TR and DM are seeded with the sum of the first n-1
    values, as in TA-Lib, so +DI/-DI match it from the first value."""
    up, dn = high.diff(), -low.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0).where(up.notna())
    mdm = dn.where((dn > up) & (dn > 0), 0.0).where(dn.notna())
    tr = true_range.__wrapped__(high, low, close)

    def sm(v):
        s = _seeded_ewm(v, n, 1.0 / n, seed_len=n - 1, seed_div=n)
        first = _started(s) & ~_started(s).shift(1, fill_value=False)
        return s.mask(first)               # first output is one step after the seed
    str_ = sm(tr)
    pdi, mdi = 100 * sm(pdm) / str_, 100 * sm(mdm) / str_
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
    return ADX(_seeded_ewm(dx, n, 1.0 / n), pdi, mdi)


@dataclass
class Aroon:
    up: Frame
    down: Frame
    osc: Frame
    _fields = ("up", "down", "osc")

    def __iter__(self):
        return iter((self.up, self.down, self.osc))


def _bars_since_extreme(x: Frame, n: int, highest: bool, chunk: int = 64) -> Frame:
    """Bars since the highest (lowest) value in the last n+1 bars; ties -> most recent.
    Uses strided windows over column chunks to keep memory small."""
    from numpy.lib.stride_tricks import sliding_window_view
    a = x.to_numpy(dtype=float)
    out = np.full(a.shape, np.nan)
    if len(a) <= n:
        return pd.DataFrame(out, index=x.index, columns=x.columns)
    for j in range(0, a.shape[1], chunk):
        w = sliding_window_view(a[:, j:j + chunk], n + 1, axis=0)[..., ::-1]   # newest first
        bad = np.isnan(w).any(axis=-1)
        filled = np.where(np.isnan(w), -np.inf if highest else np.inf, w)
        k = (filled.argmax(axis=-1) if highest else filled.argmin(axis=-1)).astype(float)
        k[bad] = np.nan
        out[n:, j:j + chunk] = k
    return pd.DataFrame(out, index=x.index, columns=x.columns)


@per_security
def aroon(high: Frame, low: Frame, n: int = 25) -> Aroon:
    upv = 100 * (n - _bars_since_extreme(high, n, True)) / n
    dnv = 100 * (n - _bars_since_extreme(low, n, False)) / n
    return Aroon(upv, dnv, upv - dnv)


# --------------------------------------------------------------------------- volume and delivery

@per_security
def volume_ratio(volume: Frame, n: int = 20) -> Frame:
    """Volume / its n-day average (the average includes today)."""
    return volume / volume.rolling(n, min_periods=n).mean()


@per_security
def obv(close: Frame, volume: Frame) -> Frame:
    """On-balance volume, starting at the first day's volume (as TA-Lib)."""
    s = np.sign(close.diff()) * volume
    first = _started(close) & ~_started(close).shift(1, fill_value=False)
    s = s.mask(first, volume)
    return s.cumsum().where(_started(close))


@per_security
def delivery_spike(deliv_qty: Frame, n: int = 20) -> Frame:
    """Delivered quantity / its n-day average."""
    return deliv_qty / deliv_qty.rolling(n, min_periods=n).mean()


# --------------------------------------------------------------------------- price levels

@per_security
def highest(x: Frame, n: int = 20) -> Frame:
    return x.rolling(n, min_periods=n).max()


@per_security
def lowest(x: Frame, n: int = 20) -> Frame:
    return x.rolling(n, min_periods=n).min()


def gap(open_: Frame, close: Frame) -> Frame:
    """Opening gap vs the previous close (fraction)."""
    return open_ / close.shift(1) - 1


# --------------------------------------------------------------------------- relative

def relative_strength(close: Frame, bench, n: int = 63) -> Frame:
    """Stock return over n days relative to a benchmark: (1+r_stock)/(1+r_bench) - 1.
    bench: a Series (one index for all) or a Frame aligned to close (e.g. sector)."""
    rs = close / close.shift(n)
    if isinstance(bench, pd.Series):
        rb = (bench / bench.shift(n)).reindex(close.index)
        return rs.div(rb, axis=0) - 1
    return rs / (bench / bench.shift(n)) - 1


def rank(x: Frame, mask: Frame | None = None, pct: bool = True) -> Frame:
    """Cross-sectional rank on each date (1 = highest when pct). mask limits the
    ranking to e.g. index members on that date."""
    if mask is not None:
        x = x.where(mask.reindex_like(x).fillna(False).astype(bool))
    return x.rank(axis=1, pct=pct)


# --------------------------------------------------------------------------- registry

@dataclass
class Spec:
    fn: Callable
    inputs: tuple            # panel fields
    params: dict = field(default_factory=dict)
    outputs: tuple = ()      # names of the parts for multi-output indicators
    lookback: Callable = lambda p: 0        # rows needed before the first exact value
    group: str = ""


def _ew(n):           # rows before an exponential average is exact to ~1e-9
    return 25 * n


REGISTRY: dict[str, Spec] = {
    "sma": Spec(sma, ("close",), {"n": 20}, lookback=lambda p: p["n"], group="trend"),
    "ema": Spec(ema, ("close",), {"n": 20}, lookback=lambda p: _ew(p["n"]), group="trend"),
    "slope": Spec(slope, ("close",), {"n": 10}, lookback=lambda p: p["n"], group="trend"),
    "macd": Spec(macd, ("close",), {"fast": 12, "slow": 26, "signal": 9}, ("line", "signal", "hist"),
                 lambda p: _ew(p["slow"]) + _ew(p["signal"]), "momentum"),
    "rsi": Spec(rsi, ("close",), {"n": 14}, lookback=lambda p: _ew(p["n"]), group="momentum"),
    "stochastic": Spec(stochastic, ("high", "low", "close"), {"n": 14, "d": 3, "smooth": 1}, ("k", "d"),
                       lambda p: p["n"] + p["d"] + p["smooth"], "momentum"),
    "roc": Spec(roc, ("close",), {"n": 20}, lookback=lambda p: p["n"], group="momentum"),
    "momentum_12_1": Spec(momentum_12_1, ("close",), {"lookback": 252, "skip": 21},
                          lookback=lambda p: p["lookback"], group="momentum"),
    "atr": Spec(atr, ("high", "low", "close"), {"n": 14}, lookback=lambda p: _ew(p["n"]), group="volatility"),
    "bollinger": Spec(bollinger, ("close",), {"n": 20, "k": 2.0}, ("mid", "upper", "lower", "pct_b", "width"),
                      lambda p: p["n"], "volatility"),
    "keltner": Spec(keltner, ("high", "low", "close"), {"n": 20, "atr_n": 10, "mult": 2.0},
                    ("mid", "upper", "lower", "pct_b", "width"), lambda p: _ew(max(p["n"], p["atr_n"])), "volatility"),
    "hist_vol": Spec(hist_vol, ("close",), {"n": 20}, lookback=lambda p: p["n"] + 1, group="volatility"),
    "adx": Spec(adx, ("high", "low", "close"), {"n": 14}, ("adx", "plus_di", "minus_di"),
                lambda p: _ew(p["n"]) * 2, "trend strength"),
    "aroon": Spec(aroon, ("high", "low"), {"n": 25}, ("up", "down", "osc"), lambda p: p["n"] + 1, "trend strength"),
    "volume_ratio": Spec(volume_ratio, ("volume",), {"n": 20}, lookback=lambda p: p["n"], group="volume"),
    "obv": Spec(obv, ("close", "volume"), {}, lookback=lambda p: 10 ** 9, group="volume"),
    "delivery_spike": Spec(delivery_spike, ("deliv_qty",), {"n": 20}, lookback=lambda p: p["n"], group="volume"),
    "highest": Spec(highest, ("high",), {"n": 20}, lookback=lambda p: p["n"], group="levels"),
    "lowest": Spec(lowest, ("low",), {"n": 20}, lookback=lambda p: p["n"], group="levels"),
}


def compute(name: str, panel, **params):
    """Compute a registered indicator from a panel (anything with the input fields
    as attributes or keys). Multi-output indicators return their dataclass."""
    spec = REGISTRY[name]
    p = {**spec.params, **params}
    get = (lambda f: panel[f]) if isinstance(panel, dict) else (lambda f: getattr(panel, f))
    return spec.fn(*[get(f) for f in spec.inputs], **p)
