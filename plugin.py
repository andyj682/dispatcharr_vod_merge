"""
Dispatcharr VOD Merge
=====================

Merges duplicate VOD *series and movies* created by providers that omit the TMDB
id from their listing. Such an entry keys by name+year and lands on its own row
-- a duplicate of a title you already have, which Dispatcharr cannot heal for
series (they only merge at scan time, and the id-conflict merger for series has
no callers) and heals destructively for movies.

This plugin matches the duplicate to the canonical title, then injects the
resulting tmdb id into the raw listing entry *before* the merge key is computed.
The merge then happens natively, and because the scan re-derives it from the
payload every run, it stays merged.

Signals, strongest first. Series use poster then plot; movies try their own
detail id first, then fall back to the same two. A manual approval outranks all
of them.

* **Detail id** (movies) -- the tmdb the provider puts in its own `get_vod_info`
  payload. Its own assertion, so no inference at all.
* **TMDB poster artwork** -- the asset basename is identical across providers,
  only the size segment differs.
* **Plot text** -- usually TMDB's overview verbatim, so it matches byte for byte.
  A backup signal: it reaches duplicates no name scheme can, but movie listings
  rarely carry a plot at all.

A derived key is only used when it maps to exactly one tmdb id across your tagged
library, so placeholder artwork and boilerplate blurbs reject themselves.

XC accounts only -- both wrappers skip any other account type.

Defaults to DRY RUN: it reports what it would merge and changes nothing until you
turn that off. Every injection is recorded in a durable log you can review.

See DESIGN.md for the rationale and patch.py for the implementation. This module
is the plugin entry point: it applies the wrappers at import time (every worker,
including the Celery children that actually run the scan).

Author: andyj682
License: MIT
"""

import logging

logger = logging.getLogger("plugins.dispatcharr_vod_merge")

try:
    from . import patch as _patch
except Exception:  # pragma: no cover - fall back to flat import layout
    import patch as _patch

try:
    # Import-time: wrap process_series_batch in every worker. The VOD scan runs
    # in a Celery prefork child, which loads plugins via worker_process_init --
    # so confirm the "[VOD-MERGE] installed ..." line appears for a Celery pid,
    # not just the uWSGI ones.
    _patch.install()
except Exception:  # never break app startup because of the plugin
    logger.exception("[VOD-MERGE] auto-install on import failed")


# The UI renders action output in a popup that does NOT scroll and does not let
# you select text, so anything past the first few lines is unreachable. That
# makes per-entry listings and absolute file paths worse than useless here --
# they push the counts, the only thing actually readable, out of view. So the
# message is counts ONLY. The filename lives in each action's description, which
# renders as static text outside the popup, and the file itself holds the detail.


def _format_preview(report):
    tally = report.get("tally") or {}
    tiers = report.get("tiers") or {}
    return (
        f"poster index={report.get('poster_index')} keys "
        f"({report.get('poster_ambiguous')} ambiguous, ignored)\n"
        f"plot index={report.get('plot_index')} keys "
        f"({report.get('plot_ambiguous')} ambiguous, ignored)\n"
        f"accounts={', '.join(report.get('accounts') or [])}  "
        f"dry_run={report.get('dry_run')}  "
        f"manual approvals={report.get('approvals')}\n"
        f"actionable={len(report.get('matches') or [])}\n"
        + (", ".join(f"{k}={v}" for k, v in sorted(tally.items())) or "nothing")
        + ("\nby tier: " + ", ".join(f"{k}={v}" for k, v in sorted(tiers.items()))
           if tiers else "")
    )


def _short_time(iso):
    """'2030-01-02T03:04:05.123456+00:00' -> '2030-01-02 03:04 UTC'.

    Timestamps come from the container, whose clock is UTC, so say so rather
    than let it be read as local time.
    """
    if not isinstance(iso, str) or len(iso) < 16:
        return str(iso)
    suffix = " UTC" if iso.endswith(("+00:00", "Z")) else ""
    return iso[:16].replace("T", " ") + suffix


