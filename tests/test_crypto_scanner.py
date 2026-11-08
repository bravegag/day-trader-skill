"""The crypto scan (trader-scanner/crypto_*.py). Offline: no test touches the network."""

import copy
import io
import json
import os
import random
import shutil
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

scan = load("trader-scanner", "scan")
cscan = load("trader-scanner", "crypto_scan")
cp = cscan.cp   # one copy of each module, so exception classes match the scan's own
cs = cscan.cs

SDIR = os.path.join(FIX, "trader-scanner")
SPOT = os.path.join(SDIR, "binance-spot-2026-10-31")   # real Binance Spot answers, recorded 2026-10-31 13:11 UTC
USDM = os.path.join(SDIR, "binance-usdm-2026-10-31")   # real Binance USD-M Futures answers, same minute
# real Binance Spot candles as of 2026-11-07 19:18 UTC (the INJ "ALIGNED" scan), recorded by a --now replay at 22:50:
# the closed candles are Binance's history; the 24h tickers (last price, volume) are those of 22:50, not 19:18
INJ_CASE = os.path.join(SDIR, "binance-spot-2026-11-07-1918")
INI = os.path.join(ROOT, "trader-scanner", "binance.example.ini")
H = 3600000
D = 86400000
T0 = 1793404800000  # 2026-10-31T00:00:00Z


def row(open_ms, o, h, l, c, v=10.0, step=H, qv=None, n=5):
    return [open_ms, str(o), str(h), str(l), str(c), str(v), open_ms + step - 1, str(qv if qv is not None else v * c),
            n, "0", "0", "0"]


def walk(n, start_ms, step=H, p0=100.0, drift=0.0, seed=1, vol=1.0):
    rng = random.Random(seed)
    out, p = [], p0
    for i in range(n):
        c = max(0.01, p * (1 + drift + rng.uniform(-0.01, 0.01)))
        out.append(row(start_ms + i * step, p, max(p, c) * 1.002, min(p, c) * 0.998, c, vol, step))
        p = c
    return out


class FakeTransport(object):
    """leaf -> list of answers (doc or HttpError), consumed in order; the last one repeats."""

    def __init__(self, answers):
        self.answers = {k: list(v) if isinstance(v, list) and v and isinstance(v[0], (cp.HttpError, dict, tuple))
                        else [v] for k, v in answers.items()}
        self.calls = []

    def get(self, base, path, params):
        leaf = path.rstrip("/").split("/")[-1]
        self.calls.append((base, leaf, dict(params)))
        q = self.answers[leaf]
        a = q.pop(0) if len(q) > 1 else q[0]
        if isinstance(a, cp.HttpError):
            raise a
        if isinstance(a, tuple):
            return a
        return a, {}


def client(answers, **kw):
    sleeps = []
    clock = [1000.0]

    def sleep(s):
        sleeps.append(s)
        clock[0] += s
    c = cp.Client(["https://a", "https://b"], FakeTransport(answers), kw.pop("limit", 6000),
                  clock=lambda: clock[0], sleep=sleep, rng=random.Random(0), **kw)
    return c, sleeps


def cfg(text=""):
    return cscan.Config(text=text)


def offline_scan(folder, market, ini_text="", now_ms=None):
    c = cscan.Config(INI)
    if ini_text:
        c.ini.read_string(ini_text)
    c.set("provider", "market", market)
    prov = cscan.build_provider(c, folder, now_ms, sleep=lambda s: None)
    return cscan.run_scan(prov, c, now_ms)


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------- normalization

class Candles(unittest.TestCase):
    def test_duplicates_unordered_incomplete_and_future_are_dropped_and_counted(self):
        rows = walk(10, T0)
        now = T0 + 8 * H + 30 * 60000            # 08:30: bar 08:00 is open, 09:00 is in the future
        dup = list(rows[3])
        dup[4], dup[2] = "999", "1000"           # a late duplicate: the last copy wins
        mixed = [rows[5], rows[0]] + rows[1:5] + rows[6:] + [dup]
        out, q = cp.normalize_klines(mixed, "1h", now)
        self.assertEqual([c.open_ms for c in out], [T0 + i * H for i in range(8)])
        self.assertTrue(q["unordered"])
        self.assertEqual(q["duplicates"], 1)
        self.assertEqual(out[3].close, 999.0)
        self.assertEqual(q["incomplete_dropped"], 1)
        self.assertEqual(q["future_dropped"], 1)
        self.assertEqual(q["closed"], 8)

    def test_a_candle_is_closed_exactly_at_its_end(self):
        rows = walk(3, T0)
        self.assertEqual(len(cp.normalize_klines(rows, "1h", T0 + 3 * H)[0]), 3)
        self.assertEqual(len(cp.normalize_klines(rows, "1h", T0 + 3 * H - 1)[0]), 2)

    def test_invalid_rows_off_grid_and_gaps(self):
        rows = walk(6, T0)
        bad = list(rows[1])
        bad[2] = "1"                             # high under the close: not a candle
        off = row(T0 + 90 * 60000, 1, 2, 0.5, 1.5)
        rows = [rows[0], bad, off, rows[2], rows[5]] + [["x"]]
        out, q = cp.normalize_klines(rows, "1h", T0 + 10 * H)
        self.assertEqual(q["invalid"], 2)
        self.assertEqual(q["off_grid"], 1)
        self.assertEqual(q["missing_bars"], 1 + 2)   # 01:00, then 03:00 and 04:00
        self.assertEqual(len(out), 3)

    def test_zero_volume_counted(self):
        rows = walk(5, T0)
        rows[2][5] = "0"
        rows[2][7] = "0"
        self.assertEqual(cp.normalize_klines(rows, "1h", T0 + 9 * H)[1]["zero_volume_bars"], 1)

    def test_expected_last_open_is_utc_grid(self):
        self.assertEqual(cp.expected_last_open("1d", T0 + 13 * H), T0 - D)
        self.assertEqual(cp.expected_last_open("1h", T0 + 13 * H + 5), T0 + 12 * H)

    def test_asset_identity(self):
        self.assertEqual(cp.asset_identity("1000PEPE"), ("PEPE", 1000))
        self.assertEqual(cp.asset_identity("1MBABYDOGE"), ("BABYDOGE", 1000000))
        self.assertEqual(cp.asset_identity("1000000MOG"), ("MOG", 1000000))
        self.assertEqual(cp.asset_identity("1INCH"), ("1INCH", 1))
        self.assertEqual(cp.asset_identity("0G"), ("0G", 1))


# ---------------------------------------------------------------- network

