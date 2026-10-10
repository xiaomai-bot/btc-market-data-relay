"""Offline clock/wait checks for an undeployed schedule proposal."""

import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wait_for_window as scheduler

HOUR_START = 1_800_000_000_000 // scheduler.HOUR_MS * scheduler.HOUR_MS
TARGET = HOUR_START + scheduler.START_OFFSET_MS
CUTOFF = HOUR_START + scheduler.END_OFFSET_MS


class WindowTests(unittest.TestCase):
    def test_nominal_seventh_minute_waits_once_for_six_minutes(self):
        times = iter([HOUR_START + 7 * 60_000, TARGET])
        sleeps = []
        self.assertEqual(scheduler.wait_for_window(lambda: next(times), sleeps.append), TARGET)
        self.assertEqual(sleeps, [360.0])

    def test_late_but_before_target_waits_only_remaining_time(self):
        times = iter([TARGET - 250, TARGET + 25])
        sleeps = []
        self.assertEqual(scheduler.wait_for_window(lambda: next(times), sleeps.append), TARGET + 25)
        self.assertEqual(sleeps, [0.25])

    def test_exact_target_and_cutoff_inclusive(self):
        for value in (TARGET, TARGET + 1, CUTOFF):
            with self.subTest(value=value):
                times = iter([value, value])
                sleeps = []
                self.assertEqual(scheduler.wait_for_window(lambda: next(times), sleeps.append), value)
                self.assertEqual(sleeps, [])

    def test_after_cutoff_does_not_sleep_or_shift_hour(self):
        for value in (CUTOFF + 1, HOUR_START + 59 * 60_000):
            with self.subTest(value=value):
                sleeps = []
                with self.assertRaisesRegex(scheduler.WindowError, "^scheduled_window_missed$"):
                    scheduler.wait_for_window(lambda: value, sleeps.append)
                self.assertEqual(sleeps, [])

    def test_os_oversleep_past_cutoff_stops_without_second_wait(self):
        for late in (CUTOFF + 1, HOUR_START + scheduler.HOUR_MS + scheduler.START_OFFSET_MS):
            with self.subTest(late=late):
                times = iter([TARGET - 1_000, late])
                sleeps = []
                with self.assertRaisesRegex(scheduler.WindowError, "^scheduled_window_missed$"):
                    scheduler.wait_for_window(lambda: next(times), sleeps.append)
                self.assertEqual(sleeps, [1.0])

    def test_backward_clock_stops_without_second_wait(self):
        times = iter([TARGET - 1, TARGET - 1])
        sleeps = []
        with self.assertRaisesRegex(scheduler.WindowError, "^scheduled_window_clock_before_target$"):
            scheduler.wait_for_window(lambda: next(times), sleeps.append)
        self.assertEqual(sleeps, [0.001])

    def test_invalid_initial_clock_stops(self):
        for value in (-1, 1.5, True, "1800000000000", None):
            with self.subTest(value=value), self.assertRaisesRegex(scheduler.WindowError, "^scheduled_window_invalid_clock$"):
                scheduler.window(value)

    def test_invalid_final_clock_stops(self):
        for value in (None, 1.5, True, "future"):
            with self.subTest(value=value):
                times = iter([TARGET, value])
                with self.assertRaisesRegex(scheduler.WindowError, "^scheduled_window_clock_before_target$"):
                    scheduler.wait_for_window(lambda: next(times), lambda _: None)

    def test_cli_failure_is_nonzero(self):
        with patch.object(scheduler, "wait_for_window", side_effect=scheduler.WindowError("scheduled_window_missed")):
            with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(scheduler.main(), 1)
                self.assertEqual(stderr.getvalue(), "scheduled_window_missed\n")

    def test_cli_success_is_zero(self):
        with patch.object(scheduler, "wait_for_window", return_value=TARGET):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(scheduler.main(), 0)
                self.assertEqual(stdout.getvalue(), "scheduled_collection_window_ready\n")


if __name__ == "__main__":
    unittest.main()
