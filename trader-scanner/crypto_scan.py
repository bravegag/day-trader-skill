#!/usr/bin/env python3
"""Crypto market scan for trader-scanner: Binance Spot and USD-M Futures. Standard library only.

    python3 scan.py crypto                                    # trader/binance.ini, else the built-in defaults
    python3 scan.py crypto --config trader/binance.ini --market usdm
    python3 scan.py crypto --symbols SUI,DEEP,WAL             # an allowlist for this run
    python3 scan.py crypto --symbols @sui_ecosystem           # a [groups] entry of the INI
    python3 scan.py crypto --symbols top:40                   # the 40 most traded (24h quote volume)
    python3 scan.py crypto --offline DIR --now 2026-10-31T12:00Z --json

Everything else lives in the INI (see binance.example.ini next to this file).

What it does, in order:
  1. Server time from Binance (closed-candle semantics hang on it; --now replays the past).
  2. exchangeInfo: every listed market. 24h tickers: last price, quote volume, trades, spread.
  3. The universe: an allowlist (base assets or full symbols), a [groups] entry, or the top N by
     24h quote volume. Each requested name resolves to one market or to a stated reason it cannot.
  4. Eligibility, each failure a named reason: status TRADING, quote asset, contract type and
     underlying (futures), stablecoin bases, quote volume, trade count, spread, price age.
  5. Closed candles per timeframe, bounded concurrency, weight-budgeted, retried with backoff.
     Quality gates: enough history, newest closed candle is the expected one (not stale), no
     more than max_missing_bars gaps, not mostly zero-volume.
  6. The setups (crypto_setups.py) on closed candles only.
  7. Futures: mark, index, basis and funding for every candidate, open interest for the first ones.
  8. Per timeframe, the direction the matched rules assert (crypto_setups.timeframe_bias); per market,
     whether those directions agree across timeframes (rule_agreement); a deterministic, descriptive
     ranking by that agreement and the count of bullish rules. It is not a probability.

Public endpoints only, no API key, no order endpoint anywhere in the code path.
"""

import argparse
import configparser
import datetime
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import crypto_providers as cp  # noqa: E402
import crypto_setups as cs  # noqa: E402

SCHEMA = "day-trader-skill/crypto-scan"
# v2: rules carry kind and summary; rvol_expansion and rsi_oversold are direction "unspecified" (v1: rvol took
# the bar's sign, rsi_oversold was "condition"); per-timeframe bias, rule_agreement and the rule counts are new;
# aligned and descriptive_score keep their names with v2 meanings (see the SKILL.md, "Output schema"). v2 has no
# "evidence" keys: the scan reports its own rules only; comparing it with any model is the reader's business.
SCHEMA_VERSION = 2
DEFAULT_CONFIG = "trader/binance.ini"
# Paths only; behaviour stays in the INI. Precedence, highest first: the command-line flag (--config, --out),
# then the environment, then the INI, then the built-in defaults. TRADER_OUTPUT_DIR stands in for the trader/
# folder: every relative path of [output] and [cache] resolves under it (an absolute path in the INI is kept).
ENV_CONFIG_FILE = "TRADER_CONFIG_FILE"      # the INI used when --config is not given
ENV_OUTPUT_DIR = "TRADER_OUTPUT_DIR"        # where scan results, recordings and the cache go
NOTE = "A scan is a list of charts that match a rule, not a list of coins to buy."
SCOPE_NOTE = ("Scope: bullish setup rules, plus observations without a direction (oversold state, volume). There "
              "are no bearish rules, so a timeframe without a match is \"no rule matched\": not bearish, not neutral.")
SCORE_NOTE = ("Order: rule agreement across timeframes, then bullish_rules (bullish-direction matches; volume and "
              "oversold observations are not counted; descriptive_score is the same number). A descriptive ranking, "
              "not a probability or a validated edge (estimated_probability is null).")
# rule_agreement values, in ranking order
AGREEMENT_ORDER = ("bullish", "partial_bullish", "conflicting", "partial_bearish", "bearish", "unspecified", "none")
AGREEMENT_TEXT = {"bullish": "BULLISH RULES", "bearish": "BEARISH RULES", "partial_bullish": "BULLISH RULES",
                  "partial_bearish": "BEARISH RULES", "conflicting": "CONFLICTING RULES",
                  "unspecified": "NO DIRECTIONAL RULE", "none": "NO RULE MATCHED"}
STABLE_BASES = ("USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1", "USDS", "RLUSD", "XUSD", "BFUSD",
                "EUR", "EURI", "AEUR", "USDEB", "RWUSD", "PYUSD", "U")

