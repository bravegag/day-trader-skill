# Binance scanner validation

Branch `feat/binance-market-scanner`, 2026-10-31. Everything below was run, not assumed. Commands
are given so each result can be reproduced.

## 1. Test suite

| run | result |
| --- | --- |
| baseline, untouched branch (`3d0f4ef`) | **315 passed** |
| baseline, second run in a row | 1 failed (`test_root_matches_the_reel`: pytest's `.pytest_cache/` at the root). Pre-existing; fixed on this branch |
| this branch, `python3 -m pytest -q tests` | **365 passed** (315 existing + 50 new), twice in a row |

No existing test was weakened. The one existing test edited (`tests/test_repo.py`) now admits a
`reports/` folder and ignores `.pytest_cache`; the other 314 existing tests, including every
Yahoo scanner test, run unmodified and pass. That is the evidence that the US-equity behaviour is
unchanged: `scan.py`'s `universe`, `daily` and `orb` functions were not edited, and the crypto
import is guarded so that a broken crypto file cannot stop them.

The new tests (`tests/test_crypto_scanner.py`) are offline: they read recorded fixtures or fake
transports. Coverage against the brief:

| brief item | tests |
| --- | --- |
| Yahoo behaviour unchanged | the 36 existing scanner tests; `Setups.test_ported_rules_agree_with_the_equity_scan` (300 random series, the five ported rules equal the equity `find_setups`) |
| Spot and Futures discovery | `Universe.test_spot_fixture_finds_deep_missing_and_wal_illiquid`, `test_futures_fixture_lists_deep`, `test_resolution_rules` (multipliers, TRADIFI perps, quarterlies, SETTLING, stablecoins), `test_top_volume_mode`, `test_symbols_option` |
| Missing or delisted symbols | `test_resolution_rules` (not listed), `Contract.test_failed_fetches_make_the_scan_partial` (-1121 on klines) |
| Rate limiting and HTTP failures | `Network.*`: 429 with Retry-After, 418 stops at once, 5xx exponential backoff and host switch, final 4xx, exhausted network retries, minute weight budget, server weight header, kline weight table; `Contract.test_a_ban_stops_the_scan_and_says_so` |
| Missing, duplicated, unordered candles | `Candles.test_duplicates_unordered_incomplete_and_future_are_dropped_and_counted`, `test_invalid_rows_off_grid_and_gaps` |
| Incomplete current candle | same, plus `test_a_candle_is_closed_exactly_at_its_end` |
| Futures/Spot symbol collisions | `Universe.test_spot_and_futures_same_symbol_do_not_collide` (ids and the per-market exchangeInfo cache) |
| Insufficient history, stale data | `Quality.test_insufficient_history`, `test_stale_when_the_newest_closed_candle_is_missing` |
| Zero-volume and illiquid markets | `Quality.test_gaps_and_zero_volume`, `Universe.test_liquidity_filters` |
| Missing indicators | `Quality.test_indicators_missing_when_short` |
| Stable output contract | `Contract.test_output_contract` (keys, UTC `Z` times, null probability, ranks, strict JSON, readable by the equity watchlist reader) |
| Timestamps and UTC | `Candles.test_expected_last_open_is_utc_grid`, `Contract.test_now_without_offset_is_utc` |
| No look-ahead | `Contract.test_no_look_ahead_in_a_replay`: a replay seven hours back over the full fixture equals a scan of a fixture physically cut at that hour |
| Offline reproducibility | `Contract.test_spot_example_is_reproducible`, `test_ranking_is_deterministic_under_input_order` |
| Safety | `Network.test_no_signed_or_order_endpoint_anywhere` |

## 2. Live smoke test (bounded, public endpoints, no key)

`python3 trader-scanner/scan.py crypto --config trader-scanner/binance.example.ini --market <m>`,
allowlist of 16, timeframes 1d (250 bars) and 1h (500 bars), 2026-10-31 13:38 UTC:

| market | status | requests | weight used | of limit | retries / 429 | time | requested / eligible / scanned / matched |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Spot | COMPLETE | 31 | 157 | 6000/min (2.6 %) | 0 / 0 | 20.7 s (cold ~18 MB exchangeInfo) | 16 / 14 / 14 / 8 |
| USD-M | COMPLETE | 44 | 171 | 2400/min (7.1 %) | 0 / 0 | 6.8 s | 16 / 15 / 15 / 9 |

Freshness on that run: the newest 1h candle was the one closing 13:00 UTC (expected), the last
price was 0.7 s (Spot) and 1.6 s (Futures) old, `incomplete_dropped = 1` on every timeframe (the
open candle was returned by Binance and correctly not used). Live connectivity is verified.

## 3. Reproducible examples (offline, from recorded real answers)

Recorded live on 2026-10-31 at 13:11:25 UTC, replayed offline; the replay equals the live run
field for field (timing and network counters aside).

### 3.1 Binance Spot

```
$ python3 trader-scanner/scan.py crypto --config trader-scanner/binance.example.ini \
      --offline tests/fixtures/trader-scanner/binance-spot-2026-10-31 --no-write
Binance Spot scan, 2026-10-31 13:11 UTC, closed candles only (1d, 1h), USDT quote. Config bca020414b69f8ac.
Requested 16, eligible 14, scanned 14, 8 matched a rule. Status: COMPLETE.
  1 ZECUSDT           1283.80  vol24h  114.4M  ALIGNED 1d[momentum_accel rsi 52] 1h[breakout_close_high rsi 60]
  2 ONDOUSDT           0.4112  vol24h   20.9M  ALIGNED 1d[momentum_accel rsi 45] 1h[reclaim_ma rsi 57]
  3 ETHUSDT           2247.55  vol24h  476.1M  1d[rvol_expansion(down) rsi 30] 1h[breakout_close_high rsi 54]
  4 SOLUSDT           95.8100  vol24h  157.0M  1d[oversold_bounce rsi 31] 1h[- rsi 50]
  5 BNBUSDT            705.91  vol24h   89.3M  1d[rvol_expansion rsi 39] 1h[- rsi 51]
  6 INJUSDT            7.1420  vol24h    7.2M  1d[- rsi 42] 1h[reclaim_ma rsi 49]
  7 PENGUUSDT        0.007296  vol24h    3.9M  1d[momentum_accel rsi 44] 1h[- rsi 48]
  8 PAXGUSDT          3898.06  vol24h   12.9M  1d[rsi_oversold,rvol_expansion(down) rsi 26] 1h[rsi_oversold rsi 29]
Excluded 2: DEEP (not listed on binance spot as DEEPUSDT); WALUSDT (24h quote volume 282.1K under 2.0M).
```

### 3.2 Binance USD-M Futures

```
$ python3 trader-scanner/scan.py crypto --config trader-scanner/binance.example.ini --market usdm \
      --offline tests/fixtures/trader-scanner/binance-usdm-2026-10-31 --no-write
Binance USD-M Futures scan, 2026-10-31 13:11 UTC, closed candles only (1d, 1h), USDT quote. Config 713d96eb85b6d2d5.
Requested 16, eligible 15, scanned 15, 9 matched a rule. Status: COMPLETE.
  1 HYPEUSDT          87.5500  vol24h  986.6M  ALIGNED 1d[breakout_close_high,rvol_expansion,momentum_accel rsi 64] 1h[rvol_expansion rsi 43] fund +0.0050%
  2 ZECUSDT           1284.42  vol24h    1.3B  ALIGNED 1d[momentum_accel rsi 52] 1h[breakout_close_high rsi 60] fund +0.0100%
  3 ONDOUSDT           0.4109  vol24h   75.9M  ALIGNED 1d[momentum_accel rsi 45] 1h[reclaim_ma rsi 57] fund +0.0031%
  ...
Excluded 1: DEEPUSDT (24h quote volume 741.6K under 2.0M).
```

Each candidate in the JSON carries mark, index, basis (bps), last funding rate, next funding time
and open interest (base and notional), labelled "context only: no leverage or execution advice".

## 4. Walk-forward check of the rules, net of costs

Command: `python3 trader-scanner/crypto_eval.py events --config trader-scanner/binance.example.ini
[--market usdm] --end 2026-10-31T00:00Z`. Full output: `reports/data/walkforward-*.json`.

Method: the scanner's own rules, on the same trailing window the live scan reads (250 daily or
500 hourly bars), at every closed bar of the allowlist (15 Spot and 16 Futures markets, up to
1,500 days of 1d and one year of 1h). Long entry at the next bar's open, exit at the close H bars
later. Cost per side 0.15 % on Spot (0.10 % taker + 5 bps slippage) and 0.10 % on Futures (0.05 %
taker + 5 bps; funding ignored). The same rule on the same symbol is ignored for H bars after a
signal. Excess is a signal's net return minus the cross-sectional mean net return of every
market entered on the same bar. Interval: 90 % moving-block bootstrap over entry dates. Folds:
the signals split into four chronological quarters. Nothing was fitted or tuned; the parameters
are the INI defaults.

