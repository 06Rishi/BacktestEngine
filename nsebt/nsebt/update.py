"""Daily update: fetch everything new since the store's last day, rebuild, report.

    nsebt update            # run once a day after ~19:00 IST (files appear ~18:00-19:00)

Steps
  1. refresh reference lists (listed securities, symbol changes, Nifty 500) and the
     NSE trading-holiday list for the current year
  2. fetch price, delivery and index files for every calendar day after the last
     stored day (weekends included: NSE sometimes trades on them)
  3. refresh corporate-action announcements for the last 90 days and the next 60
  4. rebuild the store, mapping and membership
  5. write a run report: days fetched, holidays, failures, new and vanished symbols,
     sharp price moves with no known corporate action
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import nsefetch, pipeline
from .config import Config

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
PUBLISH_HOUR_IST = 19          # bhavcopy and delivery files are normally out by then
SHARP_MOVE = 0.20              # report close-to-close moves above this with no known event
HOLIDAY_API = "https://www.nseindia.com/api/holiday-master?type=trading"
HOLIDAY_PAGE = "https://www.nseindia.com/resources/exchange-communication-holidays"


def fetch_holidays(ref_dir: Path, polite: nsefetch.Politeness, year: int, max_age_days: int = 7) -> pd.DataFrame:
    """NSE's equity trading holidays for `year`, cached as holidays_YYYY.json."""
    import requests
    p = Path(ref_dir) / f"holidays_{year}.json"
    if not p.exists() or (time.time() - p.stat().st_mtime) > max_age_days * 86400:
        try:
            s = requests.Session()
            s.headers.update({**nsefetch.HEADERS, "Accept": "application/json, text/plain, */*",
                              "Referer": HOLIDAY_PAGE})
            s.get(HOLIDAY_PAGE, timeout=polite.timeout, headers={"Accept": "text/html,*/*"})
            time.sleep(polite.min_interval + random.uniform(0, polite.jitter))
            r = s.get(HOLIDAY_API, timeout=polite.timeout)
            r.raise_for_status()
            data = r.json()
            if "CM" not in data:
                raise ValueError("no CM section")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data))
        except Exception as e:  # noqa: BLE001 - an old copy is still useful
            log.warning("holiday list refresh failed: %s", e)
    if not p.exists():
        return pd.DataFrame(columns=["date", "description"])
    cm = json.loads(p.read_text()).get("CM", [])
    return pd.DataFrame({"date": pd.to_datetime([x["tradingDate"] for x in cm], format="%d-%b-%Y").date,
                         "description": [x.get("description", "") for x in cm]})


def classify_missing(d: date, holidays: pd.DataFrame, now_ist: datetime) -> str:
    if d in set(holidays["date"]):
        return "holiday: " + holidays.loc[holidays["date"] == d, "description"].iloc[0]
    if d.weekday() >= 5:
        return "weekend"
    if d == now_ist.date() and now_ist.hour < PUBLISH_HOUR_IST:
        return "not published yet (before 19:00 IST)"
    if d == now_ist.date():
        return "not published yet"
    return "MISSING: weekday, not a listed holiday"


def last_store_date(store: Path) -> date | None:
    rep = store / "build_report.json"
    if rep.exists():
        return date.fromisoformat(json.loads(rep.read_text())["last_date"])
    return None


def _snapshot(store: Path) -> dict:
    """What the store looked like before this run, for the diff in the report."""
    if not (store / "build_report.json").exists():
        return {}
    last = last_store_date(store)
    px = pd.read_parquet(store / "prices", filters=[("year", "=", last.year)],
                         columns=["Date", "Symbol", "SecurityID"])
    return {"last": last, "symbols_last_day": set(px.loc[px["Date"] == pd.Timestamp(last), "Symbol"]),
            "all_securities": set(pd.read_parquet(store / "securities.parquet", columns=["SecurityID"])["SecurityID"])}


def run(cfg: Config, start: date | None = None, end: date | None = None, build: bool = True) -> dict:
    t0 = time.monotonic()
    now = datetime.now(IST)
    store, raw, ref = cfg.path("store"), cfg.path("nse_raw"), cfg.path("nse_reference")
    before = _snapshot(store)
    last = before.get("last")
    start = start or (last + timedelta(days=1) if last else cfg.backfill_start)
    end = end or now.date()
    rep = {"started_ist": now.isoformat(timespec="seconds"), "range": [str(start), str(end)],
           "store_last_day_before": str(last) if last else None}

    cli = nsefetch.NSEClient(raw, cfg.politeness)
    try:
        cli.fetch_reference(ref)
    except Exception as e:  # noqa: BLE001
        rep["reference_error"] = str(e)
    hol = pd.concat([fetch_holidays(ref, cfg.politeness, y) for y in sorted({start.year, end.year})],
                    ignore_index=True)

    weekends = [start + timedelta(n) for n in range((end - start).days + 1)
                if (start + timedelta(n)).weekday() >= 5]
    res = nsefetch.FetchResult()
    if start <= end:
        try:
            res = cli.fetch_range(start, end, sources=("price", "mto", "indices"), extra_dates=weekends)
        except nsefetch.GiveUp as e:
            rep["stopped"] = str(e)

    got = sorted({d for s, d in res.ok + res.cached if s in ("cm_legacy", "cm_udiff")})
    missing = sorted({d for s, d in res.missing if s in ("cm_legacy", "cm_udiff")})
    rep["days_fetched"] = [str(d) for d in got]
    rep["no_file"] = {str(d): classify_missing(d, hol, now) for d in missing}
    rep["real_gaps"] = [d for d, why in rep["no_file"].items() if why.startswith("MISSING")]
    rep["failed"] = [{"source": s, "date": str(d), "error": e} for s, d, e in res.failed]

    try:
        nsefetch.fetch_corporate_actions(raw / "corp_actions", now.date() - timedelta(days=90),
                                         now.date() + timedelta(days=60), cfg.politeness)
    except Exception as e:  # noqa: BLE001
        rep["corporate_actions_error"] = str(e)

    if build and (got or not last):
        b = pipeline.run(cfg)
        rep["build"] = {"rows": b["store"]["rows"], "last_date": b["store"]["last_date"],
                        "securities": b["store"]["securities"]}
        rep.update(_diff(store, before, set(got)))
    rep["seconds"] = round(time.monotonic() - t0, 1)
    out = cfg.path("reports") / "runs"
    out.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    (out / f"update_{stamp}.json").write_text(json.dumps(rep, indent=2, default=str))
    (out / f"update_{stamp}.md").write_text(render(rep))
    rep["report_path"] = str(out / f"update_{stamp}.md")
    return rep


