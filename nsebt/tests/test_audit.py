"""Checks that the Phase 0 audit detects what it claims to detect, on hand-built data."""
import numpy as np
import pandas as pd

from nsebt import audit


def _series(ticker, closes, start="2024-01-01", vol=1000.0, ipo_row=False):
    d = pd.bdate_range(start, periods=len(closes))
    c = np.asarray(closes, float)
    df = pd.DataFrame({"File": f"{ticker}.parquet", "Ticker": ticker, "Date": d,
                       "PX_OPEN": c, "PX_HIGH": c * 1.01, "PX_LOW": c * 0.99,
                       "PX_LAST": c, "PX_VOLUME": vol})
    if ipo_row:
        df.loc[0, ["PX_OPEN", "PX_HIGH", "PX_LOW", "PX_VOLUME"]] = np.nan
    return df


def _calendar(px):
    return audit.market_calendar(px, min_series=1)


def test_nearest_ca_matches_clean_ratios_only():
    assert audit.nearest_ca(0.5, 0.04)[1] == 0.5
    assert audit.nearest_ca(0.205, 0.04)[1] == 0.2
    assert audit.nearest_ca(0.55, 0.04)[0] == ""          # 45% fall, not a CA ratio


def test_unadjusted_split_is_labelled_corporate_action():
    closes = [100.0] * 30 + [50.0] * 30                    # 1:2 split, price halves
    px = _series("AAA IN Equity", closes)
    px.loc[30:, "PX_VOLUME"] = 2000.0                      # volume doubles after a split
    m = audit.classify_moves(px, audit.AuditConfig(), _calendar(px))
    assert len(m) == 1
    assert m.iloc[0]["label"] == "ca_ratio_with_volume_shift"
    assert m.iloc[0]["ca_ratio"] == 0.5


def test_ipo_reference_row_and_reversal_and_real_move():
    ipo = _series("IPO IN Equity", [100.0] + [180.0] * 10, ipo_row=True)
    spike = _series("SPK IN Equity", [100.0] * 10 + [140.0] + [100.0] * 10)
    real = _series("REAL IN Equity", [100.0] * 10 + [70.0] * 10)
    px = pd.concat([ipo, spike, real], ignore_index=True)
    m = audit.classify_moves(px, audit.AuditConfig(), _calendar(px)).set_index("Ticker")
    assert m.loc["IPO IN Equity", "label"] == "ipo_reference_row"
    assert (m.loc["SPK IN Equity", "label"] == "spike_reversed").all()
    assert m.loc["REAL IN Equity", "label"] == "real_move"


def test_price_checks_find_violations_gaps_and_missing():
    a = _series("AAA IN Equity", [100.0] * 40)
    b = _series("BBB IN Equity", [100.0] * 40)
    b = b.drop(index=range(10, 20))                         # 10-day hole
    a.loc[5, "PX_HIGH"] = 90.0                               # high below close
    a.loc[6, ["PX_OPEN", "PX_HIGH", "PX_LOW"]] = np.nan      # close only
    a = pd.concat([a, a.iloc[[7]]])                          # duplicate date
    px = pd.concat([a, b]).sort_values(["Ticker", "Date"]).reset_index(drop=True)
    cal = _calendar(px)
    tabs, per = audit.price_checks(px, cal, audit.AuditConfig(), px["Date"].max())
    per = per.set_index("Ticker")
    assert per.loc["AAA IN Equity", "ohlc_violations"] == 1
    assert per.loc["AAA IN Equity", "missing_ohl"] == 1
    assert per.loc["AAA IN Equity", "duplicate_rows"] == 2
    assert per.loc["BBB IN Equity", "gaps_gt_limit"] == 1
    assert tabs["gaps"].iloc[0]["missing_days"] == 10


def test_member_id_normalisation():
    s = pd.Series(["RELIANCE IS", "360ONE IN", "M&M IS "])
    assert audit.normalise_member_id(s).tolist() == [
        "RELIANCE IN Equity", "360ONE IN Equity", "M&M IN Equity"]


def test_recomputed_features_are_trailing_only():
    """Truncating the input at day t must not change any feature value up to t."""
    rng = np.random.default_rng(1)
    n = 400
    df = pd.DataFrame({"Date": pd.bdate_range("2020-01-01", periods=n),
                       "PX_LAST": 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n))),
                       "PX_VOLUME": rng.integers(1e3, 1e5, n).astype(float)})
    full = audit.recompute_features(df).set_index("Date")
    for t in (50, 260, 399):
        part = audit.recompute_features(df.iloc[:t]).set_index("Date")
        pd.testing.assert_frame_equal(part, full.iloc[:t])
