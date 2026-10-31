# Pre-registration: daily closing-high breakout, point-in-time test

Written and committed **before** any point-in-time result was computed. Every parameter below is
also in `trader-scanner/breakout_pit.ini`; the evaluation tool refuses to run if the two disagree
with the frozen values in its own code. Nothing here may be changed after results are seen; any
deviation is reported as a deviation.

## 1. Hypothesis

H1: on Binance USDT markets, a daily close above the highest close of the prior 20 daily closes
(`breakout_close_high`, lookback 20, as implemented in `trader-scanner/crypto_setups.py` at
commit `76d9e7f`) is followed over the next five days by a **positive market-adjusted return net
of costs**, in a universe selected only with information available at the signal date.

H0: the market-adjusted net return is zero or negative.

The hypothesis came from a 16-coin, survivor-selected sample, signals 2022-09-24 to 2026-10-25
(`reports/BINANCE_SCANNER_VALIDATION.md` section 4). That period is therefore contaminated.

## 2. Frozen rule and trade (not tuned, not to be tuned)

| item | value |
| --- | --- |
| rule | `crypto_setups.bar_setups(window, ["breakout_close_high"], {"lookback": 20})` |
| window | the 250 closed daily candles ending at the signal day t (the scanner's `[timeframes] bars = 250`) |
| signal | known at the close of day t (00:00 UTC of t+1) |
| entry | open of day t+1 (the next candle's open) |
| exit | close of day t+5 (five daily candles held: t+1 .. t+5) |
| overlap | after a signal, the same symbol is not entered again until t+5 has closed (no overlapping trades per symbol) |
| return (Spot) | `exit * (1 - c) / (entry * (1 + c)) - 1`, c = 0.0015 per side (0.10 % taker + 5 bps slippage) |
| return (Futures) | same with c = 0.0010 (0.05 % taker + 5 bps), minus the sum of funding rates with funding time in (entry open, exit close] (a long pays a positive rate) |

## 3. Point-in-time universe and eligibility at signal day t

Universe: every symbol quoted in USDT that ever traded on Binance Spot (primary) or USD-M
Futures (secondary), taken as the union of (a) the current `exchangeInfo` including delisted
entries (Spot `BREAK`, Futures `SETTLING`/`PENDING_TRADING`) and (b) the public data archive
listing at data.binance.vision. Today's status is used **only** to find symbols, never to decide
eligibility.

A (symbol, t) is eligible only if, using candles with open time <= t:

1. there is a closed candle for day t;
2. the scanner's quality gate passes on the 250-candle window: at least 250 closed candles, at
   most 2 missing candles, at most 5 % zero-volume candles (`crypto_scan.quality_gate`, as
   committed);
3. liquidity, from the day-t candle (the 24 h that just closed, which is what the live 24 h
   ticker would show at 00:00 UTC): quote volume >= 2,000,000 USDT and trades >= 2,000;
4. the base asset is not on the scanner's stablecoin/fiat exclusion list;
5. Futures only: the symbol is not a delivery contract (no `_` in the symbol) and its
   `underlyingType` is not an equity/commodity/index type where known (a static property).

The live scanner's spread and price-age filters cannot be reconstructed historically and are
not applied (a known deviation from the live scan).

## 4. Benchmark and the primary statistic

Benchmark for signal day t: the mean net return of **all eligible symbols** of day t, entered and
exited with the same rule and costs (and funding on Futures).
Market-adjusted excess of an event = its net return minus the benchmark of its signal day.

Primary statistic: the event-weighted mean market-adjusted excess.
Interval: 95 % moving-block bootstrap over **calendar days** (block 20 days, 5,000 replicates,
seed 7); every event is attached to its signal day, so events of the same day are resampled
together (cross-sectional dependence) and overlapping holding windows of nearby days share blocks.
Secondary: Newey-West t statistic (lag 5) of the per-day mean excess series.

## 5. Periods

| period | signal days | status |
| --- | --- | --- |
| **holdout (primary)** | Spot 2018-01-01 to 2022-09-23; Futures 2020-01-01 to 2022-09-23 | never examined with this rule |
| development | 2022-09-24 to 2026-10-25 | contaminated (the hypothesis came from it); used to check the pipeline and to measure survivorship, not for the verdict |

The holdout lies before the development period because it is the only data that was not seen.
The pipeline is frozen (committed) after the development run and before the single holdout run.

## 6. Verdict rules (Spot holdout decides; Futures is reported as a secondary check)

**Statistically supported** if all hold:

- S1 the 95 % interval of the primary statistic lies above 0;
- S2 excluding the 16 coins of the original sample, the mean excess is > 0 and its 90 % interval lies above 0;
- S3 with delisting-affected trades marked at a total loss (-100 %), in events and benchmark, the mean excess is > 0;
- S4 without the top 1 % of events by excess, the mean excess is > 0;
- S5 no single asset contributes more than 25 % of the summed excess.

**Promising, not supported**: primary mean > 0 but one or more of S1-S5 fail.
**Not supported**: primary mean <= 0.

## 7. Sensitivities (reported, never used to rescue a verdict)

Delisting exit at the last close (base), at -30 %, at -100 %; dropping trades with a missing entry
candle (base) vs counting them; costs doubled; no per-symbol overlap rule; leveraged tokens
(UP/DOWN/BULL/BEAR) excluded; stable-priced assets excluded by price behaviour; a BTC-adjusted
benchmark; per-year and per-asset results; the 16 original coins alone.

## 8. Known limits fixed in advance

- A market removed from both `exchangeInfo` and the archive cannot be seen; the count of such
  markets is estimated against a list of known historical delistings and reported.
- Historical spread, order-book depth and exact delisting announcement dates are not available.
- Futures funding is taken from the public funding-rate history.