class Network(unittest.TestCase):
    def test_429_honours_retry_after_then_succeeds(self):
        c, sleeps = client({"x": [cp.HttpError(429, "slow down", -1003, retry_after=7.0), {"ok": 1}]})
        self.assertEqual(c.get("/x"), {"ok": 1})
        self.assertEqual(c.stats["http_429"], 1)
        self.assertIn(7.0, [round(s, 2) for s in sleeps])

    def test_418_stops_at_once(self):
        c, sleeps = client({"x": [cp.HttpError(418, "banned"), {"ok": 1}]})
        with self.assertRaises(cp.BannedError):
            c.get("/x")
        self.assertEqual(c.stats["requests"], 1)

    def test_5xx_backs_off_exponentially_and_moves_host(self):
        c, sleeps = client({"x": [cp.HttpError(502, "bad"), cp.HttpError(503, "bad"), {"ok": 1}]}, backoff=1.0)
        self.assertEqual(c.get("/x"), {"ok": 1})
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(0.5 <= sleeps[0] <= 1.0 and 1.0 <= sleeps[1] <= 2.0, sleeps)
        self.assertEqual([b for b, _, _ in c.transport.calls], ["https://a", "https://b", "https://b"])

    def test_bad_symbol_is_final(self):
        c, _ = client({"klines": cp.HttpError(400, "Invalid symbol.", -1121)})
        with self.assertRaises(cp.HttpError) as e:
            c.get("/klines")
        self.assertEqual(e.exception.code, -1121)
        self.assertEqual(c.stats["requests"], 1)

    def test_network_errors_give_up_after_retries(self):
        c, sleeps = client({"x": cp.HttpError(0, "network: timed out")}, retries=2)
        with self.assertRaises(cp.HttpError):
            c.get("/x")
        self.assertEqual(c.stats["requests"], 3)
        self.assertEqual(len(sleeps), 2)

    def test_weight_budget_waits_for_the_next_minute(self):
        clock = [120.0]
        slept = []

        def sleep(s):
            slept.append(s)
            clock[0] += s
        b = cp.WeightBudget(100, safety=0.5, clock=lambda: clock[0], sleep=sleep)
        for _ in range(5):
            b.acquire(10)
        self.assertEqual(slept, [])
        b.acquire(10)                            # 60 > cap 50: wait for the next minute bucket
        self.assertEqual(len(slept), 1)
        self.assertGreaterEqual(clock[0], 180.0)

    def test_server_weight_header_is_trusted_when_higher(self):
        clock = [0.0]
        slept = []
        b = cp.WeightBudget(100, safety=1.0, clock=lambda: clock[0], sleep=lambda s: (slept.append(s), clock.__setitem__(0, clock[0] + s)))
        b.acquire(1)
        b.observe(99)
        b.acquire(5)
        self.assertEqual(len(slept), 1)

    def test_kline_weights(self):
        f = cp.BinanceFuturesProvider
        self.assertEqual([f.kline_weight(None, n) for n in (99, 100, 499, 500, 1000, 1500)], [1, 2, 2, 5, 5, 10])

    def test_pagination_walks_back_without_overlap(self):
        rows = walk(25, T0)
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, "klines"))
            with open(os.path.join(tmp, "klines", "XUSDT_1h.json"), "w") as fh:
                json.dump(rows, fh)
            prov = cp.make_provider("binance", "spot", cp.FixtureTransport(tmp, T0 + 30 * H), sleep=lambda s: None)
            prov.MAX_LIMIT = 10
            got = prov.klines("XUSDT", "1h", 18, end_ms=T0 + 24 * H)
            self.assertEqual([r[0] for r in got], [r[0] for r in rows[7:25]])
            got = prov.klines("XUSDT", "1h", 100, end_ms=T0 + 24 * H)   # stops at the listing
            self.assertEqual(len(got), 25)
            self.assertEqual(len(prov.client.transport.calls), 2 + 3)
        finally:
            shutil.rmtree(tmp)

    def test_unknown_interval_refused(self):
        prov = cp.make_provider("binance", "spot", FakeTransport({}))
        with self.assertRaises(cp.ProviderError):
            prov.klines("BTCUSDT", "1w", 10)

    def test_no_signed_or_order_endpoint_anywhere(self):
        for f in sorted(f for f in os.listdir(os.path.join(ROOT, "trader-scanner")) if f.startswith("crypto_") and f.endswith(".py")):
            with open(os.path.join(ROOT, "trader-scanner", f)) as fh:
                src = fh.read()
            for bad in ("/order", "signature", "X-MBX-APIKEY", "hmac", "api_key", "secret"):
                self.assertNotIn(bad, src, "%s mentions %s" % (f, bad))


# ---------------------------------------------------------------- universe

def ei(symbols):
    return {"symbols": symbols}


def spot_sym(sym, base, quote="USDT", status="TRADING"):
    return {"symbol": sym, "baseAsset": base, "quoteAsset": quote, "status": status, "isSpotTradingAllowed": True}


def fut_sym(sym, base, ctype="PERPETUAL", status="TRADING", under="COIN"):
    return {"symbol": sym, "baseAsset": base, "quoteAsset": "USDT", "status": status, "contractType": ctype,
            "underlyingType": under, "onboardDate": T0 - 400 * D}


def tick(sym, qv=1e8, trades=100000, bid=99.99, ask=100.01, close_ms=T0 + 13 * H):
    return {"symbol": sym, "lastPrice": "100", "quoteVolume": str(qv), "volume": "1", "count": trades,
            "priceChangePercent": "1.0", "bidPrice": str(bid), "askPrice": str(ask), "closeTime": close_ms}


