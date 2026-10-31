"""Market-data providers for the crypto scan (trader-scanner). Standard library only.

A provider answers four questions and nothing else:

    server_time_ms()        what time is it on the exchange (closed-candle semantics hang on this)
    instruments()           which markets exist: symbol, base, quote, status, contract type
    tickers()               24h liquidity and the last price, per symbol
    klines(sym, tf, bars)   OHLCV candles, oldest first (raw: the caller normalizes them)

Futures providers also answer derivatives_context(symbols).

Public market data only. Nothing here signs a request, reads an API key or
touches an order endpoint; there is no code path to one.

Transports are swappable: LiveTransport (urllib), FixtureTransport (a folder
recorded earlier, replayed deterministically, including Binance's
startTime / endTime / limit kline semantics) and RecordingTransport (live,
saving what it saw in the fixture layout).
"""

import gzip
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple

UA = "day-trader-skill/2.0 (public market data; +https://github.com/bravegag/day-trader-skill)"

INTERVAL_MS = {
    "1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000, "30m": 1800000,
    "1h": 3600000, "2h": 7200000, "4h": 14400000, "6h": 21600000, "8h": 28800000,
    "12h": 43200000, "1d": 86400000,
}
# 1w and 1M are left out on purpose: weeks open on Monday and months are not a fixed length,
# so the grid and closed-candle checks below would need their own rules.

# A futures base asset can carry a size multiplier: 1000PEPE is 1,000 PEPE. Spot lists some
# tokens in the same denomination (1000SATS, 1MBABYDOGE). The asset identity strips it.
MULTIPLIER = re.compile(r"^(1000000|1000|1M)(?=[A-Z])")
MULT_VALUE = {"1000000": 1000000, "1000": 1000, "1M": 1000000}

Instrument = namedtuple("Instrument", "instrument_id exchange market symbol base quote asset_id multiplier "
                                      "status contract_type underlying_type onboard_ms tags")
Ticker = namedtuple("Ticker", "symbol last_price quote_volume volume trades price_change_pct bid ask close_ms")
Candle = namedtuple("Candle", "open_ms close_ms open high low close volume quote_volume trades")


class ProviderError(Exception):
    pass


class HttpError(ProviderError):
    def __init__(self, status, msg, code=None, retry_after=None):
        ProviderError.__init__(self, "HTTP %s: %s" % (status, msg))
        self.status, self.code, self.retry_after = status, code, retry_after


class BannedError(ProviderError):
    """HTTP 418: Binance has banned the IP for ignoring 429s. Stop everything."""


def asset_identity(base):
    """'1000PEPE' -> ('PEPE', 1000); 'BTC' -> ('BTC', 1); '1INCH' -> ('1INCH', 1)."""
    m = MULTIPLIER.match(base or "")
    if not m:
        return base, 1
    return base[m.end():], MULT_VALUE[m.group(1)]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- transports

