# Phase 3 checkpoint: indicator and feature library

Built 2026-11-13.

## 1. Summary

- **Every indicator in the plan** is implemented, parameterised, vectorised across securities, and computed on adjusted prices.
- **All of them match TA-Lib** on every one of the 903 Nifty 500 securities: about 40 million values compared, worst relative difference 1e-8 (most 1e-11 or better), with no missing-value differences except one documented convention.
- **No look-ahead:** every indicator gives identical values when the data is cut off at day *t* (tested at three cut-off points).
- **Speed:** the whole library for all 903 securities over 10 years takes **about 5 seconds**.
- **Caching:** results are saved to disk by indicator and parameters. New days are computed incrementally, and a full recompute happens automatically if past inputs change.
- **Tests:** 79 in total (27 new).

## 2. The library (`nsebt/indicators.py`)

All functions take wide tables (rows = dates, columns = securities) and return the same shape. Every parameter is configurable, e.g. `macd(close, fast=12, slow=26, signal=9)` or `sma(close, n)`.

| Group | Functions |
|---|---|
| Trend | `sma(x, n)`, `ema(x, n)`, `price_vs(x, ref)` (e.g. close vs 200-DMA), `cross_up(a, b)` / `cross_down(a, b)` (golden/death cross = `cross_up(sma(c,50), sma(c,200))`), `slope(x, n)` |
| Momentum | `macd(close, fast, slow, signal)` → line, signal, hist (MACD crossovers = `cross_up(line, signal)`); `rsi(close, n)`; `stochastic(high, low, close, n, d, smooth)` → %K, %D; `roc(x, n)` / `returns(x, n)` (1/3/6/12 months = 21/63/126/252); `momentum_12_1(close, lookback, skip)` |
| Volatility | `atr(h, l, c, n)`, `true_range`, `bollinger(close, n, k)` → mid, upper, lower, %B, bandwidth; `keltner(h, l, c, n, atr_n, mult)`; `hist_vol(close, n)` (annualised, log returns) |
| Trend strength | `adx(h, l, c, n)` → ADX, +DI, −DI; `aroon(h, l, n)` → up, down, oscillator |
| Volume and delivery | `volume_ratio(volume, n)`, `obv(close, volume)`, `delivery_spike(deliv_qty, n)`; delivery % and turnover are panel fields (average them with `sma`) |
| Price levels | `highest(x, n)` / `lowest(x, n)` (52-week = 252), distance via `price_vs`, `gap(open, close)` |
| Relative | `relative_strength(close, bench, n)` vs Nifty 500 (`panel.index("Nifty 500")`) or vs sector (`panel.sector_bench()`); `rank(x, mask)` = cross-sectional percentile on each date, optionally among index members only |

A registry (`indicators.REGISTRY`) lists each indicator's inputs, default parameters, outputs and the history it needs. The Phase 4 rule parser will use it.

**Data access** (`nsebt/data.py`): `load_panel(store, "nifty500" | "all" | [ids])` returns adjusted fields (`open, high, low, close, volume, vwap, deliv_qty`) for indicators, raw fields (`raw_open, raw_close, …, turnover, deliv_pct`) for fills and sizing, the membership mask, and the index levels. Loading the Nifty 500 panel takes about 7 seconds.

**Gaps:** a security with a suspension in its history is computed on its own trading days, so a 20-day window always means 20 trading days. A test checks this on a real suspended stock.

## 3. Reference check against TA-Lib 0.8.3 (all 903 securities)

Re-run with `nsebt check-indicators`. Results in `reports/phase3/reference_check.csv`.

| Indicator | Values compared | Max relative difference | First-value mismatches |
|---|---|---|---|
| SMA(50), SMA(200) | 3.5M | 5e-13 | 0 |
| EMA(20), EMA(200) | 3.5M | 9e-15 | 0 |
| MACD line / signal / hist | 5.5M | 3e-11 | 0 |
| RSI(14) | 1.84M | 2e-14 | 0 |
| Stochastic %K(14) / %D(3) | 3.7M | 4e-13 | 1,806 (see below) |
| ROC(20) | 1.84M | 0 | 0 |
| ATR(14) | 1.84M | 1e-15 | 0 |
| Bollinger upper / lower | 3.7M | 1e-8 | 0 |
| ADX, +DI, −DI (14) | 5.5M | 4e-15 | 0 |
| Aroon up / down (25) | 3.7M | 0 | 0 |
| OBV, true range | 3.7M | 0 | 0 |
| Highest high / lowest low (252) | 3.3M | 0 | 0 |

