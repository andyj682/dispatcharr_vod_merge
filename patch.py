"""
Dispatcharr VOD Merge -- implementation
==============================================

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

# Minimum normalised plot length. Short blurbs ("Season 2.", a stray genre word)
# collide easily; the full text of a real synopsis does not.
#
# 40, not the 80 originally guessed. Measured over a real library, collisions do
# not move at all as the floor drops -- 4 ambiguous hashes at every threshold
# from 80 down to 40 -- while the usable index grows by ~150 keys. The floor was
# costing reach and buying no safety, and it was excluding real one-line
# synopses: "The most miserable person on Earth must save the world from
# happiness." normalises to 69 characters.
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

# Values providers use to mean "no id" (mirrors the core cleaning in tasks.py).
_BLANK_IDS = ("", "0", "none", "null")


# --------------------------------------------------------------------------- #
# Module state
# --------------------------------------------------------------------------- #

_ACTIVE = False
_orig_process_series_batch = None
_orig_process_movie_batch = None
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


def _sweep_hour(value):
    """Hour of day for the nightly sweep, clamped to a valid hour."""
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SWEEP_HOUR
    return hour if 0 <= hour <= 23 else DEFAULT_SWEEP_HOUR


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
        "merge_series": _as_bool(settings.get("merge_series"), DEFAULT_MERGE_SERIES),
        "merge_movies": _as_bool(settings.get("merge_movies"), DEFAULT_MERGE_MOVIES),
        "scheduled_sweep": _as_bool(
            settings.get("scheduled_sweep"), DEFAULT_SCHEDULED_SWEEP),
        "sweep_hour": _sweep_hour(settings.get("sweep_hour")),
        "schedule_queue": (str(settings.get("schedule_queue") or "").strip()
                           or DEFAULT_SCHEDULE_QUEUE),
        "tag_unique_movies": _as_bool(
            settings.get("tag_unique_movies"), DEFAULT_TAG_UNIQUE_MOVIES),
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
    """Normalised full plot text, or None when too short to be distinctive."""
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
    recognise costs a wasted lookup rather than a missed merge.
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


def patched_process_series_batch(account, batch, categories, relations, scan_start_time=None):
    """Wrapper: inject before the original computes the merge key.

    The original is called unconditionally, and injection is fully guarded -- a
    fault in this plugin must degrade to "no merging", never to a failed scan.
    """
    _log_pid_once("series-batch wrapper active")
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
        account, batch, categories, relations, scan_start_time
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
    `movie_data` / `detailed_fetched` onto the relation and nothing else. No
    Movie row is created, deleted, renamed or repointed here.

    Resumable -- it always picks up relations that still have no detail, so
    running it repeatedly walks the backlog. Returns a summary.
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
                props["detailed_fetched"] = True
                rel.custom_properties = props
                rel.last_advanced_refresh = timezone.now()
                try:
                    rel.save(update_fields=["custom_properties", "last_advanced_refresh"])
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


def patched_process_movie_batch(account, batch, categories, relations, scan_start_time=None):
    """Wrapper: inject before the original computes the merge key."""
    _log_pid_once("movie-batch wrapper active")
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
        account, batch, categories, relations, scan_start_time
    )


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
        if not cfg["scheduled_sweep"]:
            remove_schedule()
            return False

        from core.scheduling import create_or_update_periodic_task
        hour = cfg["sweep_hour"]
        queue = cfg["schedule_queue"]
        task = create_or_update_periodic_task(
            task_name=SWEEP_PERIODIC_NAME,
            celery_task_path=SWEEP_TASK_PATH,
            cron_expression=f"0 {hour} * * *",
            enabled=True,
        )
        try:
            if task is not None and getattr(task, "queue", None) != queue:
                task.queue = queue
                task.save(update_fields=["queue"])
        except Exception as exc:
            logger.warning(
                "[VOD-MERGE] could not route schedule to queue %r: %s", queue, exc)
        logger.info(
            "[VOD-MERGE] scheduled nightly movie detail sweep at %02d:00 "
            "(system TZ) on queue %r", hour, queue,
        )
        return True
    except Exception as exc:
        logger.warning("[VOD-MERGE] could not create beat schedule: %s", exc)
        return False


def remove_schedule() -> bool:
    try:
        from core.scheduling import delete_periodic_task
        delete_periodic_task(SWEEP_PERIODIC_NAME)
        return True
    except Exception as exc:
        logger.debug("[VOD-MERGE] could not remove beat schedule: %s", exc)
        return False


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
    global _orig_process_series_batch, _orig_process_movie_batch, _ACTIVE

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
        _ACTIVE = True
        logger.info(
            "[VOD-MERGE] installed series + movie batch wrappers in pid=%s", os.getpid()
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
            ("process_series_batch", _orig_process_series_batch),
            ("process_movie_batch", _orig_process_movie_batch),
        ):
            if original is not None and getattr(
                getattr(vod_tasks, attr, None), _PATCH_TAG, False
            ):
                setattr(vod_tasks, attr, original)
    except Exception as exc:
        logger.debug("[VOD-MERGE] uninstall patch error: %s", exc)
    invalidate_index_cache()
    logger.info("[VOD-MERGE] uninstalled patches in pid=%s", os.getpid())
    return True
