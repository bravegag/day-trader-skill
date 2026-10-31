#!/usr/bin/env python3
"""Point-in-time test of the daily closing-high breakout (research). Standard library only.

    python3 crypto_pit.py fetch --market spot        # universe + daily candles (+ funding on usdm), cached
    python3 crypto_pit.py run   --market spot --period development
    python3 crypto_pit.py run   --market spot --period holdout

Pre-registered in reports/BREAKOUT_PREREGISTRATION.md; parameters in breakout_pit.ini, checked
against FROZEN below (a mismatch refuses to run).

Universe: every USDT symbol of the current exchangeInfo INCLUDING delisted ones (Spot BREAK,
Futures SETTLING) plus every symbol of the public archive listing (data.binance.vision), so
markets that no longer trade are in. Today's status finds symbols; it never decides eligibility.
Eligibility on signal day t uses only candles with open time <= t: the scanner's own quality
gate (crypto_scan.quality_gate) on the 250-candle window, the day-t candle's quote volume and
trade count, the scanner's stablecoin list. The rule is crypto_setups.bar_setups itself.
"""

import argparse
import csv
import datetime
import io
import json
import math
import os
import random
import re
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import crypto_providers as cp  # noqa: E402
import crypto_scan as cscan  # noqa: E402
import crypto_setups as cs  # noqa: E402

D = 86400000
FROZEN = {
    ("rule", "setup"): "breakout_close_high", ("rule", "lookback"): "20", ("rule", "window_bars"): "250",
    ("rule", "horizon_days"): "5", ("rule", "per_symbol_overlap"): "false",
    ("eligibility", "min_quote_volume"): "2000000", ("eligibility", "min_trades"): "2000",
    ("eligibility", "max_missing_bars"): "2", ("eligibility", "max_zero_volume_ratio"): "0.05",
    ("costs", "spot_per_side"): "0.0015", ("costs", "usdm_per_side"): "0.0010", ("costs", "usdm_funding"): "true",
    ("periods", "holdout_spot_start"): "2018-01-01", ("periods", "holdout_usdm_start"): "2020-01-01",
    ("periods", "holdout_end"): "2022-09-23", ("periods", "development_start"): "2022-09-24",
    ("periods", "development_end"): "2026-10-25", ("periods", "data_end"): "2026-10-31T00:00Z",
    ("statistics", "block_days"): "20", ("statistics", "replicates"): "5000", ("statistics", "seed"): "7",
    ("statistics", "newey_west_lag"): "5", ("statistics", "max_asset_share"): "0.25", ("statistics", "top_trim"): "0.01",
}
ORIGINAL_16 = {"BTC", "ETH", "SUI", "INJ", "XRP", "SOL", "BNB", "PAXG", "PENGU", "WAL", "DEEP", "ONDO", "ZEC",
               "HYPE", "DOGE", "TAO"}
ARCHIVE = "https://data.binance.vision"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
ARCH_PREFIX = {"spot": "data/spot/monthly/klines/", "usdm": "data/futures/um/monthly/klines/"}
DISCONTINUITY = math.log(2.0)
NON_COIN = {"EQUITY", "HK_EQUITY", "KR_EQUITY", "CN_EQUITY", "COMMODITY", "INDEX", "FX", "PREMARKET"}


class PitError(Exception):
    pass


def load_config(path):
    c = cscan.Config()  # the scanner's defaults: exclusion list and rule code stay the committed ones
    ini = cscan.configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    ini.optionxform = str
    if not ini.read(path, encoding="utf-8"):
        raise PitError("no config at %s" % path)
    bad = []
    for (s, k), v in sorted(FROZEN.items()):
        got = ini.get(s, k, fallback=None)
        if got is None or got.strip() != v:
            bad.append("[%s] %s = %r (pre-registered %r)" % (s, k, got, v))
    if bad:
        raise PitError("pre-registered values changed: " + "; ".join(bad))
    return ini, c


# ---------------------------------------------------------------- fetch

