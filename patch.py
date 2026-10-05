"""
Dispatcharr VOD Merge & Enrich -- implementation
================================================

SEE `DESIGN.md` for the current, complete rationale. This docstring predates
movie support and describes the series path only; it is kept because the series
reasoning below is still accurate, but it is no longer the whole picture.

Problem
-------
Dispatcharr merges VOD series across providers ONLY at listing-scan time, by a
key derived from the raw `get_series` payload with strict priority
`tmdb_<id>` > `imdb_<id>` > `name_<name>_<year>` (apps/vod/tasks.py, in
`process_series_batch`). A provider that omits the TMDB id therefore keys by its
own junky name and creates a SEPARATE `Series` row -- a duplicate of a show you
already have. `lookup_by_name_year` only matches rows whose tmdb AND imdb are
both null, so the duplicate can never fall back onto the tmdb-bearing canonical.

There is no self-heal path for series: `handle_series_id_conflicts` exists in
tasks.py but has zero callers, and `refresh_series_episodes` reads the detailed
`get_series_info` payload (plot/rating/genre/year) while ignoring any id in it.

Why not "merge the rows afterwards"
-----------------------------------
Because the scan RE-DERIVES relation -> series from the provider payload on every
run (`bulk_update` includes `'series'`). Any after-the-fact row merge is undone
on the next VOD refresh, which re-keys the entry by name+year and mints a fresh
orphan. Durability requires injecting an id BEFORE the merge key is computed.

The signals: shared external artifacts
--------------------------------------
Neither signal is fuzzy similarity. Both are artifacts the providers copied from
the same upstream source, so a match is an equality test, not a judgement.

1. **TMDB poster basename.** Providers that omit the tmdb id very often still
   serve TMDB-hosted artwork, and a TMDB asset path is unique to a title. Only
   the size segment differs:

       Provider 1  .../t/p/w600_and_h900_bestv2/acYXu4KaDj1NIkMgObnhe4C4a0T.jpg
       Provider 2  .../t/p/w154/acYXu4KaDj1NIkMgObnhe4C4a0T.jpg
       Provider 3  .../t/p/w500/acYXu4KaDj1NIkMgObnhe4C4a0T.jpg

   Measured live over a full library: near-perfectly unique, a handful of
   collisions in total.

2. **Plot text.** The listing's `plot` (which is what core stores as
   `Series.description`) is usually TMDB's overview verbatim, so it matches
   byte-for-byte across providers. Measured live: a large usable index with
   only a handful of collisions.

Both beat name matching, and the plot tier beats it structurally -- in live
testing it matched a canonical titled in another language, one whose name was
truncated with an ellipsis, and one named entirely differently from the
duplicate. No name scheme reaches those.

Guard (identical for both tiers): a key is only usable if it maps to EXACTLY ONE
tmdb id across the tagged library. That single rule makes placeholder artwork and
boilerplate blurbs -- which would otherwise merge hundreds of unrelated titles in
one pass -- reject themselves automatically.

What we are trusting
--------------------
Not the provider's id (they supply none) but their metadata being INTERNALLY
CONSISTENT: that the plot/poster they attached belongs to the title they
attached. A provider that bolts the wrong synopsis onto a show would have that
error inherited. The tier is recorded on every log entry so plot-tier merges --
the only ones exposed to that failure -- can be reviewed as a class.

Mechanism
---------
`process_series_batch(account, batch, categories, relations, scan_start_time)` is
called INLINE from `refresh_series` (not via `.delay()`), and `batch` is a list
of the raw provider dicts whose `tmdb` key becomes the merge key. So this module
WRAPS the function and mutates entries in place before delegating -- it never
forks the function body. The compat surface is therefore just the signature plus
the dict keys read/written, not 300 lines that need re-diffing every release.

Injection failures are swallowed: a bug here degrades to "no merge", never to a
broken VOD ingest.

Durability (verified live over two consecutive refreshes)
---------------------------------------------------------
Once injected, every subsequent scan re-derives the same `tmdb_<id>` key, finds
the canonical, and repoints the existing relation onto it. The duplicate cannot
come back. New arrivals from an untagged provider are merged at first sight, so
no orphan is ever created for them. Orphan `Series` rows left with no relations
are pruned by the scan itself, and relation rows keep their ids (they are moved
by `bulk_update`, which does not even fire `auto_now`).

Author: andyj682
License: MIT
"""

import hashlib
import json
import os
import re
import threading
import time
from collections import namedtuple

import logging

logger = logging.getLogger("plugins.dispatcharr_vod_merge")


def _adopt_log_level() -> None:
    """Give this logger a level of its own instead of inheriting root's.

    Dispatcharr's logging config names every logger it cares about --
    `apps`, `celery`, `core.tasks` and so on -- and gives each an explicit
    handler with `propagate: False`. Plugin loggers are not in that list, so
    they carry no level and inherit whatever root happens to be set to.

    That is fine in the web workers, where root sits at the configured level.
    It is NOT fine in a Celery PREFORK CHILD: billiard reconfigures root in
    each forked child and leaves it at WARNING, so every `logger.info()` from
    a plugin is discarded at the logger, before any handler sees it. The VOD
    scan runs in exactly that process, which meant this plugin's scan-time
    output -- the install line, the wrapper-active line, the per-batch tally --
    was invisible precisely where it mattered, while core's own `apps.*` lines
    came through fine. The wrappers were running; only the evidence was gone.

    Measured on Dispatcharr 0.31.0: root level 20 in uWSGI, daphne and the
    prefork parent, but 30 in the prefork children, whose root handler
    identifies itself with billiard's SUBDEBUG level name.

    We adopt the level of the `apps` logger rather than forcing INFO, so
    `DISPATCHARR_LOG_LEVEL` is still respected -- `apps` keeps its explicitly
    configured level in the children, which is why its records survive. Only
    set it if nothing has set one already, so a caller (or a test) that
    deliberately silences this logger keeps control.
    """
    try:
        if logger.level != logging.NOTSET:
            return
        reference = logging.getLogger("apps").getEffectiveLevel()
        logger.setLevel(reference or logging.INFO)
    except Exception:  # never let logging setup break the import
        pass


_adopt_log_level()

try:
    from celery import shared_task
except Exception:  # pragma: no cover - celery is always present at runtime
    def shared_task(*dargs, **dkwargs):
        def deco(fn):
            return fn
        if len(dargs) == 1 and callable(dargs[0]) and not dkwargs:
            return dargs[0]
        return deco


# --------------------------------------------------------------------------- #
# Constants / tunables
# --------------------------------------------------------------------------- #

# Must match the installed plugin folder name.
PLUGIN_KEY = "dispatcharr_vod_merge"

# Durable audit trail (its own CoreSettings row). Every injection is recorded so
# a merged title surfaces for review instead of changing state silently.
LOG_CORE_KEY = "dispatcharr_vod_merge_log"
LOG_CORE_NAME = "Dispatcharr VOD Merge - injection log"
# Keep newest. Only actual moves are recorded (see is_new_merge), so this holds
# real history rather than re-derivations -- but a bulk import of new categories
# legitimately produces thousands of entries at once, and 500 evicted genuine
# history within a single sync. A few thousand small dicts in a JSONField is
# cheap; roughly 250 bytes each.
MAX_LOG_ENTRIES = 5000

# Poster and plot fields differ between the two listing shapes, so they are
# passed in rather than read from a global. Series listings expose `cover` and
# `plot`; movie listings expose `stream_icon` and (only after a detail fetch)
# `description`.
POSTER_FIELDS = ("cover", "cover_big")            # series
PLOT_FIELDS = ("plot", "description")             # series
MOVIE_POSTER_FIELDS = ("stream_icon", "cover_big", "cover")
MOVIE_PLOT_FIELDS = ("description", "plot")

# A TMDB image URL looks like https://image.tmdb.org/t/p/<size>/<asset>.jpg
_TMDB_URL_HINTS = ("image.tmdb.org", "/t/p/")
_IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp)$", re.I)
_NONALNUM_RE = re.compile(r"[^a-z0-9]+")

# Minimum normalized plot length. Short blurbs ("Season 2.", a stray genre word)
# collide easily; the full text of a real synopsis does not.
#
# 40, not the 80 originally guessed. Measured over a real library, collisions do
# not move at all as the floor drops -- 4 ambiguous hashes at every threshold
# from 80 down to 40 -- while the usable index grows by ~150 keys. The floor was
# costing reach and buying no safety, and it was excluding real one-line
# synopses: "The most miserable person on Earth must save the world from
# happiness." normalizes to 69 characters.
#
# The uniqueness guard is what actually protects against generic text, not this
# length; anything two shows share is rejected regardless of how long it is.
MIN_PLOT_CHARS = 40

# Variant editions are deliberately NOT merged: a separate Series row is the only
# way Dispatcharr can represent "same show, different edition", since relations
# carry no edition label. Merging would make the variant's streams silently
# selectable for the canonical's episodes. NOTE this guard is load-bearing for
# BOTH tiers -- an alternate cut shares the canonical's poster AND its plot.
DEFAULT_VARIANT_PATTERN = r"\[[^\]]*(?:b\s*&\s*w|black\s*/?\s*white|colou?ri[sz]ed)[^\]]*\]"

DEFAULT_DRY_RUN = True           # safe by default: report, do not inject
DEFAULT_INDEX_TTL_SECONDS = 600  # one scan's batches all fall inside this

# Movie detail sweep. Bounded per run so the action returns before any UI
# timeout; it is resumable, so the backlog is walked by running it again.
DEFAULT_SWEEP_LIMIT = 50
DEFAULT_SWEEP_DELAY_MS = 200     # spacing between provider calls

# Series and movies each have their own on/off switch, so either kind can be
# rolled out without the other. The defaults differ ON PURPOSE and the asymmetry
# is about upgrades, not about one being riskier: series merging predates the
# movie support and has always been on, so defaulting it off would silently stop
# it for anyone upgrading. Movies stayed opt-in for the same reason in reverse --
# turning them on by default would start merging for someone who had deliberately
# left them alone. `dry_run` is the actual safety gate for a fresh install; both
# switches sit behind it.
DEFAULT_MERGE_SERIES = True
DEFAULT_MERGE_MOVIES = False
# Injecting an id no other row holds does not merge anything -- it re-tags a
# title that already has its own entry, changing its Dispatcharr id. Useful
# long-term (it stops the row churning on provider renames) but it is churn
# without dedupe, so it is opt-in.
DEFAULT_TAG_UNIQUE_MOVIES = False
CONFIG_TTL_SECONDS = 5.0         # in-process cache of the plugin settings read

# One decided entry, carried from the decision loop to the audit phase. A plain
# tuple here cost a real bug: `tier` was added to it later and one comprehension
# kept unpacking four values, so every movie batch raised ValueError after the
# entries had already been mutated -- merges landed, the audit never ran, and the
# wrapper's own except swallowed it. Named fields make that class impossible.
Decision = namedtuple("Decision", "entry external_id action tmdb_id tier key")


# Which signal produced a match.
TIER_POSTER = "poster"
TIER_PLOT = "plot"
TIER_MANUAL = "manual"
TIER_DETAIL = "detail"   # movies: the tmdb the provider put in its own detail
TIER_PROTECT = "protect"  # not a match at all -- a prevented destructive merge
TIER_PRESERVE = "preserve"  # nor this -- essential detail a core write dropped
TIER_PRUNE = "prune"        # nor this -- a refused catalog-wide delete

# Decision outcomes (also the audit-log `action` values).
INJECT = "inject"
DRY_RUN = "dry_run"
HAS_ID = "has_id"
NO_SIGNAL = "no_signal"          # neither a usable poster nor a usable plot
NO_MATCH = "no_match"            # had a signal, nothing matched it
NO_CANONICAL = "no_canonical"    # movies: id known, but no existing row holds it
AMBIGUOUS = "ambiguous"          # signal hit a key claimed by >1 show
VARIANT = "variant"
DENIED = "denied"
PROTECTED = "protected"          # a destructive core merge was prevented
PRESERVED = "preserved"          # detail a core refresh dropped was put back
PRUNE_BLOCKED = "prune_blocked"  # a catalog-wide delete after an empty listing

# Values providers use to mean "no id" (mirrors the core cleaning in tasks.py).
_BLANK_IDS = ("", "0", "none", "null")


# --------------------------------------------------------------------------- #
# Module state
# --------------------------------------------------------------------------- #

_ACTIVE = False
_orig_process_series_batch = None
_orig_handle_movie_id_conflicts = None
_orig_process_movie_batch = None
_orig_refresh_movie_advanced_data = None
_orig_cleanup_orphaned_vod_content = None
_PATCH_TAG = "_vodmerge_patched"

_pid_logged = set()

_cfg_lock = threading.Lock()
_cfg_cache = None
_cfg_cache_ts = 0.0

_ix_lock = threading.Lock()
_ix_cache = None                 # series index bundle
_ix_cache_ts = 0.0

_mix_lock = threading.Lock()
_mix_cache = None                # movie index bundle
_mix_cache_ts = 0.0


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

_extra_logged = set()


def _note_unexpected_args(where: str, args, kwargs) -> None:
    """Report, once per shape, that core passed something this version predates.

    Every wrapper here ends `*args, **kwargs` and forwards them, so a new
    upstream parameter cannot break argument binding. That matters more than it
    sounds: a signature mismatch raises BEFORE the function body, so it lands
    ahead of any `_ACTIVE` check and ahead of our own `try/except` -- it is the
    one upstream change that turns "this plugin quietly stopped helping" into a
    hard failure. A sibling plugin took exactly that outage when a hooked
    function gained a keyword argument.

    But forwarding only buys us the crash. It does not make us CORRECT. If the
    new parameter carries meaning -- the release that caused that outage added a
    per-user permission allowlist -- then a wrapper that replaces core's work, or
    post-processes it, may now be silently dropping a constraint core was
    enforcing. Nothing useful can be decided automatically, so leave a trace
    instead of failing silently.

    Deduped by (site, arity, keyword names) because these are hot paths, and
    logged at WARNING so it survives a worker whose root logger sits above INFO.
    """
    try:
        if not args and not kwargs:
            return
        key = (where, len(args), tuple(sorted(kwargs)))
        if key in _extra_logged:
            return
        _extra_logged.add(key)
        logger.warning(
            "[VOD-MERGE] %s got arguments this plugin version does not know "
            "about (%d positional, keywords=%s). They were forwarded to core "
            "unchanged. If they carry meaning this wrapper may need to HONOR "
            "them -- check the Dispatcharr release notes.",
            where, len(args), sorted(kwargs) or "none",
        )
    except Exception:
        pass


def _log_pid_once(where: str) -> None:
    """Log once per (pid, where) so you can confirm the patch reached BOTH the
    uWSGI workers and the Celery prefork children that actually run the scan."""
    key = (os.getpid(), where)
    if key in _pid_logged:
        return
    _pid_logged.add(key)
    logger.info("[VOD-MERGE] %s in pid=%s", where, os.getpid())


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in ("false", "0", "no", "off", "")


def _as_int(value, default):
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out >= 0 else default


def _sweep_hour(value, default=None):
    """Hour of day for a nightly timer; anything invalid falls back to default."""
    default = DEFAULT_SWEEP_HOUR if default is None else default
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return default
    return hour if 0 <= hour <= 23 else default


def _enrich_minutes(value):
    """Nightly time limit. No "0 means unlimited" here: an unbounded night runs
    into the provider refreshes and anything else scheduled after it."""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ENRICH_MINUTES
    return minutes if 1 <= minutes <= MAX_ENRICH_MINUTES else DEFAULT_ENRICH_MINUTES


def _csv_set(value):
    """Parse a comma/newline separated setting into a set of trimmed strings."""
    if not value:
        return set()
    parts = re.split(r"[,\n]", str(value))
    return {p.strip() for p in parts if p and p.strip()}


def parse_approvals(value):
    """Parse manual approvals: `Account:external_series_id=tmdb`, one per line or
    comma separated. Returns {(account, external_id_str): tmdb_str}."""
    out = {}
    for raw in re.split(r"[,\n]", str(value or "")):
        item = raw.strip()
        if not item or "=" not in item or ":" not in item.split("=", 1)[0]:
            continue
        left, tmdb = item.split("=", 1)
        account, ext = left.rsplit(":", 1)
        account, ext, tmdb = account.strip(), ext.strip(), tmdb.strip()
        if account and ext and tmdb:
            out[(account, ext)] = tmdb
    return out


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def _read_plugin_settings() -> dict:
    from apps.plugins.models import PluginConfig
    row = PluginConfig.objects.filter(key=PLUGIN_KEY).values("settings").first()
    if not row:
        return {}
    return row.get("settings") or {}


def _compile_variant(pattern):
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.I)
    except re.error as exc:
        logger.error(
            "[VOD-MERGE] invalid variant pattern %r (%s); falling back to default",
            pattern, exc,
        )
        try:
            return re.compile(DEFAULT_VARIANT_PATTERN, re.I)
        except re.error:
            return None


def _load_config(force: bool = False) -> dict:
    global _cfg_cache, _cfg_cache_ts
    now = time.time()
    with _cfg_lock:
        if not force and _cfg_cache is not None and (now - _cfg_cache_ts) < CONFIG_TTL_SECONDS:
            return _cfg_cache
    try:
        settings = _read_plugin_settings()
    except Exception as exc:
        logger.debug("[VOD-MERGE] settings read failed (%s); using defaults", exc)
        settings = {}

    cfg = {
        "dry_run": _as_bool(settings.get("dry_run"), DEFAULT_DRY_RUN),
        # Empty = every account. Populate it to bound the blast radius to the one
        # provider you actually mean to merge. Series and movies are scoped
        # SEPARATELY: the two domains roll out independently, and a shared list
        # would mean enabling a provider for one silently enabled it for the
        # other.
        "accounts": _csv_set(settings.get("allowed_accounts")),
        "movie_accounts": _csv_set(settings.get("movie_accounts")),
        "variant_re": _compile_variant(
            settings.get("variant_pattern") or DEFAULT_VARIANT_PATTERN
        ),
        "denylist": _csv_set(settings.get("denylist")),
        "approvals": parse_approvals(settings.get("approved_matches")),
        "index_ttl": _as_int(settings.get("index_ttl_seconds"), DEFAULT_INDEX_TTL_SECONDS),
        "sweep_limit": _as_int(settings.get("sweep_limit"), DEFAULT_SWEEP_LIMIT),
        "sweep_delay_ms": _as_int(settings.get("sweep_delay_ms"), DEFAULT_SWEEP_DELAY_MS),
        # NOT gated by dry_run on purpose: dry_run means "do not inject", and
        # letting it disable a PROTECTIVE patch would make dry run permit the
        # very data loss this exists to stop.
        "protect_merges": _as_bool(
            settings.get("protect_merges"), DEFAULT_PROTECT_MERGES),
        # Same reasoning: this one prevents data LOSS too, so dry_run must not
        # be able to switch it off either.
        "preserve_detail": _as_bool(
            settings.get("preserve_detail"), DEFAULT_PRESERVE_DETAIL),
        # And the same again -- this is the most destructive of the three, so it
        # is the last one that should be switchable by a flag meaning "do not
        # inject".
        "protect_prune": _as_bool(
            settings.get("protect_prune"), DEFAULT_PROTECT_PRUNE),
        "merge_series": _as_bool(settings.get("merge_series"), DEFAULT_MERGE_SERIES),
        "merge_movies": _as_bool(settings.get("merge_movies"), DEFAULT_MERGE_MOVIES),
        "scheduled_sweep": _as_bool(
            settings.get("scheduled_sweep"), DEFAULT_SCHEDULED_SWEEP),
        "sweep_hour": _sweep_hour(settings.get("sweep_hour")),
        "schedule_queue": (str(settings.get("schedule_queue") or "").strip()
                           or DEFAULT_SCHEDULE_QUEUE),
        "tag_unique_movies": _as_bool(
            settings.get("tag_unique_movies"), DEFAULT_TAG_UNIQUE_MOVIES),
        "wanted_set_path": (str(settings.get("wanted_set_path") or "").strip()
                            or DEFAULT_WANTED_SET_PATH),
        "enrich_movies": _as_bool(
            settings.get("enrich_movies"), DEFAULT_ENRICH_MOVIES),
        "enrich_nightly": _as_bool(
            settings.get("enrich_nightly"), DEFAULT_ENRICH_NIGHTLY),
        "enrich_hour": _sweep_hour(settings.get("enrich_hour"),
                                   DEFAULT_ENRICH_HOUR),
        "enrich_minutes": _enrich_minutes(settings.get("enrich_minutes")),
        "enrich_limit": _as_int(settings.get("enrich_limit"), DEFAULT_ENRICH_LIMIT),
        "enrich_delay_ms": _as_int(
            settings.get("enrich_delay_ms"), DEFAULT_ENRICH_DELAY_MS),
        "probe_movies": _as_bool(
            settings.get("probe_movies"), DEFAULT_PROBE_MOVIES),
        "probe_limit": _as_int(settings.get("probe_limit"), DEFAULT_PROBE_LIMIT),
        "probe_4k_dv": _as_bool(settings.get("probe_4k_dv"), DEFAULT_PROBE_4K_DV),
        "probe_delay_ms": _as_int(
            settings.get("probe_delay_ms"), DEFAULT_PROBE_DELAY_MS),
    }
    with _cfg_lock:
        _cfg_cache = cfg
        _cfg_cache_ts = time.time()
    return cfg


