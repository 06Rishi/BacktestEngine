# Reconciliation: position ledger vs adjusted prices (amendment 2.6)

Store version: f74312a5eb8204af. Universe: every Nifty 500 member 2016-2026 (903 securities, 2530 days).

| Check | Result |
|---|---|
| (a) reinvest-equivalent: securities checked | 903 |
| (a) securities with any day outside 1e-6 | 0 (0.00%; stop rule S4 above 0.5%) |
| (a) largest daily error | 4.4e-16 |
| (a) security-days compared | 1,853,369 |
| (b) receivable mode: value events checked | 8128 (dividend 7517, special_dividend 471, arrangement 75, rights 65) |
| (b) events outside 1e-6 | 0 |
| Value events with no factor (excluded) | 0 |
| Result | PASS |

| Timing | Seconds |
|---|---|
| Load Nifty 500 panel (raw + total-return adjusted) | 5.4 |
| Reinvest-equivalent run, all securities held | 3.1 |
| Receivable-mode run, all securities held | 3.4 |
| One backtest, 3 positions, random signals, full costs | 0.7 |
| Single backtest including load (target ≤ 30) | 6.1 |

Method and assumptions: see `nsebt/nsebt/backtest/reconcile.py`. (a) reinvests every value event as shares ÷ factor, so it can match the total-return series exactly; the default engine holds dividends as receivables, which drift from the reinvested series by about D/P × the next day's return, so it is checked by (b), value preservation on each ex-date.
