"""Polite, resumable downloads from NSE's public archives.

Every file is fetched at most once: successes are kept in a raw cache, and 404s for
dates more than a few days old are remembered in a ledger so they are not asked for
again. Requests are spaced out with jitter, failures back off exponentially, and a
run of consecutive failures triggers a long cool-down (and eventually a clean stop)
instead of hammering the server.

Sources (all verified by hand on 2026-11-12):
  cm_legacy  daily equity bhavcopy, CSV in a zip, with ISIN      2000 .. 2024-07-05
  cm_udiff   daily equity bhavcopy, UDiFF format, with ISIN      2024-07-08 .. today
  mto        security-wise delivery position (.DAT)              ~2002 .. today
  sec_full   sec_bhavdata_full (OHLC + delivery, no ISIN)        ~Oct 2019 .. today
  indices    closing values of every NSE index (Nifty 50, Nifty 500 ...)  2016 .. today
"""
from __future__ import annotations

import io
import json
import logging
import random
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
UDIFF_START = date(2024, 7, 8)        # first UDiFF file; the legacy file ends 2024-07-05
BASE = "https://nsearchives.nseindia.com"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/csv,application/zip,application/octet-stream,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}

REFERENCE_FILES = {
    "EQUITY_L.csv": f"{BASE}/content/equities/EQUITY_L.csv",
    "symbolchange.csv": f"{BASE}/content/equities/symbolchange.csv",
    "ind_nifty500list.csv": "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv",
}

# NSE's published lists of delisted and suspended securities (used by the lifecycle table)
LIFECYCLE_FILES = {
    "delisted.csv": f"{BASE}/content/equities/delisted.csv",
    "suspension.csv": f"{BASE}/content/equities/suspension.csv",
}


def url_and_name(source: str, d: date) -> tuple[str, str]:
    mon = MONTHS[d.month - 1]
    if source == "cm_legacy":
        n = f"cm{d:%d}{mon}{d:%Y}bhav.csv.zip"
        return f"{BASE}/content/historical/EQUITIES/{d:%Y}/{mon}/{n}", n
    if source == "cm_udiff":
        n = f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
        return f"{BASE}/content/cm/{n}", n
    if source == "mto":
        n = f"MTO_{d:%d%m%Y}.DAT"
        return f"{BASE}/archives/equities/mto/{n}", n
    if source == "indices":
        n = f"ind_close_all_{d:%d%m%Y}.csv"
        return f"{BASE}/content/indices/{n}", n
    if source == "sec_full":
        n = f"sec_bhavdata_full_{d:%d%m%Y}.csv"
        return f"{BASE}/products/content/{n}", n
    raise ValueError(source)


def price_source(d: date) -> str:
    return "cm_udiff" if d >= UDIFF_START else "cm_legacy"