DEFAULTS = {
    "provider": {"name": "binance", "market": "spot"},
    "universe": {"mode": "allowlist",
                 "symbols": "BTC, ETH, SUI, INJ, XRP, SOL, BNB, PAXG, PENGU, WAL, DEEP, ONDO, ZEC, HYPE, DOGE, TAO",
                 "top_n": "40", "exclude_bases": ", ".join(STABLE_BASES),
                 "contract_types": "PERPETUAL", "underlying_types": "COIN"},
    "quote": {"asset": "USDT"},
    "timeframes": {"intervals": "1d, 1h", "bars": "250", "bars@1h": "500"},
    "liquidity": {"min_quote_volume_24h": "2000000", "min_trades_24h": "2000", "max_spread_bps": "30",
                  "max_zero_volume_ratio": "0.05", "max_missing_bars": "2", "max_price_age_seconds": "600"},
    "setups": {"enabled": ", ".join(cs.BAR_SETUPS)},
    "network": {"max_workers": "4", "weight_safety": "0.6", "retries": "4", "backoff_seconds": "1.0",
                "timeout_seconds": "20", "max_context_requests": "40"},
    "output": {"path": "trader/crypto_watchlist.json", "show": "20", "record_dir": "", "candles": "0",
               "unmatched": "false"},
    "cache": {"dir": "trader/cache/binance", "exchange_info_ttl_seconds": "3600"},
    "setup.utc_orb": {"enabled": "false", "interval": "15m"},
}
INT_PARAMS = {"lookback", "impulse_bars", "flag_bars", "period", "bars", "range_minutes", "expire_minutes"}


class CryptoScanError(Exception):
    pass


# ---------------------------------------------------------------- configuration

class Config(object):
    """INI with built-in defaults. A key may carry a per-timeframe override: `bars@1h = 500`."""

    def __init__(self, path=None, text=None):
        self.ini = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
        self.ini.optionxform = str
        self.ini.read_dict(DEFAULTS)
        self.source = None
        if text is not None:
            self.ini.read_string(text)
            self.source = "<text>"
        elif path:
            if not os.path.exists(path):
                raise CryptoScanError("no config at %s (copy binance.example.ini there)" % path)
            try:
                self.ini.read(path, encoding="utf-8")
            except configparser.Error as e:
                raise CryptoScanError("%s is not a valid INI: %s" % (path, e))
            self.source = path

    def get(self, section, key, tf=None, default=None):
        if tf and self.ini.has_option(section, "%s@%s" % (key, tf)):
            return self.ini.get(section, "%s@%s" % (key, tf)).strip()
        if self.ini.has_option(section, key):
            return self.ini.get(section, key).strip()
        return default

    def num(self, section, key, tf=None, cast=float):
        v = self.get(section, key, tf)
        try:
            return cast(v)
        except (TypeError, ValueError):
            raise CryptoScanError("[%s] %s = %r is not a number" % (section, key, v))

    def flag(self, section, key):
        return (self.get(section, key) or "").lower() in ("1", "true", "yes", "on")

    def items(self, section, key, tf=None):
        return [x.strip() for x in (self.get(section, key, tf) or "").replace("\n", ",").split(",") if x.strip()]

    def set(self, section, key, value):
        if not self.ini.has_section(section):
            self.ini.add_section(section)
        self.ini.set(section, key, str(value))

    def setup_params(self, sid, tf):
        p = dict(cs.DEFAULT_PARAMS[sid])
        sec = "setup." + sid
        for k in list(p):
            v = self.get(sec, k, tf)
            if v is not None:
                p[k] = v if k == "anchor" else (int(float(v)) if k in INT_PARAMS else float(v))
        return p

    def as_dict(self):
        return {s: dict(sorted(self.ini.items(s))) for s in sorted(self.ini.sections())}

    def fingerprint(self):
        """sha256 of the effective configuration, minus paths that do not change what is scanned."""
        d = self.as_dict()
        for s, k in (("output", "path"), ("output", "show"), ("output", "record_dir"), ("output", "candles"), ("output", "unmatched"),
                     ("cache", "dir")):
            d.get(s, {}).pop(k, None)
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- paths

def resolve_path(path, market=None, env=None):
    """An INI path as written, with {market} filled in; a relative one moves under TRADER_OUTPUT_DIR when set."""
    env = os.environ if env is None else env
    if not path:
        return path
    if market:
        path = path.replace("{market}", market)
    path = os.path.expanduser(path)
    root = env.get(ENV_OUTPUT_DIR)
    if os.path.isabs(path) or not root:
        return path
    rel = os.path.normpath(path)
    if rel == "trader" or rel.startswith("trader" + os.sep):
        rel = rel[len("trader") + 1:]
    return os.path.join(os.path.expanduser(root), rel)


def config_path(flag, env=None):
    """--config, else TRADER_CONFIG_FILE, else trader/binance.ini when it exists, else the built-in defaults."""
    env = os.environ if env is None else env
    if flag:
        return flag
    if env.get(ENV_CONFIG_FILE):
        return os.path.expanduser(env[ENV_CONFIG_FILE])
    return DEFAULT_CONFIG if os.path.exists(DEFAULT_CONFIG) else None


