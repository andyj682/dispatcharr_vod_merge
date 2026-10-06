# Changelog

All notable changes to this plugin. Versions follow the `version` field in
`plugin.json`.

Everything through 0.8.3 landed during initial development against a single
large live library; the entries below record *why* each change was needed, since
several were driven by failures that were invisible from the outside.

## 1.5.6 — 2026-10-05

**Added — preview adding an account to movie merging**

- New action **Preview adding accounts to movie merging**: read-only, no
  provider calls. For each active account not yet in **Limit movie merging to
  accounts**, it projects what adding it would merge, using the same decision,
  index and manual approvals a real scan would.

- It reports how many titles would get a new Dispatcharr id — a title whose
  every copy merges elsewhere is removed, and anything that stored its id breaks
  — and, with a wanted set configured, how many of those are titles you sync and
  how many fold into a title you also sync. The affected titles are listed in
  `merge_whatif.json`.

- Until now there was no safe way to rehearse this: dry run undoes existing
  merges at the next scan, so it cannot serve as a preview.

## 1.5.5 — 2026-10-05

**Changed — renamed to VOD Merge & Enrich**

- The plugin now does two things — merging duplicates and enriching the movies
  you sync — so its display name and description say both. Only the label
  changed: the folder and internal key stay `dispatcharr_vod_merge`, so
  settings, records and schedules carry over untouched.

## 1.5.4 — 2026-10-05

**Added — check 4K copies for Dolby Vision even when the provider described them**

- New setting **Also ffprobe 4K copies for Dolby Vision**, on by default; it only
  applies once ffprobe measuring is on. Until now a copy whose provider
  supplied resolution and audio was never measured, so its Dolby Vision status
  could not be known — and no provider ever reports it. Avoiding DV streams
  without a fallback layer therefore worked for some movies and silently not
  for others.

- Only copies the provider described as 4K are checked, using the same 4K rule
  the quality-ranking plugin uses, and each only once (an error retries after
  the usual window). They are measured after copies with no details at all,
  within the same per-run budget.

- A DV record found this way is added to the provider's existing video details,
  keeping every value the provider sent — that is where the ranking plugin looks
  for it. Previously a measurement only filled details that were entirely
  missing, so on these copies the record would never have reached it.

- **Enrichment status** shows the 4K checks waiting and includes them in the run
  and nights estimates. The last run reports how many 4K checks were made and,
  across **every** measurement, how many copies were Dolby Vision and how many
  of those have no fallback layer — the ones that matter on non-DV hardware.

## 1.5.3 — 2026-10-04

**Added — enrich the wanted set nightly**

- New settings **Enrich wanted movies nightly** (off by default), **Nightly
  enrichment start hour** (default 1) and **Nightly enrichment time limit**
  (default 90 minutes). Each night the run repeats the same batches as **Enrich
  movies** — lookups first, then measurements, under the same lock and the same
  busy-provider check — until the backlog is done, the time limit is reached, or
  what remains is on providers that are busy or not answering.

- **Enrichment status** says how a nightly run ended and shows roughly how many
  nights the current backlog needs at the current settings, so a large wanted
  set is a visible choice rather than weeks of provider traffic by surprise.

- A provider that stops answering during the night is left alone for the rest
  of it. Its failures are deliberately not recorded against its copies, so
  without this every later batch would choose the same copies and keep asking.

- The time limit is always bounded (1 to 720 minutes). A batch someone starts
  by hand can sensibly be unlimited; a run nobody is watching cannot.

- Turning the nightly run off takes effect at once: the run checks the switch
  when it fires. A new start hour, like the merge sweep's, is written into the
  scheduler on **Enable** or a restart.

## 1.5.2 — 2026-10-04

**Added — a provider that is busy with playback is not measured**

- Before every measurement the plugin now asks Dispatcharr whether the account
  has a free connection slot — the same check the VOD proxy makes before
  admitting a viewer, covering live TV and VOD playback alike. If not, that
  account is skipped for the rest of the run, nothing is recorded against its
  copies, and **Enrichment status** names it. On an account that allows one
  connection, measuring now only ever happens while nothing is playing from it.

  Busy accounts are left out **before** the run's batch is chosen, so one busy
  provider cannot use up the whole batch while others sit idle with copies
  waiting. The check is repeated before each measurement, for playback that
  starts mid-run.

  The measurement also goes through the profile that was checked, so the slot
  counted and the connection opened belong to the same login. If the check
  cannot be made at all, nothing is measured: not knowing whether someone is
  watching is not permission to interrupt them.

