# Securities that stopped trading: conversions and outcomes

Date 2026-11-13. Covers all 82 Nifty 500 members (2016–2026) whose NSE trading ended before the data end.
Sources: NSE corporate announcements and their attached filings (fetched to `data/nse/raw/announcements/`), NSE's delisted and suspension lists, and press reports where noted.

| Outcome | Members | Engine treatment |
|---|---|---|
| Converted (merger, scheme or capital-reduction relisting) | 49 | `mergers.csv`: shares of the acquirer(s) at the scheme ratio, plus any cash |
| Delisted, value stated in a source | 17 | Exit at the stated value (₹0 where the equity was extinguished) |
| Delisted, payout not stated | 5 + 4 on NSE's delisted list | Delisting policy (default `haircut(50)`) |
| Suspended, no final outcome by the data end | 7 | Held at the last close (amendment 2.4) |
| Unknown | 0 | — |

## Conversions

Value check: the parent's last close against what the holder received at the acquirers' first prices after the conversion. Gaps of more than a few days are listing delays (the acquirer listed later), so the change includes the market move over that period.

| Parent | Last trade | Receives per share | Value change | Days to first price | Source |
|---|---|---|---|---|---|
| SBBJ | 2017-03-15 | 28/10 SBIN | +1.3% | 1 | SBI board 18-Aug-2016 swap ratio (28 SBI per 10 SBBJ); NSE suspension notice 06-Mar-2017 |
| MYSOREBANK | 2017-03-15 | 22/10 SBIN | +1.4% | 1 | SBI board 18-Aug-2016 swap ratio (22 SBI per 10 SBM); NSE suspension notice 06-Mar-2017 |
| SBT | 2017-03-15 | 22/10 SBIN | +0.9% | 1 | SBI board 18-Aug-2016 swap ratio (22 SBI per 10 SBT); NSE suspension notice 06-Mar-2017 |
| CAIRN | 2017-04-25 | 1 VEDL + ₹40 | -3.8% | 1 | NSE filing CAIRN record date 27-Apr-2017: 1 Vedanta + 4 x Rs 10 7.5% RPS (cash exit at par = Rs 40) per share |
| DISHMAN | 2017-05-29 | 1 DCAL | +16.9% | 115 | NSE filing DISHMAN 19-May-2017: 1 DCAL per 1 DPCL; acquirer listed 21-Sep-2017 |
| ABIRLANUVO | 2017-07-04 | 15/10 GRASIM | +2.3% | 1 | NSE filing ABIRLANUVO 20-Jun-2017: 15 Grasim per 10 ABNL |
| TUBEINVEST | 2017-08-23 | 1 TIFIN + 1 TIINDIA | +7.9% | 71 | Scheme of arrangement 2017, record date 28-Aug-2017: holding continues as TI Financial Holdings (new ISIN INE149A01033); same scheme: 1 Tube Investments of India (INE974X01010, new) per share |
| TECHNO | 2018-08-07 | 1 TECHNOE | +9.5% | 119 | NSE filing TECHNO 30-Jul-2018: 1 Simran Wind (now TECHNOE) per 1 share |
| DALMIABHA | 2018-11-28 | 2 DALBHARAT | -27.5% | 55 | NSE filing DALMIABHA record date 30-Nov-2018: 2 new Dalmia Bharat (ex-Odisha Cement) per 1; listed 22-Jan-2019 |
| CAPF | 2018-12-27 | 139/10 IDFCFIRSTB | +1.7% | 1 | NSE filing CAPF 19-Dec-2018: 139 IDFC Bank per 10 Capital First |
| DENABANK | 2019-03-07 | 110/1000 BANKBARODA | -1.2% | 1 | NSE filing DENABANK 02-Jan-2019: 110 BOB per 1000 Dena Bank |
| VIJAYABANK | 2019-03-07 | 402/1000 BANKBARODA | -0.8% | 1 | NSE filing VIJAYABANK 02-Jan-2019: 402 BOB per 1000 Vijaya Bank |
| BHARATFIN | 2019-07-02 | 639/1000 INDUSINDBK | +4.6% | 1 | NSE filing BHARATFIN record date: 639 IndusInd per 1000 BFIL |
| EXCELCROP | 2019-08-28 | 51/2 SUMICHEM | +60.2% | 152 | NSE filing EXCELCROP record date 31-Aug-2019: 51 SCIL per 2 Excel Crop Care; SCIL listed 27-Jan-2020 |
| MONSANTO | 2019-09-26 | 2/3 BAYERCROP | +0.3% | 1 | NSE filing MONSANTO scheme: 2 Bayer CropScience per 3 Monsanto India |
| GRUH | 2019-10-15 | 568/1000 BANDHANBNK | +4.3% | 1 | NSE filing GRUH scheme: 568 Bandhan per 1000 Gruh (traded ~10% below conversion value) |
| RUCHISOYA | 2019-11-13 | 1/100 RUCHI | -94.9% | 75 | Resolution plan capital reduction and consolidation, record date 16-Nov-2019: 100 old shares -> 1 (filing 18-Dec-2019; press reports); relisted 27-Jan-2020 |
| ALOKTEXT | 2020-01-30 | 1 ALOKINDS | +410.6% | 20 | NSE filing ALOKTEXT record date 03-Feb-2020: face value cut Rs 10 -> Re 1, no payout, share count unchanged; new ISIN listed 19-Feb-2020 |
| CORPBANK | 2020-03-19 | 330/1000 UNIONBANK | -1.8% | 1 | NSE filing gazette scheme 04-Mar-2020: 330 Union Bank per 1000 Corporation Bank |
| ALBK | 2020-03-19 | 115/1000 INDIANB | -20.7% | 1 | NSE filing ALBK 05/17-Mar-2020: 115 Indian Bank per 1000 Allahabad Bank (ALBK traded 34% above conversion value in the Mar-2020 crash) |
| ANDHRABANK | 2020-03-19 | 325/1000 UNIONBANK | +4.1% | 1 | NSE filing gazette scheme 04-Mar-2020: 325 Union Bank per 1000 Andhra Bank (was 324, corrected) |
| SYNDIBANK | 2020-03-19 | 158/1000 CANBK | +0.4% | 1 | NSE filing SYNDIBANK 05-Mar-2020: 158 Canara per 1000 Syndicate |
| ORIENTBANK | 2020-03-20 | 1150/1000 PNB | -6.3% | 3 | NSE filing ORIENTBANK 05-Mar-2020: 1150 PNB per 1000 OBC |
| UNITEDBNK | 2020-03-20 | 121/1000 PNB | -4.8% | 3 | NSE filing UNITEDBNK 05-Mar-2020: 121 PNB per 1000 United Bank |
| GSKCONS | 2020-04-15 | 4.39 HINDUNILVR | -0.4% | 1 | NSE filing GSKCONS record date: 4.39 HUL per 1 GSKCH |
| MAXINDIA | 2020-06-11 | 99/100 MAXHEALTH + 1/5 MAXIND | +83.2% | 78 | NSE filing MAXINDIA record date 15-Jun-2020: 99 Max Healthcare per 100; same scheme: 1 Advaita (new Max India) per 5 |
| TATASTLBSL | 2021-11-12 | 1/15 TATASTEEL | -2.7% | 3 | NSE filing TATASTLBSL: 1 Tata Steel per 15 TSBSL |
| GDL | 2022-01-04 | 4 GATEWAY | +3.2% | 77 | NSE filing GDL record date 06-Jan-2022; ratio 1:4 per ICICI Securities, price-checked (GDL Rs 280 vs GATEWAY ~Rs 70) |
| SUPPETRO | 2022-04-06 | 1 SPLPETRO + ₹6 | -7.6% | 48 | NSE filing SUPPETRO 31-Mar/04-Apr-2022: capital reduction, Rs 6 refund per share, share count unchanged; new ISIN listed 24-May-2022 |
| MINDTREE | 2022-11-22 | 73/100 LTIM | +0.8% | 1 | NSE filing MINDTREE: 73 LTI per 100 Mindtree |
| SHRIRAMCIT | 2022-11-28 | 1.55 SHRIRAMFIN | +2.5% | 1 | Shriram Transport investor presentation and record-date notice 30-Nov-2022: 1.55 STFC per SCUF |
| EQUITAS | 2023-02-02 | 231/100 EQUITASBNK | +3.0% | 1 | NSE filing EQUITAS scheme/record date 03-Feb-2023: 231 ESFB per 100 EHL |
| INOXLEISUR | 2023-02-16 | 3/10 PVRINOX | -2.4% | 1 | NSE filing INOXLEISUR scheme: 3 PVR per 10 Inox |
| JSLHISAR | 2023-03-08 | 195/100 JSL | +7.0% | 1 | Jindal Stainless statement (Dec-2020) and record date 09-Mar-2023: 195 JSL per 100 JSHL |
| HDFC | 2023-07-12 | 42/25 HDFCBANK | +1.2% | 1 | NSE filing HDFC: 42 HDFC Bank per 25 HDFC |
| RNAVAL | 2023-07-13 | 1/275 SWANDEF | -94.0% | 558 | NSE filing RNAVAL: shares extinguished, 1 new share per 275 held (capital reduction under NCLT plan); relisted 21-Jan-2025 |
| TATASTLLP | 2023-11-16 | 67/10 TATASTEEL | +1.1% | 1 | NSE filing TATASTLLP record date 17-Nov-2023: 67 Tata Steel per 10 TSLP |
| TATACOFFEE | 2024-01-12 | 3/10 TATACONSUM | +0.1% | 3 | NSE filing TATACOFFEE scheme: 1 TCPL per 22 (demerger) + 14 TCPL per 55 (amalgamation) = 0.3 |
| UJJIVAN | 2024-05-02 | 116/10 UJJIVANSFB | +7.2% | 1 | NSE filing UJJIVAN scheme: 116 USFB per 10 UFSL |
| TATAMTRDVR | 2024-08-29 | 7/10 TATAMOTORS | +1.2% | 1 | Tata Motors record date 01-Sep-2024: 7 ordinary per 10 'A' ordinary (DVR) |
| TCNSBRANDS | 2024-09-02 | 11/6 ABFRL | -0.9% | 1 | NSE filing TCNSBRANDS: 11 ABFRL per 6 TCNS |
| IDFC | 2024-10-09 | 155/100 IDFCFIRSTB | +5.0% | 1 | NSE filing IDFC: 155 IDFC First Bank per 100 IDFC |
| TV18BRDCST | 2024-10-15 | 100/172 NETWORK18 | +10.4% | 1 | NSE filing TV18BRDCST scheme: 100 Network18 per 172 TV18 |
| ISEC | 2025-03-21 | 67/100 ICICIBANK | +1.5% | 3 | ICICI Securities record date 24-Mar-2025: 67 ICICI Bank per 100 ISEC |
| PEL | 2025-09-22 | 1 PIRAMALFIN | +17.7% | 46 | NSE filing PEL: 1 Piramal Finance per 1 PEL; acquirer listed 07-Nov-2025 |
| DHANI | 2025-10-27 | 294/100 YAARI | +11.0% | 1 | NSE filing DHANI record date: 294 Yaari per 100 fully paid DSL |
| ACLGATI | 2025-11-11 | 63/10 ALLCARGO | +13.3% | 1 | NSE filing ACLGATI: 63 Allcargo Logistics per 10 Allcargo Gati; ratio is on post-demerger Allcargo shares (Allcargo ex-demerger 12-Nov-2025, factor 0.313: 6.3 x Rs 11 = Rs 69 vs ACLGATI Rs 66) |
| GSPL | 2026-05-11 | 10/13 GUJGASLTD | +5.6% | 1 | NSE filing GSPL record date 12-May-2026: 10 Gujarat Gas per 13 GSPL |
| JBCHEPHARM | 2026-07-16 | 51/100 TORNTPHARM | +0.8% | 1 | NSE filing JBCHEPHARM record date 17-Jul-2026: 51 Torrent Pharma per 100 JB Chemicals |

