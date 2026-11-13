"""Gate A review: regression tests for the engine bugs found by the independent review, plus
engine-enforced eligibility (A4) and an accounting identity over random scenarios."""
import numpy as np
import pandas as pd
import pytest

from _simkit import ca, cfg, market, sig
from nsebt.backtest.account import Account
from nsebt.backtest.costs import CostModel
from nsebt.backtest.eligibility import EligibilityConfig
from nsebt.backtest.ledger import LedgerConfig
from nsebt.backtest.sim import Market, SimConfig, Signals, Simulator, Sizing


def merge(m, t, parent, parts, cash=0.0):
    m.mergers = pd.DataFrame({"parent": [parent] * len(parts), "effective_date": [m.dates[t]] * len(parts),
                              "acquirer": [a for a, _ in parts], "ratio": [r for _, r in parts],
                              "cash": [cash] + [0.0] * (len(parts) - 1)})
    return m


def go(m, entries, exits=(), merger="convert_and_hold", value=1000.0, **kw):
    c = cfg()
    c.ledger = LedgerConfig(merger=merger, **kw)
    sim = Simulator(m, sig(m, entries=list(entries), exits=list(exits), sizing=Sizing("fixed", value)), c)
    return sim, sim.run()


# --------------------------------------------------------------------------- bug 1: buy fill overwrote a holding

def test_buy_fill_never_overwrites_shares_received_in_a_merger():
    # hold A (10 x 100); a B order is placed at the day-3 close; A converts 1:1 into B on day 4
    m = merge(market({"A": [100] * 4 + [None] * 4, "B": [100] * 8}), 4, "A", [("B", 1.0)])
    sim, r = go(m, entries=[(0, 0), (3, 1)])
    assert sim.positions[1].shares == 10
    assert r.daily["equity"].iloc[-1] == pytest.approx(30_000)
    assert (r.rejected["reason"] == "held").sum() == 1


# --------------------------------------------------------------------------- bug 2: side sells missed corporate actions

def side_sell_market(event):
    # hold A and B; A converts into B on day 4 while B doesn't trade -> 10 B wait as a pending sale;
    # B has `event` on day 5
    b = [100, 100, 100, 100, None] + [event[1]] * 4
    m = merge(market({"A": [100] * 4 + [None] * 5, "B": b}), 4, "A", [("B", 1.0)])
    m.actions = ca(m, [("B", 5) + event[2:]])
    return m


def test_pending_merged_shares_get_the_acquirers_split():
    m = side_sell_market(("split", 20, "split", 0.2, "FV 10->2"))
    sim, r = go(m, entries=[(0, 0), (0, 1)], merger="convert_then_sell")
    t = r.trades.set_index("SecurityID").loc["A"]
    assert t["shares"] == 50 and t["exit_price"] == 20 and t["gross_pnl"] == pytest.approx(0)
    assert r.daily["equity"].iloc[-1] == pytest.approx(30_000)


def test_pending_merged_shares_get_the_acquirers_dividend():
    m = side_sell_market(("dividend", 90, "dividend", 0.9, "Rs 10", 100.0))
    sim, r = go(m, entries=[(0, 0), (0, 1)], merger="convert_then_sell")
    assert r.daily["receivables"].iloc[5] == pytest.approx(200)          # 10 held B + 10 pending B
    assert r.daily["equity"].iloc[5] == pytest.approx(30_000)


# --------------------------------------------------------------------------- bug 3: merger into an acquirer delisted that day

def test_shares_converted_into_an_acquirer_delisted_the_same_day_exit():
    m = merge(market({"A": [100] * 4 + [None] * 4, "B": [100] * 3 + [None] * 5}), 4, "A", [("B", 1.0)])
    m.lifecycle = pd.DataFrame({"SecurityID": ["B"], "Symbol": ["B"], "last_trade": [m.dates[2]],
                                "effective_date": [m.dates[3]], "cls": ["delisted"], "source": ["t"],
                                "list_date": [pd.NaT], "detail": [""], "value": [np.nan]})
    sim, r = go(m, entries=[(0, 0)], grace_days=1)                      # B's exit falls on day 4
    assert not sim.positions
    t = r.trades.iloc[0]
    assert t["exit_reason"].startswith("delisted") and t["exit_price"] == pytest.approx(50)


# --------------------------------------------------------------------------- bug 4: unlisted component overvalued

def test_component_that_lists_later_is_valued_from_the_rest_of_the_parent():
    # A (100) -> 1 B (80) + 0.2 C (C lists on day 7 at 100): C is worth 100 - 80 = 20 per A share
    m = merge(market({"A": [100] * 4 + [None] * 5, "B": [80] * 9, "C": [None] * 7 + [100] * 2}), 4, "A",
              [("B", 1.0), ("C", 0.2)])
    sim, r = go(m, entries=[(0, 0)])
    assert sim.positions[2].shares == 2
    d = r.daily["equity"]
    assert d.iloc[4] == pytest.approx(d.iloc[3]) and d.iloc[6] == pytest.approx(30_000)
    assert sim.positions[1].cost_basis == pytest.approx(800) and sim.positions[2].cost_basis == pytest.approx(200)


# --------------------------------------------------------------------------- bug 5: unloaded acquirer + cash lost the shares

def test_unloaded_acquirer_with_cash_keeps_the_full_value_without_trading_costs():
    m = merge(market({"A": [100] * 4 + [None] * 4}), 4, "A", [("Z", 1.0)], cash=10.0)
    c = cfg(costs=CostModel())
    sim = Simulator(m, sig(m, entries=[(0, 0)], sizing=Sizing("fixed", 1000.0)), c)
    r = sim.run()
    eq = r.daily["equity"]
    assert eq.iloc[4] == pytest.approx(eq.iloc[3])        # Rs 100 per share: Rs 90 stock (as cash) + Rs 10 cash
    t = r.trades.iloc[0]
    assert t["income"] == pytest.approx(t["shares"] * 100)
    assert r.daily["costs"].iloc[4] == 0
    assert any("credited as cash" in w for w in r.config["warnings"])


