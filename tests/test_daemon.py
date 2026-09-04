"""What the sidebar actually shows over time, driving the real daemon loop
against a fake clock and a fake herdr. Run with: python3 -m unittest discover tests
"""
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor

FRESH = {"session": {"pct": 30, "resets_at": None},
         "week": {"pct": 60, "resets_at": None}}


def drive(outcomes, hours=2):
    """Run cmd_daemon over `hours` of fake time, one fetch outcome per poll.

    The last outcome repeats once the list runs out. Returns the sidebar
    writes as (seconds since start, text), with "<cleared>" for a retraction.
    """
    clock, log, ticks = [0.0], [], [0]
    limit = hours * 3600 / monitor.TICK_S

    def sleep(seconds):
        ticks[0] += 1
        if ticks[0] > limit:
            raise SystemExit
        clock[0] += seconds

    polls = iter(outcomes)
    last = [None]

    def fetch(_account):
        last[0] = next(polls, last[0])
        return last[0]

    saved = {name: getattr(monitor, name) for name in
             ("time", "fetch_usage", "herdr_json", "report_variant",
              "clear_variants", "ACCOUNTS", "STATE_DIR", "PIDFILE")}
    with tempfile.TemporaryDirectory() as state:
        monitor.time = types.SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)
        monitor.fetch_usage = fetch
        monitor.herdr_json = lambda *a, key: (
            [{"workspace_id": "w1", "cwd": "/tmp"}] if key == "panes"
            else [{"workspace_id": "w1"}])
        monitor.report_variant = lambda ws, variant, text: log.append((clock[0], text))
        monitor.clear_variants = lambda ws: log.append((clock[0], "<cleared>"))
        monitor.ACCOUNTS = [{"name": "claude", "config_dir": None, "prefixes": []}]
        monitor.STATE_DIR = state
        monitor.PIDFILE = os.path.join(state, "monitor.pid")
        try:
            monitor.cmd_daemon()
        except SystemExit:
            pass
        finally:
            for name, value in saved.items():
                setattr(monitor, name, value)
    return log


def first(log, predicate):
    return next((entry for entry in log if predicate(entry[1])), None)


class FrozenGaugeTest(unittest.TestCase):
    """A gauge that stops being fed must say so and then go away (issue #1)."""

    def test_every_failure_path_ages_the_row_out(self):
        # a 429 included: the backoff is deliberate, but it brings no numbers,
        # so the row it leaves behind is exactly as stale as any other failure
        for label, outcome in (("silent failure", None), ("rate limited", "ratelimited")):
            with self.subTest(label):
                log = drive([FRESH, outcome])
                marked = first(log, lambda text: text.endswith(" ?"))
                cleared = first(log, lambda text: text == "<cleared>")
                self.assertIsNotNone(marked, "gauge froze with nothing marking it")
                self.assertLessEqual(marked[0], monitor.CLEAR_S)
                self.assertIsNotNone(cleared, "stale gauge was never retracted")
                self.assertLessEqual(cleared[0], monitor.CLEAR_S + monitor.TICK_S)

    def test_healthy_fetches_are_never_marked_or_retracted(self):
        log = drive([FRESH])
        # past CLEAR_S, so absence below means the row survived rather than
        # the fake clock having quietly stopped
        self.assertGreater(log[-1][0], monitor.CLEAR_S)
        self.assertIsNone(first(log, lambda text: text.endswith(" ?")))
        self.assertIsNone(first(log, lambda text: text == "<cleared>"))


if __name__ == "__main__":
    unittest.main()