def invalidate_config_cache() -> None:
    global _cfg_cache, _cfg_cache_ts
    with _cfg_lock:
        _cfg_cache = None
        _cfg_cache_ts = 0.0


def is_enabled() -> bool:
    """Whether this plugin's PluginConfig row is enabled.

    Missing row -> False (inert until Dispatcharr registers the plugin); a failed
    read -> True, so a transient DB hiccup does not silently stop merging.
    """
    try:
        from apps.plugins.models import PluginConfig
        row = PluginConfig.objects.filter(key=PLUGIN_KEY).values("enabled").first()
        return bool(row and row.get("enabled"))
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# Signal extraction + indexes (pure functions -- see test_logic.py)
# --------------------------------------------------------------------------- #

def _poster_key(url):
    """Size-invariant TMDB asset basename, or None if `url` is not usable.

    Requires a real image filename: a cover URL that ends at the size segment
    (".../t/p/w600_and_h900_bestv2") would otherwise yield the SIZE as the key
    and collide across every title that used that size.
    """
    if not url or not isinstance(url, str):
        return None
    if not any(hint in url for hint in _TMDB_URL_HINTS):
        return None
    seg = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].strip().lower()
    if not seg or not _IMAGE_EXT_RE.search(seg):
        return None
    return seg


def _plot_hash(text):
    """Normalized full plot text, or None when too short to be distinctive."""
    if not text or not isinstance(text, str):
        return None
    norm = _NONALNUM_RE.sub(" ", text.lower()).strip()
    return norm if len(norm) >= MIN_PLOT_CHARS else None


def _plot_digest(plot_hash):
    """Short stable digest, so the audit log records which plot matched without
    storing a paragraph per entry."""
    if not plot_hash:
        return None
    return "plot:" + hashlib.sha1(plot_hash.encode("utf-8")).hexdigest()[:10]


def entry_poster_key(entry, fields=POSTER_FIELDS):
    """First usable poster key from a listing payload dict."""
    for field in fields:
        key = _poster_key((entry or {}).get(field))
        if key:
            return key
    return None


def entry_plot_hash(entry, fields=PLOT_FIELDS):
    """First usable plot hash from a listing payload dict."""
    for field in fields:
        got = _plot_hash((entry or {}).get(field))
        if got:
            return got
    return None


def _add(index, ambiguous, key, tmdb_id):
    """Insert key->tmdb, demoting the key to `ambiguous` if two shows claim it."""
    if not key or key in ambiguous:
        return
    known = index.get(key)
    if known is None:
        index[key] = tmdb_id
    elif known != tmdb_id:
        ambiguous.add(key)
        index.pop(key, None)


def build_indexes_from_rows(rows, poster_fields=POSTER_FIELDS,
                            plot_fields=PLOT_FIELDS):
    """Reduce (custom_properties, tmdb_id, series_description) rows into a bundle.

    Both indexes contain ONLY keys that resolve to exactly one tmdb id; the
    rejected ones are collected for reporting. Plot text is taken from BOTH the
    relation's stored listing payload and the Series row, because a canonical's
    description may have been written by a different provider than the one whose
    listing we are matching against.
    """
    poster, poster_amb = {}, set()
    plot, plot_amb = {}, set()
    for custom_properties, tmdb_id, description in rows:
        if not tmdb_id:
            continue
        tmdb_id = str(tmdb_id)
        basic = ((custom_properties or {}).get("basic_data") or {})
        if not isinstance(basic, dict):
            basic = {}
        for field in poster_fields:
            _add(poster, poster_amb, _poster_key(basic.get(field)), tmdb_id)
        for text in ([basic.get(f) for f in plot_fields] + [description]):
            _add(plot, plot_amb, _plot_hash(text), tmdb_id)
    return {
        "poster": poster, "poster_amb": poster_amb,
        "plot": plot, "plot_amb": plot_amb,
    }


def _build_indexes():
    from apps.vod.models import M3USeriesRelation

    qs = (
        M3USeriesRelation.objects
        .exclude(series__tmdb_id=None)
        .exclude(series__tmdb_id="")
        .values_list("custom_properties", "series__tmdb_id", "series__description")
    )
    return build_indexes_from_rows(qs.iterator(chunk_size=2000))


def _get_indexes(force: bool = False):
    global _ix_cache, _ix_cache_ts
    cfg = _load_config()
    now = time.time()
    with _ix_lock:
        if (
            not force
            and _ix_cache is not None
            and (now - _ix_cache_ts) < max(1, cfg["index_ttl"])
        ):
            return _ix_cache

    idx = _build_indexes()
    if not idx["poster"] and not idx["plot"]:
        # Distinguish "nothing matched today" from "the plugin silently stopped
        # working" -- empty indexes mean no merge can ever happen.
        logger.warning(
            "[VOD-MERGE] indexes came back EMPTY -- no tagged series with usable "
            "TMDB artwork or plot text found. No merging will occur."
        )
    else:
        logger.info(
            "[VOD-MERGE] indexes built: poster=%d (%d ambiguous), plot=%d (%d ambiguous)",
            len(idx["poster"]), len(idx["poster_amb"]),
            len(idx["plot"]), len(idx["plot_amb"]),
        )
    with _ix_lock:
        _ix_cache = idx
        _ix_cache_ts = time.time()
    return _ix_cache


def _build_movie_indexes():
    from apps.vod.models import M3UMovieRelation

    qs = (
        M3UMovieRelation.objects
        .exclude(movie__tmdb_id=None)
        .exclude(movie__tmdb_id="")
        .values_list("custom_properties", "movie__tmdb_id", "movie__description")
    )
    return build_indexes_from_rows(
        qs.iterator(chunk_size=2000),
        poster_fields=MOVIE_POSTER_FIELDS,
        plot_fields=MOVIE_PLOT_FIELDS,
    )


def _get_movie_indexes(force: bool = False):
    """Same shape and guard as the series index, over movie relations.

    Movies need this because a provider can be strong on one signal and absent
    on     the other: measured live, one provider's movie listings almost never carried
    TMDB artwork while another's carried it on nearly every entry, and the
    detail id runs the other way. Relying on any single signal leaves one of
    them unreachable.
    """
    global _mix_cache, _mix_cache_ts
    cfg = _load_config()
    now = time.time()
    with _mix_lock:
        if (
            not force
            and _mix_cache is not None
            and (now - _mix_cache_ts) < max(1, cfg["index_ttl"])
        ):
            return _mix_cache

    idx = _build_movie_indexes()
    logger.info(
        "[VOD-MERGE] movie indexes built: poster=%d (%d ambiguous), plot=%d (%d ambiguous)",
        len(idx["poster"]), len(idx["poster_amb"]),
        len(idx["plot"]), len(idx["plot_amb"]),
    )
    with _mix_lock:
        _mix_cache = idx
        _mix_cache_ts = time.time()
    return _mix_cache


def invalidate_index_cache() -> None:
    global _ix_cache, _ix_cache_ts, _mix_cache, _mix_cache_ts
    with _ix_lock:
        _ix_cache = None
        _ix_cache_ts = 0.0
    with _mix_lock:
        _mix_cache = None
        _mix_cache_ts = 0.0


# --------------------------------------------------------------------------- #
# Decision logic (pure -- see test_logic.py)
# --------------------------------------------------------------------------- #

def category_enabled(entry, categories, relations):
    """Whether core will actually keep this entry.

    A scan fetches the provider's ENTIRE listing, not just the categories you
    enabled, and discards the disabled ones itself -- after our wrapper has
    already seen them. Without this check we inject into (and log) thousands of
    entries that are then thrown away: on one library, 5,000 logged merges
    against 438 real relations.

    Mirrors the check in process_series_batch / process_movie_batch. Fails open:
    anything we cannot resolve is treated as enabled, so a shape we do not
    recognize costs a wasted lookup rather than a missed merge.
    """
    try:
        raw = (entry or {}).get("category_id")
        provider_cat_id = str(raw) if raw not in (None, "") else None
        category = None
        if provider_cat_id is not None and provider_cat_id in (categories or {}):
            category = categories[provider_cat_id]
        elif categories:
            category = categories.get("__uncategorized__")
        if category is None:
            return True
        relation = (relations or {}).get(getattr(category, "id", None))
        if relation is not None and not getattr(relation, "enabled", True):
            return False
        return True
    except Exception:
        return True


def existing_id(entry):
    """The entry's own tmdb/imdb id, applying core's blank-value cleaning."""
    for field in ("tmdb", "tmdb_id", "imdb", "imdb_id"):
        raw = (entry or {}).get(field)
        if raw is None:
            continue
        if str(raw).strip().lower() in _BLANK_IDS:
            continue
        return str(raw).strip()
    return None


def is_denied(denylist, account_name, external_id, tmdb_id):
    """Denylist entries are either `tmdb:<id>` (never merge INTO this show) or
    `<account>:<external_series_id>` (never merge THIS provider entry)."""
    if not denylist:
        return False
    if tmdb_id and f"tmdb:{tmdb_id}" in denylist:
        return True
    if external_id is not None and f"{account_name}:{external_id}" in denylist:
        return True
    return False


def decide(entry, account_name, external_id, idx, cfg):
    """Classify one listing entry. Returns (action, tmdb_id, tier, key).

    Pure: no DB, no mutation. Precedence is manual approval > poster > plot, on
    the principle that an explicit human decision outranks a derived one and the
    stronger signal outranks the weaker. The denylist overrides everything; the
    variant guard applies to derived matches but is bypassed by a manual
    approval, since approving a variant entry by name IS the override.
    """
    if existing_id(entry):
        return HAS_ID, None, None, None

    tmdb_id = tier = key = None
    saw_ambiguous = False

    approved = (cfg.get("approvals") or {}).get((account_name, str(external_id)))
    if approved:
        tmdb_id, tier = approved, TIER_MANUAL
    else:
        poster_key = entry_poster_key(entry)
        plot_hash = entry_plot_hash(entry)

        if poster_key:
            if poster_key in (idx.get("poster_amb") or ()):
                saw_ambiguous = True
            elif idx.get("poster", {}).get(poster_key):
                tmdb_id, tier, key = idx["poster"][poster_key], TIER_POSTER, poster_key

        if tmdb_id is None and plot_hash:
            if plot_hash in (idx.get("plot_amb") or ()):
                saw_ambiguous = True
            elif idx.get("plot", {}).get(plot_hash):
                tmdb_id, tier = idx["plot"][plot_hash], TIER_PLOT
                key = _plot_digest(plot_hash)

        if tmdb_id is None:
            if not poster_key and not plot_hash:
                return NO_SIGNAL, None, None, None
            return (AMBIGUOUS if saw_ambiguous else NO_MATCH), None, None, poster_key

    if is_denied(cfg.get("denylist"), account_name, external_id, tmdb_id):
        return DENIED, tmdb_id, tier, key

    if tier != TIER_MANUAL:
        variant_re = cfg.get("variant_re")
        if variant_re is not None and variant_re.search(str(entry.get("name") or "")):
            return VARIANT, tmdb_id, tier, key

    return (DRY_RUN if cfg.get("dry_run") else INJECT), tmdb_id, tier, key


# --------------------------------------------------------------------------- #
# Audit trail (dedicated CoreSettings row)
# --------------------------------------------------------------------------- #
# Shape: {"entries": [ {...}, ... ], "last_run": <epoch>|None}

def _empty_log() -> dict:
    return {"entries": [], "last_run": None}


def get_log() -> dict:
    try:
        from core.models import CoreSettings
        row = CoreSettings.objects.filter(key=LOG_CORE_KEY).values("value").first()
    except Exception:
        return _empty_log()
    if not row:
        return _empty_log()
    val = row.get("value") or {}
    if not isinstance(val, dict):
        return _empty_log()
    entries = val.get("entries")
    if not isinstance(entries, list):
        entries = []
    return {"entries": entries, "last_run": val.get("last_run")}


def _append_log(new_entries) -> None:
    """Append audit entries, newest last, capped. One write per batch."""
    if not new_entries:
        return
    from django.db import transaction
    from core.models import CoreSettings

    try:
        with transaction.atomic():
            row = (
                CoreSettings.objects.select_for_update()
                .filter(key=LOG_CORE_KEY).first()
            )
            if row is None:
                data = _empty_log()
                data["entries"] = list(new_entries)[-MAX_LOG_ENTRIES:]
                data["last_run"] = time.time()
                CoreSettings.objects.create(
                    key=LOG_CORE_KEY, name=LOG_CORE_NAME, value=data,
                )
                return
            data = row.value if isinstance(row.value, dict) else _empty_log()
            entries = data.get("entries")
            if not isinstance(entries, list):
                entries = []
            entries.extend(new_entries)
            data["entries"] = entries[-MAX_LOG_ENTRIES:]
            data["last_run"] = time.time()
            row.value = data
            row.save(update_fields=["value"])
    except Exception:
        # The audit trail is valuable but never worth failing an ingest over.
        logger.exception("[VOD-MERGE] could not persist injection log")


def clear_log() -> int:
    data = get_log()
    count = len(data.get("entries") or [])
    try:
        from core.models import CoreSettings
        CoreSettings.objects.filter(key=LOG_CORE_KEY).delete()
    except Exception:
        logger.exception("[VOD-MERGE] could not clear injection log")
        return 0
    return count


# --------------------------------------------------------------------------- #
# Injection
# --------------------------------------------------------------------------- #

def inject_batch(account, batch, categories=None, relations=None) -> dict:
    """Mutate `batch` entries in place, adding a `tmdb` where a signal matches.

    Returns a small tally for logging. Never raises to the caller.
    """
    tally = {}
    if not batch:
        return tally

    cfg = _load_config()
    account_name = getattr(account, "name", "?")

    if not cfg["merge_series"]:
        return tally

    if cfg["accounts"] and account_name not in cfg["accounts"]:
        return tally

    # Series listings only ever come from XC accounts; skip anything else rather
    # than guessing at another provider type's payload shape.
    acct_type = getattr(account, "account_type", None)
    if acct_type is not None and acct_type != "XC":
        return tally

    idx = _get_indexes()
    if not idx["poster"] and not idx["plot"]:
        return tally

    decisions = []
    for entry in batch:
        if not isinstance(entry, dict):
            continue
        if not category_enabled(entry, categories, relations):
            tally["disabled_category"] = tally.get("disabled_category", 0) + 1
            continue
        external_id = entry.get("series_id") or entry.get("external_series_id")
        action, tmdb_id, tier, key = decide(entry, account_name, external_id, idx, cfg)
        tally[action] = tally.get(action, 0) + 1

        if action == INJECT:
            entry["tmdb"] = tmdb_id
        elif action not in (DRY_RUN, VARIANT, DENIED):
            # has_id / no_signal / no_match / ambiguous are the uninteresting
            # majority -- counted, not logged per item.
            continue
        decisions.append(Decision(entry, external_id, action, tmdb_id, tier, key))

    if not decisions:
        return tally

    canon = _canonical_series_map({d.tmdb_id for d in decisions if d.tmdb_id})
    current = _series_relation_map(account, [d.external_id for d in decisions])

    audit = []
    for entry, external_id, action, tmdb_id, tier, key in decisions:  # namedtuple unpacks
        now_on = current.get(str(external_id))
        if not is_new_merge(action, tmdb_id, canon, now_on):
            tally["unchanged"] = tally.get("unchanged", 0) + 1
            continue
        target = canon.get(tmdb_id) or {}
        audit.append({
            "ts": time.time(),
            "action": action,
            "tier": tier,
            "kind": "series",
            "account": account_name,
            "external_series_id": str(external_id) if external_id is not None else None,
            "name": entry.get("name"),
            "tmdb_id": tmdb_id,
            "key": key,
            "previous_series_id": now_on,
            "canonical_id": target.get("id"),
            "canonical_uuid": str(target["uuid"]) if target.get("uuid") else None,
        })
        logger.info(
            "[VOD-MERGE] %s (%s): %r (%s:%s) -> tmdb=%s via %s",
            action, tier, entry.get("name"), account_name, external_id, tmdb_id, key,
        )

    _append_log(audit)
    return tally


def patched_process_series_batch(account, batch, categories, relations,
                                 scan_start_time=None, *args, **kwargs):
    """Wrapper: inject before the original computes the merge key.

    The original is called unconditionally, and injection is fully guarded -- a
    fault in this plugin must degrade to "no merging", never to a failed scan.

    Signature-agnostic: anything core grows is accepted and forwarded rather
    than raising during argument binding. Core still runs, so a new parameter is
    honored by core itself; the only risk is one that changes how core
    INTERPRETS the batch we just mutated, which `_note_unexpected_args` surfaces.
    """
    _log_pid_once("series-batch wrapper active")
    _note_unexpected_args("process_series_batch", args, kwargs)
    try:
        if is_enabled():
            tally = inject_batch(account, batch, categories, relations)
            if tally.get(INJECT) or tally.get(DRY_RUN):
                logger.info(
                    "[VOD-MERGE] batch for %r: %s",
                    getattr(account, "name", "?"),
                    ", ".join(f"{k}={v}" for k, v in sorted(tally.items())),
                )
    except Exception:
        logger.exception("[VOD-MERGE] injection failed; continuing unpatched")

    return _orig_process_series_batch(
        account, batch, categories, relations, scan_start_time, *args, **kwargs
    )


# --------------------------------------------------------------------------- #
# Preview (on-demand, no scan required)
# --------------------------------------------------------------------------- #