class Universe(unittest.TestCase):
    def test_spot_fixture_finds_deep_missing_and_wal_illiquid(self):
        doc = offline_scan(SPOT, "spot")
        ex = {e["requested"]: e["reason"] for e in doc["excluded"]}
        self.assertEqual(ex["DEEP"], "not listed on binance spot as DEEPUSDT")
        self.assertTrue(ex["WAL"].startswith("24h quote volume"), ex["WAL"])
        self.assertEqual(doc["coverage"]["requested"], 16)
        self.assertEqual(doc["coverage"]["eligible"], 14)

    def test_futures_fixture_lists_deep(self):
        doc = offline_scan(USDM, "usdm")
        ex = {e["requested"]: e for e in doc["excluded"]}
        self.assertEqual(ex["DEEP"]["symbol"], "DEEPUSDT")
        self.assertIn("quote volume", ex["DEEP"]["reason"])
        self.assertTrue(all(c["instrument_id"].startswith("binance:usdm:") for c in doc["candidates"]))

    def test_resolution_rules(self):
        insts = cp.BinanceFuturesProvider(None)
        rows = [fut_sym("BTCUSDT", "BTC"), fut_sym("1000PEPEUSDT", "1000PEPE"),
                fut_sym("TSLAUSDT", "TSLA", "TRADIFI_PERPETUAL", under="EQUITY"),
                fut_sym("BTCUSDT_261225", "BTC", "CURRENT_QUARTER"), fut_sym("OLDUSDT", "OLD", status="SETTLING"),
                fut_sym("USDCUSDT", "USDC")]
        insts.client = type("C", (), {"get": lambda self, p, params=None, weight=1: ei(rows)})()
        ins = insts.instruments()
        c = cfg("[universe]\nsymbols = BTC, PEPE, TSLA, OLD, USDC, NOPE, BTCUSDT\n")
        sel, ex = cscan.resolve_universe(ins, {}, c, "usdm")
        self.assertEqual([(r, i.symbol) for r, i in sel], [("BTC", "BTCUSDT"), ("PEPE", "1000PEPEUSDT"),
                                                            ("TSLA", "TSLAUSDT"), ("OLD", "OLDUSDT"),
                                                            ("USDC", "USDCUSDT")])
        self.assertEqual(ex, [{"requested": "NOPE", "symbol": None, "reason": "not listed on binance usdm as NOPEUSDT"}])
        pepe = sel[1][1]
        self.assertEqual((pepe.asset_id, pepe.multiplier), ("PEPE", 1000))
        now = T0 + 13 * H
        t = cp._Binance._ticker(tick("X"))
        why = {i.symbol: cscan.eligibility(i, t, c, now) for _, i in sel}
        self.assertIsNone(why["BTCUSDT"])
        self.assertEqual(why["TSLAUSDT"], "contract type TRADIFI_PERPETUAL")
        self.assertEqual(why["OLDUSDT"], "status SETTLING, not TRADING")
        self.assertIn("stablecoin", why["USDCUSDT"])

    def test_liquidity_filters(self):
        c = cfg()
        inst = cp.Instrument("binance:spot:X", "binance", "spot", "XUSDT", "X", "USDT", "X", 1, "TRADING", "SPOT",
                             None, None, ())
        now = T0 + 13 * H
        T = lambda **kw: cp._Binance._ticker(tick("XUSDT", **kw))
        self.assertIsNone(cscan.eligibility(inst, T(), c, now))
        self.assertIn("quote volume", cscan.eligibility(inst, T(qv=0), c, now))
        self.assertIn("trades", cscan.eligibility(inst, T(trades=3), c, now))
        self.assertIn("spread", cscan.eligibility(inst, T(bid=99, ask=101), c, now))
        self.assertIn("old", cscan.eligibility(inst, T(close_ms=now - 3600000), c, now))
        self.assertEqual(cscan.eligibility(inst, None, c, now), "no 24h ticker")
        brk = inst._replace(status="BREAK")
        self.assertEqual(cscan.eligibility(brk, T(), c, now), "status BREAK, not TRADING")

    def test_spot_and_futures_same_symbol_do_not_collide(self):
        s = offline_scan(SPOT, "spot")
        f = offline_scan(USDM, "usdm")
        ids = {c["instrument_id"] for c in s["candidates"]} & {c["instrument_id"] for c in f["candidates"]}
        self.assertEqual(ids, set())
        both = {c["symbol"] for c in s["candidates"]} & {c["symbol"] for c in f["candidates"]}
        self.assertIn("ZECUSDT", both)
        tmp = tempfile.mkdtemp()
        try:
            a = cscan.CachedExchangeInfo(FakeTransport({"exchangeInfo": ei([spot_sym("BTCUSDT", "BTC")])}), tmp, "spot", 60)
            b = cscan.CachedExchangeInfo(FakeTransport({"exchangeInfo": ei([fut_sym("BTCUSDT", "BTC")])}), tmp, "usdm", 60)
            a.get("", "/api/v3/exchangeInfo", {})
            b.get("", "/fapi/v1/exchangeInfo", {})
            self.assertEqual(sorted(os.listdir(tmp)), ["spot-exchangeInfo.json", "usdm-exchangeInfo.json"])
            again, _ = a.get("", "/api/v3/exchangeInfo", {})
            self.assertEqual(len(a.inner.calls), 1)
            self.assertIn("isSpotTradingAllowed", again["symbols"][0])
        finally:
            shutil.rmtree(tmp)

    def test_top_volume_mode(self):
        doc = offline_scan(USDM, "usdm", "[universe]\nmode = top_volume\ntop_n = 5\n")
        self.assertEqual(doc["coverage"]["eligible"], 5)
        vols = {}
        with open(os.path.join(USDM, "ticker_24hr.json")) as fh:
            for t in json.load(fh):
                vols[t["symbol"]] = float(t["quoteVolume"])
        scanned = sorted(vols, key=lambda s: -vols[s])[:5]
        self.assertTrue({c["symbol"] for c in doc["candidates"]} <= set(scanned))

    def test_symbols_option(self):
        c = cscan.Config(INI)
        cscan.apply_symbols(c, "@sui_ecosystem")
        self.assertEqual(c.items("universe", "symbols"), ["SUI", "DEEP", "WAL", "CETUS", "HAEDAL"])
        cscan.apply_symbols(c, "top:7")
        self.assertEqual((c.get("universe", "mode"), c.get("universe", "top_n")), ("top_volume", "7"))
        with self.assertRaises(cscan.CryptoScanError):
            cscan.apply_symbols(c, "@nope")


# ---------------------------------------------------------------- per-timeframe quality and setups

def candles(rows, now):
    return cp.normalize_klines(rows, "1h", now)[0]


class Quality(unittest.TestCase):
    def setUp(self):
        self.c = cfg()
        self.now = T0 + 100 * H

    def test_insufficient_history(self):
        cc = candles(walk(40, T0 + 60 * H), self.now)
        self.assertIn("insufficient history: 40 closed bars", cscan.quality_gate(cc, {}, "1h", self.now, 50, self.c))

    def test_stale_when_the_newest_closed_candle_is_missing(self):
        cc = candles(walk(99, T0), self.now)           # 00:00 .. 98:00 closed; 99:00 expected
        self.assertIsNone(cscan.quality_gate(candles(walk(100, T0), self.now), {}, "1h", self.now, 50, self.c))
        self.assertIn("stale", cscan.quality_gate(cc, {}, "1h", self.now, 50, self.c))

    def test_gaps_and_zero_volume(self):
        rows = walk(100, T0)
        del rows[90:93]
        self.assertIn("3 missing", cscan.quality_gate(candles(rows, self.now), {}, "1h", self.now, 50, self.c))
        rows = walk(100, T0)
        for r in rows[60:70]:
            r[5] = "0"
        self.assertIn("zero-volume", cscan.quality_gate(candles(rows, self.now), {}, "1h", self.now, 50, self.c))

    def test_indicators_missing_when_short(self):
        ind = cs.indicators(candles(walk(10, T0), self.now))
        self.assertIsNone(ind["rsi14"])
        self.assertIsNone(ind["atr14"])
        self.assertIsNone(ind["rvol20"])
        self.assertIsNone(ind["ma20"])