def _diff(store: Path, before: dict, new_days: set) -> dict:
    if not new_days:
        return {}
    from . import nsestore
    first = min(new_days)
    px = nsestore.load_prices(store, adjusted=False, start=pd.Timestamp(first) - pd.Timedelta(days=15),
                              columns=["Date", "SecurityID", "Symbol", "Series", "Close", "PrevClose"])
    px = px.sort_values(["SecurityID", "Date"])
    newest = px["Date"].max()
    out = {}
    now_syms = set(px.loc[px["Date"] == newest, "Symbol"])
    if before.get("symbols_last_day") is not None:
        out["new_symbols"] = sorted(now_syms - before["symbols_last_day"])
        out["vanished_symbols"] = sorted(before["symbols_last_day"] - now_syms)
        out["new_securities"] = sorted(set(px["SecurityID"]) - before["all_securities"])
    # sharp moves on the new days with no corporate action
    prior = px.groupby("SecurityID")["Close"].shift(1)
    mv = px.assign(prior=prior, move=px["Close"] / prior - 1)
    mv = mv[mv["Date"].dt.date.isin(new_days) & (mv["move"].abs() > SHARP_MOVE)]
    ca = pd.read_parquet(store / "corporate_actions.parquet", columns=["SecurityID", "effective_date", "kind"])
    known = set(zip(ca.loc[ca["kind"] != "dividend", "SecurityID"], ca.loc[ca["kind"] != "dividend", "effective_date"]))
    mv = mv[[(s, d) not in known for s, d in zip(mv["SecurityID"], mv["Date"])]]
    out["sharp_moves_without_action"] = (mv[["Date", "Symbol", "Series", "prior", "Close", "move"]]
                                         .assign(Date=lambda x: x["Date"].dt.date.astype(str),
                                                 move=lambda x: x["move"].round(4))
                                         .to_dict("records"))
    return out


def render(rep: dict) -> str:
    L = [f"# Update run {rep['started_ist']}", "",
         f"- Range checked: {rep['range'][0]} to {rep['range'][1]}",
         f"- Store last day before: {rep['store_last_day_before']}",
         f"- Trading days fetched: {len(rep['days_fetched'])} ({', '.join(rep['days_fetched']) or 'none'})",
         f"- Failures: {len(rep['failed'])}"]
    if "build" in rep:
        b = rep["build"]
        L.append(f"- Store now: {b['rows']:,} rows, {b['securities']} securities, last day {b['last_date']}")
    if rep.get("stopped"):
        L.append(f"- **Stopped early**: {rep['stopped']}")
    for k in ("reference_error", "corporate_actions_error"):
        if rep.get(k):
            L.append(f"- {k.replace('_', ' ')}: {rep[k]}")
    if rep["no_file"]:
        L += ["", "## Days with no file", ""] + [f"- {d}: {w}" for d, w in rep["no_file"].items()]
    if rep["failed"]:
        L += ["", "## Failures", ""] + [f"- {f['source']} {f['date']}: {f['error']}" for f in rep["failed"]]
    for k, title in (("new_symbols", "New symbols"), ("vanished_symbols", "Symbols that stopped trading")):
        if rep.get(k):
            L += ["", f"## {title} ({len(rep[k])})", "", ", ".join(rep[k])]
    if rep.get("sharp_moves_without_action"):
        L += ["", f"## Moves over {SHARP_MOVE:.0%} with no known corporate action", "",
              "| Date | Symbol | Series | Prior close | Close | Move |", "|---|---|---|---|---|---|"]
        L += [f"| {m['Date']} | {m['Symbol']} | {m['Series']} | {m['prior']} | {m['Close']} | {m['move']:+.1%} |"
              for m in rep["sharp_moves_without_action"]]
    return "\n".join(L) + "\n"


def one_line(rep: dict) -> str:
    b = rep.get("build", {})
    s = (f"update {rep['range'][0]}..{rep['range'][1]}: {len(rep['days_fetched'])} day(s) fetched, "
         f"{len(rep['failed'])} failed, {len(rep['real_gaps'])} real gap(s)")
    if b:
        s += f"; store to {b['last_date']}"
    if rep.get("sharp_moves_without_action"):
        s += f"; {len(rep['sharp_moves_without_action'])} sharp move(s) to review"
    return s + f"  [{rep['report_path']}]"
