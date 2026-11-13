"""M2.2: dividend, rights and demerger receivables (amendments 2.3).

On the ex-date the raw price falls by the value moved out of the share and the ledger credits
the same value as a receivable: equity is unchanged, cash arrives only after the lag.
"""
import numpy as np
import pandas as pd
import pytest

from _simkit import cfg, market, sig, with_actions
from nsebt.backtest.ledger import LedgerConfig, parse_rights
from nsebt.backtest.sim import Simulator, Sizing


def bars(n, at=None):
    """n days at 100; `at` = {day: price from that day on}."""
    out, px = [], 100.0
    for t in range(n):
        px = (at or {}).get(t, px)
        out.append(px)
    return out


def run(m, entries=((0, 0),), exits=(), value=1000.0, ledger=LedgerConfig(), sizing=None, **kw):
    s = sig(m, entries=list(entries), exits=list(exits), sizing=sizing or Sizing("fixed", value), **kw)
    c = cfg(**{k: kw.pop(k) for k in ("capital", "n", "cap") if k in kw})
    c.ledger = ledger
    sim = Simulator(m, s, c)
    return sim, sim.run()


def due_index(m, t, days):
    return int(m.dates.searchsorted(m.dates[t] + pd.Timedelta(days=days)))


# --------------------------------------------------------------------------- dividends

def test_dividend_receivable_preserves_value_and_pays_after_lag():
    m = with_actions(market({"A": bars(30, {3: 98})}), [("A", 3, "dividend", 0.98, "Rs 2", 100.0)])
    sim, r = run(m)
    d = r.daily
    pay = due_index(m, 3, 30)
    assert d["equity"].iloc[3] == pytest.approx(d["equity"].iloc[2])
    assert d["cash"].iloc[3] == d["cash"].iloc[2] and d["receivables"].iloc[3] == pytest.approx(20)
    assert (d["cash"].iloc[3:pay] == d["cash"].iloc[3]).all()
    assert d["cash"].iloc[pay] - d["cash"].iloc[pay - 1] == pytest.approx(20)
    assert d["receivables"].iloc[pay] == 0
    lg = r.ledger.iloc[0]
    assert lg["treatment"] == "receivable" and lg["due_date"] == m.dates[pay] and not lg["approx"]


def test_receivable_counts_for_sizing_but_not_buying_power():
    # all-in on A; a ₹10 dividend leaves ₹900 cash + ₹100 receivable after A is sold
    m = market({"A": bars(12, {3: 90}), "B": [1.0] * 12})
    with_actions(m, [("A", 3, "dividend", 0.9, "Rs 10", 100.0)])
    s = sig(m, entries=[(0, 0), (5, 1)], exits=[(4, 0)], sizing=Sizing("equal"))
    sim = Simulator(m, s, cfg(capital=1000, n=1, cap=100.0))
    r = sim.run()
    assert r.daily["equity"].iloc[5] == pytest.approx(1000)
    assert r.daily["cash"].iloc[5] == pytest.approx(900)
    assert sim.positions[1].shares == 900                  # limited by cash, not by equity
    assert r.trades.iloc[0]["income"] == pytest.approx(100)
    assert r.trades.iloc[0]["gross_pnl"] == pytest.approx(0)   # price drop offset by the dividend


def test_dividend_instant_and_ignore():
    m = with_actions(market({"A": bars(8, {3: 98})}), [("A", 3, "dividend", 0.98, "Rs 2", 100.0)])
    _, r = run(m, ledger=LedgerConfig(dividends="instant"))
    assert r.daily["cash"].iloc[3] - r.daily["cash"].iloc[2] == pytest.approx(20)
    assert r.daily["receivables"].eq(0).all()
    _, r = run(m, ledger=LedgerConfig(dividends="ignore"))
    assert r.daily["equity"].iloc[2] - r.daily["equity"].iloc[3] == pytest.approx(20)


def test_dividend_without_factor_uses_declared_amount():
    m = with_actions(market({"A": bars(8, {3: 97})}), [("A", 3, "dividend", np.nan, "Rs 2.50", np.nan)])
    sim, r = run(m, stop=np.full(m.close.shape, 0.10))
    assert r.daily["receivables"].iloc[3] == pytest.approx(25)
    assert sim.positions[0].stop == pytest.approx(90)      # no factor: price levels unchanged


def test_buy_filled_on_ex_date_is_not_entitled():
    m = with_actions(market({"A": bars(8, {1: 98})}), [("A", 1, "dividend", 0.98, "Rs 2", 100.0)])
    _, r = run(m)
    assert r.daily["receivables"].eq(0).all() and r.ledger.empty


