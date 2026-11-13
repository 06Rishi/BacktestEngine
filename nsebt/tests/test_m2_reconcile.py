"""M2.6: ledger-vs-adjusted reconciliation (2.6), no look-ahead in the engine, speed."""
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from _simkit import ca, market, sig
from nsebt.backtest import reconcile as rc
from nsebt.backtest.account import Account
from nsebt.backtest.costs import CostModel
from nsebt.backtest.sim import Market, SimConfig, Signals, Sizing, simulate
from nsebt.nsestore import PRICE_KINDS, adjust

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "store"
TR_KINDS = PRICE_KINDS + ("dividend",)

EVENTS = [  # (security, day, kind, factor, detail, prior_close)
    ("A", 5, "split", 0.2, "FV 10->2"), ("A", 9, "dividend", 0.98, "Rs 2", 100.0),
    ("A", 9, "bonus", 0.5, "1:1"), ("B", 4, "bonus", 2 / 3, "1:2"), ("B", 7, "rights", 0.95, "1:5 @ 50", 100.0),
    ("C", 6, "arrangement", 0.6, "Demerger", 100.0), ("C", 8, "special_dividend", 0.9, "Rs 10", 100.0),
    ("C", 8, "special_dividend", 0.97, "Rs 3", 100.0), ("D", 3, "consolidation", 10.0, "FV 1->10"),
    ("D", 10, "inferred_split", 0.1, "split 1:10"),
]


def synthetic(seed=1, n=14):
    rng = np.random.default_rng(seed)
    bars = {s: list(100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))) for s in "ABCDE"}
    bars["E"][:3] = [None] * 3                     # listed on day 3
    m = market(bars)
    acts = ca(m, EVENTS)
    # the corporate-action table's prior close is the security's previous raw close
    acts["prior_close"] = [m.prev_close[m.dates.get_loc(d), list(m.ids).index(sid)]
                           for sid, d in zip(acts["SecurityID"], acts["effective_date"])]
    m.actions = acts
    px = pd.DataFrame({"Date": np.repeat(m.dates, len(m.ids)), "SecurityID": np.tile(m.ids, len(m.dates)),
                       "Close": m.close.ravel(), "Open": m.open.ravel(), "Volume": 1.0}).dropna(subset=["Close"])
    adj = adjust(px, acts, TR_KINDS).pivot(index="Date", columns="SecurityID", values="AdjClose")
    return m, adj.reindex(index=m.dates, columns=m.ids).to_numpy(float)


def test_reconciles_every_event_kind_to_1e6():
    m, adj = synthetic()
    r = rc.reconcile(m, adj)
    assert r.n == 5 and r.n_failed == 0 and r.passed
    assert r.summary["max_abs_err"].max() < 1e-12
    assert len(r.preservation) == 5 and r.b_failed == 0      # dividend, rights, demerger, 2 special dividends


def test_detects_a_missed_event():
    m, adj = synthetic()
    m.actions = m.actions[~((m.actions["SecurityID"] == "B") & (m.actions["kind"] == "rights"))]
    r = rc.reconcile(m, adj)
    f = r.failures
    assert r.n_failed == 1 and list(f["SecurityID"]) == ["B"] and f["to"].iloc[0] == m.dates[7]


def test_detects_a_reversed_factor():
    m, adj = synthetic()
    a = m.actions.copy()
    k = (a["SecurityID"] == "A") & (a["kind"] == "split")
    a.loc[k, "factor"] = 1 / a.loc[k, "factor"]
    m.actions = a
    r = rc.reconcile(m, adj)
    assert r.n_failed == 1 and r.failures["to"].iloc[0] == m.dates[5]
    assert "split" in r.failures["events"].iloc[0]


def test_detects_a_wrong_effective_date():
    m, adj = synthetic()
    a = m.actions.copy()
    k = (a["SecurityID"] == "C") & (a["kind"] == "arrangement")
    a.loc[k, "effective_date"] = m.dates[7]
    m.actions = a
    assert rc.reconcile(m, adj).n_failed == 1


def test_preservation_detects_amount_on_a_different_close():
    m, adj = synthetic()
    a = m.actions.copy()
    a.loc[(a["SecurityID"] == "A") & (a["kind"] == "dividend"), "prior_close"] *= 1.2
    m.actions = a
    r = rc.reconcile(m, adj)
    assert r.b_failed == 1 and not r.passed


# --------------------------------------------------------------------------- no look-ahead

def random_case(seed, D=120, N=15):
    rng = np.random.default_rng(seed)
    ids = [f"S{j}" for j in range(N)]
    days = pd.bdate_range("2023-01-02", periods=D)
    c = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.025, (D, N)), axis=0)), index=days, columns=ids)
    o = c.shift(1).fillna(c) * np.exp(rng.normal(0, 0.01, (D, N)))
    h = np.maximum(o, c) * (1 + rng.uniform(0, 0.02, (D, N)))
    l = np.minimum(o, c) * (1 - rng.uniform(0, 0.02, (D, N)))
    c[rng.random((D, N)) < 0.02] = np.nan                              # some days not traded
    tv = pd.DataFrame(rng.uniform(5e6, 5e7, (D, N)), index=days, columns=ids)
    acts = pd.DataFrame({"SecurityID": rng.choice(ids, 25), "kind": rng.choice(
        ["split", "bonus", "dividend", "rights", "arrangement"], 25), "applied": True})
    acts["effective_date"] = days[rng.integers(1, D, 25)]
    acts["ex_date"] = acts["effective_date"]
    acts["factor"] = np.where(acts["kind"].isin(["split", "bonus"]), 0.5, rng.uniform(0.9, 0.99, 25))
    acts["prior_close"], acts["detail"] = 100.0, "1:4 @ 50"
    sig_ = dict(entry=rng.random((D, N)) < 0.08, exit=rng.random((D, N)) < 0.03, score=rng.random((D, N)),
                stop=np.full((D, N), 0.08), target=np.full((D, N), 0.15), trail=np.full((D, N), 0.10))
    return days, (o, h, l, c, tv), acts, sig_


