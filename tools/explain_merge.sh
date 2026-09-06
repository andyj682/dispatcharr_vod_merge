#!/usr/bin/env bash
# explain_merge.sh — answer "why didn't this title merge?", and build the manual
# approval line for it if that is the answer.
#
# The plugin reports what it WILL merge (Series merge status / Movie merge status)
# but nothing explains a NEGATIVE, and a no-signal entry leaves no audit trail by
# design. This loads the installed plugin's patch.py and runs its REAL decision
# function against live DB state, so the verdict here is the verdict the scan
# will reach. Read-only: no provider calls, no writes, no scan.
#
# Run this on the Dispatcharr host (wherever `docker` is). Edit the -e vars
# below, then paste the whole block.
#
#   TITLE   substring of the title, case-insensitive. Use a distinctive FRAGMENT
#           rather than the full name -- it avoids shell-quoting trouble with
#           apostrophes and matches providers' varied punctuation. Searching
#           "phoenix" finds "The Phoenix's Return" without the quoting headache.
#   KIND    "movie" (default), "series", or "both"
#
# Change the container name "dispatcharr" if yours differs (docker ps to check).
#
# How to read the output:
#   * TAGGED     rows that already carry a tmdb id. One of these is the canonical
#                you want duplicates folded into; its tmdb is the approval target.
#   * ID-LESS    rows with no tmdb. These are the duplicates needing help.
#   * VERDICT    what the plugin decides for each id-less row right now:
#       has_id        already tagged, not a candidate
#       inject/dry_run  it WILL merge on the next scan -- no action needed
#       no_signal     no poster, no plot, no detail id: nothing to match on.
#                     A manual approval is the ONLY remedy.
#       no_match      it had a signal, but nothing in the tagged index matched
#       ambiguous     the signal hit a key claimed by more than one title, so it
#                     self-rejected. Approve manually only if you are certain.
#       no_canonical  a detail id exists but no row holds it (see tag_unique_movies)
#       variant/denied  a guard refused it, deliberately
#
# If the suggested line looks right, paste it into the plugin's "Approve
# manually" setting. Multiple approvals go in one comma-separated list.

docker exec -i \
  -e TITLE="phoenix" \
  -e KIND="movie" \
  dispatcharr python manage.py shell << 'PY'
import os, importlib.util

TOK = (os.environ.get('TITLE') or '').strip()
KIND = (os.environ.get('KIND') or 'movie').strip().lower()
assert TOK, "Set TITLE to a fragment of the title you are looking for."

PLUGIN = 'dispatcharr_vod_merge'
pdir = os.environ.get('DISPATCHARR_PLUGINS_DIR') or os.environ.get('PLUGINS_DIR') or '/data/plugins'

m = cfg = None
try:
    sp = importlib.util.spec_from_file_location('vm', os.path.join(pdir, PLUGIN, 'patch.py'))
    m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
    cfg = m._load_config(force=True)
except Exception as exc:
    print("(!) could not load the plugin, verdicts will be skipped:", exc)


def report(kind, rels, id_of, tmdb_of, name_of, decide):
    print("=" * 70)
    print("%s matching %r: %d relation(s)" % (kind.upper(), TOK, len(rels)))
    tagged = [r for r in rels if tmdb_of(r)]
    idless = [r for r in rels if not tmdb_of(r)]

    print("\nTAGGED (canonical candidates):")
    for r in tagged:
        print("   %s:%s | tmdb %s | %r" % (r.m3u_account.name, id_of(r), tmdb_of(r), name_of(r)))
    if not tagged:
        print("   (none -- nothing here is tagged, so there is no merge target;")
        print("    an approval would CREATE a newly tagged row, not merge into one)")

    print("\nID-LESS (candidates for an approval):")
    for r in idless:
        print("   %s:%s | %r" % (r.m3u_account.name, id_of(r), name_of(r)))
    if not idless:
        print("   (none -- nothing to do)")

    if m is not None and idless:
        print("\nVERDICT for each id-less row (what the next scan would do):")
        for r in idless:
            try:
                print("   %s:%s -> %s" % (r.m3u_account.name, id_of(r), decide(r)))
            except Exception as exc:
                print("   %s:%s -> (failed: %s)" % (r.m3u_account.name, id_of(r), exc))

    ids = sorted({str(tmdb_of(r)) for r in tagged})
    print("\nSUGGESTED approval line(s):")
    if not idless:
        print("   (nothing id-less)")
    elif len(ids) == 1:
        for r in idless:
            print("   %s:%s=%s" % (r.m3u_account.name, id_of(r), ids[0]))
        print("   ^ verify %s really is this title on themoviedb.org before pasting." % ids[0])
    elif not ids:
        print("   (no tagged row to point at -- find the tmdb id yourself on themoviedb.org)")
    else:
        print("   AMBIGUOUS: tagged rows carry different tmdb ids %s." % ids)
        print("   Pick the correct one by hand; auto-suggesting would be a guess.")
    print()


if KIND in ('movie', 'both'):
    from apps.vod.models import M3UMovieRelation
    rels = list(M3UMovieRelation.objects
                .filter(movie__name__icontains=TOK)
                .select_related('movie', 'm3u_account'))

    def decide_movie(r):
        props = r.custom_properties or {}
        basic = dict(props.get('basic_data') or {})
        basic.setdefault('name', r.movie.name)
        dt = m.detail_tmdb(props)
        dmap = {str(r.stream_id): (dt, None)} if dt else {}
        idx = m._get_movie_indexes(force=True)
        action, tmdb_id, tier, _ = m.decide_movie(
            basic, r.m3u_account.name, r.stream_id, dmap,
            m._canonical_map({dt} if dt else set()), idx, cfg)
        return ("%s (tier=%s tmdb=%s) | poster_key=%s | detail_tmdb=%s | swept=%s"
                % (action, tier, tmdb_id,
                   m.entry_poster_key(basic, m.MOVIE_POSTER_FIELDS),
                   dt, bool(props.get('detailed_fetched'))))

    report('movie', rels, lambda r: r.stream_id,
           lambda r: r.movie.tmdb_id, lambda r: r.movie.name, decide_movie)

if KIND in ('series', 'both'):
    from apps.vod.models import M3USeriesRelation
    rels = list(M3USeriesRelation.objects
                .filter(series__name__icontains=TOK)
                .select_related('series', 'm3u_account'))

    def decide_series(r):
        basic = dict((r.custom_properties or {}).get('basic_data') or {})
        basic.setdefault('name', r.series.name)
        if not m.entry_plot_hash(basic) and r.series.description:
            basic = dict(basic, plot=r.series.description)
        idx = m._get_indexes(force=True)
        action, tmdb_id, tier, key = m.decide(
            basic, r.m3u_account.name, r.external_series_id, idx, cfg)
        return ("%s (tier=%s tmdb=%s) | poster_key=%s"
                % (action, tier, tmdb_id, m.entry_poster_key(basic)))

    report('series', rels, lambda r: r.external_series_id,
           lambda r: r.series.tmdb_id, lambda r: r.series.name, decide_series)
PY