def check_content(source: str, content: bytes) -> None:
    """Raise if the bytes are not the file we asked for (e.g. an HTML block page
    served with status 200, or a truncated download)."""
    head = content[:200].lstrip().lower()
    if head.startswith(b"<") or b"<html" in head:
        raise BlockedError("HTML page instead of data (block or error page)")
    if source in ("cm_legacy", "cm_udiff"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                names = z.namelist()
                if not names:
                    raise ValueError("empty zip")
                first = z.read(names[0])[:100].decode("utf-8", "replace")
        except zipfile.BadZipFile as e:
            raise ValueError(f"not a valid zip: {e}") from e
        want = "SYMBOL" if source == "cm_legacy" else "TradDt"
        if not first.lstrip().startswith(want):
            raise ValueError(f"unexpected header {first[:40]!r}")
    elif source == "mto":
        if not content.lstrip().startswith(b"Security Wise Delivery Position"):
            raise ValueError(f"unexpected MTO header {content[:40]!r}")
    elif source == "indices":
        if not content.lstrip().startswith(b"Index Name"):
            raise ValueError(f"unexpected index header {content[:40]!r}")
    elif source == "sec_full":
        if not content.lstrip().upper().startswith(b"SYMBOL"):
            raise ValueError(f"unexpected header {content[:40]!r}")


class BlockedError(Exception):
    pass


class GiveUp(Exception):
    """Too many consecutive failures: stop the run and keep everything fetched so far."""


@dataclass
class Politeness:
    min_interval: float = 1.0        # seconds between request starts
    jitter: float = 0.6              # extra random 0..jitter seconds
    retries: int = 4                 # per file
    backoff: float = 4.0             # first retry wait, doubles each time
    max_backoff: float = 120.0
    cooldown_after: int = 6          # consecutive failed files before a cool-down
    cooldown: float = 600.0          # seconds
    max_cooldowns: int = 3           # then give up
    timeout: float = 45.0
    negative_cache_days: int = 5     # remember 404s only for dates older than this


@dataclass
class FetchResult:
    ok: list = field(default_factory=list)        # (source, date)
    cached: list = field(default_factory=list)
    missing: list = field(default_factory=list)   # 404: holiday or not published
    failed: list = field(default_factory=list)    # (source, date, error)

    def summary(self) -> str:
        return (f"downloaded {len(self.ok)}, already cached {len(self.cached)}, "
                f"not on server {len(self.missing)}, failed {len(self.failed)}")


class NSEClient:
    """One session, one request at a time, spaced and backed off."""

    def __init__(self, raw_dir: Path, polite: Politeness = Politeness(), session=None):
        self.raw_dir = Path(raw_dir)
        self.p = polite
        self._session = session
        self._own_session = session is None     # never replace a session the caller supplied
        self._last = 0.0
        self._consecutive_fail = 0
        self._cooldowns = 0
        self.ledger_path = self.raw_dir / "_ledger.json"
        self.ledger = json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else {}

    # -- plumbing ---------------------------------------------------------
    @property
    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.headers.update(HEADERS)
        return self._session

    def _wait(self):
        gap = self.p.min_interval + random.uniform(0, self.p.jitter)
        dt = time.monotonic() - self._last
        if dt < gap:
            time.sleep(gap - dt)
        self._last = time.monotonic()

    def _save_ledger(self):
        tmp = self.ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.ledger, indent=0, sort_keys=True))
        tmp.replace(self.ledger_path)

    def path(self, source: str, d: date) -> Path:
        return self.raw_dir / source / f"{d:%Y}" / url_and_name(source, d)[1]

    # -- one file -----------------------------------------------------------
    def get(self, source: str, d: date, today: date | None = None) -> tuple[str, Path | None]:
        """Returns ('cached'|'ok'|'missing', path). Raises after retries are exhausted."""
        today = today or date.today()
        p = self.path(source, d)
        if p.exists() and p.stat().st_size > 0:
            return "cached", p
        key = f"{source}:{d.isoformat()}"
        if self.ledger.get(key) == 404 and (today - d).days > self.p.negative_cache_days:
            return "missing", None
        url, _ = url_and_name(source, d)
        err = None
        for attempt in range(self.p.retries):
            self._wait()
            try:
                r = self.session.get(url, timeout=self.p.timeout)
                if r.status_code == 404:
                    if (today - d).days > self.p.negative_cache_days:
                        self.ledger[key] = 404
                        self._save_ledger()
                    self._consecutive_fail = 0
                    return "missing", None
                if r.status_code in (401, 403, 429) or r.status_code >= 500:
                    wait = float(r.headers.get("Retry-After", 0) or 0)
                    raise BlockedError(f"HTTP {r.status_code}" + (f", retry after {wait:.0f}s" if wait else ""))
                r.raise_for_status()
                check_content(source, r.content)
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_name(p.name + ".part")
                tmp.write_bytes(r.content)
                tmp.replace(p)
                self._consecutive_fail = 0
                self.ledger.pop(key, None)
                return "ok", p
            except Exception as e:  # noqa: BLE001 - classified and retried below
                err = e
                wait = min(self.p.backoff * 2 ** attempt, self.p.max_backoff) * random.uniform(0.8, 1.3)
                log.warning("%s %s attempt %d failed: %s; waiting %.0fs", source, d, attempt + 1, e, wait)
                time.sleep(wait)
        self._consecutive_fail += 1
        if self._consecutive_fail >= self.p.cooldown_after:
            self._cooldowns += 1
            if self._cooldowns > self.p.max_cooldowns:
                raise GiveUp(f"{self._consecutive_fail} consecutive failures after "
                             f"{self.p.max_cooldowns} cool-downs; last error: {err}")
            log.warning("%d consecutive failures: cooling down %.0fs (%d/%d)", self._consecutive_fail,
                        self.p.cooldown, self._cooldowns, self.p.max_cooldowns)
            time.sleep(self.p.cooldown)
            if self._own_session:
                self._session = None      # fresh connection after a cool-down
            self._consecutive_fail = 0
        raise RuntimeError(f"{source} {d}: {err}")

    # -- many files ---------------------------------------------------------
    def fetch_range(self, start: date, end: date, sources=("price", "mto"),
                    extra_dates=(), progress_every: int = 50) -> FetchResult:
        """Fetch every weekday in [start, end] plus extra_dates (weekend sessions).
        'price' resolves to the right bhavcopy format for each date."""
        days = {start + timedelta(n) for n in range((end - start).days + 1)}
        cand = sorted({d for d in days if d.weekday() < 5} | {d for d in extra_dates if start <= d <= end})
        res = FetchResult()
        t0 = time.monotonic()
        for i, d in enumerate(cand, 1):
            for s in sources:
                src = price_source(d) if s == "price" else s
                try:
                    status, _ = self.get(src, d)
                except GiveUp:
                    raise
                except Exception as e:  # noqa: BLE001
                    res.failed.append((src, d, str(e)))
                    continue
                {"ok": res.ok, "cached": res.cached, "missing": res.missing}[status].append((src, d))
            if i % progress_every == 0 or i == len(cand):
                el = time.monotonic() - t0
                log.info("%d/%d dates (%s) %s; %.0f min elapsed", i, len(cand), d, res.summary(), el / 60)
        return res

    def fetch_reference(self, ref_dir: Path, stamp: date | None = None,
                        files: dict[str, str] | None = None) -> dict[str, Path]:
        """Today's copies of the listed-securities, symbol-change and Nifty 500 files (or `files`),
        saved with a date stamp (they change over time)."""
        stamp = stamp or date.today()
        ref_dir = Path(ref_dir)
        out = {}
        for name, url in (REFERENCE_FILES if files is None else files).items():
            p = ref_dir / f"{Path(name).stem}_{stamp:%Y%m%d}{Path(name).suffix}"
            if p.exists() and p.stat().st_size > 0:
                out[name] = p
                continue
            self._wait()
            r = self.session.get(url, timeout=self.p.timeout)
            r.raise_for_status()
            if r.content[:200].lstrip().startswith(b"<"):
                raise BlockedError(f"{name}: HTML instead of CSV")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(r.content)
            out[name] = p
        return out