def preview_impl(limit: int = 0) -> dict:
    """Report what WOULD be injected, using each id-less series relation's stored
    `basic_data` -- the same decision path the wrapper uses, so preview matches
    reality. Read-only; makes no provider calls.

    `limit` 0 means no cap, which is what the caller wants: the report is written
    to a file the UI message points at, so truncating here would quietly make
    "full list" a lie. The API response is bounded separately by `trim_matches`.
    """
    from apps.vod.models import Series

    cfg = _load_config(force=True)
    idx = _get_indexes(force=True)

    tally = {}
    tiers = {}
    matches = []
    qs = (
        Series.objects.filter(tmdb_id__isnull=True, imdb_id__isnull=True)
        .prefetch_related("m3u_relations__m3u_account")
    )
    # chunk_size is REQUIRED (Django >=4.1) when iterator() follows
    # prefetch_related(); without it Django raises rather than silently dropping
    # the prefetch. Chunking keeps memory flat on a large id-less population.
    for series in qs.iterator(chunk_size=1000):
        for rel in series.m3u_relations.all():
            account_name = getattr(rel.m3u_account, "name", "?")
            if cfg["accounts"] and account_name not in cfg["accounts"]:
                continue
            basic = ((rel.custom_properties or {}).get("basic_data") or {})
            # The stored listing may predate a description written by a detail
            # fetch; fall back to the Series row so preview sees what the live
            # payload would carry.
            if not entry_plot_hash(basic) and series.description:
                basic = dict(basic, plot=series.description)
            action, tmdb_id, tier, key = decide(
                basic, account_name, rel.external_series_id, idx, cfg
            )
            tally[action] = tally.get(action, 0) + 1
            if tier:
                tiers[tier] = tiers.get(tier, 0) + 1
            if action in (INJECT, DRY_RUN, VARIANT, DENIED) and (
                    not limit or len(matches) < limit):
                matches.append({
                    "series_id": series.id,
                    "series_name": series.name,
                    "account": account_name,
                    "external_series_id": rel.external_series_id,
                    "action": action,
                    "tier": tier,
                    "tmdb_id": tmdb_id,
                    "key": key,
                })

    return {
        "poster_index": len(idx["poster"]),
        "poster_ambiguous": len(idx["poster_amb"]),
        "plot_index": len(idx["plot"]),
        "plot_ambiguous": len(idx["plot_amb"]),
        "tally": tally,
        "tiers": tiers,
        "matches": matches,
        "dry_run": cfg["dry_run"],
        "accounts": sorted(cfg["accounts"]) or ["<all>"],
        "approvals": len(cfg["approvals"]),
    }


# --------------------------------------------------------------------------- #
# Movie detail sweep
# --------------------------------------------------------------------------- #
# Movies need a DIFFERENT signal than series, and the reason is structural:
#
#   * A series LISTING carries `cover` and `plot`, so both matching signals are
#     available for free at scan time.
#   * A movie LISTING carries neither usefully. Measured on a real library:
#     of 3,009 id-less movie relations only 8% had TMDB artwork and 0% had a
#     usable plot, and the whole tagged-movie plot index held 5 keys -- roughly
#     the number of relations that had ever been advance-refreshed. Movie
#     descriptions only ever arrive via `get_vod_info`.
#
# But movies have something series lack: their DETAIL payload carries
# `info.tmdb_id` outright, so no matching is needed once the detail exists. This
# sweep is the bootstrap that fetches it, one call per relation, storing the
# result on the relation where a later scan can read it.
#
# IT DELIBERATELY DOES NOT CALL `refresh_movie_advanced_data`. That core task
# routes through `handle_movie_id_conflicts`, whose "preserve the user's
# selection" policy DELETES the canonical Movie row and keeps the id-less orphan
# -- and it fires precisely when the detail's tmdb is held by another row, which
# is every candidate here. Running it across a few thousand relations would
# rewrite the library. This writes `detailed_info` itself instead.

# --------------------------------------------------------------------------- #
# Our own corner of `custom_properties`
# --------------------------------------------------------------------------- #
# Until 1.2.0 the sweep stamped core's `detailed_fetched` + `last_advanced_refresh`
# to record that it had fetched a relation. That was a mistake, and not merely an
# untidy one.
#
# Those two fields are the ONLY record of "a client asked Dispatcharr for this
# movie's detail". Nothing else in core carries that signal, and it is the signal
# a future enrichment pass needs in order to know which titles anyone actually
# wants. Writing them ourselves poisons it at the source: every relation we swept
# looks like a relation someone requested.
#
# It also suppressed real work. `refresh_movie_advanced_data` skips when
# `detailed_fetched` is set and `last_advanced_refresh` is inside 24h, and the
# sweep set both WITHOUT calling that function -- so a client asking an hour
# after a sweep got core's early return instead of a fetch. Harmless only while
# our sweep happens to fetch the same endpoint and store the same payload; it
# stops being harmless the moment either side changes.
#
# The general rule, which is why this is a named section rather than a one-line
# fix: A SWEEP MUST NOT WRITE THE FIELD IT READS. Our movie sweep can obey it
# because its mechanism (a detail fetch we perform, and later an ffprobe) is
# distinct from the mechanism that records the signal (core's own refresh). The
# series case cannot -- there the sweep's action IS the function that writes the
# field -- which is exactly why the episode sweep needed an explicit watchlist
# and a hand-rolled TTL. Here the timestamp is inherently a TTL, for free, and
# only for as long as we keep our hands off it.
#
# So: we write ONE key, namespaced, and never touch core's. `process_movie_batch`
# merges relation `custom_properties` rather than replacing it
# (`{**existing_rel_cp, 'basic_data': ...}`, verified in v0.30.0), so this
# survives every scan by the same mechanism that keeps `detailed_info` alive.

OWN_PROPS_KEY = "vod_merge"


def _stamp_own(props, **fields):
    """Record our own bookkeeping under `OWN_PROPS_KEY`, merging with whatever
    is already there so separate passes (detail now, ffprobe later) accumulate
    rather than overwrite. Mutates and returns `props`."""
    existing = props.get(OWN_PROPS_KEY)
    own = dict(existing) if isinstance(existing, dict) else {}
    own.update(fields)
    props[OWN_PROPS_KEY] = own
    return props


def _clean_props(value):
    """Use core's cleaner when available so the stored shape matches what core
    would have written; fall back to storing the payload as-is."""
    if not value:
        return None
    try:
        from apps.vod.tasks import clean_custom_properties
        return clean_custom_properties(value)
    except Exception:
        return value


def detail_tmdb(relation_props):
    """The tmdb id stored on a movie relation by a previous detail fetch."""
    detail = ((relation_props or {}).get("detailed_info") or {})
    if not isinstance(detail, dict):
        return None
    raw = detail.get("tmdb_id")
    if raw is None or str(raw).strip().lower() in _BLANK_IDS:
        return None
    return str(raw).strip()


def free_signal_match(basic, idx):
    """The tmdb a no-cost tier would reach for this listing, or None.

    Used to keep the sweep off relations that need no provider call at all.
    Ambiguous keys count as no match, exactly as in `decide_movie`.
    """
    key = entry_poster_key(basic, MOVIE_POSTER_FIELDS)
    if key and key not in (idx.get("poster_amb") or ()):
        got = (idx.get("poster") or {}).get(key)
        if got:
            return got
    plot = entry_plot_hash(basic, MOVIE_PLOT_FIELDS)
    if plot and plot not in (idx.get("plot_amb") or ()):
        got = (idx.get("plot") or {}).get(plot)
        if got:
            return got
    return None


def movie_sweep_candidates(cfg=None):
    """Id-less movie relations that have no stored detail yet."""
    from apps.vod.models import M3UMovieRelation

    cfg = cfg or _load_config()
    qs = (
        M3UMovieRelation.objects
        .filter(movie__tmdb_id=None, movie__imdb_id=None)
        .select_related("m3u_account", "movie")
    )
    if cfg["movie_accounts"]:
        qs = qs.filter(m3u_account__name__in=cfg["movie_accounts"])
    return qs


def movie_sweep_status() -> dict:
    """Sweep progress AND what the wrapper would actually do right now.

    Reporting only the detail tier would be actively misleading: a provider like
    a provider can carry no ids at all but TMDB artwork on nearly every entry, so the
    poster tier can have hundreds of merges queued up while the detail tier
    shows zero.
    """
    from apps.vod.models import M3UMovieRelation

    cfg = _load_config(force=True)
    idx = _get_movie_indexes(force=True)

    # The account name is carried through so the projection can see manual
    # approvals, which are keyed on (account, stream_id).
    rows = list(
        movie_sweep_candidates(cfg)
        .values_list("custom_properties", "stream_id", "movie__name",
                     "m3u_account__name")
    )

    pending = with_detail = 0
    found = []
    for props, _sid, _name, _acct in rows:
        props = props or {}
        if props.get("detailed_info"):
            with_detail += 1
            got = detail_tmdb(props)
            if got:
                found.append(got)
        else:
            pending += 1

    # One canonical lookup for every id any tier might land on.
    wanted = set(found) | set((idx.get("poster") or {}).values())         | set((idx.get("plot") or {}).values())
    canon = _canonical_map(wanted)

    # `matches` mirrors what preview_impl reports for series: the actionable
    # entries, so the UI can show a summary and the caller can dump the rest to
    # a file rather than burying the counts under hundreds of lines.
    tally, tiers, matches = {}, {}, []
    for props, sid, name, acct in rows:
        props = props or {}
        basic = dict((props.get("basic_data") or {}))
        basic.setdefault("name", name)
        got = detail_tmdb(props)
        detail_map = {str(sid): (got, None)} if got else {}
        action, tmdb_id, tier, _ = decide_movie(
            basic, acct, sid, detail_map, canon, idx, cfg
        )
        tally[action] = tally.get(action, 0) + 1
        if tier and action in (INJECT, DRY_RUN):
            tiers[tier] = tiers.get(tier, 0) + 1
        if action in (INJECT, DRY_RUN, VARIANT, DENIED):
            target = canon.get(tmdb_id) or {}
            matches.append({
                "movie_name": basic.get("name") or name,
                "account": acct,
                "stream_id": str(sid) if sid is not None else None,
                "action": action,
                "tier": tier,
                "tmdb_id": tmdb_id,
                "canonical_id": target.get("id"),
                # No row holds this id yet, so injecting mints a newly tagged
                # movie instead of folding into one. Worth surfacing.
                "creates_new_row": not bool(target),
            })

    merges = sum(1 for t in found if t in canon)
    return {
        "matches": matches,
        "pending": pending,
        "with_detail": with_detail,
        "with_detail_tmdb": len(found),
        "would_merge": merges,
        "would_create": len(found) - merges,
        "projected": tally,
        "projected_by_tier": tiers,
        "merge_movies": cfg["merge_movies"],
        "dry_run": cfg["dry_run"],
        "accounts": sorted(cfg["movie_accounts"]) or ["<all>"],
    }


def sweep_movies_impl(limit=None, delay=None, refetch=False) -> dict:
    """Fetch `get_vod_info` for id-less movie relations and store the detail.

    Read-only against Dispatcharr's merge logic: it writes `detailed_info` /
    `movie_data` and our OWN namespaced key onto the relation and nothing else.
    No Movie row is created, deleted, renamed or repointed here, and since
    1.2.0 no field of core's is written either -- see `OWN_PROPS_KEY`.

    Resumable -- it always picks up relations that still have no detail, so
    running it repeatedly walks the backlog. Resumability keys on
    `detailed_info`, never on `detailed_fetched`, which is why dropping the
    latter costs nothing here. Returns a summary.
    """
    from django.utils import timezone
    from core.xtream_codes import Client as XtreamCodesClient

    cfg = _load_config(force=True)
    limit = cfg["sweep_limit"] if limit is None else limit
    delay = (cfg["sweep_delay_ms"] if delay is None else delay) / 1000.0

    # NEWEST FIRST, and this matters a lot. Measured on a real library: the
    # newest id-less relations returned a tmdb nearly every time, while the
    # oldest almost never did -- the old bulk import is untagged long-tail content
    # (foreign-language titles, public-domain shorts) that no one has ever
    # tagged, whereas recent arrivals are well-described AND are the ones
    # duplicating titles already in the library. Sweeping oldest-first spends
    # thousands of provider calls on the least useful end of the list.
    # Skip anything a free signal already reaches. The poster tier costs nothing
    # and handles the large majority on some providers (measured live on a
    # poster-rich provider), so fetching those would be pure waste. Ordering usually
    # saves us -- the scan merges them before a nightly sweep sees them -- but
    # relying on the clock is fragile, so check it here instead.
    idx = _get_movie_indexes()

    qs = movie_sweep_candidates(cfg).order_by("-id")
    stats = {"fetched": 0, "with_tmdb": 0, "no_tmdb": 0, "empty": 0, "errors": 0,
             "skipped_free_match": 0}
    per_account = {}

    # Group by account so one HTTP client is reused per provider.
    by_account = {}
    for rel in qs.iterator(chunk_size=500):
        props = rel.custom_properties or {}
        if not refetch and props.get("detailed_info"):
            continue
        if free_signal_match(props.get("basic_data") or {}, idx):
            stats["skipped_free_match"] += 1
            continue
        by_account.setdefault(rel.m3u_account_id, []).append(rel)
        if limit and sum(len(v) for v in by_account.values()) >= limit:
            break

    for rels in by_account.values():
        account = rels[0].m3u_account
        name = account.name
        per_account.setdefault(name, {"fetched": 0, "with_tmdb": 0, "errors": 0})
        try:
            client = XtreamCodesClient(
                server_url=account.server_url,
                username=account.username,
                password=account.password,
                user_agent=account.get_user_agent_string(),
            )
        except Exception:
            logger.exception("[VOD-MERGE] could not build client for %r", name)
            stats["errors"] += len(rels)
            per_account[name]["errors"] += len(rels)
            continue

        with client:
            for rel in rels:
                try:
                    payload = client.get_vod_info(rel.stream_id)
                except Exception as exc:
                    stats["errors"] += 1
                    per_account[name]["errors"] += 1
                    logger.warning(
                        "[VOD-MERGE] detail fetch failed for %r rel=%s: %s",
                        name, rel.id, exc,
                    )
                    continue

                info = (payload or {}).get("info")
                if isinstance(info, list):
                    info = info[0] if info and isinstance(info[0], dict) else {}
                if not isinstance(info, dict) or not info:
                    stats["empty"] += 1
                    if delay:
                        time.sleep(delay)
                    continue

                movie_data = (payload or {}).get("movie_data")
                if isinstance(movie_data, list):
                    movie_data = movie_data[0] if movie_data and isinstance(movie_data[0], dict) else {}

                props = rel.custom_properties or {}
                cleaned = _clean_props(info)
                if cleaned:
                    props["detailed_info"] = cleaned
                cleaned_movie = _clean_props(movie_data) if isinstance(movie_data, dict) else None
                if cleaned_movie:
                    props["movie_data"] = cleaned_movie
                _stamp_own(props, detail_at=timezone.now().isoformat())
                rel.custom_properties = props
                try:
                    rel.save(update_fields=["custom_properties"])
                except Exception:
                    stats["errors"] += 1
                    per_account[name]["errors"] += 1
                    logger.exception("[VOD-MERGE] could not store detail for rel=%s", rel.id)
                    continue

                stats["fetched"] += 1
                per_account[name]["fetched"] += 1
                if detail_tmdb(props):
                    stats["with_tmdb"] += 1
                    per_account[name]["with_tmdb"] += 1
                else:
                    stats["no_tmdb"] += 1
                if delay:
                    time.sleep(delay)

    remaining = sum(
        1 for props in movie_sweep_candidates(cfg)
        .values_list("custom_properties", flat=True).iterator(chunk_size=2000)
        if not (props or {}).get("detailed_info")
    )
    stats["remaining"] = remaining
    stats["per_account"] = per_account
    logger.info("[VOD-MERGE] movie detail sweep: %s", stats)
    return stats


# --------------------------------------------------------------------------- #
# Movie injection
# --------------------------------------------------------------------------- #
# No matching and no index: the sweep above already stored the tmdb the provider
# put in its own detail payload, so this is a lookup. One query per batch maps
# stream_id -> (detail tmdb, current movie id); anything with a stored id and no
# listing id gets that id injected before the key is computed.
#
# Note the movie key reads `tmdb_id` BEFORE `tmdb` (tasks.py, process_movie_batch)
# -- the reverse of the series path -- so the injected key differs.

def decide_movie(entry, account_name, stream_id, detail_map, canonical, idx, cfg):
    """Classify one movie listing entry. Returns (action, tmdb, tier, key).

    Tiers in order of strength:

    0. MANUAL  -- an explicit human decision, keyed on (account, stream_id). It
       outranks everything derived, for the same reason it does for series: a
       person who typed a tmdb id has more context than any signal here. This is
       the only way to reach an entry a provider shipped with no metadata at all
       -- no artwork, no plot, and no id in its detail -- which is otherwise
       permanently unmergeable.
    1. DETAIL  -- the tmdb the provider put in its own `get_vod_info`. Its own
       assertion, so no inference at all, but it only exists once the sweep has
       fetched it and some providers omit it entirely.
    2. POSTER  -- shared TMDB artwork basename.
    3. PLOT    -- shared TMDB overview text. Near-inert for movies in practice
       (listings rarely carry a description at all), but free to keep and it
       costs nothing when a provider does supply one.

    Tiers 2 and 3 resolve against the tagged-movie index, so by construction
    they can only ever land on a row that already exists: they merge, and can
    never mint a new one. Only the detail tier can do that, which is why it is
    the only one gated by `tag_unique_movies`.

    A detail id with no canonical FALLS THROUGH to the weaker tiers rather than
    giving up -- otherwise a provider id we cannot use would mask a good poster
    match. Pure: everything it reads is pre-fetched.
    """
    if existing_id(entry):
        return HAS_ID, None, None, None

    canonical = canonical or {}
    tmdb_id = tier = key = None
    saw_ambiguous = False

    found = (detail_map or {}).get(str(stream_id))

    # Approvals are NOT gated by `tag_unique_movies`. That gate exists to stop
    # the detail tier re-tagging thousands of rows unattended; an approval is
    # one entry a person typed out, so the intent is explicit either way.
    approved = (cfg.get("approvals") or {}).get((account_name, str(stream_id)))
    if approved:
        tmdb_id, tier = approved, TIER_MANUAL

    if tmdb_id is None and found:
        candidate = found[0]
        if candidate in canonical or cfg.get("tag_unique_movies"):
            tmdb_id, tier = candidate, TIER_DETAIL

    if tmdb_id is None:
        poster_key = entry_poster_key(entry, MOVIE_POSTER_FIELDS)
        if poster_key:
            if poster_key in (idx.get("poster_amb") or ()):
                saw_ambiguous = True
            elif (idx.get("poster") or {}).get(poster_key):
                tmdb_id, tier, key = idx["poster"][poster_key], TIER_POSTER, poster_key

    if tmdb_id is None:
        plot_hash = entry_plot_hash(entry, MOVIE_PLOT_FIELDS)
        if plot_hash:
            if plot_hash in (idx.get("plot_amb") or ()):
                saw_ambiguous = True
            elif (idx.get("plot") or {}).get(plot_hash):
                tmdb_id, tier = idx["plot"][plot_hash], TIER_PLOT
                key = _plot_digest(plot_hash)

    if tmdb_id is None:
        if found:
            # We had the provider's own id but no row holds it, and no weaker
            # signal matched either. Reported so status can count it.
            return NO_CANONICAL, found[0], TIER_DETAIL, None
        if not entry_poster_key(entry, MOVIE_POSTER_FIELDS) and                 not entry_plot_hash(entry, MOVIE_PLOT_FIELDS):
            return NO_SIGNAL, None, None, None
        return (AMBIGUOUS if saw_ambiguous else NO_MATCH), None, None, None

    if is_denied(cfg.get("denylist"), account_name, stream_id, tmdb_id):
        return DENIED, tmdb_id, tier, key

    if tier != TIER_MANUAL:
        variant_re = cfg.get("variant_re")
        if variant_re is not None and variant_re.search(str(entry.get("name") or "")):
            return VARIANT, tmdb_id, tier, key

    return (DRY_RUN if cfg.get("dry_run") else INJECT), tmdb_id, tier, key


