"""M2.1: corporate-action ledger, share events (amendments 2.2) and config switches (2.3)."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from _simkit import ca, cfg, market, sig, with_actions
from nsebt.backtest.ledger import (LedgerConfig, dividend_amount, schedule, split_shares,
                                   value_per_share)
from nsebt.backtest.sim import Simulator, Sizing


def frac(m, v):
    a = np.full(m.close.shape, np.nan)
    a[:, 0] = v
    return a


def run(m, entries=((0, 0),), exits=(), value=1000.0, **kw):
    s = sig(m, entries=list(entries), exits=list(exits), sizing=Sizing("fixed", value), **kw)
    sim = Simulator(m, s, cfg())
    return sim, sim.run()


# --------------------------------------------------------------------------- the 2.2 table

@pytest.mark.parametrize("kind,f,shares", [
    ("split", 0.2, 50),             # face value ₹10 -> ₹2
    ("bonus", 0.5, 20),             # 1:1
    ("bonus", 2 / 3, 15),           # 1:2
    ("consolidation", 10.0, 1),     # 10:1
    ("inferred_split", 0.1, 100),
])
def test_share_event_table(kind, f, shares):
    # 10 shares bought at 100 on day 1; event effective day 3 with raw prices × f from then on
    m = market({"A": [100, 100, 100, 100 * f, 100 * f]})
    with_actions(m, [("A", 3, kind, f)])
    sim, r = run(m, stop=frac(m, 0.10), target=frac(m, 0.50), trail=frac(m, 0.10))
    p = sim.positions[0]
    assert p.shares == shares
    assert p.entry_price == pytest.approx(100 * f)
    assert p.stop == pytest.approx(90 * f) and p.target == pytest.approx(150 * f)
    assert p.trail_ref == pytest.approx(100 * f)
    assert p.cost_basis == pytest.approx(1000) and r.trades.empty      # no stop fired on the scaled prices
    eq = r.daily["equity"]
    assert eq.iloc[3] == pytest.approx(eq.iloc[2])                     # value unchanged by the event


def test_no_adjustment_without_a_position():
    m = market({"A": [100, 100, 50, 50]})
    with_actions(m, [("A", 2, "split", 0.5)])
    sim, r = run(m, entries=())
    assert r.trades.empty and not sim.positions and r.daily["equity"].nunique() == 1


def test_unapplied_rows_are_ignored():
    m = market({"A": [100, 100, 100, 100]})
    with_actions(m, [("A", 2, "split", 0.5)])
    m.actions["applied"] = False
    sim, _ = run(m)
    assert sim.positions[0].shares == 10


# --------------------------------------------------------------------------- fractions

def test_fractional_bonus_paid_as_cash_at_ex_date_close():
    f = 2 / 3                                     # bonus 1:2: 3 shares -> 4.5
    m = market({"A": [100, 100, 100, (66, 67, 65, 200 / 3), 200 / 3, 70]})
    with_actions(m, [("A", 3, "bonus", f)])
    sim, r = run(m, value=300, exits=[(4, 0)])
    d = r.daily
    assert d["cash"].iloc[3] - d["cash"].iloc[2] == pytest.approx(0.5 * 200 / 3)
    assert d["equity"].iloc[3] == pytest.approx(d["equity"].iloc[2])
    t = r.trades.iloc[0]
    assert t["shares"] == 4 and t["income"] == pytest.approx(100 / 3)
    assert t["gross_pnl"] == pytest.approx(4 * 70 + 100 / 3 - 300)


def test_consolidation_below_one_share_becomes_cash():
    m = market({"A": [100, 100, 100, 1000, 1000]})
    with_actions(m, [("A", 3, "consolidation", 10.0)])
    sim, r = run(m, value=300)
    assert not sim.positions
    t = r.trades.iloc[0]
    assert t["exit_reason"] == "corporate action" and t["exit_at"] == "cash in lieu"
    assert t["net_pnl"] == pytest.approx(0) and t["income"] == pytest.approx(300)
    assert r.daily["equity"].iloc[-1] == pytest.approx(30_000)


def test_fraction_on_a_position_sold_at_the_ex_date_open():
    # 3 shares, bonus 1:2 on day 3 while a sell is pending: 4 shares sold at the open, 0.5 paid at the close
    m = market({"A": [100, 100, 100, (60, 61, 59, 64), 64]})
    with_actions(m, [("A", 3, "bonus", 2 / 3)])
    sim, r = run(m, value=300, exits=[(2, 0)])
    t = r.trades.iloc[0]
    assert t["shares"] == 4 and t["exit_price"] == 60
    assert t["income"] == pytest.approx(0.5 * 64)
    assert t["gross_pnl"] == pytest.approx(4 * 60 + 32 - 300)
    assert r.daily["cash"].iloc[3] == pytest.approx(30_000 + t["gross_pnl"])


def test_split_shares_helper():
    assert split_shares(10, 0.2) == (50, 0.0)
    assert split_shares(10, 2 / 3) == (15, 0.0)
    w, fr = split_shares(3, 2 / 3)
    assert w == 4 and fr == pytest.approx(0.5)
    w, fr = split_shares(7, 10.0)
    assert w == 0 and fr == pytest.approx(0.7)


# --------------------------------------------------------------------------- timing and entitlement

def test_split_on_the_day_a_pending_buy_fills():
    # signal at the day-0 close (100); split effective day 1; the order fills at the post-split open
    m = market({"A": [100, 20, 20, 20]})
    with_actions(m, [("A", 1, "split", 0.2)])
    sim, r = run(m, stop=frac(m, 0.10))
    p = sim.positions[0]
    assert p.entry_t == 1 and p.shares == 50          # ₹1000 / 20: not entitled, not adjusted
    assert p.entry_price == 20 and p.stop == pytest.approx(18)


def test_position_sold_at_ex_date_open_is_entitled():
    m = market({"A": [100, 100, 100, 50, 50]})
    with_actions(m, [("A", 3, "split", 0.5)])
    sim, r = run(m, exits=[(2, 0)])
    t = r.trades.iloc[0]
    assert t["shares"] == 20 and t["exit_price"] == 50 and t["gross_pnl"] == pytest.approx(0)


def test_pnl_uses_cost_basis_across_a_split():
    m = market({"A": [100, 100, 100, 50, 60, 60]})
    with_actions(m, [("A", 3, "split", 0.5)])
    _, r = run(m, exits=[(4, 0)])
    t = r.trades.iloc[0]
    assert t["entry_price"] == pytest.approx(50) and t["shares"] == 20
    assert t["gross_pnl"] == pytest.approx(200) and t["return"] == pytest.approx(0.2)


def test_trailing_stop_survives_a_split():
    # unadjusted, the trailing stop (90) would fire on the post-split low; adjusted it is 45
    m = market({"A": [100, 100, 100, 50, 50, 50]})
    with_actions(m, [("A", 3, "split", 0.5)])
    sim, r = run(m, trail=frac(m, 0.10))
    assert r.trades.empty and sim.positions[0].trail_ref == pytest.approx(50)


def test_unscheduled_event_flagged_when_a_position_is_held():
    m = market({"A": [100, 100, 100, 100, 100]})
    acts = ca(m, [("A", 3, "split", 0.5)])
    acts["effective_date"] = pd.NaT
    m.actions = acts
    sim, r = run(m)
    assert sim.positions[0].shares == 10
    assert any("no effective date" in w for w in r.config["warnings"])


# --------------------------------------------------------------------------- scheduling

def test_schedule_orders_value_before_share_and_drops_out_of_range():
    m = market({"A": [100] * 5, "B": [100] * 5})
    rows = ca(m, [("A", 2, "bonus", 0.5), ("A", 2, "dividend", 0.99, "Rs 1", 100.0),
                  ("B", 0, "split", 0.5), ("B", 3, "dividend", np.nan, "Rs 2.50", np.nan),
                  ("B", 4, "rights", 0.95, "1:4 @ 50", 100.0), ("C", 2, "split", 0.5),
                  ("A", 3, "buyback", 1.0)])
    ev, uns = schedule(m.dates, m.ids, rows)
    assert [e.kind for e in ev[2]] == ["dividend", "bonus"]
    assert 0 not in ev                                   # first day: nothing can be held yet
    assert ev[3][0].amount_ps == pytest.approx(2.5) and not ev[3][0].approx
    assert ev[4][0].amount_ps == pytest.approx(5.0) and ev[4][0].approx
    assert ev[2][0].amount_ps == pytest.approx(1.0) and ev[2][1].amount_ps == 0
    assert uns == []


def test_value_per_share_matches_rights_theory():
    # 1:15 @ 1257 on a 1479.25 close: TERP = (15·1479.25 + 1257)/16; value per old share = (1/15)(TERP − 1257)
    c, a, b, i = 1479.25, 1, 15, 1257
    terp = (b * c + a * i) / (a + b)
    assert value_per_share("rights", terp / c, c) == pytest.approx(a / b * (terp - i))
    assert value_per_share("dividend", 1 - 2.1 / 419.3, 419.3) == pytest.approx(2.1)
    assert dividend_amount("Rs 0.14") == 0.14 and np.isnan(dividend_amount("Demerger"))


# --------------------------------------------------------------------------- config

def test_ledger_config_defaults_and_validation():
    c = LedgerConfig()
    assert (c.dividends, c.dividend_pay_lag_days, c.rights, c.rights_realise_lag_days, c.demerger,
            c.demerger_realise_lag_days, c.merger) == ("receivable", 30, "sell_entitlement", 15,
                                                        "receivable", 60, "convert_then_sell")
    with pytest.raises(ValueError, match="dividends"):
        LedgerConfig(dividends="reinvest")
    with pytest.raises(ValueError, match="lag"):
        LedgerConfig(dividend_pay_lag_days=-1)
    with pytest.raises(ValueError, match="unknown"):
        LedgerConfig.from_config({"corporate_actions": {"dividend": "instant"}})


def test_repo_config_block_matches_defaults():
    raw = yaml.safe_load((Path(__file__).resolve().parents[2] / "config.yaml").read_text())
    assert LedgerConfig.from_config(raw) == LedgerConfig()
