"""Python reference strategies (registered by name)."""
from __future__ import annotations

from . import register
from .base import Context, Output, Strategy, rank_desc, rebalance_days


@register
class Momentum(Strategy):
    """12-1 momentum, top-N among point-in-time members that traded, monthly rebalance.
    Must produce the same trades as strategies/momentum.yaml."""
    name = "momentum"
    defaults = {"lookback": 252, "skip": 21, "top_n": 3}

    def generate(self, ctx: Context) -> Output:
        p = self.params
        mom = ctx.indicator("momentum_12_1", lookback=p["lookback"], skip=p["skip"])
        close = ctx.panel.close
        r = rank_desc(mom, ctx.members & close.notna())
        defined = mom.notna()
        top = (r <= p["top_n"]) & defined
        on = rebalance_days(close.index, "monthly").to_numpy()[:, None]
        # out of the top (or no longer a ranked member) on a day its momentum is defined: sell
        return Output(entry=top & on, exit=~top & defined & on, score=mom, warmup=defined.cummax())
