"""
Offline logic tests for VOD Merge.

`patch.py` imports only the standard library at module scope (every Django import
is inside a function), so the decision logic is testable on a laptop with no
Dispatcharr, no database and no Docker:

    py -3 test_logic.py

Cases are drawn from real payloads observed on a live library: the three-provider
poster match for a single show, the `w600_and_h900_bestv2` URL that has no
filename and would otherwise poison the index with a size segment as its key, and
the plot-tier matches that no name scheme could reach (a French-titled canonical,
one truncated with an ellipsis).
"""

import logging
import re
import unittest
import ast
import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone as _tz

import patch


def _cfg(dry_run=False, denylist=None, approvals=None,
         variant=patch.DEFAULT_VARIANT_PATTERN, merge_movies=True,
         tag_unique_movies=False, merge_series=True):
    return {
        "dry_run": dry_run,
        "merge_series": merge_series,
        "merge_movies": merge_movies,
        "tag_unique_movies": tag_unique_movies,
        "movie_accounts": set(),
        "accounts": set(),
        "variant_re": re.compile(variant, re.I) if variant else None,
        "denylist": set(denylist or ()),
        "approvals": approvals or {},
        "index_ttl": 600,
    }


LONG_PLOT = (
    "Patrick Jane, a former celebrity psychic medium, uses his razor sharp "
    "skills of observation and expertise at reading people to solve crimes."
)
OTHER_PLOT = (
    "Sophie Birenbaum is ready for the spotlight as the lead in her high school "
    "musical until she is suddenly facing more drama at home than on the stage."
)


class PosterKeyTests(unittest.TestCase):
    def test_size_segment_is_stripped(self):
        # The real match that motivated the whole approach: same asset, three
        # providers, three different size segments.
        urls = (
            "https://image.tmdb.org/t/p/w600_and_h900_bestv2/acYXu4KaDj1NIkMgObnhe4C4a0T.jpg",
            "https://image.tmdb.org/t/p/w154/acYXu4KaDj1NIkMgObnhe4C4a0T.jpg",
            "https://image.tmdb.org/t/p/w500/acYXu4KaDj1NIkMgObnhe4C4a0T.jpg",
        )
        self.assertEqual({patch._poster_key(u) for u in urls},
                         {"acyxu4kadj1nikmgobnhe4c4a0t.jpg"})

    def test_url_ending_at_size_segment_is_rejected(self):
        # Would otherwise return "w600_and_h900_bestv2" and collide across every
        # title using that size.
        self.assertIsNone(
            patch._poster_key("https://image.tmdb.org/t/p/w600_and_h900_bestv2")
        )

    def test_query_string_and_trailing_slash(self):
        self.assertEqual(
            patch._poster_key("https://image.tmdb.org/t/p/w500/abc123.jpg?width=9"),
            "abc123.jpg")
        self.assertEqual(
            patch._poster_key("https://image.tmdb.org/t/p/w500/abc123.jpg/"),
            "abc123.jpg")

    def test_non_tmdb_and_blank_rejected(self):
        self.assertIsNone(patch._poster_key("https://cdn.example.com/w500/abc.jpg"))
        self.assertIsNone(patch._poster_key(""))
        self.assertIsNone(patch._poster_key(None))
        self.assertIsNone(patch._poster_key(12345))

    def test_non_image_extension_rejected(self):
        self.assertIsNone(patch._poster_key("https://image.tmdb.org/t/p/w500/abc.svg"))

    def test_entry_prefers_cover_then_cover_big(self):
        entry = {"cover_big": "https://image.tmdb.org/t/p/w500/big.jpg"}
        self.assertEqual(patch.entry_poster_key(entry), "big.jpg")
        entry["cover"] = "https://image.tmdb.org/t/p/w154/small.jpg"
        self.assertEqual(patch.entry_poster_key(entry), "small.jpg")
        self.assertIsNone(patch.entry_poster_key({}))


class PlotHashTests(unittest.TestCase):
    def test_punctuation_and_case_are_normalized(self):
        a = patch._plot_hash(LONG_PLOT)
        b = patch._plot_hash(LONG_PLOT.upper().replace(",", " --"))
        self.assertIsNotNone(a)
        self.assertEqual(a, b)

    def test_short_blurbs_rejected(self):
        self.assertIsNone(patch._plot_hash("Season 2."))
        self.assertIsNone(patch._plot_hash("Comedy"))
        self.assertIsNone(patch._plot_hash(""))
        self.assertIsNone(patch._plot_hash(None))
        self.assertIsNone(patch._plot_hash("x" * (patch.MIN_PLOT_CHARS - 1)))

    def test_threshold_boundary(self):
        self.assertIsNotNone(patch._plot_hash("y" * patch.MIN_PLOT_CHARS))

    def test_a_real_one_line_synopsis_is_usable(self):
        # Regression: an 80-char floor excluded this, which is why two Provider1
        # copies of a show failed to merge despite byte-identical plots.
        text = ("The most miserable person on Earth must save the world "
                "from happiness.")
        self.assertIsNotNone(patch._plot_hash(text))

    def test_digest_is_short_and_stable(self):
        h = patch._plot_hash(LONG_PLOT)
        d1, d2 = patch._plot_digest(h), patch._plot_digest(h)
        self.assertEqual(d1, d2)
        self.assertTrue(d1.startswith("plot:"))
        self.assertLess(len(d1), 20)
        self.assertIsNone(patch._plot_digest(None))

    def test_entry_prefers_plot_then_description(self):
        self.assertIsNotNone(patch.entry_plot_hash({"description": LONG_PLOT}))
        entry = {"plot": LONG_PLOT, "description": OTHER_PLOT}
        self.assertEqual(patch.entry_plot_hash(entry), patch._plot_hash(LONG_PLOT))
        self.assertIsNone(patch.entry_plot_hash({}))


def _row(cover=None, plot=None, tmdb="5920", desc=None):
    cp = {"basic_data": {}}
    if cover:
        cp["basic_data"]["cover"] = cover
    if plot:
        cp["basic_data"]["plot"] = plot
    return (cp, tmdb, desc)


class IndexTests(unittest.TestCase):
    def test_unique_keys_are_usable(self):
        idx = patch.build_indexes_from_rows([
            _row(cover="https://image.tmdb.org/t/p/w500/m.jpg", plot=LONG_PLOT),
            _row(cover="https://image.tmdb.org/t/p/w154/m.jpg"),
        ])
        self.assertEqual(idx["poster"], {"m.jpg": "5920"})
        self.assertEqual(idx["plot"], {patch._plot_hash(LONG_PLOT): "5920"})
        self.assertEqual(idx["poster_amb"], set())
        self.assertEqual(idx["plot_amb"], set())

    def test_series_description_also_feeds_the_plot_index(self):
        # A canonical's description may have been written by a different provider
        # than the listing we are matching, so both sources are indexed.
        idx = patch.build_indexes_from_rows([_row(desc=LONG_PLOT)])
        self.assertEqual(idx["plot"], {patch._plot_hash(LONG_PLOT): "5920"})

    def test_placeholder_poster_rejects_itself(self):
        url = "https://image.tmdb.org/t/p/w500/ph.jpg"
        idx = patch.build_indexes_from_rows([
            _row(cover=url, tmdb="1"), _row(cover=url, tmdb="2"), _row(cover=url, tmdb="3"),
        ])
        self.assertEqual(idx["poster"], {})
        self.assertEqual(idx["poster_amb"], {"ph.jpg"})

    def test_boilerplate_plot_rejects_itself(self):
        idx = patch.build_indexes_from_rows([
            _row(plot=LONG_PLOT, tmdb="1"), _row(plot=LONG_PLOT, tmdb="2"),
        ])
        self.assertEqual(idx["plot"], {})
        self.assertEqual(idx["plot_amb"], {patch._plot_hash(LONG_PLOT)})

    def test_ambiguity_is_sticky_regardless_of_order(self):
        url = "https://image.tmdb.org/t/p/w500/ph.jpg"
        idx = patch.build_indexes_from_rows([
            _row(cover=url, tmdb="1"), _row(cover=url, tmdb="2"), _row(cover=url, tmdb="1"),
        ])
        self.assertNotIn("ph.jpg", idx["poster"])
        self.assertIn("ph.jpg", idx["poster_amb"])

    def test_malformed_rows_are_skipped(self):
        idx = patch.build_indexes_from_rows([
            (None, "10", None),
            ({}, "11", None),
            ({"basic_data": None}, "12", None),
            ({"basic_data": "not-a-dict"}, "13", None),
            (_row(cover="https://image.tmdb.org/t/p/w500/ok.jpg")[0], None, None),
            (_row(cover="https://image.tmdb.org/t/p/w500/ok.jpg")[0], "", None),
        ])
        self.assertEqual(idx["poster"], {})
        self.assertEqual(idx["plot"], {})

    def test_int_tmdb_is_normalized_to_str(self):
        idx = patch.build_indexes_from_rows([
            _row(cover="https://image.tmdb.org/t/p/w500/a.jpg", tmdb=5920)])
        self.assertEqual(idx["poster"], {"a.jpg": "5920"})


class ExistingIdTests(unittest.TestCase):
    def test_blank_forms_count_as_absent(self):
        for blank in ("", "0", 0, None, "None", "null"):
            self.assertIsNone(patch.existing_id({"tmdb": blank}), repr(blank))

    def test_real_ids_are_detected(self):
        self.assertEqual(patch.existing_id({"tmdb": "5920"}), "5920")
        self.assertEqual(patch.existing_id({"tmdb_id": 5920}), "5920")
        self.assertEqual(patch.existing_id({"imdb": "tt0903747"}), "tt0903747")
        self.assertIsNone(patch.existing_id({}))


class DenylistTests(unittest.TestCase):
    def test_by_tmdb(self):
        self.assertTrue(patch.is_denied({"tmdb:5920"}, "Provider1", "13309", "5920"))
        self.assertFalse(patch.is_denied({"tmdb:5920"}, "Provider1", "13309", "62852"))

    def test_by_account_and_external_id(self):
        self.assertTrue(patch.is_denied({"Provider1:13309"}, "Provider1", "13309", "5920"))
        self.assertFalse(patch.is_denied({"Provider1:13309"}, "Provider1", "99999", "5920"))
        self.assertFalse(patch.is_denied({"Provider1:13309"}, "Provider2", "13309", "5920"))

    def test_empty_denylist(self):
        self.assertFalse(patch.is_denied(set(), "Provider1", "13309", "5920"))


class ApprovalParsingTests(unittest.TestCase):
    def test_lines_and_commas(self):
        got = patch.parse_approvals("Provider1:1234=108978\nProvider1:99=5920, Provider2:7=42")
        self.assertEqual(got, {("Provider1", "1234"): "108978",
                               ("Provider1", "99"): "5920",
                               ("Provider2", "7"): "42"})

    def test_whitespace_and_junk_ignored(self):
        got = patch.parse_approvals("  Provider1:1234 = 108978  \n\nnonsense\nno_colon=5\n")
        self.assertEqual(got, {("Provider1", "1234"): "108978"})

    def test_empty(self):
        self.assertEqual(patch.parse_approvals(""), {})
        self.assertEqual(patch.parse_approvals(None), {})


class DecideTests(unittest.TestCase):
    def setUp(self):
        self.idx = {
            "poster": {"m.jpg": "5920"},
            "poster_amb": {"ph.jpg"},
            "plot": {patch._plot_hash(LONG_PLOT): "5920"},
            "plot_amb": {patch._plot_hash(OTHER_PLOT)},
        }
        self.entry = {
            "name": "EN| Example Show",
            "series_id": 13309,
            "cover": "https://image.tmdb.org/t/p/w500/m.jpg",
        }

    def _decide(self, entry=None, cfg=None, account="Provider1", ext=13309):
        return patch.decide(entry if entry is not None else self.entry,
                            account, ext, self.idx, cfg or _cfg())

    def test_poster_tier_wins(self):
        action, tmdb, tier, key = self._decide()
        self.assertEqual((action, tmdb, tier, key),
                         (patch.INJECT, "5920", patch.TIER_POSTER, "m.jpg"))

    def test_plot_tier_used_when_poster_absent(self):
        entry = {"name": "Le Chemin etroit", "series_id": 1, "plot": LONG_PLOT}
        action, tmdb, tier, key = self._decide(entry)
        self.assertEqual((action, tmdb, tier), (patch.INJECT, "5920", patch.TIER_PLOT))
        self.assertTrue(key.startswith("plot:"))

    def test_poster_takes_precedence_over_plot(self):
        entry = dict(self.entry, plot=LONG_PLOT)
        self.assertEqual(self._decide(entry)[2], patch.TIER_POSTER)

    def test_dry_run_reports_without_injecting(self):
        action, tmdb, _, _ = self._decide(cfg=_cfg(dry_run=True))
        self.assertEqual(action, patch.DRY_RUN)
        self.assertEqual(tmdb, "5920")

    def test_already_tagged_entry_is_left_alone(self):
        self.assertEqual(self._decide(dict(self.entry, tmdb="5920"))[0], patch.HAS_ID)

    def test_no_signal_at_all(self):
        self.assertEqual(self._decide({"name": "x", "series_id": 1})[0], patch.NO_SIGNAL)

    def test_signal_present_but_unknown(self):
        entry = {"name": "x", "series_id": 1,
                 "cover": "https://image.tmdb.org/t/p/w500/zz.jpg"}
        self.assertEqual(self._decide(entry)[0], patch.NO_MATCH)

    def test_ambiguous_poster(self):
        entry = dict(self.entry, cover="https://image.tmdb.org/t/p/w500/ph.jpg")
        self.assertEqual(self._decide(entry)[0], patch.AMBIGUOUS)

    def test_ambiguous_plot(self):
        entry = {"name": "x", "series_id": 1, "plot": OTHER_PLOT}
        self.assertEqual(self._decide(entry)[0], patch.AMBIGUOUS)

    def test_variant_not_merged_on_poster_tier(self):
        entry = dict(self.entry, name="EN| Spider-Noir [Black/White]")
        action, tmdb, tier, _ = self._decide(entry)
        self.assertEqual(action, patch.VARIANT)
        self.assertEqual(tmdb, "5920")          # target still reported, for the log
        self.assertEqual(tier, patch.TIER_POSTER)

    def test_variant_not_merged_on_plot_tier(self):
        # An alternate cut shares the canonical's plot as well as its poster, so
        # the guard has to hold for both tiers.
        entry = {"name": "EN| Spider-Noir [Black/White]", "series_id": 1, "plot": LONG_PLOT}
        self.assertEqual(self._decide(entry)[0], patch.VARIANT)

    def test_plain_edition_still_merges(self):
        self.assertEqual(self._decide(dict(self.entry, name="EN| Spider-Noir"))[0],
                         patch.INJECT)

    def test_quality_tag_is_not_a_variant(self):
        # A 4K copy SHOULD merge -- same edition, better quality.
        for name in ("EN| The Wheel of Time 4K", "EN| Show [1080p]", "EN| Show (UHD)"):
            self.assertEqual(self._decide(dict(self.entry, name=name))[0],
                             patch.INJECT, name)

    def test_manual_approval_matches_without_any_signal(self):
        entry = {"name": "EN| Example Show", "series_id": 555}
        cfg = _cfg(approvals={("Provider1", "555"): "108978"})
        action, tmdb, tier, _ = self._decide(entry, cfg=cfg, ext=555)
        self.assertEqual((action, tmdb, tier), (patch.INJECT, "108978", patch.TIER_MANUAL))

    def test_manual_approval_overrides_a_derived_match(self):
        cfg = _cfg(approvals={("Provider1", "13309"): "999"})
        action, tmdb, tier, _ = self._decide(cfg=cfg)
        self.assertEqual((tmdb, tier), ("999", patch.TIER_MANUAL))

    def test_manual_approval_bypasses_variant_guard(self):
        # Approving a variant entry by name IS the override.
        entry = {"name": "EN| Spider-Noir [Black/White]", "series_id": 58818}
        cfg = _cfg(approvals={("Provider1", "58818"): "220102"})
        self.assertEqual(self._decide(entry, cfg=cfg, ext=58818)[0], patch.INJECT)

    def test_denylist_beats_everything_including_manual(self):
        entry = {"name": "EN| Example Show", "series_id": 555}
        cfg = _cfg(approvals={("Provider1", "555"): "108978"}, denylist={"Provider1:555"})
        self.assertEqual(self._decide(entry, cfg=cfg, ext=555)[0], patch.DENIED)

    def test_denylist_by_tmdb_blocks_derived_match(self):
        self.assertEqual(self._decide(cfg=_cfg(denylist={"tmdb:5920"}))[0], patch.DENIED)

    def test_empty_variant_pattern_disables_check(self):
        entry = dict(self.entry, name="EN| Spider-Noir [Black/White]")
        self.assertEqual(self._decide(entry, cfg=_cfg(variant=None))[0], patch.INJECT)

    def test_decide_never_mutates_the_entry(self):
        before = dict(self.entry)
        self._decide()
        self.assertEqual(self.entry, before)

    def test_missing_index_keys_do_not_raise(self):
        # Defensive: a partially-built bundle must not explode mid-scan.
        action, _, _, _ = patch.decide(self.entry, "Provider1", 1, {}, _cfg())
        self.assertEqual(action, patch.NO_MATCH)


class VariantPatternTests(unittest.TestCase):
    def setUp(self):
        self.rx = re.compile(patch.DEFAULT_VARIANT_PATTERN, re.I)

    def test_matches(self):
        for name in ("Spider-Noir [Black/White]", "Spider-Noir [black white]",
                     "Some Show [B&W]", "Some Show [Colorized]", "Some Show [Colorised]"):
            self.assertTrue(self.rx.search(name), name)

    def test_does_not_match(self):
        for name in ("Spider-Noir", "The White Lotus", "Black Mirror",
                     "EN - Orange Is the New Black (2013)", "The Wheel of Time 4K"):
            self.assertFalse(self.rx.search(name), name)


class DetailTmdbTests(unittest.TestCase):
    """Movies carry their tmdb in the DETAIL payload, which the sweep stores on
    the relation. This reader is what the movie wrapper will inject from."""

    def test_reads_stored_detail(self):
        props = {"detailed_info": {"tmdb_id": "1504358"}}
        self.assertEqual(patch.detail_tmdb(props), "1504358")

    def test_int_is_normalized(self):
        self.assertEqual(patch.detail_tmdb({"detailed_info": {"tmdb_id": 1504358}}),
                         "1504358")

    def test_blank_forms_are_absent(self):
        for blank in ("", "0", 0, None, "None", "null", "  "):
            self.assertIsNone(
                patch.detail_tmdb({"detailed_info": {"tmdb_id": blank}}), repr(blank))

    def test_missing_or_malformed(self):
        self.assertIsNone(patch.detail_tmdb(None))
        self.assertIsNone(patch.detail_tmdb({}))
        self.assertIsNone(patch.detail_tmdb({"detailed_info": None}))
        self.assertIsNone(patch.detail_tmdb({"detailed_info": "not-a-dict"}))
        self.assertIsNone(patch.detail_tmdb({"detailed_info": {}}))

    def test_ignores_basic_data(self):
        # Only the detail counts; a listing tmdb would already have been used by
        # core's own keying.
        props = {"basic_data": {"tmdb": "999"}, "detailed_info": {}}
        self.assertIsNone(patch.detail_tmdb(props))