- **Deliberately not done: reserving a slot for the measurement.** It would
  close the one remaining window — playback that starts *during* a measurement
  of at most thirty seconds — but Dispatcharr has no cleanup for a slot that is
  never released, so one interrupted run could leave a one-connection account
  refusing all playback until the next restart.

## 1.5.1 — 2026-10-04

**Changed — one Enrich action does both steps, in the background, one run at a
time**

- **Measuring is now a step of Enrich movies, not an action of its own.** One
  run looks up provider detail for the next batch of wanted copies and then,
  if **Use ffprobe to analyze streams with no provider details** is on,
  measures the
  copies the lookup could not describe. The separate **Measure streams** action
  is gone.

  As two actions they could run in the wrong order. The measuring pass took
  any copy without video and audio — including copies whose provider simply
  had not been asked yet — so it could spend a connection-holding measurement
  where a light request would have filled the gap for free. A copy is now
  measured only after its lookup has been tried (an attempt that errored
  counts, so a provider whose detail endpoint always fails can still be
  measured). The two batch sizes stay separate settings because the two costs
  are so different.

- **Enrich movies now queues the work and returns at once**, instead of working
  inside the web request. A measurement can hold a provider connection for up
  to thirty seconds, so even three of them could outlast the request — and in
  practice did, timing it out part-way — and a batch of lookups comes close on
  its own. The run goes to the same worker queue as the nightly sweep, for the
  same reason: it is the one that can see plugin tasks.

- **Only one enrichment run can be active at a time.** A button that returns at
  once can be pressed twice, and on an account that allows a single connection
  two runs collide with each other exactly as a measurement collides with
  playback. A second request now reports that a run is in progress instead of
  starting.

  The lock is held by the run itself, not checked at the button, so it holds
  however the run was started. It is short-lived and renewed before every
  lookup and every measurement, so a worker killed mid-run frees it within
  minutes rather than blocking enrichment for hours. Anything that prevents the
  run confirming it still holds the lock — the lock store unreachable, or
  another run having taken over — stops it: a skipped run costs some missing
  data, a collision can cost someone their stream.

