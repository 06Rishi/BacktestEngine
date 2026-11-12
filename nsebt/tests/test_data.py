from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nsebt import bhavcopy, mapping, panel, store

FIX = Path(__file__).parent / "fixtures" / "sec_bhavdata_full_12112026.csv"
TEXT = FIX.read_text()


def test_parse_real_file():
    df = bhavcopy.parse(FIX)
    assert set(df["Series"]) <= {"EQ", "BE", "BZ"}
    assert df["Date"].nunique() == 1 and df["Date"].iloc[0] == pd.Timestamp("2026-11-12")
    assert not df.duplicated(["Date", "Symbol"]).any()
    rel = df[df.Symbol == "RELIANCE"].iloc[0]
    assert (rel.Open, rel.High, rel.Low, rel.Close) == (1335.4, 1342.5, 1330.9, 1339.7)
    assert rel.Volume == 13807921 and rel.DelivPct == pytest.approx(53.29)
    mm = df[df.Symbol == "M&M"].iloc[0]          # symbol with '&' survives
    assert mm.Close == 3399.0
    # '-' delivery values become NaN, not errors
    assert df["DelivPct"].isna().any() or df["DelivPct"].notna().all()


def test_parse_rejects_wrong_file(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("A,B\n1,2\n")
    with pytest.raises(ValueError):
        bhavcopy.parse(p)


class FakeResp:
    def __init__(self, status, text=""):
        self.status_code, self.content = status, text.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    """Serves the fixture for 12 Nov 2026 (relabelled for other days), 404 for 13 Nov."""
    def __init__(self):
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        if "13112026" in url:
            return FakeResp(404)
        if "16112026" in url:
            return FakeResp(200, "<html>Access denied</html>")
        ddmmyyyy = url.rsplit("_", 1)[1][:8]
        d = date(int(ddmmyyyy[4:]), int(ddmmyyyy[2:4]), int(ddmmyyyy[:2]))
        return FakeResp(200, TEXT.replace("12-Nov-2026", f"{d:%d-%b-%Y}"))


def test_update_handles_holiday_bad_content_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(bhavcopy.time, "sleep", lambda s: None)
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    raw, st, s = tmp_path / "raw", tmp_path / "store", FakeSession()
    # Wed 11 .. Mon 16 Nov: 14-15 weekend, 13 = 404, 16 = HTML page
    rep = store.update(raw, st, date(2026, 11, 11), date(2026, 11, 16), session=s)
    assert rep.fetched == [date(2026, 11, 11), date(2026, 11, 12)]
    assert rep.no_file == [date(2026, 11, 13)]
    assert [d for d, _ in rep.failed] == [date(2026, 11, 16)]
    assert not any("14112026" in u or "15112026" in u for u in s.calls)   # weekends skipped
    df = store.load(st)
    n = len(df)
    assert df["Date"].nunique() == 2
    # re-run: cached raw files, no duplicate rows
    calls_before = len(s.calls)
    store.update(raw, st, date(2026, 11, 11), date(2026, 11, 12), session=s)
    assert len(s.calls) == calls_before
    assert len(store.load(st)) == n
    assert store.last_date(st) == date(2026, 11, 12)


def test_mapping_methods_and_overlap_validation(tmp_path):
    ov = tmp_path / "ov.csv"
    ov.write_text("Bloomberg_ID,NSE_Symbol\nMM IN Equity,M&M\n")
    ids = ["RELIANCE IN Equity", "MM IN Equity", "ZZZ IN Equity", "SBIN IN Equity"]
    mp = mapping.build(ids, ["RELIANCE", "M&M", "SBIN"], overrides_csv=ov)
    got = dict(zip(mp.Bloomberg_ID, zip(mp.NSE_Symbol, mp.Method)))
    assert got["RELIANCE IN Equity"] == ("RELIANCE", "direct")
    assert got["MM IN Equity"] == ("M&M", "override")
    assert got["ZZZ IN Equity"][1] == "unmapped"

    idx = pd.to_datetime(["2026-08-26", "2026-08-27"])
    bbg = {"RELIANCE IN Equity": pd.DataFrame({"Close": [100.0, 101.0]}, index=idx),
           "SBIN IN Equity": pd.DataFrame({"Close": [100.0, 101.0]}, index=idx),
           "MM IN Equity": pd.DataFrame({"Close": [100.0, 101.0]}, index=idx)}
    nse = pd.DataFrame({"Date": list(idx) * 2 + [pd.Timestamp("2026-11-12")],
                        "Symbol": ["RELIANCE"] * 2 + ["SBIN"] * 2 + ["M&M"],
                        "Close": [100.0, 101.0, 50.0, 50.5, 3399.0]})
    v = mapping.validate_overlap(mp, bbg, nse).set_index("Bloomberg_ID")["Status"]
    assert v["RELIANCE IN Equity"] == "ok"
    assert v["SBIN IN Equity"] == "price_mismatch"    # wrong match gets caught
    assert v["MM IN Equity"] == "no_overlap"


def test_mapping_rejects_two_ids_on_one_symbol(tmp_path):
    ov = tmp_path / "ov.csv"
    ov.write_text("Bloomberg_ID,NSE_Symbol\nB IN Equity,A\n")
    with pytest.raises(ValueError):
        mapping.build(["A IN Equity", "B IN Equity"], ["A"], overrides_csv=ov)


def test_combine_appends_only_after_bloomberg_end_and_respects_trust():
    cols = ["Open", "High", "Low", "Close", "Volume"]
    idx = pd.to_datetime(["2026-08-26", "2026-08-27"])
    bbg = {"X IN Equity": pd.DataFrame(1.0, index=idx, columns=cols),
           "Y IN Equity": pd.DataFrame(1.0, index=idx, columns=cols)}
    nse = pd.DataFrame({"Date": pd.to_datetime(["2026-08-27", "2026-08-28", "2026-08-28"]),
                        "Symbol": ["X", "X", "Y"], **{c: [2.0, 3.0, 3.0] for c in cols}})
    mp = pd.DataFrame({"Bloomberg_ID": ["X IN Equity", "Y IN Equity"], "NSE_Symbol": ["X", "Y"]})
    out = panel.combine(bbg, nse, mp, trusted={"X IN Equity"})
    x = out["X IN Equity"]
    assert list(x.index) == list(pd.to_datetime(["2026-08-26", "2026-08-27", "2026-08-28"]))
    assert x.loc["2026-08-27", "Close"] == 1.0      # Bloomberg wins on overlap
    assert len(out["Y IN Equity"]) == 2              # untrusted: not extended
