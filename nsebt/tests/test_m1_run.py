"""M1.5: run record; same configuration + seed gives an identical run; frozen schema."""
import numpy as np
import pandas as pd
from _simkit import cfg, market, sig

from nsebt.backtest import run as R
from nsebt.backtest.costs import CostModel


def _mk():
    rng = np.random.default_rng(3)
    bars = {k: list(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 120)))) for k in "ABCDEFGH"}
    m = market(bars)
    D, N = m.close.shape
    e = rng.random((D, N)) < 0.08
    x = rng.random((D, N)) < 0.05
    s = sig(m, entries=list(zip(*np.nonzero(e))), exits=list(zip(*np.nonzero(x))),
            score=rng.random((D, N)), max_hold_days=10)
    return m, s


def test_same_config_and_seed_identical_run():
    m, s = _mk()
    a = R.run(m, s, cfg(costs=CostModel(), seed=7), strategy="t", params={"x": 1})
    b = R.run(m, s, cfg(costs=CostModel(), seed=7), strategy="t", params={"x": 1})
    assert a.record.config_hash == b.record.config_hash
    pd.testing.assert_frame_equal(a.result.daily, b.result.daily)
    pd.testing.assert_frame_equal(a.result.trades, b.result.trades)
    pd.testing.assert_frame_equal(a.result.rejected, b.result.rejected)
    assert a.record.n_trades > 5


def test_hash_changes_with_seed_params_account_and_costs():
    m, s = _mk()
    base = R.run(m, s, cfg(seed=1), params={"x": 1}).record.config_hash
    assert R.run(m, s, cfg(seed=2), params={"x": 1}).record.config_hash != base
    assert R.run(m, s, cfg(seed=1), params={"x": 2}).record.config_hash != base
    assert R.run(m, s, cfg(seed=1, capital=50_000), params={"x": 1}).record.config_hash != base
    assert R.run(m, s, cfg(seed=1, costs=CostModel()), params={"x": 1}).record.config_hash != base


def test_record_has_reproducibility_fields_and_is_json():
    import json
    m, s = _mk()
    rec = R.run(m, s, cfg()).record
    d = rec.to_dict()
    json.dumps(d)
    assert len(rec.git_commit) == 40 or rec.git_commit == "unknown"
    for k in ("git_commit", "git_dirty", "store_version", "config_hash", "seed", "account", "costs"):
        assert k in d


def test_schema_is_frozen():
    # changing these fields needs a new RUN_SCHEMA_VERSION and a ledger migration
    assert R.RUN_SCHEMA_VERSION == 2
    assert R.RUN_FIELDS == (
        "schema_version", "run_id", "created_utc", "strategy", "params", "account", "costs", "sim",
        "universe", "start", "end", "seed", "git_commit", "git_dirty", "store_version", "data_inputs",
        "signals_hash", "strategy_source_hash", "config_hash", "n_trades", "final_equity", "warnings")


def test_store_version_tracks_build_report(tmp_path):
    assert R.store_version(tmp_path) == "unknown"
    (tmp_path / "build_report.json").write_text('{"a": 1}')
    v1 = R.store_version(tmp_path)
    (tmp_path / "build_report.json").write_text('{"a": 2}')
    assert R.store_version(tmp_path) != v1


def test_hash_changes_with_signals_inputs_eligibility_and_strategy_source():
    m, s = _mk()
    base = R.run(m, s, cfg(seed=1)).record
    s2 = sig(m, entries=[(0, 0)])
    assert R.run(m, s2, cfg(seed=1)).record.config_hash != base.config_hash
    assert R.run(m, s2, cfg(seed=1)).record.signals_hash != base.signals_hash
    c = cfg(seed=1)
    c.eligibility = type(c.eligibility)(min_price=5.0, min_adv=0.0, adv_days=1)
    assert R.run(m, s, c).record.config_hash != base.config_hash
    assert R.run(m, s, cfg(seed=1), strategy_source="a: 1").record.config_hash != base.config_hash
    m.sources = {"mergers_csv": "abc"}
    r1 = R.run(m, s, cfg(seed=1)).record
    m.sources = {"mergers_csv": "abd"}
    assert R.run(m, s, cfg(seed=1)).record.config_hash != r1.config_hash
    assert r1.data_inputs == {"mergers_csv": "abc"}
