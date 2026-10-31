#!/usr/bin/env python3
"""Walk-forward check of the crypto setups, net of costs. Standard library only. Research tool.

    python3 crypto_eval.py events  --config trader/binance.ini --end 2026-10-31T00:00Z --out eval.json
    python3 crypto_eval.py profile --config trader/binance.ini --end 2026-10-31T00:00Z

events   For every symbol of the INI allowlist and every timeframe, walk the closed candles
         forward. At each bar t the scanner's own rules run on the same trailing window the
         live scan reads (candles t-W+1..t, W = [timeframes] bars). A matched bullish rule is
         a long entry at the NEXT bar's open (t+1), closed at the close of bar t+H.
         Net return = exit / entry - 1 - 2 x cost_per_side.
         Overlap: after a signal, the same rule on the same symbol is ignored for H bars.
         Baseline: the same trade taken at every bar of every symbol. Market-adjusted excess =
         a signal's net return minus the cross-sectional mean net return of all symbols
         entered on the same bar (crypto moves together; this removes the common move).
         Uncertainty: a moving-block bootstrap over entry dates (block = max(H, 10) bars) of
         the per-date mean excess. Stability: the excess in 4 chronological folds.
         Nothing is fitted or tuned here: the parameters are the INI's. The folds are an
         out-of-time stability check of fixed rules, not a train/test split.
profile  UTC hour-of-day profile of 1h volume share and mean absolute return: is there a
         'session open' in 24/7 crypto that would justify an anchored opening range?

--end pins the last candle (so a rerun on another day sees the same history). Candles are
fetched live from public endpoints and cached under [cache] dir/eval.
"""

import argparse
import json
import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import crypto_providers as cp  # noqa: E402
import crypto_scan as cscan  # noqa: E402
import crypto_setups as cs  # noqa: E402

HORIZONS = {"1d": (1, 5, 10), "1h": (4, 12, 24)}
HISTORY = {"1d": 1500, "1h": 8760}
COST = {"spot": 0.0015, "usdm": 0.0010}   # per side: taker fee (0.10 % spot, 0.05 % futures) + 5 bps slippage


def history(provider, symbol, tf, bars, end_ms, cache_dir):
    path = os.path.join(cache_dir, "%s_%s_%s_%d_%d.json" % (provider.market, symbol, tf, bars, end_ms))
    if os.path.exists(path):
        with open(path) as fh:
            rows = json.load(fh)
    else:
        rows = provider.klines(symbol, tf, bars + 1, end_ms=end_ms)
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w") as fh:
            json.dump(rows, fh)
    return cp.normalize_klines(rows, tf, end_ms)[0]


def walk(candles, tf, enabled, params, window, horizons, cost):
    """-> (events [(setup, t, open_ms, {H: net})], baseline {open_ms: {H: net}})."""
    events, base, last = [], {}, {}
    step = cp.INTERVAL_MS[tf]
    n = len(candles)
    hmax = max(horizons)
    for t in range(window - 1, n - 1):
        win = candles[t - window + 1:t + 1]
        if (win[-1].open_ms - win[0].open_ms) // step + 1 != len(win):
            continue  # a gap inside the window: the rules index by position, skip
        entry = candles[t + 1].open
        fwd = {H: candles[t + H].close / entry - 1 - 2 * cost for H in horizons if t + H < n}
        if not fwd:
            continue
        key = candles[t + 1].open_ms
        base[key] = fwd
        for h in cs.bar_setups(win, enabled, params):
            if h["direction"] != "bullish" and h["id"] != "rsi_oversold":
                continue
            if t - last.get(h["id"], -10 ** 9) < hmax:
                continue
            last[h["id"]] = t
            events.append((h["id"], t, key, fwd))
    return events, base


def block_bootstrap_mean(series, block, reps=2000, seed=7):
    """series: per-date values in time order -> (5 %, 95 %) of the mean."""
    n = len(series)
    if n < 2:
        return None, None
    rng = random.Random(seed)
    block = max(1, min(block, n))
    means = []
    for _ in range(reps):
        s = []
        while len(s) < n:
            i = rng.randrange(0, n - block + 1)
            s.extend(series[i:i + block])
        means.append(sum(s[:n]) / n)
    means.sort()
    return means[int(0.05 * reps)], means[int(0.95 * reps)]