# --------------------------------------------------------------------------- bug 6: flat bar after a split read as locked

def test_flat_bar_at_the_post_split_price_is_not_a_locked_circuit():
    m = market({"A": [100, 100, 100, (20, 20, 20, 20), 20]})
    m.actions = ca(m, [("A", 3, "split", 0.2, "FV 10->2")])
    sim, r = go(m, entries=[(0, 0)], exits=[(2, 0)])
    assert r.trades.iloc[0]["exit_date"] == m.dates[3] and r.unfilled.empty


# --------------------------------------------------------------------------- bug 7 and A4 wiring

def test_universe_exit_sell_needs_membership():
    m = market({"A": [100] * 3})
    with pytest.raises(ValueError, match="membership"):
        Simulator(m, sig(m), cfg(on_universe_exit="sell"))


def test_engine_enforces_eligibility():
    # B is under Rs 10, C is not a member on day 0, D's ADV is under the floor: only A is bought
    days = pd.bdate_range("2024-01-01", periods=4)
    tv = pd.DataFrame({"A": 2e7, "B": 2e7, "C": 2e7, "D": 1e6}, index=days)
    m = market({"A": [100] * 4, "B": [5] * 4, "C": [100] * 4, "D": [100] * 4}, turnover=tv, days=days)
    m.members = np.ones(m.close.shape, bool)
    m.members[0, 2] = False
    c = cfg(n=4, cap=100.0)
    c.eligibility = EligibilityConfig(min_price=10, min_adv=1e7, adv_days=1)
    entries = [(0, j) for j in range(4)]
    r = Simulator(m, sig(m, entries=entries, sizing=Sizing("fixed", 1000.0)), c).run()
    assert list(r.open_positions["SecurityID"]) == ["A"]
    warm = np.zeros(m.close.shape, bool)
    r = Simulator(m, sig(m, entries=entries, sizing=Sizing("fixed", 1000.0), warmup=warm), c).run()
    assert r.open_positions.empty


def test_signal_labels_and_adv_window_must_match_the_market():
    m = market({"A": [100] * 3, "B": [100] * 3})
    with pytest.raises(ValueError, match="ids"):
        Simulator(m, sig(m, ids=pd.Index(["B", "A"])), cfg())
    with pytest.raises(ValueError, match="dates"):
        Simulator(m, sig(m, dates=m.dates + pd.Timedelta(days=1)), cfg())
    with pytest.raises(ValueError, match="ADV window"):
        Simulator(m, sig(m), cfg(adv_days=20))


def test_simconfig_from_repo_config():
    import yaml
    from pathlib import Path
    raw = yaml.safe_load((Path(__file__).resolve().parents[2] / "config.yaml").read_text())
    c = SimConfig.from_config(raw, seed=3)
    assert c.seed == 3 and c.eligibility == EligibilityConfig() and c.ledger == LedgerConfig()
    assert c.costs == CostModel()


# --------------------------------------------------------------------------- accounting identity

@pytest.mark.parametrize("seed", range(6))
def test_equity_equals_capital_plus_realised_and_unrealised(seed):
    rng = np.random.default_rng(seed)
    D, ids = 80, [f"S{j}" for j in range(8)]
    days = pd.bdate_range("2023-01-02", periods=D)
    c = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.03, (D, len(ids))), axis=0)), index=days, columns=ids)
    o = c.shift(1).fillna(c) * np.exp(rng.normal(0, 0.01, c.shape))
    h, lo = np.maximum(o, c) * 1.01, np.minimum(o, c) * 0.99
    acts = pd.DataFrame({"SecurityID": rng.choice(ids, 12), "kind": rng.choice(
        ["split", "bonus", "dividend", "rights", "arrangement"], 12), "applied": True, "prior_close": 100.0,
        "detail": "1:4 @ 50"})
    acts["effective_date"] = acts["ex_date"] = days[rng.integers(1, D, 12)]
    acts["factor"] = np.where(acts["kind"].isin(["split", "bonus"]), 2 / 3, rng.uniform(0.9, 0.99, 12))
    mg = pd.DataFrame({"parent": ["S0", "S0"], "effective_date": [days[50]] * 2, "acquirer": ["S1", "S2"],
                       "ratio": [0.7, 0.3], "cash": [5.0, 0.0]})
    c.loc[days[50]:, "S0"] = np.nan
    m = Market.from_frames(o, h, lo, c, adv_days=20, actions=acts, mergers=mg)
    s = Signals(entry=rng.random(c.shape) < 0.1, exit=rng.random(c.shape) < 0.04, score=rng.random(c.shape),
                stop=np.full(c.shape, 0.1), trail=np.full(c.shape, 0.15), max_hold_days=20)
    sc = SimConfig(account=Account(capital=200_000, max_positions=4, max_position_pct=30), costs=CostModel(),
                   seed=seed, eligibility=EligibilityConfig(min_price=0, min_adv=0))
    sim = Simulator(m, s, sc)
    r = sim.run()
    unreal = sum(p.shares * p.last_close + p.income - p.cost_basis - p.buy_charges.total for p in sim._held())
    realised = r.trades["net_pnl"].sum() if len(r.trades) else 0.0
    assert r.daily["equity"].iloc[-1] - 200_000 == pytest.approx(realised + unreal, abs=1e-6)
    assert (r.daily["cash"] > -1e-6).all()
