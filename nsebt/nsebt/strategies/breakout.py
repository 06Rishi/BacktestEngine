"""20-day breakout above the 50-day average on heavy volume; exit on a 10-day low."""
import pandas as pd


class Breakout:
    name = "breakout"
    kind = "trades"

    def __init__(self, hi: int = 20, lo: int = 10, sma: int = 50, vol_mult: float = 1.2):
        self.hi, self.lo, self.sma, self.vol_mult = hi, lo, sma, vol_mult

    def signals(self, d: pd.DataFrame) -> pd.DataFrame:
        c = d["Close"]
        hi = d["High"].rolling(self.hi).max().shift(1)
        lo = d["Low"].rolling(self.lo).min().shift(1)
        sma = c.rolling(self.sma).mean()
        vavg = d["Volume"].rolling(20).mean()
        entry = (c > hi) & (c > sma) & (d["Volume"] > self.vol_mult * vavg)
        return pd.DataFrame({"entry": entry, "exit": c < lo}, index=d.index)
