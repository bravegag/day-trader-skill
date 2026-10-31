# Daily closing-high breakout: point-in-time verdict

Branch `feat/binance-market-scanner`, 2026-10-31. Pre-registration: `BREAKOUT_PREREGISTRATION.md`
(commit `8b472a7`, made before any point-in-time number existed). Tool: `trader-scanner/crypto_pit.py`
with `trader-scanner/breakout_pit.ini`. Raw outputs: `reports/data/pit-*.json`.

## 1. Verdict

**Not supported.** The +1.2-1.3 % five-day excess found on 16 hand-picked coins was a
survivorship effect, not a breakout effect.

- On the untouched **holdout** (Spot signals 2018-01-01 to 2022-09-23, 326 assets, 5,933
  trades), the mean market-adjusted excess is **+0.33 %**, 95 % interval **-0.24 % to +1.07 %**
  (Newey-West t 1.53). The pre-registered rule labels this "promising, not supported": the mean
  is above zero but S1, S2, S4 and S5 fail.
- The positive mean is a **right tail, not a typical outcome**. The median breakout trailed the
  market by **-2.6 %**, only **37.5 %** of breakouts beat it, and **without the top 1 % of
  trades** (60 of 5,933) the excess is **-1.12 %** (95 % interval -1.59 % to -0.62 %). One asset
  (CHZ) carries 30 % of the summed excess.
- On the **development** period (2022-09-24 to 2026-10-25, 548 assets, 14,823 trades: the period
  the hypothesis came from, re-run point-in-time), the excess is **negative and significant:
  -0.33 %** (95 % -0.60 % to -0.07 %, NW t -2.77). Every year from 2022 to 2026 is negative.
- **USD-M Futures** (secondary, funding included) agrees: holdout +0.19 % (95 % -0.36 % to
  +0.83 %), development +0.04 % (-0.20 % to +0.28 %), negative without the top 1 % or without the
  original 16, and the holdout's small positive sum is 96.5 % one asset (DOGE).
- Restricted to the **original 16 coins**, the same pipeline reproduces the original finding
  (+0.85 % against their own benchmark, +2.02 % against the point-in-time market, development
  period). The effect lives in which coins were chosen, not in the rule.

So: a statistically supported result would have needed S1-S5 to hold on the holdout; S1, S2, S4
and S5 fail (only S3, the delisting stress test, holds), and the contaminated period reverses sign
once survivorship is removed. The breakout stays in the scanner as a description of a chart, with no claim of edge.

## 2. Method (as pre-registered, plus one documented deviation)

**Rule, frozen.** `crypto_setups.bar_setups(window, ["breakout_close_high"], {"lookback": 20})`,
the committed scanner code, on the 250 closed daily candles ending at signal day t. Long at the
open of t+1, out at the close of t+5. One open trade per symbol at a time. Net return
`exit (1 - c) / (entry (1 + c)) - 1`, c = 0.15 % per side on Spot (0.10 % fee + 5 bps slippage).

**Point-in-time universe.** Every USDT symbol Binance Spot ever listed: the current
`exchangeInfo` **including its 256 delisted (`BREAK`) entries**, plus the public archive listing
(one more symbol, NBTUSDT, exists only there): **776 symbols, 257 of them delisted by the data
end, all with daily candles** (`reports/data/pit-spot-universe.json`). Today's status was used
only to find symbols. Of 14 known historical delistings checked by name (BCC, BCHABC, BCHSV, NPXS,
VEN, UST, ERD, MITH, BTCUP, ...), 13 are present; HSR is absent (likely never a USDT pair).

**Eligibility at t, from candles up to t only**: a closed day-t candle; the scanner's own
`quality_gate` on the 250-candle window (>= 250 candles, <= 2 missing, <= 5 % zero-volume);
day-t quote volume >= 2 M USDT and >= 2,000 trades (what the 24 h ticker shows at 00:00 UTC);
not on the scanner's stablecoin list. Listing date therefore enters through the 250-candle
history requirement, liquidity through the day-t candle, status through the existence of
trading candles; nothing from after t is read. The live spread and price-age filters cannot be
reconstructed and were not applied.