def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": cp.UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def s3_list(prefix, folders=True):
    out, marker = [], ""
    while True:
        t = _get("%s?delimiter=/&prefix=%s&marker=%s" % (S3, prefix, marker)).decode()
        if folders:
            out += re.findall(r"<Prefix>%s([^<]+)/</Prefix>" % re.escape(prefix), t)
        else:
            out += re.findall(r"<Key>([^<]+)</Key>", t)
        m = re.search(r"<NextMarker>([^<]+)</NextMarker>", t)
        if "<IsTruncated>true" not in t:
            break
        marker = m.group(1) if m else (out[-1] if not folders else prefix + out[-1] + "/")
    return out


def archive_rows(market, symbol, kind="klines"):
    """Monthly archive zips -> kline rows (open time in ms). Spot files from 2025 use microseconds."""
    if kind == "klines":
        prefix = "%s%s/1d/" % (ARCH_PREFIX[market], symbol)
    else:
        prefix = "data/futures/um/monthly/fundingRate/%s/" % symbol
    rows = []
    for key in sorted(k for k in s3_list(prefix, folders=False) if k.endswith(".zip")):
        z = zipfile.ZipFile(io.BytesIO(_get("%s/%s" % (ARCHIVE, key))))
        for name in z.namelist():
            text = z.read(name).decode("utf-8", "replace")
            for r in csv.reader(io.StringIO(text)):
                if not r or not r[0].strip().lstrip("-").isdigit():
                    continue  # header row
                rows.append(r)
    if kind == "klines":
        out = []
        for r in rows:
            o = int(r[0])
            if o > 10 ** 14:
                o //= 1000
            out.append([o] + r[1:6] + [o + D - 1] + r[7:12])
        return out
    return [(int(r[0]), float(r[-1])) for r in rows]  # calc_time, last_funding_rate


def build_universe(provider, market):
    ei = {i.symbol: i for i in provider.instruments() if i.quote == "USDT"}
    arch = [s for s in s3_list(ARCH_PREFIX[market]) if s.endswith("USDT")]
    uni = {}
    for s, i in ei.items():
        uni[s] = {"base": i.base, "asset_id": i.asset_id, "status_now": i.status, "source": "exchangeInfo",
                  "contract_type": i.contract_type, "underlying_type": i.underlying_type}
    for s in arch:
        if s not in uni:
            base = s[:-4]
            uni[s] = {"base": base, "asset_id": cp.asset_identity(base)[0], "status_now": "ABSENT", "source": "archive",
                      "contract_type": None, "underlying_type": None}
    return uni


def cmd_fetch(a, ini):
    folder = os.path.join(ini.get("data", "dir"), a.market)
    os.makedirs(os.path.join(folder, "klines"), exist_ok=True)
    os.makedirs(os.path.join(folder, "funding"), exist_ok=True)
    cfg = cscan.Config()
    cfg.set("provider", "market", a.market)
    cfg.set("network", "weight_safety", ini.get("data", "weight_safety"))
    provider = cscan.build_provider(cfg)
    end_ms = cscan.parse_now_utc(ini.get("periods", "data_end"))
    upath = os.path.join(folder, "universe.json")
    if os.path.exists(upath):
        with open(upath) as fh:
            uni = json.load(fh)
    else:
        uni = build_universe(provider, a.market)
        with open(upath, "w") as fh:
            json.dump(uni, fh, indent=0, sort_keys=True)
    print("universe: %d USDT symbols (%d archive-only)" % (len(uni), sum(1 for v in uni.values() if v["source"] == "archive")),
          file=sys.stderr)

    def one(sym):
        p = os.path.join(folder, "klines", sym + ".json")
        if os.path.exists(p):
            return sym, "cached"
        try:
            rows = provider.klines(sym, "1d", 4000, end_ms=end_ms - 1)
            how = "api"
        except cp.HttpError as e:
            if e.code != -1121:
                raise
            rows, how = archive_rows(a.market, sym), "archive"
        with open(p, "w") as fh:
            json.dump(rows, fh, separators=(",", ":"))
        return sym, "%s %d" % (how, len(rows))

    errors = {}
    with ThreadPoolExecutor(max_workers=int(ini.get("data", "max_workers"))) as ex:
        futs = [ex.submit(one, s) for s in sorted(uni)]
        for k, f in enumerate(as_completed(futs)):
            try:
                f.result()
            except Exception as e:
                errors[str(k)] = "%s: %s" % (type(e).__name__, e)
            if k % 100 == 0:
                print("  klines %d/%d" % (k, len(futs)), file=sys.stderr)
    if a.market == "usdm":
        for k, sym in enumerate(sorted(uni)):
            p = os.path.join(folder, "funding", sym + ".json")
            if os.path.exists(p):
                continue
            try:
                rates = funding_history(provider, sym, end_ms)
            except cp.HttpError as e:
                if e.code != -1121:
                    errors[sym + " funding"] = str(e)
                    continue
                rates = archive_rows("usdm", sym, "funding")
            with open(p, "w") as fh:
                json.dump(rates, fh, separators=(",", ":"))
            if k % 100 == 0:
                print("  funding %d/%d" % (k, len(uni)), file=sys.stderr)
    print(json.dumps({"errors": errors}, indent=1))
    return 0 if not errors else 1