class DecideMovieTests(unittest.TestCase):
    """Movies need no matching -- the sweep already stored the provider's own id,
    so this is a lookup plus the same guards the series path uses."""

    def setUp(self):
        self.map = {"2082802": ("1504358", 378574),
                    "9999": ("7654321", 400001)}
        # Only 1504358 exists already; 7654321 has no row, so injecting it would
        # mint a newly tagged movie rather than merge.
        self.canon = {"1504358": {"id": 361459, "uuid": "abc", "name": "canonical"},
                      "937287": {"id": 5001, "uuid": "def", "name": "Sample Movie"}}
        # Movie index: poster/plot tiers resolve against tagged movies only.
        self.idx = {"poster": {"chall.jpg": "937287"}, "poster_amb": {"ph.jpg"},
                    "plot": {patch._plot_hash(LONG_PLOT): "937287"},
                    "plot_amb": set()}
        self.entry = {"name": "EN - Example Film - 2026 4K", "stream_id": 2082802}

    def _decide(self, entry=None, cfg=None, sid=2082802):
        return patch.decide_movie(entry if entry is not None else self.entry,
                                  "Provider2", sid, self.map, self.canon, self.idx,
                                  cfg or _cfg())

    def test_injects_from_stored_detail(self):
        action, tmdb, tier, _ = self._decide()
        self.assertEqual((action, tmdb, tier), (patch.INJECT, "1504358", patch.TIER_DETAIL))

    def test_int_and_str_stream_ids_both_match(self):
        self.assertEqual(self._decide(sid="2082802")[0], patch.INJECT)

    def test_no_stored_detail_and_no_other_signal(self):
        self.assertEqual(self._decide(sid=999)[0], patch.NO_SIGNAL)

    def test_poster_tier_when_no_stored_detail(self):
        # The Provider1 case: 99% carry TMDB artwork, none carry a listing id.
        entry = {"name": "EN| Sample Movie - 2024 [4K]", "stream_id": 555,
                 "stream_icon": "https://image.tmdb.org/t/p/w500/chall.jpg"}
        action, tmdb, tier, key = self._decide(entry, sid=555)
        self.assertEqual((action, tmdb, tier), (patch.INJECT, "937287", patch.TIER_POSTER))

    def test_plot_tier_when_no_detail_or_poster(self):
        entry = {"name": "EN| Sample Movie - 2024 [4K]", "stream_id": 555,
                 "description": LONG_PLOT}
        self.assertEqual(self._decide(entry, sid=555)[2], patch.TIER_PLOT)

    def test_detail_outranks_poster(self):
        entry = dict(self.entry,
                     stream_icon="https://image.tmdb.org/t/p/w500/chall.jpg")
        self.assertEqual(self._decide(entry)[2], patch.TIER_DETAIL)

    def test_unusable_detail_falls_through_to_poster(self):
        # Detail id nobody holds + tag_unique off: must NOT mask a good poster.
        entry = {"name": "EN| Sample Movie - 2024 [4K]", "stream_id": 9999,
                 "stream_icon": "https://image.tmdb.org/t/p/w500/chall.jpg"}
        action, tmdb, tier, _ = self._decide(entry, sid=9999)
        self.assertEqual((action, tmdb, tier), (patch.INJECT, "937287", patch.TIER_POSTER))

    def test_ambiguous_poster_is_reported(self):
        entry = {"name": "x", "stream_id": 555,
                 "stream_icon": "https://image.tmdb.org/t/p/w500/ph.jpg"}
        self.assertEqual(self._decide(entry, sid=555)[0], patch.AMBIGUOUS)

    def test_poster_match_is_never_a_new_row(self):
        # Poster/plot resolve against the tagged index, so the target always
        # exists -- tag_unique_movies is irrelevant to them.
        entry = {"name": "EN| Sample Movie - 2024 [4K]", "stream_id": 555,
                 "stream_icon": "https://image.tmdb.org/t/p/w500/chall.jpg"}
        for opt in (False, True):
            cfg = _cfg(); cfg["tag_unique_movies"] = opt
            self.assertEqual(self._decide(entry, cfg=cfg, sid=555)[0], patch.INJECT, opt)

    def test_entry_with_its_own_id_is_left_alone(self):
        self.assertEqual(self._decide(dict(self.entry, tmdb_id="1504358"))[0], patch.HAS_ID)

    def test_dry_run(self):
        self.assertEqual(self._decide(cfg=_cfg(dry_run=True))[0], patch.DRY_RUN)

    def test_denylist_by_stream_id(self):
        self.assertEqual(self._decide(cfg=_cfg(denylist={"Provider2:2082802"}))[0], patch.DENIED)

    def test_denylist_by_tmdb(self):
        self.assertEqual(self._decide(cfg=_cfg(denylist={"tmdb:1504358"}))[0], patch.DENIED)

    def test_variant_guard_applies_to_movies(self):
        entry = dict(self.entry, name="Some Film [Black/White]")
        self.assertEqual(self._decide(entry)[0], patch.VARIANT)

    def test_quality_tag_is_not_a_variant(self):
        self.assertEqual(self._decide(dict(self.entry, name="Some Film 4K"))[0],
                         patch.INJECT)

    def test_does_not_mutate_the_entry(self):
        before = dict(self.entry)
        self._decide()
        self.assertEqual(self.entry, before)

    def test_unique_title_is_skipped_by_default(self):
        # Nothing in the library holds 7654321, and no poster/plot to fall back
        # on, so injecting would re-tag a title that already has its own entry.
        entry = {"name": "EN - Another Film - 2026", "stream_id": 9999}
        action, tmdb, tier, _ = self._decide(entry, sid=9999)
        self.assertEqual(action, patch.NO_CANONICAL)
        self.assertEqual(tmdb, "7654321")   # reported, so status can count it
        self.assertEqual(tier, patch.TIER_DETAIL)

    def test_unique_title_injects_when_opted_in(self):
        entry = {"name": "EN - Another Film - 2026", "stream_id": 9999}
        cfg = _cfg()
        cfg["tag_unique_movies"] = True
        self.assertEqual(self._decide(entry, cfg=cfg, sid=9999)[0], patch.INJECT)

    def test_real_merge_is_unaffected_by_that_setting(self):
        for opt in (False, True):
            cfg = _cfg()
            cfg["tag_unique_movies"] = opt
            self.assertEqual(self._decide(cfg=cfg)[0], patch.INJECT, opt)


class MovieApprovalTests(unittest.TestCase):
    """Manual approvals for movies.

    The motivating case is real: a provider shipped a mainstream film with an
    empty `stream_icon`, no plot, and no id in its detail payload, so all three
    derived tiers returned no_signal and no configuration could reach it.
    """

    # Bare entry: no poster, no plot, no detail id. The unreachable case.
    BARE = {"name": "EN| Example Film - 2020", "stream_id": 133452}

    def setUp(self):
        self.map = {"2082802": ("1504358", 378574)}
        self.canon = {"1504358": {"id": 361459, "uuid": "abc", "name": "canonical"},
                      "937287": {"id": 5001, "uuid": "def", "name": "Sample Movie"}}
        self.idx = {"poster": {"chall.jpg": "937287"}, "poster_amb": set(),
                    "plot": {}, "plot_amb": set()}
        self.entry = {"name": "EN - Example Film - 2026 4K",
                      "stream_id": 2082802}

    def _decide(self, entry=None, cfg=None, sid=2082802):
        return patch.decide_movie(entry if entry is not None else self.entry,
                                  "Provider2", sid, self.map, self.canon, self.idx,
                                  cfg or _cfg())

    def test_bare_entry_is_unreachable_without_an_approval(self):
        self.assertEqual(self._decide(self.BARE, sid=133452)[0], patch.NO_SIGNAL)

    def test_approval_reaches_an_otherwise_unreachable_entry(self):
        cfg = _cfg(approvals={("Provider2", "133452"): "508442"})
        action, tmdb, tier, _ = self._decide(self.BARE, cfg=cfg, sid=133452)
        self.assertEqual((action, tmdb, tier),
                         (patch.INJECT, "508442", patch.TIER_MANUAL))

    def test_approval_outranks_detail(self):
        cfg = _cfg(approvals={("Provider2", "2082802"): "999999"})
        action, tmdb, tier, _ = self._decide(cfg=cfg)
        self.assertEqual((action, tmdb, tier),
                         (patch.INJECT, "999999", patch.TIER_MANUAL))

    def test_approval_outranks_poster(self):
        entry = {"name": "EN| Sample Movie - 2024 [4K]", "stream_id": 555,
                 "stream_icon": "https://image.tmdb.org/t/p/w500/chall.jpg"}
        cfg = _cfg(approvals={("Provider2", "555"): "111111"})
        action, tmdb, tier, _ = self._decide(entry, cfg=cfg, sid=555)
        self.assertEqual((action, tmdb, tier),
                         (patch.INJECT, "111111", patch.TIER_MANUAL))

    def test_int_stream_id_matches_a_string_keyed_approval(self):
        # Approvals are parsed from text; stream ids arrive as ints.
        cfg = _cfg(approvals={("Provider2", "133452"): "508442"})
        self.assertEqual(self._decide(self.BARE, cfg=cfg, sid=133452)[0],
                         patch.INJECT)

    def test_approval_is_scoped_to_its_account(self):
        cfg = _cfg(approvals={("Provider1", "133452"): "508442"})
        # self._decide passes account "Provider2", so this must not apply.
        self.assertEqual(self._decide(self.BARE, cfg=cfg, sid=133452)[0],
                         patch.NO_SIGNAL)

    def test_approval_bypasses_the_variant_guard(self):
        # Approving a variant entry by name IS the override, as for series.
        entry = dict(self.BARE, name="Some Film [Black/White]")
        cfg = _cfg(approvals={("Provider2", "133452"): "508442"})
        self.assertEqual(self._decide(entry, cfg=cfg, sid=133452)[0],
                         patch.INJECT)

    def test_denylist_still_overrides_an_approval(self):
        cfg = _cfg(approvals={("Provider2", "133452"): "508442"},
                   denylist={"tmdb:508442"})
        self.assertEqual(self._decide(self.BARE, cfg=cfg, sid=133452)[0],
                         patch.DENIED)

    def test_approval_ignores_tag_unique_movies(self):
        # That gate bounds the detail tier's unattended re-tagging; an approval
        # is one line a person typed, so it applies either way.
        for opt in (False, True):
            cfg = _cfg(approvals={("Provider2", "133452"): "508442"},
                       tag_unique_movies=opt)
            self.assertEqual(self._decide(self.BARE, cfg=cfg, sid=133452)[0],
                             patch.INJECT, opt)

    def test_approval_respects_dry_run(self):
        cfg = _cfg(dry_run=True, approvals={("Provider2", "133452"): "508442"})
        self.assertEqual(self._decide(self.BARE, cfg=cfg, sid=133452)[0],
                         patch.DRY_RUN)

    def test_approval_does_not_override_an_entry_that_has_its_own_id(self):
        entry = dict(self.BARE, tmdb_id="1504358")
        cfg = _cfg(approvals={("Provider2", "133452"): "508442"})
        self.assertEqual(self._decide(entry, cfg=cfg, sid=133452)[0], patch.HAS_ID)

    def test_approval_does_not_mutate_the_entry(self):
        before = dict(self.BARE)
        self._decide(self.BARE, cfg=_cfg(approvals={("Provider2", "133452"): "508442"}),
                     sid=133452)
        self.assertEqual(self.BARE, before)


class IsNewMergeTests(unittest.TestCase):
    """Injection re-runs every scan by design, so the log must record moves, not
    re-derivations -- otherwise one Provider1 category buries the history under 671
    identical entries per refresh."""

    CANON = {"5920": {"id": 10757, "uuid": "u", "name": "canonical"}}

    def test_relation_already_on_target_is_not_logged(self):
        self.assertFalse(patch.is_new_merge(patch.INJECT, "5920", self.CANON, 10757))

    def test_relation_elsewhere_is_logged(self):
        self.assertTrue(patch.is_new_merge(patch.INJECT, "5920", self.CANON, 115897))

    def test_dry_run_follows_the_same_rule(self):
        self.assertFalse(patch.is_new_merge(patch.DRY_RUN, "5920", self.CANON, 10757))
        self.assertTrue(patch.is_new_merge(patch.DRY_RUN, "5920", self.CANON, 999))

    def test_variants_and_denials_always_logged(self):
        for action in (patch.VARIANT, patch.DENIED):
            self.assertTrue(patch.is_new_merge(action, "5920", self.CANON, 10757), action)

    def test_unknown_target_or_current_is_logged(self):
        # A new tagged row has no canonical yet; better to over-record than lose it.
        self.assertTrue(patch.is_new_merge(patch.INJECT, "999999", self.CANON, 10757))
        self.assertTrue(patch.is_new_merge(patch.INJECT, "5920", self.CANON, None))


class CategoryEnabledTests(unittest.TestCase):
    """A scan fetches the provider's whole listing and discards disabled
    categories itself, after the wrapper has seen them. Without this check the
    log filled with merges that never happened -- 5,000 entries against 438 real
    relations on one library."""

    class Cat:
        def __init__(self, cid): self.id = cid

    class Rel:
        def __init__(self, enabled): self.enabled = enabled

    def setUp(self):
        self.categories = {"7": self.Cat(70), "__uncategorized__": self.Cat(99)}
        self.relations = {70: self.Rel(False), 99: self.Rel(True)}

    def test_disabled_category_is_skipped(self):
        self.assertFalse(patch.category_enabled(
            {"category_id": "7"}, self.categories, self.relations))

    def test_enabled_category_passes(self):
        self.relations[70] = self.Rel(True)
        self.assertTrue(patch.category_enabled(
            {"category_id": 7}, self.categories, self.relations))

    def test_uncategorized_falls_back_to_that_relation(self):
        self.assertTrue(patch.category_enabled({}, self.categories, self.relations))
        self.relations[99] = self.Rel(False)
        self.assertFalse(patch.category_enabled({}, self.categories, self.relations))

    def test_unknown_category_passes(self):
        # No uncategorized entry to fall back on: better a wasted lookup than a
        # missed merge.
        self.assertTrue(patch.category_enabled({"category_id": "999"}, {"5": self.Cat(50)}, {}))

    def test_missing_context_fails_open(self):
        for cats, rels in ((None, None), ({}, {}), ({"7": self.Cat(70)}, None)):
            self.assertTrue(patch.category_enabled({"category_id": "7"}, cats, rels))

    def test_malformed_input_fails_open(self):
        self.assertTrue(patch.category_enabled(None, self.categories, self.relations))
        self.assertTrue(patch.category_enabled({"category_id": "7"}, "junk", "junk"))


class FreeSignalMatchTests(unittest.TestCase):
    """The sweep must not spend a provider call on something the poster or plot
    tier already reaches for free."""

    IDX = {"poster": {"chall.jpg": "937287"}, "poster_amb": {"ph.jpg"},
           "plot": {patch._plot_hash(LONG_PLOT): "5920"},
           "plot_amb": {patch._plot_hash(OTHER_PLOT)}}

    def test_poster_match_is_free(self):
        basic = {"stream_icon": "https://image.tmdb.org/t/p/w500/chall.jpg"}
        self.assertEqual(patch.free_signal_match(basic, self.IDX), "937287")

    def test_plot_match_is_free(self):
        self.assertEqual(patch.free_signal_match({"description": LONG_PLOT}, self.IDX),
                         "5920")

    def test_unknown_poster_still_needs_a_fetch(self):
        basic = {"stream_icon": "https://image.tmdb.org/t/p/w500/zz.jpg"}
        self.assertIsNone(patch.free_signal_match(basic, self.IDX))

    def test_ambiguous_keys_do_not_count_as_free(self):
        self.assertIsNone(patch.free_signal_match(
            {"stream_icon": "https://image.tmdb.org/t/p/w500/ph.jpg"}, self.IDX))
        self.assertIsNone(patch.free_signal_match({"description": OTHER_PLOT}, self.IDX))

    def test_no_signal_needs_a_fetch(self):
        self.assertIsNone(patch.free_signal_match({}, self.IDX))
        self.assertIsNone(patch.free_signal_match({"stream_icon": ""}, self.IDX))


class SweepHourTests(unittest.TestCase):
    def test_valid_hours(self):
        for h in (0, 4, 23, "7"):
            self.assertEqual(patch._sweep_hour(h), int(h))

    def test_out_of_range_and_junk_fall_back(self):
        for bad in (-1, 24, 99, None, "", "nope"):
            got = patch._sweep_hour(bad)
            self.assertEqual(got, patch.DEFAULT_SWEEP_HOUR, repr(bad))

    def test_float_truncates_to_a_valid_hour(self):
        # A number field can hand back a float; 3.9 meaning 03:00 is fine.
        self.assertEqual(patch._sweep_hour(3.9), 3)

    def test_schedule_names_are_namespaced(self):
        # A collision with another plugin's PeriodicTask row would have one
        # silently overwrite the other's schedule.
        self.assertIn("vod_merge", patch.SWEEP_PERIODIC_NAME)
        self.assertTrue(patch.SWEEP_TASK_PATH.startswith(patch.PLUGIN_KEY + "."))


class DecisionShapeTests(unittest.TestCase):
    """Regression: `tier` was added to the decision tuple but one comprehension
    kept unpacking four values, so every movie batch raised after the entries had
    already been mutated. Merges landed; the audit never ran; the wrapper's own
    except hid it. Named fields make positional drift impossible."""

    def test_fields_are_named(self):
        self.assertEqual(patch.Decision._fields,
                         ("entry", "external_id", "action", "tmdb_id", "tier", "key"))

    def test_construction_requires_every_field(self):
        with self.assertRaises(TypeError):
            patch.Decision({}, 1, patch.INJECT, "5920", patch.TIER_POSTER)

    def test_attribute_and_tuple_access_agree(self):
        d = patch.Decision({"name": "x"}, 42, patch.INJECT, "5920", patch.TIER_PLOT, "k")
        entry, ext, action, tmdb, tier, key = d
        self.assertEqual((ext, action, tmdb, tier, key),
                         (d.external_id, d.action, d.tmdb_id, d.tier, d.key))


class ModuleStateTests(unittest.TestCase):
    """Catch undefined module globals that py_compile cannot see.

    Both index caches are only touched by Django-backed code paths, so a missing
    global sits there compiling cleanly until it blows up in the container.
    `invalidate_index_cache` reaches every lock and cache variable and needs no
    database, which makes it a cheap canary.
    """

    def test_invalidate_touches_every_cache_global(self):
        patch.invalidate_index_cache()   # raises NameError if any global is missing
        patch.invalidate_config_cache()

    def test_expected_state_globals_exist(self):
        for name in ("_ix_lock", "_ix_cache", "_ix_cache_ts",
                     "_mix_lock", "_mix_cache", "_mix_cache_ts",
                     "_cfg_lock", "_cfg_cache", "_cfg_cache_ts",
                     "_orig_process_series_batch", "_orig_process_movie_batch"):
            self.assertTrue(hasattr(patch, name), name)


class DecideConflictTests(unittest.TestCase):
    """The protection against core's destructive movie merge.

    Core keeps the movie being refreshed and DELETES the pre-existing one. On
    this path the movie being refreshed is the freshly-minted orphan, so that
    destroys the canonical. We repoint the relation instead. The decision is
    pure; the caller does the queries.
    """

    def test_no_holder_is_a_noop(self):
        # Nothing else claims the id: core just sets it, which is benign, so we
        # must NOT interfere.
        self.assertEqual(patch.decide_conflict(5, None, None, True),
                         (patch.PROTECT_NOOP, None))

    def test_holder_is_the_current_row_is_a_noop(self):
        self.assertEqual(patch.decide_conflict(5, 5, None, True),
                         (patch.PROTECT_SAME, None))

    def test_another_row_holds_it_repoints(self):
        self.assertEqual(patch.decide_conflict(5, 9, None, True),
                         (patch.PROTECT_REPOINT, 9))

    def test_tmdb_holder_wins_over_imdb_holder(self):
        # Matches core's stated preference for the TMDB match.
        self.assertEqual(patch.decide_conflict(5, 9, 7, True),
                         (patch.PROTECT_REPOINT, 9))

    def test_imdb_holder_used_when_no_tmdb_holder(self):
        self.assertEqual(patch.decide_conflict(5, None, 7, True),
                         (patch.PROTECT_REPOINT, 7))

    def test_imdb_holder_that_is_current_row_is_same(self):
        self.assertEqual(patch.decide_conflict(5, None, 5, True),
                         (patch.PROTECT_SAME, None))

    def test_without_a_relation_we_do_nothing_rather_than_delete(self):
        # `relation` is an unused parameter in core's version, so it could be
        # absent. Core would delete the canonical here; doing nothing is safer.
        self.assertEqual(patch.decide_conflict(5, 9, None, False),
                         (patch.PROTECT_NOOP, None))

    def test_never_returns_a_delete_action(self):
        # There is deliberately no destructive outcome in the vocabulary.
        actions = {patch.PROTECT_NOOP, patch.PROTECT_SAME, patch.PROTECT_REPOINT}
        for args in ((5, None, None, True), (5, 5, None, True), (5, 9, None, True),
                     (5, 9, 7, True), (5, None, 7, True), (5, 9, None, False)):
            self.assertIn(patch.decide_conflict(*args)[0], actions)

    def test_repoint_target_is_never_the_current_row(self):
        for tmdb_holder, imdb_holder in ((5, None), (None, 5), (5, 5)):
            action, target = patch.decide_conflict(5, tmdb_holder, imdb_holder, True)
            self.assertNotEqual(target, 5)
            self.assertEqual(action, patch.PROTECT_SAME)

    def test_protection_defaults_on_and_is_not_tied_to_dry_run(self):
        self.assertIs(patch.DEFAULT_PROTECT_MERGES, True)
        # dry_run is about injection; it must not disable the protection.
        self.assertNotIn("protect", patch.DRY_RUN)