def identify(name: str) -> tuple[str, date] | None:
    """(source, date) for a file name NSE uses, e.g. a file saved from a browser."""
    import re
    n = Path(name).name
    pats = [
        ("cm_legacy", r"^cm(\d{2})([A-Z]{3})(\d{4})bhav\.csv\.zip$", lambda m: date(int(m[3]), MONTHS.index(m[2]) + 1, int(m[1]))),
        ("cm_udiff", r"^BhavCopy_NSE_CM_0_0_0_(\d{4})(\d{2})(\d{2})_F_0000\.csv\.zip$", lambda m: date(int(m[1]), int(m[2]), int(m[3]))),
        ("mto", r"^MTO_(\d{2})(\d{2})(\d{4})\.DAT$", lambda m: date(int(m[3]), int(m[2]), int(m[1]))),
        ("indices", r"^ind_close_all_(\d{2})(\d{2})(\d{4})\.csv$", lambda m: date(int(m[3]), int(m[2]), int(m[1]))),
        ("sec_full", r"^sec_bhavdata_full_(\d{2})(\d{2})(\d{4})\.csv$", lambda m: date(int(m[3]), int(m[2]), int(m[1]))),
    ]
    for src, pat, mk in pats:
        m = re.match(pat, n, re.I)
        if m:
            return src, mk(m)
    return None


def ingest(paths, raw_dir: Path) -> list[tuple[str, str]]:
    """Copy manually downloaded NSE files into the raw cache after checking them.
    Returns (file, result) pairs; result is 'added', 'already present' or an error."""
    out = []
    for p in map(Path, paths):
        idn = identify(p.name)
        if idn is None:
            out.append((p.name, "unrecognised file name"))
            continue
        src, d = idn
        try:
            content = p.read_bytes()
            check_content(src, content)
        except Exception as e:  # noqa: BLE001
            out.append((p.name, f"rejected: {e}"))
            continue
        dst = Path(raw_dir) / src / f"{d:%Y}" / url_and_name(src, d)[1]
        if dst.exists() and dst.read_bytes() == content:
            out.append((p.name, "already present"))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        tmp.write_bytes(content)
        tmp.replace(dst)
        out.append((p.name, "added"))
    return out


CA_API ="https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date={a}&to_date={b}"
CA_WARMUP = "https://www.nseindia.com/companies-listing/corporate-filings-actions"


