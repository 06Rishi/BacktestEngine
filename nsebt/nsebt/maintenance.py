"""Backup, restore and health checks, so the whole system can move to another machine.

What is backed up (config `backup.include`): everything that cannot be rebuilt from
code - downloaded NSE files, the Bloomberg extract, reference lists, manual decision
files, config, and the trade journal once it exists - plus a git bundle of the full
code history (so a backup is complete even without GitHub). The parquet store is not
backed up: `nsebt build` recreates it from the raw files in about two minutes.

    nsebt backup  [--dest DIR]      copy new/changed files, write a checksum manifest
    nsebt restore SRC [--apply]     verify a backup and restore missing/different files
    nsebt doctor  [--backup DIR]    check environment, config, data, store (and a backup)
"""
from __future__ import annotations

import hashlib
import importlib
import json
import platform
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from .audit import is_junk
from .config import Config

BACKUP_DIR = "nsebt-backup"
MANIFEST = "manifest.json"
REQUIRED = ["pandas", "numpy", "pyarrow", "requests", "yaml"]


def sha256(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def _include(cfg: Config) -> list[Path]:
    inc = cfg.raw.get("backup", {}).get("include", [])
    return [(cfg.root / i) for i in inc]


def _files(cfg: Config):
    for base in _include(cfg):
        if base.is_file():
            yield base
        elif base.is_dir():
            for p in sorted(base.rglob("*")):
                if p.is_file() and not is_junk(p.relative_to(cfg.root)) and not p.name.endswith((".part", ".tmp")):
                    yield p


def backup(cfg: Config, dest: Path, log=print) -> dict:
    root = Path(dest) / BACKUP_DIR
    files_dir = root / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    mpath = root / MANIFEST
    old = json.loads(mpath.read_text())["files"] if mpath.exists() else {}
    new, copied, unchanged = {}, 0, 0
    for p in _files(cfg):
        rel = p.relative_to(cfg.root).as_posix()
        st = p.stat()
        prev = old.get(rel)
        dst = files_dir / rel
        if prev and prev["size"] == st.st_size and prev["mtime"] == int(st.st_mtime) and dst.exists():
            new[rel] = prev
            unchanged += 1
            continue
        digest = sha256(p)
        if not (prev and prev["sha256"] == digest and dst.exists()):
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".part")
            shutil.copy2(p, tmp)
            tmp.replace(dst)
            copied += 1
        else:
            unchanged += 1
        new[rel] = {"sha256": digest, "size": st.st_size, "mtime": int(st.st_mtime)}
    # code: a git bundle with every branch and commit
    bundle = root / "repo.bundle"
    git_ok = subprocess.run(["git", "-C", str(cfg.root), "bundle", "create", str(bundle) + ".part", "--all"],
                            capture_output=True, text=True)
    if git_ok.returncode == 0:
        Path(str(bundle) + ".part").replace(bundle)
    man = {"created": datetime.now().isoformat(timespec="seconds"), "host": platform.node(),
           "files": new, "total_bytes": sum(v["size"] for v in new.values()),
           "git_bundle": git_ok.returncode == 0}
    tmp = mpath.with_suffix(".tmp")
    tmp.write_text(json.dumps(man, indent=0))
    tmp.replace(mpath)
    rep = {"dest": str(root), "files": len(new), "copied": copied, "unchanged": unchanged,
           "bytes": man["total_bytes"], "git_bundle": man["git_bundle"],
           "git_error": git_ok.stderr.strip()[:200] if git_ok.returncode else ""}
    with open(root / "backup_log.txt", "a") as f:
        f.write(json.dumps({"at": man["created"], **{k: rep[k] for k in ("files", "copied", "bytes")}}) + "\n")
    return rep


