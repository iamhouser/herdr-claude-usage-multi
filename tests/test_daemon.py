"""What the sidebar actually shows over time, driving the real daemon loop
against a fake clock and a fake herdr. Run with: python3 -m unittest discover tests
"""
import collections
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor

FRESH = {"session": {"pct": 30, "resets_at": None},
         "week": {"pct": 60, "resets_at": None}}
SOLO = [{"name": "claude", "config_dir": None, "prefixes": []}]


def drive(outcomes, accounts=SOLO, hours=2):
    """Run cmd_daemon over `hours` of fake time.

    `outcomes` maps an account name to the results its fetches return, one per
    poll, the last repeating once the list runs out. Every account gets a
    workspace of its own, matched by the pane cwd. Returns the sidebar writes
    as (seconds, text) and the polls as (seconds, account name).
    """
    clock, writes, polls, ticks = [0.0], [], [], [0]
    spaces = [{"workspace_id": f"w{i}", "cwd": (a["prefixes"] or ["/elsewhere"])[0]}
              for i, a in enumerate(accounts)]

    def sleep(seconds):
        ticks[0] += 1
        if ticks[0] > hours * 3600 / monitor.TICK_S:
            raise SystemExit
        clock[0] += seconds

    left = {name: list(results) for name, results in outcomes.items()}
    last = {}

    def fetch(account):
        name = account["name"]
        polls.append((clock[0], name))
        if left.get(name):
            last[name] = left[name].pop(0)
        return last.get(name)

    saved = {name: getattr(monitor, name) for name in
             ("time", "fetch_usage", "herdr_json", "report_variant",
              "clear_variants", "ACCOUNTS", "STATE_DIR", "PIDFILE")}
    with tempfile.TemporaryDirectory() as state:
        monitor.time = types.SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)
        monitor.fetch_usage = fetch
        monitor.herdr_json = lambda *a, key: (
            spaces if key == "panes"
            else [{"workspace_id": s["workspace_id"]} for s in spaces])
        monitor.report_variant = lambda ws, variant, text: writes.append((clock[0], text))
        monitor.clear_variants = lambda ws: writes.append((clock[0], "<cleared>"))
        monitor.ACCOUNTS = accounts
        monitor.STATE_DIR = state
        monitor.PIDFILE = os.path.join(state, "monitor.pid")
        try:
            monitor.cmd_daemon()
        except SystemExit:
            pass
        finally:
            for name, value in saved.items():
                setattr(monitor, name, value)
    return writes, polls


def first(writes, predicate):
    return next((write for write in writes if predicate(write[1])), None)


class FrozenGaugeTest(unittest.TestCase):
    """A gauge that stops being fed must say so and then go away (issue #1)."""

    def test_every_failure_path_ages_the_row_out(self):
        # a 429 included: the backoff is deliberate, but it brings no numbers,
        # so the row it leaves behind is exactly as stale as any other failure
        for label, outcome in (("silent failure", None), ("rate limited", "ratelimited")):
            with self.subTest(label):
                writes, _ = drive({"claude": [FRESH, outcome]})
                marked = first(writes, lambda text: text.endswith(" ?"))
                cleared = first(writes, lambda text: text == "<cleared>")
                self.assertIsNotNone(marked, "gauge froze with nothing marking it")
                self.assertLessEqual(marked[0], monitor.CLEAR_S)
                self.assertIsNotNone(cleared, "stale gauge was never retracted")
                self.assertLessEqual(cleared[0], monitor.CLEAR_S + monitor.TICK_S)

    def test_healthy_fetches_are_never_marked_or_retracted(self):
        writes, _ = drive({"claude": [FRESH]})
        # past CLEAR_S, so absence below means the row survived rather than
        # the fake clock having quietly stopped
        self.assertGreater(writes[-1][0], monitor.CLEAR_S)
        self.assertIsNone(first(writes, lambda text: text.endswith(" ?")))
        self.assertIsNone(first(writes, lambda text: text == "<cleared>"))


class BackoffTest(unittest.TestCase):
    def test_a_rate_limited_account_does_not_slow_the_healthy_ones(self):
        # the backoff is per account: one profile stuck behind a 429 used to
        # triple the poll interval for every other profile too
        accounts = [{"name": "limited", "config_dir": None, "prefixes": ["/limited"]},
                    {"name": "healthy", "config_dir": None, "prefixes": []}]
        _, polls = drive({"limited": ["ratelimited"], "healthy": [FRESH]},
                         accounts=accounts, hours=1)
        counted = collections.Counter(name for _, name in polls)
        self.assertGreaterEqual(counted["healthy"], 3600 / monitor.POLL_S - 1)
        self.assertLessEqual(counted["limited"], 3600 / (monitor.POLL_S * 3) + 1)


if __name__ == "__main__":
    unittest.main()
