# Changelog

All notable changes to this plugin. Versions follow the `version` field in
`plugin.json`.

Everything through 0.8.3 landed during initial development against a single
large live library; the entries below record *why* each change was needed, since
several were driven by failures that were invisible from the outside.

## 1.0.0 — 2026-09-06

First release. Movies reach parity with series, the settings and actions are
symmetrical between the two, and the documentation matches what the code does.

**Added**

- **Manual approvals for movies.** `approved_matches` was consulted by the series
  path only, so a movie duplicate that no signal could reach was unmergeable by
  configuration — the denylist could suppress it but nothing could merge it.
  Approvals are keyed `<account>:<stream_id>=<tmdb_id>` for movies and
  `<account>:<external_series_id>=<tmdb_id>` for series, and outrank every
  derived tier.

  Found from a real case: a provider shipped a well-known feature film with an
  empty `stream_icon`, no plot, and no id in its detail payload. All three
  derived tiers correctly reported `no_signal`, and nothing could be done.

  An approval bypasses the variant guard and ignores `Tag movies no other
  providers have`, on the same reasoning as the series path: those gates bound
  *unattended* behaviour, and an approval is one entry a person typed.

- **A `Merge series` switch.** Movies had one and series did not, so the only way
  to stop series merging was to guess at the account allowlist. Each kind now has
  its own switch and its own allowlist, and they are independent.

  The two default differently — series on, movies off — so that neither an
  upgrade nor a fresh install changes behaviour on its own. `Dry run` is the real
  safety gate and both sit behind it. Asserted in the tests, so changing it has
  to be deliberate.

- **`Movie merge status` writes `movie_status.json`.** Series had a report file
  and movies had none; the movie report now carries the same per-entry detail,
  including `creates_new_row`.

- **`tools/explain_merge.sh`** — answers "why didn't this title merge?" by running
  the plugin's real decision function against live database state, and builds the
  manual approval line when that is the answer. The plugin could report what it
  *would* merge but nothing explained a negative, and a no-signal entry leaves no
  audit trail by design. A repo resource, deliberately not in the plugin zip.

**Changed**

- Settings reordered and renamed for symmetry: `Merge series` / `Merge movies`,
  then `Limit series merging to accounts` / `Limit movie merging to accounts`,
  then the movie-only extras, the nightly sweep, the manual overrides, and
  finally tuning. Field order is now covered by the manifest parity tests, which
  compared by id before and so would not have caught a reshuffle.
- **`Limit movie details to accounts` is now `Limit movie merging to accounts`.**
  The old label understated it: the setting already bounded movie *merging*, not
  just the detail lookup. Behaviour unchanged; the label was wrong.
- `Also tag movies no one else has` is now **`Tag movies no other providers
  have`**, and its help says the change is not one-way — it alters those movies'
  Dispatcharr ids immediately, and they revert if it is turned off.
- **`Preview matches` and `Movie detail status` are now `Series merge status` and
  `Movie merge status`**, matching button styles. They do the same job for the
  two kinds and now read that way.
- **Status actions print counts only.** Dispatcharr's action popup does not
  scroll and does not allow text selection, so per-entry listings and absolute
  file paths pushed the counts — the only readable part — out of view. Both
  actions now emit a fixed handful of summary lines regardless of library size;
  the detail is in the file, named in each action's description.
- `preview_debug.json` renamed to **`series_status.json`** to match.
- **`Dry run` is documented as a first-setup switch, not a pause button.** It
  said it "changes nothing", which is true of the plugin and misleading about the
  system: merges are re-derived on every scan, so turning dry run back on and
  running a refresh lets already-merged titles split apart again. The README
  covers this, including the one case that does not self-correct.
- The long info panel was removed from the settings; its one load-bearing fact,
  that this works only with XC accounts, is in the plugin description and stated
  up front in the README. Help text trimmed throughout, with methodology moved to
  the README.

**Fixed**

- **`Series merge status` wrote a truncated file while calling it the full list.**
  The match list was capped at 400 *before* the report was written, so on a large
  first run the JSON the message pointed at was silently incomplete. The file is
  now complete and the cap moved to where it belongs — the API response, limited
  to 200 and flagged with `matches_truncated` / `matches_total`.
- **`Movie merge status` projected decisions with a placeholder account name**, so
  approvals — which are keyed on the account — could never match in its
  projection. It now carries the real account through and agrees with what a scan
  will actually do.
- UI text described a series-only plugin in several places, including a claim
  that a movie listing carries neither poster nor plot and that movies therefore
  rely solely on the detail id. Movies fall back to the same poster and plot
  tiers, and on some providers artwork does most of the work.

**Docs**

- README rewritten. It described the plugin as series-only in places, documented
  6 of 14 settings, told readers to verify the install by grepping for
  `VOD-PMERGE` when the marker is `VOD-MERGE`, and closed by describing the main
  movie feature as unbuilt. Adds **Manual approvals** (format, finding both ids,
  verification, cautions), **Known limits**, and the install route via the zip.