class TrimMatchesTests(unittest.TestCase):
    """The status actions write the FULL report to a file and send a trimmed copy
    back in the API response. Trimming the file too would make the message's
    'Full list' pointer a lie, so this must not mutate its input."""

    def _report(self, n):
        return {"tally": {"inject": n},
                "matches": [{"i": i} for i in range(n)]}

    def test_short_report_is_returned_unchanged(self):
        r = self._report(5)
        self.assertIs(patch.trim_matches(r, cap=200), r)

    def test_long_report_is_capped_and_flagged(self):
        got = patch.trim_matches(self._report(500), cap=200)
        self.assertEqual(len(got["matches"]), 200)
        self.assertEqual(got["matches_total"], 500)
        self.assertTrue(got["matches_truncated"])

    def test_input_is_not_mutated(self):
        r = self._report(500)
        patch.trim_matches(r, cap=200)
        self.assertEqual(len(r["matches"]), 500)
        self.assertNotIn("matches_truncated", r)

    def test_exactly_at_the_cap_is_not_flagged(self):
        got = patch.trim_matches(self._report(200), cap=200)
        self.assertNotIn("matches_truncated", got)

    def test_cap_of_zero_disables_trimming(self):
        r = self._report(500)
        self.assertIs(patch.trim_matches(r, cap=0), r)

    def test_survives_reports_with_no_matches_key(self):
        for r in ({}, {"tally": {}}, {"matches": None}, {"matches": "nope"}):
            self.assertIs(patch.trim_matches(r, cap=10), r)


class SignatureParityTests(unittest.TestCase):
    """Every wrapper must accept what core passes AND forward it.

    A signature mismatch raises during argument BINDING -- before the function
    body, so before any `_ACTIVE` check and before the wrapper's own
    `try/except`. Fail-open design does not cover it, and the plugin's own
    enable setting cannot either. It is the one upstream change that turns
    "quietly stopped helping" into a hard failure; a sibling plugin took a total
    VOD-playback outage that way when a hooked function gained a keyword.

    These tests deliberately encode NO parameter list, so they survive the next
    added argument as well as this one. Accepting without forwarding is the
    subtler bug -- it silently drops something core was relying on -- so each
    case asserts the extras arrived at the original.
    """

    def setUp(self):
        self.seen = []
        self._saved = {}
        for name in ("_orig_process_series_batch", "_orig_process_movie_batch",
                     "_orig_handle_movie_id_conflicts",
                     "_orig_refresh_movie_advanced_data",
                     "_orig_cleanup_orphaned_vod_content"):
            self._saved[name] = getattr(patch, name)
        self._saved["is_enabled"] = patch.is_enabled
        self._saved["_load_config"] = patch._load_config
        # Keep the wrappers on their delegate-only paths: this suite is about
        # argument plumbing, not injection or protection behavior.
        patch.is_enabled = lambda: False
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self._saved.items():
            setattr(patch, name, value)

    def _spy(self, retval=None):
        def fake(*a, **kw):
            self.seen.append((a, kw))
            return retval
        return fake

    def test_cleanup_orphaned_vod_content_accepts_and_forwards(self):
        patch._orig_cleanup_orphaned_vod_content = self._spy("ok")
        # Delegate-only path, like the rest of this suite.
        patch._load_config = lambda force=False: {"protect_prune": False}
        result = patch.patched_cleanup_orphaned_vod_content(
            0, "scan_start", 7, "extra_positional", future_kwarg="x",
        )
        self.assertEqual(result, "ok")
        (args, kwargs), = self.seen
        self.assertIn("extra_positional", args)
        self.assertEqual(kwargs.get("future_kwarg"), "x")

    def test_series_batch_accepts_and_forwards(self):
        patch._orig_process_series_batch = self._spy("ok")
        result = patch.patched_process_series_batch(
            "acct", [], {}, {}, "scan_start", "extra_positional",
            future_kwarg="x",
        )
        self.assertEqual(result, "ok")
        (args, kwargs), = self.seen
        self.assertIn("extra_positional", args)
        self.assertEqual(kwargs.get("future_kwarg"), "x")

    def test_movie_batch_accepts_and_forwards(self):
        patch._orig_process_movie_batch = self._spy("ok")
        result = patch.patched_process_movie_batch(
            "acct", [], {}, {}, "scan_start", "extra_positional",
            future_kwarg="x",
        )
        self.assertEqual(result, "ok")
        (args, kwargs), = self.seen
        self.assertIn("extra_positional", args)
        self.assertEqual(kwargs.get("future_kwarg"), "x")

    def test_refresh_movie_advanced_data_accepts_and_forwards(self):
        patch._orig_refresh_movie_advanced_data = self._spy("done")
        patch._load_config = lambda force=False: {"preserve_detail": False}
        result = patch.patched_refresh_movie_advanced_data(
            123, True, "extra_positional", future_kwarg="x",
        )
        self.assertEqual(result, "done")
        (args, kwargs), = self.seen
        # force_refresh must stay positional so a new third parameter lands in
        # the right slot rather than colliding with it.
        self.assertEqual(args[0], 123)
        self.assertEqual(args[1], True)
        self.assertIn("extra_positional", args)
        self.assertEqual(kwargs.get("future_kwarg"), "x")

    def test_handle_movie_id_conflicts_forwards_when_delegating(self):
        # With protection off this wrapper delegates, and must pass extras on.
        patch._orig_handle_movie_id_conflicts = self._spy(("movie", True))
        patch._load_config = lambda force=False: {"protect_merges": False}
        result = patch.patched_handle_movie_id_conflicts(
            "cur", "rel", "tmdb", "imdb", "extra_positional", future_kwarg="x",
        )
        self.assertEqual(result, ("movie", True))
        (args, kwargs), = self.seen
        self.assertIn("extra_positional", args)
        self.assertEqual(kwargs.get("future_kwarg"), "x")

    def test_unexpected_args_are_reported_once_per_shape(self):
        # Forwarding stops the crash; it does not make us correct. An unhandled
        # new parameter must leave a trace rather than vanish.
        patch._extra_logged.clear()
        self.addCleanup(patch._extra_logged.clear)
        with self.assertLogs("plugins.dispatcharr_vod_merge", level="WARNING") as cm:
            patch._note_unexpected_args("some_hook", ("a",), {"b": 1})
        self.assertTrue(any("does not know" in m for m in cm.output))

        # Same shape again: silent, because these are hot paths.
        patch._note_unexpected_args("some_hook", ("a",), {"b": 1})
        self.assertEqual(len(patch._extra_logged), 1)

    def test_every_wrapper_accepts_extra_args(self):
        """Structural, so it covers wrappers that do not exist yet.

        The per-wrapper tests above each name a function; this one asserts the
        rule itself, so a NEW wrapper added without `*args, **kwargs` fails the
        suite even though nobody wrote a test for it.
        """
        import ast
        import inspect
        import os

        source = os.path.join(
            os.path.dirname(os.path.abspath(inspect.getfile(patch))), "patch.py"
        )
        with open(source, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())

        wrappers = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name.startswith("patched_")
        ]
        self.assertTrue(wrappers, "no wrappers found -- test is not testing anything")

        unhardened = [
            node.name for node in wrappers
            if node.args.vararg is None or node.args.kwarg is None
        ]
        self.assertEqual(
            unhardened, [],
            "these wrappers would raise TypeError at argument binding if "
            "Dispatcharr adds a parameter, which fails ahead of every guard "
            "they have: %s" % unhardened,
        )

    def test_no_report_when_core_passes_nothing_new(self):
        patch._extra_logged.clear()
        self.addCleanup(patch._extra_logged.clear)
        patch._note_unexpected_args("some_hook", (), {})
        self.assertEqual(patch._extra_logged, set())


class LogLevelAdoptionTests(unittest.TestCase):
    """The plugin's logger must carry its own level.

    Plugin loggers have no entry in Dispatcharr's logging config, so without
    this they inherit root -- and billiard leaves root at WARNING in Celery
    prefork children, which is exactly where the VOD scan runs. The result was
    that every scan-time line this plugin emits was discarded at the logger,
    making a working wrapper indistinguishable from an absent one. Pinned
    because the failure is invisible by construction.
    """

    def setUp(self):
        self.plugin_logger = logging.getLogger("plugins.dispatcharr_vod_merge")
        self.apps_logger = logging.getLogger("apps")
        self._plugin_level = self.plugin_logger.level
        self._apps_level = self.apps_logger.level
        self.addCleanup(self.plugin_logger.setLevel, self._plugin_level)
        self.addCleanup(self.apps_logger.setLevel, self._apps_level)

    def test_module_logger_does_not_inherit_root(self):
        # As imported, patch.py must already have given it a level.
        self.assertNotEqual(patch.logger.level, logging.NOTSET)

    def test_adopts_the_apps_logger_level(self):
        # Respects DISPATCHARR_LOG_LEVEL rather than forcing INFO: `apps` keeps
        # its configured level in prefork children, which is why its records
        # survive where ours did not.
        self.apps_logger.setLevel(logging.DEBUG)
        self.plugin_logger.setLevel(logging.NOTSET)
        patch._adopt_log_level()
        self.assertEqual(self.plugin_logger.level, logging.DEBUG)

    def test_does_not_override_a_level_already_set(self):
        # A caller or test that deliberately silences this logger keeps control.
        self.plugin_logger.setLevel(logging.CRITICAL)
        patch._adopt_log_level()
        self.assertEqual(self.plugin_logger.level, logging.CRITICAL)

    def test_info_survives_a_warning_root(self):
        # The exact production condition: root at WARNING, our logger with a
        # level of its own, handler on root. The record must still be emitted.
        root = logging.getLogger()
        prev_root_level = root.level
        self.addCleanup(root.setLevel, prev_root_level)
        root.setLevel(logging.WARNING)

        seen = []

        class Capture(logging.Handler):
            def emit(self, record):
                seen.append(record.getMessage())

        handler = Capture(level=0)
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)

        self.apps_logger.setLevel(logging.INFO)
        self.plugin_logger.setLevel(logging.NOTSET)
        patch._adopt_log_level()
        self.plugin_logger.info("scan-time line")

        self.assertIn("scan-time line", seen)

    def test_inheriting_root_would_have_dropped_it(self):
        # The negative control: without a level of its own the record is
        # discarded at the logger, which is the bug this guards.
        root = logging.getLogger()
        prev_root_level = root.level
        self.addCleanup(root.setLevel, prev_root_level)
        root.setLevel(logging.WARNING)

        self.plugin_logger.setLevel(logging.NOTSET)
        self.assertFalse(self.plugin_logger.isEnabledFor(logging.INFO))


class RestoreEssentialTests(unittest.TestCase):
    """A core detail refresh REPLACES `detailed_info` wholesale, so a payload
    that arrives thinner than the last one erases the difference. The guard
    refills only the essential keys, and only where the new payload left a
    hole."""

    VIDEO = {"width": 3840, "height": 2160, "codec_name": "hevc"}
    AUDIO = {"codec_name": "ac3", "channels": 6}

    def test_dropped_keys_are_restored(self):
        old = {"tmdb_id": "12345", "video": self.VIDEO, "audio": self.AUDIO}
        new = {"plot": "A blurb.", "genre": "Drama"}
        got, restored = patch.restore_essential(old, new)
        self.assertEqual(sorted(restored), ["audio", "tmdb_id", "video"])
        self.assertEqual(got["video"], self.VIDEO)
        self.assertEqual(got["audio"], self.AUDIO)
        self.assertEqual(got["tmdb_id"], "12345")
        # Everything the new payload DID carry survives untouched.
        self.assertEqual(got["plot"], "A blurb.")
        self.assertEqual(got["genre"], "Drama")

    def test_provider_value_wins_over_the_stored_one(self):
        # The point of the refresh is to take the provider's word for it. The
        # guard must never reinstate stale data over a real answer.
        old = {"tmdb_id": "111", "video": self.VIDEO}
        new = {"tmdb_id": "222", "video": {"width": 1920, "height": 1080}}
        got, restored = patch.restore_essential(old, new)
        self.assertEqual(restored, [])
        self.assertEqual(got["tmdb_id"], "222")
        self.assertEqual(got["video"]["width"], 1920)

    def test_partial_drop_restores_only_the_missing_key(self):
        old = {"tmdb_id": "12345", "video": self.VIDEO, "audio": self.AUDIO}
        new = {"tmdb_id": "12345", "video": self.VIDEO}
        got, restored = patch.restore_essential(old, new)
        self.assertEqual(restored, ["audio"])
        self.assertEqual(got["audio"], self.AUDIO)

    def test_empty_dict_counts_as_dropped(self):
        # clean_custom_properties strips None/''/[] but NOT {}, so an empty
        # video block reaches storage looking like real data. If it were
        # treated as present the hole would never be refilled.
        old = {"video": self.VIDEO, "audio": self.AUDIO}
        new = {"video": {}, "audio": None}
        got, restored = patch.restore_essential(old, new)
        self.assertEqual(sorted(restored), ["audio", "video"])
        self.assertEqual(got["video"], self.VIDEO)

    def test_placeholder_ids_count_as_dropped(self):
        for placeholder in ("", "0", "none", "null"):
            got, restored = patch.restore_essential(
                {"tmdb_id": "12345"}, {"tmdb_id": placeholder})
            self.assertEqual(restored, ["tmdb_id"], placeholder)
            self.assertEqual(got["tmdb_id"], "12345", placeholder)

    def test_a_placeholder_is_not_worth_restoring_either(self):
        # Symmetry: a stored "0" is not data, so its absence is not a loss.
        got, restored = patch.restore_essential({"tmdb_id": "0"}, {"plot": "x"})
        self.assertEqual(restored, [])
        self.assertNotIn("tmdb_id", got)

    def test_nothing_stored_means_nothing_to_do(self):
        # The bulk case: a relation fetched for the first time. No prior
        # payload, so the guard is a no-op and costs nothing.
        new = {"plot": "A blurb."}
        got, restored = patch.restore_essential(None, new)
        self.assertEqual(restored, [])
        self.assertEqual(got, new)
        self.assertEqual(patch.restore_essential({}, new), (new, []))

    def test_identical_payload_restores_nothing(self):
        # What core's 24h skip looks like from here: before == after.
        same = {"tmdb_id": "12345", "video": self.VIDEO, "audio": self.AUDIO}
        got, restored = patch.restore_essential(same, dict(same))
        self.assertEqual(restored, [])
        self.assertEqual(got, same)

    def test_non_essential_keys_are_never_restored(self):
        # Deliberately narrow: plot, genre and the rest are core's to manage,
        # and refilling them would fight the refresh rather than repair it.
        old = {"plot": "The old blurb.", "genre": "Drama", "rating": "7"}
        got, restored = patch.restore_essential(old, {"plot": ""})
        self.assertEqual(restored, [])
        self.assertNotIn("genre", got)

    def test_input_is_not_mutated(self):
        old = {"tmdb_id": "12345", "video": self.VIDEO}
        new = {"plot": "A blurb."}
        patch.restore_essential(old, new)
        self.assertEqual(new, {"plot": "A blurb."})
        self.assertEqual(sorted(old), ["tmdb_id", "video"])

    def test_junk_types_do_not_raise(self):
        # `detailed_info` is provider-shaped JSON; a list or a string there is
        # not impossible, and the guard runs on a user-facing request path.
        for junk in ([], "text", 7, None):
            got, restored = patch.restore_essential(junk, {"plot": "x"})
            self.assertEqual(restored, [])
            got, restored = patch.restore_essential({"video": self.VIDEO}, junk)
            self.assertEqual(restored, ["video"])


class SweepWritesOnlyItsOwnKeysTests(unittest.TestCase):
    """The sweep must not write core's `detailed_fetched` /
    `last_advanced_refresh`. Those two fields are the only record of a CLIENT
    asking for a movie's detail, and writing them makes every relation we
    swept look like one someone requested. Pinned here because the mistake is
    silent and easy to reintroduce while editing the sweep."""

    def test_sweep_does_not_write_cores_fields(self):
        import inspect
        src = inspect.getsource(patch.sweep_movies_impl)
        self.assertNotIn('"detailed_fetched"', src)
        self.assertNotIn("last_advanced_refresh = ", src)

    def test_sweep_stamps_our_namespaced_key_instead(self):
        import inspect
        src = inspect.getsource(patch.sweep_movies_impl)
        self.assertIn("_stamp_own", src)

    def test_stamp_merges_rather_than_overwrites(self):
        # A later pass (an ffprobe stamp) must not erase an earlier one.
        props = {"basic_data": {"name": "Example Film"},
                 patch.OWN_PROPS_KEY: {"detail_at": "2026-01-01T00:00:00"}}
        patch._stamp_own(props, probe_at="2026-02-02T00:00:00")
        self.assertEqual(props[patch.OWN_PROPS_KEY], {
            "detail_at": "2026-01-01T00:00:00",
            "probe_at": "2026-02-02T00:00:00",
        })
        self.assertIn("basic_data", props)

    def test_stamp_survives_a_missing_or_junk_key(self):
        for existing in ({}, {patch.OWN_PROPS_KEY: None},
                         {patch.OWN_PROPS_KEY: "junk"}):
            props = dict(existing)
            patch._stamp_own(props, detail_at="now")
            self.assertEqual(props[patch.OWN_PROPS_KEY], {"detail_at": "now"})

    def test_resumability_does_not_depend_on_the_dropped_flag(self):
        # The sweep skips on stored detail, never on detailed_fetched, which
        # is why dropping the flag costs nothing here.
        import inspect
        src = inspect.getsource(patch.sweep_movies_impl)
        self.assertIn('props.get("detailed_info")', src)


class DecidePruneTests(unittest.TestCase):
    """Refuse a cleanup that follows a scan which saw nothing of an account.

    The live failure this exists for: a provider's movie endpoint returned an
    empty list, so no relation was re-stamped, so core's `stale_days=0` filter
    matched every one of them and deleted the account's entire movie catalog
    plus every title it solely supplied -- while series in the same run
    processed normally.
    """

    def test_allows_when_everything_was_seen(self):
        allow, reason = patch.decide_prune(
            {"movie": (100, 100), "series": (50, 50)})
        self.assertTrue(allow)
        self.assertIsNone(reason)

    def test_allows_a_merely_short_scan(self):
        # DELIBERATE: a short listing is not refused. Only zero is unambiguous.
        # A ratio rule would deadlock on a genuinely shrinking catalog -- the
        # unpruned rows keep the total high, so the ratio could never recover.
        allow, reason = patch.decide_prune({"movie": (100, 3), "series": (50, 50)})
        self.assertTrue(allow)
        self.assertIsNone(reason)

    def test_refuses_when_movies_exist_but_none_were_seen(self):
        allow, reason = patch.decide_prune(
            {"movie": (12345, 0), "series": (6789, 6789)})
        self.assertFalse(allow)
        self.assertIn("movie", reason)
        self.assertIn("12345", reason)

    def test_refuses_when_series_exist_but_none_were_seen(self):
        allow, reason = patch.decide_prune({"movie": (10, 10), "series": (99, 0)})
        self.assertFalse(allow)
        self.assertIn("series", reason)

    def test_an_account_with_nothing_is_not_suspicious(self):
        # A provider that genuinely carries no movies must keep being able to
        # return an empty list, or the guard would latch on for ever.
        allow, reason = patch.decide_prune({"movie": (0, 0), "series": (0, 0)})
        self.assertTrue(allow)
        self.assertIsNone(reason)


