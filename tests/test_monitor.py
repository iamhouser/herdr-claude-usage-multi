"""Unit tests for the rendering logic. Run with: python3 -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor


def usage(session=30, week=60):
    return {"session": {"pct": session, "resets_at": None},
            "week": {"pct": week, "resets_at": None}}


class RenderTest(unittest.TestCase):
    def test_normal_gauge(self):
        variant, text = monitor.render(usage(30, 60))
        self.assertEqual(variant, "cu")
        self.assertEqual(text, "■■■□□□□□□□ 30/60")

    def test_warn_and_hot_variants(self):
        self.assertEqual(monitor.render(usage(30, 75))[0], "cu_warn")
        self.assertEqual(monitor.render(usage(95, 40))[0], "cu_hot")

    def test_exhausted_window_without_reset_time(self):
        variant, text = monitor.render(usage(100, 40))
        self.assertEqual(variant, "cu_out")
        self.assertIn("limit reached", text)


class RenderEntryTest(unittest.TestCase):
    """A cached value ages out instead of freezing the gauge (issue #1)."""

    def test_fresh_entry_renders_unchanged(self):
        entry = (usage(), 1000.0)
        self.assertEqual(monitor.render_entry(entry, 1000.0 + monitor.POLL_S),
                         monitor.render(usage()))

    def test_entry_past_stale_cutoff_gains_marker(self):
        entry = (usage(), 1000.0)
        variant, text = monitor.render_entry(entry, 1000.0 + monitor.STALE_S + 1)
        self.assertEqual((variant, text),
                         (monitor.render(usage())[0], monitor.render(usage())[1] + " ?"))

    def test_entry_past_clear_cutoff_stops_rendering(self):
        entry = (usage(), 1000.0)
        self.assertIsNone(monitor.render_entry(entry, 1000.0 + monitor.CLEAR_S + 1))

    def test_cutoffs_are_ordered(self):
        self.assertLess(monitor.POLL_S, monitor.STALE_S)
        self.assertLess(monitor.STALE_S, monitor.CLEAR_S)


if __name__ == "__main__":
    unittest.main()
