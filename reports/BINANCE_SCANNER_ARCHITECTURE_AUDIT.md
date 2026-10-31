# Day Trader Skill: architecture audit and Binance scanner design

Fork: `bravegag/day-trader-skill`, branch `feat/binance-market-scanner`, based on `imp` at `3d0f4ef`.
Audit date: 2026-10-31. Method: the scanner was read line by line; the other ten skills were
audited by an independent read-only pass, and every High or Medium finding cited below was
re-checked against the code before inclusion. The README's claims were not taken as verified.

## 1. What the repository is

| fact | evidence |
| --- | --- |
| 11 skills, one folder each, `SKILL.md` plus one stdlib-only Python tool | `tests/test_repo.py` pins the folder list and the root layout |
| Python 3.8 compatible, no third-party packages | `tests/test_repo.py::test_every_tool_runs_on_python_3_8`; every import checked |
| Data: Yahoo Finance chart/spark (unofficial), Nasdaq Trader symbol files, SEC EDGAR, House Clerk, Stooq (dead per its own docstring) | `scan.py:83-85`, `alerts.py:196-197` |
| Orders: only `trader-broker`, through an Alpaca/IBKR MCP tool behind a client-side approval prompt | `trader-broker/SKILL.md:98-115` |
| Tests: 315, all offline (verified by blocking sockets), pinned real-data fixtures of one day (TSLA, 2026-10-19) | `tests/` |

Baseline test run on the untouched branch: **315 passed**. A second run failed one test
(`test_root_matches_the_reel`) because pytest's own `.pytest_cache/` appears at the root and the
layout test only ignored `.git*`. That is pre-existing; this branch fixes it (section 6).

## 2. trader-scanner (`scan.py`), the module this branch extends

Strengths:

- Closed-bar discipline is real. Today's daily bar is dropped until the session close
  (`scan.py:498-513`), an ORB bar counts only once `start + 5 min <= clock` (`scan.py:600`),
  the live off-grid tick is dropped (`scan.py:596`). No look-ahead found.
- The ORB stage 1 is a correct necessary condition (`scan.py:622-626`), and a capped stage 2 is
  reported as PARTIAL (`scan.py:1006-1008`) instead of being rounded up.
- Partial data is said as partial ("Fetched X of Y charts").
- Pure functions (`scan_daily`, `scan_orb`, `find_setups`) are separated from I/O, which is what
  made a surgical extension possible.

Findings:

| sev | finding | evidence |
| --- | --- | --- |
| Med | No per-symbol staleness check in `daily`: a symbol whose newest daily close is weeks old (halted, delisted but still answered) is scanned like the rest. `as_of` reports only the most common date. | `scan.py:564-573` |
| Med | NYSE holiday and early-close calendar hard-coded for 2026-2027; after 2027 holidays are treated as sessions. DST is a hand-rolled US rule (correct under the 2007 law), copied into nine of the eleven tools. | `scan.py:96-126`, `scan.py:150-158` |
| Med | Yahoo endpoints are unofficial and undocumented. Retry policy is one fixed 5 s wait after a 429, a host switch on 429/5xx, no jitter, no exponential backoff, sequential requests. Adequate for politeness, not a rate-limit model. | `scan.py:235-267` |
| Low | `daily` liquidity uses `regularMarketVolume`, which is the current session's volume during market hours (documented). | `scan.py:556-557`, `SKILL.md:80-84` |
| Low | Setups use closes only (spark has no highs/lows), so `breakout_20d` is a closing-high breakout, which fires more often than an intraday-high one (documented, with the KO example). | `SKILL.md:98-100` |
| Low | Wilder RSI is seeded from the first 14 closes of a 3-month window, so its value depends on the window length (another RSI 14 seeded differently will not agree to the decimal). | `scan.py:485-495` |

## 3. The other skills (independent pass, High/Med re-verified)

| skill | sev | finding | evidence |
| --- | --- | --- | --- |
| broker | High (design) | The "explicit yes" gate is advisory: `--reply` is whatever text the agent passes, and the order MCP tool can be called without `ticket.py`. The only hard gate is the client permission prompt, which the SKILL.md says to keep on "Needs approval". | `ticket.py:271-297`, `:500-526` |
| broker | Med | The `"paper"` config flag only changes labels; the real switch is `ALPACA_PAPER_TRADE` in the MCP env, never cross-checked. `uvx alpaca-mcp-server` is unpinned. | `ticket.py:355-356`, `claude_desktop_config.example.json` |
| timemachine | High (method) | See section 4. | `timemachine.py:335-386` |
| timemachine | Med | A plan entered at 10:15 and simulated at 10:50 re-draws the 10:15-10:45 bars that already happened. | `timemachine.py:366` |
| exit | Med | ATR look-ahead on replays: with `--entry` the bar cap is dropped (`entry_bar=None`) and `now` falls back to the data's last time, so the ATR includes bars after the entry. Verified. | `exit.py:553`, `:588`, `:450-451` |
| entry | Med | A partial opening range is accepted silently (two of three range bars still set the range high). The 60 s settle rule can treat a lagging intra-bar price as a closed bar. | `entry.py:482-495`, `:464` |
| alerts | Med | Quote time is parsed but never compared with now: a stale quote can fire or miss an alert. Only the last price is checked against stops and targets, not the day's high/low. | `alerts.py:238-240` vs `:464-479` |
| risk | Med | Falls back to a price labelled stale and still sizes and passes. A stop that exists only in the plan satisfies `require_stop`. | `check.py:502-503`, `:574-582` |
| entry, alerts | Low | Symbols are not URL-quoted. The host is fixed, so this is query-string tampering at most. | `entry.py:234-235`, `alerts.py:197`, `:201` |
| pelosi | Low | `fetch <doc_id>` is not validated before `os.path.join`, so `../` can write outside the PDF folder (it does check the `%PDF` magic). | `ptr.py:458-466` |