def unmatched_filter(cfg):
    """[output] unmatched: false (default), true (every scanned market), or base assets / symbols / @group entries."""
    value = (cfg.get("output", "unmatched") or "").strip()
    if value.lower() in ("", "0", "false", "no", "off"):
        return False
    if value.lower() in ("1", "true", "yes", "on"):
        return True
    names = set()
    for part in cfg.items("output", "unmatched"):
        if part.startswith("@"):
            grp = cfg.get("groups", part[1:])
            if grp is None:
                raise CryptoScanError("[output] unmatched: no [groups] %s in the config" % part[1:])
            names |= {x.strip().upper() for x in grp.replace("\n", ",").split(",") if x.strip()}
        else:
            names.add(part.upper())
    return names


# ---------------------------------------------------------------- time

def parse_now_utc(text):
    """ISO time -> unix ms. A time without an offset is UTC (crypto has no home time zone)."""
    if not text:
        return None
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise CryptoScanError('--now wants a UTC time like "2026-10-31T12:00Z"')
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def iso(ms):
    return cp._iso(ms)


# ---------------------------------------------------------------- universe

def resolve_universe(instruments, tickers, cfg, market):
    """-> (selected [(requested, Instrument)], excluded [{requested, symbol, reason}]).
    The allowlist is resolved literally: a base asset BASE becomes BASE+QUOTE, else a multiplier
    listing (1000BASE+QUOTE); a name that already ends in the quote asset is a full symbol."""
    quote = cfg.get("quote", "asset").upper()
    mode = cfg.get("universe", "mode").lower()
    by_sym = {i.symbol: i for i in instruments}
    excluded = []
    if mode == "allowlist":
        selected, seen = [], set()
        for name in cfg.items("universe", "symbols"):
            name = name.upper()
            cands = [name] if name.endswith(quote) and name in by_sym else \
                [name + quote, "1000" + name + quote, "1M" + name + quote, "1000000" + name + quote]
            inst = next((by_sym[s] for s in cands if s in by_sym), None)
            if inst is None:
                excluded.append({"requested": name, "symbol": None,
                                 "reason": "not listed on binance %s as %s" % (market, name + quote)})
            elif inst.symbol not in seen:
                seen.add(inst.symbol)
                selected.append((name, inst))
        return selected, excluded
    if mode not in ("top_volume", "all"):
        raise CryptoScanError("[universe] mode is allowlist, top_volume or all, not %s" % mode)
    pool = [(i.asset_id, i) for i in instruments if i.quote == quote and i.status == "TRADING"]
    if mode == "top_volume":
        pool.sort(key=lambda x: (-((tickers.get(x[1].symbol) or cp.Ticker(*[None] * 9)).quote_volume or 0),
                                 x[1].symbol))
        # eligibility runs after this cut, so take a margin and trim after the filters
        pool = pool[:max(1, int(cfg.num("universe", "top_n"))) * 3]
    else:
        pool.sort(key=lambda x: x[1].symbol)
    return pool, excluded


