"""Check every indicator against TA-Lib on every security of a panel, and time them.

    nsebt check-indicators          -> reports/phase3/reference_check.csv, timings.csv
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import indicators as I

MACD_EXT = dict(fastperiod=12, fastmatype=1, slowperiod=26, slowmatype=1, signalperiod=9, signalmatype=1)


def _cases(talib):
    # (label, our function on the panel, TA-Lib function, inputs, output index, kwargs, scale, note)
    return [
        ("SMA(50)", lambda p: I.sma(p.close, 50), talib.SMA, ("close",), None, {"timeperiod": 50}, 1, ""),
        ("SMA(200)", lambda p: I.sma(p.close, 200), talib.SMA, ("close",), None, {"timeperiod": 200}, 1, ""),
        ("EMA(20)", lambda p: I.ema(p.close, 20), talib.EMA, ("close",), None, {"timeperiod": 20}, 1, ""),
        ("EMA(200)", lambda p: I.ema(p.close, 200), talib.EMA, ("close",), None, {"timeperiod": 200}, 1, ""),
        ("MACD line", lambda p: I.macd(p.close).line, talib.MACDEXT, ("close",), 0, MACD_EXT, 1,
         "vs MACDEXT; TA-Lib's MACD() rounds alpha to 0.15/0.075"),
        ("MACD signal", lambda p: I.macd(p.close).signal, talib.MACDEXT, ("close",), 1, MACD_EXT, 1, ""),
        ("MACD hist", lambda p: I.macd(p.close).hist, talib.MACDEXT, ("close",), 2, MACD_EXT, 1, ""),
        ("RSI(14)", lambda p: I.rsi(p.close, 14), talib.RSI, ("close",), None, {"timeperiod": 14}, 1, ""),
        ("Stoch %K(14)", lambda p: I.stochastic(p.high, p.low, p.close).k, talib.STOCHF, ("high", "low", "close"), 0,
         dict(fastk_period=14, fastd_period=3), 1, "TA-Lib withholds %K until %D exists (2 rows)"),
        ("Stoch %D(3)", lambda p: I.stochastic(p.high, p.low, p.close).d, talib.STOCHF, ("high", "low", "close"), 1,
         dict(fastk_period=14, fastd_period=3), 1, ""),
        ("ROC(20)", lambda p: I.roc(p.close, 20), talib.ROC, ("close",), None, {"timeperiod": 20}, 100, "ours is a fraction"),
        ("ATR(14)", lambda p: I.atr(p.high, p.low, p.close, 14), talib.ATR, ("high", "low", "close"), None, {"timeperiod": 14}, 1, ""),
        ("Bollinger upper", lambda p: I.bollinger(p.close).upper, talib.BBANDS, ("close",), 0,
         dict(timeperiod=20, nbdevup=2, nbdevdn=2), 1, "population std"),
        ("Bollinger lower", lambda p: I.bollinger(p.close).lower, talib.BBANDS, ("close",), 2,
         dict(timeperiod=20, nbdevup=2, nbdevdn=2), 1, ""),
        ("ADX(14)", lambda p: I.adx(p.high, p.low, p.close).adx, talib.ADX, ("high", "low", "close"), None, {"timeperiod": 14}, 1, ""),
        ("+DI(14)", lambda p: I.adx(p.high, p.low, p.close).plus_di, talib.PLUS_DI, ("high", "low", "close"), None, {"timeperiod": 14}, 1, ""),
        ("-DI(14)", lambda p: I.adx(p.high, p.low, p.close).minus_di, talib.MINUS_DI, ("high", "low", "close"), None, {"timeperiod": 14}, 1, ""),
        ("Aroon up(25)", lambda p: I.aroon(p.high, p.low).up, talib.AROON, ("high", "low"), 1, {"timeperiod": 25}, 1, ""),
        ("Aroon down(25)", lambda p: I.aroon(p.high, p.low).down, talib.AROON, ("high", "low"), 0, {"timeperiod": 25}, 1, ""),
        ("OBV", lambda p: I.obv(p.close, p.volume), talib.OBV, ("close", "volume"), None, {}, 1, ""),
        ("True range", lambda p: I.true_range(p.high, p.low, p.close), talib.TRANGE, ("high", "low", "close"), None, {}, 1, ""),
        ("Highest high(252)", lambda p: I.highest(p.high, 252), talib.MAX, ("high",), None, {"timeperiod": 252}, 1, ""),
        ("Lowest low(252)", lambda p: I.lowest(p.low, 252), talib.MIN, ("low",), None, {"timeperiod": 252}, 1, ""),
    ]


def run(panel, out_dir: Path) -> pd.DataFrame:
    import talib
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for label, ours_fn, fn, inputs, k, kw, scale, note in _cases(talib):
        t0 = time.perf_counter()
        ours = ours_fn(panel) * scale
        t_ours = time.perf_counter() - t0
        diffs, nan_mis, n, nan_only_ours = [], 0, 0, 0
        for c in panel.close.columns:
            rows_ = panel.close[c].notna()
            if rows_.sum() < 3:
                continue
            args = [getattr(panel, f)[c][rows_].to_numpy(float) for f in inputs]
            r = fn(*args, **kw)
            r = r[k] if k is not None else r
            o = ours[c][rows_].to_numpy(float)
            both = ~np.isnan(o) & ~np.isnan(r)
            n += int(both.sum())
            nan_mis += int((np.isnan(o) != np.isnan(r)).sum())
            nan_only_ours += int((~np.isnan(o) & np.isnan(r)).sum())
            if both.any():
                diffs.append(np.max(np.abs(o[both] - r[both]) / np.maximum(np.abs(r[both]), 1)))
        rows.append({"indicator": label, "values_compared": n, "max_rel_diff": max(diffs) if diffs else np.nan,
                     "first_valid_mismatches": nan_mis, "of_which_ours_earlier": nan_only_ours,
                     "seconds_all_securities": round(t_ours, 2), "note": note})
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "reference_check.csv", index=False)
    return df


def time_library(panel) -> pd.DataFrame:
    rows = []
    for name, spec in I.REGISTRY.items():
        t0 = time.perf_counter()
        I.compute(name, panel)
        rows.append({"indicator": name, "seconds": round(time.perf_counter() - t0, 2)})
    return pd.DataFrame(rows)
