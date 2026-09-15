# Dispatcharr VOD Merge

Merges duplicate VOD series and movies created by providers that omit the
TMDB id from their listing, by matching each duplicate to the title you already
have via **TMDB poster artwork**, **plot text**, or — for movies — the
provider's own id in its **detail** payload. Merges are durable across subsequent VOD library refreshes.

**Works only with Xtream-Codes (XC) accounts.** Both wrappers skip any account
whose type is not XC, because the signals are read from the XC listing payload
and no other provider type exposes an equivalent. On an install with no XC
accounts the plugin loads, reports cleanly, and does nothing.

Validated against Dispatcharr 0.30.0 and 0.31.0.

## The problem

Dispatcharr merges VOD entries across providers **only at listing-scan time**,
using a key derived from the raw provider payload with strict priority:

```
tmdb_<id>  >  imdb_<id>  >  name_<name>_<year>
```

A provider that omits the TMDB id therefore keys by its own name and creates a
separate `Series` or `Movie` row — a duplicate of a title you already have. It
cannot recover on its own:

- `lookup_by_name_year` only matches rows whose tmdb **and** imdb are both null,
  so the duplicate can never fall back onto the tmdb-bearing canonical.
- Series have no self-heal path at all. `handle_series_id_conflicts` exists in
  `apps/vod/tasks.py` but has **zero callers**, and `refresh_series_episodes`
  reads the detailed `get_series_info` payload while ignoring any id in it.