Notable: RUCHISOYA (100 → 1) and RNAVAL (275 → 1) were insolvency capital reductions; holders lost about 95%. ALOKTEXT relisted 1:1 at five times its last price. ALBK traded 34% above its conversion value in the March 2020 crash. ACLGATI's ratio applies to Allcargo shares after Allcargo's same-day demerger; the engine applies conversions after the acquirer's own events that day, so converted shares are not entitled to them.

## Delisted and suspended

| Security | Class | Value (₹/share) | Source | Note |
|---|---|---|---|---|
| DHFL | delisted | 0 | NSE notice 11-Jun-2021: resolution plan provides for delisting with no value attributable to equity shares | delisted 29-Sep-2021 |
| CASTEXTECH | delisted | 0 | NSE notice 20-Jul-2021: resolution plan provides delisting without any exit price | delisted Dec-2021 |
| SREINFRA | delisted | 0 | NSE notice 14-Aug-2023: existing shares stand cancelled under the resolution plan | delisted 14-Dec-2023 |
| SPTL | delisted | 0 | Filing 07-May-2025: record date for extinguishment and cancellation of the entire existing equity | delisted 21-May-2025 (liquidation sale) |
| SINTEX | delisted | 0 | Filing 21-Mar-2022: resolution plan reduces existing share capital to zero | delisted 10-Mar-2023 |
| RELCAPITAL | delisted | 0 | Filing 01-Oct-2024: entire share capital cancelled and extinguished for NIL consideration | delisted 13-Feb-2025 |
| LAKSHVILAS | delisted | 0 | NSE notices 25-Nov and 16-Dec-2020; RBI/Govt scheme: paid-up capital written off on amalgamation with DBS Bank India | delisted 18-Dec-2020 |
| KSK | delisted | 0 | Filing 22-Jun-2021: share capital reduced and shares delisted under NCLT order 28-Apr-2021 (sale in liquidation) | equity holders received nothing |
| KWALITY | delisted | 0 | NSE notice 27-Feb-2022: delisted under NCLT order 21-Dec-2021 after sale in liquidation | liquidation: equity ranks last (IBC s.53) |
| JPINFRATEC | delisted | 0 | Resolution plan (NCLT 07-Mar-2023): aggregate exit of Rs 14 lakh to all public shareholders, under Rs 0.01 per share | delisted 21-Feb-2025; rounded to 0 |
| JMTAUTOLTD | delisted | 0 | Filing Aug-2023: all shares extinguished by capital reduction without payment of any price | delisted 18-Dec-2023 |
| ROLTA | delisted | 0 | Filing 31-Dec-2025: resolution plan delists and extinguishes the equity shares; no payment to shareholders stated | delisted 19-Jan-2026; assumed nil |
| JPASSOCIAT | delisted | 0 | Filing 17-Mar-2026: shares cancelled and extinguished for zero consideration; exit price NIL | delisted 18-Jun-2026 |
| AMTEKAUTO | delisted | 0.10 | Filing (resolution plan): Rs 2.5 crore in aggregate to non-promoter shareholders, about Rs 0.10-0.13 per share | delisted 27-Sep-2021; per-share figure approximate |
| JETAIRWAYS | delisted | 0 | Supreme Court liquidation order 07-Nov-2024 (filing same day); equity ranks last in liquidation (IBC s.53) | not yet formally delisted; assumed nil |
| NITINFIRE | delisted | 0 | Filing 01-Aug-2024: announcement by the company's liquidator; equity ranks last in liquidation (IBC s.53) | assumed nil |
| PUNJLLOYD | delisted | 0 | Filings 2026: company in liquidation (IBBI Liquidation Process Regulations); equity ranks last (IBC s.53) | assumed nil |
| COX&KINGS | delisted | policy | NSE notice 22-Feb-2023: delisted (company in liquidation); payout not stated | delisting policy applies |
| ESSDEE | delisted | policy | NSE notices 31-Aug-2020 and 05-Aug-2021: compulsory delisting | delisting policy applies |
| MANPASAND | delisted | policy | NSE notice 04-Aug-2025: compulsory delisting w.e.f. 05-Aug-2025 | delisting policy applies |
| GLOBOFFS | delisted | policy | NSE notice 01-Mar-2021: voluntary delisting under SEBI Delisting Regulations 2009 (exit price not found) | delisting policy applies |
| SPICEJET | delisted | policy | NSE trading stopped after 28-Apr-2023 (permitted-to-trade category; no NSE filings); the stock continued on BSE | delisting policy applies; last_close is the realistic value (sellable on BSE) |
| VIDEOIND | suspended | — | NSE notice 15-Jun-2021: suspended; NCLT plan approval set aside by NCLAT (filing 06-Jan-2022); CIRP ongoing | no final outcome by data end |
| JBFIND | suspended | — | Filings 2024-2026: CIRP; resolution plan rejected 18-Mar-2026 | no final outcome by data end |
| BRFL | suspended | — | Filings to Aug-2024: CIRP | no final outcome by data end |
| FRETAIL | suspended | — | Filings to Jul-2025: CIRP | no final outcome by data end |
| FCONSUMER | suspended | — | Filings Oct-2026: CIRP ongoing (meeting of creditors) | no final outcome by data end |
| EROSMEDIA | suspended | — | No NSE trades after 30-Jun-2025 (BZ series); company filings continue to Oct-2026 | no suspension notice found |
| BALLARPUR | suspended | — | NSE notice 21-Jun-2023: suspended for capital reduction; public holding written down to 41.25 lakh new shares, not relisted on NSE | value of the written-down shares unknown |

The four members on NSE's delisted list (LITL, ABGSHIP, SHILPI: compulsory or liquidation; HEXAWARE: voluntary) use the delisting policy.

Assumptions, stated: liquidation (JETAIRWAYS, NITINFIRE, PUNJLLOYD) and ROLTA are valued at ₹0 because equity ranks last under IBC s.53 and no payment is stated; AMTEKAUTO's ₹0.10 is an aggregate payout divided by an approximate public share count; SPICEJET left NSE but kept trading on BSE, so `last_close` is its realistic exit.
