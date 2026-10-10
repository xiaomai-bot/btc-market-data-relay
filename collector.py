#!/usr/bin/env python3
"""Collect one public OKX BTC perpetual snapshot; no accounts or credentials."""

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from claim_hour import ClaimError, require_current_marker

BASE = "https://openapi.okx.com"
INST_ID = "BTC-USDT-SWAP"
HOUR = 3_600_000
FIVE = 300_000
MAX_DRIFT_MS = 120_000
MAX_BODY_BYTES = 1_048_576
PATHS = (
    ("/api/v5/market/history-candles?instId=BTC-USDT-SWAP&bar=1H&limit=300", "hour"),
    ("/api/v5/market/history-candles?instId=BTC-USDT-SWAP&bar=5m&limit=300", "five"),
    ("/api/v5/public/time", "clock"),
    ("/api/v5/public/instruments?instType=SWAP&instId=BTC-USDT-SWAP", "contract"),
)
CONTRACT_FIELDS = {
    "instId": INST_ID,
    "instType": "SWAP",
    "ctType": "linear",
    "settleCcy": "USDT",
    "state": "live",
}


class CollectorError(Exception):
    """A constant, safe diagnostic; never includes an upstream body or header."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def now_ms():
    return time.time_ns() // 1_000_000


def require_ok(page, label):
    if not isinstance(page, dict):
        raise CollectorError(f"{label}_invalid_response")
    if page.get("code") != "0":
        raw = page.get("code")
        code = raw if isinstance(raw, str) and re.fullmatch(r"[0-9]{5}", raw) else "unknown"
        raise CollectorError(f"{label}_business_code_{code}")
    return page


def public_json(path, label):
    # Only these four fixed paths are permitted. Redirects and retries are absent.
    if (path, label) not in PATHS:
        raise CollectorError("unapproved_public_endpoint")
    request = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=12) as response:
            if response.status != 200:
                raise CollectorError(f"{label}_http_{response.status}")
            body = response.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                raise CollectorError(f"{label}_body_too_large")
        page = json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as error:
        # In particular, HTTP429 stops here without another request or body logging.
        raise CollectorError(f"{label}_http_{error.code}") from None
    except CollectorError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CollectorError(f"{label}_network_error") from None
    except (UnicodeError, ValueError):
        raise CollectorError(f"{label}_invalid_json") from None
    return require_ok(page, label)


def timestamp(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", value):
        raise CollectorError(f"{label}_invalid_timestamp")
    result = int(value)
    if result > 8_640_000_000_000_000:
        raise CollectorError(f"{label}_invalid_timestamp")
    return result


def number(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
        raise CollectorError(f"{label}_invalid_number")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise CollectorError(f"{label}_invalid_number") from None
    if not result.is_finite():
        raise CollectorError(f"{label}_invalid_number")
    return result


def validate_clock(page, wall):
    rows = page.get("data")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise CollectorError("clock_invalid_schema")
    exchange = timestamp(rows[0].get("ts"), "clock")
    if not isinstance(wall, int) or abs(exchange - wall) > MAX_DRIFT_MS:
        raise CollectorError("clock_stale")
    return exchange, {"code": "0", "data": [{"ts": str(exchange)}]}


def validate_candles(page, period, exchange, label):
    raw = page.get("data")
    if not isinstance(raw, list) or len(raw) != 300:
        raise CollectorError(f"{label}_invalid_page_size")
    rows = []
    for row in raw:
        if not isinstance(row, list) or len(row) != 9 or not all(isinstance(x, str) for x in row):
            raise CollectorError(f"{label}_invalid_candle_schema")
        opened = timestamp(row[0], label)
        if opened % period != 0 or opened > exchange:
            raise CollectorError(f"{label}_invalid_candle_time")
        if row[8] not in ("0", "1") or (opened + period <= exchange) != (row[8] == "1"):
            raise CollectorError(f"{label}_inconsistent_confirmation")
        values = [number(value, label) for value in row[1:8]]
        o, h, low, close = values[:4]
        if min(o, h, low, close) <= 0 or h < max(o, low, close) or low > min(o, h, close):
            raise CollectorError(f"{label}_invalid_ohlc")
        if any(v < 0 for v in values[4:]):
            raise CollectorError(f"{label}_invalid_volume")
        rows.append(list(row))
    rows.sort(key=lambda row: int(row[0]))
    for previous, current in zip(rows, rows[1:]):
        if int(current[0]) - int(previous[0]) != period:
            raise CollectorError(f"{label}_candle_gap_or_duplicate")
    boundary = exchange // period * period
    if period == HOUR:
        completed = [row for row in rows if row[8] == "1"]
        if len(completed) not in (299, 300) or int(completed[-1][0]) + HOUR != boundary:
            raise CollectorError("hour_stale_completed_candle")
        # Preserve the official 300-row envelope for the existing consumer.
        # Its decoder excludes confirm0 H1 from all signal/ATR calculations.
    elif period == FIVE:
        if int(rows[-1][0]) != boundary or rows[-1][8] != "0":
            raise CollectorError("five_stale_current_candle")
    else:
        raise CollectorError("unsupported_candle_period")
    return {"code": "0", "data": rows}


def validate_contract(page):
    rows = page.get("data")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise CollectorError("contract_invalid_schema")
    if any(rows[0].get(key) != value for key, value in CONTRACT_FIELDS.items()):
        raise CollectorError("contract_wrong_market")
    # Discard every unrelated field, even when the endpoint adds new fields.
    return {"code": "0", "data": [dict(CONTRACT_FIELDS)]}


def collect(fetch_json=public_json, sleeper=time.sleep, wall_clock=now_ms):
    pages = []
    for index, (path, label) in enumerate(PATHS):
        if index == 1:
            sleeper(2.1)
        pages.append(require_ok(fetch_json(path, label), label))
    wall = wall_clock()
    exchange, clock = validate_clock(pages[2], wall)
    hour = validate_candles(pages[0], HOUR, exchange, "hour")
    five = validate_candles(pages[1], FIVE, exchange, "five")
    contract = validate_contract(pages[3])
    generated = datetime.fromtimestamp(wall / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        "schemaVersion": 1,
        "source": "okx",
        "instId": INST_ID,
        "generatedAt": generated,
        "clock": clock,
        "hour": hour,
        "five": five,
        "contract": contract,
    }


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
    parser.add_argument("--output", default="snapshots/latest.json")
    parser.add_argument("--required-attempt-marker")
    args = parser.parse_args(argv)
    try:
        if args.required_attempt_marker:
            require_current_marker(args.required_attempt_marker, now_ms())
        snapshot = collect()
        write_snapshot(snapshot, args.output)
    except (CollectorError, ClaimError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("snapshot_write_failed", file=sys.stderr)
        return 1
    print("public_snapshot_validated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
