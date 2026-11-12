"""Phase 1: NSE downloads (mocked network), parsers (real file formats), identity
linking, corporate-action factors, adjustment and membership dates."""
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nsebt import corpactions, membership, nsefetch, nseparse, nsestore

FX = Path(__file__).parent / "fixtures"
FAST = nsefetch.Politeness(min_interval=0, jitter=0, retries=2, backoff=0, cooldown_after=2,
                           cooldown=0, max_cooldowns=1)


# --------------------------------------------------------------------------- network (mocked)

class Resp:
    def __init__(self, status, content=b"", headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responder):
        self.responder, self.calls, self.headers = responder, [], {}

    def get(self, url, timeout=None, **kw):
        self.calls.append(url)
        return self.responder(url)


def test_download_saves_valid_file_and_repeat_run_makes_no_request(tmp_path):
    body = (FX / "cm29AUG2016bhav.csv.zip").read_bytes()
    s = FakeSession(lambda u: Resp(200, body))
    c = nsefetch.NSEClient(tmp_path, FAST, session=s)
    assert c.get("cm_legacy", date(2016, 8, 29))[0] == "ok"
    assert c.get("cm_legacy", date(2016, 8, 29))[0] == "cached"
    assert len(s.calls) == 1


def test_404_is_missing_and_remembered_for_old_dates(tmp_path):
    s = FakeSession(lambda u: Resp(404))
    c = nsefetch.NSEClient(tmp_path, FAST, session=s)
    old = date(2017, 1, 26)
    assert c.get("mto", old, today=date(2026, 11, 12))[0] == "missing"
    c2 = nsefetch.NSEClient(tmp_path, FAST, session=s)          # a later run
    assert c2.get("mto", old, today=date(2026, 11, 12))[0] == "missing"
    assert len(s.calls) == 1


def test_404_for_recent_date_is_not_remembered(tmp_path):
    s = FakeSession(lambda u: Resp(404))
    c = nsefetch.NSEClient(tmp_path, FAST, session=s)
    today = date(2026, 11, 12)
    c.get("mto", today, today=today)
    c.get("mto", today, today=today)
    assert len(s.calls) == 2            # may simply not be published yet


def test_html_block_page_with_200_is_a_failure_and_nothing_is_saved(tmp_path):
    s = FakeSession(lambda u: Resp(200, b"<html><body>Access Denied</body></html>"))
    c = nsefetch.NSEClient(tmp_path, FAST, session=s)
    with pytest.raises(RuntimeError):
        c.get("cm_legacy", date(2016, 8, 29))
    assert not list(tmp_path.rglob("*.zip"))
    assert len(s.calls) == FAST.retries


def test_rate_limit_responses_trigger_cooldown_then_give_up(tmp_path):
    s = FakeSession(lambda u: Resp(429))
    c = nsefetch.NSEClient(tmp_path, FAST, session=s)
    with pytest.raises(nsefetch.GiveUp):
        c.fetch_range(date(2016, 8, 29), date(2016, 9, 30), sources=("mto",))


def test_truncated_zip_rejected():
    with pytest.raises(ValueError):
        nsefetch.check_content("cm_legacy", b"PK\x03\x04garbage")


def test_format_switch_date():
    assert nsefetch.price_source(date(2024, 7, 5)) == "cm_legacy"
    assert nsefetch.price_source(date(2024, 7, 8)) == "cm_udiff"
    assert nsefetch.url_and_name("cm_legacy", date(2016, 8, 29))[1] == "cm29AUG2016bhav.csv.zip"


# --------------------------------------------------------------------------- parsers (real formats)

def test_parse_legacy_bhavcopy():
    df = nseparse.parse_cm_legacy(FX / "cm29AUG2016bhav.csv.zip")
    r = df[df["Symbol"] == "RELIANCE"].iloc[0]
    assert r["ISIN"] == "INE002A01018" and r["Close"] == 1057.45 and r["Volume"] == 3701495
    assert r["Date"] == pd.Timestamp("2016-08-29")
    assert set(df["Series"]) <= set(nseparse.EQUITY_SERIES)


