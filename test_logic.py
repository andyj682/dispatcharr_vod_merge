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

import re
import unittest

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
    def test_punctuation_and_case_are_normalised(self):
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

    def test_int_tmdb_is_normalised_to_str(self):
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
                     "Some Show [B&W]", "Some Show [Colorized]", "Some Show [Colourised]"):
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

    def test_int_is_normalised(self):
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
        # changes behaviour on its own. Documented in patch.py; asserted here so
        # a "tidy up the defaults" edit has to be deliberate.
        self.assertIs(patch.DEFAULT_MERGE_SERIES, True)
        self.assertIs(patch.DEFAULT_MERGE_MOVIES, False)
        by_id = {f["id"]: f for f in self.cls.fields}
        self.assertIs(by_id["merge_series"]["default"], True)
        self.assertIs(by_id["merge_movies"]["default"], False)
        self.assertIs(by_id["dry_run"]["default"], True)

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
