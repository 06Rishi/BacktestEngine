"""Breakout backtest on the DE Bloomberg price folder.

Usage: python3 backtest.py <path to DE/data> <output trades.csv>
"""
import sys, glob, os
import numpy as np, pandas as pd

COST = 0.002                 # round-trip cost assumption
MAX_HOLD = 15                # max trading days held (entry open -> exit open)
SPLIT = pd.Timestamp("2022-01-01")
CA_FILTER = 0.35             # skip windows with a daily move this big (likely split/bonus)

def load_prices(base):
    files = glob.glob(os.path.join(base, "raw", "prices", "*.parquet"))
    if not files:
        sys.exit(f"No price files found under {os.path.join(base, 'raw', 'prices')}. "
                 "Pass the extracted DE/data folder.")
    frames = {}
    need = {"Date", "Ticker", "PX_OPEN", "PX_HIGH", "PX_LOW", "PX_LAST", "PX_VOLUME"}
    for f in files:
        df = pd.read_parquet(f)
        if not need <= set(df.columns) or df.empty:
            continue
        t = df["Ticker"].iloc[0]
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.drop_duplicates("Date").set_index("Date").sort_index()
        df = df[["PX_OPEN", "PX_HIGH", "PX_LOW", "PX_LAST", "PX_VOLUME"]]
        df.columns = ["Open", "High", "Low", "Close", "Volume"]
        df = df[(df[["Open", "High", "Low", "Close"]] > 0).all(axis=1)].dropna()
        if len(df) > 300:
            frames[t] = df
    return frames

def signals(d):
    d = d.copy()
    d["sma50"] = d["Close"].rolling(50).mean()
    d["hi20"] = d["High"].rolling(20).max().shift(1)
    d["lo10"] = d["Low"].rolling(10).min().shift(1)
    d["volavg"] = d["Volume"].rolling(20).mean()
    d["ret"] = d["Close"].pct_change()
    d["entry"] = (d["Close"] > d["hi20"]) & (d["Close"] > d["sma50"]) & (d["Volume"] > 1.2 * d["volavg"])
    d["exit"] = d["Close"] < d["lo10"]
    return d

def trades_for(d):
    """Signal on close of day i-1 -> buy open of day i.
    Exit signal on close of day j -> sell open of day j+1, or after MAX_HOLD days."""
    out, i, n = [], 60, len(d)
    entry, exit_, op, ret, idx = (d["entry"].values, d["exit"].values,
                                  d["Open"].values, d["ret"].values, d.index)
    while i < n - 1:
        if not entry[i - 1]:
            i += 1
            continue
        entry_i, j = i, i
        while j < min(entry_i + MAX_HOLD - 1, n - 1) and not exit_[j]:
            j += 1
        exit_i = j + 1
        if exit_i >= n:                     # trade still open at end of data -> drop
            break
        if np.nanmax(np.abs(ret[entry_i:exit_i + 1])) > CA_FILTER:
            i = exit_i + 1
            continue
        out.append({"entry_date": idx[entry_i], "exit_date": idx[exit_i],
                    "ret": op[exit_i] / op[entry_i] - 1 - COST, "days": exit_i - entry_i})
        i = exit_i + 1
    return out

def ew_open_to_open(frames):
    """Equal-weight universe return from open(t) to open(t+1), indexed by t."""
    opens = pd.DataFrame({t: d["Open"] for t, d in frames.items()})
    r = opens.shift(-1) / opens - 1
    return r.where(r.abs() < CA_FILTER).mean(axis=1)

def summ(x):
    return pd.Series({
        "trades": len(x),
        "win_rate": (x["ret"] > 0).mean(),
        "avg_ret": x["ret"].mean(),
        "median_ret": x["ret"].median(),
        "avg_excess_vs_EW": x["excess"].mean(),
        "beats_EW_rate": (x["excess"] > 0).mean(),
        "avg_days": x["days"].mean(),
    })

def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    base, out_csv = sys.argv[1], sys.argv[2]
    frames = load_prices(base)
    print(f"loaded {len(frames)} securities")
    ew = ew_open_to_open(frames)
    rows = []
    for t, d in frames.items():
        for tr in trades_for(signals(d)):
            tr["ticker"] = t
            # same window as the trade: open(entry) -> open(exit)
            w = ew.loc[tr["entry_date"]:tr["exit_date"]].iloc[:-1]
            tr["bench"] = (1 + w).prod() - 1
            rows.append(tr)
    if not rows:
        sys.exit("No trades generated.")
    T = pd.DataFrame(rows).dropna()
    T["excess"] = T["ret"] - T["bench"]
    T["period"] = np.where(T["entry_date"] < SPLIT, "in-sample 2016-2021", "out-of-sample 2022-2026")
    T.to_csv(out_csv, index=False)
    print("\nALL:\n", summ(T).round(4).to_string())
    print("\nBY PERIOD:\n", T.groupby("period").apply(summ).round(4).to_string())
    print(f"\ntrades written to {out_csv}")

if __name__ == "__main__":
    main()
