"""M3.1: the Strategy interface, registry, label alignment, Market.slice and the runner."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from _simkit import cfg
from nsebt.backtest.run import run
from nsebt.backtest.sim import Market, Sizing
from nsebt.data import Panel
from nsebt.strategy import Context, Output, Strategy, rank_desc, rebalance_days, register, to_signals

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "store"


def toy_panel(n=120, ids=("A", "B", "C", "D"), seed=0):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2020-01-01", periods=n)
    c = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.02, (n, len(ids))), axis=0)), index=days, columns=list(ids))
    f = {"close": c, "open": c.shift(1).fillna(c), "high": c * 1.01, "low": c * 0.99, "volume": c * 0 + 1e5,
         "raw_close": c, "raw_open": c.shift(1).fillna(c), "raw_high": c * 1.01, "raw_low": c * 0.99,
         "turnover": c * 0 + 1e12}
    mem = pd.DataFrame(True, index=days, columns=list(ids))
    return Panel(f, mem, pd.DataFrame({"SecurityID": list(ids)}), pd.DataFrame(columns=["Date", "Index", "Close"]))


class SmaCross(Strategy):
    name = "test_sma_cross"
    defaults = {"n": 20}

    def generate(self, ctx: Context) -> Output:
        c = ctx.panel.close
        s = c.rolling(self.params["n"]).mean()
        return Output(entry=(c > s) & (c.shift(1) <= s.shift(1)), exit=c < s, score=c / s,
                      warmup=s.notna().cummax(), stop=c * 0 + 0.08)


def test_python_strategy_runs_end_to_end():
    p = toy_panel()
    m = Market.from_panel(p, adv_days=1)
    st = SmaCross(n=10)
    out = st.generate(Context(p, st.params))
    r = run(m, to_signals(out, m), cfg(), strategy=st.name, params=st.params, strategy_source=st.source())
    assert r.record.n_trades > 0 and r.record.strategy_source_hash != "none"
    assert r.record.params == {"n": 10}


def test_unknown_parameter_is_rejected():
    with pytest.raises(ValueError, match="unknown parameter"):
        SmaCross(m=3)


def test_signals_align_by_label_not_position():
    p = toy_panel()
    m = Market.from_panel(p, adv_days=1)
    entry = pd.DataFrame(False, index=p.close.index, columns=p.close.columns)
    entry.loc[entry.index[5], "C"] = True
    shuffled = entry[["D", "C", "B", "A"]]                     # columns in a different order
    sig = to_signals(Output(entry=shuffled), m)
    assert sig.entry[5, list(m.ids).index("C")] and sig.entry.sum() == 1
    assert sig.ids.equals(m.ids) and sig.dates.equals(m.dates)


def test_rank_and_rebalance_helpers():
    x = pd.DataFrame({"A": [3.0, 1], "B": [2.0, 2], "C": [1.0, np.nan]}, index=pd.bdate_range("2020-01-30", periods=2))
    mask = pd.DataFrame(True, index=x.index, columns=x.columns)
    mask.iloc[0, 1] = False
    r = rank_desc(x, mask)
    assert r.iloc[0].tolist()[0] == 1 and np.isnan(r.iloc[0, 1]) and r.iloc[0, 2] == 2
    days = pd.bdate_range("2020-01-27", "2020-03-03")
    rb = rebalance_days(days, "monthly")
    assert list(rb[rb].index.strftime("%Y-%m-%d")) == ["2020-01-31", "2020-02-28"]
    assert rebalance_days(pd.bdate_range("2020-02-24", "2020-02-28"), "monthly").iloc[-1]   # Friday 28th ends Feb
    assert rebalance_days(days, "daily").all()
    with pytest.raises(ValueError):
        rebalance_days(days, "yearly")


def test_register_rejects_duplicates_and_non_strategies():
    register(SmaCross)
    register(SmaCross)                                          # same class again is fine
    with pytest.raises(ValueError, match="already registered"):
        register(type("SmaCross", (Strategy,), {"name": "test_sma_cross"}))
    with pytest.raises(TypeError):
        register(object)


def test_strategy_frames_use_no_future_data():
    p = toy_panel(n=150)
    full = SmaCross(n=15).generate(Context(p, {"n": 15}))
    cut = toy_panel(n=150)
    for k, v in cut.fields.items():
        cut.fields[k] = v.iloc[:90]
    part = SmaCross(n=15).generate(Context(cut, {"n": 15}))
    pd.testing.assert_frame_equal(part.entry, full.entry.iloc[:90])
    pd.testing.assert_frame_equal(part.exit, full.exit.iloc[:90])


# --------------------------------------------------------------------------- Market.slice

def test_market_slice_keeps_liquidity_history():
    p = toy_panel()
    m = Market.from_panel(p, adv_days=20)
    w = m.slice("2020-03-02", "2020-04-30")
    assert w.dates[0] == pd.Timestamp("2020-03-02") and w.dates[-1] == pd.Timestamp("2020-04-30")
    assert np.isfinite(w.adv[0]).all() and np.isfinite(w.prev_close[0]).all()
    assert w.members.shape == w.close.shape
    with pytest.raises(ValueError, match="2 trading days"):
        m.slice("2020-03-02", "2020-03-02")


# --------------------------------------------------------------------------- real store

@pytest.mark.skipif(not (STORE / "lifecycle.parquet").exists(), reason="store not built")
def test_runner_on_the_store_and_holdout_lock():
    from nsebt import config
    from nsebt.backtest import market as mk
    from nsebt.strategy.runner import backtest
    c = config.load(ROOT / "config.yaml")
    loaded = mk.from_store(c)
    st = SmaCross(n=50)
    r = backtest(c, st, start="2018-01-01", end="2019-12-31", loaded=loaded, use_cache=False)
    rec = r.record
    assert rec.start == "2018-01-01" and rec.end <= "2019-12-31" and rec.n_trades > 0
    assert rec.data_inputs["mergers_csv"] != "missing" and rec.sim["eligibility"]["min_price"] == 10.0
    with pytest.raises(PermissionError, match="holdout"):
        backtest(c, st, start="2021-01-01", loaded=loaded, use_cache=False)