class Setups(unittest.TestCase):
    P = {s: dict(cs.DEFAULT_PARAMS[s]) for s in cs.DEFAULT_PARAMS}

    def mk(self, closes, vols=None, opens=None):
        out = []
        for i, c in enumerate(closes):
            o = opens[i] if opens else (closes[i - 1] if i else c)
            out.append(cp.Candle(T0 + i * H, T0 + (i + 1) * H - 1, o, max(o, c) * 1.001, min(o, c) * 0.999, c,
                                 1.0, (vols[i] if vols else 1000.0), 10))
        return out

    def ids(self, closes, enabled=None, **kw):
        return [h["id"] for h in cs.bar_setups(self.mk(closes, **kw), enabled or cs.BAR_SETUPS, self.P)]

    def test_breakout_matches_the_equity_rule_on_closes(self):
        closes = [100.0] * 30 + [101.0]
        self.assertIn("breakout_close_high", self.ids(closes))
        tags, _ = scan.find_setups(closes)           # the equity rule on the same closes agrees
        self.assertIn("breakout_20d", tags)
        self.assertNotIn("breakout_close_high", self.ids([100.0] * 30 + [100.0]))

    def test_ported_rules_agree_with_the_equity_scan(self):
        rng = random.Random(4)
        mapping = {"breakout_20d": "breakout_close_high", "bull_flag": "bull_flag", "reclaim_ma20": "reclaim_ma",
                   "oversold": "rsi_oversold", "oversold_bounce": "oversold_bounce"}
        for trial in range(300):
            p, closes = 100.0, []
            drift = rng.uniform(-0.02, 0.02)
            for _ in range(40):
                p *= 1 + drift + rng.uniform(-0.04, 0.04)
                closes.append(p)
            eq, _ = scan.find_setups(closes)
            cr = set(self.ids(closes, [mapping[k] for k in mapping]))
            self.assertEqual({mapping[t] for t in eq}, cr, trial)

    def test_rvol_is_activity_on_up_and_down_bars(self):
        closes = [100.0] * 30
        vols = [1000.0] * 29 + [5000.0]
        for last, bar in ((102.0, "up"), (98.0, "down")):
            hits = cs.bar_setups(self.mk(closes[:-1] + [last], vols=vols), ["rvol_expansion"], self.P)
            self.assertEqual((hits[0]["kind"], hits[0]["direction"]), ("activity", "unspecified"))
            self.assertEqual((hits[0]["evidence"]["rvol"], hits[0]["evidence"]["bar_direction"]), (5.0, bar))
            self.assertIn("no direction", hits[0]["summary"])
            self.assertEqual(cs.timeframe_bias(hits), "unspecified")

    def test_momentum_accel(self):
        flat_then_up = [100.0] * 25 + [100.0, 100.2, 100.4, 100.6, 100.8] + [102, 104, 106, 108, 110]
        self.assertIn("momentum_accel", self.ids(flat_then_up, ["momentum_accel"]))
        steady = [100 * 1.01 ** i for i in range(35)]
        self.assertNotIn("momentum_accel", self.ids(steady, ["momentum_accel"]))

    def test_every_hit_says_where_it_came_from(self):
        closes = [100.0] * 30 + [101.0]
        for h in cs.bar_setups(self.mk(closes), cs.BAR_SETUPS, self.P):
            self.assertIn(h["origin"].split(":")[0], ("ported", "new"))
            self.assertTrue(h["label"] and h["params"])

    def test_per_timeframe_override_is_recorded_in_the_params(self):
        c = cfg("[setup.bull_flag]\nimpulse_pct@1h = 4\n")
        self.assertEqual(c.setup_params("bull_flag", "1h")["impulse_pct"], 4.0)
        self.assertEqual(c.setup_params("bull_flag", "1d")["impulse_pct"], 10.0)
        self.assertNotEqual(c.fingerprint(), cfg().fingerprint())

    def test_utc_orb_experimental(self):
        p = dict(cs.DEFAULT_PARAMS["utc_orb"])
        q = 900000
        rows = []
        for i in range(12):                          # 00:00 .. 02:45 UTC, 15-minute bars
            c = 100.0 if i < 4 else (100.5 if i < 7 else 101.5)
            rows.append(cp.Candle(T0 + i * q, T0 + (i + 1) * q - 1, 100, max(100, c) + (0.4 if i < 4 else 0),
                                  99.5, c, 1, 100, 1))
        hit, status = cs.utc_orb(rows, q, T0 + 12 * q, p)
        self.assertEqual(status, "hit")
        self.assertEqual(hit["evidence"]["range_high"], 100.4)
        self.assertEqual(hit["evidence"]["bar_open_ms"], T0 + 4 * q)
        self.assertEqual(hit["origin"], "experimental")
        self.assertEqual(cs.utc_orb(rows, q, T0 + 4 * q, p)[1], "range not complete")
        self.assertEqual(cs.utc_orb(rows[:2] + rows[3:], q, T0 + 12 * q, p)[1], "range has a missing bar")


# ---------------------------------------------------------------- direction: what each rule asserts