- Because the button no longer waits, **Enrichment status** now reports:
  - whether the last run is **in progress** or **finished**, and when (in UTC,
    the container's clock), with what each step looked up, measured and
    gained, and whether a provider was abandoned for repeated failure;
  - **how many copies still need measuring**, in total and per provider, and how
    many runs that is at the current batch size. The count uses the same
    decision a run makes, so it is the run's backlog exactly rather than an
    estimate.

  The record of the last run is kept in its own row in Dispatcharr's settings
  table, like the audit log, rather than in a file beside the plugin — uploading
  a new zip replaces the plugin folder's contents and would erase it.

- **Enrichment settings reworded and reordered.** **Enrich wanted movies** now
  comes first, since it is the switch for everything below it. *Wanted-set
  file (enrichment)* is now **Movie wanted set file**, and the measuring
  settings name the tool they use: **Use ffprobe to analyze streams with no
  provider details**, **Ffprobe lookups per run** and **Delay between ffprobe
  calls (ms)**. Every merging setting — including the nightly sweep, manual
  approvals, the never-merge list and the variant pattern — now comes before
  enrichment, with the rarely touched tuning settings last. Stored values are
  unaffected — only labels, help text and order changed.

- The status message is restructured to fit the action popup, which does not
  scroll: a few dense lines, most important first, per-provider detail last.

- Still not here: pausing when a provider is busy with real playback. **With
  measuring on, do not start an enrichment run while something is streaming
  from that provider.**

## 1.5.0 — 2026-10-04

**Added — measure the streams no provider will describe**

- Enrichment, step two of two. New setting **Use ffprobe to analyze streams
  with no provider details** (at first named *Measure streams that providers do
  not describe*), off by default, and a **Measure streams** action.

  Step one harvests what providers already know, and on a real library that is
  a minority of copies. It is strongly provider-dependent — some providers
  supply technical detail nearly always, some sometimes, and some never — and
  the ones that never do answer normally, with no errors and no empty replies.
  The gap is structural, not a provider having a bad day, so no amount of
  re-asking closes it.

  The only remaining source is the stream itself. This opens it briefly and
  reads what is actually there.

- **It is the only way to get Dolby Vision information at all.** The record
  that identifies DV without a fallback layer has not been seen in a single
  provider payload, and appeared in the very first measurement. A
  profile 5 stream with no fallback renders wrong on non-DV hardware, and
  until now there was no way to know which copies those were.

- **The cost is a different kind from everything else here, and the design
  reflects that.** A detail lookup is one request and milliseconds. Measuring
  opens the actual media and holds a provider connection slot for seconds —
  the same scarce resource playback needs. So reads are bounded, there is a
  delay between measurements, the per-run cap defaults to three rather than
  twenty-five, and repeated failure against one provider stops that provider
  for the run.

- **Repeated failure is treated as a fact about the provider, not about each
  stream.** If several measurements against the same provider fail in a row,
  the rest are abandoned for that run and **nothing is recorded against those
  streams**. Recording it would put every copy that happened to be queued
  during a bad hour into a week-long retry window — which is how a brief
  outage quietly becomes permanently missing data.

- **The per-run cap is a hard ceiling here, unlike for detail lookups.** The
  batching rule that keeps a title from being measured in halves takes an
  oversized title whole, which is right when each lookup is one cheap request
  and wrong when each one holds a provider connection for seconds. Measurement
  fills its budget exactly instead, splitting whichever title straddles the
  boundary and resuming it first next run. Exactly one title is ever partially
  done, which is the same guarantee, without a setting of three quietly doing
  several times that.

- **A measurement cut off part-way is retried, not believed.** Many accounts
  allow only one simultaneous connection, so a measurement that collides with
  playback can be dropped mid-read — and ffprobe may still exit cleanly having
  seen a video stream with no dimensions. Every real stream has dimensions, so
  their absence is treated as a truncated read rather than as a fact about the
  media. Without that, one unlucky collision would mark a copy permanently
  unmeasurable and never look at it again.

  **There is no automatic protection against colliding with playback yet.** Do
  not run this while something is streaming from that provider.

- Results are stored in their own place, which nothing else writes, **and**
  mirrored into the detail Dispatcharr already reads, so quality ranking picks
  them up with no other change. The mirror only ever fills a gap: a value a
  provider actually sent always wins.

## 1.4.0 — 2026-10-04

**Added — fill in video and audio for the movies you actually sync**

- Four new settings and two new actions, all opt-in and all off by default.

  Dispatcharr's quality ranking compares a movie's candidate copies against
  each other, so it needs measured video and audio for *every* copy, not one.
  Nothing produces that today. The XC detail endpoint refreshes a single copy
  per movie — the highest-priority account's — and that is a structural limit,
  not a setting. On one library the copy it picks happens to be the one
  provider that supplies technical detail for roughly 0.7% of movies.

  Fetching detail for all 37,000 movies is not the answer either. The useful
  set is the few thousand titles actually synced to a media library, and only
  the tool doing the syncing knows which those are. So it publishes them as a
  file, and this reads it: **Wanted-set file (enrichment)**. Demand is the one
  thing that tool holds which cannot be queried from here.

  **Enrich wanted movies** then fetches provider detail for every candidate
  copy of every wanted title — directly, per copy, which is exactly the thing
  the detail endpoint cannot do.

- **A run completes whole titles, never part of one.** Ranking compares a
  movie's copies against each other, and a copy with no audio data counts as
  zero -- so a half-measured comparison can rank an enriched stereo track above
  an unmeasured surround one. Partial coverage of a title is worse than none,
  which makes the title, not the individual copy, the unit a bounded run is
  allowed to stop at. A title with more copies than the batch size is still
  done whole, rather than being skipped for ever.

- **Enrichment status** reports what would happen and makes no provider calls
  at all. Run it first; it is safe at any time, and it tells you the size of
  the job before you start one.

- **Nothing is ever widened.** If the file is missing, unreadable, malformed,
  of an unknown schema, internally inconsistent, or simply too old to believe,
  the answer is to do nothing. It never falls back to enriching your whole
  library. A fault in the enrichment itself costs some missing metadata; a
  fault in the thing deciding *how much work to do* would cost thousands of
  provider calls on a set nobody asked for, so the two fail in opposite
  directions by design.

  Missing and unreadable are reported separately, and deliberately so. A
  permissions mistake would otherwise look exactly like "no file yet" and
  silently disable the feature with nothing to find.

- **An attempt is recorded, not just a success.** A copy whose provider
  answered and had nothing more to give is not asked again. Skipping only on
  "has data" would re-ask, every run, for precisely the copies whose providers
  never answer — and those are the expensive ones later. Genuine errors retry
  after a week.

- Enrichment writes the same stored detail Dispatcharr itself writes, and
  still never touches `detailed_fetched` or `last_advanced_refresh`. Those
  remain the only record that a *client* asked for a movie.

  It also carries the same protection the existing **Preserve essential movie
  detail** setting provides, applied to its own write. That write replaces the
  stored detail wholesale, so a provider reply that omits a TMDB id an earlier
  lookup had captured would otherwise destroy it silently -- and that id is
  this plugin's strongest signal for merging movies. A value the provider
  actually sent always wins; only a gap is refilled.

**Also in this release**

- Spelling throughout the documentation and code comments standardized to
  American English. No behavior change.

## 1.3.0 — 2026-09-18

**Added — an empty provider listing no longer deletes your catalog**

- New setting, **Prevent catalog deletion after an empty listing**, on by
  default.

  Every VOD scan ends with Dispatcharr's cleanup pass, which deletes any
  relation it did not see during that scan and then deletes every library item
  left with no relations from any account. That is correct while "the scan did
  not see it" means "the provider no longer has it".

  It stops being correct when a provider's movie endpoint returns an **empty**
  list. The scan then sees nothing, so *every* relation on that account is
  treated as stale and removed, taking with it every title that account was the
  only source for. The items come back on the next good scan, but as **new
  rows with new ids** — which breaks saved links and anything downstream that
  remembered the old ones.

  This was not theoretical. On one library an empty movie listing removed an
  account's entire set of movie relations and thousands of library items in a
  single scheduled refresh, while that same account's series — fetched seconds
  earlier, over the same connection — came back complete.

  Dispatcharr already guards the equivalent case one level up: if a provider
  returns no *categories*, the refresh aborts rather than acting on the gap. The
  same reasoning simply was not applied to the movie and series lists.

  With this setting on, the plugin checks whether the scan that just ran saw
  *any* of the account's existing content. If it saw none, the cleanup is
  refused for that scan and runs normally on the next one that returns content.
  The refusal is written to the plugin log and appears in Dispatcharr's own
  cleanup line, so it is visible without digging.

  Deliberately narrow: only a completely empty result is refused. A listing that
  merely comes back short is left alone, because a genuinely shrinking catalog
  would otherwise be blocked from ever being tidied. That case belongs upstream
  and has been reported.

  Like the other two protections, this applies even in dry-run mode — a setting
  meaning "do not inject" should not be able to switch off something that
  prevents data loss.

- `Show status` now reports whether the protection is installed in the worker
  you asked, for the same reason the other two do: a protection that has never
  needed to fire looks exactly like one that was never installed.

## 1.2.1 — 2026-09-15

**Fixed — the plugin's scan-time logging was being discarded**

- Nothing about merging changes. What changes is that you can now *see* it
  happen.

  Dispatcharr's logging configuration names every logger it manages — `apps`,
  `celery`, `core.tasks` and so on — and gives each an explicit handler with
  `propagate: False`. Plugin loggers are not in that list, so they carry no
  level of their own and inherit whatever the root logger is set to.

  That is harmless in the web workers, where root sits at the configured
  level. It is not harmless in a Celery **prefork child**: billiard
  reconfigures root there and leaves it at `WARNING`. The VOD scan runs in
  exactly that process — so this plugin's install line, its wrapper-active
  line and its per-batch injection tally were all discarded at the logger,
  before any handler saw them, while Dispatcharr's own `apps.*` lines from the
  same process came through normally.

  The wrappers were running the whole time. Only the evidence was missing —
  which made a working plugin indistinguishable from an absent one, and cost a
  long and thoroughly misdirected investigation to establish.

- The logger now takes its level from the `apps` logger rather than inheriting
  root, so `DISPATCHARR_LOG_LEVEL` is still respected. A level set by anything
  else — a test, or an operator silencing the plugin — is left alone.

- Pinned by tests, including a negative control asserting that a logger
  inheriting a `WARNING` root *would* have dropped the record. 157 offline
  tests, up from 152.

**If you upgrade and suddenly see new log lines from this plugin, that is the
fix working** — those lines were always being emitted, just never recorded.

**Hardened — wrappers survive a Dispatcharr signature change**

- All four wrappers now end their signatures with `*args, **kwargs` and forward
  them at every call site of the original.

  This is not tidiness. A signature mismatch raises during **argument
  binding** — before the function body, so before this plugin's own `_ACTIVE`
  check and before its `try/except`. The fail-open design that protects every
  other failure mode does not cover it, and neither does turning the plugin's
  feature off, because the error precedes any setting lookup. Only disabling
  the plugin outright recovers it. A sibling plugin took a total VOD-playback
  outage this way when a hooked Dispatcharr function gained a keyword argument.

- **Forwarding stops the crash; it does not make the wrapper correct.** If a new
  parameter carries meaning — the release behind that outage added a per-user
  permission allowlist — then a wrapper that replaces core's work, or
  post-processes it, may silently drop a constraint core was enforcing. Nothing
  useful can be decided automatically, so unrecognized arguments are now logged
  once per shape, at `WARNING`, naming the hook.

  Two of the four carry real risk and say so in their docstrings: the
  destructive-merge protection *replaces* core, so any new parameter is one
  nothing honors unless it is taught to — and for that one the usual instinct
  of "when in doubt defer to core" is wrong, because core's behavior on that
  path is the bug. The detail-preservation wrapper repairs core's write
  afterwards, so a parameter making core deliberately write *less* would have it
  refilling keys core meant to omit.

- Also fixed while in there: the destructive-merge protection imported
  `django.db` and the `Movie` model *outside* its own `try`, so an `ImportError`
  would have escaped the handler that exists to guarantee it never falls through
  to core's destructive version. Those imports moved inside, and after the
  delegate path, which needs neither.

- Pinned by parity tests that encode **no parameter list**, so they survive the
  next added argument too: each asserts an unknown argument is accepted *and*
  observed arriving at the original. Accepting without forwarding is the subtler
  bug. 164 offline tests, up from 152 in 1.2.0.

## 1.2.0 — 2026-09-10

Two changes, both about the boundary between what this plugin writes and what
Dispatcharr writes. Neither alters how anything is merged.

**Changed — the detail sweep no longer writes Dispatcharr's fields**

- The sweep used to mark each relation it fetched with core's `detailed_fetched`
  and `last_advanced_refresh`. It now records a timestamp under its own
  `vod_merge` key in the relation's `custom_properties` and leaves core's fields
  alone.

  Those two fields are the **only** record anywhere in Dispatcharr that a client
  asked for a particular movie's detail. Writing them ourselves destroyed that
  distinction: every relation the sweep touched became indistinguishable from
  one somebody actually requested.

  It also suppressed real work. `refresh_movie_advanced_data` skips when
  `detailed_fetched` is set and `last_advanced_refresh` is inside 24 hours, and
  the sweep set both **without calling that function** — so a client asking an
  hour after a sweep got the early return instead of a fetch. That was harmless
  only for as long as the sweep happened to call the same endpoint and store the
  same payload.

  The general rule, which is why this was worth a release: **a sweep must not
  write the field it reads.** This one can obey it because its mechanism is
  distinct from the mechanism that records the signal.

- **Consequence, and it is deliberate:** a relation the sweep has fetched no
  longer looks "already fetched" to Dispatcharr, so opening that movie triggers
  a real provider fetch where previously it did not. The provider's answer is
  the better one, and the sweep's own resumability was never based on that flag
  — it skips relations that already have stored detail — so nothing re-fetches
  in bulk.

- **Marks left by earlier versions are harmless and there is nothing to run.**
  Dispatcharr's 24-hour skip stops treating them as fresh after a day, so they
  suppress no work, and any sensible reading of the record is "asked for
  recently" — a window those timestamps have already left. Only a query of the
  form "has this *ever* been fetched" still sees them.

**Added — `Preserve essential movie detail`, default ON**

- Every time Dispatcharr fetches a movie's detail it **replaces** the stored
  payload wholesale. Providers are not consistent between calls, so one that
  returned a full stream description last time and a plot summary this time
  silently erases the difference.

  Three keys are worth more than the payload that carries them, and are now put
  back if a fetch drops them: **`tmdb_id`**, this plugin's strongest movie
  signal, whose loss quietly makes a mergeable duplicate unmergeable again;
  **`video`**, the measured resolution and codec; and **`audio`**, which has no
  fallback at all downstream — absent means "no opinion", so losing it does not
  degrade a quality comparison, it removes one side of it.

  A value the provider actually sent always wins. This only refills a hole, and
  never reinstates stale data over a real answer.

- Costs nothing in the case it was built for: a relation with no stored detail
  has nothing to lose, so a bulk pass over fresh relations pays one extra query
  each and writes nothing.

- Restored keys are written to the audit log with their own action and tier, and
  `Show status` reports whether the guard is live in that worker — the same
  reasoning as the 1.1.0 protection: it is invisible when it is working.

- **Not gated by `Dry run`**, and **fails open** — the opposite of the 1.1.0
  protection, on purpose. That one fails closed because what it replaces is
  destructive. Here there is nothing destructive to fall through to: core's
  refresh is a feature someone is waiting on, and a fault in the guard must not
  cost them the fetch.

**Changed — settings help text**

- `Prevent destructive movie merges` and `Preserve essential movie detail` both
  have much shorter help text. The previous wording explained the upstream bug
  in full, which belongs in this file and the README rather than in a settings
  panel. Behavior is unchanged.

**Note on patching**

`refresh_movie_advanced_data` is referenced from two places and they bind it
differently: `apps/output/views.py` imports it inside the function, so it
resolves at call time, while `apps/vod/api_views.py` imports it at module level
and keeps its own reference. Patching only `apps.vod.tasks` reaches the second
caller when that module is imported after the plugin, which is true at boot but
**not** when the plugin is enabled without a restart. Both namespaces are now
patched, and both restored on disable. (The 1.1.0 protection is unaffected —
`handle_movie_id_conflicts` is resolved inside `tasks.py` itself.)

152 offline tests, up from 135.

## 1.1.0 — 2026-09-06

**Added — protection against a destructive Dispatcharr movie merge**

- **New setting `Prevent destructive movie merges`, default ON.** This fixes an
  upstream bug and is independent of any merging this plugin does.

  When an on-demand movie refresh finds a TMDB id in the provider's detail that
  already belongs to a different `Movie` row, core's
  `handle_movie_id_conflicts` keeps the movie being refreshed and **deletes** the
  pre-existing one. On that path the movie being refreshed is the freshly-minted
  duplicate — the listing had no id, so it keyed by name+year, and
  `lookup_by_name_year` cannot see the id-bearing canonical — so the row deleted
  is the established one holding most of the relations, the better name, and the
  UUID downstream tools have indexed. It fires on any UI movie open or XC
  `get_vod_info` for an affected title.

  It can also abort part-way: to dodge a partial unique index it first nulls the
  canonical's `tmdb_id` in a standalone write with no transaction, and if any
  id-less row already occupies that `(name, year)` Postgres rejects it, the error
  is swallowed into a return string, and `detailed_fetched` never flips — so the
  relation re-fetches and re-fails indefinitely.

  With this on, the duplicate's relation is repointed onto the canonical instead
  and nothing is deleted. That is the direction **core's own caller already
  implements** — `refresh_movie_advanced_data` has an `if relation_updated:`
  branch for exactly this, unreachable today only because the function returns
  `False` on every path.

- **Deliberately minimal: it does not call core's `merge_movie_data`.** The
  canonical is by construction the better-populated row, the duplicate's
  listing-derived content is already stored on the relation, and
  `merge_movie_data` writes ids — `tmdb_id` and `imdb_id` are both `unique=True`,
  and its `elif source_movie.imdb_id:` branch saves the target while the source
  still holds that same unique value, a third collision separate from the two
  defects above. A single foreign-key update writes no ids at all, so no
  uniqueness constraint is reachable and the `IntegrityError` class disappears
  rather than being handled.

- **Not gated by `Dry run`**, on purpose: dry run means "do not inject", and
  letting it disable a protective patch would mean dry run permits the data loss.

- Fails **closed**: any error takes no action rather than falling through to
  core's destructive version. A missing `handle_movie_id_conflicts` (if upstream
  changes it) logs a warning and leaves the rest of the plugin working.

- Prevented merges are logged with their own action and tier so they can be
  reviewed as a class, and `Show status` reports whether the protection is
  installed in that worker — necessary because a working protection is invisible:
  nothing bad happening looks identical to it never firing.

**Note on verifying the install**

This patch matters in the **uWSGI request workers**, not the Celery children —
both callers of `refresh_movie_advanced_data` are inline calls despite its
`@shared_task` decorator. The batch wrappers are the opposite. The install log
line now reports both, so check for it in a web-worker pid as well.

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
  *unattended* behavior, and an approval is one entry a person typed.

- **A `Merge series` switch.** Movies had one and series did not, so the only way
  to stop series merging was to guess at the account allowlist. Each kind now has
  its own switch and its own allowlist, and they are independent.

  The two default differently — series on, movies off — so that neither an
  upgrade nor a fresh install changes behavior on its own. `Dry run` is the real
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
  just the detail lookup. Behavior unchanged; the label was wrong.
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
