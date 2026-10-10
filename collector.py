"""Undeployed MEXC public-data candidate. Never authenticates or submits orders.

Default CLI mode checks documented clock and contract metadata only. Full mode
is deliberate and uses standard documented OHLC fields, never ``real*`` fields.
This module has no scheduler, retries, alternate domains, or region selection.
"""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal, InvalidOperation
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from claim_hour import ClaimError, require_current_marker

BASE = "https://api.mexc.com"
SYMBOL = "BTC_USDT"
HOUR_MS = 3_600_000
FIVE_MS = 300_000
MAX_BODY = 1_048_576
MAX_SAFE_INT = 9_007_199_254_740_991
PRICE_FIELDS = ("open", "high", "low", "close", "vol")
DECIMAL_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


class DiagnosticError(Exception):
    """Only a constant, safe machine-readable reason may escape upstream IO."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _is_int(value):
    return type(value) is int


def _check_url(url: str):
    p = urlsplit(url)
    if p.scheme != "https" or p.netloc != "api.mexc.com" or p.fragment:
        raise DiagnosticError("url_not_allowlisted")
    q = parse_qs(p.query, keep_blank_values=True)
    if p.path == "/api/v1/contract/ping" and not q:
        return
    if p.path == "/api/v1/contract/detail/country" and q == {"symbol": [SYMBOL]}:
        return
    if p.path == "/api/v1/contract/kline/BTC_USDT":
        if set(q) != {"interval", "start", "end"}:
            raise DiagnosticError("url_not_allowlisted")
        if q["interval"] not in (["Min60"], ["Min5"]):
            raise DiagnosticError("url_not_allowlisted")
        if all(len(q[k]) == 1 and q[k][0].isdigit() for k in ("start", "end")):
            if 0 < int(q["start"][0]) <= int(q["end"][0]):
                return
    raise DiagnosticError("url_not_allowlisted")


def public_json(url: str):
    """One bounded official public request; redirects and env proxies disabled."""
    _check_url(url)
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "BTC-Public-Data-Candidate/1.0"})
    try:
        with opener.open(req, timeout=12) as response:
            if response.status != 200:
                raise DiagnosticError(f"http_{int(response.status)}")
            payload = response.read(MAX_BODY + 1)
    except HTTPError as e:
        raise DiagnosticError(f"http_{int(e.code)}") from None
    except (URLError, TimeoutError, OSError):
        raise DiagnosticError("network_error") from None
    if len(payload) > MAX_BODY:
        raise DiagnosticError("body_too_large")
    try:
        return json.loads(payload, parse_float=Decimal, parse_constant=lambda _: None)
    except (ValueError, UnicodeError):
        raise DiagnosticError("invalid_json") from None


def _call(fetch_json, endpoint, label):
    try:
        raw = fetch_json(BASE + endpoint)
    except DiagnosticError as e:
        # Only known reasons, never response bodies/headers/exception text.
        safe = re.fullmatch(r"http_[0-9]{3}|network_error|body_too_large|invalid_json|url_not_allowlisted", e.reason)
        raise DiagnosticError(f"{label}_{e.reason if safe else 'request_failed'}") from None
    except Exception:
        raise DiagnosticError(f"{label}_request_failed") from None
    if not isinstance(raw, dict):
        raise DiagnosticError(f"{label}_invalid_envelope")
    code = raw.get("code")
    if not _is_int(code):
        raise DiagnosticError(f"{label}_invalid_business_code")
    if code != 0:
        # Bound code values; do not include any upstream message string.
        suffix = str(code) if -1_000_000 <= code <= 1_000_000 else "invalid"
        raise DiagnosticError(f"{label}_business_{suffix}")
    if raw.get("success") is not True:
        raise DiagnosticError(f"{label}_not_success")
    return raw.get("data")


def _clock(fetch_json, wall_clock, label):
    value = _call(fetch_json, "/api/v1/contract/ping", label)
    wall = wall_clock()
    if not _is_int(value) or not 0 < value <= MAX_SAFE_INT:
        raise DiagnosticError(f"{label}_invalid_time")
    if not _is_int(wall) or abs(value - wall) > 120_000:
        raise DiagnosticError(f"{label}_clock_drift")
    return value


def _decimal(value, reason, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise DiagnosticError(reason)
    if isinstance(value, str) and not DECIMAL_RE.fullmatch(value):
        raise DiagnosticError(reason)
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise DiagnosticError(reason) from None
    if not d.is_finite() or d.is_signed() or (positive and d <= 0):
        raise DiagnosticError(reason)
    return d


def _contract(fetch_json):
    data = _call(fetch_json, "/api/v1/contract/detail/country?symbol=BTC_USDT", "contract")
    if not isinstance(data, dict):
        raise DiagnosticError("contract_expected_object")
    expected = {
        "symbol": SYMBOL, "baseCoin": "BTC", "quoteCoin": "USDT", "settleCoin": "USDT",
        "futureType": 1, "state": 0, "preMarket": False, "type": 1,
    }
    for key, value in expected.items():
        if key not in data or type(data[key]) is not type(value) or data[key] != value:
            raise DiagnosticError("contract_classification_failed")
    result = dict(expected)
    if "contractSize" in data:
        result["contractSize"] = format(_decimal(data["contractSize"], "contract_invalid_size", positive=True), "f")
    # apiAllowed describes trading permission and is deliberately not inferred.
    return result


def _range(period, interval, count, server):
    current = server // period * period
    query = urlencode({"interval": interval, "start": (current - count * period) // 1000, "end": server // 1000})
    return "/api/v1/contract/kline/BTC_USDT?" + query


def _candles(data, period, count, server, label):
    if not isinstance(data, dict) or any(not isinstance(data.get(k), list) for k in ("time", *PRICE_FIELDS)):
        raise DiagnosticError(f"{label}_invalid_arrays")
    size = len(data["time"])
    if not count <= size <= 2000 or any(len(data[k]) != size for k in PRICE_FIELDS):
        raise DiagnosticError(f"{label}_invalid_lengths")
    rows = []
    current_ms = server // period * period
    for index, seconds in enumerate(data["time"]):
        if not _is_int(seconds) or not 0 < seconds <= MAX_SAFE_INT // 1000 or seconds * 1000 % period:
            raise DiagnosticError(f"{label}_invalid_timestamp")
        if seconds * 1000 > current_ms:
            raise DiagnosticError(f"{label}_future_timestamp")
        values = [_decimal(data[k][index], f"{label}_invalid_decimal", positive=k != "vol") for k in PRICE_FIELDS]
        op, high, low, close, vol = values
        if low > min(op, close) or high < max(op, close) or low > high:
            raise DiagnosticError(f"{label}_invalid_ohlc")
        rows.append((seconds, values))
    # Upstream ordering is undocumented, so normalize order before continuity.
    rows.sort(key=lambda row: row[0])
    step = period // 1000
    if any(rows[i][0] - rows[i - 1][0] != step for i in range(1, len(rows))):
        raise DiagnosticError(f"{label}_gap_or_duplicate")
    selected = rows[-count:]
    if selected[-1][0] * 1000 != current_ms or selected[-2][0] * 1000 + period != current_ms:
        raise DiagnosticError(f"{label}_missing_current_or_stale")
    # Latest row is current, prior rows complete according to server clock.
    if sum(row[0] * 1000 + period <= server for row in selected) != count - 1:
        raise DiagnosticError(f"{label}_invalid_completion")
    clean = {"time": [row[0] for row in selected]}
    for j, key in enumerate(PRICE_FIELDS):
        clean[key] = [format(row[1][j], "f") for row in selected]
    return {"success": True, "code": 0, "data": clean}


def collect(*, mode="metadata", fetch_json=public_json, sleeper=time.sleep, wall_clock=lambda: int(time.time() * 1000)):
    """Return safe diagnostics separately from an allowlisted consumer snapshot."""
    if mode not in ("metadata", "full"):
        raise DiagnosticError("invalid_mode")
    s0 = _clock(fetch_json, wall_clock, "clock_initial")
    contract = _contract(fetch_json)
    summary = {"source": "mexc", "instId": SYMBOL, "mode": mode, "readyForProduction": False}
    if mode == "metadata":
        summary["status"] = "metadata_validated"
        return {"summary": summary}
    hour = _call(fetch_json, _range(HOUR_MS, "Min60", 721, s0), "hour")
    _candles(hour, HOUR_MS, 721, s0, "hour")
    # Explicit spacing independent of HTTP latency; no retry on any failure.
    sleeper(2.1)
    five = _call(fetch_json, _range(FIVE_MS, "Min5", 300, s0), "five")
    _candles(five, FIVE_MS, 300, s0, "five")
    s1 = _clock(fetch_json, wall_clock, "clock_final")
    if s1 < s0:
        raise DiagnosticError("clock_nonmonotonic")
    hour_clean = _candles(hour, HOUR_MS, 721, s1, "hour")
    five_clean = _candles(five, FIVE_MS, 300, s1, "five")
    generated = dt.datetime.fromtimestamp(wall_clock() / 1000, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    snapshot = {
        "schemaVersion": 1, "source": "mexc", "instId": SYMBOL, "generatedAt": generated,
        "clock": {"success": True, "code": 0, "data": s1},
        "hour": hour_clean, "five": five_clean,
        "contract": {"success": True, "code": 0, "data": contract},
    }
    summary["status"] = "validated_public_data_candidate"
    summary["hourCompleted"] = 720
    summary["fiveCompleted"] = 299
    summary["completionBasis"] = "derived_from_public_server_time"
    return {"summary": summary, "snapshot": snapshot}


def now_ms():
    return time.time_ns() // 1_000_000


def write_snapshot(snapshot, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                         prefix=".snapshot-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(snapshot, handle, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("metadata", "full"), default="metadata")
    parser.add_argument("--output", default="snapshots/latest.json")
    parser.add_argument("--required-attempt-marker", required=True)
    args = parser.parse_args(argv)
    try:
        # Both metadata and full diagnostics consume the persistent hour claim.
        # Failure keeps this claim; no same-hour mode switch or market retry.
        require_current_marker(args.required_attempt_marker, now_ms())
        result = collect(mode=args.mode)
        if args.mode == "full":
            write_snapshot(result["snapshot"], args.output)
    except (DiagnosticError, ClaimError) as e:
        print(json.dumps({"status": "failed", "reason": str(e), "readyForProduction": False}))
        return 1
    except OSError:
        print(json.dumps({"status": "failed", "reason": "snapshot_write_failed", "readyForProduction": False}))
        return 1
    # Only diagnostics go to logs; the public market snapshot is file-only.
    print(json.dumps(result["summary"], separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