def test_receivable_due_after_data_end_stays_in_equity():
    m = with_actions(market({"A": bars(6, {3: 98})}), [("A", 3, "dividend", 0.98, "Rs 2", 100.0)])
    _, r = run(m)
    assert r.daily["receivables"].iloc[-1] == pytest.approx(20)
    assert r.ledger.iloc[0]["due_date"] == "after data end"


# --------------------------------------------------------------------------- rights

def test_rights_entitlement_sold_after_15_trading_days():
    # 1:5 @ 50 on a 100 close: TERP = (5·100 + 50)/6; value per old share = (1/5)(TERP − 50)
    terp = 550 / 6
    m = with_actions(market({"A": bars(25, {3: terp})}), [("A", 3, "rights", terp / 100, "1:5 @ 50", 100.0)])
    _, r = run(m)
    d = r.daily
    v = 10 * (terp - 50) / 5
    assert d["equity"].iloc[3] == pytest.approx(d["equity"].iloc[2])
    assert d["receivables"].iloc[3] == pytest.approx(v)
    assert d["cash"].iloc[18] - d["cash"].iloc[17] == pytest.approx(v) and d["receivables"].iloc[18] == 0
    assert r.ledger.iloc[0]["approx"] and r.ledger.iloc[0]["treatment"] == "entitlement sold"


def test_rights_subscribe_takes_up_shares_with_cash():
    terp = 550 / 6
    m = with_actions(market({"A": bars(8, {3: terp})}), [("A", 3, "rights", terp / 100, "1:5 @ 50", 100.0)])
    sim, r = run(m, value=700, ledger=LedgerConfig(rights="subscribe"))   # 7 shares -> 1.4 entitlements
    p = sim.positions[0]
    assert p.shares == 8 and p.cost_basis == pytest.approx(750)
    d = r.daily
    assert d["cash"].iloc[2] - d["cash"].iloc[3] == pytest.approx(50)
    assert d["receivables"].iloc[3] == pytest.approx(0.4 * (terp - 50))
    assert d["equity"].iloc[3] == pytest.approx(d["equity"].iloc[2])


def test_rights_ignore_loses_the_entitlement():
    terp = 550 / 6
    m = with_actions(market({"A": bars(8, {3: terp})}), [("A", 3, "rights", terp / 100, "1:5 @ 50", 100.0)])
    _, r = run(m, ledger=LedgerConfig(rights="ignore"))
    assert r.daily["equity"].iloc[2] - r.daily["equity"].iloc[3] == pytest.approx(10 * (100 - terp))


def test_parse_rights():
    assert parse_rights("1:15 @ 1257") == (1, 15, 1257.0)
    assert parse_rights("613:399 @ 10") == (613, 399, 10.0)
    assert parse_rights("Demerger") is None


# --------------------------------------------------------------------------- demergers

def test_demerger_receivable_and_stop_scaled():
    m = with_actions(market({"A": bars(60, {3: 60})}), [("A", 3, "arrangement", 0.6, "Demerger", 100.0)])
    sim, r = run(m, stop=np.full(m.close.shape, 0.10))
    d = r.daily
    pay = due_index(m, 3, 60)
    assert d["equity"].iloc[3] == pytest.approx(d["equity"].iloc[2])
    assert d["receivables"].iloc[3] == pytest.approx(400)
    assert d["cash"].iloc[pay] - d["cash"].iloc[pay - 1] == pytest.approx(400)
    assert sim.positions[0].stop == pytest.approx(54) and r.trades.empty
    assert r.ledger.iloc[0]["approx"]


def test_demerger_table_option_falls_back_with_warning():
    m = with_actions(market({"A": bars(8, {3: 60})}), [("A", 3, "arrangement", 0.6, "Demerger", 100.0)])
    _, r = run(m, ledger=LedgerConfig(demerger="table_then_receivable"))
    assert r.daily["receivables"].iloc[3] == pytest.approx(400)
    assert any("demergers.csv" in w for w in r.config["warnings"])


# --------------------------------------------------------------------------- ordering

def test_value_event_before_share_event_on_the_same_day():
    # ₹2 dividend and a 1:1 bonus together: dividend on the 10 pre-bonus shares, then 20 shares
    m = market({"A": bars(8, {3: 49})})
    with_actions(m, [("A", 3, "bonus", 0.5), ("A", 3, "dividend", 0.98, "Rs 2", 100.0)])
    sim, r = run(m)
    assert sim.positions[0].shares == 20
    assert r.daily["receivables"].iloc[3] == pytest.approx(20)
    assert r.daily["equity"].iloc[3] == pytest.approx(r.daily["equity"].iloc[2])
    assert list(r.ledger["kind"]) == ["dividend", "bonus"]