def run_upto(case, T):
    days, (o, h, l, c, tv), acts, s = case
    cut = lambda f: f.iloc[:T + 1]
    m = Market.from_frames(cut(o), cut(h), cut(l), cut(c), cut(tv), actions=acts)
    sg = Signals(**{k: v[:T + 1] for k, v in s.items()}, max_hold_days=15, sizing=Sizing("equal"))
    cfg = SimConfig(account=Account(capital=500_000, max_positions=4, max_position_pct=30,
                                    max_participation_pct=2), costs=CostModel(), seed=7)
    return simulate(m, sg, cfg)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_truncating_the_future_changes_nothing_before_it(seed):
    case = random_case(seed)
    full = run_upto(case, 119)
    assert len(full.trades) > 10
    for T in (40, 75, 100):
        part = run_upto(case, T)
        pd.testing.assert_frame_equal(part.daily, full.daily.iloc[:T + 1])
        done = full.trades[full.trades["exit_date"] <= case[0][T]].reset_index(drop=True)
        pd.testing.assert_frame_equal(part.trades.reset_index(drop=True), done)
        rej = full.rejected[full.rejected["date"] < case[0][T]].reset_index(drop=True)
        pd.testing.assert_frame_equal(part.rejected[part.rejected["date"] < case[0][T]].reset_index(drop=True), rej)


# --------------------------------------------------------------------------- real data

@pytest.mark.skipif(not (STORE / "prices").exists(), reason="store not built")
def test_real_store_reconciliation_and_speed():
    # a sample with every event kind; the full Nifty 500 run is `nsebt reconcile`
    ids = ["INE002A01018", "INE003A01024", "INE008I01026", "INE005B01027", "INE007B01023",
           "INE00IK01011", "IN9175A01010", "INE040A01034", "INE040A01026"]
    t0 = time.perf_counter()
    m, adj, _ = rc.from_store(STORE, universe=ids)
    r = rc.reconcile(m, adj)
    assert r.n >= 8 and r.n_failed == 0 and r.b_failed == 0
    kinds = set(r.preservation["kind"])
    assert {"dividend", "rights", "arrangement"} <= kinds
    D, N = m.close.shape
    rng = np.random.default_rng(0)
    t1 = time.perf_counter()
    simulate(m, Signals(entry=rng.random((D, N)) < 0.05, exit=rng.random((D, N)) < 0.02),
             SimConfig(account=Account(capital=36_000, max_positions=3, max_position_pct=33)))
    assert time.perf_counter() - t1 < 30 and time.perf_counter() - t0 < 60


# --------------------------------------------------------------------------- (c) table vs prices, (d) full ledger

def test_table_check_flags_a_factor_the_prices_contradict():
    ca = pd.DataFrame({"SecurityID": ["A", "B"], "Symbol": ["A", "B"], "kind": ["rights", "bonus"],
                       "factor": [1.0, 0.5], "applied": True, "effective_date": pd.to_datetime(["2020-01-02"] * 2),
                       "prior_close": [29.0, 100.0], "ex_open": [17.95, 50.5], "ex_close": [18.25, 49.0]})
    bad, n = rc.check_table(ca, ["A", "B"])
    assert n == 2 and list(bad["Symbol"]) == ["A"]          # the IDEA-like rights row with factor 1.0


def test_full_ledger_run_identity_holds():
    m, _ = synthetic()
    r, err, min_cash = rc.full_ledger_run(m, list(m.ids))
    assert abs(err) < 1e-6 and min_cash > -1e-9 and len(r.ledger) >= 8


@pytest.mark.skipif(not (STORE / "lifecycle.parquet").exists(), reason="store not built")
def test_market_from_store_loads_everything_and_hashes_inputs():
    from nsebt import config
    from nsebt.backtest import market as mk
    cfg_ = config.load(ROOT / "config.yaml")
    m, panel = mk.from_store(cfg_, universe="nifty500", start="2023-06-01", end="2023-08-31")
    assert m.mergers is not None and m.actions is not None and m.lifecycle is not None
    assert set(m.sources) >= {"corporate_actions", "mergers_csv", "lifecycle", "lifecycle_overrides_csv",
                              "ca_overrides_csv", "build_report"}
    assert all(v != "missing" for v in m.sources.values())
    assert panel.close.columns.equals(m.ids) and panel.close.index.equals(m.dates)
    acq = set(m.mergers["acquirer"]) - set(pd.read_parquet(STORE / "membership.parquet")["SecurityID"])
    j = [list(m.ids).index(a) for a in acq if a in m.ids]
    assert j and not m.members[:, j].any()                  # acquirers loaded, never members