def eligibility(inst, ticker, cfg, now_ms):
    """None when the market may be scanned, else the reason it may not."""
    quote = cfg.get("quote", "asset").upper()
    if inst.status != "TRADING":
        return "status %s, not TRADING" % inst.status
    if inst.quote != quote:
        return "quote asset %s, not %s" % (inst.quote, quote)
    if inst.asset_id.upper() in {b.upper() for b in cfg.items("universe", "exclude_bases")}:
        return "excluded base asset (stablecoin or fiat list)"
    if inst.market == "usdm":
        if inst.contract_type not in cfg.items("universe", "contract_types"):
            return "contract type %s" % inst.contract_type
        types = cfg.items("universe", "underlying_types")
        if types and inst.underlying_type not in types:
            return "underlying type %s" % inst.underlying_type
    if ticker is None:
        return "no 24h ticker"
    if ticker.last_price is None or ticker.last_price <= 0:
        return "no last price"
    if (ticker.quote_volume or 0) < cfg.num("liquidity", "min_quote_volume_24h"):
        return "24h quote volume %s under %s" % (_human(ticker.quote_volume or 0),
                                                 _human(cfg.num("liquidity", "min_quote_volume_24h")))
    if ticker.trades < cfg.num("liquidity", "min_trades_24h"):
        return "%d trades in 24h, under %d" % (ticker.trades, cfg.num("liquidity", "min_trades_24h"))
    sp = spread_bps(ticker)
    if sp is not None and sp > cfg.num("liquidity", "max_spread_bps"):
        return "spread %.1f bps over %g" % (sp, cfg.num("liquidity", "max_spread_bps"))
    if ticker.close_ms and (now_ms - ticker.close_ms) / 1000.0 > cfg.num("liquidity", "max_price_age_seconds"):
        return "last price is %ds old" % ((now_ms - ticker.close_ms) // 1000)
    return None


def spread_bps(t):
    if not t.bid or not t.ask or t.ask < t.bid:
        return None
    return (t.ask - t.bid) / ((t.ask + t.bid) / 2.0) * 1e4


def _human(x):
    for unit, d in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= d:
            return "%.1f%s" % (x / d, unit)
    return "%.0f" % x


# ---------------------------------------------------------------- per timeframe

def quality_gate(candles, q, interval, now_ms, need, cfg):
    """None when the candles can be scanned, else the reason. Reads the closed candles only."""
    if len(candles) < need:
        return "insufficient history: %d closed bars, need %d" % (len(candles), need)
    exp = cp.expected_last_open(interval, now_ms)
    if candles[-1].open_ms < exp:
        return "stale: newest closed candle opened %s, expected %s" % (iso(candles[-1].open_ms), iso(exp))
    step = cp.INTERVAL_MS[interval]
    window = candles[-need:]
    missing = (window[-1].open_ms - window[0].open_ms) // step + 1 - len(window)
    if missing > cfg.num("liquidity", "max_missing_bars", interval, int):
        return "%d missing candles in the last %d" % (missing, need)
    zero = sum(1 for c in window if c.volume == 0)
    if zero > cfg.num("liquidity", "max_zero_volume_ratio", interval) * len(window):
        return "%d zero-volume candles in the last %d" % (zero, need)
    return None


def scan_timeframe(candles, interval, enabled, cfg):
    params = {sid: cfg.setup_params(sid, interval) for sid in enabled}
    return cs.bar_setups(candles, enabled, params), cs.indicators(candles)


def rule_agreement(bias, intervals):
    """Do the directional rule signals agree across the configured timeframes?

    bias: {tf: timeframe bias, or None when the timeframe was not evaluated (excluded or failed)}.
    -> bullish / bearish   every configured timeframe was evaluated and asserts that direction (two or more)
       partial_bullish / partial_bearish   some timeframes assert it, none asserts the opposite
       conflicting   bullish and bearish both asserted (across timeframes or within one)
       unspecified   rules matched, but none has a direction (volume, oversold state)
       none          no rule matched
    A timeframe without a match never counts for or against a direction."""
    vals = [bias.get(tf) for tf in intervals]
    bull = any(v in ("bullish", "mixed") for v in vals)
    bear = any(v in ("bearish", "mixed") for v in vals)
    if bull and bear:
        return "conflicting"
    if bull or bear:
        side = "bullish" if bull else "bearish"
        return side if len(vals) >= 2 and all(v == side for v in vals) else "partial_" + side
    return "unspecified" if "unspecified" in vals else "none"


# ---------------------------------------------------------------- the scan

def run_scan(provider, cfg, now_ms=None, log=None):
    """Pure orchestration over a provider. Returns the output document (a dict)."""
    t0 = time.time()
    log = log or (lambda m: None)
    server_ms = provider.server_time_ms()
    replay = now_ms is not None and now_ms < server_ms - 60000
    now_ms = now_ms if now_ms is not None else server_ms
    market = provider.market
    intervals = cfg.items("timeframes", "intervals")
    for tf in intervals:
        if tf not in cp.INTERVAL_MS:
            raise CryptoScanError("[timeframes] %s is not a supported interval (%s)" % (tf, ", ".join(cp.INTERVAL_MS)))
    enabled = [s for s in cfg.items("setups", "enabled")]
    for s in enabled:
        if s not in cs.BAR_SETUPS:
            raise CryptoScanError("[setups] unknown setup %s (one of %s)" % (s, ", ".join(cs.BAR_SETUPS)))
    orb_on = cfg.flag("setup.utc_orb", "enabled")
    keep_candles = int(cfg.num("output", "candles"))
    keep_unmatched, observed = unmatched_filter(cfg), []

    log("Binance %s: exchange info and 24h tickers..." % market)
    instruments = provider.instruments()
    tickers = provider.tickers()
    pool, excluded = resolve_universe(instruments, tickers, cfg, market)
    requested = len(pool) + len(excluded)

    eligible = []
    for req, inst in pool:
        why = eligibility(inst, tickers.get(inst.symbol), cfg, now_ms)
        if why:
            excluded.append({"requested": req, "symbol": inst.symbol, "reason": why})
        else:
            eligible.append(inst)
    if cfg.get("universe", "mode").lower() == "top_volume":
        top = int(cfg.num("universe", "top_n"))
        eligible.sort(key=lambda i: (-(tickers[i.symbol].quote_volume or 0), i.symbol))
        eligible = eligible[:top]
        requested = top
        excluded = []  # in top_volume mode the cut itself is the filter; per-symbol reasons would be noise

    needs = {tf: max(cs.required_bars(enabled, {s: cfg.setup_params(s, tf) for s in enabled}),
                     int(cfg.num("timeframes", "bars", tf))) for tf in intervals}
    jobs = [(i, tf, needs[tf]) for i in eligible for tf in intervals]
    orb_tf = cfg.get("setup.utc_orb", "interval")
    if orb_on:
        if orb_tf not in cp.INTERVAL_MS:
            raise CryptoScanError("[setup.utc_orb] interval %s is not supported" % orb_tf)
        jobs += [(i, "orb:" + orb_tf, 24 * 60 * 60000 // cp.INTERVAL_MS[orb_tf] + 4) for i in eligible]
    log("Fetching closed candles: %d markets x %s (%d requests or more)..."
        % (len(eligible), ", ".join(intervals + (["orb " + orb_tf] if orb_on else [])), len(jobs)))

    results, failed, banned = {}, {}, None

    def fetch(job):
        inst, tf, bars = job
        interval = tf.split(":")[-1]
        rows = provider.klines(inst.symbol, interval, bars + 1, end_ms=now_ms)  # +1: the open candle
        return cp.normalize_klines(rows, interval, now_ms)

    workers = max(1, int(cfg.num("network", "max_workers")))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch, j): j for j in jobs}
        for f in as_completed(futs):
            inst, tf, _ = futs[f]
            try:
                results[(inst.symbol, tf)] = f.result()
            except cp.BannedError as e:
                banned = str(e)
                for g in futs:
                    g.cancel()
            except cp.HttpError as e:
                failed["%s %s" % (inst.symbol, tf)] = str(e)
            except Exception as e:  # a malformed answer must not sink the rest of the scan
                if not f.cancelled():
                    failed["%s %s" % (inst.symbol, tf)] = "%s: %s" % (type(e).__name__, e)
    for j in jobs:
        key = (j[0].symbol, j[1])
        if key not in results and "%s %s" % key not in failed:
            failed["%s %s" % key] = "not fetched (the scan stopped: %s)" % (banned or "cancelled")

    candidates, scanned = [], 0
    for inst in eligible:
        t = tickers[inst.symbol]
        tfs, any_ok = {}, False
        for tf in intervals:
            if (inst.symbol, tf) not in results:
                tfs[tf] = {"status": "failed", "reason": failed.get("%s %s" % (inst.symbol, tf))}
                continue
            candles, q = results[(inst.symbol, tf)]
            why = quality_gate(candles, q, tf, now_ms, needs[tf], cfg)
            base = {"bars": len(candles), "quality": q,
                    "last_closed_open": iso(candles[-1].open_ms) if candles else None,
                    "last_closed_close": iso(candles[-1].open_ms + cp.INTERVAL_MS[tf]) if candles else None}
            if why:
                tfs[tf] = dict(base, status="excluded", reason=why)
                continue
            any_ok = True
            hits, ind = scan_timeframe(candles, tf, enabled, cfg)
            tfs[tf] = dict(base, status="ok", setups=hits, indicators=ind)
            if keep_candles > 0:
                tfs[tf]["candles"] = [[k.open_ms, k.open, k.high, k.low, k.close, k.volume]
                                      for k in candles[-keep_candles:]]
        if orb_on and (inst.symbol, "orb:" + orb_tf) in results:
            oc, _ = results[(inst.symbol, "orb:" + orb_tf)]
            hit, status = cs.utc_orb(oc, cp.INTERVAL_MS[orb_tf], now_ms, cfg.setup_params("utc_orb", orb_tf))
            tfs["orb:" + orb_tf] = {"status": "ok", "orb_status": status, "setups": [hit] if hit else []}
        if not any_ok:
            reasons = sorted({v.get("reason") or "" for k, v in tfs.items() if not k.startswith("orb:")})
            excluded.append({"requested": inst.asset_id, "symbol": inst.symbol, "reason": "; ".join(reasons)})
            continue
        scanned += 1
        for v in tfs.values():
            if v["status"] == "ok":
                v["bias"] = cs.timeframe_bias(v["setups"])
        hits = [h for v in tfs.values() for h in v.get("setups", [])]
        bull_tfs = sorted(tf for tf, v in tfs.items() if any(h["direction"] == "bullish" for h in v.get("setups", [])))
        bear_tfs = sorted(tf for tf, v in tfs.items() if any(h["direction"] == "bearish" for h in v.get("setups", [])))
        n_bull = sum(1 for h in hits if h["direction"] == "bullish")
        n_bear = sum(1 for h in hits if h["direction"] == "bearish")
        n_all = len(hits)
        rule_bias = {tf: tfs[tf].get("bias") for tf in intervals}
        agreement = rule_agreement(rule_bias, intervals)
        if not n_all and not (keep_unmatched is True or (keep_unmatched and (
                inst.asset_id.upper() in keep_unmatched or inst.symbol.upper() in keep_unmatched))):
            continue
        (candidates if n_all else observed).append({
            "instrument_id": inst.instrument_id, "exchange": "binance", "market": inst.market,
            "symbol": inst.symbol, "base_asset": inst.base, "quote_asset": inst.quote, "asset_id": inst.asset_id,
            "multiplier": inst.multiplier, "contract_type": inst.contract_type,
            "last_price": t.last_price, "price_time": iso(t.close_ms),
            "price_age_seconds": max(0.0, round((now_ms - t.close_ms) / 1000.0, 1)) if t.close_ms and not replay else None,
            "quote_volume_24h": round(t.quote_volume or 0, 2), "trades_24h": t.trades,
            "change_24h_pct": t.price_change_pct, "spread_bps": _r(spread_bps(t), 2),
            "timeframes": tfs, "rule_bias": rule_bias, "rule_agreement": agreement,
            "bullish_timeframes": bull_tfs, "bearish_timeframes": bear_tfs, "aligned": agreement == "bullish",
            "bullish_rules": n_bull, "bearish_rules": n_bear, "nondirectional_rules": n_all - n_bull - n_bear,
            "matched_rules": n_all, "descriptive_score": n_bull, "estimated_probability": None,
        })

    if provider.market == "usdm" and candidates and not banned:
        ctx = provider.derivatives_context([c["symbol"] for c in candidates],
                                           int(cfg.num("network", "max_context_requests")))
        for c in candidates:
            c["derivatives"] = dict(ctx.get(c["symbol"]) or {}, note="context only: no leverage or execution advice")

    candidates.sort(key=lambda c: (AGREEMENT_ORDER.index(c["rule_agreement"]), -c["bullish_rules"], c["bearish_rules"],
                                   -c["matched_rules"], -c["quote_volume_24h"], c["instrument_id"]))
    for k, c in enumerate(candidates):
        c["rank"] = k + 1
    excluded.sort(key=lambda e: (e["symbol"] or "", e["requested"] or ""))
    reasons = {}
    for e in excluded:
        key = e["reason"].split(":")[0].split(" under ")[0] if e["reason"] else "unknown"
        reasons[key] = reasons.get(key, 0) + 1
    status = "partial" if (failed or banned) else "complete"
    stats = getattr(getattr(provider, "client", None), "stats", {})
    doc = {
        "schema": SCHEMA, "schema_version": SCHEMA_VERSION, "created_by": "trader-scanner", "mode": "crypto",
        "created_at": iso(now_ms), "as_of_ms": now_ms, "server_time": iso(server_ms), "replay": replay,
        "provider": "binance", "exchange": "binance", "market": market, "quote_asset": cfg.get("quote", "asset"),
        "timeframes": intervals, "setups_enabled": enabled + (["utc_orb"] if orb_on else []),
        "config_source": cfg.source, "config_fingerprint": cfg.fingerprint(),
        "status": status, "stopped": banned,
        "coverage": {"requested": requested, "eligible": len(eligible), "scanned": scanned,
                     "excluded": len(excluded), "failed_fetches": len(failed), "candidates": len(candidates)},
        "exclusion_reasons": reasons, "network": stats, "seconds": round(time.time() - t0, 1),
        "scope_note": SCOPE_NOTE, "score_note": SCORE_NOTE, "note": NOTE,
        "candidates": candidates, "excluded": excluded, "failed": failed,
        "hits": [{"symbol": c["symbol"], "setups": sorted({h["id"] for v in c["timeframes"].values()
                                                           for h in v.get("setups", [])})} for c in candidates],
    }
    if keep_unmatched:
        observed.sort(key=lambda c: c["instrument_id"])
        doc["observed"] = observed
    if replay:
        doc["replay_note"] = ("replay: candles are as of --now, but the universe, 24h liquidity and prices are "
                              "today's (survivorship and liquidity are not point-in-time)")
    return doc


def _r(x, n):
    return None if x is None else round(x, n)


# ---------------------------------------------------------------- transport wiring

class CachedExchangeInfo(object):
    """Keeps exchangeInfo on disk for ttl seconds (spot's is ~18 MB and weighs 20). Market-scoped file."""

    def __init__(self, inner, folder, market, ttl):
        self.inner, self.path, self.ttl = inner, os.path.join(folder, "%s-exchangeInfo.json" % market), ttl

    def get(self, base, path, params):
        if not path.endswith("/exchangeInfo") or self.ttl <= 0:
            return self.inner.get(base, path, params)
        if os.path.exists(self.path) and time.time() - os.path.getmtime(self.path) < self.ttl:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh), {}
        doc, headers = self.inner.get(base, path, params)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, self.path)
        return doc, headers