def _movie_detail_map(account, batch):
    """{stream_id: (detail tmdb, current movie id)} for the entries in a batch."""
    from apps.vod.models import M3UMovieRelation

    ids = [
        str(e.get("stream_id")) for e in batch
        if isinstance(e, dict) and e.get("stream_id") is not None
    ]
    if not ids:
        return {}
    out = {}
    for sid, props, movie_id in (
        M3UMovieRelation.objects
        .filter(m3u_account=account, stream_id__in=ids)
        .values_list("stream_id", "custom_properties", "movie_id")
    ):
        found = detail_tmdb(props)
        if found:
            out[str(sid)] = (found, movie_id)
    return out


def is_new_merge(action, tmdb_id, canonical, current_id):
    """Whether this injection actually MOVES the relation.

    Injection re-runs on every scan by design -- that is what makes the merge
    durable -- so logging every injection turns the audit trail into thousands
    of identical entries per refresh and evicts the history that matters. A
    relation already sitting on the target is a re-derivation, not a change.

    Variants and denials are always recorded: they are decisions, not moves.
    """
    if action not in (INJECT, DRY_RUN):
        return True
    target = ((canonical or {}).get(tmdb_id) or {}).get("id")
    if target is None or current_id is None:
        return True
    return target != current_id


def _canonical_series_map(tmdb_ids):
    """{tmdb: {id, uuid, name}} for series, so a merge can be named."""
    if not tmdb_ids:
        return {}
    from apps.vod.models import Series
    return {
        row["tmdb_id"]: row
        for row in Series.objects.filter(tmdb_id__in=list(tmdb_ids))
        .values("id", "uuid", "tmdb_id", "name")
    }


def _series_relation_map(account, external_ids):
    """{external_series_id: current series id} for the entries in a batch."""
    if not external_ids:
        return {}
    from apps.vod.models import M3USeriesRelation
    return {
        str(ext): sid
        for ext, sid in M3USeriesRelation.objects
        .filter(m3u_account=account, external_series_id__in=[str(e) for e in external_ids])
        .values_list("external_series_id", "series_id")
    }


def _movie_relation_current(account, stream_ids):
    """{stream_id: current movie id} for the entries in a batch."""
    if not stream_ids:
        return {}
    from apps.vod.models import M3UMovieRelation
    return {
        str(sid): mid
        for sid, mid in M3UMovieRelation.objects
        .filter(m3u_account=account, stream_id__in=[str(s) for s in stream_ids])
        .values_list("stream_id", "movie_id")
    }


def _canonical_map(tmdb_ids):
    """{tmdb: {id, uuid, name}} so the audit trail can name where a title landed."""
    if not tmdb_ids:
        return {}
    from apps.vod.models import Movie
    return {
        row["tmdb_id"]: row
        for row in Movie.objects.filter(tmdb_id__in=list(tmdb_ids))
        .values("id", "uuid", "tmdb_id", "name")
    }


def inject_movie_batch(account, batch, categories=None, relations=None) -> dict:
    """Mutate `batch` entries in place, adding `tmdb_id` from stored detail."""
    tally = {}
    if not batch:
        return tally

    cfg = _load_config()
    if not cfg["merge_movies"]:
        return tally

    account_name = getattr(account, "name", "?")
    if cfg["movie_accounts"] and account_name not in cfg["movie_accounts"]:
        return tally
    acct_type = getattr(account, "account_type", None)
    if acct_type is not None and acct_type != "XC":
        return tally

    # Not a gate any more: the poster and plot tiers need no stored detail, so
    # an empty map still leaves plenty to match on.
    detail_map = _movie_detail_map(account, batch)

    idx = _get_movie_indexes()
    canon = _canonical_map({t for t, _ in detail_map.values()})

    decisions = []
    for entry in batch:
        if not isinstance(entry, dict):
            continue
        if not category_enabled(entry, categories, relations):
            tally["disabled_category"] = tally.get("disabled_category", 0) + 1
            continue
        stream_id = entry.get("stream_id")
        action, tmdb_id, tier, _ = decide_movie(
            entry, account_name, stream_id, detail_map, canon, idx, cfg
        )
        tally[action] = tally.get(action, 0) + 1
        if action == INJECT:
            # Movies read `tmdb_id` first; see the note above.
            entry["tmdb_id"] = tmdb_id
        if action in (INJECT, DRY_RUN, VARIANT, DENIED):
            decisions.append(Decision(entry, stream_id, action, tmdb_id, tier, None))

    # Poster/plot matches resolve against the tagged index, so their canonical
    # is not in the detail-derived map; fetch anything still missing.
    extra = {d.tmdb_id for d in decisions if d.tmdb_id and d.tmdb_id not in canon}
    if extra:
        canon.update(_canonical_map(extra))

    # Current movie for every decided relation, not just the ones with stored
    # detail -- poster/plot matches need it too, to tell a move from a re-derive.
    current = _movie_relation_current(account, [d.external_id for d in decisions])

    audit = []
    for entry, stream_id, action, tmdb_id, tier, _key in decisions:
        previous = current.get(str(stream_id))
        if not is_new_merge(action, tmdb_id, canon, previous):
            tally["unchanged"] = tally.get("unchanged", 0) + 1
            continue
        target = canon.get(tmdb_id) or {}
        audit.append({
            "ts": time.time(),
            "action": action,
            "tier": tier,
            "kind": "movie",
            "account": account_name,
            "stream_id": str(stream_id) if stream_id is not None else None,
            "name": entry.get("name"),
            "tmdb_id": tmdb_id,
            "previous_movie_id": previous,
            "canonical_id": target.get("id"),
            "canonical_uuid": str(target["uuid"]) if target.get("uuid") else None,
            # No existing row with this tmdb: the scan will create a newly
            # tagged Movie rather than folding into one. A new Dispatcharr id
            # for that title, which downstream tools will see as a new item.
            "creates_new_row": not bool(target),
        })
        logger.info(
            "[VOD-MERGE] movie %s (%s): %r (%s:%s) -> tmdb=%s %s",
            action, tier, entry.get("name"), account_name, stream_id, tmdb_id,
            ("merge into movie %s" % target.get("id")) if target else "NEW tagged row",
        )

    _append_log(audit)
    return tally


def patched_process_movie_batch(account, batch, categories, relations,
                                scan_start_time=None, *args, **kwargs):
    """Wrapper: inject before the original computes the merge key.

    Signature-agnostic for the same reasons as the series wrapper above.
    """
    _log_pid_once("movie-batch wrapper active")
    _note_unexpected_args("process_movie_batch", args, kwargs)
    try:
        if is_enabled():
            tally = inject_movie_batch(account, batch, categories, relations)
            if tally.get(INJECT) or tally.get(DRY_RUN):
                logger.info(
                    "[VOD-MERGE] movie batch for %r: %s",
                    getattr(account, "name", "?"),
                    ", ".join(f"{k}={v}" for k, v in sorted(tally.items())),
                )
    except Exception:
        logger.exception("[VOD-MERGE] movie injection failed; continuing unpatched")

    return _orig_process_movie_batch(
        account, batch, categories, relations, scan_start_time, *args, **kwargs
    )


# --------------------------------------------------------------------------- #
# Enrichment, step 1: the wanted set
# --------------------------------------------------------------------------- #
# vod_preferences ranks a movie's candidate relations against each other, so it
# needs measured video and audio for EVERY relation of a title, not one. Nothing
# in Dispatcharr produces that: the XC detail endpoint refreshes a single
# relation per movie (the highest-priority account's), which is a structural cap
# rather than a configuration one.
#
# Enriching all 37k movies is not the goal and never was -- the useful set is
# the few thousand titles actually synced to a library, and only the generator
# knows which those are. It publishes them as a file; we read it. Demand is the
# one thing it holds that we cannot query.
#
# FAIL OPEN ON ENRICHMENT, FAIL CLOSED ON SCOPE. A fault in the enrichment costs
# some missing metadata. A fault in the thing that decides HOW MUCH WORK TO DO
# costs thousands of provider calls on a set nobody asked for. So every way the
# file can be untrustworthy -- absent, unreadable, malformed, unknown schema,
# count disagreeing with the array, or simply too old to believe -- collapses to
# doing nothing. It must never widen to "enrich everything".

DEFAULT_WANTED_SET_PATH = ""        # blank = feature off, no default path
DEFAULT_ENRICH_MOVIES = False       # opt-in, like merge_movies
DEFAULT_ENRICH_LIMIT = 25
DEFAULT_ENRICH_DELAY_MS = 500

WANTED_SCHEMA = 1
# Generous on purpose. Too low blocks a weekly sync; too high defeats the check.
# A value that only matters in a failure case is not worth a settings row.
WANTED_MAX_AGE_DAYS = 14
# Only an ERROR is worth retrying -- a provider that answered and had nothing is
# a fact about that stream, not a transient.
ENRICH_ERROR_RETRY_DAYS = 7

ENRICHED = "enriched"