def funding_history(provider, sym, end_ms):
    """GET /fapi/v1/fundingRate forwards from 0. Its own limit (500 requests / 5 min / IP): paced."""
    out, start = [], 0
    while True:
        time.sleep(0.7)
        rows = provider.client.get("/fapi/v1/fundingRate", {"symbol": sym, "startTime": start, "endTime": end_ms,
                                                            "limit": 1000}, weight=1)
        out += [(int(r["fundingTime"]), float(r["fundingRate"])) for r in rows]
        if len(rows) < 1000:
            return out
        start = int(rows[-1]["fundingTime"]) + 1


# ---------------------------------------------------------------- evaluation

def day(ms):
    return ms // D


def iso_day(d):
    return (datetime.datetime(1970, 1, 1) + datetime.timedelta(days=d)).strftime("%Y-%m-%d")


def parse_day(text):
    return day(cscan.parse_now_utc(text + "T00:00Z"))


def is_leveraged(base, bases):
    m = re.match(r"^(.+?)(UP|DOWN|BULL|BEAR)$", base)
    return bool(m) and m.group(1) in bases


def symbol_rows(sym, meta, candles, funding, market, cost, exclude, end_day, lev, cfg):
    """-> list of eligible (symbol, day) records. Each: dict with day, signal, fwd variants."""
    out = []
    if not candles:
        return out
    if market == "usdm" and ("_" in sym or (meta.get("underlying_type") in NON_COIN)):
        return out
    if meta["asset_id"].upper() in exclude:
        return out
    by_day = {day(c.open_ms): c for c in candles}
    pos = {c.open_ms: k for k, c in enumerate(candles)}
    last_day = day(candles[-1].open_ms)
    delisted = last_day < end_day - 2
    params = {"breakout_close_high": {"lookback": 20}}
    # DEVIATION 1 (reports/BREAKOUT_HYPOTHESIS_VERDICT.md): a price discontinuity is an open more than 2x away
    # from the previous close (with or without missing days between them). On a 24/7 market open == previous
    # close; 18 of 859,263 adjacent Spot pairs break this, all unadjusted leveraged-token consolidations
    # (~x160,000). Rows whose window or holding period crosses one are excluded from events and benchmark.
    breaks = [k for k in range(1, len(candles))
              if abs(math.log(candles[k].open / candles[k - 1].close)) > DISCONTINUITY]
    fund_t = [t for t, _ in funding] if funding else []
    fund_r = [r for _, r in funding] if funding else []
    for i in range(249, len(candles)):
        c = candles[i]
        t = day(c.open_ms)
        if c.quote_volume < 2000000 or c.trades < 2000:
            continue
        win = candles[i - 249:i + 1]
        if cscan.quality_gate(win, None, "1d", c.open_ms + D, 250, cfg) is not None:
            continue
        if t + 5 > end_day - 1 and not delisted:
            continue  # the exit candle t+5 is not closed yet at data end
        sig = bool(cs.bar_setups(win, ["breakout_close_high"], params))
        entry = by_day.get(t + 1)
        late_entry = None
        if entry is None:
            late_entry = by_day.get(t + 2) or by_day.get(t + 3)
        ex = None
        for k in range(5, 0, -1):
            ex = by_day.get(t + k)
            if ex is not None:
                break
        rec = {"sym": sym, "asset": meta["asset_id"], "day": t, "signal": sig, "lev": lev,
               "stable_like": _stable_like(win), "missing_entry": entry is None}
        e = entry or late_entry
        if e is None or ex is None or ex.open_ms < e.open_ms:
            rec["net"] = None
            out.append(rec)
            continue
        k_exit = pos[ex.open_ms]  # exact index of the exit candle
        if any(i - 248 <= b <= k_exit for b in breaks):  # a jump between two candles of the window or the hold
            rec["net"] = None
            rec["discontinuity"] = True
            out.append(rec)
            continue
        affected = ex.open_ms // D < t + 5
        rec["delist_affected"] = affected and delisted and last_day < t + 5
        rec["gap_affected"] = affected and not rec["delist_affected"]
        f = 0.0
        if funding:
            lo = _bisect(fund_t, e.open_ms, True)
            hi = _bisect(fund_t, ex.open_ms + D - 1, False)
            f = sum(fund_r[lo:hi])
        rec["funding"] = f

        def net(px, c_):
            return px * (1 - c_) / (e.open * (1 + c_)) - 1 - f
        rec["net"] = net(ex.close, cost) if entry is not None else None
        rec["net_late_entry"] = net(ex.close, cost)
        rec["net_cost2"] = net(ex.close, 2 * cost) if entry is not None else None
        if rec["delist_affected"]:
            rec["net_d30"] = net(ex.close * 0.7, cost) if entry is not None else None
            rec["net_d100"] = -1.0 if entry is not None else None
        else:
            rec["net_d30"] = rec["net_d100"] = rec["net"]
        out.append(rec)
    return out


