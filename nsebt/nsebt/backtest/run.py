"""A backtest run and its reproducibility record.

Every run records the code commit, the data version, the fully resolved configuration,
its hash and the random seed. Same configuration + same seed + same data + same code
gives an identical run.

The record schema is frozen (RUN_SCHEMA_VERSION). Adding or changing fields needs a
new schema version and a migration of the ledger.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .sim import Market, Result, Signals, SimConfig, simulate

RUN_SCHEMA_VERSION = 1


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in sorted(x.items(), key=lambda kv: str(kv[0]))}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return float(x)
    if isinstance(x, (pd.Timestamp, datetime)):
        return x.isoformat()
    if hasattr(x, "isoformat"):
        return x.isoformat()
    return x


def config_hash(resolved: dict) -> str:
    blob = json.dumps(_jsonable(resolved), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def git_state(repo: Path | None = None) -> tuple[str, bool]:
    """(commit, dirty). Dirty means tracked files differ from the commit."""
    repo = Path(repo or Path(__file__).resolve().parents[3])
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                                check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo,
                                    capture_output=True, text=True, check=True).stdout.strip())
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        return "unknown", True


def store_version(store: Path | None) -> str:
    """Hash of the store's build report: changes whenever the store is rebuilt."""
    if store is None:
        return "none"
    p = Path(store) / "build_report.json"
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else "unknown"


@dataclass(frozen=True)
class RunRecord:
    schema_version: int
    run_id: str
    created_utc: str
    strategy: str
    params: dict
    account: dict
    costs: dict
    sim: dict
    universe: str
    start: str
    end: str
    seed: int
    git_commit: str
    git_dirty: bool
    store_version: str
    config_hash: str
    n_trades: int
    final_equity: float
    warnings: tuple

    def to_dict(self) -> dict:
        return _jsonable(asdict(self))


RUN_FIELDS = tuple(f.name for f in fields(RunRecord))


@dataclass
class Run:
    record: RunRecord
    result: Result


def resolve(signals: Signals, cfg: SimConfig, market: Market, strategy: str = "unnamed",
            params: dict | None = None, universe: str = "custom", store: Path | None = None) -> dict:
    """Everything that determines the outcome, as plain data."""
    return {
        "strategy": strategy,
        "params": dict(params or {}),
        "account": asdict(cfg.account),
        "costs": asdict(cfg.costs),
        "sim": {"buy_order_ttl_days": cfg.buy_order_ttl_days, "max_hold_days": signals.max_hold_days,
                "sizing": asdict(signals.sizing), "strategy_max_positions": signals.max_positions},
        "universe": universe,
        "start": market.dates[0].date().isoformat(),
        "end": market.dates[-1].date().isoformat(),
        "seed": cfg.seed,
        "store_version": store_version(store),
    }


def run(market: Market, signals: Signals, cfg: SimConfig = SimConfig(), strategy: str = "unnamed",
        params: dict | None = None, universe: str = "custom", store: Path | None = None,
        repo: Path | None = None) -> Run:
    resolved = resolve(signals, cfg, market, strategy, params, universe, store)
    h = config_hash(resolved)
    commit, dirty = git_state(repo)
    result = simulate(market, signals, cfg)
    now = datetime.now(timezone.utc)
    rec = RunRecord(
        schema_version=RUN_SCHEMA_VERSION,
        run_id=f"{now:%Y%m%dT%H%M%S}_{h[:10]}_s{cfg.seed}",
        created_utc=now.isoformat(timespec="seconds"),
        strategy=strategy, params=resolved["params"], account=resolved["account"], costs=resolved["costs"],
        sim=resolved["sim"], universe=universe, start=resolved["start"], end=resolved["end"], seed=cfg.seed,
        git_commit=commit, git_dirty=dirty, store_version=resolved["store_version"], config_hash=h,
        n_trades=len(result.trades), final_equity=float(result.daily["equity"].iloc[-1]),
        warnings=tuple(result.config.get("warnings", ())),
    )
    return Run(rec, result)