def build_provider(cfg, offline=None, now_ms=None, sleep=time.sleep):
    market = cfg.get("provider", "market").lower()
    if offline:
        transport = cp.FixtureTransport(offline, now_ms)
    else:
        transport = cp.LiveTransport(timeout=cfg.num("network", "timeout_seconds"))
        if cfg.get("cache", "dir"):
            transport = CachedExchangeInfo(transport, resolve_path(cfg.get("cache", "dir"), market), market,
                                           cfg.num("cache", "exchange_info_ttl_seconds"))
        if cfg.get("output", "record_dir"):
            transport = cp.RecordingTransport(transport, resolve_path(cfg.get("output", "record_dir"), market))
    return cp.make_provider(cfg.get("provider", "name"), market, transport,
                            safety=cfg.num("network", "weight_safety"), retries=int(cfg.num("network", "retries")),
                            backoff=cfg.num("network", "backoff_seconds"), sleep=sleep)


# ---------------------------------------------------------------- CLI

def apply_symbols(cfg, text):
    """--symbols: 'SUI,DEEP' (allowlist), '@group' (an INI [groups] entry), 'top:40' (top_volume)."""
    if not text:
        return
    t = text.strip()
    if t.lower().startswith("top:"):
        cfg.set("universe", "mode", "top_volume")
        cfg.set("universe", "top_n", int(t[4:]))
        return
    names = []
    for part in t.replace(" ", ",").split(","):
        if not part:
            continue
        if part.startswith("@"):
            grp = cfg.get("groups", part[1:])
            if grp is None:
                raise CryptoScanError("no [groups] %s in the config" % part[1:])
            names += [x.strip() for x in grp.replace("\n", ",").split(",") if x.strip()]
        else:
            names.append(part)
    cfg.set("universe", "mode", "allowlist")
    cfg.set("universe", "symbols", ", ".join(names))


