# Validation: position ledger and corporate-action data (amendment 2.6)

Store version: 37ec64d1644bb96f. Universe: every Nifty 500 member 2016-2026 (906 in membership; 903 with prices in the panel), 2530 days.

| Check | What it proves | Result |
|---|---|---|
| (a) Reinvest-equivalent engine vs total-return adjusted close, every day, 1e-6 | The engine applies the corporate-action table correctly. Both sides read the same table, so table errors are not visible here | 903 of 903 securities pass; largest error 4.4e-16; 1,853,369 security-days |
| (b) Receivable mode: value preserved on each ex-date, 1e-6 | Code path for receivables; table prior close agrees with market prices | 8128 of 8128 events (dividend 7517, special_dividend 471, arrangement 75, rights 65) |
| (c) Table vs prices: combined factor vs ex-date open and close moves | Independent check of the table itself | 5 of 784 member event-days differ by more than 10% on both; listed below |
| (d) Full ledger run: every member held through all actions, conversions and lifecycle exits | Accounting identity and no negative cash with mergers.csv and lifecycle applied | PASS: error 0.0e+00, 8542 ledger events (49 conversions, 32 lifecycle exits), 0 warnings |

Flagged by (c), each reviewed:

| Security | Date | Events | Factor | Open gap | Close move |
|---|---|---|---|---|---|
| EASEMYTRIP | 2022-11-21 | split+bonus | 0.1250 | 0.1388 | 0.1500 |
| IGARASHI | 2018-09-27 | bonus | 0.8899 | 0.9807 | 0.9791 |
| ONGC | 2020-03-23 | special_dividend | 0.9309 | 0.8383 | 0.8348 |
| NAZARA | 2022-06-24 | bonus | 0.5000 | 0.5529 | 0.5950 |
| VSTIND | 2024-09-06 | bonus | 0.0909 | 0.1065 | 0.1080 |

Warnings from (d): none

| Timing | Seconds |
|---|---|
| Load the Nifty 500 panel with acquirers, corporate actions, mergers and lifecycle (`market.from_store`) | 5.1 |
| One backtest, 3 positions, random signals, full costs | 0.6 |
| Single backtest including the load (target ≤ 30) | 5.7 |
| Reconciliation runs (a) and (b), every security held | 3.3 + 3.5 |

Method: `nsebt/nsebt/backtest/reconcile.py` (module docstring states what each check proves).