def verify(src: Path) -> dict:
    root = Path(src) / BACKUP_DIR if (Path(src) / BACKUP_DIR).exists() else Path(src)
    man = json.loads((root / MANIFEST).read_text())
    bad, missing = [], []
    for rel, meta in man["files"].items():
        p = root / "files" / rel
        if not p.exists():
            missing.append(rel)
        elif p.stat().st_size != meta["size"] or sha256(p) != meta["sha256"]:
            bad.append(rel)
    return {"root": str(root), "created": man["created"], "files": len(man["files"]),
            "missing": missing, "corrupt": bad, "ok": not bad and not missing,
            "git_bundle": (root / "repo.bundle").exists()}


def restore(cfg_root: Path, src: Path, apply: bool = False) -> dict:
    """Restore files that are missing locally or differ from the backup. Without
    apply=True only reports what would change."""
    v = verify(src)
    if not v["ok"]:
        return {"verified": v, "restored": [], "note": "backup failed verification; nothing restored"}
    root = Path(v["root"])
    man = json.loads((root / MANIFEST).read_text())
    todo = []
    for rel, meta in man["files"].items():
        local = Path(cfg_root) / rel
        if not local.exists() or local.stat().st_size != meta["size"] or sha256(local) != meta["sha256"]:
            todo.append(rel)
    if apply:
        for rel in todo:
            dst = Path(cfg_root) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / "files" / rel, dst)
    return {"verified": v, "to_restore": len(todo), "applied": apply, "sample": todo[:10]}


def doctor(cfg: Config, backup_dir: Path | None = None, run_tests: bool = False) -> list[tuple[str, str, str]]:
    out = []

    def add(name, ok, detail=""):
        out.append((name, "ok" if ok is True else ("WARN" if ok is None else "FAIL"), str(detail)))

    add("python", sys.version_info[:2] >= (3, 11), platform.python_version())
    for m in REQUIRED:
        try:
            mod = importlib.import_module(m)
            add(f"package {m}", True, getattr(mod, "__version__", ""))
        except Exception as e:  # noqa: BLE001
            add(f"package {m}", False, e)
    add("config", True, cfg.root / "config.yaml")
    for k in ("bloomberg_root", "nse_raw", "store"):
        add(f"path {k}", cfg.path(k).exists(), cfg.path(k))
    raw = cfg.path("nse_raw")
    for s in ("cm_legacy", "cm_udiff", "mto", "indices", "corp_actions"):
        n = len([p for p in (raw / s).rglob("*") if p.is_file()]) if (raw / s).exists() else 0
        add(f"raw {s}", n > 0, f"{n} files")
    rep = cfg.path("store") / "build_report.json"
    if rep.exists():
        b = json.loads(rep.read_text())
        last = date.fromisoformat(b["last_date"])
        stale = (date.today() - last).days
        add("store", b.get("n_parse_errors", 0) == 0, f"{b['rows']:,} rows to {last}, built {b['built_utc']}")
        add("store freshness", True if stale <= 4 else None, f"last trading day {stale} day(s) ago")
    else:
        add("store", False, "not built: run `nsebt build`")
    g = subprocess.run(["git", "-C", str(cfg.root), "status", "--porcelain"], capture_output=True, text=True)
    add("git", g.returncode == 0, "clean" if not g.stdout.strip() else f"{len(g.stdout.splitlines())} uncommitted change(s)")
    r = subprocess.run(["git", "-C", str(cfg.root), "remote", "-v"], capture_output=True, text=True)
    add("git remote", True if r.stdout.strip() else None, r.stdout.split("\n")[0] or "none (code only on this disk)")
    if backup_dir:
        try:
            v = verify(backup_dir)
            add("backup", v["ok"], f"{v['files']} files from {v['created']}; missing {len(v['missing'])}, corrupt {len(v['corrupt'])}")
        except Exception as e:  # noqa: BLE001
            add("backup", False, e)
    if run_tests:
        t = subprocess.run([sys.executable, "-m", "pytest", "-q", str(cfg.root / "nsebt" / "tests")],
                           capture_output=True, text=True)
        add("tests", t.returncode == 0, (t.stdout.strip().splitlines() or [""])[-1])
    return out
