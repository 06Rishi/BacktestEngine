"""Ledger-vs-adjusted reconciliation (amendment 2.6), run for every security in one pass.

(a) Reinvest-equivalent check. A costless buy-and-hold of every security goes through the engine
    in `reconcile` mode: each value event (dividend, rights, demerger) is reinvested as shares ÷
    factor, share events divide shares by the factor, and fractional shares are kept. The value
    held, shares × raw close, must then move exactly like the total-return adjusted close
    (kinds = PRICE_KINDS + dividend). Each day's return is compared to 1e-6. This catches factor-
    direction errors, missed or double-counted events, wrong effective dates and ISIN breaks.

(b) Receivable-mode check. The default engine (dividends, rights and demergers as receivables)
    must preserve value on every ex-date: shares_before × previous close = shares_after ×
    previous close × factor + amount credited, to 1e-6.

Why (a) and not the receivable engine against the adjusted series directly: the adjusted series
reinvests each dividend at the ex-date price, while the engine holds it as cash. The two differ by
about D/P × the next day's return (~1e-4 per ex-date), so a 1e-6 daily tolerance can't hold for the
receivable engine. (a) and (b) together check the same things without that drift.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .account import Account
from .costs import CostModel
from .sim import Market, SimConfig, Signals, Simulator

FREE = CostModel(stt_pct=0, transaction_pct=0, sebi_per_crore=0, gst_pct=0, stamp_pct_buy=0,
                 dp_per_scrip_sell_day=0, slippage_bps_buy=0, slippage_bps_sell=0)
TOL = 1e-6
FAIL_SHARE = 0.005          # more than 0.5% of securities failing -> stop rule S4
SHARES = 10_000             # large enough that no consolidation leaves less than one share


@dataclass
class Reconciliation:
    summary: pd.DataFrame        # one row per security: (a) days checked, max error, failing days
    failures: pd.DataFrame       # (a) failing days with the events between the two closes
    preservation: pd.DataFrame   # (b) one row per value event: value before/after, error
    excluded: pd.DataFrame       # events with no factor (not in the adjusted series either)
    seconds: dict

    @property
    def n(self) -> int:
        return len(self.summary)

    @property
    def n_failed(self) -> int:
        return int((self.summary["fail_days"] > 0).sum())

    @property
    def fail_share(self) -> float:
        return self.n_failed / self.n if self.n else 0.0

    @property
    def b_failed(self) -> int:
        return int((self.preservation["abs_err"] > TOL).sum()) if len(self.preservation) else 0

    @property
    def passed(self) -> bool:
        return self.fail_share <= FAIL_SHARE and self.b_failed == 0


def _hold_all(market: Market, reconcile: bool) -> Simulator:
    D, N = market.close.shape
    cfg = SimConfig(account=Account(capital=1.0, max_positions=1), costs=FREE, reconcile=reconcile)
    sim = Simulator(market, Signals(entry=np.zeros((D, N), bool)), cfg, track=True)
    first = np.argmax(np.isfinite(market.close), axis=0)
    for j in range(N):
        if np.isfinite(market.close[first[j], j]):
            sim.inject(int(first[j]), j, float(SHARES))
    return sim


def check_returns(sim: Simulator, adj_close: np.ndarray, tol: float = TOL):
    """(a): compare each day's held-value return with the adjusted close's return."""
    m = sim.m
    rows, fails = [], []
    for j in range(m.close.shape[1]):
        ok = np.isfinite(m.close[:, j]) & np.isfinite(adj_close[:, j]) & np.isfinite(sim.hold[:, j]) \
            & (sim.hold[:, j] > 0)
        t = np.flatnonzero(ok)
        if len(t) < 2:
            continue
        cred = np.cumsum(sim.credit[:, j])
        a, b = t[:-1], t[1:]
        r_eng = (sim.hold[b, j] + cred[b] - cred[a]) / sim.hold[a, j]
        r_adj = adj_close[b, j] / adj_close[a, j]
        err = r_eng / r_adj - 1
        bad = np.abs(err) > tol
        rows.append({"SecurityID": m.ids[j], "first": m.dates[t[0]], "last": m.dates[t[-1]], "days": len(b),
                     "max_abs_err": float(np.abs(err).max()), "fail_days": int(bad.sum())})
        for k in np.flatnonzero(bad):
            fails.append({"SecurityID": m.ids[j], "from": m.dates[a[k]], "to": m.dates[b[k]],
                          "engine_return": r_eng[k], "adjusted_return": r_adj[k], "err": err[k]})
    return pd.DataFrame(rows), pd.DataFrame(fails, columns=["SecurityID", "from", "to", "engine_return",
                                                            "adjusted_return", "err"])


def check_preservation(ledger: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(b): value events in receivable mode keep the holding's value on the ex-date."""
    if ledger.empty:
        return ledger, ledger
    v = ledger[ledger["kind"].isin(["dividend", "special_dividend", "rights", "arrangement"])].copy()
    excluded = v[~np.isfinite(v["factor"].astype(float))]
    v = v[np.isfinite(v["factor"].astype(float)) & np.isfinite(v["prev_close"].astype(float))]
    before = v["shares_before"] * v["prev_close"]
    after = v["shares_after"] * v["prev_close"] * v["factor"] + v["amount"]
    v["value_before"], v["value_after"] = before, after
    v["abs_err"] = (after / before - 1).abs()
    return v, excluded


def attach_events(fails: pd.DataFrame, ledger: pd.DataFrame) -> pd.DataFrame:
    """Name the corporate actions dated between the two closes of each failing day."""
    if fails.empty:
        return fails
    out = fails.copy()
    lg = ledger.groupby("SecurityID") if len(ledger) else None
    ev = []
    for r in out.itertuples(index=False):
        if lg is None or r.SecurityID not in lg.groups:
            ev.append("")
            continue
        g = lg.get_group(r.SecurityID)
        g = g[(g["date"] > r._1) & (g["date"] <= r.to)]
        ev.append("; ".join(f"{k} {d}" for k, d in zip(g["kind"], g["detail"])))
    out["events"] = ev
    return out


def reconcile(market: Market, adj_close: np.ndarray, tol: float = TOL) -> Reconciliation:
    t0 = time.perf_counter()
    sim_a = _hold_all(market, reconcile=True)
    res_a = sim_a.run()
    t1 = time.perf_counter()
    summary, fails = check_returns(sim_a, adj_close, tol)
    fails = attach_events(fails, res_a.ledger)
    t2 = time.perf_counter()
    sim_b = _hold_all(market, reconcile=False)
    res_b = sim_b.run()
    pres, excluded = check_preservation(res_b.ledger)
    t3 = time.perf_counter()
    return Reconciliation(summary, fails, pres, excluded,
                          {"reinvest_run": t1 - t0, "compare": t2 - t1, "receivable_run": t3 - t2})


def from_store(store, universe="nifty500") -> tuple[Market, np.ndarray, float]:
    """Market (raw prices + corporate actions) and the total-return adjusted close, aligned."""
    from pathlib import Path
    from ..data import load_panel
    from ..nsestore import PRICE_KINDS
    t0 = time.perf_counter()
    panel = load_panel(Path(store), universe, kinds=PRICE_KINDS + ("dividend",))
    ca = pd.read_parquet(Path(store) / "corporate_actions.parquet")
    m = Market.from_panel(panel, actions=ca)
    adj = panel.close.reindex(index=m.dates, columns=m.ids).to_numpy(float)
    return m, adj, time.perf_counter() - t0
