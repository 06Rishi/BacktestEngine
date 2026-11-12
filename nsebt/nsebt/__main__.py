"""Command line entry point: python -m nsebt <command> ...

  update     download NSE bhavcopy files and add them to the store
  ingest     add bhavcopy CSVs you downloaded yourself
  map        map Bloomberg IDs to NSE symbols and check prices where the sources overlap
  backtest   run a strategy on the combined data
  signals    show what a strategy says to hold/buy as of the latest data
  audit      audit the Bloomberg extract (DE folder) and write CSV evidence
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from . import bloomberg, mapping, panel, store
from .engine import portfolio, trades
from .strategies import REGISTRY
from .universe import Universe

D = Path("data")


def _date(s):
    return date.fromisoformat(s)


def _load(args, need_nse=True):
    bbg = bloomberg.load_prices(args.de_data)
    uni = Universe(bloomberg.load_membership(args.de_data))
    if need_nse and Path(args.store).exists() and Path(args.mapping).exists():
        nse = store.load(args.store)
        mp = pd.read_csv(args.mapping)
        trusted = None
        val = Path(args.mapping).with_name("mapping_validation.csv")
        if val.exists():
            v = pd.read_csv(val)
            trusted = set(v.loc[v["Status"] == "ok", "Bloomberg_ID"])
            if not trusted:
                print("no mapping is validated yet: download a few days that overlap the "
                      "Bloomberg data (update --start 2026-08-20), then rerun map")
        data = panel.combine(bbg, nse, mp, trusted=trusted)
        ext = sum(1 for k in data if len(data[k]) > len(bbg.get(k, [])))
        print(f"loaded {len(bbg)} Bloomberg series; extended {ext} with NSE data"
              f"{' (validated matches only)' if trusted is not None else ''}")
    else:
        data = bbg
        print(f"loaded {len(bbg)} Bloomberg series (no NSE extension: run update + map first)")
    return data, uni


def cmd_update(a):
    rep = store.update(a.raw, a.store, start=a.start, end=a.end, pause=a.pause)
    print(rep)
    print(f"store now ends {store.last_date(a.store)}")


def cmd_ingest(a):
    n = store.ingest_files(a.files, a.store)
    print(f"ingested {n} file(s); store now ends {store.last_date(a.store)}")


def cmd_map(a):
    bbg = bloomberg.load_prices(a.de_data)
    nse = store.load(a.store)
    if nse.empty:
        sys.exit("NSE store is empty: run update or ingest first.")
    ids = set(bbg) | set(bloomberg.load_membership(a.de_data)["Bloomberg_ID"])
    mp = mapping.build(ids, nse["Symbol"].unique(), a.overrides, a.bbg_isin, a.nse_list)
    Path(a.mapping).parent.mkdir(parents=True, exist_ok=True)
    mp.to_csv(a.mapping, index=False)
    val = mapping.validate_overlap(mp, bbg, nse)
    vpath = Path(a.mapping).with_name("mapping_validation.csv")
    val.to_csv(vpath, index=False)
    cur = set(bloomberg.load_membership(a.de_data).query("Date == Date.max()")["Bloomberg_ID"])
    print(mp["Method"].value_counts().to_string())
    print("\nvalidation:\n" + val["Status"].value_counts().to_string())
    um = sorted(set(mp.loc[mp["NSE_Symbol"].isna(), "Bloomberg_ID"]) & cur)
    print(f"\ncurrent Nifty 500 members still unmapped: {len(um)}")
    if um:
        p = Path(a.mapping).with_name("unmapped_current_members.csv")
        pd.DataFrame({"Bloomberg_ID": um, "NSE_Symbol": ""}).to_csv(p, index=False)
        print(f"  listed in {p}; fill NSE_Symbol and pass it as --overrides")
    print(f"\nwrote {a.mapping} and {vpath}")


def _strategy(a):
    cls = REGISTRY[a.strategy]
    return cls()


def cmd_backtest(a):
    data, uni = _load(a)
    strat = _strategy(a)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 160)
    if strat.kind == "trades":
        cfg = trades.TradeConfig(cost=a.cost if a.cost is not None else trades.TradeConfig.cost)
        T = trades.run(data, strat, cfg, universe=None if a.all_stocks else uni)
        P = trades.placebo(data, T, cfg)
        T.to_csv(out / f"{strat.name}_trades.csv", index=False)
        print(trades.summarize(T, P).round(4).to_string())
    else:
        cfg = portfolio.PortfolioConfig(top_n=a.top_n,
                                        cost_per_side=a.cost if a.cost is not None
                                        else portfolio.PortfolioConfig.cost_per_side)
        res = portfolio.run(data, strat, cfg, universe=None if a.all_stocks else uni)
        res["daily"].to_csv(out / f"{strat.name}_daily.csv")
        print(portfolio.stats(res["daily"], cfg.split).round(4).to_string())
        print(f"\ndays with open-to-open moves > {cfg.ca_filter:.0%} zeroed as corporate actions: {res['ca_hits']}")
    print(f"\nresults in {out}")


def cmd_signals(a):
    data, uni = _load(a)
    strat = _strategy(a)
    last = max(d.index.max() for d in data.values())
    members = uni.members_on(last)
    print(f"latest data: {last.date()}  (members as of latest snapshot: {len(members)})")
    stale = [t for t in members if t in data and data[t].index.max() < last]
    if stale:
        print(f"warning: {len(stale)} members have no data on {last.date()} (unmapped or not traded)")
    if strat.kind == "portfolio":
        C, V = panel.matrix(data, "Close"), panel.matrix(data, "Volume")
        s = strat.scores(C, V).iloc[-1]
        s = s[s.index.isin(members)].dropna().sort_values(ascending=False).head(a.top_n)
        print(f"\ntop {a.top_n} by {strat.name} score:")
        print(s.round(4).to_string())
    else:
        hits = []
        for t in members:
            if t in data and data[t].index.max() == last and len(data[t]) > 60:
                sig = strat.signals(data[t])
                if bool(sig["entry"].iloc[-1]):
                    hits.append(t)
        print(f"\n{strat.name} entry signals on {last.date()} (buy at next open): {len(hits)}")
        for t in sorted(hits):
            print(" ", t)


def cmd_audit(a):
    from . import audit
    s = audit.run(a.de_root, a.out, nse_bhavcopy=a.bhavcopy)
    p = s["prices"]
    print(f"{p['tickers']} series, {p['rows']:,} rows, {p['date_min']} to {p['date_max']}; "
          f"{p['big_moves']} moves over 20% ({p['big_move_labels']})")


def main(argv=None):
    p = argparse.ArgumentParser(prog="nsebt", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--de-data", type=Path, default=D / "DE" / "data", help="Bloomberg DE/data folder")
    p.add_argument("--raw", type=Path, default=D / "nse" / "raw", help="raw bhavcopy cache")
    p.add_argument("--store", type=Path, default=D / "nse" / "store", help="parsed NSE store")
    p.add_argument("--mapping", type=Path, default=D / "mapping.csv")
    sub = p.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("update", help="download bhavcopy files")
    u.add_argument("--start", type=_date, help="YYYY-MM-DD (default: day after store ends)")
    u.add_argument("--end", type=_date, help="YYYY-MM-DD (default: today)")
    u.add_argument("--pause", type=float, default=1.0, help="seconds between downloads")
    u.set_defaults(fn=cmd_update)

    g = sub.add_parser("ingest", help="add bhavcopy CSVs you already have")
    g.add_argument("files", nargs="+", type=Path)
    g.set_defaults(fn=cmd_ingest)

    m = sub.add_parser("map", help="map Bloomberg IDs to NSE symbols and validate")
    m.add_argument("--overrides", type=Path, help="CSV: Bloomberg_ID,NSE_Symbol")
    m.add_argument("--bbg-isin", type=Path, help="CSV: Bloomberg_ID,ISIN (from Bloomberg ID_ISIN)")
    m.add_argument("--nse-list", type=Path, help="NSE EQUITY_L.csv (SYMBOL, ISIN NUMBER)")
    m.set_defaults(fn=cmd_map)

    for name, fn in (("backtest", cmd_backtest), ("signals", cmd_signals)):
        b = sub.add_parser(name)
        b.add_argument("strategy", choices=sorted(REGISTRY))
        b.add_argument("--top-n", type=int, default=20)
        b.add_argument("--cost", type=float, help="trades: round trip; portfolio: per side")
        b.add_argument("--all-stocks", action="store_true",
                       help="ignore point-in-time Nifty 500 membership")
        b.add_argument("--out", type=Path, default=Path("results"))
        b.set_defaults(fn=fn)

    au = sub.add_parser("audit", help="audit the Bloomberg extract")
    au.add_argument("--de-root", type=Path, required=True, help="the DE folder (contains data/)")
    au.add_argument("--out", type=Path, default=Path("reports/audit"))
    au.add_argument("--bhavcopy", type=Path, help="a sec_bhavdata_full CSV, to count name matches")
    au.set_defaults(fn=cmd_audit)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