class LiveTransport(object):
    def __init__(self, timeout=20):
        self.timeout = timeout

    def get(self, base, path, params):
        url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read().decode("utf-8", "replace")
                headers = {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            try:
                text = e.read().decode("utf-8", "replace")
            except Exception:
                text = ""
            hdr = {k.lower(): v for k, v in (e.headers or {}).items()}
            e.close()
            code, msg = None, text[:200] or e.reason
            try:
                doc = json.loads(text)
                code, msg = doc.get("code"), doc.get("msg", msg)
            except (ValueError, AttributeError):
                pass
            ra = hdr.get("retry-after")
            raise HttpError(e.code, msg, code, float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
        except (urllib.error.URLError, OSError) as e:
            raise HttpError(0, "network: %s" % getattr(e, "reason", e))
        try:
            return json.loads(body), headers
        except ValueError:
            raise HttpError(200, "not JSON from %s" % path)


def _read_json(path):
    for p in (path, path + ".gz"):
        if os.path.exists(p):
            if p.endswith(".gz"):
                with gzip.open(p, "rt", encoding="utf-8") as fh:
                    return json.load(fh)
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
    return None


def fixture_name(path, params):
    """API path -> file in a fixture folder. One rule shared by recording and replay."""
    leaf = path.rstrip("/").split("/")[-1]
    if leaf == "klines":
        return os.path.join("klines", "%s_%s.json" % (params["symbol"], params["interval"]))
    if leaf == "openInterest":
        return os.path.join("openInterest", "%s.json" % params["symbol"])
    if leaf == "24hr":
        return "ticker_24hr.json"
    return leaf + ".json"  # exchangeInfo, time, bookTicker, premiumIndex


def slice_klines(rows, params):
    """Binance's kline window rules, for replay: startTime / endTime are on the OPEN time,
    and with no startTime the answer is the LAST `limit` rows up to endTime."""
    limit = int(params.get("limit", 500))
    start, end = params.get("startTime"), params.get("endTime")
    sel = [r for r in rows if (start is None or r[0] >= int(start)) and (end is None or r[0] <= int(end))]
    return sel[:limit] if start is not None else sel[-limit:]


class FixtureTransport(object):
    """Replays a recorded folder. Unknown symbols answer like Binance (-1121, HTTP 400)."""

    def __init__(self, folder, now_ms=None):
        if not os.path.isdir(folder):
            raise ProviderError("no offline folder at %s" % folder)
        self.folder = folder
        self.meta = _read_json(os.path.join(folder, "meta.json")) or {}
        # the exchange clock is the recorded one; --now (a replay) is applied by the scan, not here
        self.now_ms = self.meta.get("server_time_ms") if self.meta.get("server_time_ms") is not None else now_ms
        self.calls = []

    def get(self, base, path, params):
        params = dict(params or {})
        self.calls.append((path, params))
        leaf = path.rstrip("/").split("/")[-1]
        if leaf == "time":
            if self.now_ms is None:
                raise HttpError(0, "offline: no server time in meta.json; pass --now")
            return {"serverTime": int(self.now_ms)}, {}
        doc = _read_json(os.path.join(self.folder, fixture_name(path, params)))
        if leaf == "klines":
            if doc is None:
                raise HttpError(400, "Invalid symbol.", -1121)
            return slice_klines(doc, params), {}
        if doc is None:
            if leaf == "openInterest":
                raise HttpError(400, "Invalid symbol.", -1121)
            raise HttpError(0, "offline: %s has no %s" % (self.folder, fixture_name(path, params)))
        return doc, {}


class RecordingTransport(object):
    """Live, and keeps what it saw. save() writes the fixture layout (klines merged by open time)."""

    def __init__(self, inner, folder):
        self.inner, self.folder, self.lock = inner, folder, threading.Lock()
        self.docs, self.klines = {}, {}

    def get(self, base, path, params):
        doc, headers = self.inner.get(base, path, params)
        name = fixture_name(path, params or {})
        with self.lock:
            if name.startswith("klines"):
                merged = self.klines.setdefault(name, {})
                for r in doc:
                    merged[r[0]] = r
            else:
                self.docs[name] = doc
        return doc, headers

    def save(self, server_time_ms, keep_symbols=None, extra_meta=None):
        keep = set(keep_symbols or [])
        for name, doc in self.docs.items():
            if keep and name == "exchangeInfo.json":
                doc = dict(doc, symbols=[s for s in doc["symbols"] if s["symbol"] in keep])
            elif keep and isinstance(doc, list) and doc and isinstance(doc[0], dict) and "symbol" in doc[0]:
                doc = [d for d in doc if d["symbol"] in keep]
            self._write(name, doc)
        for name, rows in self.klines.items():
            self._write(name, [rows[k] for k in sorted(rows)])
        meta = {"server_time_ms": server_time_ms, "recorded_at_ms": int(time.time() * 1000)}
        meta.update(extra_meta or {})
        self._write("meta.json", meta)

    def _write(self, name, doc):
        path = os.path.join(self.folder, name)
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"))


# ---------------------------------------------------------------- rate limiting

class WeightBudget(object):
    """Request-weight budget per wall-clock minute, shared by every worker thread.

    Binance counts REQUEST_WEIGHT in 1-minute buckets and reports the bucket in
    X-MBX-USED-WEIGHT-1M. We spend at most `safety` of the published limit, trust the
    server's count when it is higher than ours, and stop everyone after a 429."""

    def __init__(self, limit, safety=0.6, clock=time.time, sleep=time.sleep):
        self.cap = max(1, int(limit * safety))
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.window, self.used, self.blocked_until, self.waited = None, 0, 0.0, 0.0

    def acquire(self, weight):
        with self.lock:
            while True:
                now = self.clock()
                if now < self.blocked_until:
                    self._wait(self.blocked_until - now)
                    continue
                win = int(now // 60)
                if win != self.window:
                    self.window, self.used = win, 0
                if self.used + weight <= self.cap or self.used == 0:
                    self.used += weight
                    return
                self._wait((win + 1) * 60 - now + 0.25)

    def _wait(self, seconds):
        self.waited += seconds
        self.sleep(seconds)

    def observe(self, used_1m):
        if used_1m is None:
            return
        with self.lock:
            if self.window == int(self.clock() // 60):
                self.used = max(self.used, int(used_1m))

    def block(self, seconds):
        with self.lock:
            self.blocked_until = max(self.blocked_until, self.clock() + seconds)


class Client(object):
    """One market's HTTP client: weight budget, retries with exponential backoff and jitter.

    429 -> wait Retry-After (or backoff) and pause every worker; 418 -> BannedError at once;
    5xx and network errors -> backoff and retry; any other 4xx is final (bad symbol, bad interval)."""

    def __init__(self, bases, transport, weight_limit, safety=0.6, retries=4, backoff=1.0, max_backoff=30.0,
                 clock=time.time, sleep=time.sleep, rng=None):
        self.bases = list(bases)
        self.transport = transport
        self.budget = WeightBudget(weight_limit, safety, clock, sleep)
        self.retries, self.backoff, self.max_backoff = retries, backoff, max_backoff
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.stats = {"requests": 0, "weight": 0, "retries": 0, "http_429": 0, "http_5xx": 0, "network": 0}
        self.lock = threading.Lock()

    def _delay(self, attempt):
        return min(self.max_backoff, self.backoff * (2 ** attempt)) * (0.5 + self.rng.random() / 2)

    def get(self, path, params=None, weight=1):
        last = None
        for attempt in range(self.retries + 1):
            base = self.bases[min(attempt, len(self.bases) - 1)] if attempt else self.bases[0]
            self.budget.acquire(weight)
            with self.lock:
                self.stats["requests"] += 1
                self.stats["weight"] += weight
            try:
                doc, headers = self.transport.get(base, path, params or {})
            except HttpError as e:
                last = e
                if e.status == 418:
                    raise BannedError("Binance answered 418 (IP banned for ignoring rate limits): stopped")
                if e.status == 429:
                    delay = e.retry_after if e.retry_after else self._delay(attempt)
                    self.budget.block(delay)
                    key = "http_429"
                elif e.status >= 500:
                    delay, key = self._delay(attempt), "http_5xx"
                elif e.status == 0:
                    delay, key = self._delay(attempt), "network"
                else:
                    raise
                with self.lock:
                    self.stats[key] += 1
                if attempt == self.retries:
                    break
                with self.lock:
                    self.stats["retries"] += 1
                if e.status != 429:
                    self.sleep(delay)
                continue
            self.budget.observe(_header_weight(headers))
            return doc
        raise last


def _header_weight(headers):
    for k in ("x-mbx-used-weight-1m", "x-mbx-used-weight"):
        v = (headers or {}).get(k)
        if v is not None:
            try:
                return int(v)
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------- providers

class MarketDataProvider(object):
    """The contract every market-discovery provider implements."""
    exchange = None
    market = None

    def server_time_ms(self):
        raise NotImplementedError

    def instruments(self):
        raise NotImplementedError

    def tickers(self):
        raise NotImplementedError

    def klines(self, symbol, interval, bars, end_ms=None):
        raise NotImplementedError

    def derivatives_context(self, symbols):
        return {}


class _Binance(MarketDataProvider):
    exchange = "binance"
    PREFIX = ""
    MAX_LIMIT = 1000
    WEIGHT_LIMIT = 6000
    W_EXCHANGE_INFO = 20
    W_TICKERS = 80

    def __init__(self, client):
        self.client = client

    def kline_weight(self, limit):
        return 2

    def server_time_ms(self):
        return int(self.client.get(self.PREFIX + "/time", weight=1)["serverTime"])

    def _exchange_info(self):
        doc = self.client.get(self.PREFIX + "/exchangeInfo", weight=self.W_EXCHANGE_INFO)
        if not isinstance(doc, dict) or not isinstance(doc.get("symbols"), list):
            raise ProviderError("exchangeInfo is not the shape this tool knows")
        return doc

    def klines(self, symbol, interval, bars, end_ms=None):
        """The last `bars` candles with open time <= end_ms, paging backwards. Raw rows, oldest first.
        Stops early at the listing date (an answer shorter than asked for)."""
        if interval not in INTERVAL_MS:
            raise ProviderError("interval %s is not supported (use one of %s)" % (interval, ", ".join(INTERVAL_MS)))
        out, cursor, want = [], end_ms, int(bars)
        while want > 0:
            limit = min(want, self.MAX_LIMIT)
            params = {"symbol": symbol, "interval": interval, "limit": limit}
            if cursor is not None:
                params["endTime"] = int(cursor)
            rows = self.client.get(self.PREFIX + "/klines", params, weight=self.kline_weight(limit))
            if not isinstance(rows, list):
                raise ProviderError("klines for %s is not a list" % symbol)
            out = rows + out
            want -= len(rows)
            if len(rows) < limit or not rows:
                break
            cursor = int(rows[0][0]) - 1
        return out

    @staticmethod
    def _ticker(d, bid=None, ask=None):
        return Ticker(d["symbol"], _f(d.get("lastPrice")), _f(d.get("quoteVolume")), _f(d.get("volume")),
                      int(d.get("count") or 0), _f(d.get("priceChangePercent")),
                      _f(d.get("bidPrice")) if bid is None else bid, _f(d.get("askPrice")) if ask is None else ask,
                      int(d.get("closeTime") or 0))


class BinanceSpotProvider(_Binance):
    market = "spot"
    PREFIX = "/api/v3"
    # data-api.binance.vision serves public market data only (Binance's own docs point there for it)
    BASES = ("https://data-api.binance.vision", "https://api.binance.com", "https://api1.binance.com")

    def instruments(self):
        out = []
        for s in self._exchange_info()["symbols"]:
            asset, mult = asset_identity(s.get("baseAsset", ""))
            status = s.get("status", "")
            if status == "TRADING" and s.get("isSpotTradingAllowed") is False:
                status = "SPOT_TRADING_NOT_ALLOWED"
            out.append(Instrument("binance:spot:" + s["symbol"], "binance", "spot", s["symbol"], s.get("baseAsset"),
                                  s.get("quoteAsset"), asset, mult, status, "SPOT", None, None, ()))
        return out

    def tickers(self):
        rows = self.client.get(self.PREFIX + "/ticker/24hr", weight=self.W_TICKERS)
        return {d["symbol"]: self._ticker(d) for d in rows}


class BinanceFuturesProvider(_Binance):
    market = "usdm"
    PREFIX = "/fapi/v1"
    BASES = ("https://fapi.binance.com",)
    MAX_LIMIT = 1500
    WEIGHT_LIMIT = 2400
    W_EXCHANGE_INFO = 1
    W_TICKERS = 40

    def kline_weight(self, limit):
        return 1 if limit < 100 else 2 if limit < 500 else 5 if limit <= 1000 else 10

    def instruments(self):
        out = []
        for s in self._exchange_info()["symbols"]:
            asset, mult = asset_identity(s.get("baseAsset", ""))
            out.append(Instrument("binance:usdm:" + s["symbol"], "binance", "usdm", s["symbol"], s.get("baseAsset"),
                                  s.get("quoteAsset"), asset, mult, s.get("status", ""), s.get("contractType", ""),
                                  s.get("underlyingType"), s.get("onboardDate"), tuple(s.get("underlyingSubType") or ())))
        return out

    def tickers(self):
        rows = self.client.get(self.PREFIX + "/ticker/24hr", weight=self.W_TICKERS)
        try:
            book = {d["symbol"]: d for d in self.client.get(self.PREFIX + "/ticker/bookTicker", weight=5)}
        except HttpError:
            book = {}  # spread is then unknown, not zero
        out = {}
        for d in rows:
            b = book.get(d["symbol"]) or {}
            out[d["symbol"]] = self._ticker(d, _f(b.get("bidPrice")), _f(b.get("askPrice")))
        return out

    def derivatives_context(self, symbols, max_requests=40):
        """Mark / index / funding for everyone (one request), open interest for up to max_requests symbols.
        Context only: no leverage or execution advice is derived from it."""
        out = {}
        try:
            prem = {d["symbol"]: d for d in self.client.get(self.PREFIX + "/premiumIndex", weight=10)}
        except HttpError as e:
            return {s: {"error": "premiumIndex: %s" % e} for s in symbols}
        for k, sym in enumerate(symbols):
            p = prem.get(sym)
            if not p:
                out[sym] = {"error": "no premium index"}
                continue
            mark, index = _f(p.get("markPrice")), _f(p.get("indexPrice"))
            ctx = {"mark_price": mark, "index_price": index,
                   "basis_bps": round((mark / index - 1) * 1e4, 2) if mark and index else None,
                   "funding_rate_last": _f(p.get("lastFundingRate")),
                   "next_funding_time": _iso(p.get("nextFundingTime")), "as_of": _iso(p.get("time"))}
            if k < max_requests:
                try:
                    oi = self.client.get(self.PREFIX + "/openInterest", {"symbol": sym}, weight=1)
                    q = _f(oi.get("openInterest"))
                    ctx["open_interest"] = q
                    ctx["open_interest_notional"] = round(q * mark, 2) if q is not None and mark else None
                except HttpError as e:
                    ctx["open_interest_error"] = str(e)
            out[sym] = ctx
        return out


def _iso(ms):
    if ms in (None, "", 0):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ms) / 1000.0))


PROVIDERS = {("binance", "spot"): BinanceSpotProvider, ("binance", "usdm"): BinanceFuturesProvider,
             ("binance", "futures"): BinanceFuturesProvider}


def make_provider(name, market, transport, safety=0.6, retries=4, backoff=1.0, timeout=20,
                  clock=time.time, sleep=time.sleep, rng=None):
    cls = PROVIDERS.get(((name or "").lower(), (market or "").lower()))
    if cls is None:
        raise ProviderError("unknown provider/market %s/%s (binance with spot or usdm)" % (name, market))
    client = Client(cls.BASES, transport, cls.WEIGHT_LIMIT, safety, retries, backoff,
                    clock=clock, sleep=sleep, rng=rng)
    return cls(client)


# ---------------------------------------------------------------- normalization

def normalize_klines(rows, interval, now_ms):
    """Raw kline rows -> (closed candles oldest first, quality report).

    Dropped and counted, never silently fixed: rows that do not parse or break OHLC
    sanity, rows off the interval grid, duplicates (the last copy wins), candles still
    open at now_ms (open + interval > now) and anything after now_ms (no look-ahead
    in a replay). Unordered input is sorted. Gaps are counted, not filled."""
    step = INTERVAL_MS[interval]
    q = {"received": len(rows or []), "invalid": 0, "off_grid": 0, "duplicates": 0, "unordered": False,
         "incomplete_dropped": 0, "future_dropped": 0, "missing_bars": 0, "zero_volume_bars": 0}
    by_open, prev = {}, None
    for r in rows or []:
        try:
            c = Candle(int(r[0]), int(r[6]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]),
                       float(r[7]), int(r[8]))
        except (TypeError, ValueError, IndexError):
            q["invalid"] += 1
            continue
        if (c.high < max(c.open, c.close, c.low) or c.low > min(c.open, c.close) or c.low <= 0
                or c.volume < 0 or c.quote_volume < 0):
            q["invalid"] += 1
            continue
        if c.open_ms % step:
            q["off_grid"] += 1
            continue
        if prev is not None and c.open_ms < prev:
            q["unordered"] = True
        prev = c.open_ms
        if c.open_ms in by_open:
            q["duplicates"] += 1
        by_open[c.open_ms] = c
    out = []
    for k in sorted(by_open):
        c = by_open[k]
        if c.open_ms > now_ms:
            q["future_dropped"] += 1
        elif c.open_ms + step > now_ms:
            q["incomplete_dropped"] += 1
        else:
            out.append(c)
    for a, b in zip(out, out[1:]):
        q["missing_bars"] += (b.open_ms - a.open_ms) // step - 1
    q["zero_volume_bars"] = sum(1 for c in out if c.volume == 0)
    q["closed"] = len(out)
    return out, q


def expected_last_open(interval, now_ms):
    """Open time of the newest candle that is closed at now_ms."""
    step = INTERVAL_MS[interval]
    return (now_ms // step) * step - step
