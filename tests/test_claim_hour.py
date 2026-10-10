import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import claim_hour
import collector


NOW = 1_800_000_123_456
PACKAGE = Path(__file__).resolve().parents[1]


def marker(wall=NOW):
    return {"utcHour": claim_hour.hour_id(wall), "attemptedAt": claim_hour.iso_ms(wall)}


class Backend:
    def __init__(self, previous=None):
        self.previous = previous
        self.published = []
        self.fail = False

    def refresh_and_read_marker(self):
        return self.previous

    def publish_marker(self, value):
        self.published.append(value)
        self.previous = value  # Model a possibly accepted push before timeout.
        if self.fail:
            raise claim_hour.ClaimError("attempt_git_failed")


class ClaimTests(unittest.TestCase):
    def test_initial_claim_then_dispatch_or_delayed_schedule_same_hour_is_skipped(self):
        backend = Backend()
        self.assertTrue(claim_hour.claim_current_hour(backend, lambda: NOW))
        self.assertEqual(backend.published, [marker()])
        self.assertFalse(claim_hour.claim_current_hour(backend, lambda: NOW + 60_000))
        self.assertEqual(len(backend.published), 1)

    def test_new_hour_may_claim_once(self):
        backend = Backend(marker(NOW - claim_hour.HOUR_MS))
        self.assertTrue(claim_hour.claim_current_hour(backend, lambda: NOW))
        self.assertFalse(claim_hour.claim_current_hour(backend, lambda: NOW))
        self.assertEqual(len(backend.published), 1)

    def test_failed_or_uncertain_push_never_grants_market_permission_or_clears_marker(self):
        backend = Backend()
        backend.fail = True
        with self.assertRaisesRegex(claim_hour.ClaimError, "^attempt_git_failed$"):
            claim_hour.claim_current_hour(backend, lambda: NOW)
        self.assertEqual(backend.previous, marker())
        backend.fail = False
        self.assertFalse(claim_hour.claim_current_hour(backend, lambda: NOW))

    def test_utc_hour_rollover_during_claim_fails_and_keeps_attempt(self):
        before = NOW // claim_hour.HOUR_MS * claim_hour.HOUR_MS + claim_hour.HOUR_MS - 1
        times = iter([before, before + 1])
        backend = Backend()
        with self.assertRaisesRegex(claim_hour.ClaimError, "^attempt_hour_changed_before_market$"):
            claim_hour.claim_current_hour(backend, lambda: next(times))
        self.assertEqual(backend.previous, marker(before))

    def test_future_marker_rejected(self):
        for future in (NOW + 1, NOW + claim_hour.HOUR_MS):
            with self.subTest(future=future):
                backend = Backend(marker(future))
                with self.assertRaisesRegex(claim_hour.ClaimError, "^attempt_future_marker$"):
                    claim_hour.claim_current_hour(backend, lambda: NOW)
                self.assertEqual(backend.published, [])

    def test_marker_only_has_two_public_utc_fields(self):
        public = marker()
        self.assertEqual(set(public), {"utcHour", "attemptedAt"})
        self.assertEqual(claim_hour.validate_marker(public), public)
        for bad in ({**public, "CAPITAL_CNY": "secret"},
                    {**public, "utcHour": public["attemptedAt"]},
                    {**public, "attemptedAt": "invalid"},
                    {"utcHour": public["utcHour"]}):
            with self.subTest(bad=bad):
                with self.assertRaises(claim_hour.ClaimError):
                    claim_hour.validate_marker(bad)

    def test_bad_or_missing_current_marker_blocks_collector_before_requests(self):
        with patch("collector.require_current_marker", side_effect=claim_hour.ClaimError("attempt_hour_changed_before_market")):
            with patch("collector.collect") as collect:
                with patch("sys.stderr", new_callable=io.StringIO):
                    self.assertEqual(collector.main(["--required-attempt-marker", "snapshots/last_attempt.json"]), 1)
                collect.assert_not_called()

    def test_current_marker_check_rejects_rollover(self):
        with tempfile.TemporaryDirectory(dir=PACKAGE) as temporary:
            path = Path(temporary) / "last_attempt.json"
            path.write_text('{"utcHour":"2027-01-15T08:00:00.000Z","attemptedAt":"2027-01-15T08:02:03.456Z"}')
            claim_hour.require_current_marker(path, NOW)
            with self.assertRaisesRegex(claim_hour.ClaimError, "^attempt_hour_changed_before_market$"):
                claim_hour.require_current_marker(path, NOW + claim_hour.HOUR_MS)

    def test_git_auth_only_in_push_environment_and_error_is_constant(self):
        backend = claim_hour.GitBackend("synthetic-test-token")
        commands = []

        def fake(args, **kwargs):
            commands.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

        with patch("subprocess.run", side_effect=fake):
            backend.git(["fetch", "--depth=1", "origin", "main"])
            backend.git(["push", "origin", "HEAD:main"], authenticated=True)
        self.assertNotIn("PUBLISH_TOKEN", commands[0][1]["env"])
        self.assertNotIn("GIT_CONFIG_VALUE_0", commands[0][1]["env"])
        self.assertIn("GIT_CONFIG_VALUE_0", commands[1][1]["env"])
        self.assertEqual(commands[1][1]["env"]["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertNotIn("synthetic-test-token", str(commands[1][0]))
        with patch("subprocess.run", return_value=subprocess.CompletedProcess(["git"], 1, b"secret", b"secret")):
            with self.assertRaisesRegex(claim_hour.ClaimError, "^attempt_git_failed$"):
                backend.git(["push", "origin", "HEAD:main"], authenticated=True)

    def test_publish_stages_and_commits_only_attempt_path(self):
        backend = claim_hour.GitBackend("synthetic-test-token")
        backend.latest = "a" * 40
        commands = []

        def fake(args, **kwargs):
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

        original = Path.cwd()
        with tempfile.TemporaryDirectory(dir=PACKAGE) as temporary:
            try:
                os.chdir(temporary)
                with patch("subprocess.run", side_effect=fake):
                    backend.publish_marker(marker())
                self.assertEqual(claim_hour.read_marker_text(Path(claim_hour.MARKER_PATH).read_text()), marker())
                self.assertIn(["git", "add", "--", claim_hour.MARKER_PATH], commands)
                commit = next(command for command in commands if "commit" in command)
                self.assertEqual(commit[-2:], ["--", claim_hour.MARKER_PATH])
                self.assertNotIn("-A", str(commands))
            finally:
                os.chdir(original)


if __name__ == "__main__":
    unittest.main()
