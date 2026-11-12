"""Disk cache for indicators, keyed by indicator name and parameters.

    fc = FeatureCache("data/features", panel)
    rsi14 = fc.get("rsi", n=14)
    m = fc.get("macd", fast=12, slow=26, signal=9)      # m.line, m.signal, m.hist

When the panel has new days at the end and everything before them is unchanged, only
the new days are computed: the indicator is run on the last `lookback` rows plus the
new rows (enough history for an exact value) and the new rows are appended. If past
inputs changed (a new corporate action re-bases adjusted history, or the set of
securities changed), the indicator is recomputed in full.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from . import indicators as I


def _key(name: str, params: dict) -> str:
    return name + "__" + "_".join(f"{k}={params[k]}" for k in sorted(params))


def _hash(frames: list[pd.DataFrame]) -> str:
    h = hashlib.sha256()
    for f in frames:
        h.update(pd.util.hash_pandas_object(f, index=True).to_numpy().tobytes())
        h.update("|".join(map(str, f.columns)).encode())
    return h.hexdigest()


class FeatureCache:
    def __init__(self, cache_dir: Path, panel):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.panel = panel
        self.last_mode = None          # 'full' | 'incremental' | 'cached' (for reports and tests)

    def _inputs(self, spec):
        get = (lambda f: self.panel[f]) if isinstance(self.panel, dict) else (lambda f: getattr(self.panel, f))
        return [get(f) for f in spec.inputs]

    def get(self, name: str, **params):
        spec = I.REGISTRY[name]
        p = {**spec.params, **params}
        key = _key(name, p)
        meta_path = self.dir / f"{key}.json"
        outs = spec.outputs or ("value",)
        ins = self._inputs(spec)
        idx, cols = ins[0].index, ins[0].columns

        meta = json.loads(meta_path.read_text()) if meta_path.exists() else None
        if meta and meta["columns"] == list(map(str, cols)):
            last = pd.Timestamp(meta["last"])
            if last in idx:
                pos = idx.get_loc(last)
                if _hash([f.iloc[:pos + 1] for f in ins]) == meta["hash"]:
                    cached = {o: pd.read_parquet(self.dir / f"{key}__{o}.parquet") for o in outs}
                    if pos == len(idx) - 1:
                        self.last_mode = "cached"
                        return self._wrap(spec, cached)
                    L = spec.lookback(p)
                    if L < pos:
                        start = pos - L
                        res = spec.fn(*[f.iloc[start:] for f in ins], **p)
                        res = self._parts(spec, res)
                        merged = {o: pd.concat([cached[o], res[o].iloc[pos - start + 1:]]) for o in outs}
                        self._save(key, merged, ins, idx)
                        self.last_mode = "incremental"
                        return self._wrap(spec, merged)
        res = self._parts(spec, spec.fn(*ins, **p))
        self._save(key, res, ins, idx)
        self.last_mode = "full"
        return self._wrap(spec, res)

    @staticmethod
    def _parts(spec, res) -> dict:
        if not spec.outputs:
            return {"value": res}
        return {o: getattr(res, o) for o in spec.outputs}

    @staticmethod
    def _wrap(spec, parts: dict):
        return parts["value"] if not spec.outputs else SimpleNamespace(**parts)

    def _save(self, key, parts, ins, idx):
        for o, f in parts.items():
            f = f.copy()
            f.columns = f.columns.map(str)
            tmp = self.dir / f"{key}__{o}.parquet.tmp"
            f.to_parquet(tmp)
            tmp.replace(self.dir / f"{key}__{o}.parquet")
        meta = {"last": str(idx[-1].date()), "columns": list(map(str, ins[0].columns)), "hash": _hash(ins)}
        (self.dir / f"{key}.json").write_text(json.dumps(meta))
