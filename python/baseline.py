"""Placebo test: random entries on the same tickers with the same hold lengths.

Usage: python3 baseline.py <path to DE/data> <trades.csv from backtest.py>
"""
import sys, os, importlib.util
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("bt", os.path.join(HERE, "backtest.py"))
bt = importlib.util.module_from_spec(spec); spec.loader.exec_module(bt)

def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    frames = bt.load_prices(sys.argv[1])
    T = pd.read_csv(sys.argv[2], parse_dates=["entry_date", "exit_date"])
    rng = np.random.default_rng(0)
    rand = []
    for t, sub in T.groupby("ticker"):
        d = frames.get(t)
        if d is None:
            continue
        op, ret = d["Open"].values, d["Close"].pct_change().values
        for days in sub["days"]:
            for _ in range(3):          # 3 placebo entries per real trade
                k = rng.integers(60, len(d) - days - 1)
                if np.nanmax(np.abs(ret[k:k + days + 1])) > bt.CA_FILTER:
                    continue            # same corporate-action filter as the real trades
                rand.append(op[k + days] / op[k] - 1 - bt.COST)
    rand = np.array(rand)
    print(f"placebo  n={len(rand):6d} avg={rand.mean():+.4f} median={np.median(rand):+.4f} win={(rand > 0).mean():.3f}")
    print(f"signal   n={len(T):6d} avg={T.ret.mean():+.4f} median={T.ret.median():+.4f} win={(T.ret > 0).mean():.3f}")

if __name__ == "__main__":
    main()
