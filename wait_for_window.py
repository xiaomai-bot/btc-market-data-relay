#!/usr/bin/env python3
"""Wait for the scheduled UTC window; never call a market API."""

import sys
import time

HOUR_MS = 3_600_000
START_OFFSET_MS = 13 * 60_000
END_OFFSET_MS = 13 * 60_000 + 5_000


class WindowError(Exception):
    pass


def now_ms():
    return time.time_ns() // 1_000_000


def window(wall_ms):
    if type(wall_ms) is not int or wall_ms < 0:
        raise WindowError("scheduled_window_invalid_clock")
    hour_start = wall_ms // HOUR_MS * HOUR_MS
    target = hour_start + START_OFFSET_MS
    cutoff = hour_start + END_OFFSET_MS
    if wall_ms > cutoff:
        raise WindowError("scheduled_window_missed")
    return target, cutoff, max(0, target - wall_ms)


def wait_for_window(wall_clock=now_ms, sleeper=time.sleep):
    target, cutoff, delay_ms = window(wall_clock())
    if delay_ms:
        sleeper(delay_ms / 1000)
    # OS delays are checked against the original hour. No second wait, next
    # hour shift or collection after a missed window is permitted.
    final = wall_clock()
    if type(final) is not int or final < target:
        raise WindowError("scheduled_window_clock_before_target")
    if final > cutoff:
        raise WindowError("scheduled_window_missed")
    return final


def main():
    try:
        wait_for_window()
    except WindowError as error:
        print(str(error), file=sys.stderr)
        return 1
    print("scheduled_collection_window_ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
