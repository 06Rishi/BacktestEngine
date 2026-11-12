"""M1.2: Zerodha delivery costs, worked examples from docs/phase45_amendments.md (A7)."""
from pathlib import Path

import pytest
import yaml

from nsebt.backtest.costs import CostModel, DPTracker

ROOT = Path(__file__).resolve().parents[2]


def test_worked_example_12000_round_trip():
    c = CostModel()
    buy = c.order("buy", 12_000)
    sell = c.order("sell", 12_000, dp=True)
    tot = buy + sell
    assert tot.stt == pytest.approx(24.00)
    assert tot.transaction == pytest.approx(0.7368)
    assert tot.sebi == pytest.approx(0.024)
    assert tot.gst == pytest.approx(0.13694, abs=1e-5)
    assert tot.stamp == pytest.approx(1.80)
    assert tot.dp == pytest.approx(15.34)
    assert tot.total == pytest.approx(42.04, abs=0.01)
    assert tot.total / 12_000 == pytest.approx(0.0035, abs=1e-5)


def test_stamp_buy_only_and_dp_sell_only():
    c = CostModel()
    assert c.order("sell", 10_000).stamp == 0
    assert c.order("buy", 10_000, dp=True).dp == 0


def test_gst_base_excludes_stt_and_stamp():
    c = CostModel(brokerage=20)
    ch = c.order("buy", 100_000)
    assert ch.gst == pytest.approx(0.18 * (20 + ch.transaction + ch.sebi))


def test_no_ipft_key_anywhere():
    assert not any("ipft" in f for f in CostModel.__dataclass_fields__)
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    assert "costs" in raw and not any("ipft" in k.lower() for k in raw["costs"])
    with pytest.raises(ValueError):
        CostModel.from_config({"costs": {"ipft_per_crore": 10}})


def test_config_matches_defaults():
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    assert CostModel.from_config(raw) == CostModel()


def test_dp_once_per_scrip_per_sell_day():
    t = DPTracker()
    assert t.due("2024-01-02", "A") and not t.due("2024-01-02", "A")
    assert t.due("2024-01-02", "B") and t.due("2024-01-03", "A")


def test_slippage_moves_price_per_side():
    c = CostModel()
    assert c.fill_price("buy", 100) == pytest.approx(100.10)
    assert c.fill_price("sell", 100) == pytest.approx(99.90)
    z = c.with_slippage(0)
    assert z.fill_price("buy", 100) == 100 and z.fill_price("sell", 100) == 100
    a = c.with_slippage(25, 10)
    assert a.fill_price("buy", 100) == pytest.approx(100.25) and a.fill_price("sell", 100) == pytest.approx(99.90)


def test_negative_rates_rejected():
    with pytest.raises(ValueError):
        CostModel(stt_pct=-0.1)
