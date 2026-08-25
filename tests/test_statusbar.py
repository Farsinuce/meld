"""Stage-to-colour mapping and the governor segment (src/statusbar.py).

The status bar is a tkinter process, so nothing here touches a widget: the parts worth testing
are the three pure functions the paint loop leans on, and the property that matters most is what
they do with input this build has never seen. A newer server can invent a stage name at any time
and the bar has to keep drawing - one square per worker, no gaps, no exception out of paint().
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.statusbar import (  # noqa: E402
    ACC2,
    MUT,
    STAGE_COLOURS,
    STAGE_UNKNOWN,
    StatusBar,
    gov_segment,
    place_segments,
    stage_style,
    truncate_label,
)


class TestKnownStages:
    def test_every_legacy_stage_keeps_its_colour_and_stays_wordless(self):
        # The look of the stages that already shipped is frozen: colour from the palette, and no
        # label, because a block that has to be captioned is a block that failed to communicate.
        for stage in ("idle", "queued", "fetch", "prepare", "build", "save", "merge", "failed"):
            colour, label = stage_style(stage)
            assert colour == STAGE_COLOURS[stage]
            assert label == ""

    def test_missing_stage_is_idle(self):
        for empty in (None, "", "   "):
            assert stage_style(empty) == (STAGE_COLOURS["idle"], "")

    def test_case_and_padding_do_not_matter(self):
        assert stage_style("  BUILD ") == (STAGE_COLOURS["build"], "")


class TestGovernorStages:
    def test_waiting_for_admission_has_its_own_colour(self):
        colour, label = stage_style("waiting for admission")
        assert colour == STAGE_COLOURS["waiting for admission"]
        assert label == ""
        assert colour != STAGE_UNKNOWN

    def test_finishing_merges_has_its_own_colour(self):
        colour, label = stage_style("finishing merges")
        assert colour == STAGE_COLOURS["finishing merges"]
        assert label == ""

    def test_the_two_new_stages_are_distinguishable_from_the_old_ones(self):
        new = {STAGE_COLOURS["waiting for admission"], STAGE_COLOURS["finishing merges"]}
        old = {STAGE_COLOURS[k] for k in
               ("idle", "queued", "fetch", "prepare", "build", "save", "merge", "failed")}
        assert not (new & old)

    def test_short_spellings_land_on_the_same_colour(self):
        assert stage_style("admit")[0] == STAGE_COLOURS["waiting for admission"]
        assert stage_style("draining")[0] == STAGE_COLOURS["finishing merges"]


class TestUnknownStages:
    def test_unknown_stage_is_neutral_and_never_idle(self):
        colour, label = stage_style("negotiating with the moon")
        assert colour == STAGE_UNKNOWN
        assert colour != STAGE_COLOURS["idle"]
        assert label                                  # it says SOMETHING rather than nothing

    def test_unknown_label_is_bounded(self):
        _, label = stage_style("a" * 400)
        assert len(label) <= 16

    def test_a_stage_that_is_not_even_a_string_still_paints(self):
        for junk in (5, 3.2, [], {}, object()):
            colour, _label = stage_style(junk)
            assert colour.startswith("#")


class TestTruncateLabel:
    def test_short_text_is_untouched(self):
        assert truncate_label("merge") == "merge"

    def test_long_text_gets_an_ellipsis_inside_the_budget(self):
        out = truncate_label("waiting on a very long thing", limit=10)
        assert len(out) == 10
        assert out.endswith("…")

    def test_whitespace_is_collapsed(self):
        assert truncate_label("  two   words \n") == "two words"


class TestGovSegment:
    def test_absent_key_draws_nothing(self):
        # An older server, and the contract for the whole feature: no key, no pixels.
        assert gov_segment({}) is None
        assert gov_segment({"workers": []}) is None
        assert gov_segment(None) is None

    def test_off_draws_nothing(self):
        assert gov_segment({"gov": {"state": "OFF", "w": 8, "target": 8}}) is None

    def test_climbing_shows_the_direction(self):
        text, colour = gov_segment({"gov": {"state": "CONVERGE", "w": 8, "target": 12}})
        assert text == "gov 8→12"
        assert colour == ACC2

    def test_settled_drops_the_arrow(self):
        text, colour = gov_segment({"gov": {"state": "STEADY", "w": 10, "target": 10}})
        assert text == "gov 10"
        assert colour != MUT                          # settled reads as settled, not as off

    def test_unknown_state_still_renders_in_the_muted_grey(self):
        text, colour = gov_segment({"gov": {"state": "SOMETHING_NEW", "w": 4, "target": 6}})
        assert text == "gov 4→6"
        assert colour == MUT

    def test_malformed_blocks_are_dropped_rather_than_raised(self):
        assert gov_segment({"gov": "STEADY"}) is None
        assert gov_segment({"gov": {"state": "STEADY"}}) is None
        assert gov_segment({"gov": {"state": "STEADY", "w": "eight"}}) is None

    def test_missing_target_falls_back_to_the_worker_count(self):
        assert gov_segment({"gov": {"state": "CALIBRATE", "w": 6}})[0] == "gov 6"

    def test_the_segment_is_short_enough_to_sit_beside_the_blocks(self):
        text, _ = gov_segment({"gov": {"state": "CONVERGE", "w": 24, "target": 64}})
        assert len(text) <= 12


class TestSeparatorSpellings:
    """A stage that differs only in punctuation is the same stage, not an unknown one."""

    def test_underscores_and_hyphens_map_to_the_known_colour(self):
        for spelling in ("waiting_for_admission", "waiting-for-admission",
                         "WAITING_FOR_ADMISSION"):
            assert stage_style(spelling) == (STAGE_COLOURS["waiting for admission"], "")
        for spelling in ("finishing_merges", "finishing-merges"):
            assert stage_style(spelling) == (STAGE_COLOURS["finishing merges"], "")

    def test_inner_whitespace_is_collapsed_before_lookup(self):
        assert stage_style("waiting  for	admission") == (
            STAGE_COLOURS["waiting for admission"], "")


class TestPlaceSegments:
    """Right-to-left layout with a hard left floor - the segments never reach the title."""

    WIDE = lambda _self, t: len(t) * 6          # noqa: E731  (a stand-in for Tk's measure)

    def test_nothing_to_place_leaves_the_edge_where_it_started(self):
        placed, edge = place_segments([], 300, 56, self.WIDE)
        assert placed == []
        assert edge == 300

    def test_segments_walk_leftwards_and_report_the_new_edge(self):
        placed, edge = place_segments([("gov 8→12", "#fff"), ("abc", "#000")], 300, 56,
                                      self.WIDE)
        assert [p[0] for p in placed] == ["gov 8→12", "abc"]
        assert placed[0][2] == 300
        assert placed[1][2] < placed[0][2]         # the second sits to the LEFT of the first
        assert edge < 300

    def test_a_segment_that_would_cross_the_floor_is_dropped(self):
        placed, edge = place_segments([("x" * 100, "#000")], 300, 56, self.WIDE)
        assert placed == []
        assert edge == 300                         # ... and the detail keeps all of its space

    def test_the_governor_is_the_last_thing_dropped(self):
        # Governor first in, unknown-stage caption second: when only one fits, it is the gov.
        placed, _edge = place_segments([("gov 8→12", "#fff"), ("y" * 60, "#000")], 200, 56,
                                       self.WIDE)
        assert [p[0] for p in placed] == ["gov 8→12"]

    def test_a_measure_that_raises_does_not_take_the_paint_down(self):
        def boom(_t):
            raise RuntimeError("no Tk here")
        placed, _edge = place_segments([("gov 8", "#fff")], 300, 56, boom)
        assert [p[0] for p in placed] == ["gov 8"]

    def test_the_floor_sits_clear_of_the_title_and_detail_column(self):
        assert StatusBar.SEG_FLOOR_X > 50


class _StubCanvas:
    """Records draw calls instead of making pixels, so paint() can run with no Tk root."""

    def __init__(self):
        self.texts = []
        self.rects = []
        self.calls = 0

    def delete(self, *_a, **_k):
        self.calls += 1

    def create_text(self, x, y, **kw):
        self.texts.append((x, y, kw.get("text", ""), kw.get("fill")))
        return len(self.texts)

    def create_rectangle(self, x0, y0, x1, y1, **kw):
        self.rects.append((x0, y0, x1, y1, kw.get("fill")))
        return len(self.rects)

    def create_image(self, *_a, **_k):
        return 0

    def create_polygon(self, *_a, **_k):
        return 0

    def tag_bind(self, *_a, **_k):
        pass

    def itemconfigure(self, *_a, **_k):
        pass

    def configure(self, *_a, **_k):
        pass


def _bar():
    bar = StatusBar.__new__(StatusBar)              # no window, no settings file, no Tk
    bar.url = ""
    bar.token = ""
    bar._icon_img = None
    bar.update_state = ""
    bar.update_latest = ""
    bar.canvas = _StubCanvas()
    return bar


def _mini(**over):
    d = {"task": {"title": "Berlin", "detail": "42,-17,2"}, "total": 100, "done": 5,
         "active": True, "percent": 5.0,
         "stats": {"cpu_pct": 70, "ram_pct": 60, "disk_free_gb": 200},
         "workers": [{"id": i, "stage": "build", "pct": 40} for i in range(8)]}
    d.update(over)
    return d


class TestPaintSmoke:
    """paint() is the one place the pure helpers are wired together - run it end to end."""

    def test_a_pre_governor_payload_paints_no_segment(self):
        bar = _bar()
        bar.paint(_mini())
        assert not [t for t in bar.canvas.texts if str(t[2]).startswith("gov ")]

    def test_a_governor_payload_paints_the_segment(self):
        bar = _bar()
        bar.paint(_mini(gov={"state": "CONVERGE", "w": 8, "target": 12}))
        assert [t for t in bar.canvas.texts if t[2] == "gov 8→12"]

    def test_the_new_stages_paint_one_block_each_and_no_caption(self):
        bar = _bar()
        stages = ["waiting for admission", "finishing merges", "build", "idle"]
        bar.paint(_mini(workers=[{"id": i, "stage": s, "pct": 0}
                                 for i, s in enumerate(stages)]))
        blocks = [r for r in bar.canvas.rects
                  if r[4] in {STAGE_COLOURS[s] for s in stages}]
        assert len(blocks) == len(stages)
        assert not [t for t in bar.canvas.texts if t[3] == STAGE_UNKNOWN]

    def test_an_unknown_stage_paints_a_neutral_block_and_a_caption(self):
        bar = _bar()
        bar.paint(_mini(workers=[{"id": 0, "stage": "reticulating splines", "pct": 0}]))
        assert [r for r in bar.canvas.rects if r[4] == STAGE_UNKNOWN]
        assert [t for t in bar.canvas.texts if t[3] == STAGE_UNKNOWN]

    def test_a_wild_payload_does_not_raise(self):
        bar = _bar()
        bar.paint(_mini(gov={"state": None, "w": None},
                        workers=[{"id": 0, "stage": "z" * 300, "pct": 999},
                                 {"id": 1, "stage": None, "pct": -5},
                                 {"id": 2, "stage": 7, "pct": None}]))
        bar.paint({})                               # and an empty one, for good measure