def test_parse_udiff_bhavcopy():
    df = nseparse.parse_cm_udiff(FX / "BhavCopy_NSE_CM_0_0_0_20261111_F_0000.csv.zip")
    r = df[df["Symbol"] == "RELIANCE"].iloc[0]
    assert r["ISIN"] == "INE002A01018" and r["Close"] == 1335.40 and r["PrevClose"] == 1324.40
    assert r["Name"] == "RELIANCE INDUSTRIES LTD"


@pytest.mark.parametrize("name,day", [("MTO_29082016.DAT", "2016-08-29"), ("MTO_11112026.DAT", "2026-11-11")])
def test_parse_delivery(name, day):
    df = nseparse.parse_mto(FX / name)
    assert (df["Date"] == pd.Timestamp(day)).all()
    assert df["DelivPct"].between(0, 100).all()
    if day == "2016-08-29":
        r = df[df["Symbol"] == "RELIANCE"].iloc[0]
        assert r["DelivQty"] == 1772992 and r["DelivPct"] == 47.90


# --------------------------------------------------------------------------- corporate actions

@pytest.mark.parametrize("subject,kinds,factor", [
    ("Bonus 1:1", ["bonus"], 0.5),
    ("Bonus 1:2", ["bonus"], 2 / 3),
    ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share", ["split"], 0.1),
    ("Fv Splt Frm Rs 10 To Rs 2", ["split"], 0.2),
    ("Bonus 1:1/Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
     ["bonus", "split"], 0.1),
    ("Consolidation Of Equity Shares From Re 1 Per Share To Rs 10 Per Share", ["consolidation"], 10.0),
    ("Interim Dividend - Rs 3 Per Share", ["dividend"], None),
    ("Annual General Meeting", [], None),
    ("Buy Back", [], None),
])
def test_parse_subject(subject, kinds, factor):
    ev = corpactions.parse_subject(subject, 10.0)
    assert [e["kind"] for e in ev] == kinds
    if factor is not None:
        assert np.prod([e["factor"] for e in ev]) == pytest.approx(factor)


def _px(rows):
    return pd.DataFrame(rows, columns=["Date", "SecurityID", "ISIN", "Symbol", "Open", "Close"]).assign(
        Date=lambda d: pd.to_datetime(d["Date"]))


def test_rights_factor_is_theoretical_ex_rights():
    ev = pd.DataFrame([{"ISIN": "INE000A01011", "Symbol": "X", "ex_date": pd.Timestamp("2020-01-03"),
                        "subject": "Rights 1:4 @ Premium Rs 90/-",
                        **corpactions.parse_subject("Rights 1:4 @ Premium Rs 90/-", 10.0)[0]}])
    px = _px([("2020-01-02", "S", "INE000A01011", "X", 200, 200), ("2020-01-03", "S", "INE000A01011", "X", 185, 182)])
    out = corpactions.price_factors(ev, px)
    # issue price 100, 4 old at 200 + 1 new at 100 -> TERP 180 -> factor 0.9
    assert out["factor"].iloc[0] == pytest.approx(0.9)


def test_back_adjustment_and_raw_prices_kept():
    px = _px([("2020-01-01", "S", "I1", "X", 100, 100), ("2020-01-02", "S", "I1", "X", 102, 102),
              ("2020-01-03", "S", "I2", "X", 52, 51), ("2020-01-06", "S", "I2", "X", 52, 52)])
    px["Volume"] = [10, 10, 20, 20]
    acts = pd.DataFrame({"SecurityID": ["S"], "effective_date": [pd.Timestamp("2020-01-03")],
                         "kind": ["split"], "factor": [0.5]})
    out = nsestore.adjust(px, acts)
    assert out["AdjClose"].tolist() == [50, 51, 51, 52]
    assert out["Close"].tolist() == [100, 102, 51, 52]          # raw untouched
    assert out["AdjVolume"].tolist() == [20, 20, 20, 20]
    # dividends are not applied unless asked for
    acts2 = pd.concat([acts, pd.DataFrame({"SecurityID": ["S"], "effective_date": [pd.Timestamp("2020-01-06")],
                                           "kind": ["dividend"], "factor": [0.98]})])
    assert nsestore.adjust(px, acts2)["AdjClose"].tolist() == [50, 51, 51, 52]