def summarize(all_events, bases, horizons, tf):
    # cross-sectional mean net per entry time and horizon
    xs = {}
    for b in bases.values():
        for k, fwd in b.items():
            for H, r in fwd.items():
                xs.setdefault((k, H), []).append(r)
    xmean = {k: sum(v) / len(v) for k, v in xs.items()}
    out = {}
    base_all = {H: [r for b in bases.values() for fwd in b.values() for HH, r in fwd.items() if HH == H]
                for H in horizons}
    for H in horizons:
        out["baseline_H%d" % H] = {"n": len(base_all[H]), "mean_net_pct": _p(_mean(base_all[H])),
                                   "hit_rate": _r(_frac(base_all[H]))}
    setups = sorted({e[0] for evs in all_events.values() for e in evs})
    for sid in setups:
        for H in horizons:
            rows = [(e[2], e[3][H]) for evs in all_events.values() for e in evs if e[0] == sid and H in e[3]]
            if not rows:
                continue
            rows.sort()
            nets = [r for _, r in rows]
            excess = [r - xmean[(k, H)] for k, r in rows]
            by_date = {}
            for (k, _), x in zip(rows, excess):
                by_date.setdefault(k, []).append(x)
            series = [sum(v) / len(v) for _, v in sorted(by_date.items())]
            lo, hi = block_bootstrap_mean(series, max(H, 10))
            folds = []
            m = len(rows)
            for f in range(4):
                part = excess[f * m // 4:(f + 1) * m // 4]
                folds.append(_p(_mean(part)) if part else None)
            out["%s_H%d" % (sid, H)] = {
                "n": m, "dates": len(series), "mean_net_pct": _p(_mean(nets)), "median_net_pct": _p(_median(nets)),
                "hit_rate": _r(_frac(nets)), "mean_excess_pct": _p(_mean(excess)),
                "excess_ci90_pct": [_p(lo), _p(hi)], "fold_excess_pct": folds,
                "folds_positive": sum(1 for x in folds if x is not None and x > 0),
                "first": cp._iso(rows[0][0]), "last": cp._iso(rows[-1][0])}
    return out


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _median(xs):
    return cs.median(xs) if xs else None


def _frac(xs):
    return sum(1 for x in xs if x > 0) / float(len(xs)) if xs else None


def _p(x):
    return None if x is None else round(x * 100, 3)


def _r(x):
    return None if x is None else round(x, 3)


def resolve(provider, cfg):
    instruments = provider.instruments()
    sel, ex = cscan.resolve_universe(instruments, {}, cfg, provider.market)
    return [i for _, i in sel if i.status == "TRADING"], ex


def cmd_events(a, cfg, provider, end_ms):
    enabled = cfg.items("setups", "enabled")
    cost = COST[provider.market] if a.cost is None else a.cost
    insts, ex = resolve(provider, cfg)
    report = {"market": provider.market, "end": cp._iso(end_ms), "cost_per_side": cost, "config": cfg.fingerprint(),
              "symbols": [i.symbol for i in insts], "not_listed": [e["requested"] for e in ex], "timeframes": {}}
    for tf in cfg.items("timeframes", "intervals"):
        params = {s: cfg.setup_params(s, tf) for s in enabled}
        window = max(cs.required_bars(enabled, params), int(cfg.num("timeframes", "bars", tf)))
        evs, bases, bars = {}, {}, {}
        for i in insts:
            c = history(provider, i.symbol, tf, HISTORY[tf] + window, end_ms, os.path.join(a.cache, "eval"))
            bars[i.symbol] = len(c)
            if len(c) < window + max(HORIZONS[tf]) + 50:
                continue
            evs[i.symbol], bases[i.symbol] = walk(c, tf, enabled, params, window, HORIZONS[tf], cost)
            print("  %s %s: %d bars, %d events" % (i.symbol, tf, len(c), len(evs[i.symbol])), file=sys.stderr)
        report["timeframes"][tf] = {"window": window, "bars": bars, "results": summarize(evs, bases, HORIZONS[tf], tf)}
    return report


def cmd_profile(a, cfg, provider, end_ms):
    insts, _ = resolve(provider, cfg)
    vol = [0.0] * 24
    absr = [[] for _ in range(24)]
    for i in insts:
        c = history(provider, i.symbol, "1h", 2160, end_ms, os.path.join(a.cache, "eval"))  # 90 days
        tot = sum(k.quote_volume for k in c) or 1
        for k in c:
            h = (k.open_ms // 3600000) % 24
            vol[h] += k.quote_volume / tot / len(insts)
            absr[h].append(abs(k.close / k.open - 1))
    return {"market": provider.market, "end": cp._iso(end_ms), "days": 90, "symbols": [i.symbol for i in insts],
            "by_utc_hour": [{"hour": h, "volume_share_pct": round(vol[h] * 100, 2),
                             "mean_abs_return_pct": round(_mean(absr[h]) * 100, 3)} for h in range(24)]}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Walk-forward check of the crypto setups (research).")
    ap.add_argument("cmd", choices=["events", "profile"])
    ap.add_argument("--config")
    ap.add_argument("--market", choices=["spot", "usdm"])
    ap.add_argument("--end", required=True, help="UTC time of the last candle, e.g. 2026-10-31T00:00Z")
    ap.add_argument("--cost", type=float, help="cost per side as a fraction (default spot 0.0015, usdm 0.0010)")
    ap.add_argument("--cache", default="trader/cache/binance")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    cfg = cscan.Config(a.config)
    if a.market:
        cfg.set("provider", "market", a.market)
    provider = cscan.build_provider(cfg)
    end_ms = cscan.parse_now_utc(a.end)
    t0 = time.time()
    doc = (cmd_events if a.cmd == "events" else cmd_profile)(a, cfg, provider, end_ms)
    doc["seconds"] = round(time.time() - t0, 1)
    text = json.dumps(doc, indent=1)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