class Direction(unittest.TestCase):
    P = Setups.P
    mk = Setups.mk

    def hits(self, closes, enabled, **kw):
        return cs.bar_setups(self.mk(closes, **kw), enabled, self.P)

    def test_every_rule_declares_kind_and_direction_and_none_is_bearish(self):
        self.assertEqual(set(cs.KIND), set(cs.DIRECTION))
        self.assertEqual(set(cs.BAR_SETUPS) | {"utc_orb"}, set(cs.KIND))
        self.assertNotIn("bearish", cs.DIRECTION.values())   # the scope note says so; a bearish rule must change it
        self.assertEqual({sid for sid, d in cs.DIRECTION.items() if d == "unspecified"}, {"rsi_oversold", "rvol_expansion"})

    def test_a_decline_that_is_only_slowing_does_not_match_momentum(self):
        # last 5 bars -1 %, the 5 before -10 %: downside momentum improved by 9 points, but price still fell
        closes = [100.0] * 25 + [100.0, 97.5, 95.0, 92.5, 90.0] + [89.82, 89.64, 89.46, 89.28, 89.1]
        self.assertEqual(self.hits(closes, ["momentum_accel"]), [])

    def test_momentum_after_a_decline_is_called_a_rebound_not_a_trend(self):
        closes = [100.0] * 25 + [100.0, 97.5, 95.0, 92.5, 90.0] + [90.5, 91.0, 91.5, 92.0, 92.5]
        h = self.hits(closes, ["momentum_accel"])[0]
        self.assertEqual((h["kind"], h["direction"], h["evidence"]["context"]), ("state", "bullish", "rebound_after_decline"))
        self.assertIn("a rebound after a decline: momentum improving, not an established trend", h["summary"])
        up = [100.0] * 25 + [100.0, 100.2, 100.4, 100.6, 100.8] + [102, 104, 106, 108, 110]
        self.assertEqual(self.hits(up, ["momentum_accel"])[0]["evidence"]["context"], "acceleration")

    def test_reclaim_is_fresh_only_and_a_failed_reclaim_does_not_match(self):
        below = [100.0] * 20 + [99.0, 98.0, 97.0, 96.0]
        fresh = self.hits(below + [101.0], ["reclaim_ma"])
        self.assertEqual((fresh[0]["kind"], fresh[0]["direction"]), ("event", "bullish"))
        self.assertIn("fresh cross on this candle", fresh[0]["summary"])
        self.assertGreater(fresh[0]["evidence"]["pct_above_ma"], 0)
        # one candle later the price closes back under the average: the earlier reclaim is not carried forward
        self.assertEqual(self.hits(below + [101.0, 98.0], ["reclaim_ma"]), [])
        # and staying above the average is a state the rule does not report either
        self.assertEqual(self.hits(below + [101.0, 101.5], ["reclaim_ma"]), [])

    def test_a_utc_orb_breakout_that_failed_since_says_so(self):
        p = dict(cs.DEFAULT_PARAMS["utc_orb"])
        q = 900000
        closes = [100.0] * 4 + [101.5, 101.0, 99.8]  # range, breakout at 01:00, then back under the range high
        rows = [cp.Candle(T0 + i * q, T0 + (i + 1) * q - 1, 100, max(100, c) + (0.4 if i < 4 else 0), 99.5, c, 1, 100, 1)
                for i, c in enumerate(closes)]
        hit, status = cs.utc_orb(rows, q, T0 + len(rows) * q, p)
        self.assertEqual(status, "hit")
        e = hit["evidence"]
        self.assertEqual((hit["kind"], e["bar_open_ms"], e["minutes_since"]), ("event", T0 + 4 * q, 30.0))
        self.assertEqual((e["last_close"], e["holds_at_last_close"]), (99.8, False))
        self.assertIn("back under it: the breakout has failed since", hit["summary"])
        held, _ = cs.utc_orb(rows[:5], q, T0 + 5 * q, p)
        self.assertTrue(held["evidence"]["holds_at_last_close"])

    def test_timeframe_bias_never_reads_no_match_as_a_direction(self):
        bull, bear = {"direction": "bullish"}, {"direction": "bearish"}
        act = {"direction": "unspecified"}
        self.assertEqual(cs.timeframe_bias([]), "no_match")
        self.assertEqual(cs.timeframe_bias([act]), "unspecified")
        self.assertEqual(cs.timeframe_bias([bull, act]), "bullish")
        self.assertEqual(cs.timeframe_bias([bear, act]), "bearish")
        self.assertEqual(cs.timeframe_bias([bull, bear]), "mixed")

    def test_rule_agreement_across_timeframes(self):
        tfs = ["1d", "1h"]
        cases = [({"1d": "bullish", "1h": "bullish"}, "bullish"),
                 ({"1d": "bullish", "1h": "no_match"}, "partial_bullish"),     # a missing match is not bearish
                 ({"1d": "bullish", "1h": "unspecified"}, "partial_bullish"),  # volume is not bullish
                 ({"1d": "bullish", "1h": None}, "partial_bullish"),           # 1h not evaluated
                 ({"1d": "bullish", "1h": "bearish"}, "conflicting"),          # opposing timeframes
                 ({"1d": "mixed", "1h": "bullish"}, "conflicting"),
                 ({"1d": "bearish", "1h": "bearish"}, "bearish"),
                 ({"1d": "no_match", "1h": "bearish"}, "partial_bearish"),
                 ({"1d": "unspecified", "1h": "no_match"}, "unspecified"),
                 ({"1d": "no_match", "1h": "no_match"}, "none")]
        for bias, want in cases:
            self.assertEqual(cscan.rule_agreement(bias, tfs), want, bias)
        self.assertEqual(cscan.rule_agreement({"1d": "bullish"}, ["1d"]), "partial_bullish")  # one timeframe is no agreement

    def test_agreement_wording(self):
        def text(bias):
            return cscan.agreement_text({"rule_bias": bias, "rule_agreement": cscan.rule_agreement(bias, list(bias))})
        self.assertEqual(text({"1d": "bullish", "1h": "bullish"}), "BULLISH RULES: 1d + 1h")
        self.assertEqual(text({"1d": "bullish", "1h": "no_match"}), "BULLISH RULES: 1d only (1h: no rule matched)")
        self.assertEqual(text({"1d": "no_match", "1h": "bearish"}), "BEARISH RULES: 1h only (1d: no rule matched)")
        self.assertEqual(text({"1d": "bullish", "1h": "bearish"}), "CONFLICTING RULES: 1d bullish, 1h bearish")
        self.assertEqual(text({"1d": "no_match", "1h": "unspecified"}),
                         "NO DIRECTIONAL RULE: only observations without a direction (1h)")
        self.assertNotIn("ALIGNED", text({"1d": "bullish", "1h": "bullish"}))


