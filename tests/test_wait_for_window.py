import io
import unittest
from unittest.mock import patch

import wait_for_window as scheduler


HOUR_START = 1_800_000_000_000 // scheduler.HOUR_MS * scheduler.HOUR_MS
TARGET = HOUR_START + scheduler.START_OFFSET_MS
CUTOFF = HOUR_START + scheduler.END_OFFSET_MS


class WindowTests(unittest.TestCase):
    def test_hourly_11minute_waits_exactly75seconds(self):
        times = iter([HOUR_START + 11 * 60_000, TARGET])
        sleeps = []
        result = scheduler.wait_for_window(lambda: next(times), sleeps.append)
        self.assertEqual(sleeps, [75.0])
        self.assertEqual(result, TARGET)

    def test_exact_target_and_later_in_window_do_not_sleep(self):
        for value in (TARGET, TARGET + 1, CUTOFF):
            with self.subTest(value=value):
                times = iter([value, value])
                sleeps = []
                self.assertEqual(scheduler.wait_for_window(lambda: next(times), sleeps.append), value)
                self.assertEqual(sleeps, [])

    def test_late_start_fails_without_sleep(self):
        sleeps = []
        for value in (CUTOFF + 1, HOUR_START + 59 * 60_000):
            with self.subTest(value=value):
                with self.assertRaisesRegex(scheduler.WindowError, "^scheduled_window_missed$"):
                    scheduler.wait_for_window(lambda: value, sleeps.append)
        self.assertEqual(sleeps, [])

    def test_oversleep_fails_without_second_wait_or_shift_to_next_hour(self):
        for late in (CUTOFF + 1, HOUR_START + scheduler.HOUR_MS + scheduler.START_OFFSET_MS):
            with self.subTest(late=late):
                times = iter([TARGET - 1_000, late])
                sleeps = []
                with self.assertRaisesRegex(scheduler.WindowError, "^scheduled_window_missed$"):
                    scheduler.wait_for_window(lambda: next(times), sleeps.append)
                self.assertEqual(sleeps, [1.0])

    def test_clock_moving_before_target_fails_without_second_wait(self):
        times = iter([TARGET - 1, TARGET - 1])
        sleeps = []
        with self.assertRaisesRegex(scheduler.WindowError, "^scheduled_window_clock_before_target$"):
            scheduler.wait_for_window(lambda: next(times), sleeps.append)
        self.assertEqual(sleeps, [0.001])

    def test_invalid_initial_clock_rejected(self):
        for value in (-1, 1.5, True, "1800000000000", None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(scheduler.WindowError, "^scheduled_window_invalid_clock$"):
                    scheduler.window(value)

    def test_failure_returns_nonzero_to_block_later_workflow_steps(self):
        with patch("wait_for_window.wait_for_window", side_effect=scheduler.WindowError("scheduled_window_missed")):
            with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(scheduler.main(), 1)
                self.assertEqual(stderr.getvalue(), "scheduled_window_missed\n")

    def test_success_returns_zero(self):
        with patch("wait_for_window.wait_for_window", return_value=TARGET):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(scheduler.main(), 0)
                self.assertEqual(stdout.getvalue(), "scheduled_collection_window_ready\n")


if __name__ == "__main__":
    unittest.main()