**Benchmark and statistic.** Excess = a trade's net return minus the mean net return of every
eligible market of the same signal day, traded the same way. Primary statistic: the event-weighted
mean excess; 95 % moving-block bootstrap over calendar days (20-day blocks, 5,000 replicates),
which keeps same-day trades together and lets overlapping holding windows share blocks;
Newey-West t (lag 5) on the per-day mean series.

**Deviation 1: price discontinuities (commit `13418a0`).** The as-registered holdout statistic
was **-1,148 %** (`reports/data/pit-spot-holdout-as-registered.json`), which is not a market
result. The benchmark was dominated by unadjusted leveraged-token share consolidations, e.g.
ETHDOWNUSDT "+128,000 %" in five days (2021). On a 24/7 market a candle opens at the previous
close. Of 859,263 adjacent Spot candle pairs, 18 open more than 2x away from the previous close,
the count is identical at 3x, and all 18 are such consolidations (about x160,000). Rule added:
a symbol-day whose window or holding period crosses an open more than 2x from the previous close
(adjacent or across missing days) is dropped from events and benchmark. It removed 1,399
holdout symbol-days and 90 signals (1 symbol-day in development). The threshold came from the gap
distribution, not from results. Two earlier fixes (commit `5657a77`, before the holdout run)
restored pre-registered definitions: every variant's benchmark is the full eligible market.

**Order of work.** Pre-registration committed -> tool committed -> development run -> pipeline
fixes committed ("freeze") -> one holdout run -> deviation 1 committed -> holdout and development
re-run. The holdout was opened twice: once as registered (invalid, kept), once after deviation 1.

## 3. Results, Spot

### 3.1 Primary and pre-registered criteria

| | holdout 2018-01-01 .. 2022-09-23 | development 2022-09-24 .. 2026-10-25 |
| --- | --- | --- |
| eligible symbol-days / symbols | 117,097 / 348 | 234,738 / 564 |
| signals / trades (one open per symbol) | 13,125 / 5,933 | 31,281 / 14,823 |
| mean net return per trade | +2.19 % | -0.11 % |
| **mean excess (primary)** | **+0.33 %** [-0.24, +1.07] | **-0.33 %** [-0.60, -0.07] |
| median excess | -2.61 % | -2.07 % |
| share of trades beating the benchmark | 37.5 % | 38.5 % |
| Newey-West t | 1.53 | -2.77 |
| S1: 95 % interval above 0 | **fail** | fail |
| S2: excluding the original 16, mean > 0 and 90 % interval above 0 | **fail** (+0.19 %, [-0.32, +0.83]) | fail (-0.45 %) |
| S3: delisting-affected trades at -100 %, mean > 0 | pass (+0.33 %; 1 trade affected) | fail |
| S4: without the top 1 %, mean > 0 | **fail** (-1.12 %, [-1.59, -0.62]) | fail (-1.30 %) |
| S5: no asset > 25 % of the summed excess | **fail** (CHZ 29.6 %) | n/a (sum negative) |
| verdict rule | promising, not supported | not supported |

### 3.2 Sensitivities (holdout; none changes the conclusion)

| variant | n | mean excess | 95 % interval |
| --- | --- | --- | --- |
| delisting exit at -30 % | 5,933 | +0.33 % | -0.24 .. +1.07 |
| costs doubled | 5,933 | +0.33 % | -0.24 .. +1.07 |
| late entry counted (missing t+1 candle) | 5,933 | +0.33 % | identical: no missing entry candles |
| leveraged tokens excluded | 5,684 | +0.37 % | -0.20 .. +1.12 |
| stable-priced assets excluded | 5,901 | +0.30 % | -0.25 .. +1.04 |
| overlapping trades allowed | 13,035 | +0.30 % | -0.32 .. +1.06 |
| benchmark = BTC instead of the market | 5,933 | +2.22 % | +0.49 .. +4.09 |
| original 16 only, market benchmark | 432 | +2.16 % | +0.19 .. +5.03 |
| original 16 only, their own benchmark | 432 | +1.00 % | -0.09 .. +2.22 |

The BTC-relative number is not a breakout effect: breakouts are mostly altcoins, and 2020-2021
was an altcoin market; against the equal-weighted market (the pre-registered benchmark) it
vanishes. Costs barely matter because both sides of the excess pay them.

