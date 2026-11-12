"""12-1 momentum: return from 12 months ago to 1 month ago, skipping the latest month.

Prices in the Bloomberg extract are not adjusted for splits/bonuses, so momentum is
built from daily returns with corporate-action-sized jumps zeroed out.
"""
import numpy as np
import pandas as pd


class Momentum:
    name = "momentum"
    kind = "portfolio"

    def __init__(self, lookback: int = 252, skip: int = 21, min_history: int = 240,
                 min_traded_value: float = 1e7, ca_filter: float = 0.35):
        self.lookback, self.skip, self.min_history = lookback, skip, min_history
        self.min_traded_value, self.ca_filter = min_traded_value, ca_filter

    def scores(self, close: pd.DataFrame, volume: pd.DataFrame) -> pd.DataFrame:
        r = close.pct_change(fill_method=None)
        r = r.where(r.abs() <= self.ca_filter, 0.0)
        logc = np.log1p(r.fillna(0.0)).cumsum()
        mom = np.exp(logc.shift(self.skip) - logc.shift(self.lookback)) - 1
        history = close.notna().rolling(self.lookback).sum() >= self.min_history
        traded = (close * volume).rolling(60, min_periods=40).median() >= self.min_traded_value
        return mom.where(history & traded & close.notna())