def _bisect(xs, v, strict_right):
    lo, hi = 0, len(xs)
    while lo < hi:
        m = (lo + hi) // 2
        if xs[m] < v or (strict_right and xs[m] == v):
            lo = m + 1
        else:
            hi = m
    return lo


def _stable_like(win):
    cl = [c.close for c in win[-30:]]
    return 0.97 <= cs.median(cl) <= 1.03 and (max(cl) - min(cl)) / cs.median(cl) < 0.05


def apply_overlap(rows, allow_overlap=False):
    """Mark which signals are trades: after a trade on day t the symbol re-enters only from t+5."""
    nxt = {}
    for r in sorted((r for r in rows if r["signal"]), key=lambda r: (r["sym"], r["day"])):
        ok = allow_overlap or r["day"] >= nxt.get(r["sym"], -1)
        r["trade"] = ok
        if ok and r.get("net") is not None:
            nxt[r["sym"]] = r["day"] + 5


def stats(rows, key, lo_day, hi_day, ini, keep=lambda r: True, bench_keep=None, btc=False, top_trim=0.0,
          intervals=(0.95, 0.90)):
    bench_keep = bench_keep or (lambda r: True)  # pre-registered benchmark: every eligible market of the day
    bench = {}
    btc_net = {}
    for r in rows:
        if lo_day <= r["day"] <= hi_day and r.get(key) is not None and bench_keep(r):
            b = bench.setdefault(r["day"], [0.0, 0])
            b[0] += r[key]
            b[1] += 1
            if r["asset"] == "BTC":
                btc_net[r["day"]] = r[key]
    ev = []
    for r in rows:
        if not (r.get("trade") and lo_day <= r["day"] <= hi_day and r.get(key) is not None and keep(r)):
            continue
        if btc:
            if r["day"] not in btc_net:
                continue
            x = r[key] - btc_net[r["day"]]
        else:
            b = bench[r["day"]]
            x = r[key] - b[0] / b[1]
        ev.append((r["day"], x, r[key], r["asset"]))
    if top_trim:
        ev.sort(key=lambda e: e[1])
        ev = ev[:len(ev) - int(math.ceil(top_trim * len(ev)))]
    if not ev:
        return {"n": 0}
    xs = [e[1] for e in ev]
    nets = [e[2] for e in ev]
    by_day = {}
    for d, x, _, _ in ev:
        s = by_day.setdefault(d, [0.0, 0])
        s[0] += x
        s[1] += 1
    res = {"n": len(ev), "days_with_events": len(by_day), "assets": len({e[3] for e in ev}),
           "mean_excess_pct": _p(sum(xs) / len(xs)), "median_excess_pct": _p(cs.median(xs)),
           "mean_net_pct": _p(sum(nets) / len(nets)), "median_net_pct": _p(cs.median(nets)),
           "hit_rate_net": round(sum(1 for x in nets if x > 0) / float(len(nets)), 3),
           "beat_benchmark_rate": round(sum(1 for x in xs if x > 0) / float(len(xs)), 3)}
    for lvl in intervals:
        lo, hi = block_ci(by_day, lo_day, hi_day, ini, lvl)
        res["ci%d_pct" % int(lvl * 100)] = [_p(lo), _p(hi)]
    res["newey_west_t"] = newey_west_t([s / n for _, (s, n) in sorted(by_day.items())],
                                       int(ini.get("statistics", "newey_west_lag")))
    contrib = {}
    for e in ev:
        contrib[e[3]] = contrib.get(e[3], 0.0) + e[1]
    tot = sum(contrib.values())
    top = sorted(contrib.items(), key=lambda kv: -kv[1])
    res["top_assets_excess_share"] = [(k, round(v / tot, 3) if tot > 0 else None, round(v * 100, 1)) for k, v in top[:8]]
    res["max_asset_share"] = round(top[0][1] / tot, 3) if tot > 0 else None
    loo = []
    for k, v in contrib.items():
        n_k = sum(1 for e in ev if e[3] == k)
        if len(ev) - n_k:
            loo.append(((sum(xs) - v) / (len(ev) - n_k), k))
    res["leave_one_asset_out_min_pct"] = [_p(min(loo)[0]), min(loo)[1]] if loo else None
    return res


