# Dispatcharr VOD Merge & Enrich — design

## Problem

Dispatcharr merges VOD entries across providers **only at listing-scan time**, by
a key derived from the raw provider payload with strict priority
`tmdb_<id>` > `imdb_<id>` > `name_<name>_<year>` (`apps/vod/tasks.py`,
`process_series_batch` / `process_movie_batch`).

A provider that omits the TMDB id therefore keys by its own name string and
creates a **separate** `Series` or `Movie` row — a duplicate of a title already
in the library. It cannot recover on its own: `lookup_by_name_year` matches only
rows whose tmdb **and** imdb are both null, so the duplicate can never fall back
onto the tmdb-bearing canonical.

Series have no self-heal path at all — `handle_series_id_conflicts` exists but
has zero callers, and `refresh_series_episodes` reads the detailed
`get_series_info` payload while ignoring any id in it.

Movies have one, and it is worse than nothing; see
[Why this never calls the core movie refresh](#why-this-never-calls-the-core-movie-refresh).

On the library this was built against, **~90% of one provider's catalog was
duplicate content.** De-duplication is not a tidiness feature here — it is the
precondition for syncing that provider at all.

## Why not merge the rows afterwards

Because the scan **re-derives** `relation -> series` / `relation -> movie` from
the provider payload on every run (its `bulk_update` field list includes those
FKs). Any after-the-fact row merge is undone by the next VOD refresh, which
re-keys the entry by name+year and mints a fresh orphan.

Durability requires injecting an id **before the merge key is computed**. That
single constraint determines the entire architecture: this plugin has to sit
inside the scan, not beside it.

## The signals: shared external artifacts

Series match on poster then plot. Movies match on detail, then poster, then plot.
A manual approval outranks all of them.

None of these is fuzzy similarity. Poster and plot are artifacts the providers
copied from the same upstream source, so a match is an **equality test, not a
judgement** — which is why they need no human review.

**1. Detail id (movies only).** A movie's `get_vod_info` response carries
`info.tmdb_id` outright: the provider's own assertion, no inference. It exists
only once the sweep has fetched it, and some providers omit it.

**2. TMDB poster basename.** Providers that omit the id very often still serve
TMDB-hosted artwork, and a TMDB asset path is unique to a title. Only the size
segment differs:

```
Provider 1  .../t/p/w600_and_h900_bestv2/acYXu4KaDj1NIkMgObnhe4C4a0T.jpg
Provider 2  .../t/p/w154/acYXu4KaDj1NIkMgObnhe4C4a0T.jpg
Provider 3  .../t/p/w500/acYXu4KaDj1NIkMgObnhe4C4a0T.jpg
```

Measured: 13,678 tagged series basenames / 2 collisions; ~43,000 movie basenames
/ 6 collisions.

**3. Plot text.** The listing's `plot` is usually TMDB's overview verbatim, so it
matches byte for byte across providers. Measured on the series side: 10,101
usable hashes / 4 collisions, with a 40-character floor.

This tier beats name matching structurally — in live testing it matched a
canonical titled in another language, one truncated with an ellipsis, and one
whose name shared almost nothing with the duplicate's.

**Uniqueness guard, identical for both derived tiers:** a key is usable only if it
maps to **exactly one** tmdb id across the tagged library. That one rule makes
placeholder artwork and boilerplate blurbs — which would otherwise merge hundreds
of unrelated titles in a single pass — reject themselves automatically.

### What this trusts

Not the provider's id (they supply none) but their metadata being **internally
consistent**: that the poster and plot they attached belong to the title they
attached. A provider that bolts the wrong synopsis onto a title propagates that
error here. The matching tier is recorded on every log entry so plot-tier merges,
the ones actually exposed to that failure, can be audited as a class. A shared
image asset is much harder to get inconsistently wrong than prose.

## Mechanism

`process_series_batch` and `process_movie_batch` are called **inline** from
`refresh_series` / `refresh_movies` — not via `.delay()` — and their `batch`
argument is a list of the raw provider dicts whose id key becomes the merge key.

So this module **wraps** both functions and mutates entries in place before
delegating. It never forks the function body. The compatibility surface is
therefore the two signatures plus the dict keys read and written, rather than
several hundred lines that need re-diffing every release.

Note the movie key reads `tmdb_id` **before** `tmdb` — the reverse of the series
path — so the injected key name differs between the two.

Injection failures are swallowed by design: a bug here degrades to "no merging",
never to a broken VOD ingest. The cost of that choice is that a silently disabled
feature and a working one look identical from outside, which is why every failure
path logs a distinct marker. (This is not hypothetical — movie audit logging was
dead for several versions while merging worked correctly, and the only evidence
was one log line.)

### Category scope

The scan fetches the provider's **whole listing**, not just enabled categories;
core discards disabled ones *after* the batch processor runs. The plugin
therefore mirrors core's `category_enabled()` check. Without that mirror the
audit log fills with phantom merges for content that is never synced — measured
at ~5,000 logged against 438 real.

### Durability

Once injected, every subsequent scan re-derives the same `tmdb_<id>` key, finds
the canonical, and repoints the existing relation onto it. **The duplicate cannot
come back.** New arrivals from an untagged provider merge at first sight, so no
orphan is created for them at all. Orphan rows left with no relations are pruned
by the scan itself, and relation rows keep their ids — they move via
`bulk_update`, which does not even fire `auto_now`.

Verified live across consecutive full refreshes. That test is the point of the
design, and it is the one to re-run after any change here.

## Direction guards: what may create versus merge

Poster and plot resolve **against the tagged index**, so by construction they can
only land on a row that already exists. They merge; they can never mint a new
row. This is a structural property, not a check that could be forgotten.

Only the detail tier can create a newly tagged row, and only when
`tag_unique_movies` is on. That gate exists because injecting an id no other row
holds does not merge anything — it re-tags a title that already has its own
entry, changing its Dispatcharr id. Useful long-term (it stops the row churning
on provider renames) but it is churn without dedupe, so it is opt-in.

Manual approvals ignore that gate: it bounds *unattended* re-tagging, and an
approval is one line a person typed.

## The detail sweep

The sweep is the only step that cannot happen inside a scan. Poster and plot work
off data the listing already carries; the detail tier needs an HTTP call per
relation, and hundreds of those mid-ingest would stall it. So it runs on its own
nightly timer and the scan consumes what it stored — with no HTTP at all.

Three properties, each measured rather than assumed:

- **It skips anything a free signal already reaches.** Spending a provider call
  on a relation the poster index merges for nothing is pure waste; on one
  provider that was 671 of 900 in a single category.
- **It runs newest-first.** The newest 40 id-less relations returned a tmdb
  40/40, while the oldest 50 returned 1/50. Old bulk imports are untagged
  long-tail content; recent arrivals are both well-described and the ones
  actually duplicating the library. Oldest-first would spend thousands of calls
  on the least useful end.
- **Its candidate set shrinks permanently.** A relation whose detail is stored is
  never fetched again, whether or not it yielded an id. Cost is one-time per
  relation, not nightly.

It runs **unbounded**, unlike the manual action. `sweep_limit` exists only
because the button runs inside a web request and a long run would time it out;
nothing constrains a beat task that way, and bounding it would mean a large new
category took a week to drain at 50 a night.

Measured on the first live run: **406 relations swept, 342 merged — 84%.** The
64 that did not are titles no provider tags at all.

### Why the `dvr` queue

A plugin `@shared_task` sent to the default prefork queue is **rejected**. That
worker's consumer runs in the main process, which never imports plugins, so it
sees an unregistered task name and drops the message — silently, worker-side.
The threads-pool `dvr` worker registers plugin tasks in its consumer. That is the
entire reason for the queue override, and why the setting is marked advanced.

### Why this never calls the core movie refresh

The obvious implementation would call `refresh_movie_advanced_data`, which
already fetches exactly this payload. It must not, for two independent reasons.

**It merges in the destructive direction.** That task routes through
`handle_movie_id_conflicts`, whose "preserve the user's selection" policy keeps
the movie being refreshed and **deletes** the pre-existing one. The movie being
refreshed is, on this path, always the freshly-minted orphan — so the canonical
carrying most of the relations, the curated name and the established UUID is
deleted in favor of a one-relation row named after a provider's raw listing
string. It fires precisely when the detail's tmdb belongs to another row, which
is every candidate here. Running it across thousands of relations would rewrite
the library.

**It can abort part-way.** To dodge the partial unique index
`UNIQUE(name, year) WHERE tmdb_id IS NULL AND imdb_id IS NULL`, it first nulls
the canonical's `tmdb_id` in a standalone write with no surrounding transaction.
If any id-less row already holds that `(name, year)` — which is exactly the
duplicate being healed whenever the names match — Postgres rejects it, the error
is swallowed into a return string, and the relation is left permanently
re-fetching with `detailed_fetched` never flipping.

Fetching the payload ourselves and letting the **scan** do the merging avoids
both. It also means the sweep writes `detailed_info` itself rather than
delegating, which matters because the core task throws before its relation save
on exactly the colliding rows.

Once a movie has been merged the core task becomes safe again, because the id it
would set is already set.

### And we now stop it happening at all

Avoiding the path is not enough, because *anything* can reach it — a UI movie
open, an XC `get_vod_info`, a client that fetches detail in bulk. So as of 1.1.0
the plugin also **replaces `handle_movie_id_conflicts`** with the direction the
caller already implements: repoint the relation onto the existing row and
`return existing_movie, True`.

Deliberately minimal — it does **not** call core's `merge_movie_data`. Three
reasons. The canonical is by construction the better-populated row. The orphan's
listing-derived content is already on the relation. And `merge_movie_data` writes
ids: `tmdb_id` and `imdb_id` are both `unique=True`, and its
`elif source_movie.imdb_id: target_movie.imdb_id = source_movie.imdb_id` branch
saves the target while the source still holds that same unique value — a third
collision, separate from the two defects above. A single FK update writes no ids,
so no uniqueness constraint is reachable and the whole `IntegrityError` class
disappears rather than being handled.

Two properties that differ from the rest of the plugin, both on purpose:

- **It is not gated by `dry_run`.** Dry run means "do not inject"; letting it
  disable a protective patch would mean dry run permits the data loss.
- **It fails closed.** The injection path fails open, degrading to "no merging".
  This one takes no action on error rather than falling through to core's
  destructive version — the benign outcome is for core's caller to set the id
  itself, which it does when we return `False`.

Note also that it matters in a different place: both callers of
`refresh_movie_advanced_data` are **inline**, so this patch is exercised in the
uWSGI request workers, while the batch wrappers are exercised in the Celery
children. Install happens at import either way, but the verification differs.

Both defects are also reported upstream; the local patch means we are not waiting
on that.

### The patch has to go in more than one namespace

`refresh_movie_advanced_data` is referenced from two places, and they bind it
differently. `apps/output/views.py` imports it *inside* the function, so the
name is resolved at call time and replacing the module attribute reaches it.
`apps/vod/api_views.py` imports it at module scope and keeps its own reference
to whatever the attribute pointed at when that module was first imported.

Patching `apps.vod.tasks` alone therefore reaches the second caller only when
`api_views` is imported *after* the plugin. At boot it is, because the URLconf
loads lazily — but not when the plugin is enabled without a restart, and that
is precisely the case where someone is about to click on a movie to see whether
it worked. So both namespaces are patched (the second only when already
imported — importing it ourselves during startup would drag the API layer in
early) and both are restored on disable.

The 1.1.0 protection needs none of this: `handle_movie_id_conflicts` is
resolved as a module global inside `tasks.py` itself, so it is covered whoever
calls it.

## Two writers, one field

As of 1.2.0 the plugin observes a rule that took a while to state properly:
**a sweep must not write the field it reads.**

Dispatcharr records that a movie's detail was fetched with two fields on the
relation, `detailed_fetched` and `last_advanced_refresh`. Between them they are
the only record anywhere that a **client** asked for that movie. The detail
sweep used to set both, which destroyed the distinction — every relation the
sweep touched became indistinguishable from one somebody had requested — and
also suppressed real work, because `refresh_movie_advanced_data` skips when
those two say the data is fresh. The sweep set them without calling that
function, so a client asking an hour later got the early return instead of a
fetch.

The sweep now writes a timestamp under its own `vod_merge` key and leaves
core's fields alone. It can afford to because **its mechanism differs from the
mechanism that records the signal**: the sweep fetches detail itself rather
than delegating to the function that stamps the fields. Where that separation
does not exist the property is unobtainable — a series episode sweep's action
*is* the function that writes the field, so doing its job necessarily destroys
the signal, which is why that problem needs an explicit watchlist and a
hand-written TTL. Here the timestamp is a TTL for free, and stays one only as
long as we keep our hands off it.

Storing our key on the **relation** rather than the `Movie` row is deliberate:
`process_movie_batch` merges relation `custom_properties`
(`{**existing_rel_cp, 'basic_data': ...}`) rather than replacing them, so our
key survives every scan by the same mechanism that keeps `detailed_info` alive.

### The same boundary, from the other side

The inverse problem is that core's detail write *replaces* `detailed_info`
wholesale, so a thinner payload erases a richer one. Here core is the hazard
rather than the signal, and the fix is to repair after it: keep a snapshot,
call the original, and refill only the essential keys — `tmdb_id`, `video`,
`audio` — that the new payload left absent or blank. A value the provider
actually sent always wins, so this can only ever restore a hole.

Two details worth recording. `clean_custom_properties` drops `None`, `''` and
`[]` but **not** `{}`, so an empty `video` block reaches storage looking like
data and has to be treated as absent. And the guard **fails open**, unlike the
merge protection above: there is nothing destructive to fall through to, and a
fault in a repair step must not cost a user the fetch they are waiting on.

The write is a read-modify-write of the relation's `custom_properties`, the
same pattern core itself uses, so a concurrent writer on the same relation
could in principle be clobbered. The window is one query wide, the two writers
would have to be refreshing the same movie simultaneously, and the outcome is a
restored key rather than a lost row — not worth transaction machinery.

## A third guard: the cleanup that trusts an empty answer

The other two guards protect a row or a field. This one protects the catalog.

Every scan ends with `cleanup_orphaned_vod_content(account_id=…,
scan_start_time=…)`, which runs at `stale_days=0` — so the cutoff is the scan's
own start and anything not re-stamped *during that scan* is stale. It deletes
those relations, then makes a second, deliberately **unscoped** pass deleting
every `Movie` with no relations left from any account.

The correctness of all that rests on one unstated assumption: **that the scan
saw the provider's real answer.** When the movie endpoint returns an empty list
the scan sees nothing, every relation on the account is stale, and the pass
removes the account's whole catalog plus every title it solely supplied. The
rows return on the next good scan with new primary keys, so the damage is not
just deletion but an id churn for everything downstream.

Two things make it hard to notice. It is logged as routine cleanup at INFO, and
the relation count — the larger number, and the actual cause — is computed and
then discarded, so only the orphaned-*movie* count is visible.

The structural version of the lesson is worth stating plainly, because it is not
specific to this function: **"the source answered successfully" is not "the
source answered completely".** Anywhere a deletion is driven by absence from a
fetched list, an empty or truncated fetch is indistinguishable from a genuine
removal, and the default reading is the destructive one.

Core already applies the right reasoning one level up — `refresh_categories`
returning nothing aborts the refresh "to preserve existing category selections".
It was simply never carried down to the movie and series lists.

So the guard refuses rather than repairs: before delegating, count how many of
that account's relations would survive core's own filter, using core's own
cutoff arithmetic so "seen" means exactly "not stale". If a content type has
relations and **none** survive, skip the cleanup for that scan. A deferred
cleanup costs a day of stale rows; the alternative costs the catalog.

**Zero, not a ratio.** A threshold would also catch a listing that came back 90%
short — but a genuinely shrinking catalog would then be refused permanently,
because the rows that were never pruned keep the total high and the ratio can
never recover. Zero-seen carries no such state: one good scan clears it. The
short-listing case is real and is left upstream.

**This one fails open**, like the detail guard and unlike the merge protection.
Core's cleanup is normally correct and only catastrophic under one condition, so
an error in our own check is not evidence that the condition holds, and
permanently disabling a correct cleanup would be its own slow data problem. The
counts are two trivial queries; if those fail, core's much larger ones were not
going to succeed either.

Only the per-account, per-scan call is assessed. An unscoped or timestamp-less
invocation cannot be judged against a scan at all, so it is delegated untouched
— a narrower patch than the bug strictly allows, but the bug only occurs on the
path that is checked.

The refusal is returned as core's own result string, so it appears inside
Dispatcharr's `VOD cleanup completed: …` line. That matters more than it looks:
plugin loggers inherit root, and root is `WARNING` in the Celery prefork child
where scans run, so a message routed through `apps.vod.tasks` is visible in
places our own logger historically was not.

## Enrichment: whose job is it to know what matters

Ranking candidate copies needs measured video and audio for **every** copy of a
movie, not one. Two constraints shape the whole design.

**The XC detail endpoint refreshes exactly one copy per movie** — it resolves
the id, filters to active accounts, and takes
`order_by('-m3u_account__priority','id').first()`. That is structural. No amount
of configuration makes a client-side caller cover all N, so a client-driven
harvest can only ever reach 1/N, and on a real library far less than that when
the winning account is one that supplies no technical detail.

**Our own sweep has no such limit**, because it calls the provider per relation
directly rather than going through the endpoint. That asymmetry is the entire
reason this lives here rather than in the tool that knows which titles matter.

**But we do not know which titles matter, and cannot.** That is demand, and it
exists only in the thing doing the syncing. Everything else — how many copies a
movie has, which providers carry it, what detail is already stored — is a query.
So the contract carries exactly one thing and the rest is looked up.

### Fail open on enrichment, fail closed on scope

The two halves fail in opposite directions, and the asymmetry is the point.

A fault in the enrichment costs some missing metadata. Nothing is destroyed, and
a user waiting on data should not lose it to an over-careful guard. That half
fails open.

A fault in the thing that decides **how much work to do** is different in kind:
the failure mode is thousands of provider calls against a set nobody asked for.
So every way the input can be untrustworthy — absent, unreadable, malformed,
unknown schema, a count disagreeing with its own array, or a timestamp too old
to believe — collapses to doing nothing. It must never widen.

Absent and unreadable are reported separately. They are indistinguishable in
effect, and absent is defined as "do nothing", so collapsing them would turn a
permissions mistake into a silent no-op with no symptom. The one that is normal
logs INFO; the one that is a misconfiguration logs WARNING.

### Stamp the attempt, not the success

The skip rule keys on *was this tried*, not *does this have data*.

Keying on data looks equivalent and is not. The copies that never yield anything
are the ones whose providers do not supply it — so a data-keyed rule re-asks for
exactly those, every run, forever. They are also the ones a later measuring pass
pays the most for, which makes it precisely the wrong set to keep retrying.

Recording the outcome alongside the attempt is what lets the distinction be
drawn later: a provider that answered and had nothing is a fact about that
stream, while an error is a fact about that moment. Only the second is retried,
and only after a week.

### Order by movie, not by relation

The detail sweep walks newest-relation-first, which is right for it: new
arrivals are the ones duplicating titles already in the library.

Enrichment must not. Providers cluster in id ranges -- overwhelmingly so for one
that has been wholesale recreated -- so newest-first walks a single provider's
copies across every movie before reaching the next provider's. Measured on a
real wanted set, the first several batches all went to one provider.

The cost is not slowness, it is shape. That ordering leaves every movie
partially covered for the whole backlog, and partial coverage is the one state
ranking handles worst: a copy with no audio data scores zero, so a half-measured
set can rank an enriched stereo track above an unmeasured surround one. Grouping
by movie keeps coverage uniform across whatever has been completed, and makes
the movie the unit a bounded run may stop at.

A movie with more copies than the batch size is taken whole anyway. The
alternative is that it never gets enriched at all -- and a title with many
copies is precisely the one where ranking matters most.

### Measuring: a different cost class, and what follows from it

A detail call is one request, kilobytes, milliseconds, and no contention worth
modeling. A measurement opens the media and holds a provider connection slot
for seconds — the resource playback competes for, and the one a sibling plugin
exists entirely to manage. Plausibly a hundred to a thousand times the cost per
copy, which is why scope matters here far more than it did for merging, where
one call settled one relation for ever.

Everything structural follows from that. Reads are bounded so a measurement
describes the stream without pulling the file. The per-run cap defaults to three
rather than twenty-five. And repeated failure against one provider stops that
provider for the run rather than continuing to spend slots on something that is
not answering.

### Repeated failure is a fact about the provider

The subtle part is not stopping, it is what gets recorded.

Treating each failure as a fact about its stream would be reasonable in
isolation and wrong in aggregate: during an outage every copy that happened to
be queued gets stamped as failed, and the retry rule then declines all of them
for a week. A bad hour becomes a week of missing data, invisibly, and the longer
the outage the more copies it poisons.

So failures are held rather than written. If the breaker trips for an account,
they are not written as errors — that account simply was not measured this run
— but they are marked held (see below). If it does not, they are written and
the normal retry applies. The rule is one pure
function so it can be tested directly, because it is the kind of logic that
looks like bookkeeping and is actually the difference between an outage costing
an hour and costing a week.

### A bad stream is not a provider outage

The breaker's subtle half — record nothing when a provider trips it — has a
failure mode of its own. Unrecorded copies stay queued, and the queue is in a
fixed order, so if what tripped the breaker was three bad STREAMS next to each
other rather than an outage, every later run reaches the same three first,
trips again, and the provider's measuring stops for good. It happened: three
copies that a provider answered `http 400` for, minutes later and with sensible
extensions, so not a timing problem and not a malformed URL.

The distinction is whether the provider answered. `400`, `404` and `410` are
answers about one request from a provider that is up, so they are recorded
against the copy and never counted toward the breaker. `401`/`403` stay on the
provider side because they usually mean the account. The cost is bounded: a
provider that began answering `400` to everything would see its queued copies
each wait a week instead of being protected — time, not data, and the pre-flight
sign-in still catches a provider that is actually down.

### Held copies go to the back of the queue

Classifying the answer fixed the case where the provider *answered*. It did not
fix the root of the trap, which is that an unrecorded copy is first again next
run. A stream that never answers at all — it hangs until the timeout — is
indistinguishable from an outage one request at a time, so it still counts
toward the breaker. Three such copies at the front of a queue trip it at the
start of every run, and that provider's measuring stops for good. It happened,
with timeouts this time.

So a failure the breaker discards is no longer left unmarked. It is marked
**held**: still due, with no retry wait, but every held copy goes after every
other copy when the queue is built, for lookups and measuring alike. The two
cases the breaker exists to tell apart now both come out right:

- **An outage** costs nothing. The held copies are retried on the next run,
  just later in it.
- **A few bad streams** cannot block the queue. The next run reaches the
  copies behind them first.

A held copy that is held again counts up; on the third consecutive hold it is
written as an ordinary error with the normal retry window. A copy that only ever
fails is therefore tried a bounded number of times rather than every night, and
one that failed during an outage needs three trips in a row — each of which
reached it only after everything else — before it waits a week. Any other mark
resets the count, so only *consecutive* holds add up.

### Two destinations for one measurement

Results go to a key of our own and are mirrored into the detail Dispatcharr
already reads.

The separate key is the durable record: nothing else writes it, so a later
provider refresh cannot destroy a DV record — the one thing only a measurement
produces. The mirror is what makes the data usable today, by the readers that
already exist, with no change to the plugin that consumes it. It only ever fills
a gap, so a value a provider actually sent still wins.

Writing only to the mirror would have been simpler and was rejected: a provider
that *sometimes* returns a video block could overwrite a measured DV record with
one that lacks it, and nothing would report the loss.

### One action, two steps, cheapest first

Lookup and measurement began as separate actions, and that let them run in the
wrong order: the measuring pass picked any copy without video and audio,
including copies whose provider had simply not been asked yet — spending a
connection-holding measurement where a light request would have filled the gap
for free. On a provider that supplies detail nearly always, every copy it
carried was being counted as needing measurement.

The fix is a rule and a shape. The rule: a copy is measured only once its
lookup has been **tried** — any attempt, including an error, since a provider
whose detail endpoint always fails must still be measurable. The shape: one
action that runs the lookup batch and then, if measuring is on, measures what
the lookup could not fill. Ordering stops being something a user has to know.

The two batch sizes stay separate because the two costs are different in kind,
and measuring keeps its own switch because that is the one real decision —
whether to spend provider connections at all. An untrustworthy wanted set
stops both steps, not just the first.

### Enrichment runs in the background, one run at a time

The first version measured inside the web request, and timed one out: a probe
may hold a connection for thirty seconds, so even a batch of three can outlast
a request, and a batch of lookups at about a second each comes close on its
own. The button now only enqueues, on the same queue as the nightly sweep and
for the same routing reason.

Moving the work off the request created a problem the request had been hiding:
nothing stopped two runs. A button that waits cannot easily be pressed twice; one
that returns at once can. On a one-connection account two concurrent runs are
the same collision as a probe against playback, so overlap is excluded by a lock.

Two choices in it are deliberate. The lock is taken **inside the task**, not
checked at the button — a check at enqueue time cannot stop two queued runs, and
a future scheduled run must respect it too. And it is **short-lived and renewed
before every lookup and every probe** rather than sized to the whole run, so a
killed worker frees it in minutes, while a run that cannot confirm it still
holds it stops. That last rule fails closed for the same reason scope does: a
missed run costs data, a collision costs someone's stream.

### Not probing a provider that is busy

On a one-connection account a probe during playback is not a risk but a
certainty: one of the two connections gets dropped, and it may be the viewer's.
That makes this check a prerequisite for any unattended run, not a refinement.

Dispatcharr counts every real stream, live and VOD alike, in one per-profile
counter, and exposes a non-mutating check of it — the one the VOD proxy asks
before admitting a viewer. The probe asks the same question, in the same order
(default profile first, active profiles only), **before every probe** rather
than once per run, and probes through the profile that answered, so the slot
checked and the connection opened belong to the same login. A busy account is
left alone for the rest of the run and nothing is stamped against its copies;
they were not attempted.

Accounts are also checked once **before the batch is chosen**, and busy ones
left out of it. The first live run showed why: the batch is taken in title
order, the next title's waiting copies were all on the busy provider, and a
batch chosen first and checked later measured nothing while another provider
had copies waiting. The per-probe check stays, for playback that starts
mid-run. Not being able to tell — the check failing, or a future
Dispatcharr moving it — counts as busy, and an unloadable check stops measuring
altogether. Not knowing whether someone is watching is not permission to
interrupt them.

The obvious stronger design is to **reserve** a slot for the probe, so
Dispatcharr sees it and steers viewers elsewhere. It was rejected on two counts.
Core has no cleanup for a leaked counter, so a worker killed mid-probe would
leave a one-connection account looking full — refusing every viewer — until
Redis restarts: a rare event with an unbounded cost. And while a probe held the
slot, an arriving viewer would be refused outright (the proxy answers 429)
instead of merely risking a collision. Checking without reserving leaves one
bounded window — playback that starts during a probe of at most thirty
seconds — and a measurement cut short in that window is already recorded as a
retryable error rather than a fact about the stream.

### One lane per provider login

Measuring ran one copy at a time across every provider, and on a large
wanted set it is most of a night's time. The serialization protected
nothing: each provider's connection limit is its own, so one copy per provider
at once is no more load on any of them than one copy in total. Lanes are keyed
on the provider LOGIN (host and username), not the Dispatcharr account,
because Dispatcharr lets several accounts share one login and they share its
limit too; an account whose login cannot be read gets a lane of its own, which
is exactly the old behavior.

Everything per provider stays per provider: the busy check before each probe,
the not-answering check, the breaker and its held failures. Shared counters
are touched only under one lock (a test walks the lane and fails on any write
outside it), each lane checks a shared stop flag before every probe so a time
limit or lost run lock halts them all promptly, and each thread closes its own
database connection.

### Learning which accounts never describe their copies

On a large wanted set, many lookups go to accounts that have never once
returned video and audio; every one of those copies goes on to be measured
anyway. Naming those providers would over-fit one installation, so the
rule is learned: count each account's answered lookups (errors and skips say
nothing about it) and, after a sample of 50 with none carrying video and audio,
mark that account's remaining copies as looked up, with a mark of their own,
so every downstream rule treats them as a lookup that came back empty. One
answered lookup with video and audio keeps the account looking up for good.