class InjCase(unittest.TestCase):
    """The scan of 2026-11-07 19:18 UTC that printed `INJUSDT ALIGNED`, replayed on Binance's recorded candles."""

    @classmethod
    def setUpClass(cls):
        with unittest.mock.patch.dict(os.environ, NO_ENV):
            cls.doc = offline_scan(INJ_CASE, "spot")
        cls.by = {c["symbol"]: c for c in cls.doc["candidates"]}

    def test_the_same_candidates_as_the_live_scan(self):
        self.assertEqual(self.doc["created_at"], "2026-11-07T19:18:00Z")
        self.assertEqual(set(self.by), {"INJUSDT", "ZECUSDT", "PAXGUSDT", "SOLUSDT", "BNBUSDT", "TAOUSDT", "ONDOUSDT"})
        ids = {s: {tf: [h["id"] for h in v["setups"]] for tf, v in c["timeframes"].items()} for s, c in self.by.items()}
        self.assertEqual(ids["INJUSDT"], {"1d": ["momentum_accel"], "1h": ["reclaim_ma"]})
        self.assertEqual(ids["PAXGUSDT"], {"1d": ["rvol_expansion", "momentum_accel"], "1h": []})
        self.assertEqual(ids["ONDOUSDT"], {"1d": [], "1h": ["rvol_expansion"]})

    def test_closed_candles_only_and_their_timestamps(self):
        for c in self.doc["candidates"]:
            self.assertEqual(c["timeframes"]["1h"]["last_closed_open"], "2026-11-07T18:00:00Z")
            self.assertEqual(c["timeframes"]["1h"]["last_closed_close"], "2026-11-07T19:00:00Z")
            self.assertEqual(c["timeframes"]["1d"]["last_closed_close"], "2026-11-07T00:00:00Z")
            # the recording also holds the 19:00 candle (closed by 22:50); at 19:18 it was open and is dropped
            self.assertEqual(c["timeframes"]["1h"]["quality"]["incomplete_dropped"], 1)
        text = cscan.render(self.doc, 7)
        self.assertIn("Evaluated candles, by their UTC close: 1d 2026-11-07 00:00, 1h 2026-11-07 19:00.", text)
        self.assertIn("(candle closed 2026-11-07 19:00, RSI 53)", text)

    def test_inj_rules_are_bullish_on_both_timeframes_and_say_exactly_what_they_saw(self):
        inj = self.by["INJUSDT"]
        self.assertEqual((inj["rule_agreement"], inj["rule_bias"]), ("bullish", {"1d": "bullish", "1h": "bullish"}))
        rc = inj["timeframes"]["1h"]["setups"][0]
        self.assertEqual((rc["evidence"]["close"], rc["evidence"]["ma"], rc["evidence"]["prev_close"]), (8.271, 8.2459, 8.101))
        self.assertIn("fresh cross on this candle: close 8.271 over the 20-bar SMA 8.2459 (+0.30%)", rc["summary"])
        mo = inj["timeframes"]["1d"]["setups"][0]
        self.assertEqual(mo["evidence"]["context"], "rebound_after_decline")

    def test_volume_gets_no_bullish_credit(self):
        paxg, ondo = self.by["PAXGUSDT"], self.by["ONDOUSDT"]
        rv = paxg["timeframes"]["1d"]["setups"][0]
        self.assertEqual((rv["direction"], rv["evidence"]["bar_direction"]), ("unspecified", "down"))
        self.assertEqual((paxg["bullish_rules"], paxg["nondirectional_rules"]), (1, 1))  # momentum only
        self.assertEqual((ondo["rule_agreement"], ondo["bullish_rules"], ondo["aligned"]), ("unspecified", 0, False))
        self.assertEqual(self.doc["candidates"][-1]["symbol"], "ONDOUSDT")   # activity only ranks last

    def test_cli_says_what_the_json_says(self):
        text = cscan.render(self.doc, 10)
        self.assertNotIn("ALIGNED", text)
        self.assertNotIn("(down)", text)
        for c in self.doc["candidates"]:
            self.assertIn(cscan.agreement_text(c), text)
            for tf, v in c["timeframes"].items():
                for h in v["setups"]:
                    self.assertIn(h["summary"], text)
        self.assertIn("BULLISH RULES: 1d + 1h", text)
        self.assertIn("BULLISH RULES: 1d only (1h: no rule matched)", text)
        self.assertIn("NO DIRECTIONAL RULE: only observations without a direction (1h)", text)
        self.assertIn("no bearish rules", text)
        # the recording's tickers are from 22:50: the price is flagged and no live comparison is drawn
        self.assertIn("after the scan time: not point-in-time", text)
        self.assertNotIn("from the 1h close", text)

    def test_a_live_price_is_compared_with_the_evaluated_close(self):
        c = copy.deepcopy(self.by["INJUSDT"])
        c["last_price"], c["price_time"] = 8.216, "2026-11-07T19:17:59Z"
        line = cscan._live_line(c, self.doc, ["1d", "1h"])
        self.assertEqual(line, "live 8.2160 at 2026-11-07 19:17 UTC is -0.66% from the 1h close 8.2710 "
                               "(after the evaluated candle, not in the rules)")

    def test_a_futures_volume_spike_no_longer_makes_hype_aligned(self):
        """Recorded 2026-10-31: v1 ranked HYPEUSDT first and ALIGNED because its only 1h match was an up-bar volume spike."""
        with unittest.mock.patch.dict(os.environ, NO_ENV):
            hype = next(c for c in offline_scan(USDM, "usdm")["candidates"] if c["symbol"] == "HYPEUSDT")
        self.assertEqual([h["id"] for h in hype["timeframes"]["1h"]["setups"]], ["rvol_expansion"])
        self.assertEqual((hype["rule_bias"]["1h"], hype["rule_agreement"], hype["aligned"]),
                         ("unspecified", "partial_bullish", False))
        self.assertEqual(cscan.agreement_text(hype), "BULLISH RULES: 1d only (1h: no direction)")


# ---------------------------------------------------------------- end to end, offline

