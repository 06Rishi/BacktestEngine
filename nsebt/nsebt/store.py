"""On-disk store for parsed NSE daily data: one parquet file per calendar month."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from . import bhavcopy

DEFAULT_START = date(2026, 8, 28)   # day after the Bloomberg history ends


@dataclass
class UpdateReport:
    fetched: list[date] = field(default_factory=list)
    no_file: list[date] = field(default_factory=list)     # holidays / not yet published
    failed: list[tuple[date, str]] = field(default_factory=list)

    def __str__(self):
        s = (f"fetched {len(self.fetched)} day(s), no file (holiday/not published) "
             f"for {len(self.no_file)}, failed {len(self.failed)}")
        if self.failed:
            s += "\n" + "\n".join(f"  {d}: {e}" for d, e in self.failed)
        return s


def month_path(store_dir: Path, d) -> Path:
    return Path(store_dir) / f"{d:%Y-%m}.parquet"


def write_day(store_dir: Path, day_df: pd.DataFrame) -> None:
    """Merge one parsed day into its month file (idempotent)."""
    if day_df.empty:
        raise ValueError("no equity rows in file")
    if day_df["Date"].nunique() != 1:
        raise ValueError("expected exactly one date per file")
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    d = day_df["Date"].iloc[0]
    p = month_path(store_dir, d)
    if p.exists():
        old = pd.read_parquet(p)
        day_df = pd.concat([old[old["Date"] != d], day_df], ignore_index=True)
    day_df = day_df.sort_values(["Date", "Symbol"]).reset_index(drop=True)
    tmp = p.with_name(p.name + ".tmp")
    day_df.to_parquet(tmp, index=False)
    tmp.replace(p)


def load(store_dir: Path) -> pd.DataFrame:
    files = sorted(Path(store_dir).glob("*.parquet"))
    if not files:
        return pd.DataFrame(columns=["Date", "Symbol"])
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def last_date(store_dir: Path) -> date | None:
    files = sorted(Path(store_dir).glob("*.parquet"))
    if not files:
        return None
    return pd.read_parquet(files[-1], columns=["Date"])["Date"].max().date()


def update(raw_dir: Path, store_dir: Path, start: date | None = None, end: date | None = None,
           session=None, pause: float = 1.0) -> UpdateReport:
    """Fetch every weekday from start to end, parse, and store. Re-running is safe:
    raw files are cached and each day overwrites only itself."""
    if start is None:
        ld = last_date(store_dir)
        start = ld + timedelta(days=1) if ld else DEFAULT_START
    end = end or date.today()
    rep = UpdateReport()
    d = start
    while d <= end:
        if d.weekday() < 5:
            try:
                path = bhavcopy.fetch(d, raw_dir, session=session)
                if path is None:
                    rep.no_file.append(d)
                else:
                    write_day(store_dir, bhavcopy.parse(path))
                    rep.fetched.append(d)
                    time.sleep(pause)
            except Exception as e:
                rep.failed.append((d, str(e)))
        d += timedelta(days=1)
    return rep


def ingest_files(paths, store_dir: Path) -> int:
    """Add already-downloaded bhavcopy files (e.g. saved from a browser)."""
    n = 0
    for p in paths:
        write_day(store_dir, bhavcopy.parse(Path(p)))
        n += 1
    return n
