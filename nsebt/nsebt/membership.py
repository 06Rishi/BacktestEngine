"""Point-in-time Nifty 500 membership keyed by SecurityID.

Sources, in date order:
  * Bloomberg snapshots (INDX_MWEIGHT_HIST) on 1 March and 1 September, 2016-2026
  * Bloomberg current master (2026-08-27)
  * NSE's own current constituent list (ind_nifty500list.csv, dated when downloaded)

Effective dates. NSE reconstitutes the index twice a year, effective after the last
trading day of March and of September. A snapshot therefore reflects the most recent
reconstitution before it, and is valid from that reconstitution until the next one:
  snapshot in Apr-Sep of year Y -> effective from the first trading day of April Y
  snapshot in Oct-Mar           -> effective from the first trading day of October
Mid-period changes (mergers, replacements) between reconstitutions are not captured.

Repair. The 2018-09-01 Bloomberg snapshot is incomplete (338 members). Stocks that
were members in both neighbouring snapshots are added back and flagged `repaired`.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .audit import normalise_member_id

BROKEN_SNAPSHOTS = ("2018-09-01",)


def reconstitution_start(snapshot: pd.Timestamp, trading_days: pd.DatetimeIndex) -> pd.Timestamp:
    s = pd.Timestamp(snapshot)
    if 4 <= s.month <= 9:
        start = pd.Timestamp(s.year, 4, 1)
    elif s.month >= 10:
        start = pd.Timestamp(s.year, 10, 1)
    else:
        start = pd.Timestamp(s.year - 1, 10, 1)
    i = trading_days.searchsorted(start)
    return trading_days[i] if i < len(trading_days) else start


def build(constituents_dir: Path, mapping: pd.DataFrame, trading_days: pd.DatetimeIndex,
          nse_list: Path | None = None, isin_to_sid: dict | None = None) -> tuple[pd.DataFrame, dict]:
    c = Path(constituents_dir)
    m = pd.read_parquet(c / "nifty500_historical_membership.parquet")
    m = pd.DataFrame({"snapshot": pd.to_datetime(m["Date"]), "Bloomberg_ID": normalise_member_id(m["Ticker"]),
                      "source": "bloomberg_hist"})
    sm = pd.read_parquet(c / "security_master.parquet")
    m = pd.concat([m, pd.DataFrame({"snapshot": pd.to_datetime(sm["Snapshot_Date"]),
                                    "Bloomberg_ID": sm["Bloomberg_ID"].astype(str).str.strip(),
                                    "source": "bloomberg_current"})], ignore_index=True)
    m["repaired"] = False

    snaps = sorted(m["snapshot"].unique())
    rep = {"repaired": {}}
    for b in BROKEN_SNAPSHOTS:
        b = pd.Timestamp(b)
        if b not in snaps:
            continue
        i = snaps.index(b)
        before = set(m.loc[m["snapshot"] == snaps[i - 1], "Bloomberg_ID"])
        after = set(m.loc[m["snapshot"] == snaps[i + 1], "Bloomberg_ID"])
        have = set(m.loc[m["snapshot"] == b, "Bloomberg_ID"])
        add = sorted((before & after) - have)
        m = pd.concat([m, pd.DataFrame({"snapshot": b, "Bloomberg_ID": add, "source": "bloomberg_hist",
                                        "repaired": True})], ignore_index=True)
        rep["repaired"][str(b.date())] = {"had": len(have), "added": len(add), "now": len(have) + len(add)}

    ok = mapping[mapping["status"].isin(["ok", "ok_name_only", "ok_override"])].set_index("Bloomberg_ID")["SecurityID"]
    m["SecurityID"] = m["Bloomberg_ID"].map(ok)

    if nse_list and Path(nse_list).exists():
        n = pd.read_csv(nse_list, dtype=str)
        stamp = pd.Timestamp(Path(nse_list).stem.split("_")[-1])
        n = pd.DataFrame({"snapshot": stamp, "Bloomberg_ID": None, "source": "nse_current",
                          "repaired": False, "ISIN": n["ISIN Code"].str.strip(), "Symbol": n["Symbol"].str.strip()})
        n["SecurityID"] = n["ISIN"].map(isin_to_sid or {})
        rep["nse_current_unmatched"] = n.loc[n["SecurityID"].isna(), "Symbol"].tolist()
        m = pd.concat([m, n.drop(columns=["ISIN", "Symbol"])], ignore_index=True)

    m["effective_from"] = m["snapshot"].map(lambda s: reconstitution_start(s, trading_days))
    # snapshots that share a reconstitution period: keep the latest snapshot of the period
    latest = m.groupby("effective_from")["snapshot"].transform("max")
    m = m[m["snapshot"] == latest]
    eff = sorted(m["effective_from"].unique())
    nxt = dict(zip(eff, eff[1:] + [pd.NaT]))
    m["effective_to"] = m["effective_from"].map(nxt)       # exclusive; NaT = still current

    per = m.groupby("snapshot").agg(members=("Bloomberg_ID", "size"),
                                    mapped=("SecurityID", lambda s: s.notna().sum()),
                                    source=("source", "first"),
                                    effective_from=("effective_from", "first"))
    rep["snapshots"] = per.reset_index().astype(str).to_dict("records")
    rep["unmapped_members"] = sorted(m.loc[m["SecurityID"].isna() & m["Bloomberg_ID"].notna(),
                                           "Bloomberg_ID"].unique().tolist())
    return m.reset_index(drop=True), rep