### 4.1 Results

1h, both markets: **the high-frequency rules do not survive costs.** For the six rules with
1,000-3,800 signals each, the mean net return is negative at every horizon (-0.13 % to -0.47 % per
trade), like the unconditional baseline (-0.21 % to -0.35 %): costs of 0.2-0.3 % a round trip are
the size of a few hours' move. Their market-adjusted excess is within +/-0.1 %; the largest,
`rvol_expansion` at 24 h on Futures, is +0.08 % (interval +0.06 to +0.28 %), still a net loss per
trade. The exception is the rare `bull_flag` (169 signals on Spot, 206 on Futures): on Futures at
4 h its mean net is +0.24 % and its excess +0.46 % (interval +0.12 to +0.93 %, 3 of 4 folds), on
Spot +0.33 % excess (+0.01 to +0.93 %) but a net loss; at 12 h and 24 h the folds flip sign. With
that sample size and the comparisons below, it is noise until shown otherwise.

1d, selected rows (excess = market-adjusted, net of costs):

| market | rule | H (days) | n | mean net | median net | hit rate | mean excess | 90 % interval | folds > 0 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Spot | breakout_close_high | 5 | 529 | +2.17 % | +0.45 % | 0.53 | **+1.33 %** | +0.95 to +2.41 | 4 / 4 |
| USD-M | breakout_close_high | 5 | 501 | +2.18 % | +0.79 % | 0.56 | **+1.20 %** | +0.63 to +2.21 | 4 / 4 |
| USD-M | breakout_close_high | 10 | 501 | +3.50 % | +0.51 % | 0.51 | +1.20 % | +0.49 to +3.35 | 4 / 4 |
| Spot | rvol_expansion | 5 | 451 | +2.69 % | +0.46 % | 0.52 | +0.95 % | -0.05 to +1.60 | 4 / 4 |
| Spot | momentum_accel | 5 | 1154 | +1.24 % | -0.09 % | 0.50 | +0.36 % | +0.18 to +1.13 | 3 / 4 |
| Spot | bull_flag | 5 | 233 | +1.63 % | -0.21 % | 0.49 | +0.81 % | +0.03 to +2.14 | 2 / 4 |
| Spot | rsi_oversold | 10 | 129 | +2.59 % | +1.77 % | 0.61 | +0.49 % | -0.07 to +2.32 | 3 / 4 |
| Spot | reclaim_ma | 10 | 593 | +0.56 % | -0.83 % | 0.47 | **-0.59 %** | -1.46 to -0.29 | 1 / 4 |
| Spot | oversold_bounce | 1 | 130 | -0.95 % | -0.90 % | 0.39 | **-0.61 %** | -1.17 to -0.14 | 1 / 4 |
| Spot | baseline (all bars) | 5 | 16,041 | +0.45 % | | 0.47 | 0 by construction | | |

