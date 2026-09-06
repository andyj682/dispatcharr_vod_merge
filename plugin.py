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
    version = "1.0.0"
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