def _parse_iso_z(text):
    """ISO 8601 with a trailing Z, which fromisoformat rejects before 3.11."""
    from datetime import datetime

    s = str(text or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def validate_wanted_set(doc, now=None, max_age_days=WANTED_MAX_AGE_DAYS):
    """Pure. -> (ok, reason). Every failure means DO NOTHING, never 'do all'."""
    from datetime import timedelta

    if not isinstance(doc, dict):
        return False, "not a JSON object"
    if doc.get("schema") != WANTED_SCHEMA:
        return False, "unsupported schema %r (expected %r)" % (
            doc.get("schema"), WANTED_SCHEMA)
    ids = doc.get("tmdb_ids")
    if not isinstance(ids, list):
        return False, "tmdb_ids missing or not a list"
    unid = doc.get("unidentified") or []
    if not isinstance(unid, list):
        return False, "unidentified present but not a list"
    count = doc.get("count")
    # Their writer runs at the end of a sync that can abort part-way, so a count
    # disagreeing with the array is the cheapest possible corruption signal.
    if count != len(ids):
        return False, "count=%r disagrees with len(tmdb_ids)=%d" % (count, len(ids))
    stamped = _parse_iso_z(doc.get("generated_at"))
    if stamped is None:
        return False, "generated_at missing or unparseable"
    if now is not None and max_age_days:
        age = now - stamped
        if age > timedelta(days=max_age_days):
            # NB a stale file can mean "their provider was flaky", not only
            # "the generator stopped" -- they publish nothing on a partial
            # fetch rather than publishing a subset. Either way we decline.
            return False, "generated_at is %d days old (limit %d)" % (
                age.days, max_age_days)
    if not ids and not unid:
        return False, "wanted set is empty"
    return True, None


def read_wanted_set(path, now=None):
    """-> (doc|None, status, detail). Status separates the two cases that must
    NOT look alike: absent is NORMAL (pre-first-sync, or a wiped mount) and logs
    INFO; unreadable is a MISCONFIGURATION and logs WARNING. Collapsing them
    would let a permissions mistake silently disable the whole feature."""
    import json

    if not path:
        return None, "disabled", "no wanted-set path configured"
    if not os.path.exists(path):
        return None, "absent", path
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except Exception as exc:
        return None, "unreadable", "%s: %s" % (type(exc).__name__, exc)
    try:
        doc = json.loads(raw)
    except Exception as exc:
        return None, "malformed", str(exc)
    ok, reason = validate_wanted_set(doc, now=now)
    if not ok:
        return None, "invalid", reason
    return doc, "ok", None


def resolve_unidentified(entry, lookup_by_id, lookup_by_name):
    """Pure given the two lookups. -> (movie_id|None, how, skip_reason).

    Strict order, because these are not interchangeable:
      1. stream_id -- authoritative while the row exists
      2. name      -- ONLY on a unique match; ambiguity is skipped, never guessed
      3. resolved_tmdb_id -- never alone. It comes from an unverified
         first-result lookup, so it may only CORROBORATE a match something else
         already made. Disagreement means trust neither.
    """
    sid = entry.get("stream_id")
    name = entry.get("name")
    rid = entry.get("resolved_tmdb_id")

    if sid is not None:
        row = lookup_by_id(sid)
        if row is not None:
            return row[0], "stream_id", None

    if name:
        hits = lookup_by_name(name)
        if len(hits) == 1:
            mid, tmdb = hits[0]
            if rid and tmdb and str(tmdb) != str(rid):
                return None, None, "name and resolved_tmdb_id disagree"
            return mid, "name", None
        if len(hits) > 1:
            return None, None, "name matches %d rows (ambiguous)" % len(hits)

    return None, None, "stream_id dead and no unique name match"


def wanted_movie_ids(doc):
    """-> (set of Movie.id, stats dict). DB-backed wrapper around the above."""
    from apps.vod.models import Movie

    ids = [str(t) for t in (doc.get("tmdb_ids") or [])]
    unid = doc.get("unidentified") or []

    found = {}
    by_tmdb = dict(
        Movie.objects.filter(tmdb_id__in=ids).values_list("tmdb_id", "id"))
    for t in ids:
        mid = by_tmdb.get(t)
        if mid:
            found[mid] = "tmdb_id"

    def lookup_by_id(sid):
        row = Movie.objects.filter(id=sid).values_list("id", "tmdb_id").first()
        return row

    def lookup_by_name(name):
        return list(
            Movie.objects.filter(name=name).values_list("id", "tmdb_id")[:3])

    stats = {"tmdb_total": len(ids), "tmdb_resolved": len(by_tmdb),
             "unid_total": len(unid), "unid_resolved": 0, "skipped": []}
    for e in unid:
        mid, how, why = resolve_unidentified(e, lookup_by_id, lookup_by_name)
        if mid is None:
            stats["skipped"].append("%s: %s" % (e.get("stream_id"), why))
            continue
        found.setdefault(mid, how)
        stats["unid_resolved"] += 1
    return set(found), stats


# --------------------------------------------------------------------------- #
# Enrichment, step 2: harvest what the providers already know
# --------------------------------------------------------------------------- #
# Our sweep calls `get_vod_info` PER RELATION, straight at the provider, so it
# is not subject to the one-relation-per-movie cap the XC endpoint imposes. That
# asymmetry is the whole reason this belongs here rather than in the generator.
#
# Cheap -- a light API call, measured around 0.65s median -- and its real job is
# to SHRINK the expensive ffprobe pass precisely rather than leaving it guessed.
#
# We stamp an ATTEMPT, not a success. Skipping on "has usable data" would
# re-ask, every single run, for exactly the relations whose providers never
# answer -- and those are the ones a later probe pass pays the most for.

def relation_has_essentials(props):
    """Measured video dimensions AND an audio block. "Essential" here means
    resolution plus audio codec; a Dolby Vision record is explicitly not
    required, since no provider payload has ever carried one."""
    d = (props or {}).get("detailed_info") or {}
    if not isinstance(d, dict):
        return False
    v = d.get("video")
    return bool(isinstance(v, dict) and v.get("width") and d.get("audio"))


def decide_enrich(props, now_iso, retry_days=ENRICH_ERROR_RETRY_DAYS):
    """Pure. -> (should_fetch, reason)."""
    if relation_has_essentials(props):
        return False, "has essentials"
    own = ((props or {}).get(OWN_PROPS_KEY) or {})
    mark = own.get("enrich") if isinstance(own, dict) else None
    if not isinstance(mark, dict):
        return True, "never attempted"
    got = mark.get("got")
    if got != "error":
        # The provider answered and this is what it has. Asking again is how a
        # permanent gap turns into a nightly cost.
        return False, "attempted, provider had nothing more (%s)" % got
    when = _parse_iso_z(mark.get("at"))
    if when is None:
        return True, "previous error, unreadable timestamp"
    from datetime import timedelta
    now = _parse_iso_z(now_iso)
    if now is not None and (now - when) < timedelta(days=retry_days):
        return False, "previous error, still inside retry window"
    return True, "previous error, retry window elapsed"


def enrich_candidates(movie_ids):
    """Relations of wanted movies, GROUPED BY MOVIE. Not account-scoped: the
    point is every candidate of a wanted title, whoever carries it.

    Deliberately not newest-relation-first, unlike the detail sweep. Providers
    cluster in id ranges -- overwhelmingly so for one that has been wholesale
    recreated -- so `-id` walks a single provider's copies across every movie
    before reaching the next provider's. Measured on a real wanted set: the
    first thirty fetches were all one provider.

    That ordering leaves EVERY movie partially covered for as long as the
    backlog lasts, which is the worst possible state for ranking. A candidate
    with no audio data scores zero, so a half-enriched comparison set can rank
    an enriched stereo track above an unmeasured surround one. Completing
    movies one at a time keeps coverage uniform across whatever has been done.
    """
    from apps.vod.models import M3UMovieRelation

    return (M3UMovieRelation.objects
            .filter(movie_id__in=list(movie_ids))
            .select_related("m3u_account", "movie")
            .order_by("movie_id", "id"))


def take_whole_movies(groups, limit, allow_oversized=True):
    """Pick whole movies up to `limit` relations. Pure.

    `groups` is an ordered sequence of `(movie_id, [items])`. A movie is not
    split across runs, for the coverage reason above.

    `allow_oversized` decides what happens when a SINGLE movie exceeds the
    limit on its own, and the right answer differs by how expensive the work
    is:

    * True (detail lookups) -- take it whole. The calls are light, and the
      alternative is that a title with more copies than the batch size never
      gets enriched at all, which are exactly the titles ranking matters most
      for.

    * False (stream measurement) -- truncate it to the limit and resume next
      run. Here the limit is a SAFETY ceiling, not a target: every item costs a
      provider connection slot for seconds, so honoring the coverage
      preference would mean a setting that says three quietly doing several
      times that. In practice that was over a minute of continuous
      connections on a provider that allows one, and it timed out the
      request that started it.

      Only the FIRST movie of a run can ever be split, and ordering is stable,
      so at most ONE title is partially covered at any moment and it is the
      first thing resumed. That is a far smaller version of the problem than
      the one whole-movie batching exists to prevent.
    """
    chosen = []
    for _mid, items in groups:
        if limit and not allow_oversized:
            # Hard ceiling: fill the budget exactly, splitting whichever title
            # straddles the boundary. Stopping short to avoid a split would
            # waste the run for nothing -- exactly one title ends up partial
            # either way, and it is the first thing the next run resumes.
            room = limit - len(chosen)
            if room <= 0:
                break
            chosen.extend(items[:room])
            continue
        if limit and chosen and len(chosen) + len(items) > limit:
            break
        chosen.extend(items)
        if limit and len(chosen) >= limit:
            break
    return chosen


def enrich_status(cfg=None):
    """Read-only preview. Makes NO provider calls -- safe to run any time, and
    the thing to look at before pointing this at a real wanted set."""
    from django.utils import timezone

    cfg = cfg or _load_config(force=True)
    out = {"enrich_movies": cfg["enrich_movies"],
           "path": cfg["wanted_set_path"] or "<unset>",
           # Measuring runs in the background, so this is where its result shows.
           "last_enrich_run": read_run_status()}
    doc, status, detail = read_wanted_set(cfg["wanted_set_path"],
                                          now=timezone.now())
    out["file_status"] = status
    out["file_detail"] = detail
    if doc is None:
        return out

    out["generated_at"] = doc.get("generated_at")
    ids, stats = wanted_movie_ids(doc)
    out.update(stats)
    out["movies"] = len(ids)

    now_iso = timezone.now().isoformat()
    todo = have = attempted = measure = dv_checks = 0
    per_account = {}
    for rel in enrich_candidates(ids).iterator(chunk_size=500):
        props = rel.custom_properties or {}
        name = rel.m3u_account.name
        slot = per_account.setdefault(name, {"relations": 0, "need": 0,
                                             "measure": 0})
        slot["relations"] += 1
        if relation_has_essentials(props):
            have += 1
            if (cfg["probe_movies"] and cfg["probe_4k_dv"]
                    and decide_dv_check(props, now_iso)[0]):
                dv_checks += 1
            continue
        # The same decision a run makes when measuring, so this is its backlog
        # exactly rather than an estimate of it.
        if decide_probe(props, now_iso)[0]:
            measure += 1
            slot["measure"] += 1
        should, _ = decide_enrich(props, now_iso)
        if should:
            todo += 1
            slot["need"] += 1
        else:
            attempted += 1
    out.update({"relations": have + todo + attempted, "have_essentials": have,
                "need_fetch": todo, "already_attempted": attempted,
                "need_measure": measure,
                "per_account": per_account,
                "runs_at_current_limit": (
                    0 if not cfg["enrich_limit"]
                    else -(-todo // cfg["enrich_limit"])),
                "need_dv_check": dv_checks,
                "measure_runs_at_current_limit": (
                    0 if not cfg["probe_limit"]
                    else -(-(measure + dv_checks) // cfg["probe_limit"])),
                # Disclosure, not a guard: a big wanted set is legitimate, but
                # how many nights it takes should never be a surprise.
                "nights": estimate_nights(todo, measure + dv_checks, cfg)})
    return out


def enrich_movies_impl(limit=None, delay=None, heartbeat=None):
    """Fetch provider detail for every relation of every wanted movie.

    `heartbeat` works as in `probe_movies_impl`: checked before every lookup,
    and the run stops if it returns False.

    Writes `detailed_info` -- the same key core writes and vod_preferences
    reads -- but never core's `detailed_fetched` or `last_advanced_refresh`.
    Those two remain the only record that a CLIENT asked for a movie, and a
    sweep must not write the field it reads.
    """
    from django.utils import timezone
    from core.xtream_codes import Client as XtreamCodesClient

    cfg = _load_config(force=True)
    limit = cfg["enrich_limit"] if limit is None else limit
    delay = (cfg["enrich_delay_ms"] if delay is None else delay) / 1000.0

    stats = {"fetched": 0, "got_essentials": 0, "empty": 0, "errors": 0,
             "skipped": 0, "remaining": 0}
    if not cfg["enrich_movies"]:
        stats["aborted"] = "enrichment is off"
        return stats

    doc, status, detail = read_wanted_set(cfg["wanted_set_path"],
                                          now=timezone.now())
    if doc is None:
        # Fail CLOSED on scope. Never widen to "everything".
        stats["aborted"] = "wanted set %s (%s)" % (status, detail)
        if status in ("unreadable", "malformed", "invalid"):
            logger.warning("[VOD-MERGE] enrichment declined -- wanted set %s: %s",
                           status, detail)
        else:
            logger.info("[VOD-MERGE] enrichment skipped -- wanted set %s: %s",
                        status, detail)
        return stats

    movie_ids, resolve_stats = wanted_movie_ids(doc)
    stats["wanted_movies"] = len(movie_ids)
    stats.update({k: v for k, v in resolve_stats.items() if k != "skipped"})

    now_iso = timezone.now().isoformat()
    # Collect first, then take WHOLE movies, so a bounded run never leaves a
    # title half-measured. The scan is cheap next to the provider calls.
    per_movie = []
    for rel in enrich_candidates(movie_ids).iterator(chunk_size=500):
        should, _why = decide_enrich(rel.custom_properties or {}, now_iso)
        if not should:
            stats["skipped"] += 1
            continue
        if per_movie and per_movie[-1][0] == rel.movie_id:
            per_movie[-1][1].append(rel)
        else:
            per_movie.append((rel.movie_id, [rel]))

    by_account = {}
    chosen = take_whole_movies(per_movie, limit)
    stats["movies_this_run"] = len({r.movie_id for r in chosen})
    for rel in chosen:
        by_account.setdefault(rel.m3u_account_id, []).append(rel)

    per_account = {}
    for rels in by_account.values():
        if stats.get("stopped"):
            break
        account = rels[0].m3u_account
        name = account.name
        slot = per_account.setdefault(
            name, {"fetched": 0, "essentials": 0, "empty": 0, "errors": 0})
        try:
            client = XtreamCodesClient(
                server_url=account.server_url,
                username=account.username,
                password=account.password,
                user_agent=account.get_user_agent_string(),
            )
        except Exception:
            logger.exception("[VOD-MERGE] could not build client for %r", name)
            stats["errors"] += len(rels)
            slot["errors"] += len(rels)
            continue

        with client:
            for rel in rels:
                if heartbeat is not None and not heartbeat():
                    stats["stopped"] = _stop_reason(heartbeat)
                    logger.info("[VOD-MERGE] enrichment stopped early -- %s",
                                stats["stopped"])
                    break
                props = rel.custom_properties or {}
                outcome = "none"
                try:
                    payload = client.get_vod_info(rel.stream_id)
                except Exception as exc:
                    stats["errors"] += 1
                    slot["errors"] += 1
                    outcome = "error"
                    logger.warning("[VOD-MERGE] enrich fetch failed %r rel=%s: %s",
                                   name, rel.id, exc)
                    payload = None

                if payload is not None:
                    info = (payload or {}).get("info")
                    if isinstance(info, list):
                        info = info[0] if info and isinstance(info[0], dict) else {}
                    if isinstance(info, dict) and info:
                        cleaned = _clean_props(info)
                        if cleaned:
                            # Our own write is a WHOLE REPLACE of detailed_info,
                            # exactly like core's -- so it can drop a tmdb_id the
                            # detail sweep captured, which is the merge plugin's
                            # strongest movie signal. `preserve_detail` guards
                            # core's refresh and does not reach this path, so the
                            # same rule is applied here directly: a value the
                            # provider actually sent always wins, and this only
                            # ever refills a hole the new payload left.
                            previous = props.get("detailed_info")
                            if isinstance(previous, dict) and previous:
                                cleaned, refilled = restore_essential(previous, cleaned)
                                if refilled:
                                    logger.info(
                                        "[VOD-MERGE] enrichment preserved %s on "
                                        "rel=%s", refilled, rel.id)
                            props["detailed_info"] = cleaned
                        stats["fetched"] += 1
                        slot["fetched"] += 1
                        if relation_has_essentials(props):
                            stats["got_essentials"] += 1
                            slot["essentials"] += 1
                            outcome = "full"
                        else:
                            outcome = "partial"
                    else:
                        stats["empty"] += 1
                        slot["empty"] += 1
                        outcome = "none"

                _stamp_own(props, enrich={"at": now_iso, "got": outcome})
                rel.custom_properties = props
                try:
                    rel.save(update_fields=["custom_properties"])
                except Exception:
                    logger.exception(
                        "[VOD-MERGE] could not store enrichment for rel=%s", rel.id)
                if delay:
                    time.sleep(delay)

    remaining = 0
    for rel in enrich_candidates(movie_ids).iterator(chunk_size=500):
        should, _why = decide_enrich(rel.custom_properties or {}, now_iso)
        if should:
            remaining += 1
    stats["remaining"] = remaining
    stats["per_account"] = per_account
    logger.info("[VOD-MERGE] movie enrichment: %s", stats)
    return stats


# --------------------------------------------------------------------------- #
# Enrichment, step 3: measure what no provider will tell us
# --------------------------------------------------------------------------- #
# Step 2 harvests what providers already know. On a real library that is a
# minority of copies, because many providers return a detail payload with no
# video or audio block at all, consistently, with no errors. The gap is
# structural, not operational, so no amount of re-asking closes it.
#
# The only remaining source is the stream itself. A live spike confirmed this
# works and, more importantly, yields something the API never does: the DOVI
# configuration record that identifies Dolby Vision without a fallback layer.
# That field has not been seen in a single provider payload, and appeared in
# the very first probe. Avoid-DV goes from unreachable to measurable.
#
# THE COST IS A DIFFERENT CLASS AND THE DESIGN IS SHAPED AROUND IT. A detail
# call is one request, kilobytes, milliseconds. A probe opens the actual media
# and holds a provider CONNECTION SLOT for seconds -- the same scarce resource
# playback competes for. So: bounded reads, a delay between probes, a hard
# per-run cap, and a circuit breaker that treats repeated failure as a fact
# about the provider rather than about each stream.

DEFAULT_PROBE_MOVIES = False
DEFAULT_PROBE_LIMIT = 3             # deliberately tiny; this is not a sweep yet
DEFAULT_PROBE_DELAY_MS = 2000
# On by default because it only ever applies once measuring itself is on,
# and it is the only way to learn DV status for a copy a provider described.
DEFAULT_PROBE_4K_DV = True

PROBE_KEY = "ffprobe_info"
PROBE_TIMEOUT_S = 30
# Consecutive failures against ONE account before we stop blaming the streams.
PROBE_BREAKER = 3
PROBE_ERROR_RETRY_DAYS = 7
PROBED = "probed"
TIER_PROBE = "probe"


def parse_ffprobe(payload):
    """Pure. ffprobe JSON -> the shape providers use, so existing readers work.

    Takes the first video and first audio stream verbatim rather than picking
    fields out: a provider's `detailed_info.video` IS an ffprobe stream object,
    so copying the shape means `side_data_list` and anything else useful comes
    along without being enumerated here.
    """
    if not isinstance(payload, dict):
        return {}
    out = {}
    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        kind = stream.get("codec_type")
        if kind == "video" and "video" not in out:
            out["video"] = stream
        elif kind == "audio" and "audio" not in out:
            out["audio"] = stream
    fmt = payload.get("format")
    if isinstance(fmt, dict):
        if fmt.get("duration"):
            out["duration_secs"] = fmt.get("duration")
        if fmt.get("bit_rate"):
            out["bitrate"] = fmt.get("bit_rate")
    return out


def classify_probe(returncode, parsed):
    """Pure. -> 'full' | 'none' | 'error'.

    The distinction that matters: 'none' means ffprobe RAN and the stream has
    nothing useful, which is permanent for that stream. 'error' means we never
    got an answer, which may be transient and is the only outcome worth
    retrying.
    """
    if returncode != 0 or not parsed:
        return "error"
    video = parsed.get("video") or {}
    if video.get("width") and parsed.get("audio"):
        return "full"
    # A video stream with no dimensions is not a property of the media -- every
    # real one has them. It is the signature of a read that was cut short, and
    # on a provider capped at one connection that happens whenever a probe
    # collides with playback. Calling it "none" would record a transient
    # collision as a permanent fact and never look again, so it is retryable.
    # Presence of the key, not truthiness of its contents: an empty block is
    # just as suspicious, and keying on contents would make this depend on how
    # `parse_ffprobe` happens to represent a stream it could not read.
    if "video" in parsed and not video.get("width"):
        return "error"
    return "none"


def decide_probe(props, now_iso, retry_days=PROBE_ERROR_RETRY_DAYS):
    """Pure. -> (should_probe, reason). Same attempt-not-success rule as step 2,
    and it matters more here: a probe costs a connection slot, so re-asking for
    the streams that never answer is the most expensive possible mistake."""
    if relation_has_essentials(props):
        return False, "already has video and audio"
    own = ((props or {}).get(OWN_PROPS_KEY) or {})
    if not isinstance(own, dict):
        own = {}
    # Measure only what the provider lookup could not fill. The lookup is a
    # light request; a measurement holds the connection for seconds. Spending
    # one on a copy whose provider would have described it for free is the
    # waste this whole ordering exists to avoid. Any attempt counts, including
    # an error -- the lookup has its own retry, and a provider whose detail
    # endpoint always fails must still be measurable.
    if not isinstance(own.get("enrich"), dict):
        return False, "provider lookup not tried yet"
    return _probe_mark_decision(own.get("probe"), now_iso, retry_days)


def _probe_mark_decision(mark, now_iso, retry_days):
    """Pure. The retry rule every kind of probe shares: never probed -> yes;
    probed and answered -> never again; errored -> after the retry window."""
    if not isinstance(mark, dict):
        return True, "never probed"
    got = mark.get("got")
    if got != "error":
        return False, "probed, stream had nothing more (%s)" % got
    when = _parse_iso_z(mark.get("at"))
    if when is None:
        return True, "previous error, unreadable timestamp"
    from datetime import timedelta
    now = _parse_iso_z(now_iso)
    if now is not None and (now - when) < timedelta(days=retry_days):
        return False, "previous error, still inside retry window"
    return True, "previous error, retry window elapsed"


# A copy can have everything ranking needs from its provider and still hide the
# one thing no provider payload carries: the Dolby Vision record. That decides
# whether a DV stream without a fallback layer renders correctly on non-DV
# hardware, and DV lives almost entirely on 4K copies -- so those, and only
# those, are worth a probe even when the provider described them.
#
# The 4K rule mirrors vod_preferences' tiering exactly (the larger dimension
# within 5% of 3840, or a height of at least 2160), so the copies checked here
# are the copies that plugin ranks as 4K. It is a copy, not an import: the two
# plugins never depend on each other.
DV_WIDTH = 3840
DV_HEIGHT = 2160
DIM_TOLERANCE = 0.05


def _is_cover_image(video):
    """An embedded poster rides in the video list too; never treat it as the
    picture. A tall poster can be as large as a 4K frame."""
    disp = video.get("disposition")
    if isinstance(disp, dict) and disp.get("attached_pic"):
        return True
    return str(video.get("codec_name") or "").lower() in ("mjpeg", "png", "bmp")


def is_4k_video(video):
    """Pure. Same threshold vod_preferences uses for its 4K tier."""
    if not isinstance(video, dict) or _is_cover_image(video):
        return False
    try:
        w = int(video.get("width") or 0)
        h = int(video.get("height") or 0)
    except (TypeError, ValueError):
        return False
    return max(w, h) >= DV_WIDTH * (1 - DIM_TOLERANCE) or h >= DV_HEIGHT


def has_dovi(video):
    """Pure. Does a video block carry a Dolby Vision configuration record?"""
    if not isinstance(video, dict):
        return False
    return any(isinstance(sd, dict) and "DOVI" in str(sd.get("side_data_type") or "")
               for sd in (video.get("side_data_list") or []))


def is_dv_no_fallback(video):
    """Pure. Dolby Vision with NO fallback layer (Profile 5): renders with the
    wrong colors on non-DV hardware. Same rule vod_preferences avoids by --
    a base-layer compatibility id of 0, or Profile 5 with the id missing."""
    if not isinstance(video, dict):
        return False
    for sd in video.get("side_data_list") or []:
        if not isinstance(sd, dict) or "DOVI" not in str(sd.get("side_data_type") or ""):
            continue
        compat = sd.get("dv_bl_signal_compatibility_id")
        if compat == 0 or (compat is None and sd.get("dv_profile") == 5):
            return True
    return False


def note_dv(stats, parsed, was_dv_check):
    """Pure. Tally what one answered measurement says about Dolby Vision.

    DV found is counted for EVERY measurement, not just the 4K checks: a copy
    no provider described is measured for its gaps and reveals DV the same way,
    and a count that ignored those would under-report exactly the copies most
    likely to be the problem.
    """
    if was_dv_check:
        stats["dv_checked"] = stats.get("dv_checked", 0) + 1
    video = (parsed or {}).get("video")
    if has_dovi(video):
        stats["dv_found"] = stats.get("dv_found", 0) + 1
        if is_dv_no_fallback(video):
            stats["dv_no_fallback"] = stats.get("dv_no_fallback", 0) + 1
    return stats


def decide_dv_check(props, now_iso, retry_days=PROBE_ERROR_RETRY_DAYS):
    """Pure. -> (should_probe, reason) for a copy the provider DID describe.

    Only 4K copies, only where no side data is known yet, and under the same
    retry rule as every other probe -- a check that answered is never repeated,
    whatever it found.
    """
    if not relation_has_essentials(props):
        return False, "not described by the provider (gap-filling handles it)"
    video = (props.get("detailed_info") or {}).get("video")
    if not is_4k_video(video):
        return False, "not 4K"
    if video.get("side_data_list"):
        return False, "side data already known"
    own = props.get(OWN_PROPS_KEY)
    mark = own.get("probe") if isinstance(own, dict) else None
    return _probe_mark_decision(mark, now_iso, retry_days)


def run_ffprobe(url, user_agent=None, timeout=PROBE_TIMEOUT_S):
    """Probe one stream. -> (returncode, parsed_or_None).

    Reads are bounded on purpose. Without `-probesize`/`-analyzeduration`
    ffprobe will happily pull far more of the file than it needs to describe
    it, and every byte is provider bandwidth on a slot something else wants.
    `-rw_timeout` is in microseconds and stops a stalled connection holding the
    slot until the outer timeout fires.
    """
    import subprocess

    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_streams", "-show_format",
           "-probesize", "5M", "-analyzeduration", "5M",
           "-rw_timeout", "15000000"]
    if user_agent:
        cmd += ["-user_agent", user_agent]
    cmd.append(url)
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception as exc:
        logger.warning("[VOD-MERGE] ffprobe could not run: %s", exc)
        return 1, None
    if done.returncode != 0:
        return done.returncode, None
    try:
        import json as _json
        return 0, parse_ffprobe(_json.loads(done.stdout.decode("utf-8", "replace")))
    except Exception as exc:
        logger.warning("[VOD-MERGE] ffprobe output unparseable: %s", exc)
        return 1, None


def store_probe(props, parsed):
    """Pure-ish: put the measurement where it is durable AND where today's
    readers already look. Returns the mutated props.

    Two places on purpose. `ffprobe_info` is ours, carries provenance, and
    nothing else writes it -- so a provider refresh can never destroy a DOVI
    record, which is the one thing only a probe produces. The mirror into
    `detailed_info` is what makes the data usable with no change to the plugin
    that consumes it, and it only ever FILLS A HOLE: a value a provider
    actually sent always wins, exactly as everywhere else here.
    """
    props[PROBE_KEY] = parsed
    detail = props.get("detailed_info")
    if not isinstance(detail, dict):
        detail = {}
    for key in ("video", "audio"):
        # `{}` is falsy, which is what we want -- an empty block reaches
        # storage looking like data and must be treated as absent.
        if parsed.get(key) and not detail.get(key):
            detail[key] = parsed[key]
    # The same fill-a-hole rule one level down, for the one key a provider
    # never sends. A 4K copy the provider described already HAS a video block,
    # so the rule above leaves it alone -- and the DV record would then live
    # only in our own key, where the plugin that acts on it never looks.
    # Every value the provider sent is kept; only the missing key is added.
    measured = parsed.get("video")
    existing = detail.get("video")
    if (isinstance(measured, dict) and measured.get("side_data_list")
            and isinstance(existing, dict) and not existing.get("side_data_list")):
        existing["side_data_list"] = measured["side_data_list"]
    props["detailed_info"] = detail
    return props


def errors_to_stamp(deferred, broken):
    """Pure. Which failed probes are a fact about the STREAM, not the provider.

    A failure against an account that then tripped the breaker says nothing
    about the individual stream -- the provider was not answering. Stamping it
    anyway would put every copy that happened to be queued during a bad hour
    into a week-long retry window, which is the quiet way an outage turns into
    permanently missing data.
    """
    out = []
    for name in sorted(deferred):
        if name in broken:
            continue
        out.extend(deferred[name])
    return out


# --------------------------------------------------------------------------- #
# Do not probe a provider that is busy with real playback
# --------------------------------------------------------------------------- #
# Many accounts allow ONE connection, so a probe during playback is not a risk
# but a certainty: one of the two gets dropped, and it may be the viewer's.
#
# Dispatcharr counts every real stream -- live and VOD alike -- in a per-profile
# Redis counter, and exposes a non-mutating check of it. We ask that check
# before every probe, exactly as the VOD proxy asks it before admitting a
# viewer, and only probe through a profile that has room. On a one-connection
# account that means "only when nothing is playing".
#
# We deliberately do NOT reserve a slot. Core has no cleanup for a leaked
# counter, so a worker killed mid-probe would leave a one-connection account
# looking full -- refusing all playback -- until Redis restarts. And while a
# probe held the slot, a viewer arriving would be refused outright rather than
# merely risking a collision. Checking without reserving leaves one narrow
# window: playback that starts DURING a probe of up to PROBE_TIMEOUT_S. A
# measurement cut short there is recorded as a retryable error, not as a fact
# about the stream.


def pick_probe_profile(profiles, has_capacity):
    """Pure. First profile with room, in the order the VOD proxy tries them:
    the default first, then the rest. -> profile or None (all busy)."""
    profiles = list(profiles)
    ordered = ([p for p in profiles if getattr(p, "is_default", False)]
               + [p for p in profiles if not getattr(p, "is_default", False)])
    for profile in ordered:
        if has_capacity(profile):
            return profile
    return None


def busy_account_ids(groups, profile_for):
    """Pure. Ids of the accounts in `groups` that have no free profile.

    `profile_for(account)` returns the profile a probe would use, or None when
    the account is busy (or cannot be checked). Each account is asked once.
    """
    seen, busy = set(), set()
    for _mid, rels in groups:
        for r in rels:
            if r.m3u_account_id in seen:
                continue
            seen.add(r.m3u_account_id)
            if profile_for(r.m3u_account) is None:
                busy.add(r.m3u_account_id)
    return busy


def without_accounts(groups, account_ids):
    """Pure. `(movie_id, [relations])` groups minus relations on the given
    accounts; groups left empty are dropped."""
    if not account_ids:
        return list(groups)
    out = []
    for mid, rels in groups:
        kept = [r for r in rels if r.m3u_account_id not in account_ids]
        if kept:
            out.append((mid, kept))
    return out


def _capacity_checker(client):
    """-> callable(profile) -> bool, using Dispatcharr's own capacity rule.

    Raises if that rule cannot be loaded. The caller turns that into "measure
    nothing": a future Dispatcharr that moves the function must stop probing
    loudly, not start probing blind.
    """
    from apps.m3u.connection_pool import pool_has_capacity_for_profile
    return lambda profile: pool_has_capacity_for_profile(profile, client)


def _probe_profile(account, has_capacity):
    """The account profile to probe through, or None if it has no free slot.

    Any failure to tell is treated as busy. Not knowing whether someone is
    watching is not permission to interrupt them.
    """
    try:
        from apps.m3u.models import M3UAccountProfile
        profiles = M3UAccountProfile.objects.filter(m3u_account=account,
                                                    is_active=True)
        return pick_probe_profile(profiles, has_capacity)
    except Exception as exc:
        logger.warning("[VOD-MERGE] could not check whether %r is busy, so not "
                       "probing it: %s", getattr(account, "name", account), exc)
        return None


def probe_movies_impl(limit=None, delay=None, heartbeat=None, skip_accounts=None):
    """Measure the wanted copies no provider would describe.

    `skip_accounts`: account ids to leave out entirely -- a nightly run passes
    the ones whose breaker tripped in an earlier batch. Their failures are not
    stamped, so without this every later batch would pick the same copies and
    probe a provider that has already shown it is not answering.

    Scope comes from the same wanted-set file as step 2 and fails closed the
    same way. Candidates are what step 2 could not fill.

    `heartbeat`, if given, is called before every probe and must return True
    to continue. The background task uses it to keep its run lock alive, and
    to stop if the lock was lost -- at that point another run may hold it, and
    two runs probing at once is exactly the collision the lock exists to stop.
    """
    from django.utils import timezone

    cfg = _load_config(force=True)
    limit = cfg["probe_limit"] if limit is None else limit
    delay = (cfg["probe_delay_ms"] if delay is None else delay) / 1000.0

    stats = {"probed": 0, "gained": 0, "nothing": 0, "errors": 0,
             "skipped": 0, "no_url": 0, "broken": [], "busy": [],
             "dv_checked": 0, "dv_found": 0, "dv_no_fallback": 0}
    if not cfg["probe_movies"]:
        stats["aborted"] = "stream probing is off"
        return stats

    try:
        has_capacity = _capacity_checker(_redis_client())
    except Exception as exc:
        stats["aborted"] = "cannot check whether providers are busy (%s)" % (exc,)
        logger.warning("[VOD-MERGE] probing declined -- %s", stats["aborted"])
        return stats

    doc, status, detail = read_wanted_set(cfg["wanted_set_path"],
                                          now=timezone.now())
    if doc is None:
        stats["aborted"] = "wanted set %s (%s)" % (status, detail)
        logger.info("[VOD-MERGE] probing skipped -- wanted set %s: %s",
                    status, detail)
        return stats

    movie_ids, _res = wanted_movie_ids(doc)
    now_iso = timezone.now().isoformat()

    fill, dv = [], []
    dv_ids = set()
    for rel in enrich_candidates(movie_ids).iterator(chunk_size=500):
        props = rel.custom_properties or {}
        if decide_probe(props, now_iso)[0]:
            target = fill
        elif cfg["probe_4k_dv"] and decide_dv_check(props, now_iso)[0]:
            target = dv
            dv_ids.add(rel.id)
        else:
            stats["skipped"] += 1
            continue
        if target and target[-1][0] == rel.movie_id:
            target[-1][1].append(rel)
        else:
            target.append((rel.movie_id, [rel]))
    # Gaps first, DV checks with whatever budget is left: a copy with no
    # resolution or audio at all costs ranking more than an unknown DV status.
    per_movie = fill + dv

    # Leave accounts that are busy RIGHT NOW out of the batch before choosing
    # it. Otherwise the budget can land entirely on a busy provider -- the next
    # title's copies may all be there -- and the run measures nothing while
    # other providers sit idle with copies waiting. The check before each probe
    # still stands, for playback that starts mid-run.
    busy_ids = busy_account_ids(
        per_movie, lambda acc: _probe_profile(acc, has_capacity))
    if busy_ids:
        stats["busy"] = sorted({r.m3u_account.name for _m, rels in per_movie
                                for r in rels if r.m3u_account_id in busy_ids})
        logger.info("[VOD-MERGE] busy with playback, not probing this run: %s",
                    ", ".join(stats["busy"]))
    per_movie = without_accounts(per_movie, busy_ids)
    per_movie = without_accounts(per_movie, set(skip_accounts or ()))

    # allow_oversized=False: here the limit is a SAFETY ceiling, not a target.
    chosen = take_whole_movies(per_movie, limit, allow_oversized=False)
    per_account = {}
    consecutive = {}
    deferred_errors = {}

    for rel in chosen:
        account = rel.m3u_account
        name = account.name
        if name in stats["broken"] or name in stats["busy"]:
            continue
        if heartbeat is not None and not heartbeat():
            stats["stopped"] = _stop_reason(heartbeat)
            logger.info("[VOD-MERGE] stream probe stopped early -- %s",
                        stats["stopped"])
            break
        # Checked before EVERY probe, not once per run: playback can start at
        # any point. A busy account is left alone for the rest of the run and
        # nothing is recorded against its copies -- they were not attempted.
        profile = _probe_profile(account, has_capacity)
        if profile is None:
            stats["busy"].append(name)
            logger.info("[VOD-MERGE] %r is busy with playback -- not probing "
                        "it this run", name)
            continue
        slot = per_account.setdefault(
            name, {"probed": 0, "gained": 0, "nothing": 0, "errors": 0})

        try:
            # Through the profile that was checked, so the connection we open
            # is the one whose slot was free.
            url = rel.get_stream_url(profile)
        except Exception:
            logger.exception("[VOD-MERGE] could not build stream URL rel=%s", rel.id)
            url = None
        if not url:
            stats["no_url"] += 1
            continue

        try:
            ua = account.get_user_agent_string()
        except Exception:
            ua = None

        rc, parsed = run_ffprobe(url, ua)
        outcome = classify_probe(rc, parsed)

        if outcome == "error":
            stats["errors"] += 1
            slot["errors"] += 1
            consecutive[name] = consecutive.get(name, 0) + 1
            # Hold the stamp. Repeated failure against one account is a fact
            # about the PROVIDER, not about each stream, and recording it per
            # stream would put every queued copy into a week-long retry window
            # because the provider had a bad hour.
            deferred_errors.setdefault(name, []).append(rel)
            if consecutive[name] >= PROBE_BREAKER:
                stats["broken"].append(name)
                stats.setdefault("broken_ids", []).append(rel.m3u_account_id)
                logger.warning(
                    "[VOD-MERGE] probe circuit breaker tripped for %r after %d "
                    "consecutive failures -- abandoning it for this run and "
                    "recording nothing against its streams", name, consecutive[name])
            if delay:
                time.sleep(delay)
            continue

        consecutive[name] = 0
        note_dv(stats, parsed, rel.id in dv_ids)
        props = rel.custom_properties or {}
        if outcome == "full":
            props = store_probe(props, parsed)
            stats["gained"] += 1
            slot["gained"] += 1
        else:
            stats["nothing"] += 1
            slot["nothing"] += 1
        stats["probed"] += 1
        slot["probed"] += 1
        _stamp_own(props, probe={"at": now_iso, "got": outcome})
        rel.custom_properties = props
        try:
            rel.save(update_fields=["custom_properties"])
        except Exception:
            logger.exception("[VOD-MERGE] could not store probe for rel=%s", rel.id)
        if delay:
            time.sleep(delay)

    # Only now do we know whether each account's failures were about the
    # streams or about the provider.
    for rel in errors_to_stamp(deferred_errors, stats["broken"]):
        props = rel.custom_properties or {}
        _stamp_own(props, probe={"at": now_iso, "got": "error"})
        rel.custom_properties = props
        try:
            rel.save(update_fields=["custom_properties"])
        except Exception:
            logger.exception(
                "[VOD-MERGE] could not store probe error for rel=%s", rel.id)

    stats["per_account"] = per_account
    logger.info("[VOD-MERGE] stream probe: %s", stats)
    return stats


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #
# The sweep is the only step that cannot happen inside a scan: the poster and
# plot tiers work off data the listing already carries, but the detail tier
# needs an HTTP call per relation, and doing hundreds of those mid-ingest would
# stall it. So it runs on its own nightly timer instead.
#
# Newest-first ordering means a bounded run always looks at new arrivals before
# anything else, which is what makes a provider that tags late (ids present in
# the detail immediately, in the listing about a week later) merge in a day or
# two rather than whenever the provider gets round to it. Any older backlog gets
# swept as a side effect and then stops being a candidate forever, so the cost
# is one-time rather than nightly.
#
# ROUTING: a plugin @shared_task sent to the default prefork queue is REJECTED
# -- that worker's consumer runs in the main process, which never imports
# plugins, so it sees an unregistered task name. The threads-pool 'dvr' worker
# does register them. Hence the queue override.

SWEEP_PERIODIC_NAME = "dispatcharr_vod_merge_movie_sweep"
SWEEP_TASK_PATH = "dispatcharr_vod_merge.run_movie_sweep"
DEFAULT_SCHEDULE_QUEUE = "dvr"
DEFAULT_SWEEP_HOUR = 4
DEFAULT_SCHEDULED_SWEEP = False   # opt-in, like everything else here

# Nightly enrichment: its own timer, separate from the merge sweep, because it
# is a separate feature with a separate cost and someone may want either alone.
ENRICH_PERIODIC_NAME = "dispatcharr_vod_merge_enrichment"
ENRICH_NIGHTLY_TASK_PATH = "dispatcharr_vod_merge.run_nightly_enrichment"
DEFAULT_ENRICH_NIGHTLY = False
DEFAULT_ENRICH_HOUR = 1
DEFAULT_ENRICH_MINUTES = 90
MAX_ENRICH_MINUTES = 720
# Safety cap on batches in one night, far above anything a time limit allows,
# so a bug that stops a batch from making progress cannot spin for ever.
MAX_NIGHTLY_BATCHES = 5000


@shared_task(name=SWEEP_TASK_PATH)
def run_movie_sweep():
    """Beat entry point. Runs UNBOUNDED, unlike the manual action.

    `sweep_limit` exists because the button runs inside a web request and a long
    run would time it out. Nothing constrains a nightly task that way, and
    bounding it would mean a large new category took a week to drain at 50 a
    night. The candidate set is already narrow -- id-less relations, no stored
    detail, and nothing a free signal reaches -- and it shrinks permanently as
    it is swept, so an unbounded run is self-limiting rather than open-ended.
    """
    try:
        return sweep_movies_impl(limit=0)
    except Exception as exc:
        logger.exception("[VOD-MERGE] scheduled movie sweep failed")
        return "Error: %s" % (exc,)


def ensure_schedule() -> bool:
    """Create/update the nightly beat task, or remove it when disabled.

    Idempotent and fail-open: a scheduling problem must never stop the plugin
    from doing the work it can do inline.
    """
    try:
        cfg = _load_config(force=True)
    except Exception as exc:
        logger.warning("[VOD-MERGE] could not read settings for the schedule: %s", exc)
        return False
    queue = cfg["schedule_queue"]
    sweep = _sync_periodic(SWEEP_PERIODIC_NAME, SWEEP_TASK_PATH,
                           cfg["scheduled_sweep"], cfg["sweep_hour"], queue,
                           "nightly movie detail sweep")
    enrich = _sync_periodic(ENRICH_PERIODIC_NAME, ENRICH_NIGHTLY_TASK_PATH,
                            cfg["enrich_nightly"] and cfg["enrich_movies"],
                            cfg["enrich_hour"], queue, "nightly enrichment")
    return sweep or enrich


def _sync_periodic(name, path, enabled, hour, queue, what) -> bool:
    """Create/update one beat task, or delete it when off. Each timer is
    handled on its own, so a problem with one never blocks the other."""
    try:
        if not enabled:
            _delete_periodic(name)
            return False

        from core.scheduling import create_or_update_periodic_task
        task = create_or_update_periodic_task(
            task_name=name,
            celery_task_path=path,
            cron_expression=f"0 {hour} * * *",
            enabled=True,
        )
        try:
            if task is not None and getattr(task, "queue", None) != queue:
                task.queue = queue
                task.save(update_fields=["queue"])
        except Exception as exc:
            logger.warning(
                "[VOD-MERGE] could not route %s to queue %r: %s", what, queue, exc)
        logger.info("[VOD-MERGE] scheduled %s at %02d:00 (system TZ) on queue %r",
                    what, hour, queue)
        return True
    except Exception as exc:
        logger.warning("[VOD-MERGE] could not schedule %s: %s", what, exc)
        return False


def _delete_periodic(name) -> bool:
    try:
        from core.scheduling import delete_periodic_task
        delete_periodic_task(name)
        return True
    except Exception as exc:
        logger.debug("[VOD-MERGE] could not remove beat task %s: %s", name, exc)
        return False


def remove_schedule() -> bool:
    """Remove BOTH timers (on disable)."""
    a = _delete_periodic(SWEEP_PERIODIC_NAME)
    b = _delete_periodic(ENRICH_PERIODIC_NAME)
    return a and b


# --------------------------------------------------------------------------- #
# Enrichment runs in the background, both steps in one run
# --------------------------------------------------------------------------- #
# One run does everything that can be done for the next batch: look up
# provider detail, then -- if measuring is on -- measure the copies the lookup
# could not describe. Lookup always goes first, because it is a light request
# and a measurement holds the provider connection for seconds.
#
# Neither step belongs inside a web request: a probe can hold a connection for
# up to PROBE_TIMEOUT_S, and a batch of lookups at about a second each gets
# close to a request timeout on its own. The button therefore only ENQUEUES;
# the work runs on the same queue as the nightly sweep, for the same routing
# reason.
#
# Exactly one run at a time, enforced by a Redis lock taken INSIDE the task
# rather than at the button: a check at enqueue time cannot stop two clicks
# from queuing two runs. On a provider that allows one connection, two runs
# at once collide with each other just as surely as with playback.
#
# The lock is short-lived and refreshed before every lookup and every probe,
# so a worker killed mid-run frees it within minutes instead of blocking
# enrichment for hours. Anything that stops us confirming we hold it -- Redis
# down, lock taken over -- stops the run: a missed run costs a night of
# enrichment, a collision can cost someone their stream.

ENRICH_TASK_PATH = "dispatcharr_vod_merge.run_enrichment"
RUN_LOCK_KEY = "dispatcharr_vod_merge:enrich_lock"


def run_lock_ttl(delay_ms) -> int:
    """Pure. Seconds the run lock lives between heartbeats.

    A heartbeat comes before every lookup and every probe, so the longest gap
    between two is one probe (the slower step) plus the spacing after it --
    pass the larger of the two delays. Double the probe timeout and add a
    minute of slack for the URL lookup and the save, so a slow-but-healthy run
    never lets its lock lapse.
    """
    try:
        delay_s = max(0, int(delay_ms)) / 1000.0
    except (TypeError, ValueError):
        delay_s = 0
    return int(PROBE_TIMEOUT_S * 2 + delay_s + 60)


def _as_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def acquire_run_lock(client, token, ttl) -> bool:
    return bool(client.set(RUN_LOCK_KEY, token, nx=True, ex=ttl))


def refresh_run_lock(client, token, ttl) -> bool:
    """Extend our lock. -> False if we cannot confirm we still hold it.

    A lock that lapsed while nobody else took it is re-taken, since nothing ran
    in between. One held by a different token means another run is active,
    and this one must stop.
    """
    try:
        held = _as_text(client.get(RUN_LOCK_KEY))
        if held is None:
            return acquire_run_lock(client, token, ttl)
        if held != token:
            return False
        client.expire(RUN_LOCK_KEY, ttl)
        return True
    except Exception as exc:
        logger.warning("[VOD-MERGE] could not refresh the enrichment run lock: %s", exc)
        return False


def release_run_lock(client, token) -> None:
    """Delete the lock only if it is still ours.

    Read-then-delete is not atomic, but the gap only matters if our lock lapsed
    and another run took it within that instant, which needs a heartbeat to
    have been missed by minutes first.
    """
    try:
        if _as_text(client.get(RUN_LOCK_KEY)) == token:
            client.delete(RUN_LOCK_KEY)
    except Exception as exc:
        logger.warning("[VOD-MERGE] could not release the enrichment run lock: %s", exc)


def run_locked(client, token, ttl, work):
    """-> (ran, result). Runs `work(heartbeat)` only while holding the lock.

    Pure apart from the client, so the lock discipline is testable with a fake:
    no lock, no work; the lock is released however `work` ends.
    """
    if not acquire_run_lock(client, token, ttl):
        return False, None
    try:
        return True, work(lambda: refresh_run_lock(client, token, ttl))
    finally:
        release_run_lock(client, token)


def _redis_client():
    from core.utils import RedisClient
    client = RedisClient.get_client()
    if client is None:
        raise RuntimeError("Redis is unavailable")
    return client


def enrich_run_impl(probe_enabled, lookup, measure, heartbeat=None,
                    skip_accounts=None):
    """-> {"lookup": stats, "measure": stats?}. Lookup first, then measuring.

    The steps are passed in so the ordering and the stop conditions are
    testable without Django. Measuring is skipped when it is off, when the
    lookup declined to run at all (an untrustworthy wanted set must stop BOTH
    steps -- fail closed on scope), and when the lookup was stopped early.
    """
    out = {"lookup": lookup(heartbeat=heartbeat)}
    if not probe_enabled:
        return out
    if out["lookup"].get("aborted") or out["lookup"].get("stopped"):
        return out
    out["measure"] = measure(heartbeat=heartbeat, skip_accounts=skip_accounts)
    return out


class RunGuard:
    """Called before every lookup and every probe; True means carry on.

    Keeps the run lock alive and, for a nightly run, enforces the time limit.
    Remembers WHY it said stop, so the record can tell "time limit reached"
    (expected, every night with a backlog) from "lost the run lock" (not).
    """

    def __init__(self, refresh, deadline=None, clock=time.time):
        self.refresh, self.deadline, self.clock = refresh, deadline, clock
        self.reason = None

    def time_left(self):
        return self.deadline is None or self.clock() < self.deadline

    def __call__(self):
        if not self.time_left():
            self.reason = "time limit reached"
            return False
        if not self.refresh():
            self.reason = "lost the run lock"
            logger.warning("[VOD-MERGE] enrichment lost its run lock -- another "
                           "run may be active, so this one is stopping")
            return False
        return True


def _stop_reason(heartbeat):
    return getattr(heartbeat, "reason", None) or "lost the run lock"


_LOOKUP_SUMS = ("fetched", "got_essentials", "empty", "errors")
_MEASURE_SUMS = ("probed", "gained", "nothing", "errors", "no_url",
                 "dv_checked", "dv_found", "dv_no_fallback")


def _attempted(result):
    """Copies a batch actually tried. Not `no_url`: those are never stamped,
    so counting them as progress would let a run re-pick them for ever."""
    look = result.get("lookup") or {}
    meas = result.get("measure") or {}
    return (sum(look.get(k, 0) for k in ("fetched", "empty", "errors"))
            + meas.get("probed", 0) + meas.get("errors", 0))


def run_batches(batch, time_left, max_batches=MAX_NIGHTLY_BATCHES):
    """Repeat `batch(skip_accounts)` for a nightly run. -> totals with `ended`.

    Pure apart from `batch`. Stops when the time is up, when a batch made no
    progress (backlog done, or all that is left is on busy providers -- the
    status says which), when anything stopped a batch early, or when the
    wanted set was refused. Providers whose breaker tripped stay skipped for
    the rest of the night.
    """
    totals = {"lookup": {k: 0 for k in _LOOKUP_SUMS},
              "measure": {k: 0 for k in _MEASURE_SUMS},
              "batches": 0}
    busy, broken, skip = set(), set(), set()
    measured = False
    ended = "safety limit on batches reached"
    for _ in range(max_batches):
        if not time_left():
            ended = "time limit reached"
            break
        res = batch(set(skip))
        totals["batches"] += 1
        look = res.get("lookup") or {}
        meas = res.get("measure")
        for k in _LOOKUP_SUMS:
            totals["lookup"][k] += look.get(k, 0)
        if meas is not None:
            measured = True
            for k in _MEASURE_SUMS:
                totals["measure"][k] += meas.get(k, 0)
            busy.update(meas.get("busy") or ())
            broken.update(meas.get("broken") or ())
            skip.update(meas.get("broken_ids") or ())
        if look.get("aborted"):
            totals["lookup"]["aborted"] = look["aborted"]
            ended = "nothing done -- %s" % look["aborted"]
            break
        stopped = look.get("stopped") or (meas or {}).get("stopped")
        if stopped:
            ended = stopped
            break
        if not _attempted(res):
            # Judged on THIS batch: a provider busy at 01:00 and free by 02:00
            # must not make a finished backlog read as blocked.
            blocked = (meas or {}).get("busy") or skip
            ended = ("the rest is on providers that are busy or not answering"
                     if blocked else "nothing left to try")
            break
    if not measured:
        totals.pop("measure")
    else:
        totals["measure"]["busy"] = sorted(busy)
        totals["measure"]["broken"] = sorted(broken)
    totals["ended"] = ended
    return totals


# Rough per-copy costs for the nights estimate: a lookup is one light request
# (about a second with the default spacing), a measurement a few seconds of
# reading. Spacing settings are added on top. An estimate to disclose the size
# of a backlog, not a promise.
LOOKUP_EST_S = 1.0
PROBE_EST_S = 6.0


def estimate_nights(need_lookup, need_measure, cfg):
    """Pure. Nights the current backlog needs at the current settings, or None
    when nightly enrichment is off. Rounded up; 0 when nothing is left."""
    if not cfg.get("enrich_nightly"):
        return None
    secs = need_lookup * (LOOKUP_EST_S + cfg.get("enrich_delay_ms", 0) / 1000.0)
    if cfg.get("probe_movies"):
        secs += need_measure * (PROBE_EST_S + cfg.get("probe_delay_ms", 0) / 1000.0)
    window = max(1, cfg.get("enrich_minutes") or DEFAULT_ENRICH_MINUTES) * 60
    return int(-(-secs // window))


def _run_enrichment(nightly=False):
    """Shared body of the manual and nightly runs: one lock, one record."""
    import uuid
    from django.utils import timezone

    started = timezone.now().isoformat()
    try:
        client = _redis_client()
        cfg = _load_config(force=True)
        ttl = run_lock_ttl(max(cfg["probe_delay_ms"], cfg["enrich_delay_ms"]))
        deadline = (time.time() + cfg["enrich_minutes"] * 60) if nightly else None

        def work(refresh):
            guard = RunGuard(refresh, deadline=deadline)
            write_run_status({"state": "running", "started_at": started,
                              "nightly": nightly})

            def one_batch(skip_accounts=None):
                return enrich_run_impl(cfg["probe_movies"], enrich_movies_impl,
                                       probe_movies_impl, heartbeat=guard,
                                       skip_accounts=skip_accounts)

            if not nightly:
                return one_batch()
            return run_batches(one_batch, guard.time_left)

        ran, stats = run_locked(client, uuid.uuid4().hex, ttl, work)
    except Exception as exc:
        logger.exception("[VOD-MERGE] enrichment run failed")
        write_run_status({"state": "failed", "started_at": started,
                          "finished_at": timezone.now().isoformat(),
                          "nightly": nightly, "error": str(exc)})
        return "Error: %s" % (exc,)
    if not ran:
        # Deliberately leaves the status record alone: it describes the run
        # that IS in progress, and overwriting it would hide that run.
        logger.info("[VOD-MERGE] enrichment not started -- another run is "
                    "in progress")
        return {"skipped": "another enrichment run is in progress"}
    logger.info("[VOD-MERGE] enrichment run%s: %s",
                " (nightly)" if nightly else "", stats)
    write_run_status({"state": "finished", "started_at": started,
                      "finished_at": timezone.now().isoformat(),
                      "nightly": nightly, "result": stats})
    return stats


@shared_task(name=ENRICH_TASK_PATH)
def run_enrichment():
    """Background entry point for 'Enrich now': one batch, each step bounded
    by its per-run limit."""
    return _run_enrichment(nightly=False)


@shared_task(name=ENRICH_NIGHTLY_TASK_PATH)
def run_nightly_enrichment():
    """Beat entry point: batches until the backlog is done or the time limit.

    Re-reads the switch when it fires. The timer itself is only rewritten on
    Enable or a restart, so without this check turning the feature off would
    not take effect until then.
    """
    cfg = _load_config(force=True)
    if not (cfg["enrich_nightly"] and cfg["enrich_movies"]):
        logger.info("[VOD-MERGE] nightly enrichment fired but is switched off "
                    "-- doing nothing")
        return {"skipped": "nightly enrichment is off"}
    return _run_enrichment(nightly=True)


def enqueue_enrichment(cfg=None):
    """-> (started, message). Queues an enrichment run; NEVER works inline.

    The busy check here only produces a better message. The task re-checks
    under its own lock, which is what actually prevents overlap.
    """
    cfg = cfg or _load_config(force=True)
    if not cfg["enrich_movies"]:
        return False, "enrichment is off"
    try:
        if _redis_client().exists(RUN_LOCK_KEY):
            return False, "an enrichment run is already in progress"
    except Exception:
        pass
    queue = cfg["schedule_queue"]
    run_enrichment.apply_async(queue=queue)
    return True, "started on queue %r" % (queue,)


# A first run against a large untagged provider can produce thousands of
# actionable entries. All of them belong in the JSON file -- that is what the UI
# message points at -- but shipping the whole list back inside the action's API
# response bloats it for no benefit, since the popup only ever shows a sample.
# So: write the file first, then trim the copy that goes back to the caller.
RESPONSE_MATCH_CAP = 200


def trim_matches(report, cap: int = RESPONSE_MATCH_CAP):
    """Shallow copy of `report` with `matches` truncated for the API response.

    Pure, and deliberately non-mutating: the caller has already written the full
    report to disk by this point, and reusing the same dict would truncate the
    thing it just wrote if that ever changed order.
    """
    matches = (report or {}).get("matches")
    if not isinstance(matches, list) or cap <= 0 or len(matches) <= cap:
        return report
    trimmed = dict(report)
    trimmed["matches"] = matches[:cap]
    trimmed["matches_total"] = len(matches)
    trimmed["matches_truncated"] = True
    return trimmed


# --------------------------------------------------------------------------- #
# Protection: core's destructive movie merge
# --------------------------------------------------------------------------- #
# `handle_movie_id_conflicts` (apps/vod/tasks.py) runs when an on-demand
# `refresh_movie_advanced_data` finds a tmdb in the provider's detail that
# already belongs to a DIFFERENT Movie row. Its policy is to keep the movie being
# refreshed and DELETE the pre-existing one, which is backwards on this path: the
# movie being refreshed is the freshly-minted orphan (the listing had no id, so
# it keyed by name+year and `lookup_by_name_year` cannot see the tmdb-bearing
# canonical), while the row it deletes is the canonical holding most of the
# relations, the better name, and the uuid downstream tools have indexed.
#
# It can also abort part-way. To dodge the partial unique index it first nulls
# the canonical's tmdb in a standalone write with no surrounding transaction; if
# any id-less row already occupies that (name, year) -- which is exactly the
# duplicate being healed whenever the names match -- Postgres rejects it, the
# error is swallowed into a return string, and `detailed_fetched` never flips, so
# the relation re-fetches and re-fails forever.
#
# We replace it with the direction the CALLER already implements: repoint the
# relation onto the existing row and return True. `refresh_movie_advanced_data`
# has an `if relation_updated:` branch for exactly this, unreachable today only
# because the function returns False on every path.
#
# DELIBERATELY MINIMAL -- we do NOT call core's `merge_movie_data`. Three
# reasons. The canonical is by construction the better-populated row, so there is
# little to copy up. The orphan's listing-derived content is already stored on
# the relation itself. And `merge_movie_data` writes ids: `tmdb_id`/`imdb_id` are
# BOTH `unique=True`, and its
# `elif source_movie.imdb_id: target_movie.imdb_id = source_movie.imdb_id`
# branch saves the target while the source still holds that same unique imdb --
# a collision of its own, separate from the two defects above. A single FK update
# writes no ids at all, so no uniqueness constraint is reachable and the whole
# IntegrityError class disappears rather than being handled.
#
# Note `relation` is an UNUSED parameter in core's version; ours needs it, so a
# missing one degrades to "do nothing" rather than to core's deletion.

PROTECT_NOOP = "noop"          # nothing conflicts -- let core proceed untouched
PROTECT_SAME = "same"          # the holder IS the current row; core no-ops too
PROTECT_REPOINT = "repoint"    # another row holds it -- repoint, do not delete

DEFAULT_PROTECT_MERGES = True  # a data-loss bug; on unless deliberately disabled


def decide_conflict(current_id, tmdb_holder_id, imdb_holder_id, has_relation):
    """Pure: what to do about an id conflict. Returns (action, target_id).

    Mirrors core's preference for the TMDB match over the IMDB one. Pure so the
    decision is testable without a database; the caller does the queries.
    """
    holder = tmdb_holder_id if tmdb_holder_id is not None else imdb_holder_id
    if holder is None:
        return PROTECT_NOOP, None
    if holder == current_id:
        return PROTECT_SAME, None
    if not has_relation:
        # No relation to repoint. Core would delete the canonical here; doing
        # nothing is strictly safer, and the next refresh retries.
        return PROTECT_NOOP, None
    return PROTECT_REPOINT, holder


def patched_handle_movie_id_conflicts(current_movie, relation,
                                      tmdb_id_to_set, imdb_id_to_set,
                                      *args, **kwargs):
    """Non-destructive replacement for core's `handle_movie_id_conflicts`.

    This is the one wrapper here that REPLACES core rather than wrapping it:
    when protection is on, the original never runs. So it carries the most
    signature risk of the four -- any parameter core grows is one that NOTHING
    honors unless this function is taught to.

    ⚠ The instinct for a replace-shaped wrapper is "when in doubt, defer to
    core". That is WRONG here, because core's behavior on this path is the bug
    we exist to prevent: deferring on an argument we do not recognize would
    reinstate the destructive merge. So we keep protecting and log loudly
    instead -- see `_note_unexpected_args`. The extras are still forwarded on
    the one path that does delegate, below.
    """
    _note_unexpected_args("handle_movie_id_conflicts", args, kwargs)

    try:
        cfg = _load_config()
        if not cfg["protect_merges"]:
            if _orig_handle_movie_id_conflicts is not None:
                return _orig_handle_movie_id_conflicts(
                    current_movie, relation, tmdb_id_to_set, imdb_id_to_set,
                    *args, **kwargs)
            return current_movie, False

        # Imported here rather than at the top of the function for two reasons:
        # the delegate path above needs neither, and keeping them INSIDE the
        # try means the fail-closed promise below actually covers them. An
        # ImportError used to escape the very handler that exists to guarantee
        # we never fall through to core's destructive version.
        from django.db import transaction
        from apps.vod.models import Movie

        def holder(field, value):
            if not value:
                return None
            row = Movie.objects.filter(**{field: value}).values("id").first()
            return row["id"] if row else None

        action, target_id = decide_conflict(
            getattr(current_movie, "id", None),
            holder("tmdb_id", tmdb_id_to_set),
            holder("imdb_id", imdb_id_to_set),
            getattr(relation, "id", None) is not None,
        )

        if action != PROTECT_REPOINT:
            return current_movie, False

        target = Movie.objects.filter(id=target_id).first()
        if target is None:                       # raced away; let core's caller
            return current_movie, False          # try again next refresh

        with transaction.atomic():
            relation.movie = target
            relation.save(update_fields=["movie"])

        logger.info(
            "[VOD-MERGE] prevented destructive merge: kept canonical movie %s "
            "%r, repointed relation %s off orphan movie %s %r",
            target.id, target.name, relation.id,
            getattr(current_movie, "id", None),
            getattr(current_movie, "name", None),
        )
        try:
            _append_log([{
                "ts": time.time(),
                "action": PROTECTED,
                "tier": TIER_PROTECT,
                "kind": "movie",
                "account": getattr(
                    getattr(relation, "m3u_account", None), "name", None),
                "stream_id": str(getattr(relation, "stream_id", "") or "") or None,
                "name": getattr(current_movie, "name", None),
                "tmdb_id": tmdb_id_to_set or imdb_id_to_set,
                "previous_movie_id": getattr(current_movie, "id", None),
                "canonical_id": target.id,
                "canonical_uuid": str(target.uuid) if getattr(target, "uuid", None) else None,
                "creates_new_row": False,
            }])
        except Exception:
            logger.exception("[VOD-MERGE] could not log a prevented merge")

        return target, True

    except Exception:
        # Fail CLOSED on this one, unlike the injection path: returning
        # (current, False) makes core set the id itself, which is the benign
        # branch. Never fall through to core's destructive version on error.
        logger.exception(
            "[VOD-MERGE] merge protection failed; taking no action")
        return current_movie, False


# --------------------------------------------------------------------------- #
# Protection: essential detail dropped by a core refresh
# --------------------------------------------------------------------------- #
# `refresh_movie_advanced_data` stores the provider's detail payload with
# `relation_custom_props['detailed_info'] = cleaned_info` -- a WHOLE-DICT
# replacement of that sub-payload (v0.30.0 apps/vod/tasks.py:2361). Other
# top-level keys survive, ours included, because it mutates the surrounding
# dict; but everything previously inside `detailed_info` is gone unless the new
# payload repeats it.
#
# Providers are not consistent between calls. One that returned a full ffprobe
# block on Monday can return plot and genre only on Tuesday, and the second
# response silently erases the first. Three keys are worth more than the
# payload they arrive in:
#
#   tmdb_id  -- the detail tier's entire signal. Lose it and a movie this
#               plugin could have merged goes back to unmergeable, with no
#               error anywhere.
#   video    -- resolution and codec, the ground truth for quality ranking.
#   audio    -- codec and channel count, which has no fallback at all
#               downstream: absent means "no opinion", so a dropped audio
#               block does not degrade a comparison, it silently removes one.
#
# We therefore let core write whatever it likes and then put back only the
# essential keys its write DROPPED. The provider still wins wherever it
# supplied a value -- this never overwrites real data with older data, it only
# refills a hole.
#
# Note this is the mirror image of the reason we stopped writing core's fields
# above. There, core's field is the signal and we must not touch it. Here,
# core's write is the hazard and we repair after it. Both follow from the same
# rule about not confusing the two writers.
#
# Cost is near zero in the case that motivated it: a relation with no stored
# detail has nothing to lose, so a bulk pass over fresh relations pays one
# extra SELECT each and never writes.

ESSENTIAL_DETAIL_KEYS = ("tmdb_id", "video", "audio")

DEFAULT_PRESERVE_DETAIL = True


def _is_blank_detail_value(value) -> bool:
    """Blank in the sense core's own cleaner uses -- plus the empty dict.

    `clean_custom_properties` drops None, '' and [], so those never reach
    storage. It does NOT drop `{}`, so an empty `video` block survives the
    clean and lands looking like data. Treat it as absent too, or the guard
    would consider a hole already filled.
    """
    if value is None or value == "" or value == [] or value == {}:
        return True
    if isinstance(value, list) and all(v is None or v == "" for v in value):
        return True
    return False


def _is_blank_essential(key, value) -> bool:
    if _is_blank_detail_value(value):
        return True
    # Providers spell "no id" several ways; the detail tier already knows them.
    return key == "tmdb_id" and str(value).strip().lower() in _BLANK_IDS


def restore_essential(old_detail, new_detail):
    """Carry essential keys a fresh detail payload dropped back over it.

    Returns `(detail, restored_keys)`. Pure, so the policy is testable with no
    database: the caller does the reads and the write.
    """
    old = old_detail if isinstance(old_detail, dict) else {}
    new = new_detail if isinstance(new_detail, dict) else {}
    if not old:
        return new, []

    merged = dict(new)
    restored = []
    for key in ESSENTIAL_DETAIL_KEYS:
        was = old.get(key)
        if _is_blank_essential(key, was):
            continue                      # nothing worth keeping to begin with
        if not _is_blank_essential(key, merged.get(key)):
            continue                      # the provider supplied one; it wins
        merged[key] = was
        restored.append(key)
    return merged, restored


def _read_stored_detail(relation_id):
    """The relation's current `detailed_info`, or None.

    A separate query, so the dict returned here is a separate object from the
    one core loads and mutates -- no aliasing, no copy needed.
    """
    from apps.vod.models import M3UMovieRelation

    props = (
        M3UMovieRelation.objects.filter(id=relation_id)
        .values_list("custom_properties", flat=True).first()
    )
    detail = (props or {}).get("detailed_info")
    return detail if isinstance(detail, dict) else None


def _restore_dropped_detail(relation_id, before) -> list:
    """Put back any essential key core's write dropped. Returns what it put."""
    from apps.vod.models import M3UMovieRelation

    if not before:
        return []

    rel = (
        M3UMovieRelation.objects.filter(id=relation_id)
        .select_related("m3u_account", "movie").first()
    )
    if rel is None:
        return []

    props = rel.custom_properties or {}
    after = props.get("detailed_info")
    if not isinstance(after, dict):
        # Core skipped, or its payload was empty and it left ours in place.
        return []

    merged, restored = restore_essential(before, after)
    if not restored:
        return []

    props["detailed_info"] = merged
    rel.custom_properties = props
    rel.save(update_fields=["custom_properties"])

    logger.info(
        "[VOD-MERGE] restored detail dropped by a core refresh: rel=%s keys=%s",
        relation_id, restored,
    )
    try:
        _append_log([{
            "ts": time.time(),
            "action": PRESERVED,
            "tier": TIER_PRESERVE,
            "kind": "movie",
            "account": getattr(
                getattr(rel, "m3u_account", None), "name", None),
            "stream_id": str(getattr(rel, "stream_id", "") or "") or None,
            "name": getattr(getattr(rel, "movie", None), "name", None),
            "tmdb_id": merged.get("tmdb_id"),
            "restored": restored,
            "creates_new_row": False,
        }])
    except Exception:
        logger.exception("[VOD-MERGE] could not log a restored detail payload")
    return restored


def patched_refresh_movie_advanced_data(m3u_movie_relation_id,
                                        force_refresh=False, *args, **kwargs):
    """Run core's refresh, then repair the essential keys its write dropped.

    Fails OPEN, and deliberately not like `patched_handle_movie_id_conflicts`.
    That one fails closed because the thing it replaces is destructive, so
    falling through would do the damage. Here there is nothing destructive to
    fall through to: core's refresh is a feature a user is waiting on, and a
    fault in our guard must not cost them the fetch. The original is called
    exactly once whatever happens, and its return value is passed back
    untouched.

    Signature risk sits in the middle of the four. Core still runs, so a new
    parameter is honored by core -- but this wrapper REPAIRS core's write
    afterwards, and a parameter that made core deliberately write LESS (a
    partial or lightweight refresh, say) would have us refilling keys core
    meant to omit, fighting its intent rather than repairing an accident. We
    cannot currently tell "the provider dropped this" from "core chose not to
    write it", so `_note_unexpected_args` flags the case for a human.
    """
    original = _orig_refresh_movie_advanced_data
    if original is None:                 # not installed; nothing to delegate to
        return None

    _note_unexpected_args("refresh_movie_advanced_data", args, kwargs)

    before = None
    guard = False
    try:
        guard = _load_config()["preserve_detail"]
        if guard:
            before = _read_stored_detail(m3u_movie_relation_id)
    except Exception:
        logger.exception(
            "[VOD-MERGE] could not snapshot detail for rel=%s; the refresh "
            "proceeds unguarded", m3u_movie_relation_id)
        guard = False

    # force_refresh passed POSITIONALLY on purpose: if core grows a third
    # parameter, `*args` then lands in the right slot. Passing it by keyword
    # ahead of `*args` would collide the moment a positional extra appeared.
    result = original(m3u_movie_relation_id, force_refresh, *args, **kwargs)

    if guard and before:
        try:
            _restore_dropped_detail(m3u_movie_relation_id, before)
        except Exception:
            logger.exception(
                "[VOD-MERGE] could not restore dropped detail for rel=%s",
                m3u_movie_relation_id)
    return result


# --------------------------------------------------------------------------- #
# Protection: a catalog-wide delete triggered by an empty listing
# --------------------------------------------------------------------------- #
# `refresh_vod_content` ends every scan with
# `cleanup_orphaned_vod_content(account_id=..., scan_start_time=...)`, and that
# runs with `stale_days=0`, so the cutoff is the scan's own start: any relation
# not re-stamped DURING the scan is stale and is deleted. Then a second,
# DELIBERATELY UNSCOPED pass deletes every Movie left with no relations from any
# account.
#
# That is correct only while "the scan did not see it" implies "the provider no
# longer has it". When a provider's movie endpoint returns an EMPTY list, the
# scan sees nothing, so every relation on the account is stale and the whole
# catalog goes -- along with every title that account was the sole source for.
# The rows come back on the next good scan with NEW primary keys, which is an id
# churn for every downstream consumer.
#
# Core guards the equivalent case one layer up: `refresh_categories` returning
# nothing aborts the refresh "to preserve existing category selections". The
# same reasoning was never applied to the movie and series lists.
#
# Observed live: an empty movie listing deleted an entire account's movie
# relations and thousands of Movie rows in one scheduled refresh, while series
# in the same run processed normally.
#
# We do NOT try to repair or second-guess the cleanup -- we refuse it outright
# for that scan and let the next one do it. A deferred cleanup costs some stale
# rows for a day; the alternative costs the catalog.

DEFAULT_PROTECT_PRUNE = True


def decide_prune(counts):
    """Pure: should core's cleanup be allowed to run? -> (allow, reason).

    `counts` maps a content type to `(total, seen)` for one account, where
    `seen` is the number of that account's relations that would SURVIVE core's
    staleness filter. Refuse when a content type has relations and the scan
    re-stamped none of them, because that is the empty-listing signature and
    nothing else produces it.

    Deliberately ZERO-seen only, not a ratio. A ratio threshold would also catch
    a listing that came back 90% short, but a genuinely shrinking catalog
    would then be refused for ever -- the stale rows keep the total high, so the
    ratio never recovers and the cleanup deadlocks. Zero-seen has no such state:
    one good scan clears it. The short-listing case is left to upstream.
    """
    for kind in sorted(counts):
        total, seen = counts[kind]
        if total and not seen:
            return False, "%s: %d relations exist, none seen in this scan" % (
                kind, total)
    return True, None


def _prune_counts(account_id, cutoff):
    """(total, surviving) relation counts per content type for one account."""
    from apps.vod.models import M3UMovieRelation, M3USeriesRelation

    counts = {}
    for kind, model in (("movie", M3UMovieRelation), ("series", M3USeriesRelation)):
        qs = model.objects.filter(m3u_account_id=account_id)
        total = qs.count()
        counts[kind] = (total, qs.filter(last_seen__gte=cutoff).count() if total else 0)
    return counts


def patched_cleanup_orphaned_vod_content(stale_days=0, scan_start_time=None,
                                         account_id=None, *args, **kwargs):
    """Refuse core's cleanup when the scan that preceded it saw nothing.

    Only the per-account, per-scan call is assessed -- the path the failure
    occurs on. An unscoped or timestamp-less call cannot be evaluated against a
    scan at all, so it is delegated untouched.

    Fails OPEN, unlike `patched_handle_movie_id_conflicts`. That one replaces
    core because core's behavior there IS the bug; here core's cleanup is
    normally correct and only catastrophic under one condition, so an error in
    our own check is not evidence that condition holds. Permanently disabling a
    correct cleanup on a bug of ours would be its own slow data problem, and the
    counts we need are two trivial queries -- if those fail, core's much larger
    ones were not going to succeed either.
    """
    _note_unexpected_args("cleanup_orphaned_vod_content", args, kwargs)

    original = _orig_cleanup_orphaned_vod_content
    if original is None:
        return None

    def _delegate():
        return original(stale_days, scan_start_time, account_id, *args, **kwargs)

    try:
        if not _load_config()["protect_prune"]:
            return _delegate()
        if account_id is None or scan_start_time is None:
            return _delegate()

        from datetime import timedelta

        # Mirror core's own cutoff exactly, so "seen" means precisely "would
        # survive core's filter" rather than an approximation of it.
        cutoff = scan_start_time - timedelta(days=stale_days or 0)
        counts = _prune_counts(account_id, cutoff)
        allow, reason = decide_prune(counts)
        if allow:
            return _delegate()
    except Exception:
        logger.exception(
            "[VOD-MERGE] prune guard could not evaluate account %s; allowing "
            "core cleanup to proceed", account_id)
        return _delegate()

    logger.warning(
        "[VOD-MERGE] REFUSED core VOD cleanup for account %s -- %s. An empty "
        "provider listing would have deleted the account's relations and every "
        "title it solely supplies. Cleanup will run on the next scan that sees "
        "content.", account_id, reason,
    )
    try:
        _append_log([{
            "ts": time.time(),
            "action": PRUNE_BLOCKED,
            "tier": TIER_PRUNE,
            "kind": "movie",
            "account": str(account_id),
            "stream_id": None,
            "name": None,
            "tmdb_id": None,
            "reason": reason,
            "counts": {k: list(v) for k, v in sorted(counts.items())},
            "creates_new_row": False,
        }])
    except Exception:
        logger.exception("[VOD-MERGE] could not log a refused cleanup")

    # Returned as core's own result string, so the refusal appears in core's
    # `VOD cleanup completed: ...` line even where our logger is silenced.
    return ("Skipped by dispatcharr_vod_merge: %s -- refusing to delete an "
            "entire catalog after a scan that saw nothing" % reason)


def _refresh_binding_sites():
    """Every module namespace holding its own reference to core's refresh.

    Module-attribute patching only reaches a caller that looks the name up at
    CALL time. `apps/output/views.py` imports it inside the function, so it
    does. `apps/vod/api_views.py` imports it at module level, binding whatever
    the attribute pointed at when api_views was first imported -- so patching
    `apps.vod.tasks` alone reaches that caller only when api_views is imported
    after us. At boot it is, because the URLconf loads lazily; on an
    enable-without-restart it is not, and the UI movie-open path would keep
    calling the unguarded original.

    So patch both. api_views is only touched when already imported: importing
    it ourselves during startup would drag the whole API layer in early, for a
    module that is about to be imported anyway.
    """
    import sys
    from apps.vod import tasks as vod_tasks

    sites = [vod_tasks]
    api_views = sys.modules.get("apps.vod.api_views")
    if api_views is not None and hasattr(api_views,
                                         "refresh_movie_advanced_data"):
        sites.append(api_views)
    return sites


def _plugin_file(name: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def _write_json_file(name: str, payload) -> str:
    """Dump a report beside the plugin. The UI truncates long action output, so
    anything worth reading in full gets a file too."""
    path = _plugin_file(name)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True, default=str)
    except Exception:
        logger.exception("[VOD-MERGE] could not write %s", name)
    return path


def _debug_file_path() -> str:
    return _plugin_file("series_status.json")


def write_series_status_file(payload) -> str:
    return _write_json_file("series_status.json", payload)


def write_log_file(payload) -> str:
    return _write_json_file("injection_log.json", payload)


def write_movie_status_file(payload) -> str:
    return _write_json_file("movie_status.json", payload)


# The last enrichment run lives in its own CoreSettings row, NOT in a file beside
# the plugin: uploading a new zip replaces the plugin folder's contents, so a
# file there is erased by every upgrade -- which is how the first version of
# this lost its record.
RUN_STATUS_KEY = "dispatcharr_vod_merge_enrich_run"
RUN_STATUS_NAME = "Dispatcharr VOD Merge - last enrichment run"


def write_run_status(payload) -> None:
    try:
        from core.models import CoreSettings
        CoreSettings.objects.update_or_create(
            key=RUN_STATUS_KEY,
            defaults={"name": RUN_STATUS_NAME,
                      "value": json.loads(json.dumps(payload, default=str))})
    except Exception:
        # A missing report is not worth failing a run over.
        logger.exception("[VOD-MERGE] could not record the enrichment run")


def read_run_status():
    """The last background enrichment run, or None if there has not been one."""
    try:
        from core.models import CoreSettings
        row = (CoreSettings.objects.filter(key=RUN_STATUS_KEY)
               .values("value").first())
    except Exception:
        return None
    val = (row or {}).get("value")
    return val if isinstance(val, dict) else None


# --------------------------------------------------------------------------- #
# Install / uninstall
# --------------------------------------------------------------------------- #

def _wrap(module, attr_name, replacement):
    """Swap `module.attr_name` for `replacement`, returning the original.

    Idempotent: re-installing over an already-installed wrapper keeps the FIRST
    original, so a reload cannot end up wrapping a wrapper.
    """
    current = getattr(module, attr_name)
    original = None if getattr(current, _PATCH_TAG, False) else current
    # Nothing in core dispatches these via Celery today, but keep `.delay` /
    # `.apply_async` reachable through the wrapper in case that ever changes.
    for extra in ("delay", "apply_async", "name"):
        if hasattr(current, extra) and not hasattr(replacement, extra):
            try:
                setattr(replacement, extra, getattr(current, extra))
            except Exception:
                pass
    setattr(replacement, _PATCH_TAG, True)
    setattr(module, attr_name, replacement)
    return original


def install(manage_schedule=None) -> bool:
    """Wrap the series and movie batch processors. Idempotent and reload-safe.

    manage_schedule: True -> also create/refresh the beat schedule (on enable);
    None -> only touch it in the master process at import, so a restart
    re-establishes it without every worker racing on the same row.
    """
    global _orig_process_series_batch, _orig_process_movie_batch
    global _orig_handle_movie_id_conflicts, _orig_refresh_movie_advanced_data
    global _orig_cleanup_orphaned_vod_content
    global _ACTIVE

    try:
        from apps.vod import tasks as vod_tasks
    except Exception as exc:
        logger.error("[VOD-MERGE] could not import apps.vod.tasks: %s", exc)
        return False

    for fn in ("process_series_batch", "process_movie_batch"):
        if not hasattr(vod_tasks, fn):
            logger.error("[VOD-MERGE] tasks.%s missing -- not patching.", fn)
            return False

    try:
        original = _wrap(vod_tasks, "process_series_batch", patched_process_series_batch)
        if original is not None:
            _orig_process_series_batch = original
        original = _wrap(vod_tasks, "process_movie_batch", patched_process_movie_batch)
        if original is not None:
            _orig_process_movie_batch = original

        # Separate from the batch wrappers, and separately reported: this one
        # matters in the uWSGI REQUEST workers (both callers of
        # refresh_movie_advanced_data are inline), whereas the batch wrappers
        # matter in the Celery children that run the scan. Absent = no
        # protection rather than broken merging, so it must not fail install.
        protect = False
        if hasattr(vod_tasks, "handle_movie_id_conflicts"):
            original = _wrap(vod_tasks, "handle_movie_id_conflicts",
                             patched_handle_movie_id_conflicts)
            if original is not None:
                _orig_handle_movie_id_conflicts = original
            protect = True
        else:
            logger.warning(
                "[VOD-MERGE] tasks.handle_movie_id_conflicts missing -- merge "
                "protection NOT installed (upstream may have changed it)")

        # Same treatment, and the same reason it must not fail the install:
        # absent = detail is unguarded, not = merging is broken. Patched in
        # every namespace that binds it, which is more than one.
        preserve = 0
        if hasattr(vod_tasks, "refresh_movie_advanced_data"):
            for site in _refresh_binding_sites():
                original = _wrap(site, "refresh_movie_advanced_data",
                                 patched_refresh_movie_advanced_data)
                if original is not None:
                    _orig_refresh_movie_advanced_data = original
                preserve += 1
        else:
            logger.warning(
                "[VOD-MERGE] tasks.refresh_movie_advanced_data missing -- "
                "detail preservation NOT installed (upstream may have "
                "changed it)")

        # Third protective patch, same treatment: absent means the catalog is
        # unguarded against an empty listing, not that merging is broken.
        # Single call site, in this module, resolved as a global at call time --
        # so unlike `refresh_movie_advanced_data` this needs no binding-site
        # sweep.
        prune = False
        if hasattr(vod_tasks, "cleanup_orphaned_vod_content"):
            original = _wrap(vod_tasks, "cleanup_orphaned_vod_content",
                             patched_cleanup_orphaned_vod_content)
            if original is not None:
                _orig_cleanup_orphaned_vod_content = original
            prune = True
        else:
            logger.warning(
                "[VOD-MERGE] tasks.cleanup_orphaned_vod_content missing -- "
                "empty-listing protection NOT installed (upstream may have "
                "changed it)")

        _ACTIVE = True
        logger.info(
            "[VOD-MERGE] installed series + movie batch wrappers%s%s%s in pid=%s",
            " + destructive-merge protection" if protect else "",
            " + detail preservation (%d binding site%s)" % (
                preserve, "" if preserve == 1 else "s") if preserve else "",
            " + empty-listing protection" if prune else "",
            os.getpid(),
        )
    except Exception as exc:
        logger.exception("[VOD-MERGE] install failed: %s", exc)
        uninstall()
        return False

    try:
        want = manage_schedule
        if want is None:
            try:
                from dispatcharr.app_initialization import should_skip_initialization
                want = not should_skip_initialization()
            except Exception:
                want = False
        if want:
            ensure_schedule()
    except Exception:
        logger.debug("[VOD-MERGE] schedule setup skipped", exc_info=True)

    return True


def uninstall() -> bool:
    global _ACTIVE
    _ACTIVE = False
    try:
        from apps.vod import tasks as vod_tasks
        for attr, original in (
            ("handle_movie_id_conflicts", _orig_handle_movie_id_conflicts),
            ("cleanup_orphaned_vod_content", _orig_cleanup_orphaned_vod_content),
            ("process_series_batch", _orig_process_series_batch),
            ("process_movie_batch", _orig_process_movie_batch),
        ):
            if original is not None and getattr(
                getattr(vod_tasks, attr, None), _PATCH_TAG, False
            ):
                setattr(vod_tasks, attr, original)

        # This one can be bound in more than one namespace, so it is restored
        # per site rather than on `vod_tasks` alone -- otherwise the UI
        # movie-open path would keep calling our wrapper after a disable.
        if _orig_refresh_movie_advanced_data is not None:
            for site in _refresh_binding_sites():
                if getattr(getattr(site, "refresh_movie_advanced_data", None),
                           _PATCH_TAG, False):
                    setattr(site, "refresh_movie_advanced_data",
                            _orig_refresh_movie_advanced_data)
    except Exception as exc:
        logger.debug("[VOD-MERGE] uninstall patch error: %s", exc)
    invalidate_index_cache()
    logger.info("[VOD-MERGE] uninstalled patches in pid=%s", os.getpid())
    return True
