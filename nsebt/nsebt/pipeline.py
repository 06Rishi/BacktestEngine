"""Phase 1 build: raw NSE files -> store, Bloomberg mapping, CA reconciliation, membership."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pandas as pd

from . import bbgmap, bloomberg, membership, nsestore
from .config import Config

log = logging.getLogger(__name__)


def bloomberg_clean(de_data: Path) -> dict[str, pd.DataFrame]:
    """Bloomberg prices with the audit's fixes: rows missing O/H/L (the synthetic IPO
    rows) are dropped by load_prices; '/F'-style artefact series are excluded."""
    b = bloomberg.load_prices(de_data)
    return {k: v for k, v in b.items() if not k.split()[0].endswith("/F")}


def run(cfg: Config, skip_store: bool = False) -> dict:
    t0 = time.monotonic()
    store = cfg.path("store")
    out = cfg.path("reports") / "phase1"
    out.mkdir(parents=True, exist_ok=True)
    rep: dict = {}

    if not skip_store:
        rep["store"] = nsestore.build(cfg.path("nse_raw"), store, cfg.path("nse_reference"),
                                      cfg.root / cfg.raw["paths"].get("ca_overrides", "ca_overrides.csv"))
        log.info("store built: %s rows, %s securities", rep["store"]["rows"], rep["store"]["securities"])

    nse = nsestore.load_prices(store, adjusted=True,
                               columns=["Date", "SecurityID", "ISIN", "Symbol", "Close", "AdjClose", "AdjFactor"])
    hist = pd.read_parquet(store / "symbol_history.parquet")

    de_data = cfg.path("bloomberg_root") / "data"
    bbg = bloomberg_clean(de_data)
    bbg_isin = None
    p = cfg.root / "bbg_isin.csv"
    if p.exists():
        bbg_isin = pd.read_csv(p, dtype=str)
    log.info("matching %d Bloomberg series to NSE securities ...", len(bbg))
    mp = bbgmap.match(bbg, nse, hist, bbg_isin)
    last_sym = hist.sort_values("last_date").groupby("SecurityID")["Symbol"].last()
    # Bloomberg IDs in the membership lists that have no Bloomberg prices (recent listings)
    cons = de_data / "raw" / "constituents"
    from .audit import normalise_member_id
    hm = pd.read_parquet(cons / "nifty500_historical_membership.parquet")
    sm = pd.read_parquet(cons / "security_master.parquet")
    when = dict(zip(normalise_member_id(hm["Ticker"]), pd.to_datetime(hm["Date"])))
    when.update(dict(zip(sm["Bloomberg_ID"].astype(str), pd.to_datetime(sm["Snapshot_Date"]))))
    extra = sorted(set(when) - set(mp["Bloomberg_ID"]) - {k for k in when if k.split()[0].endswith("/F")})
    if extra:
        nm = bbgmap.map_by_name(extra, hist, when)
        mp = pd.concat([mp, nm], ignore_index=True)
    ov_path = cfg.root / "bbg_overrides.csv"
    if ov_path.exists():
        mp = bbgmap.apply_overrides(mp, pd.read_csv(ov_path, dtype=str), hist)
    mp["NSE_Symbol"] = mp["SecurityID"].map(last_sym)
    mp.to_parquet(store / "bbg_mapping.parquet", index=False)
    mp.to_csv(out / "bbg_mapping.csv", index=False)
    rep["mapping"] = {"bloomberg_series": len(mp), "by_status": mp["status"].value_counts().to_dict(),
                      "ok_by_method": mp.loc[mp["status"] == "ok", "method"].value_counts().to_dict(),
                      "median_agreement_ok": float(mp.loc[mp["status"] == "ok", "agreement"].median())}

    log.info("reconciling corporate actions ...")
    summ, steps = bbgmap.reconcile(bbg, nse, mp)
    summ.to_csv(out / "ca_reconciliation.csv", index=False)
    steps.to_csv(out / "ca_residual_steps.csv", index=False)
    rep["reconciliation"] = {"pairs": len(summ), "pairs_flat": int((summ["n_steps"] == 0).sum()),
                             "pairs_with_steps": int((summ["n_steps"] > 0).sum()), "steps": len(steps)}

    log.info("building membership ...")
    days = pd.DatetimeIndex(sorted(nse["Date"].unique()))
    isin_to_sid = dict(zip(hist["ISIN"], hist["SecurityID"]))
    n5 = sorted(cfg.path("nse_reference").glob("ind_nifty500list_*.csv"))
    mem, mrep = membership.build(de_data / "raw" / "constituents", mp, days,
                                 n5[-1] if n5 else None, isin_to_sid)
    mem.to_parquet(store / "membership.parquet", index=False)
    rep["membership"] = mrep
    # securities that stopped trading: reclassified on every build so it follows the data end
    from . import lifecycle
    lc = lifecycle.build(store, cfg.path("nse_reference"),
                         cfg.root / cfg.raw["paths"].get("mergers", "mergers.csv"),
                         cfg.root / cfg.raw["paths"].get("lifecycle_overrides", "lifecycle_overrides.csv"))
    rep["lifecycle"] = lc["cls"].value_counts().to_dict()
    rep["seconds"] = round(time.monotonic() - t0, 1)
    (out / "phase1_summary.json").write_text(json.dumps(rep, indent=2, default=str))
    return rep