class PruneGuardTests(unittest.TestCase):
    """The wrapper around core's cleanup: when it defers, and when it refuses."""

    def setUp(self):
        self._saved = {
            name: getattr(patch, name) for name in (
                "_orig_cleanup_orphaned_vod_content", "_load_config",
                "_prune_counts", "_append_log",
            )
        }
        self.calls = []
        patch._orig_cleanup_orphaned_vod_content = self._spy
        patch._load_config = lambda force=False: {"protect_prune": True}
        patch._append_log = lambda entries: None
        patch._prune_counts = lambda account_id, cutoff: {
            "movie": (100, 100), "series": (10, 10)}
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self._saved.items():
            setattr(patch, name, value)

    def _spy(self, *a, **kw):
        self.calls.append((a, kw))
        return "core ran"

    def _call(self, **kw):
        params = {"stale_days": 0, "scan_start_time": datetime(2026, 1, 1),
                  "account_id": 7}
        params.update(kw)
        return patch.patched_cleanup_orphaned_vod_content(**params)

    def test_delegates_when_the_guard_is_off(self):
        patch._load_config = lambda force=False: {"protect_prune": False}
        patch._prune_counts = lambda account_id, cutoff: {"movie": (5, 0)}
        self.assertEqual(self._call(), "core ran")
        self.assertEqual(len(self.calls), 1)

    def test_delegates_when_there_is_no_scan_to_assess(self):
        # An unscoped or timestamp-less call cannot be judged against a scan, so
        # it is passed through rather than blocked on a guess.
        self.assertEqual(self._call(account_id=None), "core ran")
        self.assertEqual(self._call(scan_start_time=None), "core ran")
        self.assertEqual(len(self.calls), 2)

    def test_delegates_on_a_healthy_scan(self):
        self.assertEqual(self._call(), "core ran")
        self.assertEqual(len(self.calls), 1)

    def test_refuses_and_does_not_call_core(self):
        patch._prune_counts = lambda account_id, cutoff: {
            "movie": (12345, 0), "series": (6789, 6789)}
        result = self._call()
        self.assertEqual(self.calls, [])
        self.assertIn("dispatcharr_vod_merge", result)
        self.assertIn("movie", result)

    def test_refusal_is_logged_to_the_audit_trail(self):
        written = []
        patch._append_log = lambda entries: written.extend(entries)
        patch._prune_counts = lambda account_id, cutoff: {"movie": (12, 0)}
        self._call()
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["action"], patch.PRUNE_BLOCKED)

    def test_a_broken_audit_write_still_refuses(self):
        def boom(entries):
            raise RuntimeError("log unavailable")
        patch._append_log = boom
        patch._prune_counts = lambda account_id, cutoff: {"movie": (12, 0)}
        result = self._call()
        self.assertEqual(self.calls, [])
        self.assertIn("dispatcharr_vod_merge", result)

    def test_fails_open_when_the_check_itself_breaks(self):
        # Opposite of the merge protection, on purpose: core's cleanup is
        # normally correct, so an error in OUR check is not evidence that the
        # dangerous condition holds, and permanently disabling a correct cleanup
        # would be its own slow data problem.
        def boom(account_id, cutoff):
            raise RuntimeError("db unavailable")
        patch._prune_counts = boom
        self.assertEqual(self._call(), "core ran")
        self.assertEqual(len(self.calls), 1)

    def test_cutoff_mirrors_cores_own_staleness_arithmetic(self):
        seen = {}

        def capture(account_id, cutoff):
            seen["cutoff"] = cutoff
            return {"movie": (5, 5)}

        patch._prune_counts = capture
        start = datetime(2026, 1, 10)
        self._call(stale_days=3, scan_start_time=start)
        # "Seen" must mean exactly "would survive core's filter", so the cutoff
        # has to be computed the same way core computes it.
        self.assertEqual(seen["cutoff"], start - timedelta(days=3))

    def test_missing_original_is_not_an_exception(self):
        patch._orig_cleanup_orphaned_vod_content = None
        self.assertIsNone(self._call())


NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=_tz.utc)


def _doc(**over):
    base = {"schema": 1, "generated_at": "2026-10-04T08:00:00Z",
            "generator": "x", "count": 2, "tmdb_ids": [1, 2],
            "unidentified": []}
    base.update(over)
    return base