def render(doc, show):
    """The terminal view: per market, what happened on each timeframe, its direction, and whether the
    directional rules agree across timeframes. Every number shown is in the JSON document (the same `summary`,
    `bias` and `rule_agreement` fields)."""
    cov = doc["coverage"]
    out = []
    closes = {}
    for c in doc["candidates"]:
        for tf, v in c["timeframes"].items():
            if v.get("status") == "ok" and not tf.startswith("orb:"):
                closes.setdefault(tf, v["last_closed_close"])
    out.append("Binance %s scan as of %s UTC%s, closed candles only, %s quote. Config %s."
               % ("Spot" if doc["market"] == "spot" else "USD-M Futures", _when(doc["created_at"]),
                  " (REPLAY)" if doc["replay"] else "", doc["quote_asset"], doc["config_fingerprint"]))
    if closes:
        out.append("Evaluated candles, by their UTC close: %s. Later price moves are not in the rules."
                   % ", ".join("%s %s" % (tf, _when(closes[tf])) for tf in doc["timeframes"] if tf in closes))
    out.append("Requested %d, eligible %d, scanned %d, %d matched a rule. Status: %s%s."
               % (cov["requested"], cov["eligible"], cov["scanned"], cov["candidates"], doc["status"].upper(),
                  (" (%d fetches failed)" % cov["failed_fetches"]) if cov["failed_fetches"] else ""))
    if doc.get("stopped"):
        out.append("STOPPED: %s" % doc["stopped"])
    out.append(SCOPE_NOTE)
    for c in doc["candidates"][:show]:
        out.append("")
        out.extend(_render_candidate(c, doc))
    if len(doc["candidates"]) > show:
        out.append("")
        out.append("... and %d more in the JSON." % (len(doc["candidates"]) - show))
    out.append("")
    if doc["excluded"]:
        out.append("Excluded %d: %s." % (len(doc["excluded"]), "; ".join(
            "%s (%s)" % (e["symbol"] or e["requested"], e["reason"]) for e in doc["excluded"][:8])
            + (" ..." if len(doc["excluded"]) > 8 else "")))
    out.append(SCORE_NOTE)
    out.append(NOTE)
    return "\n".join(out)


