"""Offline checks only: injected transport, clocks and spacing; no real IO."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import datetime as dt
from decimal import Decimal
import json
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector as c
import claim_hour

NOW = 1_791_636_030_000  # A fixed UTC H1 boundary plus 30 seconds.


def envelope(data):
    return {"success": True, "code": 0, "data": data}


def candle_arrays(period, count, now=NOW):
    current = now // period * period // 1000
    times = [current - (count - 1 - i) * (period // 1000) for i in range(count)]
    return {"time": times, "open": ["60000.25"] * count, "high": ["60100.75"] * count,
            "low": ["59900.5"] * count, "close": ["60050.25"] * count, "vol": ["0"] * count}


class Fake:
    def __init__(self, s1=NOW + 4_000):
        self.calls = []
        self.sleeps = []
        self.walls = [NOW, s1, s1]
        self.replies = [
            envelope(NOW), envelope({"symbol": "BTC_USDT", "baseCoin": "BTC", "quoteCoin": "USDT",
                                    "settleCoin": "USDT", "futureType": 1, "state": 0,
                                    "preMarket": False, "type": 1, "contractSize": Decimal("0.0001")}),
            envelope(candle_arrays(c.HOUR_MS, 722)), envelope(candle_arrays(c.FIVE_MS, 301)), envelope(s1),
        ]

    def fetch(self, url):
        self.calls.append(url)
        response = self.replies[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    def wall(self):
        return self.walls.pop(0)

    def run(self, mode="full"):
        return c.collect(mode=mode, fetch_json=self.fetch, sleeper=self.sleeps.append, wall_clock=self.wall)


class CollectorTest(unittest.TestCase):
    def fails(self, fake, reason, call_count):
        with self.assertRaises(c.DiagnosticError) as cm:
            fake.run()
        self.assertEqual(cm.exception.reason, reason)
        self.assertEqual(len(fake.calls), call_count)
        return cm.exception.reason

    def test_default_is_metadata_only_and_never_healthy(self):
        f = Fake()
        result = c.collect(fetch_json=f.fetch, sleeper=f.sleeps.append, wall_clock=f.wall)
        self.assertEqual(len(f.calls), 2)
        self.assertEqual(f.sleeps, [])
        self.assertNotIn("snapshot", result)
        self.assertEqual(result["summary"]["status"], "metadata_validated")
        self.assertIs(result["summary"]["readyForProduction"], False)
        self.assertNotIn("healthy", json.dumps(result))

    def test_full_sequence_spacing_and_seconds_range(self):
        f = Fake()
        result = f.run()
        self.assertEqual(len(f.calls), 5)
        self.assertEqual(f.sleeps, [2.1])
        self.assertEqual([urlsplit(u).path for u in f.calls], [
            "/api/v1/contract/ping", "/api/v1/contract/detail/country", "/api/v1/contract/kline/BTC_USDT",
            "/api/v1/contract/kline/BTC_USDT", "/api/v1/contract/ping"])
        h = parse_qs(urlsplit(f.calls[2]).query)
        self.assertEqual(h, {"interval": ["Min60"], "start": [str((NOW // c.HOUR_MS * c.HOUR_MS - 721 * c.HOUR_MS) // 1000)], "end": [str(NOW // 1000)]})
        v = parse_qs(urlsplit(f.calls[3]).query)
        self.assertEqual(v["interval"], ["Min5"])
        self.assertEqual(v["start"], [str((NOW // c.FIVE_MS * c.FIVE_MS - 300 * c.FIVE_MS) // 1000)])
        self.assertIs(result["summary"]["readyForProduction"], False)

    def test_snapshot_exact_allowlist_and_counts(self):
        result = Fake().run()
        s = result["snapshot"]
        self.assertEqual(set(s), {"schemaVersion", "source", "instId", "generatedAt", "clock", "hour", "five", "contract"})
        self.assertEqual((s["source"], s["instId"]), ("mexc", "BTC_USDT"))
        self.assertEqual(len(s["hour"]["data"]["time"]), 721)
        self.assertEqual(len(s["five"]["data"]["time"]), 300)
        for key in ("hour", "five"):
            self.assertEqual(set(s[key]["data"]), {"time", "open", "high", "low", "close", "vol"})
            self.assertTrue(all(type(x) is int for x in s[key]["data"]["time"]))
            self.assertTrue(all(type(x) is str for name in c.PRICE_FIELDS for x in s[key]["data"][name]))
        self.assertTrue(s["generatedAt"].endswith(".000Z"))
        self.assertEqual(result["summary"]["hourCompleted"], 720)

    def test_undocumented_fields_are_ignored(self):
        f = Fake()
        f.replies[1]["data"]["apiAllowed"] = False
        f.replies[1]["data"]["privatePayload"] = "ignored"
        for reply in f.replies[2:4]:
            reply["data"]["realOpen"] = ["1"]
            reply["data"]["realClose"] = ["999999"]
            reply["data"]["amount"] = "injected"
        s = f.run()["snapshot"]
        self.assertEqual(s["hour"]["data"]["open"][0], "60000.25")
        self.assertNotIn("realOpen", json.dumps(s))
        self.assertNotIn("apiAllowed", json.dumps(s))

    def test_transport_forbidden_rate_limit_and_business_halt(self):
        for index, label in enumerate(("clock_initial", "contract", "hour", "five", "clock_final")):
            for status in (403, 429):
                with self.subTest(index=index, status=status):
                    f = Fake()
                    f.replies[index] = c.DiagnosticError(f"http_{status}")
                    self.fails(f, f"{label}_http_{status}", index + 1)
            with self.subTest(index=index, business=510):
                f = Fake()
                f.replies[index] = {"success": False, "code": 510, "message": "sensitive upstream text"}
                self.fails(f, f"{label}_business_510", index + 1)

    def test_exception_body_never_leaks(self):
        f = Fake()
        f.replies[0] = RuntimeError("secret text url credential")
        self.fails(f, "clock_initial_request_failed", 1)
        f = Fake()
        f.replies[0] = c.DiagnosticError("secret text")
        self.fails(f, "clock_initial_request_failed", 1)

    def test_missing_contract_fields_fail_before_market(self):
        for key in ("symbol", "baseCoin", "quoteCoin", "settleCoin", "futureType", "state", "preMarket", "type"):
            with self.subTest(key=key):
                f = Fake()
                del f.replies[1]["data"][key]
                self.fails(f, "contract_classification_failed", 2)

    def test_contract_wrong_classification_or_boolean_integer(self):
        for key, value in (("symbol", "ETH_USDT"), ("settleCoin", "BTC"), ("futureType", 2),
                           ("state", 4), ("preMarket", True), ("type", 2), ("futureType", True)):
            with self.subTest(key=key, value=value):
                f = Fake()
                f.replies[1]["data"][key] = value
                self.fails(f, "contract_classification_failed", 2)

    def test_legacy_contract_list_rejected(self):
        f = Fake()
        f.replies[1]["data"] = [f.replies[1]["data"]]
        self.fails(f, "contract_expected_object", 2)

    def test_contract_size_is_optional_but_positive_when_present(self):
        f = Fake()
        del f.replies[1]["data"]["contractSize"]
        self.assertNotIn("contractSize", f.run()["snapshot"]["contract"]["data"])
        for value in ("0", "-1", "NaN", False):
            with self.subTest(value=value):
                f = Fake()
                f.replies[1]["data"]["contractSize"] = value
                self.fails(f, "contract_invalid_size", 2)

    def test_clock_invalid_and_drift(self):
        for value in (True, "1791636030000", 0, c.MAX_SAFE_INT + 1):
            with self.subTest(value=value):
                f = Fake()
                f.replies[0]["data"] = value
                self.fails(f, "clock_initial_invalid_time", 1)
        f = Fake()
        f.walls[0] = NOW + 120_001
        self.fails(f, "clock_initial_clock_drift", 1)

    def test_envelope_requires_exact_success_and_integer_zero(self):
        for code in (False, "0", Decimal("0")):
            with self.subTest(code=code):
                f = Fake()
                f.replies[0]["code"] = code
                self.fails(f, "clock_initial_invalid_business_code", 1)
        f = Fake()
        f.replies[0]["success"] = 1
        self.fails(f, "clock_initial_not_success", 1)

    def test_unequal_arrays_and_limits(self):
        f = Fake()
        f.replies[2]["data"]["open"].pop()
        self.fails(f, "hour_invalid_lengths", 3)
        for count in (720, 2001):
            with self.subTest(count=count):
                f = Fake()
                f.replies[2]["data"] = candle_arrays(c.HOUR_MS, count)
                self.fails(f, "hour_invalid_lengths", 3)

    def test_invalid_array_shape(self):
        f = Fake()
        f.replies[2]["data"]["time"] = "not array"
        self.fails(f, "hour_invalid_arrays", 3)

    def test_decimal_price_positive_volume_nonnegative_and_finite(self):
        for field, value in (("close", "0"), ("vol", "-0.1"), ("close", "NaN"), ("close", Decimal("Infinity")),
                             ("close", True), ("close", "1e5"), ("close", float("inf")), ("vol", Decimal("-0"))):
            with self.subTest(field=field, value=value):
                f = Fake()
                f.replies[2]["data"][field][0] = value
                self.fails(f, "hour_invalid_decimal", 3)

    def test_ohlc_must_be_coherent(self):
        for field, value in (("high", "59000"), ("low", "61000")):
            with self.subTest(field=field):
                f = Fake()
                f.replies[2]["data"][field][0] = value
                self.fails(f, "hour_invalid_ohlc", 3)

    def test_duplicate_gap_and_unaligned_timestamps(self):
        for offset, reason in ((0, "hour_gap_or_duplicate"), (7200, "hour_gap_or_duplicate"), (1, "hour_invalid_timestamp")):
            with self.subTest(offset=offset):
                f = Fake()
                t = f.replies[2]["data"]["time"]
                t[1] = t[0] + offset
                self.fails(f, reason, 3)

    def test_stale_or_future_current_rejected_without_refetch(self):
        for shift, reason in ((-3600, "hour_missing_current_or_stale"), (3600, "hour_future_timestamp")):
            with self.subTest(shift=shift):
                f = Fake()
                f.replies[2]["data"]["time"] = [t + shift for t in f.replies[2]["data"]["time"]]
                self.fails(f, reason, 3)

    def test_unspecified_order_is_normalized(self):
        f = Fake()
        for field in ("time", *c.PRICE_FIELDS):
            f.replies[2]["data"][field].reverse()
        t = f.run()["snapshot"]["hour"]["data"]["time"]
        self.assertEqual(t, sorted(t))

    def test_final_clock_boundary_change_rejects_instead_of_retry(self):
        f = Fake(s1=NOW // c.FIVE_MS * c.FIVE_MS + c.FIVE_MS + 1)
        self.fails(f, "five_missing_current_or_stale", 5)
        f = Fake(s1=NOW // c.HOUR_MS * c.HOUR_MS + c.HOUR_MS + 1)
        self.fails(f, "hour_missing_current_or_stale", 5)

    def test_final_clock_cannot_move_backwards(self):
        f = Fake(s1=NOW - 1)
        self.fails(f, "clock_nonmonotonic", 5)

    def test_url_allowlist_refuses_alternate_domains_and_paths(self):
        for url in ("https://contract.mexc.com/api/v1/contract/ping", "http://api.mexc.com/api/v1/contract/ping",
                    "https://api.mexc.com/api/v1/contract/order", "https://api.mexc.com/api/v1/contract/ping?region=us",
                    "https://api.mexc.com/api/v1/contract/kline/BTC_USDT?interval=Min1&start=1&end=2"):
            with self.subTest(url=url), self.assertRaises(c.DiagnosticError):
                c._check_url(url)
        for url in Fake().calls:
            c._check_url(url)
        f = Fake()
        f.run()
        for url in f.calls:
            c._check_url(url)

    def test_marker_required_in_cli_even_for_metadata(self):
        for mode in ("metadata", "full"):
            with self.subTest(mode=mode), patch.object(c, "collect") as collect:
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                    c.main(["--mode", mode])
                self.assertEqual(cm.exception.code, 2)
                collect.assert_not_called()

    def test_bad_claim_prevents_metadata_and_full_network(self):
        for mode in ("metadata", "full"):
            with self.subTest(mode=mode), patch.object(c, "require_current_marker", side_effect=c.ClaimError("attempt_marker_stale")), patch.object(c, "collect") as collect:
                stream = io.StringIO()
                with redirect_stdout(stream):
                    status = c.main(["--mode", mode, "--required-attempt-marker", "snapshots/last_attempt.json"])
                self.assertEqual(status, 1)
                collect.assert_not_called()
                self.assertEqual(json.loads(stream.getvalue())["reason"], "attempt_marker_stale")

    def test_cli_claim_is_checked_first_and_snapshot_is_file_only(self):
        events = []
        result = Fake().run()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "latest.json"
            stream = io.StringIO()
            with patch.object(c, "require_current_marker", side_effect=lambda *_: events.append("claim")), patch.object(c, "collect", side_effect=lambda **_: events.append("collect") or result), redirect_stdout(stream):
                status = c.main(["--mode", "full", "--output", str(output), "--required-attempt-marker", "marker.json"])
            self.assertEqual(status, 0)
            self.assertEqual(events, ["claim", "collect", "claim"])
            self.assertEqual(json.loads(output.read_text()), result["snapshot"])
            self.assertEqual(json.loads(stream.getvalue()), result["summary"])
            self.assertNotIn("60000.25", stream.getvalue())
            self.assertEqual(list(Path(directory).glob(".snapshot-*.tmp")), [])

    def test_metadata_cli_does_not_write_snapshot(self):
        result = {"summary": {"status": "metadata_validated", "readyForProduction": False}}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "latest.json"
            with patch.object(c, "require_current_marker"), patch.object(c, "collect", return_value=result), redirect_stdout(io.StringIO()):
                status = c.main(["--output", str(output), "--required-attempt-marker", "marker.json"])
            self.assertEqual(status, 0)
            self.assertFalse(output.exists())

    def test_failed_collection_preserves_old_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "latest.json"
            output.write_text("old unchanged\n")
            with patch.object(c, "require_current_marker"), patch.object(c, "collect", side_effect=c.DiagnosticError("hour_http_403")), redirect_stdout(io.StringIO()):
                status = c.main(["--mode", "full", "--output", str(output), "--required-attempt-marker", "marker.json"])
            self.assertEqual(status, 1)
            self.assertEqual(output.read_text(), "old unchanged\n")

    def test_atomic_replace_failure_preserves_old_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "latest.json"
            output.write_text("old unchanged\n")
            with patch.object(c.os, "replace", side_effect=OSError("private path")):
                with self.assertRaises(OSError):
                    c.write_snapshot(Fake().run()["snapshot"], output)
            self.assertEqual(output.read_text(), "old unchanged\n")
            self.assertEqual(list(Path(directory).glob(".snapshot-*.tmp")), [])

    def test_each_http_is_guarded_against_utc_hour_rollover(self):
        real_collect = c.collect
        next_hour = NOW // c.HOUR_MS * c.HOUR_MS + c.HOUR_MS
        for crossing_request in range(5):
            with self.subTest(crossing_request=crossing_request), tempfile.TemporaryDirectory() as directory:
                marker = Path(directory) / "last_attempt.json"
                marker_text = json.dumps({"utcHour": claim_hour.hour_id(NOW), "attemptedAt": claim_hour.iso_ms(NOW)})
                marker.write_text(marker_text)
                output = Path(directory) / "latest.json"
                output.write_text("old unchanged\n")
                f = Fake()
                stream = io.StringIO()
                clocks = [NOW] * (crossing_request + 1) + [next_hour]
                def offline_collect(**kwargs):
                    return real_collect(**kwargs, sleeper=f.sleeps.append, wall_clock=f.wall)
                with patch.object(c, "now_ms", side_effect=clocks), patch.object(c, "public_json", side_effect=f.fetch), patch.object(c, "collect", side_effect=offline_collect), redirect_stdout(stream):
                    status = c.main(["--mode", "full", "--output", str(output), "--required-attempt-marker", str(marker)])
                self.assertEqual(status, 1)
                self.assertEqual(len(f.calls), crossing_request)
                self.assertEqual(json.loads(stream.getvalue())["reason"], "attempt_hour_changed_before_market")
                self.assertEqual(output.read_text(), "old unchanged\n")
                self.assertEqual(marker.read_text(), marker_text)
                self.assertEqual(list(Path(directory).glob(".snapshot-*.tmp")), [])

    def test_final_write_guard_rejects_rollover_after_last_http(self):
        real_collect = c.collect
        next_hour = NOW // c.HOUR_MS * c.HOUR_MS + c.HOUR_MS
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "last_attempt.json"
            marker_text = json.dumps({"utcHour": claim_hour.hour_id(NOW), "attemptedAt": claim_hour.iso_ms(NOW)})
            marker.write_text(marker_text)
            output = Path(directory) / "latest.json"
            output.write_text("old unchanged\n")
            f = Fake()
            stream = io.StringIO()
            def offline_collect(**kwargs):
                return real_collect(**kwargs, sleeper=f.sleeps.append, wall_clock=f.wall)
            with patch.object(c, "now_ms", side_effect=[NOW] * 6 + [next_hour]), patch.object(c, "public_json", side_effect=f.fetch), patch.object(c, "collect", side_effect=offline_collect), redirect_stdout(stream):
                status = c.main(["--mode", "full", "--output", str(output), "--required-attempt-marker", str(marker)])
            self.assertEqual(status, 1)
            self.assertEqual(len(f.calls), 5)
            self.assertEqual(json.loads(stream.getvalue())["reason"], "attempt_hour_changed_before_market")
            self.assertEqual(output.read_text(), "old unchanged\n")
            self.assertEqual(marker.read_text(), marker_text)
            self.assertEqual(list(Path(directory).glob(".snapshot-*.tmp")), [])

    def test_metadata_http_guard_is_also_required(self):
        real_collect = c.collect
        next_hour = NOW // c.HOUR_MS * c.HOUR_MS + c.HOUR_MS
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "last_attempt.json"
            marker.write_text(json.dumps({"utcHour": claim_hour.hour_id(NOW), "attemptedAt": claim_hour.iso_ms(NOW)}))
            f = Fake()
            def offline_collect(**kwargs):
                return real_collect(**kwargs, sleeper=f.sleeps.append, wall_clock=f.wall)
            with patch.object(c, "now_ms", side_effect=[NOW, NOW, next_hour]), patch.object(c, "public_json", side_effect=f.fetch), patch.object(c, "collect", side_effect=offline_collect), redirect_stdout(io.StringIO()):
                status = c.main(["--mode", "metadata", "--required-attempt-marker", str(marker)])
            self.assertEqual(status, 1)
            self.assertEqual(len(f.calls), 1)


if __name__ == "__main__":
    unittest.main()
