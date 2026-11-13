"""Engine and data validation (amendment 2.6), run for every security in one pass.

What each check proves, and what it does not:

(a) Engine mechanics. A costless buy-and-hold of every security runs in `reconcile` mode (each
    value event reinvested as shares ÷ factor, share events applied, fractions kept). The value
    held must move exactly like the total-return adjusted close, day by day, to 1e-6. The engine
    and the adjusted series read the SAME corporate-action table, so this proves the engine
    applies the table correctly (scheduling, factor direction, same-day compounding, entitlement
    timing). It cannot detect an error in the table itself or in the store's ISIN linking.

(b) Receivable mode. On each ex-date: shares_before × previous close = shares_after × previous
    close × factor + amount credited. Because the amount is derived from the factor, this can
    only fail when the table's prior close disagrees with the market's previous close, or when
    the receivable code path drops or double-books an amount. It is a code-path check.

(c) Table against prices (independent of the table's own logic). For every member event-day with
    a price-moving action, the combined factor is compared with the actual ex-date open and close
    moves. Days where both differ by more than 10% are listed for review. This is what catches
    a wrong or missing factor (it found the IDEA rights issue and the sub-3% demergers).

(d) Full ledger run. Every member is held from listing through its splits, dividends, rights,
    demergers, conversions (mergers.csv) and lifecycle exits (delisted, suspended), with real
    acquirer prices, costs off. At the end of the run, equity must equal capital plus realised and
    unrealised P&L to 1e-6; every warning is listed. (Daily identity, costs and negative-cash
    behaviour are covered by the randomised tests in test_m2_review_fixes.py.)

Why (a) and not the receivable engine against the adjusted series directly: the adjusted series
reinvests each dividend at the ex-date price, while the engine holds it as cash. The two differ by
about D/P × the next day's return (~1e-4 per ex-date). Approved by the user at Gate A.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .account import Account
from .costs import CostModel
from .eligibility import EligibilityConfig
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
    cfg = SimConfig(account=Account(capital=1.0, max_positions=1), costs=FREE, reconcile=reconcile,
                    eligibility=EligibilityConfig(min_price=0.0, min_adv=0.0, adv_days=market.adv_days))
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


def from_store(store, universe="nifty500", extra=()) -> tuple[Market, np.ndarray, float]:
    """Market (raw prices + corporate actions) and the total-return adjusted close, aligned."""
    from pathlib import Path
    from ..data import load_panel
    from ..nsestore import PRICE_KINDS
    t0 = time.perf_counter()
    panel = load_panel(Path(store), universe, kinds=PRICE_KINDS + ("dividend",), extra=extra)
    ca = pd.read_parquet(Path(store) / "corporate_actions.parquet")
    m = Market.from_panel(panel, actions=ca)
    adj = panel.close.reindex(index=m.dates, columns=m.ids).to_numpy(float)
    return m, adj, time.perf_counter() - t0


PRICE_KINDS_CHECKED = ("split", "bonus", "consolidation", "inferred_split", "rights", "arrangement",
                       "special_dividend")
TABLE_TOL = 0.10


def check_table(ca: pd.DataFrame, member_ids, first_day=None, tol: float = TABLE_TOL) -> tuple[pd.DataFrame, int]:
    """(c): event-days where the combined factor disagrees with both the ex-date open gap and the
    close move by more than `tol`. Returns (flagged rows, event-days checked)."""
    c = ca[ca["applied"].astype(bool) & ca["SecurityID"].isin(set(member_ids)) & ca["effective_date"].notna()
           & ca["kind"].isin(PRICE_KINDS_CHECKED)]
    if first_day is not None:
        c = c[c["effective_date"] > pd.Timestamp(first_day)]
    g = c.groupby(["SecurityID", "effective_date"]).agg(
        Symbol=("Symbol", "first"), kinds=("kind", lambda x: "+".join(x)), factor=("factor", "prod"),
        prior_close=("prior_close", "first"), ex_open=("ex_open", "first"), ex_close=("ex_close", "first")
    ).reset_index()
    g["open_gap"], g["close_move"] = g["ex_open"] / g["prior_close"], g["ex_close"] / g["prior_close"]
    lim = np.log(1 + tol)
    bad = g[(np.log(g["open_gap"] / g["factor"]).abs() > lim) & (np.log(g["close_move"] / g["factor"]).abs() > lim)]
    return bad.reset_index(drop=True), len(g)


def full_ledger_run(market: Market, member_ids, shares: float = 1000.0):
    """(d): hold every member from its first trade through every corporate action, conversion and
    lifecycle exit in the default (receivable) engine. Returns (result, accounting error, min cash)."""
    from .eligibility import EligibilityConfig
    D, N = market.close.shape
    cfg = SimConfig(account=Account(capital=1.0, max_positions=1), costs=FREE,
                    eligibility=EligibilityConfig(min_price=0.0, min_adv=0.0, adv_days=market.adv_days))
    sim = Simulator(market, Signals(entry=np.zeros((D, N), bool)), cfg)
    first = np.argmax(np.isfinite(market.close), axis=0)
    mem = set(member_ids)
    injected = 0.0                    # injected holdings are not paid for: their cost is added back
    for j in range(N):
        if market.ids[j] in mem and np.isfinite(market.close[first[j], j]):
            sim.inject(int(first[j]), j, shares)
            injected += shares * market.close[first[j], j]
    r = sim.run()
    unreal = sum(p.shares * p.last_close + p.income - p.cost_basis - p.buy_charges.total for p in sim._held())
    realised = float(r.trades["net_pnl"].sum()) if len(r.trades) else 0.0
    err = float(r.daily["equity"].iloc[-1] - 1.0 - injected - realised - unreal)
    return r, err, float(r.daily["cash"].min())