def block_ci(by_day, lo_day, hi_day, ini, level):
    days = list(range(lo_day, hi_day + 1))
    s = [by_day.get(d, (0.0, 0))[0] for d in days]
    n = [by_day.get(d, (0.0, 0))[1] for d in days]
    L = int(ini.get("statistics", "block_days"))
    reps = int(ini.get("statistics", "replicates"))
    rng = random.Random(int(ini.get("statistics", "seed")))
    N = len(days)
    if N < L or sum(n) < 2:
        return None, None
    cs_, cn = [0.0], [0]
    for a_, b_ in zip(s, n):
        cs_.append(cs_[-1] + a_)
        cn.append(cn[-1] + b_)
    bs = [cs_[i + L] - cs_[i] for i in range(N - L + 1)]
    bn = [cn[i + L] - cn[i] for i in range(N - L + 1)]
    k = int(math.ceil(N / float(L)))
    means = []
    for _ in range(reps):
        ss = nn = 0
        for _ in range(k):
            i = rng.randrange(0, N - L + 1)
            ss += bs[i]
            nn += bn[i]
        if nn:
            means.append(ss / nn)
    means.sort()
    a = (1 - level) / 2
    return means[int(a * len(means))], means[int((1 - a) * len(means)) - 1]


def newey_west_t(x, lag):
    n = len(x)
    if n < lag + 2:
        return None
    m = sum(x) / n
    e = [v - m for v in x]
    g0 = sum(v * v for v in e) / n
    var = g0
    for l in range(1, lag + 1):
        g = sum(e[i] * e[i - l] for i in range(l, n)) / n
        var += 2 * (1 - l / (lag + 1.0)) * g
    return round(m / math.sqrt(var / n), 2) if var > 0 else None


