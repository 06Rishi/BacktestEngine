"""Load config.yaml (searched for in the current folder and its parents)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import yaml

from .nsefetch import Politeness


@dataclass
class Config:
    root: Path
    raw: dict

    def path(self, key: str) -> Path:
        return (self.root / self.raw["paths"][key]).resolve()

    @property
    def politeness(self) -> Politeness:
        return Politeness(**self.raw.get("nse", {}).get("politeness", {}))

    @property
    def backfill_start(self) -> date:
        return _d(self.raw["nse"]["backfill_start"])

    def extra_dates(self, end: date) -> list[date]:
        nse = self.raw.get("nse", {})
        out = [_d(x) for x in nse.get("weekend_sessions", [])]
        after = nse.get("try_weekends_after")
        if after:
            d = _d(after)
            while d <= end:
                if d.weekday() >= 5:
                    out.append(d)
                d += timedelta(days=1)
        return sorted(set(out))


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x))


def find(start: Path | None = None) -> Path:
    p = Path(start or Path.cwd()).resolve()
    for q in (p, *p.parents):
        if (q / "config.yaml").exists():
            return q / "config.yaml"
    raise FileNotFoundError("config.yaml not found in this folder or any parent")


def load(path: Path | None = None) -> Config:
    path = Path(path) if path else find()
    return Config(root=path.parent, raw=yaml.safe_load(path.read_text()))