### 3.3 Time and concentration

| year | trades | mean excess | median excess | 90 % interval |
| --- | --- | --- | --- | --- |
| 2018 | 29 | +0.34 % | -0.65 % | -0.56 .. +1.92 |
| 2019 | 235 | +0.48 % | -0.91 % | -0.13 .. +1.27 |
| 2020 | 919 | +1.71 % | -1.07 % | +1.01 .. +2.22 |
| 2021 | 2,792 | +0.46 % | -3.76 % | -0.36 .. +1.63 |
| 2022 (holdout part) | 1,958 | -0.52 % | -2.37 % | -1.03 .. +0.09 |
| 2022 (development part) | 653 | -1.95 % | -2.54 % | -2.77 .. -1.09 |
| 2023 | 4,404 | -0.35 % | -2.22 % | -0.83 .. +0.05 |
| 2024 | 4,923 | -0.21 % | -2.12 % | -0.50 .. +0.05 |
| 2025 | 2,533 | -0.32 % | -1.81 % | -0.84 .. +0.45 |
| 2026 | 2,310 | -0.08 % | -1.83 % | -0.70 .. +0.44 |

Only 2020 is positive with an interval above zero. Holdout top contributors: CHZ 29.6 %, FTM
26.7 %, DOGE 21.7 %, WRX 19.1 %, CHR 18.6 % of the summed excess (shares of a small positive
sum, so they add to more than 100 %). Development top contributors: OM, PEPE, ZEC, ONE, QNT.

## 4. Results, USD-M Futures (secondary)

Same rule, eligibility and statistics; costs 0.10 % per side (0.05 % fee + 5 bps slippage) plus
the funding a long pays between entry and exit (sum of funding rates with funding time in
(entry open, exit close]). Universe: 928 USDT perpetual symbols (current `exchangeInfo` with its
136 `SETTLING` contracts, plus 31 symbols that exist only in the public archive, e.g. old LUNA,
EOS, MATIC, BTCST); delivery contracts and equity, commodity, index and FX perpetuals excluded.
Holdout starts 2020-01-01 (USD-M launched in September 2019; 250 candles of history are needed).

| | holdout 2020-01-01 .. 2022-09-23 | development 2022-09-24 .. 2026-10-25 |
| --- | --- | --- |
| eligible symbol-days / symbols | 54,260 / 129 | 308,316 / 604 |
| trades (one open per symbol) | 2,096 | 13,496 |
| mean net return per trade (after funding) | +1.17 % | +0.32 % |
| **mean excess** | **+0.19 %** [-0.36, +0.83] | **+0.04 %** [-0.20, +0.28] |
| median excess | -2.19 % | -1.79 % |
| share beating the benchmark | 37.9 % | 39.1 % |
| Newey-West t | 1.09 | 0.58 |
| excluding the original 16 | -0.19 % [-0.69, +0.30] | -0.06 % [-0.31, +0.18] |
| without the top 1 % of trades | -0.79 % [-1.29, -0.35] | -1.01 % [-1.29, -0.74] |
| largest single-asset share of the summed excess | DOGE 96.5 % | BTR 69.6 % |
| original 16 only, market benchmark | +3.44 % [+0.76, +7.67] | +1.86 % [+0.85, +3.11] |
| verdict rule | promising, not supported (S1, S2, S4, S5 fail) | promising, not supported |

Futures confirms Spot: a mean indistinguishable from zero, a negative median, a negative mean once
the top 1 % of trades or the original 16 coins are removed, and the positive mean concentrated in
one asset. The original 16 again look strong against the market, which is the survivorship effect.

Two Futures-specific data facts, both found before the Futures results were computed or checked
against them:

- **Funding history bug, fixed before use.** `day-trader-skill`, `trader-scanner/crypto_pit.py`,
  function `funding_history()` (introduced in `f2813f4`, fixed in `f4ef357`): the first request
  used `startTime=0`, which `/fapi/v1/fundingRate` treats as absent, answering only the latest 500
  records; 726 of 928 files covered only 2026-05 onwards. The fix pages forward from 2019-09-01
  (BTCUSDT now from 2019-09-10, 7,823 records; no file capped at 500; every market that can be
  eligible has its history). The live scanner (`crypto_scan.py`, `crypto_providers.py`) never
  fetches funding history and was not affected.
