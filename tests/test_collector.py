import copy
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import collector


NOW = 1_800_000_123_456
PACKAGE = Path(__file__).resolve().parents[1]


def candles(period, include_current=True):
    newest = NOW // period * period
    if not include_current:
        newest -= period
    rows = []
    for index in range(300):
        ts = newest - index * period
        rows.append([str(ts), "80000", "80100", "79900", "80050", "1", "1", "80000",
                     "1" if ts + period <= NOW else "0"])
    return {"code": "0", "data": rows}


def pages():
    return {
        "hour": candles(collector.HOUR),
        "five": candles(collector.FIVE),
        "clock": {"code": "0", "data": [{"ts": str(NOW)}]},
        "contract": {"code": "0", "data": [dict(collector.CONTRACT_FIELDS)]},
    }


def run(p=None):
    data = p or pages()
    calls = []
    sleeps = []

    def fake(path, label):
        calls.append((path, label))
        value = data[label]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    output = collector.collect(fetch_json=fake, sleeper=sleeps.append, wall_clock=lambda: NOW)
    return output, calls, sleeps


class CollectorTests(unittest.TestCase):
    def test_exact_sequence_and_2100ms_single_spacing(self):
        output, calls, sleeps = run()
        self.assertEqual(calls, list(collector.PATHS))
        self.assertEqual(sleeps, [2.1])
        self.assertEqual(output["generatedAt"], "2027-01-15T08:02:03.456Z")

    def test_unfinished_hour_excluded_from_completed_boundary_and_current_five_kept(self):
        output, _, _ = run()
        hour = output["hour"]["data"]
        completed = [row for row in hour if row[8] == "1"]
        self.assertEqual(len(hour), 300)
        self.assertEqual(len(completed), 299)
        self.assertEqual(hour[-1][8], "0")
        self.assertEqual(int(completed[-1][0]) + collector.HOUR, NOW // collector.HOUR * collector.HOUR)
        self.assertEqual(output["five"]["data"][-1][8], "0")
        self.assertEqual(int(output["five"]["data"][-1][0]), NOW // collector.FIVE * collector.FIVE)

    def test_all_300_completed_hours_supported(self):
        p = pages()
        p["hour"] = candles(collector.HOUR, include_current=False)
        output, _, _ = run(p)
        self.assertTrue(all(row[8] == "1" for row in output["hour"]["data"]))

    def test_http429_stops_without_sleep_or_remaining_requests(self):
        calls, sleeps = [], []

        def fake(path, label):
            calls.append(label)
            raise collector.CollectorError("hour_http_429")

        with self.assertRaisesRegex(collector.CollectorError, "^hour_http_429$"):
            collector.collect(fake, sleeps.append, lambda: NOW)
        self.assertEqual(calls, ["hour"])
        self.assertEqual(sleeps, [])

    def test_business50011_stops_without_remaining_requests(self):
        calls, sleeps = [], []

        def fake(path, label):
            calls.append(label)
            return {"code": "50011", "msg": "private arbitrary server response"}

        with self.assertRaisesRegex(collector.CollectorError, "^hour_business_code_50011$"):
            collector.collect(fake, sleeps.append, lambda: NOW)
        self.assertEqual(calls, ["hour"])
        self.assertEqual(sleeps, [])

    def test_second_endpoint429_stops_before_time_and_contract(self):
        calls, sleeps = [], []
        p = pages()

        def fake(path, label):
            calls.append(label)
            if label == "five":
                raise collector.CollectorError("five_http_429")
            return p[label]

        with self.assertRaisesRegex(collector.CollectorError, "^five_http_429$"):
            collector.collect(fake, sleeps.append, lambda: NOW)
        self.assertEqual(calls, ["hour", "five"])
        self.assertEqual(sleeps, [2.1])

    def test_transport429_has_safe_constant_error_and_no_body_read(self):
        path, label = collector.PATHS[0]
        upstream = io.BytesIO(b"secret arbitrary response body")
        error = urllib.error.HTTPError(collector.BASE + path, 429, "Too many requests", {}, upstream)
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(collector.CollectorError, "^hour_http_429$"):
                collector.public_json(path, label)
            opener.return_value.open.assert_called_once()
        self.assertEqual(upstream.tell(), 0)

    def test_redirect_not_followed(self):
        handler = collector.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_only_fixed_endpoint_allowed(self):
        with patch("urllib.request.build_opener") as opener:
            with self.assertRaisesRegex(collector.CollectorError, "^unapproved_public_endpoint$"):
                collector.public_json("/api/v5/account/balance", "account")
            opener.assert_not_called()

    def test_gap_duplicate_and_stale_pages_rejected(self):
        for label, period in (("hour", collector.HOUR), ("five", collector.FIVE)):
            for mode in ("gap", "duplicate", "stale"):
                with self.subTest(label=label, mode=mode):
                    p = pages()
                    rows = p[label]["data"]
                    if mode == "gap":
                        rows[-1][0] = str(int(rows[-1][0]) - period)
                    elif mode == "duplicate":
                        rows[-1] = rows[-2].copy()
                    else:
                        for row in rows:
                            row[0] = str(int(row[0]) - 2 * period)
                            row[8] = "1"
                    with self.assertRaises(collector.CollectorError):
                        run(p)

    def test_confirmation_schema_ohlc_and_nonfinite_rejected(self):
        for mode in ("bad_confirm", "truncated", "extra_column", "nan", "high_too_low", "zero_price", "negative_volume", "off_grid"):
            with self.subTest(mode=mode):
                p = pages()
                row = p["hour"]["data"][0]
                if mode == "bad_confirm":
                    row[8] = "1"
                elif mode == "truncated":
                    row.pop()
                elif mode == "extra_column":
                    row.append("secret")
                elif mode == "nan":
                    row[1] = "NaN"
                elif mode == "high_too_low":
                    row[2] = "79999"
                elif mode == "zero_price":
                    row[3] = "0"
                elif mode == "negative_volume":
                    row[5] = "-1"
                elif mode == "off_grid":
                    row[0] = str(int(row[0]) + 1)
                with self.assertRaises(collector.CollectorError):
                    run(p)

    def test_clock_drift_and_clock_schema_rejected(self):
        for value in (str(NOW - 120_001), str(NOW + 120_001), "NaN", 1):
            with self.subTest(value=value):
                p = pages()
                p["clock"]["data"][0]["ts"] = value
                with self.assertRaises(collector.CollectorError):
                    run(p)
        p = pages()
        p["clock"]["data"].append({"ts": str(NOW)})
        with self.assertRaises(collector.CollectorError):
            run(p)

    def test_unknown_or_duplicate_contract_rejected(self):
        for key in collector.CONTRACT_FIELDS:
            with self.subTest(key=key):
                p = pages()
                p["contract"]["data"][0][key] = "unknown"
                with self.assertRaisesRegex(collector.CollectorError, "^contract_wrong_market$"):
                    run(p)
        p = pages()
        p["contract"]["data"].append(dict(collector.CONTRACT_FIELDS))
        with self.assertRaisesRegex(collector.CollectorError, "^contract_invalid_schema$"):
            run(p)

    def test_public_output_is_allowlisted_and_discards_extra_fields(self):
        p = pages()
        for page in p.values():
            page["CAPITAL_CNY"] = "secret capital"
            page["SENDKEY"] = "secret credential"
        p["clock"]["data"][0]["account"] = "secret account"
        p["contract"]["data"][0]["position"] = "secret position"
        output, _, _ = run(p)
        self.assertEqual(set(output), {"schemaVersion", "source", "instId", "generatedAt", "clock", "hour", "five", "contract"})
        for label in ("clock", "hour", "five", "contract"):
            self.assertEqual(set(output[label]), {"code", "data"})
        self.assertEqual(set(output["contract"]["data"][0]), set(collector.CONTRACT_FIELDS))
        self.assertEqual(set(output["clock"]["data"][0]), {"ts"})
        self.assertNotIn("secret", json.dumps(output))

    def test_failure_does_not_refresh_existing_snapshot(self):
        with tempfile.TemporaryDirectory(dir=PACKAGE) as temporary:
            path = Path(temporary) / "snapshots" / "latest.json"
            path.parent.mkdir()
            original = '{"generatedAt":"old"}\n'
            path.write_text(original)
            with patch("collector.collect", side_effect=collector.CollectorError("hour_http_429")):
                with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    self.assertEqual(collector.main(["--output", str(path)]), 1)
                    self.assertEqual(stderr.getvalue(), "hour_http_429\n")
            self.assertEqual(path.read_text(), original)

    def test_atomic_success_writes_public_snapshot_and_removes_temporary(self):
        output, _, _ = run()
        with tempfile.TemporaryDirectory(dir=PACKAGE) as temporary:
            path = Path(temporary) / "snapshots" / "latest.json"
            collector.write_snapshot(output, path)
            self.assertEqual(json.loads(path.read_text()), output)
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