None of these were changed: they are outside this branch's scope (the brief says not to change
unrelated skills, and not to touch trader-broker). They are recorded for a follow-up.

## 4. The Time Machine's Monte Carlo: useful conditional information or false confidence?

What it does (`timemachine.py:335-386`): each future 5-minute bar is drawn independently, with
replacement, from all ~59 past sessions' bars at the same time-of-day slot +/-2, stored as OHLC
relative to the previous close and chained from the entry price. It is an i.i.d. per-bar
bootstrap stratified by time of day, not a session or block bootstrap.

What the 10,000 replays are: 10,000 recombinations of about 4,500 historical bars. The count only
reduces Monte Carlo noise (seed-to-seed standard deviation of the target-first share was 0.5
points in the independent pass). It adds no information about the stock. Shuffling bars
destroys exactly what decides a stop-versus-target race: autocorrelation, trend persistence and
volatility clustering.

What it is conditional on: time of day, the 60-day average bar distribution (including its
drift), and the stop/target distances. It is not conditional on the breakout having happened
(all sessions are pooled), on today's volatility or gap, or on the bars already realised after
the entry.

Measured on the reel's TSLA plan (entry 364.18, stop 358.32, target 370.04), independent pass:

| variant | target first, share of resolved paths |
| --- | --- |
| as shipped, seed 19 | 53.6 % ("close to a coin flip") |
| 60-day drift removed | 45.5 % |
| outer bootstrap of which sessions are in the pool, 5/50/95 % | 42.2 / 55.1 / 65.2 % |
| actual ORB trades replayed on the same 59 days (n = 33) | 46 % |

The verdict thresholds (5 and 15 points) are small against a 23-point interval from resampling
the pool alone, so the verdict label is not robust. No transaction costs are charged.

Conclusion: it is a reasonable *volatility-scaled sanity check* of whether a stop and target are
of comparable size given recent bar ranges. It is not evidence about the setup, and the
"10,000 sessions" framing overstates the information content. For crypto it should not be ported
as-is; if ported, use a block or whole-session bootstrap, report a resampling interval, and charge
costs. This branch does not port it.

## 5. Crypto portability, per skill

| skill | verdict | reason |
| --- | --- | --- |
| scanner `daily` rules | **ported** (this branch) | pure functions on closes; meaning changes from sessions to bars, so new names |
| scanner `orb` | **not ported as-is** | tied to the 9:30 ET bell; an experimental UTC-anchored variant exists, off by default (section 7.5) |
| timemachine | adapt later | engine is market-neutral; method needs fixing first (section 4) |
| exit, budget | adapt later | stop/target and sizing maths are generic; need tick/step sizes, fees, no `day` TIF |
| entry, alerts, risk | adapt later | NYSE clock, ticker regex blocks crypto pairs (`check.py:87`, `:482-484`) |
| broker | irrelevant / out of scope | Alpaca/IBKR stock orders; the brief forbids order placement |
| news, analyst, pelosi | irrelevant | SEC filings, US fundamentals, House disclosures |

For crypto the scanner (breadth discovery across hundreds of markets) is the piece worth
porting first; timemachine, exit and alerts need method fixes before they are worth porting.

## 6. What this branch changes

Changed (existing files):

- `trader-scanner/scan.py`: a `crypto` subcommand. The crypto module is imported in a guarded
  block: if anything in the crypto files fails to import, `universe`, `daily` and `orb` still run
  and `crypto` prints one plain error. No existing function was modified.
- `trader-scanner/SKILL.md`: a crypto section and trigger phrases; the equity text is unchanged.
- `README.md`: one command line, one folder-listing line.
- `tests/test_repo.py`: the root layout now admits `reports/` and ignores `.pytest_cache`.
- `.gitignore` (new): `__pycache__/`, `*.pyc`, `.pytest_cache/`.

New files: `trader-scanner/crypto_providers.py`, `crypto_setups.py`, `crypto_scan.py`,
`crypto_eval.py`, `crypto_pit.py`, `binance.example.ini`, `breakout_pit.ini`;
`tests/test_crypto_scanner.py` and fixtures; the reports in `reports/`.

Constraints kept: stdlib only, Python 3.8 syntax, 11 skill folders, no em dashes, no new skill,
no change to the other ten skills, no authenticated endpoint, no order code.

## 7. Binance scanner design

### 7.1 Layers

