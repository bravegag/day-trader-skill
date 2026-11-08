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

What each rule asserts about the newest closed candle (KIND, DIRECTION):

  rule                 kind      direction    what it detects
  breakout_close_high  event     bullish      that close is the highest close of the last n+1 bars
  bull_flag            state     bullish      a >impulse_pct run, then flag_bars closes inside flag_range_pct
  reclaim_ma           event     bullish      a FRESH cross: the previous close was under the n-bar SMA of
                                              closes, this close is over it. It never carries forward: one
                                              candle later it is no longer a match, whatever the price does
  rsi_oversold         state     unspecified  RSI under the threshold: selling so far, no reversal trigger
  oversold_bounce      event     bullish      an up close after an oversold bar: a counter-trend bounce attempt
  rvol_expansion       activity  unspecified  heavy quote volume. The bar's own up/down sign is copied as
                                              evidence (bar_direction) and is never bullish or bearish credit
  momentum_accel       state     bullish      the last k-bar return is positive and min_delta_pct above the k
                                              bars before. It cannot fire while the k-bar return is negative
                                              (a decline that is merely slowing); after a decline it is a
                                              rebound (context rebound_after_decline), not an established trend
  utc_orb              event     bullish      the first close above a UTC-anchored range, up to expire_minutes
                                              old: it carries its age and whether the newest close still holds

event = triggered on that candle only; state = holds at that close over a window ending there;
activity = something happened, with no direction. There is no bearish rule: the scan looks for
bullish setups, and a timeframe with no match is "no rule matched", never bearish or neutral.
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
# The table in the module docstring explains each value. Only bullish and bearish are directional.
DIRECTION = {"breakout_close_high": "bullish", "bull_flag": "bullish", "reclaim_ma": "bullish",
             "rsi_oversold": "unspecified", "oversold_bounce": "bullish", "rvol_expansion": "unspecified",
             "momentum_accel": "bullish", "utc_orb": "bullish"}
KIND = {"breakout_close_high": "event", "bull_flag": "state", "reclaim_ma": "event", "rsi_oversold": "state",
        "oversold_bounce": "event", "rvol_expansion": "activity", "momentum_accel": "state", "utc_orb": "event"}
DIRECTIONAL = ("bullish", "bearish")
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


def summary(sid, p, e):
    """One line saying what the rule saw on this candle, in the rule's own terms (copied into the JSON)."""
    if sid == "breakout_close_high":
        return "new %d-bar closing high: close %s is %+.2f%% over %s" % (p["lookback"], _g(e["close"]), e["pct_over"],
                                                                        _g(e["prior_close_high"]))
    if sid == "bull_flag":
        return "flag shape at this close: up %.1f%% over %d bars, then %d closes within %.1f%%" % (
            e["impulse_pct"], p["impulse_bars"], p["flag_bars"], e["flag_range_pct"])
    if sid == "reclaim_ma":
        return "fresh cross on this candle: close %s over the %d-bar SMA %s (%+.2f%%); previous close %s was under %s" % (
            _g(e["close"]), p["period"], _g(e["ma"]), e["pct_above_ma"], _g(e["prev_close"]), _g(e["prev_ma"]))
    if sid == "rsi_oversold":
        return "RSI %d at %.1f, under %g: an oversold state, no reversal trigger" % (p["period"], e["rsi"], p["threshold"])
    if sid == "oversold_bounce":
        return "up close %s after RSI %d %.1f on the bar before: a counter-trend bounce attempt" % (
            _g(e["close"]), p["period"], e["rsi_prev"])
    if sid == "rvol_expansion":
        return "quote volume %.1fx the %d-bar median on a%s bar (%+.2f%%): activity, no direction" % (
            e["rvol"], p["lookback"], {"up": "n up", "down": " down", "flat": " flat"}[e["bar_direction"]],
            e["bar_change_pct"])
    if sid == "momentum_accel":
        what = ("a rebound after a decline: momentum improving, not an established trend"
                if e["context"] == "rebound_after_decline" else "upward momentum accelerating")
        return "%d-bar return %+.2f%% vs %+.2f%% the %d bars before (%s)" % (p["bars"], e["ret_last_pct"],
                                                                            e["ret_prev_pct"], p["bars"], what)
    if sid == "utc_orb":
        return "closed over the range high %s %.0f min ago; newest close %s %s" % (
            _g(e["range_high"]), e["minutes_since"], _g(e["last_close"]),
            "still over it" if e["holds_at_last_close"] else "is back under it: the breakout has failed since")
    raise KeyError(sid)


def _g(x):
    return "%.6g" % x


def timeframe_bias(hits):
    """What the matched rules of one timeframe assert: bullish, bearish, mixed (both), unspecified (only
    non-directional matches) or no_match. No match is never read as bearish or neutral."""
    dirs = {h["direction"] for h in hits}
    if "bullish" in dirs and "bearish" in dirs:
        return "mixed"
    if "bullish" in dirs or "bearish" in dirs:
        return "bullish" if "bullish" in dirs else "bearish"
    return "unspecified" if hits else "no_match"


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

    def hit(sid, evidence):
        p = params[sid]
        out.append({"id": sid, "label": label(sid, p), "origin": ORIGIN[sid], "kind": KIND[sid],
                    "direction": DIRECTION[sid], "summary": summary(sid, p, evidence), "params": dict(p),
                    "evidence": evidence})

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
                hit(sid, {"close": c[-1], "ma": _r(ma, 8), "prev_close": c[-2], "prev_ma": _r(ma_prev, 8),
                          "pct_above_ma": _r((c[-1] / ma - 1) * 100, 3)})
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
                k = candles[-1]
                hit(sid, {"rvol": _r(k.quote_volume / med, 3), "bar_change_pct": _r((k.close / k.open - 1) * 100, 3),
                          "bar_direction": "up" if k.close > k.open else "down" if k.close < k.open else "flat"})
        elif sid == "momentum_accel":
            k = p["bars"]
            r1 = c[-1] / c[-1 - k] - 1
            r0 = c[-1 - k] / c[-1 - 2 * k] - 1
            if r1 > 0 and (r1 - r0) * 100 >= p["min_delta_pct"]:
                hit(sid, {"ret_last_pct": _r(r1 * 100, 3), "ret_prev_pct": _r(r0 * 100, 3),
                          "context": "rebound_after_decline" if r0 < 0 else "acceleration"})
    return out


def utc_orb(candles, interval_ms, now_ms, p):
    """EXPERIMENTAL. Crypto has no opening bell; this anchors a 'session' at a UTC clock time
    (00:00 by default, the daily-candle open). Range = closed bars in [anchor, anchor+range);
    a hit = the first later closed bar to CLOSE above the range high, within expire_minutes.
    The hit is an event that may be several bars old: it carries minutes_since and whether the
    newest closed bar still closes above the range high (holds_at_last_close).
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
            last = max((b for b in candles if b.open_ms + interval_ms <= now_ms), key=lambda b: b.open_ms)
            e = {"range_high": hi, "range_low": lo, "close": k.close, "bar_open_ms": t,
                 "pct_over": _r((k.close / hi - 1) * 100, 3), "minutes_since": round(age, 1),
                 "last_close": last.close, "last_bar_open_ms": last.open_ms, "holds_at_last_close": last.close > hi}
            return {"id": "utc_orb", "label": label("utc_orb", p), "origin": ORIGIN["utc_orb"], "kind": KIND["utc_orb"],
                    "direction": DIRECTION["utc_orb"], "summary": summary("utc_orb", p, e), "params": dict(p),
                    "evidence": e}, "hit"
        t += interval_ms
    return None, "no close above the range"