def _render_candidate(c, doc):
    """Indented lines for one market."""
    tfs = [tf for tf in doc["timeframes"] if tf in c["timeframes"]] + sorted(
        tf for tf in c["timeframes"] if tf.startswith("orb:"))
    extra = ""
    d = c.get("derivatives")
    if d and d.get("funding_rate_last") is not None:
        extra = "  funding %+.4f%%" % (d["funding_rate_last"] * 100)
    later = _price_is_later(c, doc)
    lines = ["%3d  %-12s last %s%s  vol24h %s%s" % (
        c["rank"], c["symbol"], _price(c["last_price"]),
        " (priced %s UTC, after the scan time: not point-in-time)" % _when(c.get("price_time")) if later else "",
        _human(c["quote_volume_24h"]), extra)]
    lines.append("     %s" % agreement_text(c))
    for tf in tfs:
        v = c["timeframes"][tf]
        if v.get("status") != "ok":
            lines.append("     %-4s not evaluated: %s" % (tf, v.get("reason")))
            continue
        ind = v.get("indicators") or {}
        facts = ([("candle closed " + _when(v["last_closed_close"]))] if v.get("last_closed_close") else []) + (
            ["RSI %.0f" % ind["rsi14"]] if ind.get("rsi14") is not None else [])
        bias = v.get("bias") or cs.timeframe_bias(v.get("setups") or [])
        head = {"no_match": "no rule matched", "unspecified": "no direction"}.get(bias, bias)
        lines.append("     %-4s %s%s" % (tf, head, ("  (%s)" % ", ".join(facts)) if facts else ""))
        for h in v.get("setups") or []:
            lines.append("           %-19s %s" % (h["id"], h.get("summary") or h.get("label")))
    live = _live_line(c, doc, tfs)
    if live:
        lines.append("     " + live)
    return lines


