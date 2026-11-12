"""Phase 2: manual ingest, missing-day classification, backup/restore round trip."""
import json
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from nsebt import config, maintenance, nsefetch, update

FX = Path(__file__).parent / "fixtures"


def test_identify_file_names():
    assert nsefetch.identify("cm29AUG2016bhav.csv.zip") == ("cm_legacy", date(2016, 8, 29))
    assert nsefetch.identify("BhavCopy_NSE_CM_0_0_0_20261111_F_0000.csv.zip") == ("cm_udiff", date(2026, 11, 11))
    assert nsefetch.identify("MTO_11112026.DAT") == ("mto", date(2026, 11, 11))
    assert nsefetch.identify("ind_close_all_29082016.csv") == ("indices", date(2016, 8, 29))
    assert nsefetch.identify("holiday photo.jpg") is None


def test_ingest_checks_and_files_correctly(tmp_path):
    bad = tmp_path / "MTO_01012020.DAT"
    bad.write_text("<html>blocked</html>")
    res = dict(nsefetch.ingest([FX / "cm29AUG2016bhav.csv.zip", bad, FX / "cm29AUG2016bhav.csv.zip"], tmp_path / "raw"))
    assert (tmp_path / "raw/cm_legacy/2016/cm29AUG2016bhav.csv.zip").exists()
    assert res["MTO_01012020.DAT"].startswith("rejected")
    again = dict(nsefetch.ingest([FX / "cm29AUG2016bhav.csv.zip"], tmp_path / "raw"))
    assert again["cm29AUG2016bhav.csv.zip"] == "already present"


def test_classify_missing_days():
    hol = pd.DataFrame({"date": [date(2026, 11, 10)], "description": ["Diwali-Balipratipada"]})
    eve = datetime(2026, 11, 13, 17, 0, tzinfo=update.IST)
    assert update.classify_missing(date(2026, 11, 10), hol, eve).startswith("holiday")
    assert update.classify_missing(date(2026, 11, 7), hol, eve) == "weekend"
    assert update.classify_missing(date(2026, 11, 13), hol, eve).startswith("not published yet")
    assert update.classify_missing(date(2026, 11, 12), hol, eve).startswith("MISSING")


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "data/nse/raw/mto/2016").mkdir(parents=True)
    shutil.copy(FX / "MTO_29082016.DAT", root / "data/nse/raw/mto/2016/")
    (root / "notes.csv").write_text("a,b\n1,2\n")
    (root / "config.yaml").write_text(
        "paths: {nse_raw: data/nse/raw, store: data/store, bloomberg_root: bbg, logs: logs, reports: reports}\n"
        "backup: {include: [data/nse/raw, notes.csv, config.yaml]}\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)
    return root


def test_backup_verify_corruption_and_restore(project, tmp_path):
    cfg = config.load(project / "config.yaml")
    dest = tmp_path / "drive"
    dest.mkdir()
    r = maintenance.backup(cfg, dest)
    assert r["files"] == 3 and r["copied"] == 3 and r["git_bundle"]
    assert maintenance.backup(cfg, dest)["copied"] == 0                 # incremental: nothing new
    assert maintenance.verify(dest)["ok"]

    # corrupt the backup copy -> verification fails and nothing is restored
    f = dest / "nsebt-backup/files/notes.csv"
    f.write_text("tampered")
    v = maintenance.verify(dest)
    assert not v["ok"] and v["corrupt"] == ["notes.csv"]
    assert maintenance.restore(project, dest, apply=True).get("restored") == []

    # fresh backup, then lose a local file and restore it
    f.write_text("a,b\n1,2\n")
    maintenance.backup(cfg, dest)
    (project / "data/nse/raw/mto/2016/MTO_29082016.DAT").unlink()
    dry = maintenance.restore(project, dest)
    assert dry["to_restore"] == 1 and not (project / "data/nse/raw/mto/2016/MTO_29082016.DAT").exists()
    maintenance.restore(project, dest, apply=True)
    assert (project / "data/nse/raw/mto/2016/MTO_29082016.DAT").read_bytes() == (FX / "MTO_29082016.DAT").read_bytes()
