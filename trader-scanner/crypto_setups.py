"""Crypto setups for trader-scanner, on CLOSED OHLCV candles. Standard library only.

Every rule reads candles[0..-1], where candles[-1] is the newest CLOSED candle at the
scan clock. Nothing reads a later candle, so a replay with --now cannot see the future.

Origin of each rule, so no name is silently redefined:

  ported (the equity daily scan's math, counted in BARS of the chosen timeframe):
    breakout_close_high  equity: breakout_20d  (20 sessions there; here n bars: 20 calendar
                         days on 1d, 20 hours on 1h)
    bull_flag            equity: bull_flag     (same shape; thresholds configurable per timeframe)
    reclaim_ma           equity: reclaim_ma20
    rsi_oversold         equity: oversold      (RSI 14, Wilder, the scanner's own rsi())
    oversold_bounce      equity: oversold_bounce
  new for crypto (the equity scanner has no such setup):
    rvol_expansion       the last bar's QUOTE volume against the median of the bars before it
    momentum_accel       return over the last k bars beats the return of the k bars before
  experimental (off by default, see the SKILL.md):
    utc_orb              a range anchored at a UTC clock time, then a close above its high

A match is a description of the chart, not a forecast. None of these rules has a
validated edge on crypto; the validation report says what was measured.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from scan import rsi as wilder_rsi  # noqa: E402  the equity scanner's RSI, not a second copy

DEFAULT_PARAMS = {
    "breakout_close_high": {"lookback": 20},
    "bull_flag": {"impulse_bars": 10, "impulse_pct": 10.0, "flag_bars": 5, "flag_range_pct": 5.0},
    "reclaim_ma": {"period": 20},
    "rsi_oversold": {"period": 14, "threshold": 30.0},
    "oversold_bounce": {"period": 14, "threshold": 30.0},
    "rvol_expansion": {"lookback": 20, "min_rvol": 2.0},
    "momentum_accel": {"bars": 5, "min_delta_pct": 2.0},
    "utc_orb": {"anchor": "00:00", "range_minutes": 60, "expire_minutes": 480},
}
ORIGIN = {"breakout_close_high": "ported:breakout_20d", "bull_flag": "ported:bull_flag",
          "reclaim_ma": "ported:reclaim_ma20", "rsi_oversold": "ported:oversold",
          "oversold_bounce": "ported:oversold_bounce", "rvol_expansion": "new", "momentum_accel": "new",
          "utc_orb": "experimental"}
# bullish = a rule whose trigger is an up move; condition = a state worth a look, no trigger
DIRECTION = {"breakout_close_high": "bullish", "bull_flag": "bullish", "reclaim_ma": "bullish",
             "rsi_oversold": "condition", "oversold_bounce": "bullish", "rvol_expansion": None,
             "momentum_accel": "bullish", "utc_orb": "bullish"}
BAR_SETUPS = ["breakout_close_high", "bull_flag", "reclaim_ma", "rsi_oversold", "oversold_bounce",
              "rvol_expansion", "momentum_accel"]


LABELS = {
    "breakout_close_high": ("close above the highest close of the prior %d bars", ("lookback",)),
    "bull_flag": ("up >%g%% over %d bars, then %d closes inside %g%%",
                  ("impulse_pct", "impulse_bars", "flag_bars", "flag_range_pct")),
    "reclaim_ma": ("closed back above the %d-bar average", ("period",)),
    "rsi_oversold": ("RSI %d under %g", ("period", "threshold")),
    "oversold_bounce": ("RSI %d under %g on the bar before, then an up close", ("period", "threshold")),
    "rvol_expansion": ("quote volume >= %gx the %d-bar median", ("min_rvol", "lookback")),
    "momentum_accel": ("%d-bar return positive and %g pts above the %d bars before", ("bars", "min_delta_pct", "bars")),
    "utc_orb": ("EXPERIMENTAL: close above the %d-minute range from %s UTC", ("range_minutes", "anchor")),
}


def label(sid, p):
    text, keys = LABELS[sid]
    return text % tuple(p[k] for k in keys)


def required_bars(enabled, params):
    """Closed bars a timeframe needs for every enabled bar rule (plus RSI warm-up)."""
    need = 30
    for sid in enabled:
        p = params.get(sid, DEFAULT_PARAMS.get(sid, {}))
        if sid == "breakout_close_high":
            need = max(need, p["lookback"] + 1)
        elif sid == "bull_flag":
            need = max(need, p["impulse_bars"] + p["flag_bars"] + 1)
        elif sid == "reclaim_ma":
            need = max(need, p["period"] + 1)
        elif sid in ("rsi_oversold", "oversold_bounce"):
            need = max(need, p["period"] * 2 + 2)
        elif sid == "rvol_expansion":
            need = max(need, p["lookback"] + 1)
        elif sid == "momentum_accel":
            need = max(need, 2 * p["bars"] + 1)
    return need


# ---------------------------------------------------------------- indicators

def sma(xs, n):
    return sum(xs[-n:]) / float(n) if len(xs) >= n else None


def atr(candles, n=14):
    """Wilder's ATR on closed candles."""
    if len(candles) < n + 1:
        return None
    tr = [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)) for p, c in zip(candles, candles[1:])]
    a = sum(tr[:n]) / n
    for x in tr[n:]:
        a = (a * (n - 1) + x) / n
    return a