def agreement_text(c):
    """What the directional rules assert across timeframes, in words: 'BULLISH RULES: 1d + 1h' only when every
    evaluated timeframe has a bullish rule and none a bearish one."""
    agreement = c.get("rule_agreement")
    bias = c.get("rule_bias") or {}
    text = AGREEMENT_TEXT.get(agreement, "RULES")
    side = {"bullish": "bullish", "partial_bullish": "bullish", "bearish": "bearish", "partial_bearish": "bearish"}
    if agreement in ("bullish", "bearish"):
        return "%s: %s" % (text, " + ".join(bias))
    if agreement in side:
        on = [tf for tf, b in bias.items() if b == side[agreement]]
        rest = ["%s: %s" % (tf, _bias_words(b)) for tf, b in bias.items() if b != side[agreement]]
        return "%s: %s only%s" % (text, " + ".join(on), (" (%s)" % "; ".join(rest)) if rest else "")
    if agreement == "conflicting":
        return "%s: %s" % (text, ", ".join("%s %s" % (tf, _bias_words(b)) for tf, b in bias.items()))
    if agreement == "unspecified":
        return "%s: only observations without a direction (%s)" % (
            text, ", ".join(tf for tf, b in bias.items() if b == "unspecified"))
    return text


def _bias_words(b):
    return {"no_match": "no rule matched", "unspecified": "no direction", None: "not evaluated"}.get(b, b)


def _live_line(c, doc, tfs):
    """The live price against the newest evaluated close: context after the evaluation, never part of a rule."""
    if doc.get("replay") or c.get("last_price") is None or _price_is_later(c, doc):
        return None
    ok = [tf for tf in tfs if not tf.startswith("orb:") and c["timeframes"][tf].get("status") == "ok"
          and (c["timeframes"][tf].get("indicators") or {}).get("close")]
    if not ok:
        return None
    tf = min(ok, key=lambda t: cp.INTERVAL_MS[t])
    close = c["timeframes"][tf]["indicators"]["close"]
    return "live %s at %s UTC is %+.2f%% from the %s close %s (after the evaluated candle, not in the rules)" % (
        _price(c["last_price"]), _when(c.get("price_time")), (c["last_price"] / close - 1) * 100, tf, _price(close))


def _price_is_later(c, doc):
    """A replay, or a recording whose tickers were taken after its candles, prices the market later than the scan."""
    t = parse_now_utc(c["price_time"]) if c.get("price_time") else None
    return bool(doc.get("replay")) or (t is not None and t > doc["as_of_ms"] + 60000)


def _when(text):
    """'2026-11-07T19:00:00Z' -> '2026-11-07 19:00' (UTC)."""
    if not text:
        return "?"
    return str(text).replace("T", " ").replace("+00:00", "").rstrip("Z")[:16]


def _price(x):
    if x is None:
        return "-"
    return ("%.2f" if x >= 100 else "%.4f" if x >= 1 else "%.6g") % x


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def add_arguments(p):
    p.add_argument("--config", help="INI file (default $%s, else %s if it exists, else the built-in defaults)"
                   % (ENV_CONFIG_FILE, DEFAULT_CONFIG))
    p.add_argument("--market", choices=["spot", "usdm", "futures"], help="override [provider] market")
    p.add_argument("--symbols", help="'SUI,DEEP' allowlist, '@group' from [groups], or 'top:N'")
    p.add_argument("--offline", metavar="DIR", help="a recorded fixture folder instead of the network")
    p.add_argument("--now", help="UTC time to scan as of (replay), e.g. 2026-10-31T12:00Z")
    p.add_argument("--out", help="override [output] path (taken as given, no $%s)" % ENV_OUTPUT_DIR)
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--show", type=int, help="candidates to print (all are written)")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true")


def cmd_crypto(a):
    cfg = Config(config_path(a.config))
    if a.market:
        cfg.set("provider", "market", "usdm" if a.market == "futures" else a.market)
    apply_symbols(cfg, a.symbols)
    now_ms = parse_now_utc(a.now)
    log = (lambda m: None) if (a.json or a.quiet) else (lambda m: print(m, file=sys.stderr))
    provider = build_provider(cfg, a.offline, now_ms)
    doc = run_scan(provider, cfg, now_ms, log)
    t = provider.client.transport
    if isinstance(t, cp.RecordingTransport):
        # trim exchangeInfo and tickers to the markets this scan touched, so a replay gives the same answer
        keep = {n.split(os.sep)[-1].rsplit("_", 1)[0] for n in t.klines}
        keep |= {e["symbol"] for e in doc["excluded"] if e["symbol"]}
        t.save(doc["as_of_ms"], keep, {"market": provider.market, "config": cfg.fingerprint()})
        log("Recorded %d markets into %s" % (len(keep), t.folder))
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    out = a.out or resolve_path(cfg.get("output", "path"), provider.market)
    if not a.no_write:
        write_json(out, doc)
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        print(render(doc, a.show if a.show is not None else int(cfg.num("output", "show"))))
        if not a.no_write:
            print("Wrote %s (%d candidates)." % (out, len(doc["candidates"])))
    return 0 if doc["candidates"] else 1


def run_cli(a):
    try:
        return cmd_crypto(a)
    except (CryptoScanError, cp.ProviderError) as e:
        print("scan.py crypto: %s" % e, file=sys.stderr)
        return 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Crypto market scan (Binance Spot / USD-M Futures).")
    add_arguments(ap)
    return run_cli(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
