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

    def test_rvol_direction(self):
        closes = [100.0] * 30
        vols = [1000.0] * 29 + [5000.0]
        hits = cs.bar_setups(self.mk(closes[:-1] + [102.0], vols=vols), ["rvol_expansion"], self.P)
        self.assertEqual(hits[0]["direction"], "bullish")
        self.assertEqual(hits[0]["evidence"]["rvol"], 5.0)
        hits = cs.bar_setups(self.mk(closes[:-1] + [98.0], vols=vols), ["rvol_expansion"], self.P)
        self.assertEqual(hits[0]["direction"], "bearish")

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
        self.assertEqual([c["symbol"] for c in a["candidates"]],
                         ["ZECUSDT", "ONDOUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "INJUSDT", "PENGUUSDT", "PAXGUSDT"])
        top = a["candidates"][0]
        self.assertTrue(top["aligned"])
        self.assertEqual([h["id"] for h in top["timeframes"]["1h"]["setups"]], ["breakout_close_high"])

    def test_futures_example_carries_derivatives_context(self):
        d = offline_scan(USDM, "usdm")
        self.assertEqual(d["candidates"][0]["symbol"], "HYPEUSDT")
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
        self.assertEqual((d["schema"], d["schema_version"]), ("day-trader-skill/crypto-scan", 1))
        for c in d["candidates"]:
            for k in ("rank", "instrument_id", "exchange", "market", "symbol", "asset_id", "last_price",
                      "price_time", "price_age_seconds", "quote_volume_24h", "timeframes", "aligned",
                      "descriptive_score", "estimated_probability"):
                self.assertIn(k, c)
            self.assertIsNone(c["estimated_probability"])
            for tf, v in c["timeframes"].items():
                self.assertTrue(v["last_closed_close"].endswith("Z"))
                self.assertLessEqual(v["last_closed_close"], d["created_at"])
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


# ---------------------------------------------------------------- paths and external evidence

ce = cscan.ce
NO_ENV = {k: "" for k in (cscan.ENV_CONFIG_FILE, cscan.ENV_OUTPUT_DIR, cscan.ENV_EVIDENCE_DIR)}


def evidence_file(folder, name, assets, version=1, source="ext"):
    with open(os.path.join(folder, name), "w") as fh:
        json.dump({"schema": "trader-evidence", "schema_version": version, "source": source,
                   "generated_at": "2026-10-31T13:00:00Z", "assets": assets}, fh)


class PathsAndEvidence(unittest.TestCase):
    def test_paths_without_environment_are_unchanged(self):
        self.assertEqual(cscan.resolve_path("trader/crypto_watchlist.json", "spot", env={}), "trader/crypto_watchlist.json")
        self.assertEqual(cscan.resolve_path("trader/scan-{market}.json", "usdm", env={}), "trader/scan-usdm.json")
        self.assertEqual(cscan.evidence_dir(cfg(), env={}), ("trader/evidence", False))
        self.assertIsNone(cscan.config_path(None, env={}) if not os.path.exists(cscan.DEFAULT_CONFIG) else None)

    def test_output_dir_moves_the_trader_folder_but_not_absolute_paths(self):
        env = {cscan.ENV_OUTPUT_DIR: "/data/desk"}
        self.assertEqual(cscan.resolve_path("trader/crypto-scan-{market}.json", "spot", env=env),
                         "/data/desk/crypto-scan-spot.json")
        self.assertEqual(cscan.resolve_path("trader/cache/binance", None, env=env), "/data/desk/cache/binance")
        self.assertEqual(cscan.resolve_path("other/x.json", None, env=env), "/data/desk/other/x.json")
        self.assertEqual(cscan.resolve_path("/abs/x.json", None, env=env), "/abs/x.json")
        self.assertEqual(cscan.evidence_dir(cfg(), env=env), ("/data/desk/evidence", False))

    def test_precedence_flag_then_environment_then_ini(self):
        env = {cscan.ENV_CONFIG_FILE: "/etc/x.ini", cscan.ENV_EVIDENCE_DIR: "/ev", cscan.ENV_OUTPUT_DIR: "/o"}
        self.assertEqual(cscan.config_path("mine.ini", env=env), "mine.ini")
        self.assertEqual(cscan.config_path(None, env=env), "/etc/x.ini")
        self.assertEqual(cscan.evidence_dir(cfg("[evidence]\ndir = /ini/ev\n"), env=env), ("/ev", True))
        self.assertEqual(cscan.evidence_dir(cfg("[evidence]\ndir = /ini/ev\n"), env={}), ("/ini/ev", True))

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
            self.assertEqual((doc["config_source"], doc["evidence"]["dir"]), (ini, os.path.join(tmp, "desk", "evidence")))
            self.assertEqual(doc["evidence"]["sources"], [])
        finally:
            shutil.rmtree(tmp)

    def test_standalone_scan_has_no_evidence_and_the_same_ranking(self):
        with unittest.mock.patch.dict(os.environ, NO_ENV):
            d = offline_scan(SPOT, "spot")
        self.assertEqual(d["evidence"], {"dir": "trader/evidence", "sources": [], "notes": []})
        self.assertTrue(all(c["evidence"] == {} for c in d["candidates"]))
        self.assertTrue(all("candles" not in v for c in d["candidates"] for v in c["timeframes"].values()))

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

    def test_evidence_states_and_ranking_unchanged(self):
        with unittest.mock.patch.dict(os.environ, NO_ENV):
            base = offline_scan(SPOT, "spot")
        first = base["candidates"][0]
        a, close_1d = first["asset_id"], first["timeframes"]["1d"]["last_closed_close"]
        close_1h = first["timeframes"]["1h"]["last_closed_close"]
        b = base["candidates"][1]["asset_id"]
        tmp = tempfile.mkdtemp()
        try:
            evidence_file(tmp, "a.json", {
                a: {"timeframes": {"1d": {"direction": -1, "label": "SELL", "decision_close": close_1d,
                                          "details": {"win_rate": 0.55, "trades": 40}},
                                   "1h": {"direction": 2, "label": "STRONG BUY", "decision_close": "2026-10-31T09:00:00Z"}}},
                b: {"timeframes": {"1d": {"direction": None, "label": "UNAVAILABLE", "decision_close": close_1d}}}})
            evidence_file(tmp, "old.json", {}, version=7, source="future")
            with open(os.path.join(tmp, "other.json"), "w") as fh:
                json.dump({"not": "evidence"}, fh)
            with unittest.mock.patch.dict(os.environ, dict(NO_ENV, TRADER_EVIDENCE_DIR=tmp)):
                d = offline_scan(SPOT, "spot")
        finally:
            shutil.rmtree(tmp)
        self.assertEqual([c["symbol"] for c in d["candidates"]], [c["symbol"] for c in base["candidates"]])
        self.assertEqual([s["source"] for s in d["evidence"]["sources"]], ["ext"])
        self.assertEqual(len(d["evidence"]["notes"]), 1)
        self.assertIn("incompatible schema_version 7", d["evidence"]["notes"][0])
        ev = d["candidates"][0]["evidence"]["ext"]["timeframes"]
        self.assertEqual(ev["1d"]["state"], "current")
        self.assertEqual(ev["1d"]["details"], {"win_rate": 0.55, "trades": 40})
        self.assertEqual(ev["1d"].get("relation"), "disagrees" if "1d" in first["bullish_timeframes"] else None)
        behind = (cscan.parse_now_utc(close_1h) - cscan.parse_now_utc("2026-10-31T09:00:00Z")) // H
        self.assertEqual((ev["1h"]["state"], ev["1h"]["candles_behind"]), ("stale", behind))
        self.assertNotIn("relation", ev["1h"])
        ev_b = d["candidates"][1]["evidence"]["ext"]["timeframes"]
        self.assertEqual((ev_b["1d"]["state"], ev_b["1h"]["state"]), ("unavailable", "missing"))
        self.assertTrue(all(c["evidence"]["ext"]["timeframes"]["1d"]["state"] == "missing"
                            for c in d["candidates"][2:] if c["asset_id"] not in (a, b)))
        self.assertIn("ext: 1d SELL current", cscan.render(d, 1))

    def test_assess_is_never_neutral_when_missing_or_stale(self):
        self.assertEqual(ce.assess(None, "1h", 0, True), {"state": "missing"})
        stale = ce.assess({"direction": -2, "decision_close": "2026-10-31T00:00:00Z"}, "1h", T0 + 3 * H, True)
        self.assertEqual((stale["state"], stale["candles_behind"], "relation" in stale), ("stale", 3, False))
        ahead = ce.assess({"direction": 1, "decision_close": "2026-10-31T02:00:00Z"}, "1h", T0, True)
        self.assertEqual((ahead["state"], ahead["candles_behind"]), ("ahead", -2))
        neutral = ce.assess({"direction": 0, "decision_close": "2026-10-31T00:00:00Z"}, "1d", T0, True)
        self.assertEqual(neutral["relation"], "neutral")

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


if __name__ == "__main__":
    unittest.main()