**Differences from TA-Lib, and the choices made:**

1. **Seeding.** Exponential averages (EMA, and Wilder smoothing in RSI, ATR and ADX) start from the simple average of the first *n* values, exactly as TA-Lib does. A plain `pandas.ewm` starts from the first price instead. The old Bloomberg feature file did that, so its RSI and MACD differ early on and converge after about 100–150 days (section 5).
2. **MACD.** TA-Lib's `MACD()` function uses rounded constants (0.15 and 0.075) for the default 12/26 periods instead of 2/(n+1). That gives a persistent difference of up to 0.23 in the line. The library uses the standard constants and is checked against TA-Lib's `MACDEXT`, which uses them too. As in TA-Lib, the fast and slow EMAs start on the same day.
3. **Stochastic %K.** TA-Lib withholds %K for the 2 days before %D exists. The library reports %K as soon as it can be computed. The values are identical (that's the 1,806 = 903 × 2 mismatches).
4. **Bollinger** uses the population standard deviation (ddof = 0), as TA-Lib does. The 1e-8 difference is floating-point accumulation in TA-Lib's running sums.
5. **Units.** Returns, ROC, slopes and distances are fractions (0.05 = 5%). TA-Lib's ROC is in percent. RSI, stochastic, ADX/DI and Aroon use their usual 0–100 scale.
6. **Not in TA-Lib** and checked by definition instead: Keltner (EMA(20) ± 2 × ATR(10)), annualised historical volatility, 12-1 momentum, relative strength, delivery spike, cross-sectional rank.

## 4. No look-ahead

`test_no_lookahead_any_indicator` computes **every registered indicator and every output**, plus relative strength, rank, crossovers, gaps, momentum, Keltner and delivery spike. It does this on the full sample and on the sample cut off at rows 60, 300 and 600, and requires identical values up to the cut-off. All pass.

## 5. Compared with the old Bloomberg feature file (`reports/phase3/old_features_regression.csv`)

| Old column | New | Agreement |
|---|---|---|
| DMA_20, DMA_200 | `sma(20)`, `sma(200)` | identical |
| Return_12M | `roc(252)` | identical |
| RSI | `rsi(14)` | identical in the median; early values differ (seeding, item 1) and agree within 0.05 points after 100 days |
| MACD | `macd().line` | identical in the median; early values differ (seeding) and converge after about 150 days |
| Volatility_20D | std of simple daily returns | reproduced exactly; the new `hist_vol` is annualised and uses log returns |

So the old file's numbers were consistent with its own definitions. The new library uses standard definitions and the NSE data.

## 6. Feature cache (`nsebt/features.py`)

```python
fc = FeatureCache("data/features", panel)
rsi = fc.get("rsi", n=14)
m = fc.get("macd", fast=12, slow=26, signal=9)      # m.line, m.signal, m.hist
```

- The cache key is the indicator name plus its parameters, e.g. `macd__fast=12_signal=9_slow=26`.
- **New days only:** if the cached inputs are unchanged up to the cached date, the indicator is recomputed on just enough history (from the registry) plus the new days, and the new rows are appended. Tested equal to a full recompute.
- **Full recompute when history changes:** a new split re-bases past adjusted prices, and the input fingerprint catches it. Also tested.
- Indicators whose exact restart needs more history than is cached (e.g. MACD needs about 875 rows; OBV is cumulative) are recomputed in full. That takes under a second.

## 7. Open items

- Industry tags exist only for current Nifty 500 members, so former members use Nifty 500 as their sector benchmark.
- Delivery data starts with each stock's first delivery file. SME and trade-for-trade series have occasional missing delivery days.
- Next: **Phases 4–5**, the YAML rule parser and Python strategies, then the realistic ₹36,000 / 3-position engine with Zerodha costs and the honesty checks. Then the three example strategies with full reports.
