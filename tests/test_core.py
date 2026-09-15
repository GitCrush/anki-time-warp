"""
Standalone tests for core.py – no Anki required.

    python3 tests/test_core.py

`aqt.mw` is replaced by a stub exposing col.sched.today and the small
collection surface apply_transformed_due_dates() touches.
"""

import importlib.util
import itertools
import os
import sys
import types
import unittest

TODAY = 5000


class _Sched:
    today = TODAY


class _FakeCard:
    def __init__(self, cid, due):
        self.id = cid
        self.due = due


class _Col:
    def __init__(self):
        self.sched = _Sched()
        self.cards = {}
        self.updated = []
        self.undo_entries = []
        self.merged = []

    def get_card(self, cid):
        return self.cards[cid]

    def update_cards(self, cards):
        self.updated.append(list(cards))

    def add_custom_undo_entry(self, name):
        self.undo_entries.append(name)
        return len(self.undo_entries)

    def merge_undo_entries(self, entry):
        self.merged.append(entry)


class _Mw:
    def __init__(self):
        self.col = _Col()


mw = _Mw()
sys.modules["aqt"] = types.SimpleNamespace(mw=mw)

_spec = importlib.util.spec_from_file_location(
    "core", os.path.join(os.path.dirname(__file__), "..", "core.py"))
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)


def make_cards(offsets, kind="review"):
    """offsets: list of due offsets relative to today (negative = overdue)."""
    return [{"cid": 1000 + i, "due": TODAY + off, "ivl": 10, "type": kind,
             "review_timeline": []}
            for i, off in enumerate(offsets)]


def horizon_for(cards, minimum=30):
    oldest = core.oldest_review_due(cards)
    return max(minimum, TODAY - oldest + 1) if oldest is not None else minimum


def check_invariants(test, cards, horizon_past):
    """Every review card has exactly one slot and due matches that slot."""
    length = None
    for c in cards:
        if c["type"] != "review":
            continue
        tl = c["review_timeline"]
        if length is None:
            length = len(tl)
        test.assertEqual(len(tl), length, "timelines must share one length")
        test.assertEqual(sum(tl), 1, f"card {c['cid']} must have exactly one slot")
        idx = tl.index(True)
        test.assertEqual(c["due"], TODAY + idx - horizon_past)


# Distributions that exercise the edge cases
DISTRIBUTIONS = {
    "mixed":        [-25, -20, -20, -3, -1, 0, 0, 0, 1, 2, 2, 5, 10, 10, 30, 45, 60, 89],
    "all_future":   [1, 2, 3, 5, 8, 13, 21, 34, 55, 89],
    "heavy_overdue": [-29] * 40 + [-10] * 10 + [0] * 5 + [50] * 3,
    "paused_5_months": [-150 + (i % 60) for i in range(300)],
    "single":       [7],
    "today_only":   [0] * 25,
}


class MassConservation(unittest.TestCase):

    def test_grid(self):
        grid = itertools.product(
            DISTRIBUTIONS.items(),
            [-100, -60, -1, 0, 1, 50, 200, 500],   # stretch
            [-40, -5, 0, 3, 30, 150, 365],         # shift
            [False, True],                          # collapse
            [-1, 0, 1, 7],                          # cap
        )
        for (name, offsets), stretch, shift, collapse, cap in grid:
            with self.subTest(dist=name, stretch=stretch, shift=shift,
                              collapse=collapse, cap=cap):
                cards = make_cards(offsets)
                hp = horizon_for(cards)
                core.simulate_review_timeline(
                    cards, stretch_pct=stretch, shift=shift,
                    horizon_past=hp, horizon_future=90,
                    collapse_overdues=collapse, max_cards_per_day=cap)
                check_invariants(self, cards, hp)
                hist = core.timeline_histogram(cards)
                self.assertEqual(sum(hist), len(offsets))
                if cap > 0:
                    self.assertLessEqual(max(hist), cap)

    def test_non_review_cards_untouched(self):
        cards = make_cards([-5, 0, 4]) + make_cards([0, 0], kind="new") \
            + make_cards([1_700_000_000], kind="learning")
        core.simulate_review_timeline(cards, stretch_pct=100, shift=10,
                                      horizon_past=30, horizon_future=90)
        for c in cards:
            if c["type"] != "review":
                self.assertFalse(any(c["review_timeline"]))
                self.assertEqual(c["due"], c["original_due"])