def median(xs):
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def indicators(candles):
    c = [k.close for k in candles]
    r = wilder_rsi(c, 14)
    a = atr(candles, 14)
    prior_qv = [k.quote_volume for k in candles[-21:-1]]
    med = median(prior_qv) if len(prior_qv) == 20 else None
    return {"close": c[-1], "rsi14": _r(r, 2), "ma20": _r(sma(c, 20), 8), "atr14": _r(a, 8),
            "atr14_pct": _r(a / c[-1] * 100 if a else None, 3),
            "rvol20": _r(candles[-1].quote_volume / med if med else None, 3),
            "quote_volume_last_bar": round(candles[-1].quote_volume, 2)}


def _r(x, n):
    return None if x is None else round(x, n)


# ---------------------------------------------------------------- rules

def bar_setups(candles, enabled, params):
    """candles: closed, oldest first, contiguous. -> list of {id, ..., evidence}."""
    c = [k.close for k in candles]
    out = []

    def hit(sid, evidence, direction=None):
        p = params[sid]
        out.append({"id": sid, "label": label(sid, p), "origin": ORIGIN[sid],
                    "direction": direction or DIRECTION[sid], "params": dict(p), "evidence": evidence})

    for sid in enabled:
        p = params[sid]
        if sid == "breakout_close_high":
            n = p["lookback"]
            hi = max(c[-n - 1:-1])
            if c[-1] > hi:
                hit(sid, {"close": c[-1], "prior_close_high": hi, "pct_over": _r((c[-1] / hi - 1) * 100, 3)})
        elif sid == "bull_flag":
            fb, ib = p["flag_bars"], p["impulse_bars"]
            run = c[-fb - 1] / c[-fb - 1 - ib] - 1
            rng = (max(c[-fb:]) - min(c[-fb:])) / c[-1]
            if run * 100 > p["impulse_pct"] and rng * 100 < p["flag_range_pct"]:
                hit(sid, {"impulse_pct": _r(run * 100, 3), "flag_range_pct": _r(rng * 100, 3)})
        elif sid == "reclaim_ma":
            n = p["period"]
            ma, ma_prev = sma(c, n), sma(c[:-1], n)
            if c[-2] < ma_prev and c[-1] > ma:
                hit(sid, {"close": c[-1], "ma": _r(ma, 8), "prev_close": c[-2], "prev_ma": _r(ma_prev, 8)})
        elif sid == "rsi_oversold":
            r = wilder_rsi(c, p["period"])
            if r is not None and r < p["threshold"]:
                hit(sid, {"rsi": _r(r, 2)})
        elif sid == "oversold_bounce":
            rp = wilder_rsi(c[:-1], p["period"])
            if rp is not None and rp < p["threshold"] and c[-1] > c[-2]:
                hit(sid, {"rsi_prev": _r(rp, 2), "close": c[-1], "prev_close": c[-2]})
        elif sid == "rvol_expansion":
            prior = [k.quote_volume for k in candles[-p["lookback"] - 1:-1]]
            med = median(prior)
            if med > 0 and candles[-1].quote_volume / med >= p["min_rvol"]:
                up = candles[-1].close > candles[-1].open
                hit(sid, {"rvol": _r(candles[-1].quote_volume / med, 3), "bar_change_pct":
                          _r((candles[-1].close / candles[-1].open - 1) * 100, 3)},
                    "bullish" if up else "bearish")
        elif sid == "momentum_accel":
            k = p["bars"]
            r1 = c[-1] / c[-1 - k] - 1
            r0 = c[-1 - k] / c[-1 - 2 * k] - 1
            if r1 > 0 and (r1 - r0) * 100 >= p["min_delta_pct"]:
                hit(sid, {"ret_last_pct": _r(r1 * 100, 3), "ret_prev_pct": _r(r0 * 100, 3)})
    return out


def utc_orb(candles, interval_ms, now_ms, p):
    """EXPERIMENTAL. Crypto has no opening bell; this anchors a 'session' at a UTC clock time
    (00:00 by default, the daily-candle open). Range = closed bars in [anchor, anchor+range);
    a hit = the first later closed bar to CLOSE above the range high, within expire_minutes.
    -> (hit dict or None, status text)."""
    hh, mm = [int(x) for x in str(p["anchor"]).split(":")]
    day_ms = 86400000
    anchor = (now_ms // day_ms) * day_ms + (hh * 60 + mm) * 60000
    if anchor > now_ms:
        anchor -= day_ms
    rng_end = anchor + p["range_minutes"] * 60000
    if rng_end + interval_ms > now_ms:
        return None, "range not complete"
    by = {k.open_ms: k for k in candles}
    rng = [by.get(t) for t in range(anchor, rng_end, interval_ms)]
    if any(k is None for k in rng):
        return None, "range has a missing bar"
    hi, lo = max(k.high for k in rng), min(k.low for k in rng)
    t = rng_end
    while t + interval_ms <= now_ms and t < anchor + day_ms:
        k = by.get(t)
        if k is None:
            return None, "missing bar after the range"
        if k.close > hi:
            age = (now_ms - (t + interval_ms)) / 60000.0
            if age > p["expire_minutes"]:
                return None, "breakout expired"
            return {"id": "utc_orb", "label": label("utc_orb", p), "origin": ORIGIN["utc_orb"],
                    "direction": "bullish", "params": dict(p),
                    "evidence": {"range_high": hi, "range_low": lo, "close": k.close, "bar_open_ms": t,
                                 "pct_over": _r((k.close / hi - 1) * 100, 3), "minutes_since": round(age, 1)}}, "hit"
        t += interval_ms
    return None, "no close above the range"
