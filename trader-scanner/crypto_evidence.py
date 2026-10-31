#!/usr/bin/env python3
"""External evidence for the crypto scan: read-only, standard library only, optional.

Any analytics system can put its own per-asset view next to the scan by dropping JSON files in
the evidence folder (TRADER_EVIDENCE_DIR, else [evidence] dir, default trader/evidence). The scan
copies each file's values onto the matching candidates, says how old they are against the scan's
own closed candles, and never changes the ranking. Without a folder or a file, nothing changes.

File contract, schema "trader-evidence", schema_version 1:

    {"schema": "trader-evidence", "schema_version": 1,
     "source": "my-models",                      # a short name; one file per source
     "generated_at": "2026-10-31T18:05:00Z",
     "assets": {"SUI": {"timeframes": {"1d": {
         "direction": 2,                         # sign: > 0 bullish, < 0 bearish, 0 neutral; null = no view
         "label": "STRONG BUY",                  # the source's own words, copied
         "decision_close": "2026-10-31T00:00:00Z",   # close of the candle the view was decided on
         "details": {...}}}}}}                   # anything else, copied verbatim

Per candidate and timeframe the state is one of:
  missing      the source has nothing for this asset or timeframe
  unavailable  the source lists it with direction null
  stale        decision_close is older than the scan's newest closed candle on that timeframe
  ahead        decision_close is newer than the scan's newest closed candle (clock or replay mismatch)
  current      only then is the direction compared with the scan: agrees / disagrees / neutral
Missing, unavailable and stale are never read as neutral or as disagreement.
"""

import datetime
import glob
import json
import os

SCHEMA = "trader-evidence"
SCHEMA_VERSIONS = (1,)
PERIOD_MS = {"1m": 60000, "5m": 300000, "15m": 900000, "30m": 1800000, "1h": 3600000, "2h": 7200000,
             "4h": 14400000, "1d": 86400000}


def _ms(text):
    if not text:
        return None
    dt = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def load_dir(folder):
    """-> (sources, notes). A file that is unreadable or of another schema is a note, never a source."""
    sources, notes = [], []
    if not folder:
        return sources, notes
    if not os.path.isdir(folder):
        return sources, ["no evidence folder at %s" % folder]
    for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            notes.append("%s: unreadable (%s)" % (os.path.basename(path), e))
            continue
        if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
            continue  # not an evidence file: other JSON may share the folder
        if doc.get("schema_version") not in SCHEMA_VERSIONS:
            notes.append("%s: incompatible schema_version %s (this reader takes %s)"
                         % (os.path.basename(path), doc.get("schema_version"), SCHEMA_VERSIONS))
            continue
        if not doc.get("source") or not isinstance(doc.get("assets"), dict):
            notes.append("%s: no source name or no assets" % os.path.basename(path))
            continue
        sources.append({"source": str(doc["source"]), "file": os.path.abspath(path),
                        "generated_at": doc.get("generated_at"), "assets": doc["assets"]})
    names = [s["source"] for s in sources]
    for n in sorted({n for n in names if names.count(n) > 1}):
        notes.append("source %s appears in more than one file; each is shown separately" % n)
    return sources, notes


def assess(view, tf, scan_close_ms, scanner_bullish):
    """One timeframe of one source against the scan's newest closed candle on that timeframe."""
    if view is None:
        return {"state": "missing"}
    out = {k: view.get(k) for k in ("direction", "label", "decision_close")}
    if view.get("details") is not None:
        out["details"] = view["details"]
    if view.get("direction") is None:
        out["state"] = "unavailable"
        return out
    close, period = _ms(view.get("decision_close")), PERIOD_MS.get(tf)
    if close is None or scan_close_ms is None or period is None:
        out["state"] = "unknown_age"
        return out
    behind = (scan_close_ms - close) // period if scan_close_ms >= close else -((close - scan_close_ms + period - 1) // period)
    out["candles_behind"] = behind
    out["state"] = "current" if behind == 0 else "stale" if behind > 0 else "ahead"
    if out["state"] == "current" and scanner_bullish:
        d = view["direction"]
        out["relation"] = "agrees" if d > 0 else "disagrees" if d < 0 else "neutral"
    return out


def annotate(candidates, sources):
    """Adds candidate['evidence'] = {source: {generated_at, file, timeframes: {tf: assessment}}}. Ranking unchanged."""
    for c in candidates:
        ev = {}
        for s in sources:
            asset = s["assets"].get(c["asset_id"]) or {}
            tfs = {}
            for tf, v in c["timeframes"].items():
                if tf.startswith("orb:") or v.get("status") != "ok":
                    continue
                tfs[tf] = assess((asset.get("timeframes") or {}).get(tf), tf, _ms(v.get("last_closed_close")),
                                 tf in c["bullish_timeframes"])
            ev[s["source"]] = {"generated_at": s["generated_at"], "file": s["file"], "timeframes": tfs}
        c["evidence"] = ev


def cell(ev):
    """Short text for the terminal: per source, per timeframe, the label and its state."""
    parts = []
    for name, s in sorted(ev.items()):
        tfs = []
        for tf, a in sorted(s["timeframes"].items()):
            st = a["state"]
            if st in ("missing", "unavailable", "unknown_age"):
                tfs.append("%s %s" % (tf, st))
            else:
                tfs.append("%s %s %s%s" % (tf, a.get("label") or a.get("direction"),
                                           "current" if st == "current" else "%s %+d" % (st, a["candles_behind"]),
                                           " " + a["relation"] if a.get("relation") else ""))
        parts.append("%s: %s" % (name, "; ".join(tfs)))
    return " | ".join(parts)