- **Settled perpetuals keep printing flat, zero-volume candles to the present** (all 136
  `SETTLING` contracts). They never pass the liquidity floor, so they are never eligible after
  settlement, but a trade opened before settlement exits at the flat settlement price and is not
  counted as delisting-affected: 0 holdout trades and 16 of 13,496 development trades (0.12 %).
  Marking those 16 at -100 % would move the development mean by about -0.12 points.

## 5. Audit of the earlier walk-forward (`trader-scanner/crypto_eval.py`)

| issue | evidence | effect |
| --- | --- | --- |
| **Selection bias (decisive)** | universe = today's allowlist, `status == "TRADING"` only (`crypto_eval.py:167-170`) | survivors only; the point-in-time universe reverses the sign (section 3) |
| benchmark built from the same survivors | cross-sectional mean of the 15 allowlisted symbols (`summarize`) | measures breakouts against other winners, not the market |
| no liquidity filter | `walk()` scans every bar after warm-up | tested a population the live scanner does not scan |
| overlap rule mismatch | docstring "ignored for H bars" (`:12`) vs code `hmax`, the longest horizon (`:66, :80`) | H = 5 signals were thinned with a 10-bar rule; fewer, not biased, events |
| interval vs estimand mismatch | point estimate event-weighted (`:140`), bootstrap over per-date means of event dates only (`:131-132`) | the interval was for a different statistic; blocks spanned irregular calendar time |
| folds by event count | `excess[f*m//4:(f+1)*m//4]` (`:136`) | "4 of 4 folds" were quarters of events, not of time |
| return arithmetic | `exit/entry - 1 - 2c` (`:72`) instead of `exit(1-c)/(entry(1+c)) - 1` | below 0.01 % per trade; immaterial |
| delisting handling | a trade whose exit falls after the data end is dropped (`t + H < n`, `:72`) | irrelevant there (no delisted symbols), would bias a real universe |
| temporal leakage | window = candles t-W+1..t, entry at t+1 open (`:69-71`) | **none found** |

Reproduction: the point-in-time tool restricted to the 16 coins with their own benchmark gives
+0.85 % (development), the earlier tool +1.33 % on 15 of them; the difference is the liquidity
filter, the overlap rule and the window. The sign and the conclusion change only with the universe.

Note on commit identifiers: the branch history was rewritten on 2026-10-31 to remove unrelated
content, keeping each commit's date and order. The pre-registration text is unchanged and still
names `76d9e7f` as the frozen scanner commit; that commit is now `ddb8b05`, and
`trader-scanner/crypto_setups.py` is byte-identical in both (git blob `fcd4b9c`).

## 6. Limitations

- Markets removed from both `exchangeInfo` and the archive cannot be seen (estimated small: 13 of
  14 named delistings found).
- No historical spread, depth or delisting-announcement dates; delisting was stress-tested at -30 %
  and -100 %, with only 1 (holdout) and 13 (development) trades affected.
- The holdout precedes the development period: it is the only unexamined data, so this is an
  earlier-period test, not a forward one.
- The holdout was viewed twice (as registered, then after deviation 1); the deviation is a data
  validity rule documented with its evidence.
- Equal-weighted benchmark; a volume-weighted one was not pre-registered and was not run.
- Futures: settled contracts exit at the settlement price (section 4); funding is the published
  rate times the entry notional, ignoring price drift within the holding period.

## 7. Reproduce

```bash
cd <scratch dir>   # candles are cached under [data] dir (about 0.14 GB for Spot plus the Futures candles and funding, not committed)
python3 trader-scanner/crypto_pit.py fetch --market spot
python3 trader-scanner/crypto_pit.py run --market spot --period development
python3 trader-scanner/crypto_pit.py run --market spot --period holdout
python3 trader-scanner/crypto_pit.py fetch --market usdm      # candles, then funding (rate-limited, ~40 min)
python3 trader-scanner/crypto_pit.py run --market usdm --period development
python3 trader-scanner/crypto_pit.py run --market usdm --period holdout
```

Candles are final once closed, so a re-fetch gives the same history up to `data_end`, except
markets Binance removes entirely in the meantime.
