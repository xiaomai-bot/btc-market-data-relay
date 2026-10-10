#!/usr/bin/env python3
"""Persist a public UTC-hour attempt claim before any market request."""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HOUR_MS = 3_600_000
MARKER_PATH = "snapshots/last_attempt.json"


class ClaimError(Exception):
    pass


def now_ms():
    return time.time_ns() // 1_000_000


def iso_ms(wall):
    if type(wall) is not int or wall < 0:
        raise ClaimError("attempt_invalid_clock")
    try:
        return datetime.fromtimestamp(wall / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        raise ClaimError("attempt_invalid_clock") from None


def hour_id(wall):
    iso_ms(wall)
    return iso_ms(wall // HOUR_MS * HOUR_MS)


def parse_iso(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ClaimError("attempt_invalid_marker")
    try:
        stamp = datetime.fromisoformat(value[:-1] + "+00:00")
        wall = int(stamp.timestamp() * 1000)
        if iso_ms(wall) != value:
            raise ClaimError("attempt_invalid_marker")
        return wall
    except (ValueError, OverflowError, OSError, ClaimError):
        raise ClaimError("attempt_invalid_marker") from None


def validate_marker(marker):
    if not isinstance(marker, dict) or set(marker) != {"utcHour", "attemptedAt"}:
        raise ClaimError("attempt_invalid_marker")
    opened = parse_iso(marker["utcHour"])
    attempted = parse_iso(marker["attemptedAt"])
    if opened % HOUR_MS or hour_id(attempted) != marker["utcHour"]:
        raise ClaimError("attempt_invalid_marker")
    return marker


def read_marker_text(raw):
    if len(raw.encode("utf-8")) > 1024:
        raise ClaimError("attempt_invalid_marker")
    try:
        return validate_marker(json.loads(raw))
    except (ValueError, UnicodeError):
        raise ClaimError("attempt_invalid_marker") from None


def require_current_marker(path, wall):
    try:
        marker = read_marker_text(Path(path).read_text(encoding="utf-8"))
    except OSError:
        raise ClaimError("attempt_marker_missing") from None
    if marker["utcHour"] != hour_id(wall) or parse_iso(marker["attemptedAt"]) > wall:
        raise ClaimError("attempt_hour_changed_before_market")


def claim_current_hour(backend, wall_clock=now_ms):
    # Refresh main after the previous concurrency-group job has ended. A
    # queued event's old GITHUB_SHA is not a reliable deduplication record.
    previous = backend.refresh_and_read_marker()
    wall = wall_clock()
    current = hour_id(wall)
    if previous is not None:
        previous = validate_marker(previous)
        if previous["utcHour"] > current or parse_iso(previous["attemptedAt"]) > wall:
            raise ClaimError("attempt_future_marker")
        if previous["utcHour"] == current:
            return False
    marker = {"utcHour": current, "attemptedAt": iso_ms(wall)}
    # Only a successful non-force push grants permission to call the market.
    # Failed/uncertain publication never grants it, and no marker is cleared.
    backend.publish_marker(marker)
    if hour_id(wall_clock()) != current:
        raise ClaimError("attempt_hour_changed_before_market")
    return True


def git_env():
    env = os.environ.copy()
    env.pop("PUBLISH_TOKEN", None)
    for key in list(env):
        if key.startswith("GIT_TRACE") or key.startswith("GIT_CONFIG_") or key == "GIT_CURL_VERBOSE":
            env.pop(key)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


class GitBackend:
    def __init__(self, token):
        if not isinstance(token, str) or not token:
            raise ClaimError("attempt_publish_credential_missing")
        self.token = token

    def git(self, args, authenticated=False):
        env = git_env()
        if authenticated:
            authorization = base64.b64encode(("x-access-token:" + self.token).encode()).decode()
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
            env["GIT_CONFIG_VALUE_0"] = "AUTHORIZATION: basic " + authorization
        try:
            result = subprocess.run(["git", *args], env=env, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise ClaimError("attempt_git_failed") from None
        if result.returncode:
            raise ClaimError("attempt_git_failed")
        try:
            return result.stdout.decode("utf-8")
        except UnicodeError:
            raise ClaimError("attempt_git_failed") from None

    def refresh_and_read_marker(self):
        self.git(["fetch", "--depth=1", "origin", "main"])
        self.latest = self.git(["rev-parse", "FETCH_HEAD"]).strip()
        if len(self.latest) != 40 or any(c not in "0123456789abcdef" for c in self.latest):
            raise ClaimError("attempt_git_failed")
        exists = self.git(["ls-tree", "--name-only", self.latest, "--", MARKER_PATH]).strip()
        if not exists:
            return None
        if exists != MARKER_PATH:
            raise ClaimError("attempt_git_failed")
        return read_marker_text(self.git(["show", self.latest + ":" + MARKER_PATH]))

    def publish_marker(self, marker):
        # The checkout is an ephemeral public GitHub runner, never a user's
        # working tree. Base the claim on refreshed main for atomic git push.
        self.git(["checkout", "--detach", self.latest])
        output = Path(MARKER_PATH)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                             prefix=".attempt-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(validate_marker(marker), handle, separators=(",", ":"), allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, output)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.git(["add", "--", MARKER_PATH])
        self.git(["-c", "user.name=github-actions[bot]", "-c",
                  "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                  "commit", "--only", "-m", "Claim hourly public collection attempt", "--", MARKER_PATH])
        self.git(["push", "origin", "HEAD:main"], authenticated=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-output", required=True)
    args = parser.parse_args(argv)
    try:
        backend = GitBackend(os.environ.get("PUBLISH_TOKEN"))
        claimed = claim_current_hour(backend)
        with Path(args.github_output).open("a", encoding="utf-8") as output:
            output.write("claimed=" + ("true" if claimed else "false") + "\n")
    except ClaimError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("attempt_output_failed", file=sys.stderr)
        return 1
    print("hourly_attempt_claimed" if claimed else "hourly_attempt_already_recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