```
scan.py crypto ──> crypto_scan.py ──────────────> crypto_setups.py (rules, indicators; reuses scan.rsi)
                   Config (INI), universe,          
                   eligibility, quality gates,
                   ranking, output contract
                        │
                        ▼
                   crypto_providers.py
                   MarketDataProvider ── BinanceSpotProvider / BinanceFuturesProvider
                   Client (weight budget, retries) ── Transport: Live | Fixture | Recording
                   normalize_klines (closed-candle hygiene)
```

The provider contract is four calls: `server_time_ms`, `instruments`, `tickers`,
`klines(symbol, interval, bars, end_ms)`; futures add `derivatives_context`. A new exchange is
a new provider class; nothing in the scan branches on the exchange name. The Yahoo path was
deliberately not rewrapped in the new interface: doing so would have meant rewriting working,
tested code for no user-visible gain.

### 7.2 Binance facts verified live on 2026-10-31 (not assumed)

| fact | consequence in the code |
| --- | --- |
| Spot REQUEST_WEIGHT 6000/min; Futures 2400/min (from `exchangeInfo.rateLimits`) | per-market `WeightBudget`, default 60 % of the limit |
| Spot `exchangeInfo` is ~18 MB, weight 20 | cached on disk for 1 h, per market |
| Spot `ticker/24hr` carries bid/ask; Futures' does not | futures spread from `ticker/bookTicker` |
| Spot: 1373 TRADING, 2369 BREAK symbols | only TRADING is eligible, BREAK is a stated reason |
| Futures: 703 PERPETUAL, 235 TRADIFI_PERPETUAL (equity perps), quarterlies, PREMARKET | default `contract_types = PERPETUAL`, `underlying_types = COIN` |
| Multiplier symbols: `1000PEPEUSDT`, `1MBABYDOGEUSDT`, `1000000MOGUSDT` | `asset_id` strips the multiplier, `multiplier` is reported |
| DEEP: no Spot USDT market; Futures only | the allowlist reports "not listed on binance spot as DEEPUSDT" |
| `data-api.binance.vision` serves public Spot market data | used first; `api.binance.com` is the fallback host |
| Kline close time = open + interval - 1 ms; the open candle is returned | a candle counts as closed when `open + interval <= server time` |

### 7.3 Data hygiene (all counted in each timeframe's `quality` block, never silently fixed)

Rows that fail OHLC sanity, rows off the interval grid, duplicates (last copy wins), unordered
input (sorted), the open candle, and candles after the scan clock are dropped and counted. Gaps
are counted, not filled. A timeframe is then excluded, with a reason, when it has too little
history, when its newest closed candle is not the one expected at server time (stale), when the
rules' window holds more than `max_missing_bars` gaps, or when it is mostly zero-volume.

### 7.4 Rules: no name silently redefined

The equity rule names are not reused. `breakout_20d` means 20 sessions on a stock; on crypto the
same maths counts 20 bars (20 calendar days on 1d, 20 hours on 1h), so it is
`breakout_close_high` with `lookback` in its params. Every match carries `origin`
(`ported:<equity name>`, `new`, `experimental`), a label generated from its actual parameters,
and its evidence. A per-timeframe override (`impulse_pct@1h = 4`) is visible in the params and
changes the config fingerprint. A randomized property test checks that the five ported rules
agree with the equity `find_setups` on 300 random series.

Relative volume and momentum acceleration did not exist in the original scanner (the brief
listed them as existing). They are implemented as new, labelled `origin: new`.

### 7.5 Opening-range breakout on a 24/7 market

Crypto has no opening bell. The UTC hour-of-day profile (15 Spot markets, 90 days of 1h bars to
2026-10-31) shows no activity spike at 00:00 UTC; the busiest hours are 13:00-15:59 UTC (14:00
holds 7.0 % of daily volume against a uniform 4.2 %, and the mean absolute hourly return is about
1.5x the quiet hours), around the US equity cash open. An anchored range is therefore
defensible only around 13:30 UTC, and only as a hypothesis. `utc_orb` is implemented, off by
default, labelled EXPERIMENTAL in every match, with a configurable anchor. Its edge was not
tested.

### 7.6 Ranking and scores

Candidates sort by: 1D and 1H both bullish (`aligned`), then `descriptive_score` (a count of
matched bullish rules across timeframes), then all matched rules, then 24h quote volume, then the
instrument id. Every key is deterministic. `estimated_probability` is always `null`: no
probability is fabricated from indicator weights, and the walk-forward check
(`BINANCE_SCANNER_VALIDATION.md`) gives no basis for one.

## 8. Limitations

- Replays (`--now` in the past) use point-in-time candles but today's universe, 24h liquidity
  and prices (survivorship). The output says so (`replay_note`).
- Rules index candles by position. A gap inside the window is tolerated up to
  `max_missing_bars`, so the rules can then span slightly more wall-clock time than their name.
- Futures funding is reported as the last rate; funding intervals differ per symbol (4 h or 8 h)
  and are not annualized.
- The spot `exchangeInfo` download (~18 MB) dominates the first run's time (~20 s); cached runs
  take a few seconds.