def _p(x):
    return None if x is None else round(x * 100, 3)


def load_market(folder, market, ini, cfg):
    with open(os.path.join(folder, "universe.json")) as fh:
        uni = json.load(fh)
    end_day = day(cscan.parse_now_utc(ini.get("periods", "data_end")))
    end_ms = end_day * D
    exclude = {b.upper() for b in cfg.items("universe", "exclude_bases")}
    bases = {v["base"] for v in uni.values()}
    cost = float(ini.get("costs", "%s_per_side" % market))
    rows, coverage = [], {"symbols": len(uni), "with_candles": 0, "archive_only": 0, "delisted": 0,
                          "empty": 0, "funding_missing": 0}
    for sym in sorted(uni):
        p = os.path.join(folder, "klines", sym + ".json")
        if not os.path.exists(p):
            coverage["empty"] += 1
            continue
        with open(p) as fh:
            raw = json.load(fh)
        candles, _ = cp.normalize_klines(raw, "1d", end_ms)
        if not candles:
            coverage["empty"] += 1
            continue
        coverage["with_candles"] += 1
        coverage["archive_only"] += uni[sym]["source"] == "archive"
        coverage["delisted"] += day(candles[-1].open_ms) < end_day - 2
        funding = None
        if market == "usdm":
            fp = os.path.join(folder, "funding", sym + ".json")
            if os.path.exists(fp):
                with open(fp) as fh:
                    funding = sorted((int(t), float(r)) for t, r in json.load(fh))
            else:
                coverage["funding_missing"] += 1
                funding = []
        rows += symbol_rows(sym, uni[sym], candles, funding, market, cost, exclude, end_day,
                            is_leveraged(uni[sym]["base"], bases), cfg)
    return rows, coverage