- Movies *do* have a self-heal path, and it is worse than none — see
  [Why this doesn't call the core movie refresh](#why-this-doesnt-call-the-core-movie-refresh).

Merging the rows after the fact does not work either: the scan **re-derives**
`relation -> series` / `relation -> movie` from the provider payload on every
run (its `bulk_update` includes those fields), so an after-the-fact merge is
undone by the next VOD refresh, which re-keys the entry by name+year and mints a
fresh orphan.

Durability requires injecting an id **before the merge key is computed**. That is
the whole design.

## The signals

In strength order — series use poster then plot; movies use detail, then poster,
then plot. A [manual approval](#manual-approvals) outranks all of them.

None of these is fuzzy similarity. Poster and plot are artifacts the providers
copied from the same upstream source, so a match is an equality test rather than
a judgement, which is why no human review is needed for them.

### 1. Detail id (movies only)

A movie's `get_vod_info` response carries `info.tmdb_id` outright — the
provider's own id, no matching required. Movie *listings* rarely carry it, but
movie *details* usually do. Fetching it costs one HTTP call per relation, which
is why it runs as a [nightly sweep](#the-nightly-detail-sweep) rather than inline
during a scan.

An unusable or blank detail id **falls through** to the poster and plot tiers
rather than failing the entry.

### 2. TMDB poster artwork

Providers that omit the tmdb id very often still serve TMDB-hosted artwork, and a
TMDB asset path is unique to a title. Only the size segment differs:

```
Provider 1  .../t/p/w600_and_h900_bestv2/acYXu4KaDj1NIkMgObnhe4C4a0T.jpg
Provider 2   .../t/p/w154/acYXu4KaDj1NIkMgObnhe4C4a0T.jpg
Provider 3    .../t/p/w500/acYXu4KaDj1NIkMgObnhe4C4a0T.jpg
```

The basename is a size-invariant join key. Across a full library's worth of
tagged artwork this proved near-perfectly unique in live testing — collisions
were rare enough to count on one hand, and one of them was a URL that ended at
the size segment with no filename at all, which is why an image extension is
required.

### 3. Plot text

The listing's `plot` — exactly what core stores as the description — is usually
TMDB's overview verbatim, so it matches byte for byte across providers. A 40-character
minimum keeps short blurbs out.

This tier reaches duplicates no name scheme can. It is a backup signal by design: it catches what the tiers above miss. That makes
it considerably more productive for series than for movies — see
[Known limits](#known-limits).

**Guard (poster and plot):** a key is only usable if it maps to **exactly one**
tmdb id across the tagged library. That single rule makes placeholder artwork and
boilerplate blurbs — which would otherwise merge hundreds of unrelated titles in
one pass — reject themselves automatically.

**Direction guard.** Poster and plot resolve only against the *tagged* index, so
they can **merge into an existing title but never mint a new one**. Only the
detail tier can create a newly tagged row, and only when
**Tag movies no other providers have** is enabled.

**What this trusts:** not the provider's id (they supply none) but their metadata
being *internally consistent* — that the plot and poster they attached belong to
the title they attached. A provider that bolts the wrong synopsis onto a show
would have that error inherited. The matching signal is recorded on every log
entry so plot-tier merges, the only ones meaningfully exposed to that, can be
reviewed as a class. A shared image asset is much harder to get wrong.

## How it works

`process_series_batch` and `process_movie_batch` are called **inline** from
`refresh_series` / `refresh_movies` (not via `.delay()`), and their `batch`
argument is a list of raw provider dicts whose `tmdb` key becomes the merge key.
This plugin **wraps** both functions and mutates entries in place before
delegating — it never forks the function body, so the compat surface is just the
two signatures plus the dict keys read and written.

Once injected, every subsequent scan re-derives the same `tmdb_<id>` key, finds
the canonical, and repoints the existing relation onto it. **The duplicate cannot
come back.** New arrivals from an untagged provider are merged on first sight, so
no orphan is created for them at all.

Injection is fully guarded: a fault in this plugin degrades to "no merging",
never to a broken VOD ingest.

### Category scope

The scan fetches the provider's **whole listing**, not just your enabled
categories — core discards the disabled ones *after* the batch processor sees
them. The plugin mirrors core's `category_enabled()` check, so it only acts on
entries that will actually be kept. Without that mirror the audit log fills with
phantom merges for content you never sync.

## Install

1. **Upload `dispatcharr_vod_merge.zip`** on Dispatcharr's Plugins page. (If you
   have filesystem access you can instead drop the folder at
   `data/plugins/dispatcharr_vod_merge` — the zip is just the supported route.)
2. Enable it there.
3. **Restart the container** so the wrappers reach every worker. Enabling without
   a restart relies on a file-mtime reload token and is not guaranteed to reach
   an idle worker promptly.

Note that **uploading a zip replaces the contents of the plugin's folder.**
Anything the plugin has written there — see [Audit trail](#audit-trail) — should
be copied elsewhere first if you want to keep it.

Verify it landed where it matters — the VOD scan runs in a **Celery prefork
child**, not the web worker:

```bash
docker logs dispatcharr 2>&1 | grep VOD-MERGE
# Dispatcharr 0.31.0+ also files the container's output here, and rotates it:
docker exec dispatcharr grep VOD-MERGE /data/logs/dispatcharr.log /data/logs/dispatcharr.log.1
```

You want `installed series + movie batch wrappers ... in pid=N` for a Celery
pid, and a `wrapper active in pid=N` line appears the first time a scan reaches
it.

**This check only works from 1.2.1 onwards.** Before that, plugin log records
were discarded in Celery prefork children — the process the scan runs in — so
those two lines could never appear there no matter how well the plugin was
working. If you are reading an older log, their absence tells you nothing. See
the 1.2.1 entry in [CHANGELOG.md](CHANGELOG.md).

Timestamps are worth a caution when correlating: a line emitted inside a request
or a Celery task carries your local zone with an offset, while one from a bare
`manage.py shell` falls back to UTC without one.

The same log line reports `+ destructive-merge protection` when that patch
installed. **Look for that one in a web-worker pid too**, not just a Celery
child: the merge protection matters in the uWSGI request workers, because both
callers of the affected function are inline calls rather than queued tasks.

**Also grep for failures explicitly**, e.g. `movie injection failed`. The
wrapper's outermost handler is deliberately broad so a fault can never break
ingest, which means a silently disabled feature and a working one look identical
from the outside. The log is the only place the difference shows.

## Settings

Series and movies are controlled symmetrically: each has its own on/off switch
and its own account allowlist, so either kind can be rolled out without touching
the other. Listed in the order the UI shows them.

| Setting | Default | Notes |
| --- | --- | --- |
| **Dry run (report only)** | on | Reports what would merge, injects nothing. Meant for first setup — **not a pause switch**, see below. |
| **Merge series** | on | Merge duplicate series. |
| **Merge movies** | off | Merge duplicate movies. Off by default so an upgrade never starts merging movies on its own. |
| **Prevent destructive movie merges** | on | Fixes an upstream Dispatcharr bug; independent of this plugin's own merging. See [Protecting against a destructive core merge](#protecting-against-a-destructive-core-merge). Not affected by dry run. |
| **Preserve essential movie detail** | on | Puts back the TMDB id, video and audio details when a Dispatcharr detail fetch drops them. Also an upstream fix rather than a merging change. See [Preserving detail a refresh drops](#preserving-detail-a-refresh-drops). Not affected by dry run. |
| **Limit series merging to accounts** | *(empty = all)* | Account names to merge series **from**. The canonical can live on any account. |
| **Limit movie merging to accounts** | *(empty = all)* | The movie equivalent, **independent of the series list**. Also bounds which accounts the detail sweep looks up, so scope and sweep stay in step. |
| **Tag movies no other providers have** | off | Lets the detail tier *create* a newly tagged movie rather than only merging into an existing one. Changes those movies' Dispatcharr ids now, and they revert if it is turned off. |
| **Fetch movie details nightly** | off | Creates the beat schedule. |
| **Nightly sweep hour (0-23)** | 4 | System timezone. |
| **Approve manually** | *(empty)* | `<account>:<id>=<tmdb_id>`, for duplicates no signal reaches. Series and movies — see [Manual approvals](#manual-approvals). |
| **Never merge** | *(empty)* | `tmdb:<id>` or `<account>:<id>`. Overrides everything, including approvals. |
| **Variant edition pattern (regex)** | `[B&W]`, `[Black/White]`, `[Colorized]` | Matching entries are never merged (see below). |
| **Movie details per manual run** | 50 | Bounds the **manual** sweep only — it runs inside a web request and would otherwise time out. The nightly sweep is unbounded by design. |
| **Delay between detail calls (ms)** | 200 | Provider rate-limit throttle. Raise before adding several large categories at once. |
| **Schedule queue (advanced)** | `dvr` | See [Why the sweep runs on the `dvr` queue](#why-the-sweep-runs-on-the-dvr-queue). Change only if you know why. |
| **Index cache (seconds)** | 600 | One scan's batches all fall inside this. |

The two switches default differently on purpose, and the reason is upgrades
rather than risk. Series merging predates movie support and has always been on,
so defaulting it off would silently stop it for an existing install; movies
stayed opt-in because turning them on by default would start merging for someone
who had deliberately left them alone. **Dry run** is what makes a fresh install
safe regardless — both switches sit behind it.

An empty account list means **every account**, not none. To merge nothing for a
kind, turn its switch off.

### Turning merging off is not a pause

Merges persist only because the plugin **re-derives them on every scan** — the
scan re-keys `relation → series/movie` from the provider listing each run, and
the wrapper re-injects the id before that happens. Nothing is written once and
left alone.

So switching a kind off — **or turning Dry run back on** — and then running a VOD
refresh lets the affected titles split apart again: the listing has no id, the
entry re-keys by name+year, and the relation lands on a freshly minted duplicate.
Turning it back on and running another scan re-derives the merges identically, so
this is normally self-correcting.

The exception is worth knowing before you toggle anything. A canonical row that
exists *only* because of this plugin — one created by **Tag movies no other
providers have**, whose relations all come from a scoped account — loses every
relation when merging is off, and Dispatcharr's scan prunes relation-less rows.
The tagged row is then gone, and the poster and plot tiers have nothing to
resolve against, so that title stays split until the detail tier or an approval
re-tags it. Titles where some other provider supplies the tmdb natively are
unaffected.

Precedence for **series** is **manual approval > poster > plot**; for **movies**,
**manual approval > detail > poster > plot**. The denylist overrides all of them
in both cases. An approval bypasses the variant guard — approving a variant by
name *is* the override.

Quality tags (`4K`, `1080p`, `UHD`) are **not** variants — a 4K copy is the same
edition and should merge.

## Manual approvals

Some entries are unreachable by any signal. A provider can ship a title as a bare
name — empty `stream_icon`, no plot, and no id in its detail payload — and that
includes mainstream films, not just obscure ones. Nothing can be derived from an
entry carrying no metadata. An approval is the way to merge it anyway: you supply
the TMDB id yourself.

### Format

```
<account>:<id>=<tmdb_id>
```

- **`<account>`** — the M3U account name exactly as it appears in Dispatcharr,
  case-sensitive. An approval only applies to the account it names.
- **`<id>`** — the **external series id** for a series, the **stream id** for a
  movie. Both are the provider's id, not Dispatcharr's.
- **`<tmdb_id>`** — the numeric TMDB id of the title to merge into.

Multiple approvals go in one **comma-separated** list:

```
Provider1:12345=67890, Provider2:24680=13579, Provider3:11223=44556
```

Whitespace around each entry is ignored. The parser also accepts newlines, but
the settings field is single-line, so commas are what you can actually type.

One consequence: an account name containing a comma cannot be expressed here.
Rename the account if you hit that.

### Finding the TMDB id

From the title's TMDB page URL — `themoviedb.org/movie/<id>-<slug>`, where the
leading number is the id. For a series it's the `/tv/<id>-<slug>` number. Use the id of the
**canonical** entry, the one you want the duplicate folded into; if that entry is
already tagged in Dispatcharr, its existing `tmdb_id` is the value you want.

### Finding the provider id

The repo ships a helper for exactly this — `tools/explain_merge.sh`. Set `TITLE`
to a fragment of the name, run it on the Dispatcharr host, and it prints the
tagged rows, the id-less ones, **why** each id-less row is unreachable, and a
ready-to-paste approval line. It is not part of the plugin zip; it runs against
the container from outside.

Use a distinctive *fragment* rather than the full title — it sidesteps shell
quoting around apostrophes and matches providers' varied punctuation.

If you would rather not use the script, the same lookup by hand:

```bash
docker exec dispatcharr python manage.py shell -c '
from apps.vod.models import M3UMovieRelation, M3USeriesRelation
print("MOVIES")
for r in M3UMovieRelation.objects.filter(movie__name__icontains="[TITLE]").select_related("movie","m3u_account"):
    print("  ", r.m3u_account.name + ":" + str(r.stream_id), "| tmdb", r.movie.tmdb_id, "|", r.movie.name)
print("SERIES")
for r in M3USeriesRelation.objects.filter(series__name__icontains="[TITLE]").select_related("series","m3u_account"):
    print("  ", r.m3u_account.name + ":" + str(r.external_series_id), "| tmdb", r.series.tmdb_id, "|", r.series.name)
'
```

Replace `[TITLE]` with part of the title. The left-hand column is already in
`<account>:<id>` form — append `=<tmdb_id>` and paste it into the setting. The row
with a `tmdb` value is the canonical (that number is your `<tmdb_id>`); the row
showing `None` is the duplicate you need to approve.


### Verifying

Check the approval is recognised **before** running a refresh. Everything below
is read-only and makes no provider calls, so none of it needs dry run turned on.

**Movies — Movie merge status.** Its output leads with a short summary rather than a
list, so the UI shows it in full. `projected_by_tier` should now count a
`manual`; if you added one approval, that count rising by one is the
confirmation. Note **Series merge status covers series only** and will never
show a movie approval.

**Series — read the file, not the popup.** **Series merge status** can report
hundreds of entries and the UI truncates long action output, so run the action
(which writes the file) and then read it:

```bash
docker exec dispatcharr python -c "
import json
d = json.load(open('/data/plugins/dispatcharr_vod_merge/series_status.json'))
print('tiers:', d.get('tiers'), '| approvals loaded:', d.get('approvals'))
for m in d.get('matches', []):
    if m.get('tier') == 'manual':
        print(m)
"
```

`approvals loaded` is worth checking on its own: it is the count the plugin
actually parsed, so a value of `0` means the setting did not parse rather than
that the entry did not match.

**After the refresh**, confirm the merge landed. The audit log lives in the
database, so this works without clicking anything:

```bash
docker exec dispatcharr python -c "
import json
from core.models import CoreSettings
v = CoreSettings.objects.get(key='dispatcharr_vod_merge_log').value
d = v if isinstance(v, dict) else json.loads(v)
for e in d.get('entries', []):
    if e.get('tier') == 'manual':
        print(e.get('kind'), e.get('action'), e.get('account'), e.get('stream_id'),
              '->', e.get('tmdb_id'), '| canonical', e.get('canonical_id'),
              '| creates_new_row', e.get('creates_new_row'), '|', e.get('name'))
"
```

`creates_new_row: True` is the one result to look at twice — it means no existing
row held that tmdb id, so the approval will mint a newly tagged entry instead of
folding into an existing one. That is usually a sign of a wrong id.

Once the merge lands the entry is no longer id-less, so it drops out of **Movie
detail status** entirely. After the fact the audit log is the record, not the
status action.

### Cautions

Use them sparingly. A name that looks unique in your library may not be unique in
the world. In live testing only a couple of series duplicates ever needed a human
decision, and they split both ways: one was a distinctive title with an agreeing
year and was safe to approve; the other was a short, common word that names
several unrelated shows, and approving it would have merged the wrong one. That
asymmetry is the whole reason there is no automatic name-matching tier.

An approval is trusted completely: it outranks every derived signal, bypasses the
variant guard, and ignores **Tag movies no other providers have**. A wrong tmdb id
merges the entry into the wrong title, and the scan will keep re-deriving that
merge on every run until you remove the line. The only thing it does not override
is **Never merge**.

Approvals are keyed on the provider's id, and providers do sometimes renumber. An
approval whose id no longer exists simply stops applying — it does not error, so
it can go stale quietly.

## Actions

| Action | Does |
| --- | --- |
| **Show status** | Config, wrapper state, index sizes. |
| **Series merge status** | Read-only, no provider calls. What would be merged for series, by which signal. Full list written to `series_status.json`. |
| **Movie merge status** | Read-only, no provider calls. What would be merged for movies and how much detail backlog is left. Full list written to `movie_status.json`. |
| **Fetch movie details** | Runs the sweep manually, bounded by *Movie details per manual run*. |
| **Show injection log** | The audit trail. |
| **Clear injection log** | Resets it. |

## Rollout

1. **Preview.** Read-only, no provider calls. Uses the same decision function the
   wrapper uses, so it cannot disagree with what a real run would do.
2. **Set the account allowlists** to the untagged provider.
3. **Turn dry run off**, run a VOD refresh, confirm the duplicates collapse.
4. **Run a second refresh.** This is the point of the whole design — confirm the
   merges persist.
5. For movies, **enable the nightly sweep** and check *Movie merge status* the
   next day.

## Movies: what to expect

Movie listings are much thinner than series listings, and *how* thin is
**provider-dependent** — this is the single biggest variable in what the plugin
can do for you.

Live testing found both extremes on the same library. For some providers, id-less
movie entries carried almost no TMDB artwork and no usable plot at all — the free
tiers are effectively dead there and the detail sweep does nearly all the work.
Another provider attached TMDB artwork to virtually everything while supplying no
ids whatsoever, and for that one the poster tier alone handled the large majority
of a category on its own, with the sweep mopping up the remainder.

Do not plan around either shape. **Measure yours** with **Series merge status**
and **Movie merge status** before enabling anything — between them they report what
each tier would do against your actual data, without making a single provider
call.

### The nightly detail sweep

**Fetch movie details** looks up `get_vod_info` for id-less movie relations and
stores the result on the relation. It **collects data only and merges nothing** —
no `Movie` row is created, deleted, renamed or repointed by the sweep itself. The
next scan does the merging, from stored data, with no HTTP at all.

Three properties worth knowing:

- **It skips anything a free tier already reaches.** No point spending an HTTP
  call on a relation the poster index will merge for nothing.
- **It goes newest-first**, and in live testing the difference was stark: the
  newest id-less relations returned a tmdb almost every time, while the oldest
  returned one almost never. Old bulk imports are untagged long-tail content;
  recent arrivals are both well-described and the ones actually duplicating your
  library. Oldest-first would spend most of its calls on the least useful end.
- **The candidate set shrinks permanently.** A relation whose detail is stored is
  never fetched again, whether or not it yielded an id. Cost is one-time per
  relation, not nightly.

Because the nightly sweep is deliberately **unbounded** — a bounded one would
take a week to drain a large new category at 50 a night — adding several large
categories at once produces a correspondingly large burst of sequential provider
calls. Raise **Delay between detail calls** *before* the add, not after a
rate-limit.

### Why the sweep runs on the `dvr` queue

A plugin's `@shared_task` sent to the default prefork queue is **rejected**: that
worker's consumer runs in the main process, which never imports plugins, so it
sees an unregistered task name. The threads-pool `dvr` worker does register them.
That is the only reason for the queue override, and why changing it is marked
advanced.

### Why this doesn't call the core movie refresh

The sweep deliberately does **not** call `refresh_movie_advanced_data`. That core
task routes through `handle_movie_id_conflicts`, whose "preserve the user's
selection" policy **deletes the canonical `Movie` row and keeps the id-less
orphan** — inheriting the orphan's raw provider name and a new UUID. It fires
precisely when the detail's tmdb belongs to another row, which is every candidate
here, so running it across thousands of relations would rewrite the library.

It can also abort part-way. To dodge a partial unique index it first nulls the
canonical's `tmdb_id` in a standalone write, with no surrounding transaction; if
any id-less row already holds that `(name, year)`, Postgres rejects it, the error
is swallowed into a return string, and the relation is left permanently
re-fetching.

Fetching the payload ourselves and letting the *scan* do the merging avoids both
problems entirely. Once a movie has been merged the core task becomes safe again,
because the id it would set is already set.

## Protecting against a destructive core merge

This is a **bug fix, not a feature**, and it applies whether or not you use any
of the merging above. It is on by default.

When an on-demand movie refresh finds a TMDB id in the provider's detail that
already belongs to a different `Movie` row, Dispatcharr's
`handle_movie_id_conflicts` keeps the movie being refreshed and **deletes** the
pre-existing one, describing this as preserving "the user's selection".

On that path it is backwards. The movie being refreshed is the freshly-minted
duplicate — the listing carried no id, so it keyed by `name+year`, and
`lookup_by_name_year` only matches rows with *both* ids null, so it cannot see
the id-bearing canonical. The row deleted is therefore the established one,
holding most of the relations, the better name, and the UUID your other tools
have already indexed. The survivor inherits the provider's raw listing name and a
new id.

It fires on any UI movie open, or any XC `get_vod_info`, for an affected title —
so a client that fetches movie detail in bulk can trigger it at scale,
unattended. It can also abort part-way, leaving a relation that re-fetches and
re-fails indefinitely.

**With this setting on**, the duplicate's relation is repointed onto the
canonical and nothing is deleted. That is the direction Dispatcharr's own caller
already implements — it has a branch for exactly this case which is unreachable
only because the function never signals it.

The trade, stated plainly: the row you were viewing becomes relation-less and is
pruned by a later scan, so that one page may look slightly stale until you
reload. A transiently stale page is clearly preferable to permanently losing the
canonical, but it is a real difference rather than a free win.

Prevented merges appear in the injection log with their own action, so you can
review them as a group. **Show status** reports whether the protection is active
in that worker, which matters because a working protection is invisible — nothing
bad happening looks exactly like it never firing.

Turn it off only if your Dispatcharr version has fixed this upstream.

## Preserving detail a refresh drops

A second upstream fix, unrelated to merging, controlled by **Preserve essential
movie detail** (on by default).

Every time Dispatcharr fetches a movie's detail it **replaces** what it stored
last time rather than merging into it. Providers are not consistent between
calls: one that returned a full stream description on Monday can return a plot
summary and nothing else on Tuesday, and the second response silently erases the
first. Nothing reports it, and the loss only shows up later as a title that
stopped ranking correctly or stopped merging.

Three keys are worth more than the payload they arrive in, so they are put back
if a fetch drops them:

- **`tmdb_id`** — the provider's own id, this plugin's strongest movie signal. If
  it disappears, a duplicate this plugin could have merged becomes unmergeable
  again, silently.
- **`video`** — measured resolution and codec, as opposed to whatever the stream
  name claims.
- **`audio`** — codec and channel count. This one has no fallback anywhere
  downstream: absent means "no opinion", so losing it does not weaken a quality
  comparison, it removes one side of it entirely.

**A value the provider actually sent always wins.** This only refills a hole; it
never puts stale data back over a real answer. Restored keys go to the injection
log with their own action, and **Show status** reports whether the guard is live
in that worker.

### What the sweep records, and what it leaves alone

Dispatcharr marks a relation whose detail it has fetched with two fields,
`detailed_fetched` and `last_advanced_refresh`. Between them they are the only
record anywhere that a **client** asked for that movie. Versions before 1.2.0
set both from the detail sweep, which destroyed the distinction: every relation
the sweep touched looked like one somebody had requested.

Since 1.2.0 the sweep records its own timestamp under its own key in the
relation's `custom_properties` and leaves Dispatcharr's fields alone. The rule
behind it is that **a sweep must not write the field it reads**.

If you ran the sweep on an earlier version, its marks are still there and are
**harmless**. Dispatcharr's 24-hour skip only considers them fresh for a day,
so they suppress nothing after that, and anything reading the record sensibly
reads it as "asked for recently" — a window those old timestamps have already
fallen out of. There is no cleanup step to run.

## Known limits

**The plot tier does much less for movies than for series.** It is a backup
signal by design, and for movies the tiers above it do most of the work. It also
has less to work with: series listings routinely carry a `plot`, while in live
testing movie listings almost never did — the movie plot index came out orders of
magnitude smaller than the poster index. It costs nothing to keep, catches real
duplicates when a provider does supply descriptions, and the detail tier covers
the same ground for movies. Just don't size your expectations on the series
behaviour.

**Poster matching requires TMDB-hosted URLs.** Some providers mirror TMDB artwork
onto their own CDN, preserving the asset basename but not the hostname, and those
are not matched. In live testing this was rare enough not to be worth relaxing
the check for — but it is provider-specific, so a library whose providers all
mirror would see less from the poster tier.

**Entries with no metadata at all cannot be merged automatically.** Some
providers ship even mainstream titles as a bare name with an empty `stream_icon`,
no plot, and no id in the detail payload. No signal can reach those; their number
is a property of the provider, not of this plugin.
[Manual approvals](#manual-approvals) are the escape hatch.

## Variant editions are deliberately not merged

A separate row is the only way Dispatcharr can represent "same title, different
edition" — relations carry no edition label, so merging a black-and-white cut
into the canonical would make its streams silently selectable for the normal
version. Leaving it unmerged keeps it as its own library entry, which is the
faithful representation until upstream has a notion of editions.

Declined variants are logged on **every** scan by design. That is a standing
decision being restated, not a state change.

## After the first merge

Observed on a real merge:

**Orphan rows clean themselves up.** The scan prunes a row left with no
relations, so duplicates disappear in the same refresh that merges them.

**Relation rows survive and keep their ids.** They are repointed via
`bulk_update`, which does not fire `auto_now`, so even `updated_at` is unchanged.
Merging does not churn relation identity.

**A merged series' episodes need one refresh.** Deleting the orphan `Series`
cascades away its `Episode` rows and their `M3UEpisodeRelation` rows, so the
merged relation starts with no episode streams. Most relations come back with
`episodes_fetched=False` and refresh on demand, but any relation that *had* been
episode-refreshed before the merge is left claiming `episodes_fetched=True` while
holding none — and the gated callers (UI browse, XC `get_series_info`) skip it on
that basis.

Note also that after the merge nothing advertises the merged provider's relation
over XC — `get_series` publishes only the highest-priority relation per series —
so the gated paths will not target it again either way. Two things do:

- `batch_refresh_series_episodes(account_id, series_ids=[...])`, a core task,
  which bypasses the 24h gate when given explicit ids.
- A sweep that refreshes *every* relation of a series (e.g.
  `dispatcharr_vod_episode_sweep`), which uses that same call and therefore
  repairs blocked and unblocked relations alike.

If the merged provider is low priority it never wins stream selection anyway, so
this only affects failover depth — worth fixing, rarely urgent.

## Audit trail

Every injection — and every declined variant or denylisted entry — is recorded in
a durable `CoreSettings` row (`dispatcharr_vod_merge_log`, newest 5,000),
viewable via **Show injection log**. A merged title should surface for review
rather than changing state quietly, particularly if you drive a `.strm` generator
that keys on Dispatcharr ids.

Each entry records the **signal that matched** (`detail`, `poster`, `plot` or
`manual`), so the tiers can be reviewed as separate classes.

The plugin writes three JSON files into its own config folder, because the UI
truncates long action output:

| File | Written by | Contents |
| --- | --- | --- |
| `series_status.json` | **Series merge status** | Every series that would merge, and by which signal |
| `movie_status.json` | **Movie merge status** | Every movie that would merge, plus the sweep backlog counts |
| `injection_log.json` | **Show injection log** | The full audit trail, not just the newest entries shown in the UI |

**The files are complete; the UI shows counts only.** Dispatcharr renders action
output in a popup that does not scroll and does not allow text selection, so a
per-entry listing there is unreadable no matter how short it is. Both status
actions therefore print a fixed handful of summary lines — index sizes, counts,
the projection by tier — and nothing else. The per-entry detail is in the file,
which is never truncated; on a first run against a large untagged provider that
can be thousands of entries.

One place a cap does apply: the JSON payload the action hands back to the web UI
is limited to 200 matches, since sending thousands would bloat the response for
no benefit when the popup shows 10. When that happens the payload carries
`matches_truncated: true` and `matches_total`. **The file on disk is unaffected**
— if you are reading `series_status.json` or `movie_status.json`, you are seeing
everything.

All three are regenerated on each run, so copy `series_status.json` aside before a
first merge if you want it as a record — once a duplicate is merged it no longer
appears in the preview.

**Copy it somewhere outside the plugin folder.** Uploading a plugin zip replaces
that folder's contents, so a snapshot kept beside the plugin does not survive the
next version bump.

## Testing

```bash
py -3 test_logic.py
```

164 tests, no Django, no database, no Docker — the decision logic is pure.
Covers both real-world failure modes that cost debugging time: the size-segment
URL with no filename, and a placeholder asset shared across many titles.

## License

MIT
