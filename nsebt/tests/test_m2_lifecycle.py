"""M2.4: securities that stop trading (amendments 2.4): classification and engine treatment."""
import numpy as np
import pandas as pd
import pytest

from _simkit import cfg, market, sig
from nsebt import lifecycle
from nsebt.backtest.ledger import LedgerConfig, delisting_value
from nsebt.backtest.sim import Simulator, Sizing

CAL = pd.bdate_range("2020-01-01", periods=40)


def secs(rows):
    return pd.DataFrame(rows, columns=["SecurityID", "Symbol", "last_date"])


# --------------------------------------------------------------------------- classification

def test_one_security_per_class():
    sec = secs([("M", "MRG", CAL[10]), ("T", "TRN", CAL[-4]), ("D", "DEL", CAL[12]), ("S", "SUS", CAL[14]),
                ("U", "UNK", CAL[16]), ("L", "LIVE", CAL[-1])])
    mg = pd.DataFrame({"parent": ["M"], "effective_date": [CAL[11]], "acquirer": ["X"], "ratio": [1.5]})
    dl = pd.DataFrame({"Symbol": ["DEL"], "list_date": [CAL[20]], "detail": ["Compulsory Delisting"]})
    sp = pd.DataFrame({"Symbol": ["SUS"], "list_date": [CAL[15]], "detail": [""]})
    out = lifecycle.classify(sec, CAL, mergers=mg, delisted=dl, suspended=sp).set_index("SecurityID")
    assert "L" not in out.index                                         # still trading at the end
    assert out["cls"].to_dict() == {"M": "merged", "T": "truncated", "D": "delisted", "S": "suspended",
                                    "U": "unknown"}
    assert out.at["D", "effective_date"] == CAL[13] and out.at["D", "detail"] == "Compulsory Delisting"
    assert out.at["U", "source"] == "data"


def test_truncation_boundary():
    assert lifecycle.is_truncated(CAL[-6], CAL)          # 5 trading days before the end
    assert not lifecycle.is_truncated(CAL[-7], CAL)


def test_list_match_uses_the_security_that_last_traded_under_the_symbol():
    # symbol ABC reused: the old security stopped at CAL[5] (delisted CAL[6]); a new one stopped at CAL[30]
    sec = secs([("OLD", "ABC", CAL[5]), ("NEW", "ABC", CAL[30])])
    dl = pd.DataFrame({"Symbol": ["ABC"], "list_date": [CAL[6]], "detail": ["Voluntary Delisting"]})
    out = lifecycle.classify(sec, CAL, delisted=dl).set_index("SecurityID")["cls"]
    assert out["OLD"] == "delisted" and out["NEW"] == "unknown"


def test_read_nse_list_format(tmp_path):
    p = tmp_path / "delisted_20261113.csv"
    p.write_bytes(b"Symbol,Company,Delisted Date,Type of Delisting,,,,,\r\n"
                  b"HEXAWARE,Hexaware Technologies Limited,09-Nov-20,Voluntary Delisting\xa0,,,,,\r\n"
                  b",,,,,,,,\r\n")
    df = lifecycle.read_list(lifecycle.latest(tmp_path, "delisted"), "Delisted Date")
    assert df.to_dict("records") == [{"Symbol": "HEXAWARE", "list_date": pd.Timestamp("2020-11-09"),
                                      "detail": "Voluntary Delisting"}]
    assert lifecycle.read_list(tmp_path / "missing.csv", "Delisted Date").empty


# --------------------------------------------------------------------------- engine

def stopped_market(cls, n=12, stop_after=3, resume=None):
    """A trades at 100 on days 0..stop_after, then stops (resumes at `resume` if given)."""
    rows = [100.0] * (stop_after + 1) + [None] * (n - stop_after - 1)
    if resume is not None:
        rows[resume:] = [110.0] * (n - resume)
    m = market({"A": rows, "B": [50.0] * n})
    m.lifecycle = pd.DataFrame({"SecurityID": ["A"], "Symbol": ["A"], "last_trade": [m.dates[stop_after]],
                                "effective_date": [m.dates[stop_after + 1]], "cls": [cls], "source": ["test"],
                                "list_date": [pd.NaT], "detail": [""]})
    return m


def run(m, ledger=LedgerConfig(), **kw):
    s = sig(m, entries=[(0, 0)], sizing=Sizing("fixed", 1000.0), **kw)
    c = cfg()
    c.ledger = ledger
    sim = Simulator(m, s, c)
    return sim, sim.run()