class ValidateWantedSetTests(unittest.TestCase):
    """FAIL CLOSED ON SCOPE. Every rejection here must mean "do nothing" --
    never "do everything", which is the expensive direction."""

    def test_a_good_document_passes(self):
        self.assertEqual(patch.validate_wanted_set(_doc(), now=NOW), (True, None))

    def test_unknown_schema_is_refused(self):
        ok, why = patch.validate_wanted_set(_doc(schema=2), now=NOW)
        self.assertFalse(ok)
        self.assertIn("schema", why)

    def test_count_disagreeing_with_the_array_is_refused(self):
        # Their writer runs at the end of a sync that can abort part-way, so
        # this is the cheapest corruption signal available to either side.
        ok, why = patch.validate_wanted_set(_doc(count=3), now=NOW)
        self.assertFalse(ok)
        self.assertIn("count", why)

    def test_missing_or_wrong_typed_arrays_are_refused(self):
        self.assertFalse(patch.validate_wanted_set(_doc(tmdb_ids=None), now=NOW)[0])
        self.assertFalse(patch.validate_wanted_set(
            _doc(unidentified="nope"), now=NOW)[0])
        self.assertFalse(patch.validate_wanted_set("not a dict", now=NOW)[0])

    def test_unparseable_timestamp_is_refused(self):
        ok, why = patch.validate_wanted_set(_doc(generated_at="last tuesday"),
                                            now=NOW)
        self.assertFalse(ok)
        self.assertIn("generated_at", why)

    def test_a_stale_set_is_refused(self):
        old = (NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ok, why = patch.validate_wanted_set(_doc(generated_at=old), now=NOW)
        self.assertFalse(ok)
        self.assertIn("old", why)

    def test_an_empty_set_is_refused(self):
        ok, _ = patch.validate_wanted_set(
            _doc(count=0, tmdb_ids=[], unidentified=[]), now=NOW)
        self.assertFalse(ok)

    def test_unidentified_only_is_legitimate(self):
        ok, why = patch.validate_wanted_set(
            _doc(count=0, tmdb_ids=[], unidentified=[{"stream_id": 1}]), now=NOW)
        self.assertTrue(ok)
        self.assertIsNone(why)


class ReadWantedSetTests(unittest.TestCase):
    """Absent and unreadable must NOT look alike. Absent is normal; unreadable
    is a misconfiguration, and collapsing them lets a permissions mistake
    silently disable the whole feature with no symptom."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _write(self, text):
        p = os.path.join(self.dir, "wanted-set.json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def test_no_path_configured(self):
        self.assertEqual(patch.read_wanted_set("", now=NOW)[1], "disabled")

    def test_absent_file(self):
        self.assertEqual(
            patch.read_wanted_set(os.path.join(self.dir, "nope.json"), now=NOW)[1],
            "absent")

    def test_unreadable_is_distinct_from_absent(self):
        # A directory stands in for any open() failure; the point is only that
        # it lands in a different bucket from "absent".
        doc, status, _ = patch.read_wanted_set(self.dir, now=NOW)
        self.assertIsNone(doc)
        self.assertEqual(status, "unreadable")

    def test_malformed_json(self):
        self.assertEqual(
            patch.read_wanted_set(self._write("{nope"), now=NOW)[1], "malformed")

    def test_invalid_contents(self):
        p = self._write(json.dumps(_doc(count=99)))
        doc, status, why = patch.read_wanted_set(p, now=NOW)
        self.assertIsNone(doc)
        self.assertEqual(status, "invalid")
        self.assertIn("count", why)

    def test_a_good_file_reads(self):
        p = self._write(json.dumps(_doc()))
        doc, status, _ = patch.read_wanted_set(p, now=NOW)
        self.assertEqual(status, "ok")
        self.assertEqual(doc["tmdb_ids"], [1, 2])


class ResolveUnidentifiedTests(unittest.TestCase):
    """Strict order: stream_id, then a UNIQUE name, and resolved_tmdb_id only
    ever as a corroborator -- it comes from an unverified first-result lookup,
    so on its own it could silently enrich the wrong movie."""

    def _lookups(self, by_id=None, by_name=None):
        return (lambda s: (by_id or {}).get(s),
                lambda n: (by_name or {}).get(n, []))

    def test_live_stream_id_wins(self):
        bid, bname = self._lookups(by_id={7: (7, None)})
        self.assertEqual(
            patch.resolve_unidentified({"stream_id": 7, "name": "x"}, bid, bname),
            (7, "stream_id", None))

    def test_dead_stream_id_falls_back_to_a_unique_name(self):
        bid, bname = self._lookups(by_name={"x": [(42, None)]})
        mid, how, why = patch.resolve_unidentified(
            {"stream_id": 7, "name": "x"}, bid, bname)
        self.assertEqual((mid, how, why), (42, "name", None))

    def test_ambiguous_name_is_skipped_not_guessed(self):
        bid, bname = self._lookups(by_name={"x": [(1, None), (2, None)]})
        mid, _, why = patch.resolve_unidentified(
            {"stream_id": 7, "name": "x"}, bid, bname)
        self.assertIsNone(mid)
        self.assertIn("ambiguous", why)

    def test_no_match_at_all_is_expected_churn(self):
        bid, bname = self._lookups()
        mid, _, why = patch.resolve_unidentified(
            {"stream_id": 7, "name": "x"}, bid, bname)
        self.assertIsNone(mid)
        self.assertIn("dead", why)

    def test_resolved_tmdb_id_may_corroborate(self):
        bid, bname = self._lookups(by_name={"x": [(42, "555")]})
        mid, how, _ = patch.resolve_unidentified(
            {"stream_id": 7, "name": "x", "resolved_tmdb_id": 555}, bid, bname)
        self.assertEqual((mid, how), (42, "name"))

    def test_resolved_tmdb_id_disagreeing_blocks_the_match(self):
        bid, bname = self._lookups(by_name={"x": [(42, "555")]})
        mid, _, why = patch.resolve_unidentified(
            {"stream_id": 7, "name": "x", "resolved_tmdb_id": 999}, bid, bname)
        self.assertIsNone(mid)
        self.assertIn("disagree", why)

    def test_resolved_tmdb_id_never_resolves_on_its_own(self):
        # No stream_id row and no name: a resolved id alone must not be enough.
        bid, bname = self._lookups()
        mid, _, _ = patch.resolve_unidentified(
            {"stream_id": 7, "resolved_tmdb_id": 555}, bid, bname)
        self.assertIsNone(mid)


class EssentialsAndEnrichDecisionTests(unittest.TestCase):

    def _props(self, video=None, audio=None, mark=None):
        d = {}
        if video is not None:
            d["video"] = video
        if audio is not None:
            d["audio"] = audio
        p = {"detailed_info": d}
        if mark is not None:
            p[patch.OWN_PROPS_KEY] = {"enrich": mark}
        return p

    def test_essentials_need_both_dims_and_audio(self):
        self.assertTrue(patch.relation_has_essentials(
            self._props(video={"width": 1920}, audio={"codec": "ac3"})))
        self.assertFalse(patch.relation_has_essentials(
            self._props(video={"width": 1920})))
        self.assertFalse(patch.relation_has_essentials(
            self._props(audio={"codec": "ac3"})))

    def test_an_empty_video_block_is_absent_not_data(self):
        # clean_custom_properties drops None/''/[] but NOT {}, so an empty block
        # reaches storage looking like data.
        self.assertFalse(patch.relation_has_essentials(
            self._props(video={}, audio={"codec": "ac3"})))

    def test_junk_detailed_info_does_not_raise(self):
        self.assertFalse(patch.relation_has_essentials({"detailed_info": "nope"}))
        self.assertFalse(patch.relation_has_essentials({}))
        self.assertFalse(patch.relation_has_essentials(None))

    def test_already_complete_is_not_refetched(self):
        should, _ = patch.decide_enrich(
            self._props(video={"width": 1920}, audio={"c": 1}), NOW.isoformat())
        self.assertFalse(should)

    def test_never_attempted_is_fetched(self):
        should, why = patch.decide_enrich(self._props(), NOW.isoformat())
        self.assertTrue(should)
        self.assertIn("never", why)

    def test_a_provider_that_had_nothing_is_not_asked_again(self):
        # THE point of stamping an attempt rather than a success: otherwise the
        # relations whose providers never answer are re-asked every single run,
        # and those are exactly the ones a later probe pass pays most for.
        # OLD timestamp on purpose -- with a recent one this passes even if the
        # "not an error" branch is deleted, because the retry window declines it
        # anyway, and the test would be asserting nothing.
        old = (NOW - timedelta(days=90)).isoformat()
        for got in ("none", "partial", "full"):
            should, _ = patch.decide_enrich(
                self._props(mark={"at": old, "got": got}), NOW.isoformat())
            self.assertFalse(should, got)

    def test_an_error_retries_but_only_after_the_window(self):
        recent = (NOW - timedelta(days=1)).isoformat()
        should, _ = patch.decide_enrich(
            self._props(mark={"at": recent, "got": "error"}), NOW.isoformat())
        self.assertFalse(should)

        old = (NOW - timedelta(days=30)).isoformat()
        should, why = patch.decide_enrich(
            self._props(mark={"at": old, "got": "error"}), NOW.isoformat())
        self.assertTrue(should)
        self.assertIn("retry", why)

    def test_an_unreadable_mark_retries_rather_than_stalls(self):
        should, _ = patch.decide_enrich(
            self._props(mark={"got": "error"}), NOW.isoformat())
        self.assertTrue(should)
        should, _ = patch.decide_enrich(
            self._props(mark="not a dict"), NOW.isoformat())
        self.assertTrue(should)


class TakeWholeMoviesTests(unittest.TestCase):
    """A bounded run must never leave a movie half-measured.

    Ranking compares a movie's copies against each other, and a copy with no
    audio data scores zero -- so a half-enriched comparison set can rank an
    enriched stereo track above an unmeasured surround one. Partial coverage of
    a movie is worse than none, which makes the movie, not the relation, the
    unit a run is allowed to stop at.
    """

    def _g(self, *sizes):
        return [(i, ["r%d-%d" % (i, j) for j in range(n)])
                for i, n in enumerate(sizes)]

    def test_takes_whole_movies_only(self):
        got = patch.take_whole_movies(self._g(3, 3, 3), 7)
        self.assertEqual(len(got), 6)        # not 7 -- would have split the third

    def test_exact_fit_is_taken(self):
        self.assertEqual(len(patch.take_whole_movies(self._g(3, 3), 6)), 6)

    def test_zero_limit_means_everything(self):
        self.assertEqual(len(patch.take_whole_movies(self._g(3, 4, 5), 0)), 12)

    def test_a_movie_bigger_than_the_limit_is_still_taken(self):
        # Otherwise a title with more copies than the batch size could never be
        # enriched at all -- and those are the titles ranking matters most for.
        got = patch.take_whole_movies(self._g(9), 5)
        self.assertEqual(len(got), 9)

    def test_oversized_is_truncated_when_the_limit_is_a_safety_ceiling(self):
        # For stream measurement the limit is not a target. Taking an oversized
        # title whole meant a setting that said three did several times that --
        # over a minute of continuous connections on a one-connection provider,
        # which timed out the request that started it.
        got = patch.take_whole_movies(self._g(14), 3, allow_oversized=False)
        self.assertEqual(len(got), 3)

    def test_a_capped_run_fills_its_budget_exactly(self):
        # 2 whole + 3 of the next. Stopping at 2 to avoid splitting would waste
        # the run for nothing: exactly one title ends up partial either way.
        got = patch.take_whole_movies(self._g(2, 14), 5, allow_oversized=False)
        self.assertEqual(len(got), 5)

    def test_at_most_one_title_is_ever_partial(self):
        # Titles are filled in order, so only the one straddling the boundary
        # is incomplete -- and stable ordering means it is resumed first.
        got = patch.take_whole_movies(self._g(2, 2, 2, 9), 7, allow_oversized=False)
        self.assertEqual(len(got), 7)      # three whole titles, then 1 of the next

    def test_no_limit_still_takes_everything_when_capped(self):
        self.assertEqual(
            len(patch.take_whole_movies(self._g(9, 4), 0, allow_oversized=False)), 13)

    def test_an_oversized_movie_does_not_drag_in_the_next_one(self):
        got = patch.take_whole_movies(self._g(9, 2), 5)
        self.assertEqual(len(got), 9)

    def test_nothing_to_do(self):
        self.assertEqual(patch.take_whole_movies([], 5), [])


class EnrichmentPreservesEssentialTests(unittest.TestCase):
    """Enrichment's write is a WHOLE REPLACE of detailed_info, so it can drop a
    tmdb_id the detail sweep captured -- the merge plugin's strongest movie
    signal, and precisely the failure `preserve_detail` exists to stop. That
    guard wraps CORE's refresh and does not reach this path, so the same rule
    has to be applied here explicitly.

    Structural rather than behavioral, because the bug is an OMISSION: a later
    rewrite of the write path that simply forgot the call would pass any test
    that did not happen to construct the drop case. This fails on absence.
    """

    def _impl(self):
        import ast
        src = open(patch.__file__, encoding="utf-8").read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "enrich_movies_impl":
                return node
        self.fail("enrich_movies_impl not found")

    def _fn(self, name):
        import ast
        tree = ast.parse(open(patch.__file__, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail("%s not found" % name)

    def test_the_probe_pass_treats_its_limit_as_a_hard_ceiling(self):
        """The call site, not the function -- this is where it actually broke.

        `take_whole_movies` defaults to taking an oversized title whole, which
        is right for cheap detail lookups and wrong for measurement. With the
        default, a setting of three ran several times that many probes: over a
        minute of continuous connections on a provider that allows one, and a
        timed-out request. The policy lives in the keyword at the call site, so
        that is what has to be asserted; every test of the function itself
        passed throughout.
        """
        import ast
        fn = self._fn("probe_movies_impl")
        calls = [c for c in ast.walk(fn)
                 if isinstance(c, ast.Call)
                 and getattr(c.func, "id", None) == "take_whole_movies"]
        self.assertEqual(len(calls), 1, "expected exactly one batching call")
        kw = {k.arg: k.value for k in calls[0].keywords}
        self.assertIn("allow_oversized", kw,
                      "the probe pass must pass allow_oversized explicitly")
        self.assertIsInstance(kw["allow_oversized"], ast.Constant)
        self.assertFalse(kw["allow_oversized"].value,
                         "allow_oversized must be False for stream measurement")

    def test_the_write_path_restores_essential_keys(self):
        fn = self._impl()
        called = {c.func.id for c in ast.walk(fn)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        self.assertIn(
            "restore_essential", called,
            "enrich_movies_impl replaces detailed_info wholesale and must call "
            "restore_essential, or a provider reply lacking tmdb_id silently "
            "destroys a captured merge signal")

    def test_restore_actually_protects_the_merge_signal(self):
        # The policy itself, on the case that matters: sweep captured a tmdb,
        # the fresh payload has richer video but no id.
        old = {"tmdb_id": "555", "video": {}, "audio": None}
        new = {"video": {"width": 1920}, "audio": {"codec": "ac3"}}
        merged, restored = patch.restore_essential(old, new)
        self.assertEqual(merged.get("tmdb_id"), "555")
        self.assertEqual(merged["video"], {"width": 1920})
        self.assertIn("tmdb_id", restored)

    def test_a_value_the_provider_sent_always_wins(self):
        old = {"tmdb_id": "111"}
        new = {"tmdb_id": "222", "video": {"width": 1}, "audio": {"c": 1}}
        merged, restored = patch.restore_essential(old, new)
        self.assertEqual(merged["tmdb_id"], "222")
        self.assertNotIn("tmdb_id", restored)


class ParseFfprobeTests(unittest.TestCase):
    """ffprobe output is reshaped into the form providers already use, rather
    than having fields picked out of it -- a provider's `detailed_info.video`
    IS an ffprobe stream object, so copying it wholesale carries anything
    useful along without enumerating it here."""

    def _payload(self, *streams, **fmt):
        return {"streams": list(streams), "format": fmt}

    def test_first_video_and_audio_are_taken(self):
        out = patch.parse_ffprobe(self._payload(
            {"codec_type": "video", "width": 3840, "height": 2160},
            {"codec_type": "video", "width": 99},
            {"codec_type": "audio", "codec_name": "ac3", "channels": 6},
            {"codec_type": "audio", "codec_name": "aac"},
        ))
        self.assertEqual(out["video"]["width"], 3840)
        self.assertEqual(out["audio"]["codec_name"], "ac3")

    def test_the_dovi_record_survives(self):
        # The entire reason probing beats the API: no provider payload has ever
        # carried side_data_list, and avoid-DV cannot work without it.
        out = patch.parse_ffprobe(self._payload({
            "codec_type": "video", "width": 3840,
            "side_data_list": [{"side_data_type": "DOVI configuration record",
                                "dv_profile": 5, "dv_bl_signal_compatibility_id": 0}],
        }))
        self.assertEqual(out["video"]["side_data_list"][0]["dv_profile"], 5)

    def test_format_duration_and_bitrate_are_carried(self):
        out = patch.parse_ffprobe(self._payload(
            {"codec_type": "video", "width": 1},
            duration="5400.0", bit_rate="12000000"))
        self.assertEqual(out["duration_secs"], "5400.0")
        self.assertEqual(out["bitrate"], "12000000")

    def test_junk_and_empties_do_not_raise(self):
        self.assertEqual(patch.parse_ffprobe(None), {})
        self.assertEqual(patch.parse_ffprobe("nope"), {})
        self.assertEqual(patch.parse_ffprobe({"streams": [None, "x"]}), {})


class ClassifyProbeTests(unittest.TestCase):
    """'none' means ffprobe RAN and the stream has nothing -- permanent for
    that stream. 'error' means we never got an answer, and is the only outcome
    worth retrying. Collapsing them would either re-probe dead streams for ever
    or abandon good ones after one bad moment."""

    def test_full(self):
        self.assertEqual(patch.classify_probe(
            0, {"video": {"width": 1920}, "audio": {"codec_name": "ac3"}}), "full")

    def test_ran_but_nothing_useful(self):
        self.assertEqual(patch.classify_probe(0, {"video": {"width": 1920}}), "none")
        self.assertEqual(patch.classify_probe(0, {"audio": {"c": 1}}), "none")

    def test_a_video_stream_with_no_dimensions_is_a_truncated_read(self):
        # Every real video stream has dimensions, so their absence is not a
        # property of the media -- it is a read that was cut short. On a
        # provider capped at one connection that happens whenever a probe
        # collides with playback. Recording it as "none" would turn a transient
        # collision into a permanently unmeasurable copy.
        self.assertEqual(
            patch.classify_probe(0, {"video": {"codec_name": "hevc"}}), "error")
        self.assertEqual(
            patch.classify_probe(0, {"video": {}, "audio": {"c": 1}}), "error")

    def test_failure_is_an_error_not_an_absence(self):
        self.assertEqual(patch.classify_probe(1, None), "error")
        self.assertEqual(patch.classify_probe(0, None), "error")
        self.assertEqual(patch.classify_probe(255, {"video": {"width": 1}}), "error")


class StoreProbeTests(unittest.TestCase):
    """Two destinations on purpose: our own key is durable and nothing else
    writes it, and the mirror into detailed_info is what makes the data usable
    with no change to the plugin that consumes it."""

    def test_writes_our_key_and_mirrors_into_detail(self):
        parsed = {"video": {"width": 1920}, "audio": {"codec_name": "ac3"}}
        props = patch.store_probe({}, parsed)
        self.assertEqual(props[patch.PROBE_KEY], parsed)
        self.assertEqual(props["detailed_info"]["video"]["width"], 1920)
        self.assertEqual(props["detailed_info"]["audio"]["codec_name"], "ac3")

    def test_a_value_the_provider_sent_is_not_overwritten(self):
        props = {"detailed_info": {"video": {"width": 1280, "from": "provider"}}}
        props = patch.store_probe(props, {"video": {"width": 3840},
                                          "audio": {"codec_name": "ac3"}})
        self.assertEqual(props["detailed_info"]["video"]["from"], "provider")
        self.assertEqual(props["detailed_info"]["audio"]["codec_name"], "ac3")
        # but our measurement is still recorded in full
        self.assertEqual(props[patch.PROBE_KEY]["video"]["width"], 3840)

    def test_an_empty_block_counts_as_absent(self):
        props = patch.store_probe({"detailed_info": {"video": {}}},
                                  {"video": {"width": 3840}})
        self.assertEqual(props["detailed_info"]["video"]["width"], 3840)

    def test_junk_detail_is_replaced_not_crashed_on(self):
        props = patch.store_probe({"detailed_info": "nope"},
                                  {"video": {"width": 1}})
        self.assertEqual(props["detailed_info"]["video"]["width"], 1)


class ProbeDecisionAndBreakerTests(unittest.TestCase):

    def _props(self, mark=None, essentials=False, lookup="partial"):
        d = ({"video": {"width": 1}, "audio": {"c": 1}} if essentials else {})
        p = {"detailed_info": d}
        own = {}
        if lookup is not None:
            own["enrich"] = {"at": NOW.isoformat(), "got": lookup}
        if mark is not None:
            own["probe"] = mark
        if own:
            p[patch.OWN_PROPS_KEY] = own
        return p

    def test_a_copy_that_already_has_data_is_not_probed(self):
        self.assertFalse(patch.decide_probe(
            self._props(essentials=True), NOW.isoformat())[0])

    def test_never_probed_is_probed(self):
        self.assertTrue(patch.decide_probe(self._props(), NOW.isoformat())[0])

    def test_nothing_is_measured_before_the_provider_lookup_has_tried(self):
        # A measurement holds the connection for seconds; the lookup is a light
        # request that often fills the gap for free. Never spend the expensive
        # one first.
        should, why = patch.decide_probe(self._props(lookup=None), NOW.isoformat())
        self.assertFalse(should)
        self.assertIn("lookup", why)

    def test_a_failed_lookup_still_counts_as_tried(self):
        # Otherwise a provider whose detail endpoint always fails could never
        # be measured at all.
        self.assertTrue(patch.decide_probe(
            self._props(lookup="error"), NOW.isoformat())[0])

    def test_a_stream_with_nothing_is_not_probed_again(self):
        # The most expensive possible mistake: a connection slot per re-ask,
        # for the streams guaranteed to yield nothing.
        # The timestamp is deliberately OLD. With a recent one this assertion
        # passes even when the "not an error" branch is removed, because the
        # retry window would decline it anyway -- so it would not actually test
        # the rule it claims to.
        old = (NOW - timedelta(days=90)).isoformat()
        self.assertFalse(patch.decide_probe(
            self._props({"at": old, "got": "none"}), NOW.isoformat())[0])

    def test_errors_retry_only_after_the_window(self):
        recent = (NOW - timedelta(days=1)).isoformat()
        self.assertFalse(patch.decide_probe(
            self._props({"at": recent, "got": "error"}), NOW.isoformat())[0])
        old = (NOW - timedelta(days=30)).isoformat()
        self.assertTrue(patch.decide_probe(
            self._props({"at": old, "got": "error"}), NOW.isoformat())[0])

    def test_a_tripped_breaker_blames_the_provider_not_the_streams(self):
        # An outage must not put every queued copy into a week-long retry
        # window. That is how a bad hour becomes permanently missing data.
        deferred = {"good": ["r1", "r2"], "down": ["r3", "r4", "r5"]}
        self.assertEqual(patch.errors_to_stamp(deferred, ["down"]), ["r1", "r2"])

    def test_with_no_breaker_every_failure_is_the_streams(self):
        deferred = {"a": ["r1"], "b": ["r2"]}
        self.assertEqual(patch.errors_to_stamp(deferred, []), ["r1", "r2"])

    def test_nothing_deferred(self):
        self.assertEqual(patch.errors_to_stamp({}, ["x"]), [])


class _FakeRedis:
    """Just enough of redis-py for the run lock: SET NX EX, GET, EXPIRE, DELETE."""

    def __init__(self, fail=False):
        self.store = {}
        self.ttl = {}
        self.fail = fail

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    def set(self, key, value, nx=False, ex=None):
        self._check()
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttl[key] = ex
        return True

    def get(self, key):
        self._check()
        return self.store.get(key)

    def expire(self, key, ttl):
        self._check()
        self.ttl[key] = ttl
        return key in self.store

    def delete(self, key):
        self._check()
        self.store.pop(key, None)

    def exists(self, key):
        self._check()
        return int(key in self.store)


class ProbeRunLockTests(unittest.TestCase):
    """One measuring run at a time. On a provider that allows a single
    connection, two runs probing at once collide with each other exactly as a
    probe collides with playback."""

    def test_a_second_run_does_not_start_while_one_holds_the_lock(self):
        r = _FakeRedis()
        calls = []

        def inner(_hb):
            calls.append("inner")
            return "inner"

        def outer(_hb):
            calls.append("outer")
            # A second run arriving mid-run must do nothing at all.
            self.assertEqual(patch.run_locked(r, "b", 60, inner), (False, None))
            return "outer"

        self.assertEqual(patch.run_locked(r, "a", 60, outer), (True, "outer"))
        self.assertEqual(calls, ["outer"])

    def test_the_lock_is_released_however_the_run_ends(self):
        r = _FakeRedis()

        def boom(_hb):
            raise RuntimeError("probe blew up")

        with self.assertRaises(RuntimeError):
            patch.run_locked(r, "a", 60, boom)
        self.assertNotIn(patch.RUN_LOCK_KEY, r.store)
        # ...so the next run is not locked out.
        self.assertEqual(patch.run_locked(r, "b", 60, lambda hb: 1), (True, 1))

    def test_release_never_deletes_another_runs_lock(self):
        r = _FakeRedis()
        r.set(patch.RUN_LOCK_KEY, "other")
        patch.release_run_lock(r, "mine")
        self.assertEqual(r.store[patch.RUN_LOCK_KEY], "other")

    def test_heartbeat_stops_the_run_when_another_has_taken_over(self):
        r = _FakeRedis()
        r.set(patch.RUN_LOCK_KEY, "other")
        self.assertFalse(patch.refresh_run_lock(r, "mine", 60))

    def test_heartbeat_extends_our_own_lock(self):
        r = _FakeRedis()
        patch.acquire_run_lock(r, "mine", 10)
        self.assertTrue(patch.refresh_run_lock(r, "mine", 99))
        self.assertEqual(r.ttl[patch.RUN_LOCK_KEY], 99)

    def test_heartbeat_retakes_a_lapsed_lock_nobody_else_took(self):
        r = _FakeRedis()
        self.assertTrue(patch.refresh_run_lock(r, "mine", 60))
        self.assertEqual(r.store[patch.RUN_LOCK_KEY], "mine")

    def test_heartbeat_reads_bytes_replies(self):
        r = _FakeRedis()
        r.set(patch.RUN_LOCK_KEY, b"mine")
        self.assertTrue(patch.refresh_run_lock(r, "mine", 60))

    def test_heartbeat_fails_closed_when_redis_errors(self):
        # Unable to confirm the lock means unable to rule out a second run.
        self.assertFalse(patch.refresh_run_lock(_FakeRedis(fail=True), "x", 60))

    def test_ttl_outlasts_the_longest_gap_between_heartbeats(self):
        for delay_ms in (0, 2000, 60000):
            gap = patch.PROBE_TIMEOUT_S + delay_ms / 1000.0
            self.assertGreater(patch.run_lock_ttl(delay_ms), gap)

    def test_ttl_stays_short_so_a_killed_worker_frees_it_soon(self):
        self.assertLessEqual(patch.run_lock_ttl(patch.DEFAULT_PROBE_DELAY_MS), 300)

    def test_ttl_tolerates_junk_delay(self):
        self.assertGreater(patch.run_lock_ttl(None), patch.PROBE_TIMEOUT_S)
        self.assertGreater(patch.run_lock_ttl(-5), patch.PROBE_TIMEOUT_S)


class ProbeEnqueueTests(unittest.TestCase):
    """The button queues work; it must never do it. A probe can hold a provider
    connection for 30 seconds, and the inline version timed out a request."""

    class _Task:
        def __init__(self):
            self.calls = []

        def apply_async(self, **kw):
            self.calls.append(kw)

    def setUp(self):
        self._saved = (patch.run_enrichment, patch._redis_client)
        self.task = self._Task()
        self.redis = _FakeRedis()
        patch.run_enrichment = self.task
        patch._redis_client = lambda: self.redis

    def tearDown(self):
        patch.run_enrichment, patch._redis_client = self._saved

    def _cfg(self, on=True, queue="dvr"):
        return {"enrich_movies": on, "schedule_queue": queue}

    def test_routes_to_the_configured_queue(self):
        started, _ = patch.enqueue_enrichment(self._cfg(queue="dvr"))
        self.assertTrue(started)
        self.assertEqual(self.task.calls, [{"queue": "dvr"}])

    def test_off_means_nothing_is_queued(self):
        started, why = patch.enqueue_enrichment(self._cfg(on=False))
        self.assertFalse(started)
        self.assertIn("off", why)
        self.assertEqual(self.task.calls, [])

    def test_a_run_in_progress_is_reported_not_queued_behind(self):
        self.redis.set(patch.RUN_LOCK_KEY, "someone")
        started, why = patch.enqueue_enrichment(self._cfg())
        self.assertFalse(started)
        self.assertIn("in progress", why)
        self.assertEqual(self.task.calls, [])

    def test_an_unreadable_lock_still_queues(self):
        # The pre-check is only for a nicer message; the task takes the lock
        # itself and fails closed, so overlap is still impossible.
        self.redis.fail = True
        started, _ = patch.enqueue_enrichment(self._cfg())
        self.assertTrue(started)


class ProbeCallSiteTests(unittest.TestCase):
    """Policy chosen by an argument at a call site is invisible to tests of the
    function. Two bugs this project shipped lived exactly there, so each such
    choice here is asserted structurally."""

    def _tree(self, module):
        return ast.parse(open(module.__file__, encoding="utf-8").read())

    def _fn(self, name):
        for node in ast.walk(self._tree(patch)):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail("%s not found" % name)

    @staticmethod
    def _called_names(node):
        out = set()
        for c in ast.walk(node):
            if isinstance(c, ast.Call):
                f = c.func
                out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None))
        return out

    def _action_branch(self, action):
        import plugin as plugin_mod
        for node in ast.walk(self._tree(plugin_mod)):
            if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                    and any(isinstance(c, ast.Constant) and c.value == action
                            for c in node.test.comparators)):
                return node
        return None

    def test_the_button_never_works_inline(self):
        branch = self._action_branch("enrich_now")
        self.assertIsNotNone(branch, "enrich_now branch not found")
        called = self._called_names(ast.Module(body=branch.body, type_ignores=[]))
        for inline in ("enrich_movies_impl", "probe_movies_impl", "enrich_run_impl"):
            self.assertNotIn(inline, called,
                             "the button must enqueue, not work inside the request")
        self.assertIn("enqueue_enrichment", called)

    def test_there_is_no_separate_measuring_button(self):
        # Measuring is a step of 'Enrich now'. A button of its own would let it
        # run without the lookup first -- the expensive step before the free one.
        import plugin as plugin_mod
        ids = [a["id"] for a in plugin_mod.Plugin.actions]
        self.assertNotIn("probe_now", ids)
        self.assertIsNone(self._action_branch("probe_now"))

    def test_the_task_runs_lookup_before_measuring(self):
        calls = [c for c in ast.walk(self._fn("_run_enrichment"))
                 if isinstance(c, ast.Call)
                 and getattr(c.func, "id", None) == "enrich_run_impl"]
        self.assertEqual(len(calls), 1)
        args = [getattr(a, "id", None) for a in calls[0].args]
        self.assertEqual(args[1:3], ["enrich_movies_impl", "probe_movies_impl"],
                         "lookup must be passed as the first step, measuring second")

    def test_the_enqueue_names_its_queue(self):
        # Without an explicit queue the task goes to the default prefork worker,
        # which never imports plugins and silently drops it.
        calls = [c for c in ast.walk(self._fn("enqueue_enrichment"))
                 if isinstance(c, ast.Call)
                 and getattr(c.func, "attr", None) == "apply_async"]
        self.assertEqual(len(calls), 1)
        self.assertIn("queue", {k.arg for k in calls[0].keywords})

    def test_the_task_hands_the_run_a_heartbeat(self):
        calls = [c for c in ast.walk(self._fn("_run_enrichment"))
                 if isinstance(c, ast.Call)
                 and getattr(c.func, "id", None) == "enrich_run_impl"]
        self.assertEqual(len(calls), 1)
        self.assertIn("heartbeat", {k.arg for k in calls[0].keywords},
                      "without a heartbeat the lock lapses mid-run and a second "
                      "run can start alongside this one")

    def test_the_probe_loop_checks_the_heartbeat(self):
        loops = [n for n in ast.walk(self._fn("probe_movies_impl"))
                 if isinstance(n, ast.For) and getattr(n.iter, "id", None) == "rels"]
        self.assertEqual(len(loops), 1)
        self.assertIn("heartbeat", self._called_names(loops[0]),
                      "the heartbeat must be checked before each probe")

    def test_the_lookup_loop_checks_the_heartbeat(self):
        loops = [n for n in ast.walk(self._fn("enrich_movies_impl"))
                 if isinstance(n, ast.For) and getattr(n.iter, "id", None) == "rels"]
        self.assertEqual(len(loops), 1)
        self.assertIn("heartbeat", self._called_names(loops[0]),
                      "the heartbeat must be checked before each lookup")


class EnrichRunOrderTests(unittest.TestCase):
    """One run, two steps: the free lookup always first, the connection-holding
    measurement only for what it could not fill."""

    def _run(self, probe_enabled=True, lookup_result=None):
        calls = []

        def lookup(heartbeat=None, skip_accounts=None):
            calls.append(("lookup", heartbeat))
            return lookup_result if lookup_result is not None else {"fetched": 5}

        def measure(heartbeat=None, skip_accounts=None):
            calls.append(("measure", heartbeat))
            return {"probed": 2}

        hb = object()
        out = patch.enrich_run_impl(probe_enabled, lookup, measure, heartbeat=hb)
        return out, calls, hb

    def test_lookup_then_measure_both_with_the_heartbeat(self):
        out, calls, hb = self._run()
        self.assertEqual(calls, [("lookup", hb), ("measure", hb)])
        self.assertEqual(out, {"lookup": {"fetched": 5}, "measure": {"probed": 2}})

    def test_the_nights_skip_list_reaches_the_measuring_step(self):
        got = []

        def measure(heartbeat=None, skip_accounts=None):
            got.append(skip_accounts)
            return {}

        patch.enrich_run_impl(True, lambda heartbeat=None, skip_accounts=None: {}, measure,
                              skip_accounts={7})
        self.assertEqual(got, [{7}])

    def test_measuring_off_means_lookup_only(self):
        out, calls, _ = self._run(probe_enabled=False)
        self.assertEqual([c[0] for c in calls], ["lookup"])
        self.assertNotIn("measure", out)

    def test_an_untrusted_wanted_set_stops_both_steps(self):
        # Fail closed on scope: if the lookup refused the wanted set, measuring
        # must not go on to use it.
        _, calls, _ = self._run(lookup_result={"aborted": "wanted set stale"})
        self.assertEqual([c[0] for c in calls], ["lookup"])

    def test_losing_the_lock_during_lookup_stops_measuring(self):
        _, calls, _ = self._run(lookup_result={"stopped": "lost the run lock"})
        self.assertEqual([c[0] for c in calls], ["lookup"])


class LastProbeReportTests(unittest.TestCase):
    """The button no longer waits, so this line is the only way to tell a run
    that is still going from one that ended. It has to say which, outright."""

    def setUp(self):
        import plugin as plugin_mod
        self.fmt = plugin_mod._format_last_run
        self.short = plugin_mod._short_time

    def test_no_run_yet(self):
        self.assertIn("none yet", self.fmt(None))

    def test_a_running_run_says_so(self):
        out = self.fmt({"state": "running",
                        "started_at": "2030-01-02T03:00:00.1+00:00"})
        self.assertIn("IN PROGRESS", out)
        self.assertNotIn("finished", out)

    def _finished(self, result):
        return self.fmt({"state": "finished",
                         "finished_at": "2030-01-02T03:04:05.123456+00:00",
                         "result": result})

    def test_a_finished_run_reports_both_steps(self):
        out = self._finished({
            "lookup": {"fetched": 25, "got_essentials": 4, "errors": 1},
            "measure": {"probed": 3, "gained": 3, "errors": 2}})
        self.assertIn("last run 01-02 03:04 UTC", out)
        self.assertIn("+4 lookup", out)
        self.assertIn("+3 measured", out)
        self.assertIn("3 errors", out)          # summed across both steps
        self.assertEqual(len(out.splitlines()), 1)

    def test_measuring_off_is_said(self):
        out = self._finished({"lookup": {"fetched": 25, "got_essentials": 4}})
        self.assertIn("measuring off", out)

    def test_a_refused_wanted_set_is_said(self):
        out = self._finished({"lookup": {"aborted": "wanted set stale (old)"}})
        self.assertIn("nothing done -- wanted set stale", out)

    def test_a_tripped_breaker_and_a_lost_lock_are_said(self):
        out = self._finished({
            "lookup": {"fetched": 1},
            "measure": {"probed": 1, "broken": ["ProviderX"],
                        "stopped": "lost the run lock"}})
        self.assertIn("ProviderX", out)
        self.assertIn("lost the run lock", out)

    def test_a_failed_run_shows_why(self):
        out = self.fmt({"state": "failed", "finished_at": "x",
                        "error": "Redis is unavailable"})
        self.assertIn("failed", out)
        self.assertIn("Redis is unavailable", out)

    def test_short_time(self):
        self.assertEqual(self.short("2030-01-02T03:04:05.12+00:00"),
                         "2030-01-02 03:04 UTC")
        self.assertEqual(self.short("2030-01-02T03:04:05-07:00"),
                         "2030-01-02 03:04")
        self.assertEqual(self.short(None), "None")


class EnrichStatusMessageTests(unittest.TestCase):
    """The action popup does not scroll. The first version of this message ran
    past it, so its size is a requirement, not a style choice."""

    def _st(self, accounts=4, on=True):
        return {
            "enrich_movies": on, "generated_at": "2030-01-01T00:00:00Z",
            "movies": 10, "tmdb_resolved": 6, "tmdb_total": 6,
            "unid_resolved": 4, "unid_total": 4, "relations": 100,
            "have_essentials": 20, "need_fetch": 50, "runs_at_current_limit": 2,
            "need_measure": 30, "measure_runs_at_current_limit": 10,
            "last_enrich_run": {
                "state": "finished",
                "finished_at": "2030-01-02T03:04:05+00:00",
                "result": {"lookup": {"fetched": 25, "got_essentials": 4,
                                      "errors": 0},
                           "measure": {"probed": 3, "gained": 3, "errors": 1}}},
            "per_account": {"Provider%d" % i: {"relations": 30, "need": 20,
                                               "measure": 25}
                            for i in range(accounts)},
        }

    def _fmt(self, st):
        import plugin as plugin_mod
        return plugin_mod._format_enrich_status(st)

    def test_fits_in_four_lines_however_many_providers(self):
        for n in (1, 4, 12):
            self.assertLessEqual(len(self._fmt(self._st(n)).splitlines()), 4, n)

    def test_carries_the_numbers_that_matter(self):
        out = self._fmt(self._st())
        for needle in ("measure 30", "lookup 50", "done 20",
                       "last run 01-02 03:04 UTC", "10 movies, 100 copies",
                       "Provider0 20/25"):
            self.assertIn(needle, out)

    def test_a_full_scale_status_fits_the_popup(self):
        # The popup shows a fixed number of characters and cuts the rest from
        # BOTH ends; a full wanted set overflowed it twice. Budget a realistic
        # worst case: four providers, five-digit counts, a nightly run.
        import plugin as plugin_mod
        st = dict(self._st(4), movies=12345, relations=99999, have_essentials=99999,
                  need_fetch=99999, need_measure=99999, need_dv_check=999, nights=99)
        st["per_account"] = {"Provider%d" % i: {"need": 99999, "measure": 99999,
                                                 "not_describing": i % 2 == 0}
                             for i in range(4)}
        st["last_enrich_run"] = {
            "state": "finished", "nightly": True,
            "finished_at": "2030-01-02T03:04:05+00:00",
            "result": {"batches": 999, "ended": "time limit reached",
                       "lookup": {"got_essentials": 9999, "errors": 99},
                       "measure": {"gained": 9999, "dv_found": 999,
                                   "dv_no_fallback": 99, "errors": 99}}}
        out = plugin_mod._format_enrich_status(st)
        self.assertLessEqual(len(out), 400, out)

    def test_unresolved_is_mentioned_only_when_there_is_some(self):
        import plugin as plugin_mod
        self.assertNotIn("unresolved", plugin_mod._format_enrich_status(self._st()))
        self.assertIn("2 unresolved", plugin_mod._format_enrich_status(
            dict(self._st(), unid_resolved=2)))

    def test_off_is_said_first(self):
        self.assertTrue(self._fmt(self._st(on=False)).startswith("ENRICHMENT IS OFF"))
        self.assertNotIn("OFF", self._fmt(self._st()))


class ProbeStatusIsDurableTests(unittest.TestCase):
    """Uploading a zip replaces the plugin folder, so anything stored beside the
    plugin is erased by every upgrade. The run record lost its first version
    exactly that way."""

    def test_the_run_record_is_not_a_file_beside_the_plugin(self):
        tree = ast.parse(open(patch.__file__, encoding="utf-8").read())
        for name in ("write_run_status", "read_run_status"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            called = {getattr(c.func, "id", None) for c in ast.walk(fn)
                      if isinstance(c, ast.Call)}
            self.assertFalse(called & {"open", "_write_json_file", "_plugin_file"},
                             "%s must not store the record in the plugin folder"
                             % name)
            names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            self.assertIn("RUN_STATUS_KEY", names)


class MeasureBacklogTests(unittest.TestCase):

    def test_status_counts_the_backlog_with_the_runs_own_decision(self):
        # A status that estimated the backlog its own way would drift from what
        # a run actually does, and nothing would notice.
        tree = ast.parse(open(patch.__file__, encoding="utf-8").read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "enrich_status")
        called = {getattr(c.func, "id", None) for c in ast.walk(fn)
                  if isinstance(c, ast.Call)}
        self.assertIn("decide_probe", called)


class _Profile:
    def __init__(self, pid, default=False, free=True):
        self.id, self.is_default, self.free = pid, default, free


class BusyProviderTests(unittest.TestCase):
    """Never probe a provider that is busy with real playback. On a
    one-connection account a probe during playback is a certainty of
    collision, and the stream that gets dropped may be the viewer's."""

    def _pick(self, *profiles):
        got = patch.pick_probe_profile(profiles, lambda p: p.free)
        return got.id if got else None

    def test_an_idle_account_is_probed_through_its_default_profile(self):
        self.assertEqual(self._pick(_Profile(2), _Profile(1, default=True)), 1)

    def test_a_busy_default_falls_back_to_another_free_profile(self):
        # Same order the VOD proxy uses to admit a viewer.
        self.assertEqual(
            self._pick(_Profile(1, default=True, free=False), _Profile(2)), 2)

    def test_every_profile_busy_means_no_probe(self):
        self.assertIsNone(self._pick(_Profile(1, default=True, free=False),
                                     _Profile(2, free=False)))

    def test_no_profiles_means_no_probe(self):
        self.assertIsNone(self._pick())

    def test_unable_to_tell_counts_as_busy(self):
        # Offline there is no Dispatcharr to ask, which is exactly the "could
        # not check" case: it must answer busy, never free.
        self.assertIsNone(patch._probe_profile(object(), lambda p: True))

    def _probe_loop(self):
        tree = ast.parse(open(patch.__file__, encoding="utf-8").read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "probe_movies_impl")
        return fn, next(n for n in ast.walk(fn)
                        if isinstance(n, ast.For) and getattr(n.iter, "id", None) == "rels")

    def test_busy_is_checked_before_every_probe(self):
        # Per probe, not per run: playback can start at any moment.
        _fn, loop = self._probe_loop()
        calls = {}
        for c in ast.walk(loop):
            if isinstance(c, ast.Call):
                name = getattr(c.func, "id", None) or getattr(c.func, "attr", None)
                calls.setdefault(name, c.lineno)
        self.assertIn("_probe_profile", calls)
        self.assertLess(calls["_probe_profile"], calls["run_ffprobe"])

    def test_a_busy_account_is_actually_skipped(self):
        # Calling the check is not enough; its answer has to stop the probe.
        _fn, loop = self._probe_loop()
        branches = [n for n in ast.walk(loop) if isinstance(n, ast.If)
                    and isinstance(n.test, ast.Compare)
                    and getattr(n.test.left, "id", None) == "profile"
                    and isinstance(n.test.ops[0], ast.Is)]
        self.assertEqual(len(branches), 1, "expected one `if profile is None`")
        body = branches[0].body
        self.assertTrue(any(isinstance(s, (ast.Continue, ast.Break)) for s in body),
                        "a busy account must not fall through to the probe")
        self.assertIn("busy", ast.dump(ast.Module(body=body, type_ignores=[])))

    def test_an_unloadable_capacity_check_measures_nothing(self):
        # A future Dispatcharr that moves the check must stop probing loudly,
        # not probe blind.
        fn, _loop = self._probe_loop()
        tries = [t for t in ast.walk(fn) if isinstance(t, ast.Try)
                 and any(isinstance(c, ast.Call)
                         and getattr(c.func, "id", None) == "_capacity_checker"
                         for c in ast.walk(ast.Module(body=t.body, type_ignores=[])))]
        self.assertEqual(len(tries), 1)
        handler = ast.dump(ast.Module(body=tries[0].handlers[0].body, type_ignores=[]))
        self.assertIn("aborted", handler)
        self.assertTrue(any(isinstance(s, ast.Return) for s in tries[0].handlers[0].body))

    def test_the_probe_uses_the_profile_that_was_checked(self):
        # Otherwise the slot checked and the connection opened can belong to
        # different logins.
        _fn, loop = self._probe_loop()
        urls = [c for c in ast.walk(loop) if isinstance(c, ast.Call)
                and getattr(c.func, "attr", None) == "get_stream_url"]
        self.assertEqual(len(urls), 1)
        self.assertEqual([getattr(a, "id", None) for a in urls[0].args], ["profile"])

    def test_no_slot_is_ever_reserved(self):
        # Core has no cleanup for a leaked counter; a reserved slot left behind
        # by a killed worker would block all playback on a one-connection
        # account until Redis restarts.
        src = open(patch.__file__, encoding="utf-8").read()
        for forbidden in ("reserve_profile_slot", "release_profile_slot",
                          "profile_connections:", ".incr("):
            self.assertNotIn(forbidden, src)

    class _Rel:
        def __init__(self, rid, account_id):
            self.id, self.m3u_account_id = rid, account_id
            self.m3u_account = account_id

    def test_busy_accounts_are_found_and_each_is_asked_once(self):
        R = self._Rel
        groups = [(1, [R(1, "busy"), R(2, "free")]),
                  (2, [R(3, "busy"), R(4, "free")])]
        asked = []

        def profile_for(account):
            asked.append(account)
            return None if account == "busy" else object()

        self.assertEqual(patch.busy_account_ids(groups, profile_for), {"busy"})
        self.assertEqual(sorted(asked), ["busy", "free"])

    def test_the_batch_filter_is_fed_the_busy_check(self):
        # The filter is only as good as its input: an empty or inverted set at
        # the call site would leave every test of the filter itself green.
        fn, _loop = self._probe_loop()
        assigns = {}
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        assigns.setdefault(t.id, getattr(n.value.func, "id", None))
        call = next(c for c in ast.walk(fn) if isinstance(c, ast.Call)
                    and getattr(c.func, "id", None) == "without_accounts")
        arg = call.args[1]
        self.assertIsInstance(arg, ast.Name)
        self.assertEqual(assigns.get(arg.id), "busy_account_ids")
        check = next(c for c in ast.walk(fn) if isinstance(c, ast.Call)
                     and getattr(c.func, "id", None) == "busy_account_ids")
        self.assertIn("_probe_profile", ast.dump(check))

    def test_a_busy_account_does_not_use_up_the_batch(self):
        # Seen live: the next title's waiting copies were all on the busy
        # provider, so a batch chosen first and checked later measured nothing
        # while another provider had copies waiting.
        R = self._Rel
        groups = [(1, [R(1, "busy"), R(2, "busy"), R(3, "busy")]),
                  (2, [R(4, "free"), R(5, "busy")]),
                  (3, [R(6, "free"), R(7, "free")])]
        kept = patch.without_accounts(groups, {"busy"})
        self.assertEqual([m for m, _ in kept], [2, 3])
        chosen = patch.take_whole_movies(kept, 3, allow_oversized=False)
        self.assertEqual([r.id for r in chosen], [4, 6, 7])

    def test_no_busy_accounts_changes_nothing(self):
        groups = [(1, [self._Rel(1, "a")])]
        self.assertEqual(patch.without_accounts(groups, set()), groups)

    def test_busy_accounts_are_removed_before_the_batch_is_chosen(self):
        fn, _loop = self._probe_loop()
        lines = {}
        for c in ast.walk(fn):
            if isinstance(c, ast.Call):
                name = getattr(c.func, "id", None)
                if name in ("without_accounts", "take_whole_movies"):
                    lines.setdefault(name, c.lineno)
        self.assertIn("without_accounts", lines)
        self.assertLess(lines["without_accounts"], lines["take_whole_movies"])

    def test_status_names_the_busy_account(self):
        import plugin as plugin_mod
        out = plugin_mod._format_last_run({
            "state": "finished", "finished_at": "2030-01-02T03:04:05+00:00",
            "result": {"lookup": {"fetched": 1},
                       "measure": {"probed": 0, "busy": ["ProviderX"]}}})
        self.assertIn("ProviderX", out)
        self.assertIn("busy", out)


class RunGuardTests(unittest.TestCase):
    """Called before every lookup and probe. It must stop a nightly run at its
    time limit and say so, distinctly from losing the run lock."""

    def test_carries_on_with_time_and_the_lock(self):
        g = patch.RunGuard(lambda: True, deadline=100, clock=lambda: 50)
        self.assertTrue(g())
        self.assertIsNone(g.reason)

    def test_stops_at_the_time_limit_without_touching_the_lock(self):
        refreshed = []
        g = patch.RunGuard(lambda: refreshed.append(1) or True,
                           deadline=100, clock=lambda: 100)
        self.assertFalse(g())
        self.assertEqual(g.reason, "time limit reached")
        self.assertEqual(refreshed, [])

    def test_a_lost_lock_is_reported_as_such(self):
        g = patch.RunGuard(lambda: False, deadline=100, clock=lambda: 1)
        self.assertFalse(g())
        self.assertEqual(g.reason, "lost the run lock")

    def test_a_manual_run_has_no_time_limit(self):
        g = patch.RunGuard(lambda: True, clock=lambda: 10 ** 12)
        self.assertTrue(g.time_left())
        self.assertTrue(g())

    def test_the_stop_reason_reaches_the_record(self):
        g = patch.RunGuard(lambda: True, deadline=0, clock=lambda: 1)
        g()
        self.assertEqual(patch._stop_reason(g), "time limit reached")
        self.assertEqual(patch._stop_reason(lambda: False), "lost the run lock")


class NightlyBatchTests(unittest.TestCase):
    """A nightly run repeats batches. Every way it can end has to end it, and a
    provider that stopped answering must not be asked again all night."""

    def _batches(self, results, time_ok=None):
        calls = []
        it = iter(results)

        def batch(skip):
            calls.append(set(skip))
            return next(it)

        clock = iter(time_ok) if time_ok is not None else None
        time_left = (lambda: next(clock)) if clock else (lambda: True)
        return patch.run_batches(batch, time_left), calls

    def _res(self, fetched=0, probed=0, **meas):
        out = {"lookup": {"fetched": fetched, "got_essentials": 0}}
        if probed is not None:
            out["measure"] = dict({"probed": probed, "gained": probed}, **meas)
        return out

    def test_repeats_until_a_batch_finds_nothing_to_do(self):
        tot, calls = self._batches([self._res(25, 3), self._res(10, 3),
                                    self._res(0, 0)])
        self.assertEqual(len(calls), 3)
        self.assertEqual(tot["batches"], 3)
        self.assertEqual(tot["lookup"]["fetched"], 35)
        self.assertEqual(tot["measure"]["probed"], 6)
        self.assertEqual(tot["ended"], "nothing left to try")

    def test_no_batch_starts_after_the_time_limit(self):
        tot, calls = self._batches([self._res(25, 3)] * 5,
                                   time_ok=[True, True, False])
        self.assertEqual(len(calls), 2)
        self.assertEqual(tot["ended"], "time limit reached")

    def test_a_batch_stopped_mid_way_ends_the_night_with_its_reason(self):
        tot, calls = self._batches([self._res(5, None) | {"lookup": {
            "fetched": 5, "stopped": "time limit reached"}}, self._res(1, 1)])
        self.assertEqual(len(calls), 1)
        self.assertEqual(tot["ended"], "time limit reached")

    def test_a_refused_wanted_set_ends_the_night(self):
        tot, calls = self._batches([{"lookup": {"aborted": "wanted set stale"}},
                                    self._res(1, 1)])
        self.assertEqual(len(calls), 1)
        self.assertIn("wanted set stale", tot["ended"])

    def test_a_provider_that_stopped_answering_is_skipped_for_the_night(self):
        # Its failures are deliberately not stamped, so nothing else would
        # stop the next batch picking exactly the same copies again.
        tot, calls = self._batches([
            self._res(5, 1, broken=["ProviderX"], broken_ids=[7], errors=3),
            self._res(5, 1), self._res(0, 0)])
        self.assertEqual(calls[0], set())
        self.assertEqual(calls[1], {7})
        self.assertEqual(calls[2], {7})
        self.assertEqual(tot["measure"]["broken"], ["ProviderX"])

    def test_ending_blocked_by_busy_providers_is_said(self):
        tot, _ = self._batches([self._res(0, 0, busy=["ProviderY"])])
        self.assertIn("busy", tot["ended"])
        self.assertEqual(tot["measure"]["busy"], ["ProviderY"])

    def test_a_provider_busy_earlier_does_not_mislabel_a_finished_backlog(self):
        tot, _ = self._batches([self._res(5, 0, busy=["ProviderY"]),
                                self._res(0, 0)])
        self.assertEqual(tot["ended"], "nothing left to try")

    def test_unstamped_copies_do_not_count_as_progress(self):
        # A copy with no stream URL is never stamped, so it is picked again
        # every batch; counting it as progress would loop all night.
        tot, calls = self._batches([self._res(0, 0, no_url=3)] * 3)
        self.assertEqual(len(calls), 1)

    def test_a_safety_cap_bounds_the_batches(self):
        n = []

        def batch(skip):
            n.append(1)
            return self._res(1, 0)

        tot = patch.run_batches(batch, lambda: True, max_batches=4)
        self.assertEqual(len(n), 4)
        self.assertIn("safety", tot["ended"])

    def test_measuring_off_leaves_no_measure_totals(self):
        tot, _ = self._batches([self._res(3, None), self._res(0, None)])
        self.assertNotIn("measure", tot)


class NightlyScheduleTests(unittest.TestCase):

    def _cfg(self, **kw):
        cfg = {"enrich_nightly": True, "probe_movies": True, "enrich_minutes": 60,
               "enrich_delay_ms": 0, "probe_delay_ms": 0}
        cfg.update(kw)
        return cfg

    def _n(self, per_account, **kw):
        return patch.estimate_nights(per_account, self._cfg(**kw))

    def test_lookups_add_up(self):
        # 3600 lookups at 1s, all of them filling their copy: one 60-minute night.
        full = {"answered": 100, "full": 100}
        self.assertEqual(self._n({"A": dict(full, need=3600)}), 1)
        self.assertEqual(self._n({"A": dict(full, need=3601)}), 2)
        self.assertEqual(self._n({"A": dict(full, need=0)}), 0)

    def test_measuring_runs_side_by_side(self):
        # Two providers with an hour of measuring each take ONE hour, not two:
        # the busiest lane sets the pace.
        one_hour = 3600 // int(patch.PROBE_EST_S)
        self.assertEqual(self._n({"A": {"measure": one_hour},
                                  "B": {"measure": one_hour}}), 1)
        self.assertEqual(self._n({"A": {"measure": one_hour + 1},
                                  "B": {"measure": 1}}), 2)

    def test_future_measuring_is_projected_from_each_accounts_yield(self):
        # 100 lookups that will fill nothing also mean 100 measurements later.
        never = {"need": 100, "answered": 100, "full": 0}
        always = {"need": 100, "answered": 100, "full": 100}
        ten_min = dict(enrich_minutes=10)
        self.assertEqual(self._n({"A": never}, **ten_min), 2)
        self.assertEqual(self._n({"A": always}, **ten_min), 1)
        self.assertAlmostEqual(patch.projected_measures(
            {"need": 100, "answered": 100, "full": 25}), 75)

    def test_a_small_sample_assumes_the_worst(self):
        lucky = {"need": 100, "answered": patch.YIELD_SAMPLE - 1,
                 "full": patch.YIELD_SAMPLE - 1}
        self.assertEqual(patch.projected_measures(lucky), 100)

    def test_waiting_and_4k_checks_count(self):
        self.assertEqual(patch.projected_measures({"measure": 3, "dv": 4}), 7)

    def test_measuring_off_adds_nothing(self):
        self.assertEqual(self._n({"A": {"measure": 10 ** 6}}, probe_movies=False), 0)

    def test_no_estimate_when_nightly_is_off(self):
        self.assertIsNone(self._n({"A": {"need": 10}}, enrich_nightly=False))

    def test_spacing_counts(self):
        full = {"answered": 100, "full": 100}
        self.assertEqual(self._n({"A": dict(full, need=1800)}, enrich_delay_ms=1000), 1)
        self.assertEqual(self._n({"A": dict(full, need=1801)}, enrich_delay_ms=1000), 2)

    def test_time_limit_setting_is_bounded_and_never_unlimited(self):
        f = patch._enrich_minutes
        self.assertEqual(f(30), 30)
        for junk in (0, -5, patch.MAX_ENRICH_MINUTES + 1, "abc", None):
            self.assertEqual(f(junk), patch.DEFAULT_ENRICH_MINUTES, junk)

    def test_start_hour_setting(self):
        self.assertEqual(patch._sweep_hour(2, patch.DEFAULT_ENRICH_HOUR), 2)
        self.assertEqual(patch._sweep_hour(24, patch.DEFAULT_ENRICH_HOUR),
                         patch.DEFAULT_ENRICH_HOUR)
        self.assertEqual(patch._sweep_hour("x"), patch.DEFAULT_SWEEP_HOUR)

    def _fn(self, name):
        tree = ast.parse(open(patch.__file__, encoding="utf-8").read())
        return next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name)

    def test_only_the_nightly_run_has_a_deadline_and_the_guard_gets_it(self):
        fn = self._fn("_run_enrichment")
        src = ast.unparse(fn)
        self.assertRegex(src, r"deadline = .*enrich_minutes.* if nightly else None")
        guards = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
                  and getattr(c.func, "id", None) == "RunGuard"]
        self.assertEqual(len(guards), 1)
        kw = {k.arg: k.value for k in guards[0].keywords}
        self.assertEqual(getattr(kw.get("deadline"), "id", None), "deadline")

    def test_the_nightly_run_repeats_batches_with_the_guards_clock(self):
        fn = self._fn("_run_enrichment")
        src = ast.unparse(fn)
        self.assertIn("run_batches(one_batch, guard.time_left)", src)
        self.assertIn("heartbeat=guard", src)
        # The single-batch shortcut must be taken ONLY for a manual run.
        shortcut = [n for n in ast.walk(fn) if isinstance(n, ast.If)
                    and any(isinstance(s, ast.Return)
                            and "one_batch()" in ast.unparse(s) for s in n.body)]
        self.assertEqual(len(shortcut), 1)
        self.assertEqual(ast.unparse(shortcut[0].test), "not nightly")

    def test_the_nightly_task_rechecks_its_switch_when_it_fires(self):
        fn = self._fn("run_nightly_enrichment")
        ifs = [n for n in fn.body if isinstance(n, ast.If)]
        self.assertTrue(ifs, "expected a switch check before running")
        self.assertIn("enrich_nightly", ast.unparse(ifs[0].test))
        self.assertIn("enrich_movies", ast.unparse(ifs[0].test))
        run_line = next(c.lineno for c in ast.walk(fn) if isinstance(c, ast.Call)
                        and getattr(c.func, "id", None) == "_run_enrichment")
        self.assertLess(ifs[0].lineno, run_line)

    def test_both_timers_are_removed_on_disable(self):
        src = ast.unparse(self._fn("remove_schedule"))
        self.assertIn("SWEEP_PERIODIC_NAME", src)
        self.assertIn("ENRICH_PERIODIC_NAME", src)

    def test_the_enrichment_timer_needs_enrichment_itself_on(self):
        src = ast.unparse(self._fn("ensure_schedule"))
        self.assertRegex(src, r"ENRICH_NIGHTLY_TASK_PATH,\s*cfg\['enrich_nightly'\] "
                              r"and cfg\['enrich_movies'\]")

    def test_status_reports_a_nightly_run_and_how_it_ended(self):
        import plugin as plugin_mod
        out = plugin_mod._format_last_run({
            "state": "finished", "nightly": True,
            "finished_at": "2030-01-02T03:04:05+00:00",
            "result": {"lookup": {"fetched": 40, "got_essentials": 9},
                       "measure": {"probed": 12, "gained": 11},
                       "batches": 5, "ended": "time limit reached"}})
        self.assertIn("last nightly 01-02 03:04 UTC, 5 batches, "
                      "time limit reached: +9 lookup, +11 measured", out)
        self.assertEqual(len(out.splitlines()), 1)

    def test_status_shows_the_nights_estimate_only_when_there_is_one(self):
        import plugin as plugin_mod
        base = EnrichStatusMessageTests()._st()
        self.assertIn("| ~4 nights\n",
                      plugin_mod._format_enrich_status(dict(base, nights=4)))
        self.assertIn("| ~1 night\n",
                      plugin_mod._format_enrich_status(dict(base, nights=1)))
        for none in (None, 0):
            self.assertNotIn(" night",
                             plugin_mod._format_enrich_status(dict(base, nights=none)))

    def test_the_probe_pass_honors_the_skip_list(self):
        fn = self._fn("probe_movies_impl")
        self.assertIn("without_accounts(per_movie, set(skip_accounts or ()))",
                      ast.unparse(fn))