def test_isin_change_on_split_links_to_one_security():
    rows = []
    for i, d in enumerate(pd.bdate_range("2020-01-01", periods=10)):
        isin = "INE019A01020" if i < 5 else "INE019A01038"
        rows.append((d, isin, "JSWSTEEL"))
        rows.append((d, "INE999Z01011", "OTHER"))
    px = pd.DataFrame(rows, columns=["Date", "ISIN", "Symbol"])
    sid = nsestore.link_isins(px)
    assert sid["INE019A01020"] == sid["INE019A01038"] == "INE019A01020"
    assert sid["INE999Z01011"] == "INE999Z01011"


def test_different_issuer_same_symbol_not_linked():
    rows = [(d, "INE111A01011", "ABC") for d in pd.bdate_range("2020-01-01", periods=3)]
    rows += [(d, "INE222B01011", "ABC") for d in pd.bdate_range("2020-01-06", periods=3)]
    sid = nsestore.link_isins(pd.DataFrame(rows, columns=["Date", "ISIN", "Symbol"]))
    assert sid["INE111A01011"] != sid["INE222B01011"]


# --------------------------------------------------------------------------- membership

def test_reconstitution_effective_dates():
    days = pd.bdate_range("2016-01-01", "2027-01-01")
    assert membership.reconstitution_start(pd.Timestamp("2017-09-01"), days) == pd.Timestamp("2017-04-03")
    assert membership.reconstitution_start(pd.Timestamp("2017-03-01"), days) == pd.Timestamp("2016-10-03")
    assert membership.reconstitution_start(pd.Timestamp("2026-11-12"), days) == pd.Timestamp("2026-10-01")


def test_parse_index_closes():
    df = nseparse.parse_indices(FX / "ind_close_all_29082016.csv")
    r = df[df["Index"] == "Nifty 500"].iloc[0]
    assert r["Close"] == 7361.00 and r["Date"] == pd.Timestamp("2016-08-29")


def test_bonus_with_hyphen_and_special_dividend():
    assert corpactions.parse_subject("Bonus- 1:2")[0]["factor"] == pytest.approx(2 / 3)
    ev = corpactions.parse_subject("Special Dividend- Rs 202 Per Share")
    assert ev[0]["kind"] == "dividend" and ev[0]["_div"] == 202


def test_unconfirmed_bonus_not_applied_and_same_day_demerger_not_double_counted():
    rows = [("2022-10-04", "S", "INE000A01011", "X", 345, 350), ("2022-10-06", "S", "INE000A01011", "X", 185, 176),
            ("2022-10-04", "T", "INE000B01011", "Y", 300, 300), ("2022-10-06", "T", "INE000B01011", "Y", 303, 301)]
    px = _px(rows)
    ann = pd.DataFrame([
        {"isin": "INE000A01011", "symbol": "X", "exDate": pd.Timestamp("2022-10-06"), "subject": "Bonus 1:2", "faceVal": 10},
        {"isin": "INE000A01011", "symbol": "X", "exDate": pd.Timestamp("2022-10-06"), "subject": "Demerger", "faceVal": 10},
        {"isin": "INE000B01011", "symbol": "Y", "exDate": pd.Timestamp("2022-10-06"), "subject": "Bonus 1:2", "faceVal": 10},
    ])
    ev = corpactions.price_factors(corpactions.events(ann), px).set_index(["Symbol", "kind"])
    total = ev.loc[("X", "bonus"), "factor"] * ev.loc[("X", "arrangement"), "factor"]
    assert total == pytest.approx(185 / 350, rel=1e-6)           # = the actual opening gap
    assert not ev.loc[("Y", "bonus"), "applied"]                  # price never moved