### 4.2 What this does and does not show

- The daily closing-high breakout is the only rule whose market-adjusted excess is positive with
  an interval clear of zero in every fold, on both markets. That is consistent with the
  time-series momentum documented for crypto, and it is a hypothesis worth testing properly.
- It is **not** a demonstrated edge, for four reasons:
  1. **Survivorship.** The 16 markets were chosen today, from coins that are listed, liquid and
     of interest now (HYPE, ZEC). A breakout rule run on survivors is biased upward: the coins
     whose breakouts failed and then delisted or faded are absent. The positive 1d baseline
     (+0.45 % per 5 days after costs) is itself a sign of a selected, winning sample.
  2. **Multiple comparisons.** 7 rules x 3 horizons x 2 timeframes x 2 markets = 84 rows, and
     Spot and Futures are nearly the same assets, so they are not two independent confirmations.
  3. **Skew.** Means far above medians: a few large runs carry the average. The hit rate is
     53-56 %.
  4. **Execution.** Next-open entry at the stated cost, no capacity limit, no funding on
     Futures.
- The two negative rows (`reclaim_ma` at 10 days, `oversold_bounce` at 1 day) say those rules,
  as written, have tended to precede *underperformance*. They stay in the scan as descriptions,
  and the SKILL.md says no rule has a demonstrated edge.
- Hence `estimated_probability` stays `null` and the ranking is a count of matched rules.

**Update (point-in-time test, `BREAKOUT_HYPOTHESIS_VERDICT.md`): not supported.** On every USDT
market Binance Spot ever listed, delisted ones included, the breakout's excess on the untouched
2018-2022 holdout is +0.33 % (95 % -0.24 % to +1.07 %, median -2.6 %) and on 2022-2026 it is
-0.33 % (significant). The result above was survivorship. The audit of this section's method is in
that report's section 5.

The original plan read: a point-in-time universe (all USDT markets
listed at each date, including later delistings, which the exchange's `BREAK` symbols and
Binance's public data archive make possible), a pre-registered rule and horizon, and an
untouched final period.

## 5. UTC session profile (the ORB question)

`python3 trader-scanner/crypto_eval.py profile --end 2026-10-31T00:00Z`, 15 Spot markets, 90 days of 1h bars:

| UTC hour | volume share | mean abs. hourly return |
| --- | --- | --- |
| 00 | 3.87 % | 0.52 % |
| 08 | 4.32 % | 0.51 % |
| 12 | 5.21 % | 0.59 % |
| 13 | 5.88 % | 0.55 % |
| **14** | **6.97 %** | **0.78 %** |
| 15 | 6.34 % | 0.73 % |
| 16 | 5.17 % | 0.55 % |
| 23 | 2.99 % | 0.45 % |
| uniform | 4.17 % | |

There is no opening at 00:00 UTC. The one recurring "open" is the US equity cash session
(13:30 UTC in US daylight time). `utc_orb` therefore stays experimental and off; the INI says
which anchor has a structural reason and that none has been tested for edge.

## 6. Known limitations

- Replays use point-in-time candles but today's universe and 24h liquidity (flagged in output).
- The walk-forward sample is survivorship-biased (section 4.2).
- Rules index by position; up to `max_missing_bars` gaps are tolerated inside a window.
- Funding intervals (4 h vs 8 h) are not normalized.