- `DESIGN.md` added, superseding the `patch.py` module docstring as the record of
  rationale — that docstring predated movie support. It includes a *Where this
  goes next* section recording the movie metadata enrichment finding: the sweep
  already stores full ffprobe stream data, and the obstacle is that its candidate
  filter targets the opposite population from the one that would need it.
- Manual approvals documented as comma-separated only. The parser also accepts
  newlines, but the settings field is single-line.

## 0.8.3 — 2026-09-05

**Fixed**

- Movie audit logging never worked. Injections and merges were happening
  correctly, but nothing about movies reached the injection log. The wrapper's
  outermost handler is deliberately broad so a fault can never break VOD ingest,
  which meant the only external symptom was a `movie injection failed` line in
  the container log. Grep for that line before trusting any feature here.

## 0.8.2 — 2026-09-05

**Fixed**

- Only act on entries whose category is enabled. The scan fetches the provider's
  **whole** listing and core discards disabled categories *after* the batch
  processor runs, so the wrapper was seeing — and logging — thousands of entries
  for content that was never going to be synced. On the library this was found
  on, the log showed ~5,000 phantom merges against 438 real ones. The plugin now
  mirrors core's `category_enabled()` check.

## 0.8.1 — 2026-09-05

**Changed**

- The nightly sweep runs **unbounded**. `sweep_limit` exists because the manual
  action runs inside a web request and a long run would time it out; nothing
  constrains a beat task that way, and a bounded nightly run meant a large new
  category took a week to drain at 50 a night. The candidate set is already
  narrow and shrinks permanently as it is swept, so an unbounded run is
  self-limiting rather than open-ended.

## 0.8.0 — 2026-09-05

**Changed**

- Plot-hash minimum length lowered from 80 to 40 characters. The 80-character
  floor was rejecting a large number of legitimate short TMDB overviews. The
  exactly-one-tmdb guard, not the length floor, is what actually prevents
  boilerplate from merging unrelated titles; measured at 40 the index held
  10,101 usable keys with 4 collisions.

## 0.7.2 — 2026-09-05

**Changed**

- Injection log cap raised from 500 to 5,000 entries. Note this log is a single
  JSON blob rewritten per batch, so the cap trades off against ingest cost —
  raising it much further would want an append-only file instead.

## 0.7.1 — 2026-09-04

**Changed**

- The sweep now skips any relation a free signal already reaches. Fetching a
  relation the poster index will merge for nothing is pure waste, and on a
  poster-rich provider that is the majority of them (measured: 671 of 900 in one
  category). Scan ordering usually saved us anyway, but relying on the clock was
  fragile.

## 0.7.0 — 2026-09-04

**Added**

- Nightly movie detail sweep, as a Celery beat task, with `scheduled_sweep`,
  `sweep_hour` and `schedule_queue` settings.
- The sweep runs **newest-first**. Measured on a real library, the newest 40
  id-less relations returned a tmdb 40/40 while the oldest 50 returned 1/50 —
  old bulk imports are untagged long-tail content, while recent arrivals are both
  well-described and the ones actually duplicating the library.

**Notes**

- The task is routed to the `dvr` queue because a plugin `@shared_task` sent to
  the default prefork queue is **rejected** — that worker's consumer runs in the
  main process, which never imports plugins, so it sees an unregistered task
  name. The threads-pool `dvr` worker does register them.

## 0.6.0 — 2026-09-04

**Added**

- Tiered movie matching: **detail → poster → plot**, with an unusable detail id
  falling through rather than failing the entry.
- Status projection across **all** tiers. Reporting only the detail tier was
  actively misleading — a provider carrying no ids but TMDB artwork on nearly
  every entry can have hundreds of poster-tier merges queued while the detail
  tier shows zero.
- `tag_unique_movies`, gating the only operation that can *create* a newly
  tagged row rather than merge into an existing one.

**Changed**

- Log only actual state changes, so the audit trail stops restating unchanged
  decisions on every scan. (Declined variants remain logged every scan by
  design — that is a standing decision, not a state change.)

## 0.4.1 — 2026-09-04

**Added**

- Movie support: the `process_movie_batch` wrapper and the manual detail sweep,
  with `merge_movies`, `movie_accounts`, `sweep_limit` and `sweep_delay_ms`.
- Movie account scope is **separate** from the series allowlist, on purpose —
  the two provider populations rarely coincide.

**Notes**

- The sweep deliberately does not call core's `refresh_movie_advanced_data`. That
  task routes through `handle_movie_id_conflicts`, which deletes the canonical
  `Movie` row and keeps the id-less orphan, and it fires on precisely every
  candidate here. See the README for the full reasoning.

## 0.2.0 — 2026-09-04

Initial release. Series only.

**Added**

- `process_series_batch` wrapper injecting a TMDB id before the merge key is
  computed, making merges durable across rescans.
- Poster-artwork and plot-text signals, each guarded by an exactly-one-tmdb
  uniqueness test.
- Dry run (default on), account allowlist, variant-edition guard, denylist,
  manual approvals, index cache.
- Preview and status actions, and a durable audit log.