class Contract(unittest.TestCase):
    def test_spot_example_is_reproducible(self):
        a = offline_scan(SPOT, "spot")
        b = offline_scan(SPOT, "spot")
        for d in (a, b):
            d.pop("seconds")
            d.pop("network")
        self.assertEqual(a, b)
        self.assertEqual(a["status"], "complete")
        self.assertEqual(a["created_at"], "2026-10-31T13:11:25Z")
        # v1 ranked BNBUSDT 5th on an up-bar volume spike counted as bullish; in v2 volume has no direction
        self.assertEqual([c["symbol"] for c in a["candidates"]],
                         ["ZECUSDT", "ONDOUSDT", "ETHUSDT", "SOLUSDT", "INJUSDT", "PENGUUSDT", "PAXGUSDT", "BNBUSDT"])
        top = a["candidates"][0]
        self.assertTrue(top["aligned"])
        self.assertEqual(top["rule_agreement"], "bullish")
        self.assertEqual([h["id"] for h in top["timeframes"]["1h"]["setups"]], ["breakout_close_high"])

    def test_futures_example_carries_derivatives_context(self):
        d = offline_scan(USDM, "usdm")
        self.assertEqual(d["candidates"][0]["symbol"], "ZECUSDT")
        ctx = d["candidates"][0]["derivatives"]
        for k in ("mark_price", "index_price", "basis_bps", "funding_rate_last", "next_funding_time",
                  "open_interest", "open_interest_notional"):
            self.assertIn(k, ctx)
        self.assertIn("no leverage", ctx["note"])

    def test_output_contract(self):
        d = offline_scan(SPOT, "spot")
        for k in ("schema", "schema_version", "created_at", "as_of_ms", "exchange", "market", "timeframes",
                  "config_fingerprint", "status", "coverage", "candidates", "excluded", "failed", "hits",
                  "score_note", "note"):
            self.assertIn(k, d)
        self.assertEqual((d["schema"], d["schema_version"]), ("day-trader-skill/crypto-scan", 2))
        self.assertIn("no bearish rules", d["scope_note"])
        for c in d["candidates"]:
            for k in ("rank", "instrument_id", "exchange", "market", "symbol", "asset_id", "last_price",
                      "price_time", "price_age_seconds", "quote_volume_24h", "timeframes", "aligned",
                      "rule_bias", "rule_agreement", "bullish_rules", "bearish_rules", "nondirectional_rules",
                      "descriptive_score", "estimated_probability"):
                self.assertIn(k, c)
            self.assertIsNone(c["estimated_probability"])
            self.assertEqual(c["aligned"], c["rule_agreement"] == "bullish")
            self.assertEqual(c["descriptive_score"], c["bullish_rules"])
            self.assertEqual(c["bullish_rules"] + c["bearish_rules"] + c["nondirectional_rules"], c["matched_rules"])
            for tf, v in c["timeframes"].items():
                self.assertTrue(v["last_closed_close"].endswith("Z"))
                self.assertLessEqual(v["last_closed_close"], d["created_at"])
                self.assertEqual(v["bias"], c["rule_bias"][tf])
                for h in v["setups"]:
                    self.assertIn(h["kind"], ("event", "state", "activity"))
                    self.assertIn(h["direction"], ("bullish", "bearish", "unspecified"))
                    self.assertTrue(h["summary"])
        self.assertEqual([c["rank"] for c in d["candidates"]], list(range(1, len(d["candidates"]) + 1)))
        self.assertEqual(d["hits"][0]["symbol"], d["candidates"][0]["symbol"])
        json.dumps(d, allow_nan=False)
        # the watchlist reader of the equity scanner reads the crypto file too
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "w.json")
            cscan.write_json(p, d)
            self.assertEqual(scan.symbols_from_file(p)[0], "ZECUSDT")
        finally:
            shutil.rmtree(tmp)

    def test_ranking_is_deterministic_under_input_order(self):
        a = offline_scan(SPOT, "spot")
        tmp = tempfile.mkdtemp()
        try:
            shutil.copytree(SPOT, os.path.join(tmp, "f"))
            p = os.path.join(tmp, "f", "ticker_24hr.json")
            with open(p) as fh:
                t = json.load(fh)
            random.Random(3).shuffle(t)
            with open(p, "w") as fh:
                json.dump(t, fh)
            b = offline_scan(os.path.join(tmp, "f"), "spot", "[network]\nmax_workers = 1\n")
        finally:
            shutil.rmtree(tmp)
        self.assertEqual([c["instrument_id"] for c in a["candidates"]], [c["instrument_id"] for c in b["candidates"]])

    def test_no_look_ahead_in_a_replay(self):
        """Scanning as of an earlier hour with the full fixture equals scanning a fixture cut at that hour."""
        now = 1793452285653 - 7 * H              # seven hours before the recording (meta.json server time)
        full = offline_scan(SPOT, "spot", now_ms=now)
        tmp = tempfile.mkdtemp()
        try:
            cut = os.path.join(tmp, "cut")
            shutil.copytree(SPOT, cut)
            kd = os.path.join(cut, "klines")
            for f in os.listdir(kd):
                rows = cp._read_json(os.path.join(kd, f[:-3]))
                os.remove(os.path.join(kd, f))
                with open(os.path.join(kd, f[:-3]), "w") as fh:
                    json.dump([r for r in rows if r[0] + (D if "_1d" in f else H) <= now], fh)
            trunc = offline_scan(cut, "spot", now_ms=now)
        finally:
            shutil.rmtree(tmp)
        self.assertTrue(full["replay"])
        for d in (full, trunc):
            for k in ("seconds", "network"):
                d.pop(k)
            for c in d["candidates"]:
                for v in c["timeframes"].values():
                    # the full fixture also answers the candle still open at `now` (dropped, and counted)
                    v["quality"].pop("received")
                    v["quality"].pop("incomplete_dropped")
        self.assertEqual(full, trunc)
        self.assertTrue(full["candidates"])
        self.assertIn("not point-in-time", full["replay_note"])

    def test_failed_fetches_make_the_scan_partial(self):
        tmp = tempfile.mkdtemp()
        try:
            f = os.path.join(tmp, "f")
            shutil.copytree(SPOT, f)
            os.remove(os.path.join(f, "klines", "BTCUSDT_1h.json.gz"))   # answers -1121, like a delisted pair
            d = offline_scan(f, "spot")
        finally:
            shutil.rmtree(tmp)
        self.assertEqual(d["status"], "partial")
        self.assertIn("BTCUSDT 1h", d["failed"])
        self.assertEqual(d["coverage"]["failed_fetches"], 1)

    def test_a_ban_stops_the_scan_and_says_so(self):
        c = cscan.Config(INI)
        c.set("universe", "symbols", "BTC, ETH")
        rows = walk(600, T0 - 599 * H)
        answers = {"time": {"serverTime": T0 + 1000},
                   "exchangeInfo": ei([spot_sym("BTCUSDT", "BTC"), spot_sym("ETHUSDT", "ETH")]),
                   "24hr": ([tick("BTCUSDT", close_ms=T0), tick("ETHUSDT", close_ms=T0)], {}),
                   "klines": [cp.HttpError(418, "banned")]}
        prov = cp.make_provider("binance", "spot", FakeTransport(answers), sleep=lambda s: None)
        d = cscan.run_scan(prov, c)
        self.assertEqual(d["status"], "partial")
        self.assertIn("418", d["stopped"])
        self.assertEqual(d["candidates"], [])
        del rows

    def test_cli_offline_and_exit_codes(self):
        tmp = tempfile.mkdtemp()
        try:
            out = os.path.join(tmp, "w.json")
            code, text, err = run_cli(["crypto", "--config", INI, "--offline", USDM, "--market", "usdm", "--out", out])
            self.assertEqual(code, 0, err)
            self.assertIn("Binance USD-M Futures scan", text)
            self.assertIn("not a probability", text)
            with open(out) as fh:
                self.assertEqual(json.load(fh)["market"], "usdm")
            code, text, err = run_cli(["crypto", "--config", INI, "--offline", SPOT, "--symbols", "DEEP", "--no-write"])
            self.assertEqual(code, 1)
            self.assertIn("not listed", text)
            code, _, err = run_cli(["crypto", "--config", os.path.join(tmp, "nope.ini"), "--offline", SPOT])
            self.assertEqual(code, 1)
            self.assertIn("no config", err)
            bad = os.path.join(tmp, "bad.ini")
            with open(bad, "w") as fh:
                fh.write("[setups]\nenabled = breakout_20d\n")
            code, _, err = run_cli(["crypto", "--config", bad, "--offline", SPOT, "--no-write"])
            self.assertEqual(code, 1)
            self.assertIn("unknown setup breakout_20d", err)
        finally:
            shutil.rmtree(tmp)

    def test_now_without_offset_is_utc(self):
        self.assertEqual(cscan.parse_now_utc("2026-10-31 00:00"), T0)
        self.assertEqual(cscan.parse_now_utc("2026-10-31T02:00+02:00"), T0)
        with self.assertRaises(cscan.CryptoScanError):
            cscan.parse_now_utc("yesterday")