The status uses the same rule, and its nights estimate stopped counting only
the work that is visible: a fresh wanted set has nothing measurable until its
lookups run, so it reported days for weeks of work. It now projects each
account's future measuring from that account's own fill rate (assuming the
worst on a small sample) and times measuring by the busiest lane.

### A provider that is not answering

Measuring had a breaker from the start; lookups did not, and the asymmetry was
the dangerous kind. A lookup failure is recorded against its copy with a
week-long retry, so on a nightly run against a large wanted set an outage would
not merely waste requests: batch after batch would park the provider's backlog
for a week, and the only symptom would be a stubborn error count.

So lookups now get both defenses. A pre-flight sign-in per provider, before the
batch is chosen (the busy check taught that a check made after choosing lets a
skipped provider's copies fill the batch); it fails open, because wrongly
skipping a healthy provider costs it a night. And a breaker for an outage that
begins mid-run, whose failures are held rather than stamped as errors, using
the same functions the measuring side uses for exactly that decision.

### Checking described 4K copies for Dolby Vision

"Essential" was defined as resolution and audio, which a provider can supply.
That left one blind spot: a copy the provider described is never measured, so
its Dolby Vision status is unknowable, while an undescribed copy is measured
and gains it. Avoid-DV would work for some movies and silently not for others,
indistinguishably — and since detection is positive-only, an unmeasured
no-fallback stream is not "known safe", it is invisible and can win selection.

The fix is narrow on purpose. DV lives on 4K copies, so only copies the
provider described as 4K are checked, using the ranking plugin's own 4K rule
(copied, not imported — the plugins never depend on each other) so the two
agree on what 4K means. They go after gap-filling within the same budget: a
copy with no resolution or audio costs ranking more than an unknown DV status.

The subtle part is where the result lands. The mirror into the detail the
ranking plugin reads fills holes, and a described copy has no hole — its video
block exists. Left alone, the DV record would sit only in our own key, where
nothing that acts on it looks. So the same rule is applied one level down: the
one key a provider never sends is added into the provider's video block, and
every value the provider did send is kept.

### Running unattended

The busy check was the prerequisite: an unattended run cannot choose its
moment, so it has to be able to tell for itself that a provider is in use. With
that in place, the nightly run is the manual run repeated — same lock, same
order, same checks — under a time limit.

One guard object is consulted before every lookup and every probe. It keeps the
run lock alive and enforces the deadline, and it remembers which of the two
said stop, because "time limit reached" is the expected end of every night with
a backlog and "lost the run lock" is not. They must not read alike in the
record.

The loop ends on the first batch that tries nothing. Two details keep that
honest. Copies with no stream URL are never stamped, so they are picked again
every batch; counting them as progress would spin until the deadline. And a
provider whose breaker trips is carried into a skip list for the rest of the
night: its failures are deliberately not stamped, so without the list every
later batch would choose the same copies and ask a provider that has already
shown it is not answering. A safety cap on batches backs both up.

The time limit is never unlimited, unlike the batch sizes. "0 means no limit"
is a reasonable convention for a batch someone starts by hand; for a run nobody
is watching, an unbounded night runs into whatever is scheduled after it.

The timer is written into Dispatcharr's scheduler on Enable and at startup, the
same as the merge sweep's. Rather than leave a switched-off feature running
until the next restart, the task re-reads the switch when it fires.

## Previewing an account before adding it

Scope is a setting whose undo is itself a change: adding an account merges at
its next scan, removing it unwinds those merges, and dry run cannot stand in
for a preview because it unwinds *existing* merges too. The
preview therefore runs the real decision function over the account's id-less
copies with merging switched on in a copy of the settings, and writes nothing.

What it counts is chosen for the risk, not the activity. A copy moving is not
the event that matters; the title it leaves is. An id-less title whose every
copy moves is left empty and pruned, so its id dies, and anything outside
Dispatcharr that stored that id breaks. That is decided by the copies left
behind across *every* account, so the count of a title's copies is taken over
all of them, not just the account being previewed.

With no usable wanted set the synced counts are reported as unavailable rather
than as zero: "nothing you sync is affected" is the reassuring answer, and it
must never be given by default.

## Manual approvals

Some entries are unreachable by every signal: a provider can ship a title as a
bare name — empty `stream_icon`, no plot, no id in the detail payload — and that
includes mainstream films, not only obscure ones. Nothing can be derived from an
entry carrying no metadata, so the only correct answer is to let a person supply
the id.

An approval is trusted completely: it outranks every derived tier, bypasses the
variant guard (approving a variant by name *is* the override), and ignores
`tag_unique_movies`. The denylist still overrides it. Keyed on
`(account, external_series_id)` for series and `(account, stream_id)` for movies.

## Variant editions are deliberately not merged

A separate row is the only way Dispatcharr can represent "same title, different
edition" — relations carry no edition label. Merging a black-and-white cut into
the canonical would make its streams silently selectable for the normal version.
Leaving it unmerged keeps it as its own library entry, which is the faithful
representation until upstream has a notion of editions.

This guard is load-bearing for **both** derived tiers: an alternate cut shares the
canonical's poster *and* its plot, so neither tier can distinguish it.

Quality tags (`4K`, `1080p`, `UHD`) are not variants — a 4K copy is the same
edition and should merge.

## Known limits

- **The plot tier does much less for movies than for series.** It is a backup
  signal, and movie listings largely carry no description: against ~43,000 movie
  poster keys, the movie plot index held 6. It costs nothing to keep and the
  detail tier covers the same ground for movies.
- **Poster matching requires TMDB-hosted URLs.** Several providers mirror TMDB
  artwork onto their own CDN, keeping the asset basename but not the hostname.
  Measured, this costs almost nothing: of 1,205 id-less movie relations carrying
  a poster, 3 used a mirrored URL. Not worth widening the extractor for.
- **Entries with no metadata cannot be merged automatically.** Their number is a
  property of the provider, not of this plugin. Manual approvals are the recourse.
- **The audit log is one JSON blob rewritten per batch**, capped at 5,000
  entries. Raising the cap much further trades against ingest cost; a bulk import
  beyond that wants an append-only file outside the plugin folder instead.

## Where this goes next: movie metadata enrichment

This section records a **finding**, not a design. The feature is not built and
the central question is open.

`vod_preferences` ranks streams by real video dimensions. For **episodes** it
can: episode relations store ffprobe-style data at
`custom_properties.info.info.video`. For **movies** it cannot — provider listings
return little beyond bitrate — so prefer-4K is effectively blind on movies.

**The finding: the sweep is already storing exactly the data that gap needs.**
`sweep_movies_impl` writes the cleaned `get_vod_info` `info` payload to
`custom_properties.detailed_info`, and on at least one provider that payload is a
complete ffprobe stream description. Verified on a swept relation:

```
keys: avg_frame_rate, bits_per_raw_sample, chroma_location, codec_long_name,
      codec_name, codec_tag, codec_time_base, coded_height, coded_width,
      display_aspect_ratio, disposition, field_order, has_b_frames, height,
      index, is_avc, level, nal_length_size, pix_fmt, profile, r_frame_rate,
      refs, sample_aspect_ratio, start_pts, start_time, tags, time_base, width

width x height = 1920 x 800   codec_name = h264   disposition.attached_pic = 0
```

Plus a real audio track and a top-level `bitrate`. The mkvmerge muxing statistics
in `tags` confirm it describes an actual video stream.

So the extraction path is proven and the payload shape is known. That is the
cheap part, and it is already done.

**The blocker is that the sweep's coverage is precisely inverted relative to what
enrichment needs.** `movie_sweep_candidates` filters
`movie__tmdb_id=None, movie__imdb_id=None` — it fetches **only id-less
relations**, which are by construction the obscure, untagged tail. The
well-tagged mainstream titles anyone would want 4K ranking for are never fetched
at all, because merging has no reason to look at them.

That is not a small adjustment. Serving `vod_preferences` means fetching detail
for **tagged** relations, a population in the tens of thousands rather than the
hundreds the merge sweep touches. Before this plugin existed, 5 of 75,387 movie
relations had ever been advance-refreshed. The nightly merge sweep now costs
roughly one provider call; a library-wide enrichment backfill is orders of
magnitude larger and needs its own cost decision, not a widened filter.

Constraints any future design inherits, all of them already paid for:

- **It must fetch the payload itself.** Delegating to
  `refresh_movie_advanced_data` reintroduces the destructive merge described
  above, at a far larger scale.
- **Scope it to titles that are actually watched or synced**, the way
  `dispatcharr_vod_episode_sweep` scopes its work by watchlist. A blind backfill
  spends its calls on content no one will play.
- **Beware the cover-art trap.** A poster embedded as a stream appears as a
  `video` entry with `disposition.attached_pic=1` and an image `codec_name`; its
  1920×1080 must be ignored or a 4K title mis-ranks as 1080p. `vod_preferences`
  already hit this on episodes. The sample above shows `attached_pic = 0`, so
  the check has to be explicit rather than assumed from one observation.
- **Storage is free.** The data lands on a relation field that already exists and
  that the scan preserves across runs via its `custom_properties` merge. Nothing
  new needs persisting.

The open question is scope and provider-call budget. Everything else is known.

## Relationship to the other plugins

- **`dispatcharr_vod_episode_sweep`** repairs the episode side after a series
  merge: deleting an orphan `Series` cascades away its `Episode` rows, and a
  relation that had been episode-refreshed before the merge is left claiming
  `episodes_fetched=True` while holding none. That sweep refreshes *every*
  relation of a series, so it repairs blocked and unblocked relations alike. It
  is also the reference implementation for watchlist-scoped nightly work.
- **`dispatcharr_vod_preferences`** is the consumer of the enrichment idea above;
  merging also helps it directly, since a stranded 4K copy on a duplicate row is
  invisible to stream selection until the rows are merged.
- **`dispatcharr_vod_concurrency_fix`** is unrelated in mechanism but shares the
  wrap-don't-fork patching discipline used here.
