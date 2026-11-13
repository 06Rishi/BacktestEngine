"""M2.3: stock-for-stock mergers from mergers.csv (amendments 2.3)."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from _simkit import cfg, market, sig
from nsebt.backtest import mergers
from nsebt.backtest.ledger import LedgerConfig
from nsebt.backtest.sim import Market, Simulator, Sizing, Signals

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "store"


def merger_market(acquirer="B", ratio=1.5):
    # A trades days 0-3 at 150, then merges into B (100) on day 4 at `ratio` B per A
    m = market({"A": [150] * 4 + [None] * 4, "B": [100] * 8})
    m.mergers = pd.DataFrame({"parent": ["A"], "effective_date": [m.dates[4]], "acquirer": [acquirer],
                              "ratio": [ratio]})
    return m


def run(m, entries=((0, 0),), value=1050.0, merger="convert_then_sell", **kw):
    s = sig(m, entries=list(entries), sizing=Sizing("fixed", value), **kw)
    c = cfg()
    c.ledger = LedgerConfig(merger=merger)
    sim = Simulator(m, s, c)
    return sim, sim.run()


def test_convert_then_sell_at_the_acquirer_open():
    m = merger_market()
    sim, r = run(m)                       # 7 A shares -> 10.5 B: 10 shares sold, 0.5 paid at the close
    t = r.trades.iloc[0]
    assert (t["SecurityID"], t["exit_security"], t["exit_reason"]) == ("A", "B", "merger exit")
    assert t["shares"] == 10 and t["exit_price"] == 100 and t["exit_date"] == m.dates[4]
    assert t["income"] == pytest.approx(50) and t["gross_pnl"] == pytest.approx(0)
    assert not sim.positions
    assert r.daily["equity"].iloc[-1] == pytest.approx(30_000)


def test_convert_and_hold_keeps_the_acquirer():
    m = merger_market()
    stop = np.full(m.close.shape, 0.10)
    sim, r = run(m, merger="convert_and_hold", stop=stop)
    p = sim.positions[1]
    assert p.shares == 10 and p.from_j == 0 and p.cost_basis == pytest.approx(1050)
    assert p.stop == pytest.approx(135 / 1.5) and p.entry_price == pytest.approx(100)
    assert r.trades.empty
    d = r.daily["equity"]
    assert d.iloc[4] == pytest.approx(d.iloc[3])
    assert r.ledger.iloc[0]["kind"] == "merger" and r.ledger.iloc[0]["shares_after"] == 10


def test_merger_into_an_existing_holding():
    m = merger_market()
    # hold both A (7 shares) and B (10 shares)
    s_kw = dict(entries=((0, 0), (0, 1)))
    sim, r = run(m, merger="convert_and_hold", value=1050, **s_kw)
    p = sim.positions[1]
    assert p.shares == 10 + 10 and p.cost_basis == pytest.approx(7 * 150 + 10 * 100) and len(sim.positions) == 1
    sim, r = run(m, merger="convert_then_sell", value=1050, **s_kw)
    assert sim.positions[1].shares == 10                                  # B holding kept
    t = r.trades.iloc[0]
    assert t["SecurityID"] == "A" and t["exit_reason"] == "merger exit" and t["shares"] == 10


def test_acquirer_missing_from_market_closes_at_last_close():
    m = merger_market(acquirer="Z")
    sim, r = run(m)
    t = r.trades.iloc[0]
    assert t["exit_reason"] == "merger" and t["exit_price"] == 150 and not sim.positions
    assert any("not in the market" in w for w in r.config["warnings"])


def test_no_merger_without_a_position():
    m = merger_market()
    sim, r = run(m, entries=())
    assert r.ledger.empty and r.trades.empty


# --------------------------------------------------------------------------- the table

def test_load_parses_ratios_and_skips_blank_rows(tmp_path):
    p = tmp_path / "mergers.csv"
    pd.DataFrame({"parent": ["P1", "P2", "P3", "P4"], "parent_symbol": "", "last_trade": "",
                  "effective_date": ["2023-07-13", "2020-04-01", "2019-04-01", "2024-01-01"],
                  "acquirer": ["Q1", "Q2", "Q3", ""], "acquirer_symbol": "",
                  "ratio": ["42/25", "1.15", "402:1000", ""], "implied_ratio": "", "source": "",
                  "note": ""}).to_csv(p, index=False)
    t = mergers.load(p)
    assert list(t["parent"]) == ["P1", "P2", "P3"]
    assert t["ratio"].tolist() == pytest.approx([1.68, 1.15, 0.402])
    assert t["effective_date"].iloc[0] == pd.Timestamp("2023-07-13")


@pytest.mark.parametrize("bad", [{"ratio": "0"}, {"ratio": "abc"}, {"effective_date": "soon"},
                                 {"acquirer": "P1"}])
def test_load_rejects_invalid_rows(tmp_path, bad):
    row = {c: "" for c in mergers.COLUMNS}
    row.update(parent="P1", effective_date="2023-07-13", acquirer="Q1", ratio="1.5")
    row.update(bad)
    p = tmp_path / "m.csv"
    pd.DataFrame([row]).to_csv(p, index=False)
    with pytest.raises(ValueError, match="invalid"):
        mergers.load(p)


def test_load_rejects_duplicate_parents(tmp_path):
    rows = [{c: "" for c in mergers.COLUMNS} for _ in range(2)]
    for r in rows:
        r.update(parent="P1", effective_date="2023-07-13", acquirer="Q1", ratio="1.5")
    p = tmp_path / "m.csv"
    pd.DataFrame(rows).to_csv(p, index=False)
    with pytest.raises(ValueError, match="twice"):
        mergers.load(p)


def test_candidates_and_seed_merge():
    cal = pd.bdate_range("2020-01-01", periods=30)
    sec = pd.DataFrame({"SecurityID": ["S1", "S2", "S3", "S4"], "Symbol": ["ONE", "TWO", "THREE", "FOUR"],
                        "ISIN": ["I1", "I2", "I3", "I4"],
                        "last_date": [cal[10], cal[-2], cal[-1], cal[5]]})
    ann = pd.DataFrame({"isin": ["I4", "I1"], "subject": ["Scheme Of Amalgamation", "De-Merger"]})
    c = mergers.candidates(sec, ["S1", "S2", "S3"], cal, ann)
    assert list(c["parent"]) == ["S4", "S1"]                      # S2, S3 truncated; S4 from announcement
    assert c.set_index("parent").at["S1", "effective_date"] == cal[11].date().isoformat()
    existing = c.iloc[[1]].copy()
    existing["acquirer"], existing["ratio"] = "S9", "2"
    merged = mergers.merge_seed(existing, c)
    assert len(merged) == 2 and merged.set_index("parent").at["S1", "ratio"] == "2"


def test_implied_ratio():
    close = pd.DataFrame({"P": [168.0, np.nan], "Q": [99.0, 100.0]}, index=pd.bdate_range("2023-07-12", periods=2))
    assert mergers.implied_ratio(close, "P", "Q", "2023-07-12") == pytest.approx(168 / 99)


# --------------------------------------------------------------------------- real data: HDFC Ltd -> HDFC Bank

HDFC, HDFCBANK = "INE001A01036", "INE040A01026"


@pytest.mark.skipif(not (STORE / "prices").exists(), reason="store not built")
def test_hdfc_into_hdfc_bank_real_data():
    table = mergers.load(ROOT / "mergers.csv")
    row = table.set_index("parent").loc[HDFC]
    assert row["acquirer"] == HDFCBANK and row["ratio"] == pytest.approx(42 / 25)
    px = pd.read_parquet(STORE / "prices", columns=["Date", "SecurityID", "Open", "High", "Low", "Close"],
                         filters=[("SecurityID", "in", [HDFC, HDFCBANK]),
                                  ("Date", ">=", pd.Timestamp("2023-06-01")),
                                  ("Date", "<=", pd.Timestamp("2023-08-31"))])
    w = {k: px.pivot_table(index="Date", columns="SecurityID", values=k).sort_index()
         for k in ("Open", "High", "Low", "Close")}
    assert w["Close"][HDFC].last_valid_index() == pd.Timestamp("2023-07-12")
    assert row["effective_date"] == pd.Timestamp("2023-07-13")
    m = Market.from_frames(w["Open"], w["High"], w["Low"], w["Close"], adv_days=1, mergers=table)
    D, N = m.close.shape
    j = list(m.ids).index(HDFC)
    entry = np.zeros((D, N), bool)
    entry[0, j] = True
    for mode in ("convert_and_hold", "convert_then_sell"):
        c = cfg(capital=1_000_000, cap=100.0)
        c.ledger = LedgerConfig(merger=mode)
        sim = Simulator(m, Signals(entry=entry, sizing=Sizing("fixed", 500_000)), c)
        r = sim.run()
        lg = r.ledger.iloc[0]
        assert lg["kind"] == "merger" and lg["date"] == pd.Timestamp("2023-07-13")
        assert lg["shares_after"] == int(lg["shares_before"] * 42 / 25 + 1e-9)
        k = r.daily.index.get_loc(pd.Timestamp("2023-07-13"))
        # value carried across the conversion: the day's move plus the pre-merger discount (~0.7%)
        assert r.daily["equity"].iloc[k] / r.daily["equity"].iloc[k - 1] == pytest.approx(1, abs=0.03)
        if mode == "convert_then_sell":
            t = r.trades.iloc[0]
            assert t["exit_security"] == HDFCBANK and t["exit_date"] == pd.Timestamp("2023-07-13")
        else:
            assert sim.positions[list(m.ids).index(HDFCBANK)].from_j == j


def test_market_without_turnover_has_no_liquidity_limit():
    c = pd.DataFrame({"A": [100.0, 100.0, 100.0]}, index=pd.bdate_range("2024-01-01", periods=3))
    m = Market.from_frames(c, c * 1.01, c * 0.99, c)
    assert np.isinf(m.adv).all()