class DolbyVisionCheckTests(unittest.TestCase):
    """A provider that describes a copy never includes its Dolby Vision record,
    so 4K copies get a probe anyway -- and the record has to land where the
    ranking plugin actually reads it."""

    DOVI = [{"side_data_type": "DOVI configuration record", "dv_profile": 5,
             "dv_bl_signal_compatibility_id": 0}]

    def _described(self, w=3840, h=2160, mark=None, **video):
        props = {"detailed_info": {"video": dict({"width": w, "height": h}, **video),
                                   "audio": {"codec_name": "eac3"}}}
        own = {"enrich": {"at": NOW.isoformat(), "got": "full"}}
        if mark is not None:
            own["probe"] = mark
        props[patch.OWN_PROPS_KEY] = own
        return props

    # --- what counts as 4K: the same rule vod_preferences ranks by ---------
    def test_4k_threshold_matches_the_ranking_plugin(self):
        f = patch.is_4k_video
        self.assertTrue(f({"width": 3840, "height": 2160}))
        self.assertTrue(f({"width": 3840, "height": 1600}))   # scope crop
        self.assertTrue(f({"width": 3700, "height": 1540}))   # within 5%
        self.assertTrue(f({"width": 2880, "height": 2160}))   # 4:3 at 2160
        self.assertFalse(f({"width": 3600, "height": 1500}))  # outside 5%
        self.assertFalse(f({"width": 1920, "height": 1080}))

    def test_a_poster_is_never_4k_video(self):
        f = patch.is_4k_video
        self.assertFalse(f({"width": 2160, "height": 3840,
                            "disposition": {"attached_pic": 1}}))
        self.assertFalse(f({"width": 3840, "height": 2160, "codec_name": "mjpeg"}))

    def test_junk_is_not_4k(self):
        for junk in (None, "x", {}, {"width": "wide"}):
            self.assertFalse(patch.is_4k_video(junk))

    def test_dovi_detection(self):
        self.assertTrue(patch.has_dovi({"side_data_list": self.DOVI}))
        self.assertFalse(patch.has_dovi({"side_data_list": [
            {"side_data_type": "Mastering display metadata"}]}))
        self.assertFalse(patch.has_dovi({}))
        self.assertFalse(patch.has_dovi(None))

    # --- which described copies get a check --------------------------------
    def test_a_described_4k_copy_is_checked_once(self):
        now = NOW.isoformat()
        self.assertTrue(patch.decide_dv_check(self._described(), now)[0])
        old = (NOW - timedelta(days=90)).isoformat()
        for got in ("full", "none"):
            self.assertFalse(patch.decide_dv_check(
                self._described(mark={"at": old, "got": got}), now)[0], got)

    def test_a_failed_check_retries_after_the_window(self):
        now = NOW.isoformat()
        recent = (NOW - timedelta(days=1)).isoformat()
        old = (NOW - timedelta(days=30)).isoformat()
        self.assertFalse(patch.decide_dv_check(
            self._described(mark={"at": recent, "got": "error"}), now)[0])
        self.assertTrue(patch.decide_dv_check(
            self._described(mark={"at": old, "got": "error"}), now)[0])

    def test_non_4k_and_already_known_are_not_checked(self):
        now = NOW.isoformat()
        self.assertFalse(patch.decide_dv_check(self._described(1920, 1080), now)[0])
        self.assertFalse(patch.decide_dv_check(
            self._described(side_data_list=self.DOVI), now)[0])

    def test_undescribed_copies_belong_to_gap_filling_not_here(self):
        props = {"detailed_info": {},
                 patch.OWN_PROPS_KEY: {"enrich": {"at": NOW.isoformat(), "got": "partial"}}}
        self.assertFalse(patch.decide_dv_check(props, NOW.isoformat())[0])
        self.assertTrue(patch.decide_probe(props, NOW.isoformat())[0])

    def test_gap_filling_still_skips_described_copies(self):
        self.assertFalse(patch.decide_probe(self._described(), NOW.isoformat())[0])

    # --- where the record lands --------------------------------------------
    def test_the_record_joins_the_providers_video_block(self):
        props = self._described(w=3840, h=1600)
        props = patch.store_probe(props, {
            "video": {"width": 3840, "height": 1608, "side_data_list": self.DOVI},
            "audio": {"codec_name": "aac"}})
        video = props["detailed_info"]["video"]
        self.assertEqual(video["side_data_list"], self.DOVI)
        # every value the provider sent is untouched
        self.assertEqual(video["height"], 1600)
        self.assertEqual(props["detailed_info"]["audio"]["codec_name"], "eac3")

    def test_known_side_data_is_never_overwritten(self):
        mine = [{"side_data_type": "Mastering display metadata"}]
        props = self._described(side_data_list=mine)
        props = patch.store_probe(props, {"video": {"side_data_list": self.DOVI}})
        self.assertEqual(props["detailed_info"]["video"]["side_data_list"], mine)

    def test_nothing_is_added_when_the_probe_found_no_side_data(self):
        props = patch.store_probe(self._described(), {"video": {"width": 3840}})
        self.assertNotIn("side_data_list", props["detailed_info"]["video"])

    # --- ordering and wiring -------------------------------------------------
    def _fn(self, name):
        tree = ast.parse(open(patch.__file__, encoding="utf-8").read())
        return next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name)

    def test_gaps_go_before_dv_checks_and_the_switch_is_honored(self):
        src = ast.unparse(self._fn("probe_movies_impl"))
        self.assertIn("per_movie = fill + dv", src)
        # elif: a copy needing gap-filling is never ALSO queued as a DV check
        self.assertRegex(src, r"if decide_probe\(props, now_iso\)\[0\]:\s*target = fill\s*"
                              r"elif cfg\['probe_4k_dv'\] and decide_dv_check\(props, now_iso\)\[0\]:")

    def test_status_counts_dv_checks_by_the_runs_own_rule(self):
        src = ast.unparse(self._fn("enrich_status"))
        self.assertRegex(src, r"cfg\['probe_movies'\] and cfg\['probe_4k_dv'\] and "
                              r"decide_dv_check\(props, now_iso\)\[0\]")
        self.assertIn("slot['dv'] += 1", src)
        self.assertIn("estimate_nights(per_account, cfg)", src)

    def test_status_text(self):
        import plugin as plugin_mod
        base = EnrichStatusMessageTests()._st()
        self.assertIn("measure 30 + 5 DV checks",
                      plugin_mod._format_enrich_status(dict(base, need_dv_check=5)))
        self.assertNotIn("DV checks",
                         plugin_mod._format_enrich_status(dict(base, need_dv_check=0)))
        out = plugin_mod._format_last_run({
            "state": "finished", "finished_at": "2030-01-02T03:04:05+00:00",
            "result": {"lookup": {"fetched": 1},
                       "measure": {"probed": 6, "gained": 4, "dv_checked": 2,
                                   "dv_found": 1, "dv_no_fallback": 1}}})
        self.assertIn("DV 1 (1 no-fallback)", out)
        quiet = plugin_mod._format_last_run({
            "state": "finished", "finished_at": "2030-01-02T03:04:05+00:00",
            "result": {"lookup": {"fetched": 1},
                       "measure": {"probed": 6, "gained": 6}}})
        self.assertNotIn("DV", quiet)

    def test_dv_is_counted_on_every_measurement(self):
        # A copy measured to fill its gaps reveals DV exactly as a 4K check
        # does; counting only the checks would hide the likeliest problems.
        st = {}
        patch.note_dv(st, {"video": {"side_data_list": self.DOVI}}, False)
        patch.note_dv(st, {"video": {"width": 3840}}, True)
        patch.note_dv(st, None, True)
        self.assertEqual(st, {"dv_checked": 2, "dv_found": 1, "dv_no_fallback": 1})

    def test_no_fallback_matches_the_ranking_plugins_rule(self):
        f = patch.is_dv_no_fallback
        rec = lambda **kw: {"side_data_list": [dict(
            {"side_data_type": "DOVI configuration record"}, **kw)]}
        self.assertTrue(f(rec(dv_profile=5, dv_bl_signal_compatibility_id=0)))
        self.assertTrue(f(rec(dv_profile=5)))                 # compat missing
        self.assertFalse(f(rec(dv_profile=8, dv_bl_signal_compatibility_id=1)))
        self.assertFalse(f(rec(dv_profile=8, dv_bl_signal_compatibility_id=4)))
        self.assertFalse(f({"side_data_list": [{"side_data_type": "Mastering display",
                                                "dv_bl_signal_compatibility_id": 0}]}))
        self.assertFalse(f(None))

    def test_the_probe_loop_tallies_every_measurement(self):
        fn = self._fn("probe_movies_impl")
        loop = next(n for n in ast.walk(fn)
                    if isinstance(n, ast.For) and getattr(n.iter, "id", None) == "rels")
        self.assertIn("note_dv(stats, parsed, rel.id in dv_ids)", ast.unparse(loop))

    def test_nightly_totals_carry_dv_counts(self):
        res = {"lookup": {"fetched": 1},
               "measure": {"probed": 2, "dv_checked": 2, "dv_found": 1}}
        it = iter([res, res, {"lookup": {}, "measure": {}}])
        tot = patch.run_batches(lambda skip: next(it), lambda: True)
        self.assertEqual(tot["measure"]["dv_checked"], 4)
        self.assertEqual(tot["measure"]["dv_found"], 2)