def cmd_run(a, ini):
    cfg = cscan.Config()
    folder = os.path.join(ini.get("data", "dir"), a.market)
    rows, coverage = load_market(folder, a.market, ini, cfg)
    if a.period == "holdout":
        lo = parse_day(ini.get("periods", "holdout_%s_start" % a.market))
        hi = parse_day(ini.get("periods", "holdout_end"))
    else:
        lo = parse_day(ini.get("periods", "development_start"))
        hi = parse_day(ini.get("periods", "development_end"))
    apply_overlap(rows)
    sig = [r for r in rows if r["signal"] and lo <= r["day"] <= hi]
    out = {"market": a.market, "period": a.period, "signal_days": [iso_day(lo), iso_day(hi)], "coverage": coverage,
           "eligible_symbol_days": sum(1 for r in rows if lo <= r["day"] <= hi),
           "eligible_symbols": len({r["sym"] for r in rows if lo <= r["day"] <= hi}),
           "signals": len(sig), "trades": sum(1 for r in sig if r.get("trade")),
           "missing_entry": sum(1 for r in sig if r["missing_entry"]),
           "discontinuity_excluded_symbol_days": sum(1 for r in rows if lo <= r["day"] <= hi and r.get("discontinuity")),
           "discontinuity_excluded_signals": sum(1 for r in sig if r.get("discontinuity")),
           "delist_affected_trades": sum(1 for r in sig if r.get("trade") and r.get("delist_affected")),
           "gap_affected_trades": sum(1 for r in sig if r.get("trade") and r.get("gap_affected"))}
    not16 = lambda r: r["asset"] not in ORIGINAL_16
    only16 = lambda r: r["asset"] in ORIGINAL_16
    top = float(ini.get("statistics", "top_trim"))
    res = {
        "primary": stats(rows, "net", lo, hi, ini),
        "S2_excluding_original_16": stats(rows, "net", lo, hi, ini, keep=not16),
        "S3_delisting_total_loss": stats(rows, "net_d100", lo, hi, ini),
        "S4_without_top_1pct": stats(rows, "net", lo, hi, ini, top_trim=top),
        "delisting_minus_30pct": stats(rows, "net_d30", lo, hi, ini),
        "costs_doubled": stats(rows, "net_cost2", lo, hi, ini),
        "late_entry_counted": stats(rows, "net_late_entry", lo, hi, ini),
        "leveraged_tokens_excluded": stats(rows, "net", lo, hi, ini, keep=lambda r: not r["lev"],
                                           bench_keep=lambda r: not r["lev"]),
        "stable_like_excluded": stats(rows, "net", lo, hi, ini, keep=lambda r: not r["stable_like"],
                                      bench_keep=lambda r: not r["stable_like"]),
        "btc_adjusted": stats(rows, "net", lo, hi, ini, btc=True),
        "original_16_only_pit_benchmark": stats(rows, "net", lo, hi, ini, keep=only16),
        "original_16_only_own_benchmark": stats(rows, "net", lo, hi, ini, keep=only16, bench_keep=only16),
    }
    nov = [dict(r) for r in rows]
    apply_overlap(nov, allow_overlap=True)
    res["overlap_allowed"] = stats(nov, "net", lo, hi, ini)
    years = {}
    for y in range(int(iso_day(lo)[:4]), int(iso_day(hi)[:4]) + 1):
        ylo, yhi = max(lo, parse_day("%d-01-01" % y)), min(hi, parse_day("%d-12-31" % y))
        years[str(y)] = stats(rows, "net", ylo, yhi, ini, intervals=(0.90,))
    res["by_year"] = {y: {k: v.get(k) for k in ("n", "mean_excess_pct", "median_excess_pct", "ci90_pct",
                                                    "mean_net_pct", "beat_benchmark_rate")} for y, v in years.items()}
    out["results"] = res
    p = res["primary"]
    s2, s3, s4 = res["S2_excluding_original_16"], res["S3_delisting_total_loss"], res["S4_without_top_1pct"]
    checks = {
        "S1_ci95_above_0": bool(p.get("ci95_pct") and p["ci95_pct"][0] is not None and p["ci95_pct"][0] > 0),
        "S2_excl16_mean_and_ci90_above_0": bool(s2.get("n") and s2["mean_excess_pct"] > 0 and s2["ci90_pct"][0] > 0),
        "S3_delist_total_loss_mean_above_0": bool(s3.get("n") and s3["mean_excess_pct"] > 0),
        "S4_trimmed_mean_above_0": bool(s4.get("n") and s4["mean_excess_pct"] > 0),
        "S5_max_asset_share_le_25pct": bool(p.get("max_asset_share") is not None
                                            and p["max_asset_share"] <= float(ini.get("statistics", "max_asset_share"))),
    }
    out["criteria"] = checks
    if not p.get("n") or p["mean_excess_pct"] <= 0:
        out["verdict"] = "NOT SUPPORTED"
    elif all(checks.values()):
        out["verdict"] = "STATISTICALLY SUPPORTED"
    else:
        out["verdict"] = "PROMISING, NOT SUPPORTED"
    if a.period == "development":
        out["verdict_note"] = "development period: contaminated; reported for pipeline checks, not for the verdict"
    text = json.dumps(out, indent=1)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text)
    print(text)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Point-in-time test of the daily closing-high breakout.")
    ap.add_argument("cmd", choices=["fetch", "run"])
    ap.add_argument("--config", default=os.path.join(HERE, "breakout_pit.ini"))
    ap.add_argument("--market", choices=["spot", "usdm"], required=True)
    ap.add_argument("--period", choices=["development", "holdout"])
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    try:
        ini, _ = load_config(a.config)
        if a.cmd == "fetch":
            return cmd_fetch(a, ini)
        if not a.period:
            raise PitError("run wants --period development or holdout")
        return cmd_run(a, ini)
    except PitError as e:
        print("crypto_pit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
