# Dispatcharr VOD Merge — design

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

On the library this was built against, **~90% of one provider's catalogue was
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
deleted in favour of a one-relation row named after a provider's raw listing
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

Both defects are reported upstream. Once a movie has been merged the core task
becomes safe again, because the id it would set is already set.

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