class MergeWhatIfTests(unittest.TestCase):
    """The preview has to answer one question correctly: which synced titles
    would get a new Dispatcharr id. A title keeps its id while anything still
    holds its row up; it loses it only when every copy moves away."""

    def _d(self, mid, action=None, tier="poster", into=100, new=False, name="T"):
        return {"movie_id": mid, "name": name, "action": action or patch.INJECT,
                "tier": tier, "canonical_id": None if new else into,
                "creates_new_row": new}

    def test_merges_and_new_titles_are_counted_by_tier(self):
        s = patch.whatif_summary(
            [self._d(1), self._d(2, tier="detail"), self._d(3, tier="detail", new=True),
             self._d(4, action=patch.NO_MATCH, tier=None)],
            {1: 1, 2: 1, 3: 1})
        self.assertEqual((s["copies"], s["merge"], s["create"]), (4, 2, 1))
        self.assertEqual(s["by_tier"], {"poster": 1, "detail": 2})
        self.assertEqual(s["projected"][patch.NO_MATCH], 1)

    def test_a_title_dies_only_when_every_copy_leaves(self):
        s = patch.whatif_summary(
            [self._d(1), self._d(1),           # both of movie 1's copies move
             self._d(2)],                      # one of movie 2's three
            {1: 2, 2: 3})
        self.assertEqual((s["titles_emptied"], s["titles_keep_id"]), (1, 1))

    def test_copies_on_other_accounts_keep_the_title_alive(self):
        # The count is of ALL copies, any account -- an in-scope provider's
        # unmatched copy still holds the row up.
        s = patch.whatif_summary([self._d(1)], {1: 2})
        self.assertEqual(s["titles_emptied"], 0)

    def test_synced_titles_that_would_be_renumbered(self):
        wanted = {1, 2, 3, 100}
        s = patch.whatif_summary(
            [self._d(1, into=100, name="A"),   # synced, into a synced title
             self._d(2, into=200, name="B"),   # synced, into an unsynced one
             self._d(3, into=100),             # synced but keeps its id
             self._d(9, into=100)],            # not synced at all
            {1: 1, 2: 1, 3: 2, 9: 1}, wanted)
        self.assertEqual(s["wanted_renumbered"], 2)
        self.assertEqual(s["wanted_into_wanted"], 1)
        self.assertEqual(s["wanted_lose_copy"], 1)
        names = {e["name"]: e["into_synced_title"] for e in s["wanted_examples"]}
        self.assertEqual(names, {"A": True, "B": False})

    def test_a_newly_tagged_title_is_never_already_synced(self):
        s = patch.whatif_summary([self._d(1, new=True)], {1: 1}, {1})
        self.assertEqual((s["wanted_renumbered"], s["wanted_into_wanted"]), (1, 0))

    def test_no_wanted_set_means_no_wanted_counts_not_zero(self):
        # "0 synced titles affected" would be a reassuring lie.
        s = patch.whatif_summary([self._d(1)], {1: 1}, None)
        self.assertIsNone(s["wanted_renumbered"])

    def _fn(self, name):
        tree = ast.parse(open(patch.__file__, encoding="utf-8").read())
        return next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name)

    def test_the_preview_uses_the_real_decision_with_merging_on(self):
        src = ast.unparse(self._fn("merge_whatif"))
        self.assertIn("sim = dict(cfg, dry_run=False)", src)
        self.assertRegex(src, r"decide_movie\(basic, name, sid, detail_map, canon, idx, sim\)")

    def test_the_preview_only_looks_at_accounts_not_yet_in_scope(self):
        self.assertIn(".exclude(name__in=scoped)", ast.unparse(self._fn("merge_whatif")))

    def test_the_preview_writes_nothing(self):
        src = ast.unparse(self._fn("merge_whatif"))
        for verb in (".save(", ".update(", ".delete(", "bulk_", ".create(",
                     "_append_log", "write_run_status"):
            self.assertNotIn(verb, src, verb)

    def test_popup_text(self):
        import plugin as plugin_mod
        s = patch.whatif_summary([self._d(1, into=100), self._d(2, tier="detail")],
                                 {1: 1, 2: 2}, {1, 100})
        s["not_looked_up"] = 7
        out = plugin_mod._format_whatif({"accounts": {"ProviderX": s}, "wanted": "ok"})
        self.assertIn("ProviderX: 2 id-less copies, 2 would merge (detail 1, poster 1)", out)
        self.assertIn("1 titles would get a new id, 1 of them synced "
                      "(1 into another synced title)", out)
        self.assertIn("7 not yet looked up", out)
        none = patch.whatif_summary([self._d(1)], {1: 1}, None)
        none["not_looked_up"] = 0
        out = plugin_mod._format_whatif({"accounts": {"P": none},
                                         "wanted": "no wanted set configured"})
        self.assertNotIn("synced (", out)
        self.assertIn("synced-title counts unavailable", out)
        self.assertIn("already in scope",
                      plugin_mod._format_whatif({"accounts": {}, "note":
                          "Every account is already in scope: x"}))