def _format_enrich_status(st):
    """Enrichment status in a few dense lines, most important first.

    The popup does not scroll, and the first version of this ran well past it.
    Per-provider detail goes last, on ONE line, because it is what grows with
    the number of accounts.
    """
    per = " | ".join(
        f"{a} {v['relations']}/{v['need']}/{v['measure']}"
        for a, v in sorted(st.get("per_account", {}).items()))
    return (
        ("" if st.get("enrich_movies") else "ENRICHMENT IS OFF\n")
        + f"wanted: {st['movies']} movies (TMDB {st['tmdb_resolved']}/"
        f"{st['tmdb_total']}, unidentified {st['unid_resolved']}/"
        f"{st['unid_total']}), {st['relations']} copies, set from "
        f"{_short_time(st.get('generated_at'))}\n"
        f"have video+audio {st['have_essentials']} | need lookup "
        f"{st['need_fetch']} ({st['runs_at_current_limit']} runs) | need "
        f"measuring {st['need_measure']} "
        f"({st['measure_runs_at_current_limit']} runs)\n"
        + _format_last_run(st.get("last_enrich_run"))
        + (f"\ncopies/lookup/measure: {per}" if per else "")
    )


def _format_last_run(lp):
    """One line (two if something stopped early) on the last enrichment run."""
    if not lp:
        return "last run: none yet"
    state = lp.get("state")
    if state == "running":
        return (f"enrichment run IN PROGRESS, started "
                f"{_short_time(lp.get('started_at'))}")
    when = _short_time(lp.get("finished_at") or lp.get("started_at"))
    if state != "finished":
        return f"last run {state} at {when}" + (
            f" -- {lp['error']}" if lp.get("error") else "")
    res = lp.get("result") or {}
    look = res.get("lookup") or {}
    if look.get("aborted"):
        return f"last run finished {when}: nothing done -- {look['aborted']}"
    line = (f"last run finished {when}: looked up {look.get('fetched', 0)} "
            f"(gained {look.get('got_essentials', 0)})")
    meas = res.get("measure")
    if meas is None:
        line += ", measuring off"
    elif meas.get("aborted"):
        line += f", measuring skipped -- {meas['aborted']}"
    else:
        line += (f", measured {meas.get('probed', 0)} "
                 f"(gained {meas.get('gained', 0)})")
    errors = look.get("errors", 0) + ((meas or {}).get("errors", 0))
    line += f", errors {errors}"
    notes = []
    if (meas or {}).get("busy"):
        notes.append("did not measure " + ", ".join(meas["busy"])
                     + " (busy with playback)")
    if (meas or {}).get("broken"):
        notes.append("stopped measuring " + ", ".join(meas["broken"])
                     + " (not answering; nothing recorded)")
    stopped = look.get("stopped") or (meas or {}).get("stopped")
    if stopped:
        notes.append(f"stopped: {stopped}")
    return line + ("\n   " + "; ".join(notes) if notes else "")


def _format_log(data, limit=60):
    entries = (data or {}).get("entries") or []
    if not entries:
        return "Injection log is empty."
    import time
    shown = entries[-limit:]
    lines = []
    for e in reversed(shown):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(e.get("ts") or 0)))
        lines.append(
            f"  {when}  [{e.get('action')}/{e.get('tier')}]  {e.get('name')!r} "
            f"({e.get('account')}:{e.get('external_series_id')}) "
            f"-> tmdb={e.get('tmdb_id')}"
        )
    header = f"{len(entries)} logged entries"
    if len(shown) < len(entries):
        header += f" (newest {len(shown)})"
    return header + ":\n" + "\n".join(lines)


