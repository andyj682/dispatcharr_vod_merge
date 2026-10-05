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
        calls = [c for c in ast.walk(self._fn("run_enrichment"))
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
        calls = [c for c in ast.walk(self._fn("run_enrichment"))
                 if isinstance(c, ast.Call)
                 and getattr(c.func, "id", None) == "enrich_run_impl"]
        self.assertEqual(len(calls), 1)
        self.assertIn("heartbeat", {k.arg for k in calls[0].keywords},
                      "without a heartbeat the lock lapses mid-run and a second "
                      "run can start alongside this one")

    def test_the_probe_loop_checks_the_heartbeat(self):
        loops = [n for n in ast.walk(self._fn("probe_movies_impl"))
                 if isinstance(n, ast.For) and getattr(n.iter, "id", None) == "chosen"]
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

        def lookup(heartbeat=None):
            calls.append(("lookup", heartbeat))
            return lookup_result if lookup_result is not None else {"fetched": 5}

        def measure(heartbeat=None):
            calls.append(("measure", heartbeat))
            return {"probed": 2}

        hb = object()
        out = patch.enrich_run_impl(probe_enabled, lookup, measure, heartbeat=hb)
        return out, calls, hb

    def test_lookup_then_measure_both_with_the_heartbeat(self):
        out, calls, hb = self._run()
        self.assertEqual(calls, [("lookup", hb), ("measure", hb)])
        self.assertEqual(out, {"lookup": {"fetched": 5}, "measure": {"probed": 2}})

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
        self.assertIn("finished 2030-01-02 03:04 UTC", out)
        self.assertIn("looked up 25 (gained 4)", out)
        self.assertIn("measured 3 (gained 3)", out)
        self.assertIn("errors 3", out)          # summed across both steps
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
        for needle in ("need measuring 30", "(10 runs)", "need lookup 50",
                       "have video+audio 20", "finished 2030-01-02 03:04 UTC",
                       "Provider0 30/20/25"):
            self.assertIn(needle, out)

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