# ---------------------------------------------------------------- paths, standalone

NO_ENV = {k: "" for k in (cscan.ENV_CONFIG_FILE, cscan.ENV_OUTPUT_DIR)}


class PathsAndStandalone(unittest.TestCase):
    def test_paths_without_environment_are_unchanged(self):
        self.assertEqual(cscan.resolve_path("trader/crypto_watchlist.json", "spot", env={}), "trader/crypto_watchlist.json")
        self.assertEqual(cscan.resolve_path("trader/scan-{market}.json", "usdm", env={}), "trader/scan-usdm.json")
        self.assertIsNone(cscan.config_path(None, env={}) if not os.path.exists(cscan.DEFAULT_CONFIG) else None)

    def test_output_dir_moves_the_trader_folder_but_not_absolute_paths(self):
        env = {cscan.ENV_OUTPUT_DIR: "/data/desk"}
        self.assertEqual(cscan.resolve_path("trader/crypto-scan-{market}.json", "spot", env=env),
                         "/data/desk/crypto-scan-spot.json")
        self.assertEqual(cscan.resolve_path("trader/cache/binance", None, env=env), "/data/desk/cache/binance")
        self.assertEqual(cscan.resolve_path("other/x.json", None, env=env), "/data/desk/other/x.json")
        self.assertEqual(cscan.resolve_path("/abs/x.json", None, env=env), "/abs/x.json")

    def test_precedence_flag_then_environment_then_ini(self):
        env = {cscan.ENV_CONFIG_FILE: "/etc/x.ini", cscan.ENV_OUTPUT_DIR: "/o"}
        self.assertEqual(cscan.config_path("mine.ini", env=env), "mine.ini")
        self.assertEqual(cscan.config_path(None, env=env), "/etc/x.ini")

    def test_cli_writes_under_the_output_dir_with_the_environment_config(self):
        tmp = tempfile.mkdtemp()
        try:
            ini = os.path.join(tmp, "desk.ini")
            with open(INI) as fh, open(ini, "w") as out:
                out.write(fh.read().replace("path = trader/crypto_watchlist.json", "path = trader/crypto-scan-{market}.json"))
            env = dict(NO_ENV, TRADER_CONFIG_FILE=ini, TRADER_OUTPUT_DIR=os.path.join(tmp, "desk"))
            with unittest.mock.patch.dict(os.environ, env):
                code, text, err = run_cli(["crypto", "--offline", USDM, "--market", "usdm"])
            self.assertEqual(code, 0, err)
            path = os.path.join(tmp, "desk", "crypto-scan-usdm.json")
            self.assertIn("Wrote %s" % path, text)
            with open(path) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["config_source"], ini)
        finally:
            shutil.rmtree(tmp)

    def test_candles_are_copied_when_asked(self):
        with unittest.mock.patch.dict(os.environ, NO_ENV):
            d = offline_scan(SPOT, "spot", "[output]\ncandles = 30\n")
        tf = d["candidates"][0]["timeframes"]["1d"]
        self.assertEqual(len(tf["candles"]), 30)
        self.assertEqual(tf["candles"][-1][0] + D, cscan.parse_now_utc(tf["last_closed_close"]))
        self.assertEqual(d["config_fingerprint"], offline_scan(SPOT, "spot")["config_fingerprint"])

    def test_unmatched_markets_are_listed_apart_and_never_ranked(self):
        with unittest.mock.patch.dict(os.environ, NO_ENV):
            base = offline_scan(SPOT, "spot")
            d = offline_scan(SPOT, "spot", "[output]\nunmatched = true\ncandles = 5\n")
        self.assertNotIn("observed", base)
        self.assertEqual([(c["symbol"], c["rank"]) for c in d["candidates"]],
                         [(c["symbol"], c["rank"]) for c in base["candidates"]])
        self.assertTrue(d["observed"])
        self.assertTrue(all(o["matched_rules"] == 0 and "rank" not in o for o in d["observed"]))
        self.assertEqual(d["coverage"]["scanned"], len(d["candidates"]) + len(d["observed"]))
        with unittest.mock.patch.dict(os.environ, NO_ENV):
            some = offline_scan(SPOT, "spot", "[groups]\nmine = %s\n[output]\nunmatched = @mine, ZZZ\n"
                                % d["observed"][0]["asset_id"])
        self.assertEqual([o["asset_id"] for o in some["observed"]], [d["observed"][0]["asset_id"]])
        self.assertEqual(cscan.unmatched_filter(cfg("[output]\nunmatched = sui, BTCUSDT\n")), {"SUI", "BTCUSDT"})
        with self.assertRaises(cscan.CryptoScanError):
            cscan.unmatched_filter(cfg("[output]\nunmatched = @nope\n"))

    def test_the_scan_reports_only_its_own_rules(self):
        """No external model views: no evidence module, no evidence keys, and the text names no outside system."""
        self.assertFalse(os.path.exists(os.path.join(ROOT, "trader-scanner", "crypto_evidence.py")))
        with unittest.mock.patch.dict(os.environ, NO_ENV):
            d = offline_scan(SPOT, "spot")
        self.assertNotIn("evidence", d)
        self.assertTrue(all("evidence" not in c for c in d["candidates"]))
        self.assertEqual([c["symbol"] for c in d["candidates"]][:2], ["ZECUSDT", "ONDOUSDT"])

    def test_scanner_is_standalone_and_points_at_no_other_project(self):
        import ast
        import re
        import sys
        folder = os.path.join(ROOT, "trader-scanner")
        local = {f[:-3] for f in os.listdir(folder) if f.endswith(".py")}
        stdlib = set(getattr(sys, "stdlib_module_names", ())) or None
        for f in sorted(os.listdir(folder)):
            path = os.path.join(folder, f)
            if f.endswith(".py"):
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
                for node in ast.walk(tree):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                        [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                    for n in names:
                        top = n.split(".")[0]
                        self.assertTrue(top in local or stdlib is None or top in stdlib, "%s imports %s" % (f, n))
            if f.endswith((".py", ".ini", ".md")):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                self.assertIsNone(re.search(r"(/home/|/Users/|~/code)", text), "%s names a machine path" % f)
                # one-way dependency: other systems may read the scan; the scanner knows none of them
                self.assertIsNone(re.search(r"(?i)(gold.?digger|\bGD\b|trader-evidence|EVIDENCE_DIR)", text),
                                  "%s names an outside system" % f)


if __name__ == "__main__":
    unittest.main()