class ProviderDownTests(unittest.TestCase):
    """A provider that is not answering is one fact about the provider. Asking
    anyway sends a doomed request per copy and, worse, records a week-long
    retry against every one -- one outage night parks a provider's backlog."""

    class _Client:
        def __init__(self, fails):
            self.fails, self.calls = fails, 0

        def authenticate(self):
            self.calls += 1
            if self.calls <= self.fails:
                raise ConnectionError("no answer")

    def test_a_panel_that_answers(self):
        c = self._Client(0)
        self.assertEqual(patch.panel_answers(c), (True, None))
        self.assertEqual(c.calls, 1)

    def test_one_dropped_call_is_not_an_outage(self):
        self.assertTrue(patch.panel_answers(self._Client(1))[0])

    def test_a_panel_that_never_answers(self):
        c = self._Client(99)
        ok, why = patch.panel_answers(c)
        self.assertFalse(ok)
        self.assertIsInstance(why, ConnectionError)
        self.assertEqual(c.calls, patch.PREFLIGHT_ATTEMPTS)

    def test_unable_to_check_counts_as_answering(self):
        # Fails OPEN, the opposite of the busy check: wrongly skipping a
        # healthy provider costs it a night; a doomed lookup costs one request.
        self.assertTrue(patch._account_panel_answers(object()))

    def test_account_names(self):
        class A:
            def __init__(self, name): self.name = name

        class Rel:
            def __init__(self, aid, name):
                self.m3u_account_id, self.m3u_account = aid, A(name)

        groups = [(1, [Rel(1, "B"), Rel(2, "A")]), (2, [Rel(1, "B")])]
        self.assertEqual(patch.account_names(groups, {1, 2}), ["A", "B"])
        self.assertEqual(patch.account_names(groups, {2}), ["A"])

    # --- wiring --------------------------------------------------------------
    def _fn(self, name):
        tree = ast.parse(open(patch.__file__, encoding="utf-8").read())
        return next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name)

    def _before_batch(self, fn_name):
        fn = self._fn(fn_name)
        lines = {}
        for c in ast.walk(fn):
            if isinstance(c, ast.Call):
                lines.setdefault(ast.unparse(c), c.lineno)
        down = lines.get("without_accounts(per_movie, down_ids)")
        take = next(v for k, v in lines.items() if k.startswith("take_whole_movies("))
        self.assertIsNotNone(down, "%s must leave down providers out" % fn_name)
        self.assertLess(down, take, "...BEFORE the batch is chosen")
        self.assertIn("down_ids = down_account_ids(per_movie)", ast.unparse(fn))

    def test_lookups_leave_down_providers_out_before_batching(self):
        self._before_batch("enrich_movies_impl")

    def test_measuring_leaves_down_providers_out_before_batching(self):
        self._before_batch("probe_movies_impl")

    def test_down_check_finds_exactly_the_providers_not_answering(self):
        class A:
            def __init__(self, aid): self.id = aid

        class Rel:
            def __init__(self, aid): self.m3u_account_id, self.m3u_account = aid, A(aid)

        saved = patch._account_panel_answers
        asked = []
        patch._account_panel_answers = lambda acc: asked.append(acc.id) or acc.id != 2
        try:
            got = patch.down_account_ids([(1, [Rel(1), Rel(2)]), (2, [Rel(2), Rel(3)])])
        finally:
            patch._account_panel_answers = saved
        self.assertEqual(got, {2})
        self.assertEqual(sorted(asked), [1, 2, 3])   # each provider asked once

    def test_the_lookup_breaker_trips_quickly(self):
        # A provider gets a few failures' benefit of the doubt, no more: every
        # failure past the threshold is a doomed request and a held record.
        self.assertEqual(patch.LOOKUP_BREAKER, patch.PROBE_BREAKER)
        self.assertTrue(1 < patch.LOOKUP_BREAKER <= 5)

    def test_lookups_honor_the_nights_skip_list(self):
        self.assertIn("without_accounts(per_movie, set(skip_accounts or ()))",
                      ast.unparse(self._fn("enrich_movies_impl")))

    def test_a_failed_lookup_is_held_not_stamped(self):
        fn = self._fn("enrich_movies_impl")
        handlers = [h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)
                    and "get_vod_info" in ast.unparse(
                        next(t for t in ast.walk(fn) if isinstance(t, ast.Try)
                             and h in t.handlers))]
        self.assertEqual(len(handlers), 1)
        body = ast.unparse(ast.Module(body=handlers[0].body, type_ignores=[]))
        self.assertIn("deferred_errors.setdefault(name, []).append(rel)", body)
        self.assertNotIn("_stamp_own", body)
        self.assertIn("consecutive[name] >= LOOKUP_BREAKER", body)
        self.assertIn("continue", body)

    def test_held_failures_are_stamped_only_for_providers_still_answering(self):
        self.assertIn("errors_to_stamp(deferred_errors, stats['broken'])",
                      ast.unparse(self._fn("enrich_movies_impl")))

    def test_a_success_resets_the_breaker(self):
        self.assertIn("consecutive[name] = 0", ast.unparse(self._fn("enrich_movies_impl")))

    # --- the night ----------------------------------------------------------
    def test_lookups_get_the_skip_list(self):
        got = []
        patch.enrich_run_impl(False, lambda heartbeat=None, skip_accounts=None:
                              got.append(skip_accounts) or {}, None,
                              skip_accounts={5})
        self.assertEqual(got, [{5}])

    def test_a_provider_that_stopped_answering_lookups_is_skipped_all_night(self):
        calls = []
        results = iter([
            {"lookup": {"fetched": 2, "broken": ["P"], "broken_ids": [5]}},
            {"lookup": {"fetched": 2}},
            {"lookup": {}}])

        def batch(skip):
            calls.append(set(skip))
            return next(results)

        tot = patch.run_batches(batch, lambda: True)
        self.assertEqual(calls, [set(), {5}, {5}])
        self.assertEqual(tot["lookup"]["broken"], ["P"])

    def test_ending_on_a_down_provider_is_said(self):
        tot = patch.run_batches(lambda skip: {"lookup": {"down": ["P"]}}, lambda: True)
        self.assertIn("not answering", tot["ended"])
        self.assertEqual(tot["lookup"]["down"], ["P"])

    def test_status_names_down_and_stopped_providers(self):
        import plugin as plugin_mod
        out = plugin_mod._format_last_run({
            "state": "finished", "finished_at": "2030-01-02T03:04:05+00:00",
            "result": {"lookup": {"fetched": 3, "down": ["P1"], "broken": ["P2"]},
                       "measure": {"probed": 1, "down": ["P1", "P3"]}}})
        self.assertIn("not answering: P1, P3", out)
        self.assertIn("stopped looking up: P2", out)


class ParallelLaneTests(unittest.TestCase):
    """Measuring runs one lane per provider LOGIN. Each provider has its own
    connection limit, so lanes side by side cost no provider more than before;
    but two accounts on ONE login share its limit and must never overlap."""

    class _Acct:
        def __init__(self, aid, url, user):
            self.id, self.server_url, self.username = aid, url, user

    class _Rel:
        def __init__(self, rid, acct):
            self.id, self.m3u_account = rid, acct

    def test_accounts_on_one_login_share_a_lane(self):
        a = self._Acct(1, "http://Panel.example:8080/", "Me")
        b = self._Acct(2, "https://panel.example", "me")
        self.assertEqual(patch.provider_login(a), patch.provider_login(b))

    def test_different_logins_get_different_lanes(self):
        a = self._Acct(1, "http://panel.example", "me")
        self.assertNotEqual(patch.provider_login(a),
                            patch.provider_login(self._Acct(2, "http://panel.example", "you")))
        self.assertNotEqual(patch.provider_login(a),
                            patch.provider_login(self._Acct(3, "http://other.example", "me")))

    def test_an_unreadable_login_keeps_the_account_alone(self):
        a, b = self._Acct(1, "", ""), self._Acct(2, None, None)
        self.assertNotEqual(patch.provider_login(a), patch.provider_login(b))

    def test_lanes_keep_each_providers_order(self):
        p = self._Acct(1, "http://p.example", "u")
        q = self._Acct(2, "http://q.example", "u")
        rels = [self._Rel(i, acct) for i, acct in enumerate([p, q, p, q, p])]
        lanes = patch.group_by_login(rels)
        self.assertEqual([[r.id for r in v] for v in lanes.values()], [[0, 2, 4], [1, 3]])

    def test_lanes_really_run_side_by_side(self):
        # Each lane waits for the other at a barrier: run one after another,
        # the first would time out and the result would be missing.
        barrier = threading.Barrier(2, timeout=5)
        met = []

        def work(key, items):
            barrier.wait()
            met.append(key)

        patch.run_lanes({"a": [1], "b": [2]}, work, close_db=False)
        self.assertEqual(sorted(met), ["a", "b"])

    def test_a_failing_lane_does_not_stop_the_others(self):
        done = []

        def work(key, items):
            if key == "bad":
                raise RuntimeError("lane blew up")
            done.extend(items)

        # A thread's exception never reaches the others anyway; what the
        # wrapper must guarantee is that the failure is LOGGED (and the
        # thread's DB connection closed) rather than lost to stderr.
        with self.assertLogs(patch.logger, level="ERROR") as logs:
            patch.run_lanes({"bad": [0], "ok": [1, 2]}, work, close_db=False)
        self.assertEqual(done, [1, 2])
        self.assertTrue(any("probe lane 'bad' failed" in m for m in logs.output))

    def _fn(self, name):
        tree = ast.parse(open(patch.__file__, encoding="utf-8").read())
        return next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name)

    def _lane_src(self):
        return ast.unparse(self._fn("probe_lane"))

    def test_a_failed_probe_is_held_not_stamped(self):
        # The probe-side twin of the lookup rule: a failure is only recorded
        # once we know it was the stream and not the whole provider.
        lane = self._fn("probe_lane")
        stamps = [n for n in ast.walk(lane) if isinstance(n, ast.If)
                  and "_stamp_own(props, probe=" in ast.unparse(n)
                  and isinstance(n.test, ast.Compare)]
        tests = {ast.unparse(n.test) for n in stamps}
        self.assertIn("outcome != 'error'", tests,
                      "the probe record must be written only for answered probes")
        errs = [n for n in ast.walk(lane) if isinstance(n, ast.If)
                and ast.unparse(n.test) == "outcome == 'error'"]
        self.assertEqual(len(errs), 1)
        body = ast.unparse(ast.Module(body=errs[0].body, type_ignores=[]))
        self.assertIn("deferred_errors.setdefault(name, []).append(rel)", body)
        self.assertNotIn("_stamp_own", body)

    def test_the_probe_breaker_trips_and_is_recorded(self):
        lane = self._fn("probe_lane")
        trips = [n for n in ast.walk(lane) if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "consecutive[name] >= PROBE_BREAKER"]
        self.assertEqual(len(trips), 1)
        body = ast.unparse(ast.Module(body=trips[0].body, type_ignores=[]))
        self.assertIn("stats['broken'].append(name)", body)
        self.assertIn("stats.setdefault('broken_ids', []).append(", body)
        self.assertTrue(1 < patch.PROBE_BREAKER <= 5)

    def test_one_lane_stopping_stops_them_all(self):
        # Every lane checks the shared stop flag before each probe, so a time
        # limit or lost lock seen by one lane halts the others promptly.
        loop = next(n for n in ast.walk(self._fn("probe_lane"))
                    if isinstance(n, ast.For))
        first_with = next(s for s in loop.body if isinstance(s, ast.With))
        self.assertIn("if stats.get('stopped'):\n    return",
                      ast.unparse(ast.Module(body=first_with.body, type_ignores=[])))

    def test_measuring_runs_in_login_lanes(self):
        self.assertIn("run_lanes(group_by_login(chosen), probe_lane)",
                      ast.unparse(self._fn("probe_movies_impl")))

    def test_every_shared_counter_is_touched_under_the_lock(self):
        lane = self._fn("probe_lane")
        parents = {}
        for node in ast.walk(lane):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def under_lock(node):
            while node in parents:
                node = parents[node]
                if isinstance(node, ast.With) and any(
                        ast.unparse(i.context_expr) == "lock" for i in node.items):
                    return True
            return False

        shared = ("stats", "slot", "consecutive", "deferred_errors", "per_account")
        writes = []
        for node in ast.walk(lane):
            if isinstance(node, (ast.AugAssign, ast.Assign)):
                targets = [node.target] if isinstance(node, ast.AugAssign) else node.targets
                if any(ast.unparse(t).startswith(shared) for t in targets):
                    writes.append(node)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                owner = ast.unparse(node.func.value)
                if owner.startswith(shared) and node.func.attr in (
                        "append", "setdefault", "update"):
                    writes.append(node)
                if ast.unparse(node.func) == "note_dv" or ast.unparse(node) .startswith("note_dv("):
                    writes.append(node)
            elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "note_dv":
                writes.append(node)
        self.assertTrue(writes)
        loose = [ast.unparse(w)[:60] for w in writes if not under_lock(w)]
        self.assertEqual(loose, [], "shared state touched outside the lock")


class LearnedLookupSkipTests(unittest.TestCase):
    """An account whose lookups have never returned video and audio, over a
    real sample, stops being looked up: its copies go straight to measuring.
    Learned from what each account returns, so it names no provider."""

    def _props(self, got):
        return {patch.OWN_PROPS_KEY: {"enrich": {"at": NOW.isoformat(), "got": got}}}

    def test_only_real_answers_are_counted(self):
        y = {}
        for got in ("full", "partial", "none", "error", "skipped"):
            patch.tally_lookup_yield(y, 1, self._props(got))
        patch.tally_lookup_yield(y, 1, {})               # never looked up
        self.assertEqual(y, {1: [3, 1]})

    def test_the_threshold(self):
        n = patch.LEARN_AFTER
        self.assertEqual(patch.non_describing({1: [n - 1, 0]}), set())
        self.assertEqual(patch.non_describing({1: [n, 0]}), {1})

    def test_describing_even_once_disqualifies(self):
        self.assertEqual(patch.non_describing({1: [10 ** 4, 1]}), set())

    def test_a_skipped_lookup_counts_as_done_and_sends_the_copy_to_measuring(self):
        props = self._props("skipped")
        self.assertFalse(patch.decide_enrich(props, NOW.isoformat())[0])
        self.assertTrue(patch.decide_probe(props, NOW.isoformat())[0])

    def test_marking_nothing_touches_nothing(self):
        self.assertEqual(patch.mark_lookup_skipped([], NOW.isoformat()), 0)

    def _fn(self, name):
        tree = ast.parse(open(patch.__file__, encoding="utf-8").read())
        return next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name)

    def test_the_mark_is_a_skip_in_the_lookup_key(self):
        src = ast.unparse(self._fn("mark_lookup_skipped"))
        self.assertIn("_stamp_own(props, enrich={'at': now_iso, 'got': 'skipped'})", src)

    def test_every_candidate_is_counted_and_silent_accounts_leave_before_batching(self):
        fn = self._fn("enrich_movies_impl")
        src = ast.unparse(fn)
        # Counted BEFORE the fetch decision, so copies that need no lookup
        # still teach us about their account.
        self.assertRegex(src, r"tally_lookup_yield\(yields, rel\.m3u_account_id, props\)\s*"
                              r"should, _why = decide_enrich\(props, now_iso\)")
        lines = {ast.unparse(c): c.lineno for c in ast.walk(fn) if isinstance(c, ast.Call)}
        drop = lines["without_accounts(per_movie, silent)"]
        take = next(v for k, v in lines.items() if k.startswith("take_whole_movies("))
        self.assertLess(drop, take)
        self.assertIn("mark_lookup_skipped(marked, now_iso)", src)

    def test_status_moves_silent_lookups_to_measuring(self):
        src = ast.unparse(self._fn("enrich_status"))
        self.assertIn("slot['measure'] += slot['need']", src)
        self.assertIn("slot['need'] = 0", src)
        self.assertIn("silent = non_describing(yields)", src)

    def test_status_marks_silent_accounts(self):
        import plugin as plugin_mod
        st = EnrichStatusMessageTests()._st()
        st["per_account"]["Provider1"]["not_describing"] = True
        out = plugin_mod._format_enrich_status(st)
        self.assertIn("Provider1* 20/25", out)
        self.assertNotIn("*", plugin_mod._format_enrich_status(
            EnrichStatusMessageTests()._st()).split("left lookup/measure:")[-1])

    def test_last_run_reports_skipped_lookups(self):
        import plugin as plugin_mod
        out = plugin_mod._format_last_run({
            "state": "finished", "finished_at": "2030-01-02T03:04:05+00:00",
            "result": {"lookup": {"fetched": 0, "lookup_skipped": 400,
                                  "not_describing": ["P"]},
                       "measure": {"probed": 25, "gained": 25}}})
        self.assertIn("lookups now skipped: P", out)

    def test_nightly_totals_carry_skips(self):
        it = iter([{"lookup": {"fetched": 1, "lookup_skipped": 400,
                               "not_describing": ["P"]}},
                   {"lookup": {}}])
        tot = patch.run_batches(lambda skip: next(it), lambda: True)
        self.assertEqual(tot["lookup"]["lookup_skipped"], 400)
        self.assertEqual(tot["lookup"]["not_describing"], ["P"])


class ManifestParityTests(unittest.TestCase):
    """plugin.json and the Plugin class both declare the UI, so they must agree.

    Editing one and forgetting the other is the easiest mistake to make here, and
    it is invisible until Dispatcharr renders whichever it happens to read.
    """

    @classmethod
    def setUpClass(cls):
        import json
        import logging
        import os
        # Importing plugin.py runs install(), which cannot find Django here.
        # That is handled and logged; silence it so test output stays readable.
        logging.getLogger("plugins.dispatcharr_vod_merge").setLevel(logging.CRITICAL)
        import plugin
        cls.cls = plugin.Plugin
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "plugin.json"), encoding="utf-8") as fh:
            cls.man = json.load(fh)

    def test_top_level_matches(self):
        for key in ("name", "version", "description", "author", "help_url"):
            if key in self.man or hasattr(self.cls, key):
                self.assertEqual(self.man.get(key), getattr(self.cls, key, None), key)

    def test_fields_match(self):
        jf = {f["id"]: f for f in self.man["fields"]}
        pf = {f["id"]: f for f in self.cls.fields}
        self.assertEqual(sorted(jf), sorted(pf))
        for fid in jf:
            self.assertEqual(jf[fid], pf[fid], "field %s" % fid)

    def test_field_ORDER_matches(self):
        # Order is what the UI renders, so it is part of the contract -- and
        # test_fields_match compares by id, which would not notice a reshuffle.
        self.assertEqual([f["id"] for f in self.man["fields"]],
                         [f["id"] for f in self.cls.fields])

    def test_each_kind_has_its_own_switch_and_its_own_scope(self):
        # Series and movies must stay symmetrical: a toggle and an account
        # allowlist each. An asymmetry here is what prompted the settings
        # rework, so pin it.
        ids = [f["id"] for f in self.cls.fields]
        for fid in ("merge_series", "merge_movies",
                    "allowed_accounts", "movie_accounts"):
            self.assertIn(fid, ids)
        by_id = {f["id"]: f for f in self.cls.fields}
        self.assertEqual(by_id["merge_series"]["type"], "boolean")
        self.assertEqual(by_id["merge_movies"]["type"], "boolean")
        # Each toggle sits above the scope it governs.
        self.assertLess(ids.index("merge_series"), ids.index("allowed_accounts"))
        self.assertLess(ids.index("merge_movies"), ids.index("movie_accounts"))

    def test_defaults_are_the_deliberate_asymmetry(self):
        # Series ON, movies OFF -- so neither an upgrade nor a fresh install
        # changes behavior on its own. Documented in patch.py; asserted here so
        # a "tidy up the defaults" edit has to be deliberate.
        self.assertIs(patch.DEFAULT_MERGE_SERIES, True)
        self.assertIs(patch.DEFAULT_MERGE_MOVIES, False)
        by_id = {f["id"]: f for f in self.cls.fields}
        self.assertIs(by_id["merge_series"]["default"], True)
        self.assertIs(by_id["merge_movies"]["default"], False)
        self.assertIs(by_id["dry_run"]["default"], True)

    def test_the_two_protective_settings_default_on(self):
        # Both fix upstream data loss rather than changing this plugin's
        # behavior, so an install that ignores them is still protected. And
        # neither may be gated by dry_run -- that would make dry run permit
        # the loss it exists to avoid.
        self.assertIs(patch.DEFAULT_PROTECT_MERGES, True)
        self.assertIs(patch.DEFAULT_PRESERVE_DETAIL, True)
        by_id = {f["id"]: f for f in self.cls.fields}
        self.assertIs(by_id["protect_merges"]["default"], True)
        self.assertIs(by_id["preserve_detail"]["default"], True)

    def test_actions_match(self):
        ja = {a["id"]: a for a in self.man["actions"]}
        pa = {a["id"]: a for a in self.cls.actions}
        self.assertEqual(sorted(ja), sorted(pa))
        for aid in ja:
            self.assertEqual(ja[aid], pa[aid], "action %s" % aid)

    def test_every_action_is_handled(self):
        import inspect
        src = inspect.getsource(self.cls.run)
        for a in self.man["actions"]:
            self.assertIn('"%s"' % a["id"], src, "action %s has no branch in run()" % a["id"])

    def test_variant_default_regex_compiles(self):
        # The manifest carries a JSON-escaped copy of the pattern; a bad escape
        # would only surface at runtime.
        for f in self.man["fields"]:
            if f["id"] == "variant_pattern":
                re.compile(f["default"], re.I)
                self.assertEqual(f["default"], patch.DEFAULT_VARIANT_PATTERN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
