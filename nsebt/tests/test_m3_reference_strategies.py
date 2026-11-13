"""M3.5: the three reference strategies run end to end; YAML and Python momentum trade identically.

All runs end before the holdout (research.holdout_start); nothing here looks at performance.
"""
from pathlib import Path

import pandas as pd
import pytest

from nsebt.data import Panel
from nsebt.strategy import Context, get
from nsebt.strategy.spec import load

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "store"
STRATS = ROOT / "strategies"
pytestmark = pytest.mark.skipif(not (STORE / "lifecycle.parquet").exists(), reason="store not built")


@pytest.fixture(scope="module")
def env():
    from nsebt import config
    from nsebt.backtest import market as mk
    c = config.load(ROOT / "config.yaml")
    return c, mk.from_store(c)


def test_reference_files_load_without_lints():
    for f in ("macd_trend_pullback", "breakout", "momentum"):
        st = load(STRATS / f"{f}.yaml")
        assert st.name == f and st.lints() == []


def test_yaml_and_python_momentum_trade_identically(env):
    from nsebt.strategy.runner import backtest
    c, loaded = env
    a = backtest(c, load(STRATS / "momentum.yaml"), start="2017-10-01", end="2021-12-31", loaded=loaded,
                 use_cache=False, seed=11)
    b = backtest(c, get("momentum"), start="2017-10-01", end="2021-12-31", loaded=loaded, use_cache=False, seed=11)
    assert a.record.n_trades > 20
    pd.testing.assert_frame_equal(a.result.trades, b.result.trades)
    pd.testing.assert_frame_equal(a.result.daily, b.result.daily)
    pd.testing.assert_frame_equal(a.result.rejected, b.result.rejected)
    # the run records differ only in what defines the strategy (its source)
    assert a.record.signals_hash == b.record.signals_hash
    assert a.record.strategy_source_hash != b.record.strategy_source_hash


@pytest.mark.parametrize("name", ["macd_trend_pullback", "breakout"])
def test_yaml_reference_strategies_run_end_to_end(env, name):
    from nsebt.strategy.runner import backtest
    c, loaded = env
    r = backtest(c, load(STRATS / f"{name}.yaml"), start="2017-01-01", end="2021-12-31", loaded=loaded,
                 use_cache=False)
    rec, res = r.record, r.result
    assert rec.n_trades > 0 and rec.end <= "2021-12-31" and rec.strategy == name
    assert set(res.trades["exit_reason"]) <= {"exit rule", "max hold", "stop", "target", "trailing stop",
                                              "merger exit", "corporate action"} | \
        {x for x in res.trades["exit_reason"] if str(x).endswith("-day grace)")}
    assert (res.daily["cash"] > -1e-6).all()


def test_strategy_frames_use_no_future_data_on_real_data(env):
    _, (m, panel) = env
    cut_day = pd.Timestamp("2019-06-28")
    cut = Panel({k: v.loc[:cut_day] for k, v in panel.fields.items()}, panel.members.loc[:cut_day],
                panel.securities, panel.indices)
    for st in (load(STRATS / "momentum.yaml"), load(STRATS / "breakout.yaml"), load(STRATS / "macd_trend_pullback.yaml")):
        full = st.generate(Context(panel, st.params))
        part = st.generate(Context(cut, st.params))
        for k in ("entry", "exit", "warmup"):
            pd.testing.assert_frame_equal(getattr(part, k), getattr(full, k).loc[:cut_day], obj=f"{st.name}.{k}")