@pytest.mark.parametrize("policy,price", [("haircut(50)", 50.0), ("last_close", 100.0), ("zero", 0.0),
                                          ("haircut(20)", 80.0)])
def test_delisted_exits_under_each_policy(policy, price):
    m = stopped_market("delisted")
    sim, r = run(m, LedgerConfig(delisting=policy))
    t = r.trades.iloc[0]
    assert t["exit_reason"] == "delisted" and t["exit_at"] == policy and t["exit_date"] == m.dates[4]
    assert t["exit_price"] == pytest.approx(price) and t["costs"] == 0
    assert r.daily["cash"].iloc[4] == pytest.approx(30_000 - 1000 + 10 * price)
    assert not sim.positions


def test_unknown_held_through_grace_then_delisted():
    m = stopped_market("unknown", n=12)
    sim, r = run(m, LedgerConfig(unknown_grace_days=5))
    t = r.trades.iloc[0]
    assert t["exit_date"] == m.dates[4 + 5] and t["exit_price"] == pytest.approx(50)
    assert "grace" in t["exit_reason"]
    assert (r.daily["equity"].iloc[3:9] == 30_000).all()             # held at the last close meanwhile


def test_unknown_beyond_the_data_is_held():
    m = stopped_market("unknown", n=12)
    sim, r = run(m)                                                   # 60-day grace runs past the end
    assert r.trades.empty and sim.positions[0].last_close == 100


def test_suspended_held_stops_cannot_fill_exit_after_resume():
    # suspended days 4-7; a 10% stop can't fire while suspended; an exit decided on day 5 fills at resumption
    m = stopped_market("suspended", n=12, resume=8)
    stop = np.full(m.close.shape, 0.10)
    sim, r = run(m, stop=stop, exits=[(5, 0)])
    t = r.trades.iloc[0]
    assert t["exit_date"] == m.dates[8] and t["exit_price"] == 110 and t["exit_reason"] == "exit rule"
    assert (r.daily["equity"].iloc[4:8] == 30_000).all()
    assert (r.unfilled["why"] == "not traded").sum() == 2             # days 6 and 7


def test_truncated_marked_at_last_close():
    m = stopped_market("truncated")
    sim, r = run(m)
    assert r.trades.empty and r.daily["equity"].iloc[-1] == 30_000 and sim.positions[0].shares == 10


def test_delisting_policy_validation():
    assert delisting_value("haircut(50)") == 0.5 and delisting_value("zero") == 0
    for bad in ("haircut(150)", "haircut", "half"):
        with pytest.raises(ValueError, match="delisting"):
            LedgerConfig(delisting=bad)


# --------------------------------------------------------------------------- sourced overrides

def test_overrides_set_class_and_stated_value(tmp_path):
    p = tmp_path / "o.csv"
    pd.DataFrame({"SecurityID": ["D", "S"], "Symbol": ["DEL", "SUS"], "cls": ["delisted", "suspended"],
                  "value": ["0", ""], "source": ["NSE notice: shares extinguished", "NSE notice: suspended"],
                  "note": ["", ""]}).to_csv(p, index=False)
    ov = lifecycle.read_overrides(p)
    sec = secs([("D", "DEL", CAL[12]), ("S", "SUS", CAL[14])])
    out = lifecycle.classify(sec, CAL, overrides=ov).set_index("SecurityID")
    assert out.at["D", "cls"] == "delisted" and out.at["D", "value"] == 0
    assert out.at["S", "cls"] == "suspended" and np.isnan(out.at["S", "value"])
    assert out.at["D", "source"] == "lifecycle_overrides.csv"


@pytest.mark.parametrize("bad", [{"cls": "gone"}, {"value": "-1"}, {"source": ""}])
def test_overrides_validation(tmp_path, bad):
    row = {"SecurityID": "D", "Symbol": "DEL", "cls": "delisted", "value": "0", "source": "x", "note": ""}
    row.update(bad)
    p = tmp_path / "o.csv"
    pd.DataFrame([row]).to_csv(p, index=False)
    with pytest.raises(ValueError, match="invalid"):
        lifecycle.read_overrides(p)


def test_stated_delisting_value_overrides_the_policy():
    m = stopped_market("delisted")
    m.lifecycle["value"] = 0.0
    sim, r = run(m, LedgerConfig(delisting="last_close"))
    t = r.trades.iloc[0]
    assert t["exit_price"] == 0 and t["exit_at"] == "stated value" and t["net_pnl"] == pytest.approx(-1000)