def fetch_corporate_actions(out_dir: Path, start: date, end: date, polite: Politeness = Politeness(),
                            chunk_months: int = 3, session=None) -> list[Path]:
    """NSE's corporate-action announcements (splits, bonuses, rights, dividends,
    demergers ...) as JSON, one cached file per chunk of months. The www API needs
    the cookies a browser gets from visiting the site, so a warm-up page is loaded
    first and again whenever the API refuses. Chunks that end more than 30 days ago
    are never fetched twice; newer chunks are refreshed."""
    import requests
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    s = session or requests.Session()
    s.headers.update({**HEADERS, "Accept": "application/json, text/plain, */*", "Referer": CA_WARMUP})

    def warm():
        try:
            s.get(CA_WARMUP, timeout=polite.timeout, headers={"Accept": "text/html,*/*"})
        except Exception as e:  # noqa: BLE001 - the cookies matter, not the page
            log.warning("warm-up page failed: %s", e)
        time.sleep(polite.min_interval + random.uniform(0, polite.jitter))

    warm()
    paths, a = [], date(start.year, start.month, 1)
    while a <= end:
        m = a.month - 1 + chunk_months
        b = min(date(a.year + m // 12, m % 12 + 1, 1) - timedelta(days=1), end)
        p = out_dir / f"ca_{a:%Y%m%d}_{b:%Y%m%d}.json"
        fresh = (date.today() - b).days > 30
        if p.exists() and fresh:
            paths.append(p)
            a = b + timedelta(days=1)
            continue
        url = CA_API.format(a=f"{a:%d-%m-%Y}", b=f"{b:%d-%m-%Y}")
        for attempt in range(polite.retries):
            time.sleep(polite.min_interval * 2 + random.uniform(0, polite.jitter))
            try:
                r = s.get(url, timeout=polite.timeout)
                if r.status_code in (401, 403):
                    raise BlockedError(f"HTTP {r.status_code}")
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, list):
                    raise ValueError(f"unexpected JSON: {str(data)[:100]}")
                tmp = p.with_suffix(".part")
                tmp.write_text(json.dumps(data))
                tmp.replace(p)
                paths.append(p)
                log.info("corporate actions %s..%s: %d records", a, b, len(data))
                break
            except Exception as e:  # noqa: BLE001
                wait = min(polite.backoff * 2 ** attempt, polite.max_backoff)
                log.warning("corporate actions %s..%s attempt %d: %s; waiting %.0fs", a, b, attempt + 1, e, wait)
                time.sleep(wait)
                warm()
        else:
            raise RuntimeError(f"corporate actions {a}..{b}: gave up after {polite.retries} attempts")
        a = b + timedelta(days=1)
    return paths


def setup_logging(log_file: Path | None = None, level=logging.INFO):
    handlers = [logging.StreamHandler()]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(level=level, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


ANN_API = "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={symbol}"
ANN_WARMUP = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"


def fetch_announcements(out_dir: Path, symbols, polite: Politeness = Politeness(), session=None,
                        refresh: bool = False) -> dict[str, Path]:
    """NSE corporate announcements (filings: schemes, record dates, delisting and suspension
    notices) for each symbol, cached as one JSON file per symbol. Used to classify securities
    that stopped trading."""
    import requests
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    s = session or requests.Session()
    s.headers.update({**HEADERS, "Accept": "application/json, text/plain, */*", "Referer": ANN_WARMUP})
    warmed = False
    out = {}
    for sym in symbols:
        p = out_dir / f"{sym.replace('&', '_')}.json"
        if p.exists() and not refresh:
            out[sym] = p
            continue
        if not warmed:
            s.get(ANN_WARMUP, timeout=polite.timeout, headers={"Accept": "text/html,*/*"})
            warmed = True
        for attempt in range(polite.retries):
            time.sleep(polite.min_interval * 2 + random.uniform(0, polite.jitter))
            try:
                r = s.get(ANN_API.format(symbol=requests.utils.quote(sym)), timeout=polite.timeout)
                if r.status_code in (401, 403):
                    s.get(ANN_WARMUP, timeout=polite.timeout, headers={"Accept": "text/html,*/*"})
                    raise BlockedError(f"HTTP {r.status_code}")
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, list):
                    raise ValueError(f"unexpected JSON: {str(data)[:100]}")
                p.write_text(json.dumps(data))
                out[sym] = p
                break
            except Exception as e:  # noqa: BLE001
                wait = min(polite.backoff * 2 ** attempt, polite.max_backoff)
                log.warning("announcements %s attempt %d: %s; waiting %.0fs", sym, attempt + 1, e, wait)
                time.sleep(wait)
    return out