class Plugin:
    # UI title only; "Dispatcharr" is redundant inside the Dispatcharr UI.
    name = "VOD Merge"
    version = "1.5.2"
    description = (
        "Durably merges duplicate VOD titles from providers that omit TMDB ids, "
        "by matching metadata like poster artwork and plot text to a title you "
        "already have. Works only with XC accounts."
    )
    author = "andyj682"
    help_url = "https://github.com/andyj682/dispatcharr_vod_merge"
    # Order here is the order the UI renders. Grouped as: what to merge, where
    # to merge it from, then the movie-only extras, then the manual overrides,
    # then tuning nobody should need to touch.
    fields = [
        {
            "id": "dry_run",
            "label": "Dry run (report only)",
            "type": "boolean",
            "default": True,
            "help_text": (
                "Report what would merge, but inject nothing. Best used on "
                "first setup: check 'Series merge status' and 'Movie merge "
                "status', then turn this off. NOT a pause switch once you have "
                "merged -- merges are re-derived on every VOD scan, so turning "
                "this back on and running a scan lets already-merged titles "
                "split apart again."
            ),
        },
        {
            "id": "merge_series",
            "label": "Merge series",
            "type": "boolean",
            "default": True,
            "help_text": "",
        },
        {
            "id": "merge_movies",
            "label": "Merge movies",
            "type": "boolean",
            "default": False,
            "help_text": "",
        },
        {
            "id": "protect_merges",
            "label": "Prevent destructive movie merges",
            "type": "boolean",
            "default": True,
            "help_text": (
                "Ensures duplicate movies are merged into established entries "
                "instead of the other way around, fixing an existing "
                "Dispatcharr bug. Applies even in \"dry run\" mode."
            ),
        },
        {
            "id": "preserve_detail",
            "label": "Preserve essential movie detail",
            "type": "boolean",
            "default": True,
            "help_text": (
                "Preserves TMDB id and video and audio details for a library "
                "item even if a provider returns less detail on one call than "
                "it did on the last. Dispatcharr's default allows empty data "
                "to overwrite existing data. A value the provider actually "
                "sent always wins -- this only refills what went missing. "
                "Applies even in \"dry run\" mode."
            ),
        },
        {
            "id": "protect_prune",
            "label": "Prevent catalog deletion after an empty listing",
            "type": "boolean",
            "default": True,
            "help_text": (
                "Blocks Dispatcharr's post-scan cleanup when a provider returns "
                "an empty listing, which otherwise deletes that account's "
                "entire catalog and every title only it supplies. Cleanup "
                "resumes on the next scan that sees content. Applies even in "
                "\"dry run\" mode."
            ),
        },
        {
            "id": "allowed_accounts",
            "label": "Limit series merging to accounts",
            "type": "string",
            "default": "",
            "help_text": (
                "Comma-separated M3U account names FROM which to merge series. "
                "Empty means every account."
            ),
        },
        {
            "id": "movie_accounts",
            "label": "Limit movie merging to accounts",
            "type": "string",
            "default": "",
            "help_text": (
                "Comma-separated M3U account names FROM which to merge movies, "
                "exactly parallel to the series setting above. Empty means "
                "every account."
            ),
        },
        {
            "id": "tag_unique_movies",
            "label": "Tag movies no other providers have",
            "type": "boolean",
            "default": False,
            "help_text": (
                "Inject a TMDB tag even for movies no other provider has, to "
                "reduce future churn and let later copies merge properly. "
                "Changes those movies' Dispatcharr ids now, and they revert if "
                "this is turned off."
            ),
        },
        {
            "id": "scheduled_sweep",
            "label": "Fetch movie details nightly",
            "type": "boolean",
            "default": False,
            "help_text": (
                "Run a nightly timer to sweep details for all movies without an "
                "existing merge signal. Use 'Movie merge status' to see the "
                "backlog size. To fetch details in smaller batches, use the "
                "manual sweep action and the 'Movie details per manual run' "
                "setting below."
            ),
        },
        {
            "id": "sweep_hour",
            "label": "Nightly sweep hour (0-23)",
            "type": "number",
            "default": 4,
            "help_text": (
                "Hour the nightly sweep runs, in Dispatcharr's configured "
                "system timezone. Put it before your VOD refresh so newly "
                "fetched detail is used by the next scan."
            ),
        },
        {
            "id": "approved_matches",
            "label": "Approve manually",
            "type": "string",
            "default": "",
            "help_text": (
                "Manually specify a TMDB ID for movies or series with no signal "
                "so that they can merge as necessary. Comma-separated list. "
                "Format is '<account>:<id>=<tmdb_id>' -- the id is the external "
                "series id for series, the stream id for movies. Overrides any "
                "derived matches, but never overrides 'Never merge'. See the "
                "README for how to find the ids."
            ),
        },
        {
            "id": "denylist",
            "label": "Never merge",
            "type": "string",
            "default": "",
            "help_text": (
                "Comma-separated escape hatch for a bad match. Either "
                "'tmdb:<id>' (never merge anything INTO that title) or "
                "'<account>:<id>' (never merge that one provider entry) -- the "
                "id is the external series id for series, the stream id for "
                "movies. Overrides everything, including manual approvals. "
                "Read the ids off a preview or the log."
            ),
        },
        {
            "id": "variant_pattern",
            "label": "Variant edition pattern (regex)",
            "type": "string",
            "default": (
                r"\[[^\]]*(?:b\s*&\s*w|black\s*/?\s*white|colou?ri[sz]ed)[^\]]*\]"
            ),
            "help_text": (
                "Entries whose name matches are never merged, so an alternate "
                "edition stays its own library entry even when metadata signals "
                "match another title. Empty disables the check."
            ),
        },
        {
            "id": "enrich_movies",
            "label": "Enrich wanted movies",
            "type": "boolean",
            "default": False,
            "help_text": (
                "Fetch available audio/video details for every candidate copy "
                "of the movies in the wanted set file below (first by fetching "
                "provider details, and if necessary/enabled below, analyzing "
                "streams with ffprobe). Use \"Enrichment status\" first to see "
                "the size."
            ),
        },
        {
            "id": "wanted_set_path",
            "label": "Movie wanted set file",
            "type": "string",
            "default": "",
            "help_text": (
                "Path, inside the container, to the list of wanted movies. See "
                "README for details on the format. Blank disables enrichment. "
                "If the file is missing, stale or malformed, nothing happens."
            ),
        },
        {
            "id": "enrich_limit",
            "label": "Enrichment lookups per run",
            "type": "number",
            "default": 25,
            "help_text": (
                "Maximum number of individual movie copies one \"Enrich now\" "
                "run looks up. 0 means no limit."
            ),
        },
        {
            "id": "enrich_delay_ms",
            "label": "Delay between enrichment calls (ms)",
            "type": "number",
            "default": 500,
            "help_text": "Spacing between provider calls during enrichment.",
        },
        {
            "id": "probe_movies",
            "label": "Use ffprobe to analyze streams with no provider details",
            "type": "boolean",
            "default": False,
            "help_text": (
                "Measure audio/video details directly for any streams for "
                "which provider details don't include video/audio information. "
                "Also the only way to get information about Dolby Vision. Each "
                "probe uses a provider slot."
            ),
        },
        {
            "id": "probe_limit",
            "label": "Ffprobe lookups per run",
            "type": "number",
            "default": 3,
            "help_text": (
                "Maximum number of streams ffprobe analyzes during one "
                "\"Enrich now\" run. 0 means no limit."
            ),
        },
        {
            "id": "probe_delay_ms",
            "label": "Delay between ffprobe calls (ms)",
            "type": "number",
            "default": 2000,
            "help_text": "Spacing between stream measurements.",
        },
        {
            "id": "sweep_limit",
            "label": "Movie details per manual run",
            "type": "number",
            "default": 50,
            "help_text": (
                "How many movie detail lookups 'Fetch movie details' does in one "
                "go. Only movies without any existing merge signal are swept. "
                "0 means no limit. Does not apply to the nightly auto-run."
            ),
        },
        {
            "id": "sweep_delay_ms",
            "label": "Delay between detail calls (ms)",
            "type": "number",
            "default": 200,
            "help_text": (
                "Spacing between provider calls during the movie detail sweep, "
                "to stay friendly with provider rate limits. Worth raising "
                "before you enable a large new category, not after a rate-limit."
            ),
        },
        {
            "id": "schedule_queue",
            "label": "Schedule queue (advanced)",
            "type": "string",
            "default": "dvr",
            "help_text": (
                "Advanced: the Celery queue the nightly sweep is dispatched to. "
                "It must be served by a worker that loads plugins -- on a stock "
                "Dispatcharr that is the threads-pool 'dvr' worker, because the "
                "default prefork worker's consumer never imports plugins and "
                "rejects the task as unregistered. Change only if your worker "
                "layout differs."
            ),
        },
        {
            "id": "index_ttl_seconds",
            "label": "Index cache (seconds)",
            "type": "number",
            "default": 600,
            "help_text": (
                "How long the poster and plot indexes are reused within a "
                "worker before being rebuilt from the database. One VOD scan's "
                "batches all fall inside the default. Lower it only if you are "
                "debugging."
            ),
        },
    ]
    actions = [
        {
            "id": "status",
            "label": "Show status",
            "description": "Report whether the wrapper is active in this worker, "
                           "the settings in force, and both index sizes.",
            "button_label": "Check status",
            "button_variant": "outline",
        },
        {
            "id": "preview",
            "label": "Series merge status",
            "description": "What would be merged for SERIES on the next scan, "
                           "and by which signal. Read-only, no provider calls. "
                           "The full list is written to series_status.json in "
                           "the plugin's config folder.",
            "button_label": "Series status",
            "button_variant": "outline",
        },
        {
            "id": "movie_status",
            "label": "Movie merge status",
            "description": "What would be merged for MOVIES on the next scan, "
                           "plus how much of the detail backlog is left to "
                           "sweep. Read-only, no provider calls. The full list "
                           "is written to movie_status.json in the plugin's "
                           "config folder.",
            "button_label": "Movie status",
            "button_variant": "outline",
        },
        {
            "id": "sweep_movies",
            "label": "Fetch movie details",
            "description": "Look up provider detail for id-less movies and store "
                           "it. Collects data only; merges nothing. Use 'Movie "
                           "details per manual run' to adjust the number fetched "
                           "each run. Resumable, so run it again to continue.",
            "button_label": "Fetch details",
            "button_variant": "filled",
        },
        {
            "id": "enrich_status",
            "label": "Enrichment status",
            "description": "Read the wanted-set file and report how many of its "
                           "movies resolve, how many candidate copies they have, "
                           "and how many still need a provider lookup. Makes no "
                           "provider calls -- safe to run any time.",
            "button_label": "Enrichment status",
            "button_variant": "outline",
        },
        {
            "id": "enrich_now",
            "label": "Enrich movies",
            "description": "Fill in video and audio for the next batch of "
                           "wanted copies: provider detail first, then -- if "
                           "measuring is on -- measure what no provider "
                           "described. Runs in the background; 'Enrichment "
                           "status' shows the result. Resumable, so run it "
                           "again to continue.",
            "button_label": "Enrich now",
            "button_variant": "filled",
        },
        {
            "id": "list_log",
            "label": "Show injection log",
            "description": "List recorded merges and skipped variants. Also "
                           "writes to injection_log.json in the plugin's config "
                           "folder.",
            "button_label": "Show log",
            "button_variant": "outline",
        },
        {
            "id": "clear_log",
            "label": "Clear injection log",
            "description": "Forget the recorded merge history. Does not undo any "
                           "merge; entries reappear as the next scan re-derives "
                           "them.",
            "button_label": "Clear log",
            "button_variant": "light",
            "button_color": "red",
            "confirm": {
                "title": "Clear the injection log?",
                "message": "This only clears the audit trail. Merges already "
                           "made are unaffected and will be re-derived on the "
                           "next scan.",
            },
        },
    ]

    def run(self, action=None, params=None, context=None):
        context = context or {}

        if action == "enable":
            _patch.invalidate_config_cache()
            ok = _patch.install(manage_schedule=True)
            return {
                "status": "ok" if ok else "error",
                "message": (
                    "VOD merge enabled (series batch wrapped). Still "
                    "DRY RUN unless you turned that off."
                ) if ok else "Failed to enable (see logs)",
            }

        if action == "disable":
            _patch.uninstall()
            _patch.remove_schedule()
            return {"status": "ok",
                    "message": "VOD merge disabled (patches reverted, schedule removed)"}

        if action == "status":
            import os
            cfg = _patch._load_config(force=True)
            idx = _patch._get_indexes(force=True)
            log = _patch.get_log()
            return {
                "status": "ok",
                "message": (
                    f"active={_patch._ACTIVE} in worker pid={os.getpid()} "
                    f"(reflects ONE worker; check logs for all pids -- the scan "
                    f"runs in a Celery child). "
                    f"dry_run={cfg['dry_run']}, "
                    f"merge_series={cfg['merge_series']}, "
                    f"merge_movies={cfg['merge_movies']}, "
                    # Reported separately because it is invisible when working:
                    # nothing bad happening looks exactly like never firing.
                    f"protect_merges={cfg['protect_merges']}"
                    f"{'' if _patch._orig_handle_movie_id_conflicts else ' (NOT INSTALLED in this worker)'}, "
                    # Invisible when working, for the same reason.
                    f"preserve_detail={cfg['preserve_detail']}"
                    f"{'' if _patch._orig_refresh_movie_advanced_data else ' (NOT INSTALLED in this worker)'}, "
                    # And again: a cleanup that never had to be refused looks
                    # identical to a guard that was never installed.
                    f"protect_prune={cfg['protect_prune']}"
                    f"{'' if _patch._orig_cleanup_orphaned_vod_content else ' (NOT INSTALLED in this worker)'}, "
                    f"series_accounts={sorted(cfg['accounts']) or '<all>'}, "
                    f"movie_accounts={sorted(cfg['movie_accounts']) or '<all>'}, "
                    f"denylist={len(cfg['denylist'])}, "
                    f"approvals={len(cfg['approvals'])}, "
                    f"poster_index={len(idx['poster'])} ({len(idx['poster_amb'])} amb), "
                    f"plot_index={len(idx['plot'])} ({len(idx['plot_amb'])} amb), "
                    f"nightly_sweep={cfg['scheduled_sweep']}"
                    + (f" at {cfg['sweep_hour']:02d}:00 on queue '{cfg['schedule_queue']}'"
                       if cfg['scheduled_sweep'] else "")
                    + f", logged={len(log.get('entries') or [])}"
                ),
            }

        if action == "preview":
            try:
                report = _patch.preview_impl()
                # File first (complete), then trim the API response.
                _patch.write_series_status_file(report)
                message = _format_preview(report)
                return {
                    "status": "ok",
                    "message": message,
                    "report": _patch.trim_matches(report),
                }
            except Exception as exc:
                logger.exception("[VOD-MERGE] preview failed")
                return {"status": "error", "message": f"Preview failed: {exc}"}

        if action == "movie_status":
            try:
                st = _patch.movie_sweep_status()
                _patch.write_movie_status_file(st)
                message = (
                    f"accounts={', '.join(st['accounts'])}  "
                    f"merge_movies={st['merge_movies']}  dry_run={st['dry_run']}\n"
                    f"id-less movies still needing a detail lookup: {st['pending']}\n"
                    f"already fetched: {st['with_detail']}, "
                    f"of which carry a TMDB id: {st['with_detail_tmdb']}\n"
                    f"of those, would merge into an existing movie: {st['would_merge']}, "
                    f"would become a newly tagged movie: {st['would_create']}\n"
                    f"actionable={len(st.get('matches') or [])}\n"
                    f"PROJECTED next scan (all tiers): "
                    + (", ".join(f"{k}={v}" for k, v in sorted(st['projected'].items()))
                       or "nothing")
                    + ("\n  by tier: "
                       + ", ".join(f"{k}={v}"
                                   for k, v in sorted(st['projected_by_tier'].items()))
                       if st['projected_by_tier'] else "")
                )
                return {
                    "status": "ok",
                    "message": message,
                    "report": _patch.trim_matches(st),
                }
            except Exception as exc:
                logger.exception("[VOD-MERGE] movie_status failed")
                return {"status": "error", "message": f"Movie status failed: {exc}"}

        if action == "sweep_movies":
            try:
                st = _patch.sweep_movies_impl()
                per = "\n".join(
                    f"   {a}: fetched={v['fetched']}, with_tmdb={v['with_tmdb']}, errors={v['errors']}"
                    for a, v in sorted(st.get("per_account", {}).items())
                )
                return {
                    "status": "ok",
                    "message": (
                        f"fetched={st['fetched']} (with_tmdb={st['with_tmdb']}, "
                        f"no_tmdb={st['no_tmdb']}), empty={st['empty']}, "
                        f"errors={st['errors']}\n"
                        f"remaining: {st['remaining']}"
                        + ("\n" + per if per else "")
                    ),
                    "report": st,
                }
            except Exception as exc:
                logger.exception("[VOD-MERGE] sweep_movies failed")
                return {"status": "error", "message": f"Detail sweep failed: {exc}"}

        if action == "enrich_status":
            try:
                st = _patch.enrich_status()
                if st.get("file_status") != "ok":
                    # Absent is normal before the first sync; the others are
                    # misconfiguration. Either way nothing would run -- say which.
                    return {
                        "status": "ok",
                        "message": (
                            f"enrich_movies={st['enrich_movies']}\n"
                            f"wanted-set file: {st['file_status']}"
                            f" ({st.get('file_detail')})\n"
                            f"path: {st['path']}\n"
                            "Nothing would be enriched. This never falls back "
                            "to enriching the whole library.\n"
                            + _format_last_run(st.get("last_enrich_run"))
                        ),
                        "report": st,
                    }
                return {"status": "ok", "message": _format_enrich_status(st),
                        "report": st}
            except Exception as exc:
                logger.exception("[VOD-MERGE] enrich_status failed")
                return {"status": "error", "message": f"Enrichment status failed: {exc}"}

        if action == "enrich_now":
            # Enqueue only. Lookups run about a second each and a measurement
            # can hold a provider connection for 30s, so a batch outlives a
            # web request.
            try:
                started, why = _patch.enqueue_enrichment()
                if not started:
                    return {"status": "ok", "message": f"Not started: {why}"}
                return {
                    "status": "ok",
                    "message": (
                        f"Enrichment {why}. Results appear under 'Enrichment "
                        f"status' when it finishes."
                    ),
                }
            except Exception as exc:
                logger.exception("[VOD-MERGE] enrich_now failed")
                return {"status": "error",
                        "message": f"Could not start enrichment: {exc}"}

        if action == "list_log":
            data = _patch.get_log()
            path = _patch.write_log_file(data)
            return {
                "status": "ok",
                "message": _format_log(data) + f"\n\nFull log: {path}",
                "entries": data.get("entries") or [],
            }

        if action == "clear_log":
            try:
                removed = _patch.clear_log()
                return {"status": "ok", "message": f"Cleared {removed} log entries."}
            except Exception as exc:
                logger.exception("[VOD-MERGE] clear_log failed")
                return {"status": "error", "message": f"Failed to clear: {exc}"}

        return {"status": "error", "message": f"Unknown action: {action}"}

    def stop(self, context=None):
        """Called by Dispatcharr on disable / delete / reload."""
        _patch.uninstall()
        return {"status": "ok", "message": "VOD merge reverted"}