class ReportedRegressions(unittest.TestCase):

    def test_compression_with_collapse_keeps_all_cards(self):
        # Bug: overdue mass swept by the checkbox was dropped in the
        # compression branch, then scattered 1/day by the rounding step.
        offsets = [-25] * 30 + [-10] * 20 + [5] * 10 + [40] * 10 + [80] * 10
        cards = make_cards(offsets)
        core.simulate_review_timeline(cards, stretch_pct=-50, shift=0,
                                      horizon_past=30, horizon_future=90,
                                      collapse_overdues=True)
        check_invariants(self, cards, 30)
        hist = core.timeline_histogram(cards)
        self.assertEqual(sum(hist), len(offsets))
        # everything overdue is now on today, nothing before it
        self.assertEqual(sum(hist[:30]), 0)
        self.assertGreaterEqual(hist[30], 50)
        # no lonely 1-card tail beyond the original horizon
        self.assertEqual(len(hist), 120)

    def test_compression_without_collapse(self):
        offsets = [-25] * 30 + [5] * 10 + [80] * 10
        cards = make_cards(offsets)
        core.simulate_review_timeline(cards, stretch_pct=-80, shift=0,
                                      horizon_past=30, horizon_future=90)
        check_invariants(self, cards, 30)
        hist = core.timeline_histogram(cards)
        self.assertEqual(sum(hist), 50)
        # compressed: all mass within +-20% of the original spread around today
        self.assertEqual(sum(hist[30 - 6:30 + 17]), 50)

    def test_shift_beyond_horizon_extends_timeline(self):
        # Paused deck: 300 cards 90..150 days overdue, shifted by +150.
        offsets = [-150 + (i % 60) for i in range(300)]
        cards = make_cards(offsets)
        hp = horizon_for(cards)
        self.assertEqual(hp, 151)
        core.simulate_review_timeline(cards, stretch_pct=0, shift=150,
                                      horizon_past=hp, horizon_future=90)
        check_invariants(self, cards, hp)
        # relative spacing preserved exactly
        for c in cards:
            self.assertEqual(c["due"], c["original_due"] + 150)

    def test_negative_shift_clamps_instead_of_dropping(self):
        cards = make_cards([-29, -28, 0, 1])
        core.simulate_review_timeline(cards, stretch_pct=0, shift=-10,
                                      horizon_past=30, horizon_future=90)
        check_invariants(self, cards, 30)
        dues = sorted(c["due"] - TODAY for c in cards)
        self.assertEqual(dues, [-30, -30, -10, -9])

    def test_positive_stretch_is_flatten_within_horizon(self):
        cards = make_cards([0] * 90)
        core.simulate_review_timeline(cards, stretch_pct=500, shift=0,
                                      horizon_past=30, horizon_future=90)
        hist = core.timeline_histogram(cards)
        self.assertEqual(len(hist), 120)
        self.assertEqual(sum(hist[30:]), 90)
        self.assertLessEqual(max(hist), 90 * (1 / 6) + 90 * (5 / 6) / 90 + 1)

    def test_cap_auto_and_spill(self):
        cards = make_cards([0] * 200)
        core.simulate_review_timeline(cards, stretch_pct=0, shift=0,
                                      horizon_past=30, horizon_future=90,
                                      max_cards_per_day=0)
        hist = core.timeline_histogram(cards)
        self.assertEqual(sum(hist), 200)
        self.assertEqual(max(hist), 3)   # ceil(200/90)

    def test_deterministic(self):
        offsets = DISTRIBUTIONS["mixed"]
        a = make_cards(offsets)
        b = make_cards(offsets)
        for cards in (a, b):
            core.simulate_review_timeline(cards, stretch_pct=120, shift=4,
                                          horizon_past=30, horizon_future=90,
                                          collapse_overdues=True, max_cards_per_day=3)
        self.assertEqual([c["due"] for c in a], [c["due"] for c in b])


class ApplyRoundTrip(unittest.TestCase):

    def setUp(self):
        mw.col.cards.clear()
        mw.col.updated.clear()
        mw.col.undo_entries.clear()
        mw.col.merged.clear()

    def test_apply_writes_simulated_dues_once(self):
        offsets = [-150 + (i % 60) for i in range(120)] + [3, 3, 40]
        cards = make_cards(offsets)
        for c in cards:
            mw.col.cards[c["cid"]] = _FakeCard(c["cid"], c["due"])
        hp = horizon_for(cards)
        core.simulate_review_timeline(cards, stretch_pct=0, shift=150,
                                      horizon_past=hp, horizon_future=90)
        updated, skipped = core.apply_transformed_due_dates(cards, hp)
        self.assertEqual((updated, skipped), (len(cards), 0))
        self.assertEqual(len(mw.col.updated), 1)          # one update_cards() call
        self.assertEqual(mw.col.undo_entries, ["Time Warp"])
        self.assertEqual(mw.col.merged, [1])
        for c in cards:
            self.assertEqual(mw.col.cards[c["cid"]].due, c["due"])
            self.assertEqual(mw.col.cards[c["cid"]].due, c["original_due"] + 150)

    def test_apply_skips_unchanged_and_out_of_range(self):
        cards = make_cards([-100, 0, 5])
        for c in cards:
            mw.col.cards[c["cid"]] = _FakeCard(c["cid"], c["due"])
        # deliberately too small horizon: the -100 card is out of range
        core.simulate_review_timeline(cards, stretch_pct=0, shift=0,
                                      horizon_past=30, horizon_future=90)
        updated, skipped = core.apply_transformed_due_dates(cards, 30)
        self.assertEqual((updated, skipped), (0, 1))
        self.assertEqual(mw.col.updated, [])
        self.assertEqual(mw.col.undo_entries, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
