"""nsebt: NSE research and backtesting platform.

Data
  update     daily: fetch new NSE files, rebuild the store, write a run report
  backfill   download NSE files for a date range (polite, resumable)
  ingest     add NSE files you downloaded yourself in a browser
  build      rebuild the store, Bloomberg mapping and membership from the raw files
  audit      audit the Bloomberg extract (Phase 0)
  check-indicators   compare every indicator with TA-Lib on all Nifty 500 securities

Account
  account    set capital, maximum positions, position cap and liquidity limit (asks for each value)

Safety
  backup     copy everything that cannot be rebuilt to a backup folder (checksummed)
  restore    verify a backup and restore missing or changed files
  doctor     check the environment, config, data, store and (optionally) a backup

Research (legacy: runs on the Bloomberg extract until the Phase 5 engine)
  backtest   run a strategy
  signals    what a strategy says as of the latest data

All commands read config.yaml from the current folder or a parent.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path


def _date(s):
    return date.fromisoformat(s)


def _cfg(a):
    from . import config
    return config.load(a.config)


def _log(cfg, name):
    from . import nsefetch
    nsefetch.setup_logging(cfg.path("logs") / f"{name}_{nsefetch.stamp()}.log")


# --------------------------------------------------------------------------- data

def cmd_update(a):
    from . import update
    cfg = _cfg(a)
    _log(cfg, "update")
    rep = update.run(cfg, start=a.start, end=a.end, build=not a.no_build)
    print(update.one_line(rep))
    if rep["failed"] or rep.get("stopped"):
        sys.exit(1)


def cmd_backfill(a):
    from . import nsefetch
    cfg = _cfg(a)
    _log(cfg, "backfill")
    start = a.start or cfg.backfill_start
    end = a.end or date.today()
    cli = nsefetch.NSEClient(cfg.path("nse_raw"), cfg.politeness)
    if not a.skip_reference:
        ref = cli.fetch_reference(cfg.path("nse_reference"))
        print("reference files:", ", ".join(p.name for p in ref.values()))
    try:
        res = cli.fetch_range(start, end, sources=tuple(a.sources.split(",")), extra_dates=cfg.extra_dates(end))
    except nsefetch.GiveUp as e:
        sys.exit(f"stopped: {e}\nEverything fetched so far is cached; rerun the same command later to resume.")
    print(res.summary())
    for s, d, e in res.failed[:20]:
        print(f"  failed {s} {d}: {e}")


def cmd_ingest(a):
    from . import nsefetch
    cfg = _cfg(a)
    res = nsefetch.ingest(a.files, cfg.path("nse_raw"))
    for name, r in res:
        print(f"  {name}: {r}")
    added = sum(r == "added" for _, r in res)
    print(f"{added} file(s) added" + ("; run `nsebt build` to update the store" if added else ""))


def cmd_build(a):
    from . import pipeline
    cfg = _cfg(a)
    _log(cfg, "build")
    rep = pipeline.run(cfg, skip_store=a.skip_store)
    s = rep.get("store", {})
    if s:
        print(f"store: {s['rows']:,} rows, {s['securities']} securities ({s['active_securities']} active), "
              f"{s['first_date']} to {s['last_date']}, {s['corporate_actions']} corporate actions")
    print("mapping:", rep["mapping"]["by_status"])
    print(f"done in {rep['seconds']}s; details in reports/phase1/")


def cmd_audit(a):
    from . import audit
    s = audit.run(a.de_root, a.out, nse_bhavcopy=a.bhavcopy)
    p = s["prices"]
    print(f"{p['tickers']} series, {p['rows']:,} rows, {p['date_min']} to {p['date_max']}; "
          f"{p['big_moves']} moves over 20% ({p['big_move_labels']})")


def cmd_check_indicators(a):
    from . import data, refcheck
    cfg = _cfg(a)
    p = data.load_panel(cfg.path("store"), a.universe)
    out = cfg.path("reports") / "phase3"
    df = refcheck.run(p, out)
    refcheck.time_library(p).to_csv(out / "timings.csv", index=False)
    worst = df["max_rel_diff"].max()
    print(df[["indicator", "values_compared", "max_rel_diff", "first_valid_mismatches"]].to_string(index=False))
    print(f"{len(df)} indicators, {df['values_compared'].sum():,} values; worst relative difference {worst:.1e}; "
          f"written to {out}")


# --------------------------------------------------------------------------- safety

def cmd_backup(a):
    from . import maintenance
    cfg = _cfg(a)
    dests = [a.dest] if a.dest else [Path(d) for d in cfg.raw.get("backup", {}).get("destinations", [])]
    if not dests:
        sys.exit("no destination: pass --dest or set backup.destinations in config.yaml")
    bad = False
    for d in dests:
        if not Path(d).exists():
            print(f"  {d}: not available (drive not mounted?) - skipped")
            bad = True
            continue
        r = maintenance.backup(cfg, Path(d))
        print(f"  {r['dest']}: {r['files']} files ({r['bytes'] / 1e6:.0f} MB), {r['copied']} copied, "
              f"git bundle {'ok' if r['git_bundle'] else 'FAILED ' + r['git_error']}")
    sys.exit(1 if bad else 0)


def cmd_restore(a):
    from . import config, maintenance
    root = config.find().parent if not a.to else Path(a.to)
    r = maintenance.restore(root, a.src, apply=a.apply)
    v = r["verified"]
    print(f"backup {v['root']} from {v['created']}: {v['files']} files, "
          f"{'verified' if v['ok'] else 'FAILED verification'} (missing {len(v['missing'])}, corrupt {len(v['corrupt'])})")
    if "to_restore" in r:
        print(f"{r['to_restore']} file(s) {'restored' if r['applied'] else 'would be restored (add --apply)'}")
    if v["git_bundle"]:
        print(f"code: git clone {v['root']}/repo.bundle <folder>   (if you have no GitHub access)")


def cmd_doctor(a):
    from . import maintenance
    cfg = _cfg(a)
    rows = maintenance.doctor(cfg, a.backup, a.tests)
    w = max(len(r[0]) for r in rows)
    for name, status, detail in rows:
        print(f"  {status:4}  {name:<{w}}  {detail}")
    fails = sum(r[1] == "FAIL" for r in rows)
    print(f"{fails} problem(s)" if fails else "all checks passed")
    sys.exit(1 if fails else 0)


# --------------------------------------------------------------------------- account

def cmd_account(a):
    from . import config
    from .backtest import account
    path = Path(a.config) if a.config else config.find()
    cfg = config.load(path)
    cur = account.from_config(cfg.raw)
    flags = {k: getattr(a, k) for k in account.LIMITS if getattr(a, k) is not None}
    try:
        if a.show:
            print(account.Account().describe() + "  (defaults; nothing saved yet)" if cur is None else cur.describe())
            return
        if flags:
            new = (cur or account.Account()).with_overrides(**{k: account.parse(k, v) for k, v in flags.items()})
        else:
            print("Account limits. Press Enter to keep the value in brackets.")
            new = account.prompt(cur)
    except account.AccountError as e:
        sys.exit(f"error: {e}")
    account.save(path, new)
    print(f"saved to {path}: {new.describe()}")


def cmd_mergers(a):
    """Seed mergers.csv with candidates and refresh price-implied ratios of filled rows."""
    import pandas as pd
    from .backtest import mergers
    from .corpactions import load_announcements
    cfg = _cfg(a)
    store, path = cfg.path("store"), cfg.root / cfg.raw["paths"].get("mergers", "mergers.csv")
    table = mergers.read(path)
    if a.seed:
        sec = pd.read_parquet(store / "securities.parquet")
        mem = pd.read_parquet(store / "membership.parquet")["SecurityID"].dropna()
        cal = pd.DatetimeIndex(pd.read_parquet(store / "prices", columns=["Date"])["Date"].unique())
        ann = load_announcements(cfg.path("nse_raw") / "corp_actions")
        before = len(table)
        table = mergers.merge_seed(table, mergers.candidates(sec, mem, cal, ann))
        print(f"{len(table) - before} candidate row(s) added")
    ids = set(table["parent"]) | {x.strip() for x in table["acquirer"] if str(x).strip()}
    px = pd.read_parquet(store / "prices", columns=["Date", "SecurityID", "Close"],
                         filters=[("SecurityID", "in", sorted(ids))])
    close = px.pivot_table(index="Date", columns="SecurityID", values="Close").sort_index()
    table = mergers.refresh_implied(table, close)
    table.to_csv(path, index=False)
    filled = table[table["acquirer"].str.strip() != ""]
    print(f"{path}: {len(table)} rows, {len(filled)} filled")
    if a.check and len(filled):
        r = pd.to_numeric(filled["ratio"].map(mergers._ratio), errors="coerce")
        im = pd.to_numeric(filled["implied_ratio"], errors="coerce")
        print(pd.DataFrame({"parent": filled["parent_symbol"], "acquirer": filled["acquirer_symbol"], "ratio": r,
                            "implied": im, "diff_pct": (im / r - 1) * 100}).round(4).to_string(index=False))
    mergers.load(path)       # validate


def cmd_lifecycle(a):
    """Classify securities that stopped trading into data/store/lifecycle.parquet."""
    import pandas as pd
    from . import lifecycle
    from .nsefetch import LIFECYCLE_FILES, NSEClient
    cfg = _cfg(a)
    store, ref = cfg.path("store"), cfg.path("nse_reference")
    if a.fetch:
        try:
            NSEClient(cfg.path("nse_raw"), cfg.politeness).fetch_reference(ref, files=LIFECYCLE_FILES)
        except Exception as e:      # fallback: the cached lists, or classification from the data alone
            print(f"warning: NSE lists not fetched ({e}); using cached copies if any")
    out = lifecycle.build(store, ref, cfg.root / cfg.raw["paths"].get("mergers", "mergers.csv"),
                          cfg.root / cfg.raw["paths"].get("lifecycle_overrides", "lifecycle_overrides.csv"))
    mem = set(pd.read_parquet(store / "membership.parquet")["SecurityID"].dropna())
    print(f"{store / 'lifecycle.parquet'}: {len(out)} securities stopped trading before the end of the data")
    print(pd.DataFrame({"all": out["cls"].value_counts(),
                        "Nifty 500 members": out[out["SecurityID"].isin(mem)]["cls"].value_counts()})
          .fillna(0).astype(int).to_string())


def cmd_reconcile(a):
    """Ledger-vs-adjusted reconciliation for every Nifty 500 security (amendment 2.6), plus speed."""
    import time
    import numpy as np
    from .backtest import reconcile as rc
    from .backtest.account import Account
    from .backtest.sim import SimConfig, Signals, simulate
    cfg = _cfg(a)
    out = cfg.path("reports")
    m, adj, t_load = rc.from_store(cfg.path("store"))
    r = rc.reconcile(m, adj)
    D, N = m.close.shape
    rng = np.random.default_rng(0)
    t0 = time.perf_counter()
    simulate(m, Signals(entry=rng.random((D, N)) < 0.05, exit=rng.random((D, N)) < 0.02),
             SimConfig(account=Account(capital=36_000, max_positions=3, max_position_pct=33)))
    t_sim = time.perf_counter() - t0
    r.summary.to_csv(out / "phase45_reconciliation_securities.csv", index=False)
    r.failures.to_csv(out / "phase45_reconciliation_failures.csv", index=False)
    r.preservation.to_csv(out / "phase45_reconciliation_events.csv", index=False)
    kinds = r.preservation["kind"].value_counts().to_dict() if len(r.preservation) else {}
    lines = [
        "# Reconciliation: position ledger vs adjusted prices (amendment 2.6)", "",
        f"Store version: {__import__('nsebt.backtest.run', fromlist=['store_version']).store_version(cfg.path('store'))}. "
        f"Universe: every Nifty 500 member 2016-2026 ({N} securities, {D} days).", "",
        "| Check | Result |", "|---|---|",
        f"| (a) reinvest-equivalent: securities checked | {r.n} |",
        f"| (a) securities with any day outside 1e-6 | {r.n_failed} ({r.fail_share:.2%}; stop rule S4 above 0.5%) |",
        f"| (a) largest daily error | {r.summary['max_abs_err'].max():.1e} |",
        f"| (a) security-days compared | {int(r.summary['days'].sum()):,} |",
        f"| (b) receivable mode: value events checked | {len(r.preservation)} ({', '.join(f'{k} {v}' for k, v in kinds.items())}) |",
        f"| (b) events outside 1e-6 | {r.b_failed} |",
        f"| Value events with no factor (excluded) | {len(r.excluded)} |",
        f"| Result | {'PASS' if r.passed else 'FAIL'} |", "",
        "| Timing | Seconds |", "|---|---|",
        f"| Load Nifty 500 panel (raw + total-return adjusted) | {t_load:.1f} |",
        f"| Reinvest-equivalent run, all securities held | {r.seconds['reinvest_run']:.1f} |",
        f"| Receivable-mode run, all securities held | {r.seconds['receivable_run']:.1f} |",
        f"| One backtest, 3 positions, random signals, full costs | {t_sim:.1f} |",
        f"| Single backtest including load (target ≤ 30) | {t_load + t_sim:.1f} |", "",
        "Method and assumptions: see `nsebt/nsebt/backtest/reconcile.py`. (a) reinvests every value event as "
        "shares ÷ factor, so it can match the total-return series exactly; the default engine holds dividends "
        "as receivables, which drift from the reinvested series by about D/P × the next day's return, so it is "
        "checked by (b), value preservation on each ex-date.",
    ]
    (out / "phase45_reconciliation.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    if not r.passed:
        sys.exit(1)


# --------------------------------------------------------------------------- research (legacy)

def _legacy_load(a):
    from . import bloomberg
    from .universe import Universe
    cfg = _cfg(a)
    de = cfg.path("bloomberg_root") / "data"
    print("note: legacy path on Bloomberg prices; the Phase 5 engine moves this to the NSE store")
    return bloomberg.load_prices(de), Universe(bloomberg.load_membership(de))


def cmd_backtest(a):
    import pandas as pd

    from .engine import portfolio, trades
    from .strategies import REGISTRY
    data, uni = _legacy_load(a)
    strat = REGISTRY[a.strategy]()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 160)
    if strat.kind == "trades":
        tc = trades.TradeConfig(cost=a.cost if a.cost is not None else trades.TradeConfig.cost)
        T = trades.run(data, strat, tc, universe=None if a.all_stocks else uni)
        P = trades.placebo(data, T, tc)
        T.to_csv(out / f"{strat.name}_trades.csv", index=False)
        print(trades.summarize(T, P).round(4).to_string())
    else:
        pc = portfolio.PortfolioConfig(top_n=a.top_n, cost_per_side=a.cost if a.cost is not None
                                       else portfolio.PortfolioConfig.cost_per_side)
        res = portfolio.run(data, strat, pc, universe=None if a.all_stocks else uni)
        res["daily"].to_csv(out / f"{strat.name}_daily.csv")
        print(portfolio.stats(res["daily"], pc.split).round(4).to_string())
    print(f"\nresults in {out}")


def cmd_signals(a):
    from . import panel
    from .strategies import REGISTRY
    data, uni = _legacy_load(a)
    strat = REGISTRY[a.strategy]()
    last = max(d.index.max() for d in data.values())
    members = uni.members_on(last)
    print(f"latest data: {last.date()}  (members: {len(members)})")
    if strat.kind == "portfolio":
        s = strat.scores(panel.matrix(data, "Close"), panel.matrix(data, "Volume")).iloc[-1]
        print(s[s.index.isin(members)].dropna().sort_values(ascending=False).head(a.top_n).round(4).to_string())
    else:
        hits = [t for t in members if t in data and data[t].index.max() == last and len(data[t]) > 60
                and bool(strat.signals(data[t])["entry"].iloc[-1])]
        print(f"{strat.name} entry signals on {last.date()}: {len(hits)}")
        for t in sorted(hits):
            print(" ", t)


# --------------------------------------------------------------------------- parser

def main(argv=None):
    p = argparse.ArgumentParser(prog="nsebt", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, help="config.yaml (default: search this folder and parents)")
    sub = p.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("update", help="daily: fetch new days, rebuild, report")
    u.add_argument("--start", type=_date, help="default: day after the store's last day")
    u.add_argument("--end", type=_date, help="default: today")
    u.add_argument("--no-build", action="store_true", help="download only")
    u.set_defaults(fn=cmd_update)

    bf = sub.add_parser("backfill", help="download NSE files for a date range")
    bf.add_argument("--start", type=_date)
    bf.add_argument("--end", type=_date)
    bf.add_argument("--sources", default="price,mto", help="comma list: price,mto,indices,sec_full")
    bf.add_argument("--skip-reference", action="store_true")
    bf.set_defaults(fn=cmd_backfill)

    g = sub.add_parser("ingest", help="add NSE files downloaded by hand")
    g.add_argument("files", nargs="+", type=Path)
    g.set_defaults(fn=cmd_ingest)

    bd = sub.add_parser("build", help="rebuild store, mapping and membership")
    bd.add_argument("--skip-store", action="store_true", help="reuse the existing store")
    bd.set_defaults(fn=cmd_build)

    au = sub.add_parser("audit", help="audit the Bloomberg extract")
    au.add_argument("--de-root", type=Path, required=True, help="the DE folder (contains data/)")
    au.add_argument("--out", type=Path, default=Path("reports/audit"))
    au.add_argument("--bhavcopy", type=Path, help="a sec_bhavdata_full CSV, to count name matches")
    au.set_defaults(fn=cmd_audit)

    ci = sub.add_parser("check-indicators", help="compare indicators with TA-Lib")
    ci.add_argument("--universe", default="nifty500", help="nifty500 or all")
    ci.set_defaults(fn=cmd_check_indicators)

    ac = sub.add_parser("account", help="set account limits (asks for each value)")
    ac.add_argument("--show", action="store_true", help="print the saved values and exit")
    ac.add_argument("--capital", help="₹, more than 0 and at most 1,000,000,000")
    ac.add_argument("--max-positions", dest="max_positions", help="1-100")
    ac.add_argument("--max-position-pct", dest="max_position_pct", help="1-100 (% of equity)")
    ac.add_argument("--max-participation-pct", dest="max_participation_pct",
                    help="0.1-100 (% of 20-day average traded value)")
    ac.set_defaults(fn=cmd_account)

    mg = sub.add_parser("mergers", help="seed mergers.csv and check filled ratios against prices")
    mg.add_argument("--seed", action="store_true", help="add stopped Nifty 500 members and merger announcements")
    mg.add_argument("--check", action="store_true", help="print each filled ratio next to the price-implied one")
    mg.set_defaults(fn=cmd_mergers)

    lc = sub.add_parser("lifecycle", help="classify securities that stopped trading (lifecycle.parquet)")
    lc.add_argument("--fetch", action="store_true", help="download NSE's delisted and suspension lists first")
    lc.set_defaults(fn=cmd_lifecycle)

    rc_ = sub.add_parser("reconcile", help="reconcile the position ledger with adjusted prices (all securities)")
    rc_.set_defaults(fn=cmd_reconcile)

    b = sub.add_parser("backup", help="back up everything that cannot be rebuilt")
    b.add_argument("--dest", type=Path, help="default: backup.destinations in config.yaml")
    b.set_defaults(fn=cmd_backup)

    r = sub.add_parser("restore", help="verify a backup and restore from it")
    r.add_argument("src", type=Path, help="folder containing nsebt-backup/")
    r.add_argument("--to", type=Path, help="project folder (default: this one)")
    r.add_argument("--apply", action="store_true", help="actually copy (default: dry run)")
    r.set_defaults(fn=cmd_restore)

    d = sub.add_parser("doctor", help="health checks")
    d.add_argument("--backup", type=Path, help="also verify this backup")
    d.add_argument("--tests", action="store_true", help="also run the test suite")
    d.set_defaults(fn=cmd_doctor)

    from .strategies import REGISTRY
    for name, fn in (("backtest", cmd_backtest), ("signals", cmd_signals)):
        x = sub.add_parser(name, help="(legacy) " + ("run a strategy" if name == "backtest" else "latest signals"))
        x.add_argument("strategy", choices=sorted(REGISTRY))
        x.add_argument("--top-n", type=int, default=20)
        x.add_argument("--cost", type=float, help="trades: round trip; portfolio: per side")
        x.add_argument("--all-stocks", action="store_true", help="ignore point-in-time membership")
        x.add_argument("--out", type=Path, default=Path("results"))
        x.set_defaults(fn=fn)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
