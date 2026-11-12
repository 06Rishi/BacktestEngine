"""Phase 3: indicators vs TA-Lib on real data, no look-ahead for every indicator,
gap handling, and the feature cache."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nsebt import indicators as I
from nsebt.features import FeatureCache

FX = Path(__file__).parent / "fixtures"
talib = pytest.importorskip("talib")


@pytest.fixture(scope="module")
def panel():
    df = pd.read_parquet(FX / "panel_sample.parquet")
    return {f: df.pivot(index="Date", columns="SecurityID", values=f).sort_index()
            for f in ("open", "high", "low", "close", "volume", "deliv_qty", "raw_close")}


def _ref(fn, panel, inputs, out=None, **kw):
    """TA-Lib run per security on its own trading days."""
    cols = {}
    for c in panel["close"].columns:
        rows = panel["close"][c].notna()
        args = [panel[f][c][rows].to_numpy(float) for f in inputs]
        r = fn(*args, **kw)
        r = r[out] if out is not None else r
        cols[c] = pd.Series(r, index=panel["close"].index[rows])
    return pd.DataFrame(cols).reindex(panel["close"].index)


def _same(ours, ref, tol=1e-9, both_only=False):
    m = ours.notna() & ref.notna()
    if not both_only:
        assert (ours.isna() == ref.isna()).all().all(), "different first valid row"
    d = (ours[m] - ref[m]).abs() / ref[m].abs().clip(lower=1)
    assert np.nanmax(d.to_numpy()) < tol


CASES = [
    ("sma", lambda p: I.sma(p["close"], 50), talib.SMA, ("close",), None, {"timeperiod": 50}),
    ("ema", lambda p: I.ema(p["close"], 20), talib.EMA, ("close",), None, {"timeperiod": 20}),
    ("rsi", lambda p: I.rsi(p["close"], 14), talib.RSI, ("close",), None, {"timeperiod": 14}),
    ("atr", lambda p: I.atr(p["high"], p["low"], p["close"], 14), talib.ATR, ("high", "low", "close"), None, {"timeperiod": 14}),
    ("adx", lambda p: I.adx(p["high"], p["low"], p["close"], 14).adx, talib.ADX, ("high", "low", "close"), None, {"timeperiod": 14}),
    ("plus_di", lambda p: I.adx(p["high"], p["low"], p["close"], 14).plus_di, talib.PLUS_DI, ("high", "low", "close"), None, {"timeperiod": 14}),
    ("minus_di", lambda p: I.adx(p["high"], p["low"], p["close"], 14).minus_di, talib.MINUS_DI, ("high", "low", "close"), None, {"timeperiod": 14}),
    ("macd_line", lambda p: I.macd(p["close"]).line, talib.MACDEXT, ("close",), 0,
     dict(fastperiod=12, fastmatype=1, slowperiod=26, slowmatype=1, signalperiod=9, signalmatype=1)),
    ("macd_signal", lambda p: I.macd(p["close"]).signal, talib.MACDEXT, ("close",), 1,
     dict(fastperiod=12, fastmatype=1, slowperiod=26, slowmatype=1, signalperiod=9, signalmatype=1)),
    ("macd_hist", lambda p: I.macd(p["close"]).hist, talib.MACDEXT, ("close",), 2,
     dict(fastperiod=12, fastmatype=1, slowperiod=26, slowmatype=1, signalperiod=9, signalmatype=1)),
    ("bb_upper", lambda p: I.bollinger(p["close"]).upper, talib.BBANDS, ("close",), 0, dict(timeperiod=20, nbdevup=2, nbdevdn=2)),
    ("bb_lower", lambda p: I.bollinger(p["close"]).lower, talib.BBANDS, ("close",), 2, dict(timeperiod=20, nbdevup=2, nbdevdn=2)),
    ("aroon_up", lambda p: I.aroon(p["high"], p["low"], 25).up, talib.AROON, ("high", "low"), 1, {"timeperiod": 25}),
    ("aroon_down", lambda p: I.aroon(p["high"], p["low"], 25).down, talib.AROON, ("high", "low"), 0, {"timeperiod": 25}),
    ("obv", lambda p: I.obv(p["close"], p["volume"]), talib.OBV, ("close", "volume"), None, {}),
    ("roc", lambda p: I.roc(p["close"], 20) * 100, talib.ROC, ("close",), None, {"timeperiod": 20}),
    ("trange", lambda p: I.true_range(p["high"], p["low"], p["close"]), talib.TRANGE, ("high", "low", "close"), None, {}),
    ("max", lambda p: I.highest(p["high"], 20), talib.MAX, ("high",), None, {"timeperiod": 20}),
    ("min", lambda p: I.lowest(p["low"], 20), talib.MIN, ("low",), None, {"timeperiod": 20}),
]


@pytest.mark.parametrize("name,ours,fn,inputs,out,kw", CASES, ids=[c[0] for c in CASES])
def test_matches_talib(panel, name, ours, fn, inputs, out, kw):
    _same(ours(panel), _ref(fn, panel, inputs, out, **kw))


def test_stochastic_matches_talib_where_both_report(panel):
    # TA-Lib withholds %K until %D exists (2 extra rows); values are identical
    s = I.stochastic(panel["high"], panel["low"], panel["close"], 14, 3)
    _same(s.k, _ref(talib.STOCHF, panel, ("high", "low", "close"), 0, fastk_period=14, fastd_period=3), both_only=True)
    _same(s.d, _ref(talib.STOCHF, panel, ("high", "low", "close"), 1, fastk_period=14, fastd_period=3))


def test_hist_vol_definition(panel):
    c = panel["close"]["INE002A01018"]
    want = np.log(c).diff().rolling(20).std() * np.sqrt(252)
    pd.testing.assert_series_equal(I.hist_vol(panel["close"], 20)["INE002A01018"], want, check_names=False)


# --------------------------------------------------------------------------- no look-ahead

def _all_outputs(panel):
    out = {}
    for name, spec in I.REGISTRY.items():
        r = I.compute(name, panel)
        for o in (spec.outputs or ("value",)):
            out[f"{name}.{o}"] = r if o == "value" else getattr(r, o)
    c = panel["close"]
    bench = c.mean(axis=1)
    out["relative_strength"] = I.relative_strength(c, bench, 63)
    out["rank"] = I.rank(I.roc(c, 20))
    out["cross_up"] = I.cross_up(I.sma(c, 10), I.sma(c, 30)).astype(float)
    out["gap"] = I.gap(panel["open"], c)
    out["momentum"] = I.momentum_12_1(c, 120, 10)
    out["keltner.mid"] = I.keltner(panel["high"], panel["low"], c).mid
    out["delivery_spike"] = I.delivery_spike(panel["deliv_qty"], 20)
    return out


@pytest.mark.parametrize("cut", [60, 300, 600])
def test_no_lookahead_any_indicator(panel, cut):
    full = _all_outputs(panel)
    t = panel["close"].index[cut]
    trunc = _all_outputs({k: v.loc[:t] for k, v in panel.items()})
    for k, f in full.items():
        pd.testing.assert_frame_equal(trunc[k], f.loc[:t], check_exact=False, rtol=1e-12, atol=1e-12, obj=k)


def test_gap_is_computed_on_own_trading_days(panel):
    gappy = I._gappy_columns(panel["close"])
    assert gappy, "fixture should contain a suspended stock"
    c = gappy[0]
    own = panel["close"][c].dropna()
    full = I.sma(panel["close"], 20)[c].dropna()
    pd.testing.assert_series_equal(full, own.rolling(20).mean().dropna(), check_names=False)


# --------------------------------------------------------------------------- cache

def test_cache_full_then_incremental_then_cached(panel, tmp_path):
    cut = panel["close"].index[500]
    early = {k: v.loc[:cut] for k, v in panel.items()}
    fc = FeatureCache(tmp_path, early)
    fc.get("rsi", n=14)
    m0 = fc.get("macd")
    assert fc.last_mode == "full"
    fc2 = FeatureCache(tmp_path, panel)
    r = fc2.get("rsi", n=14)
    assert fc2.last_mode == "incremental"
    pd.testing.assert_frame_equal(r, I.rsi(panel["close"], 14), check_exact=False, rtol=1e-9, atol=1e-9,
                                  check_freq=False, check_names=False)
    m = fc2.get("macd")
    # MACD needs ~875 rows of history for an exact restart; with 500 cached rows the
    # cache must fall back to a full recompute
    assert fc2.last_mode == "full"
    pd.testing.assert_frame_equal(m.hist, I.macd(panel["close"]).hist, check_exact=False, rtol=1e-8, atol=1e-8,
                                  check_freq=False, check_names=False)
    fc2.get("rsi", n=14)
    assert fc2.last_mode == "cached"
    assert m0.line.shape[0] == 501


def test_cache_recomputes_when_history_changes(panel, tmp_path):
    fc = FeatureCache(tmp_path, panel)
    fc.get("sma", n=20)
    changed = {k: v.copy() for k, v in panel.items()}
    changed["close"].iloc[:100, 0] *= 0.5          # e.g. a new split re-bases adjusted history
    fc2 = FeatureCache(tmp_path, changed)
    r = fc2.get("sma", n=20)
    assert fc2.last_mode == "full"
    pd.testing.assert_frame_equal(r, I.sma(changed["close"], 20), check_freq=False, check_names=False)
