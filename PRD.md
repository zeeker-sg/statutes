# Zeeker Statutes Resource — Handoff Plan

Project: a new Zeeker database for Singapore legislation from Singapore Statutes
Online (SSO), https://sso.agc.gov.sg. Sibling project to `zeeker-sg/judgements`
(eLitigation), which is the architectural reference — reuse its crawl
scaffolding, two-phase pipeline, disk-cache pattern, and checkpoint design
wherever they fit.

This plan was produced from live site analysis on 2026-06-11. Verify anything
marked **HYPOTHESIS** in the Phase 0 spike before building on it.

---

## Decisions already made (do not relitigate)

1. **Scope (Phase 1):** Current Acts only — 525 documents. Repealed/spent and
   uncommenced Acts, and all Subsidiary Legislation, are explicitly out of
   scope for the first release (the browse templates are identical, so they
   are cheap later phases).
2. **Versioning:** Extract full text for the *current* expression of each Act
   only, but capture **version timeline metadata** (every historical version's
   date, amending Act, and URLs) from day one. Historical *text* extraction is
   a deferred Phase 4.
3. **Granularity:** Catalog + per-provision fragments (`--fragments`), not
   whole-act blobs. Provisions (sections) are the unit lawyers search for.
4. **Conventions:** `uv` for everything (`uv run ...`), `black` formatting
   (line-length 100, py312), `httpx`, `tenacity`, `beautifulsoup4` + `lxml`,
   `click.echo` for progress. Follow the zeeker-source-creator skill in
   `.claude/skills/` (copy it into this repo like the judgements repo does).

## Terms of Use compliance (RESOLVED — these are hard requirements)

The user has reviewed SSO Terms of Use clause 13
(https://sso.agc.gov.sg/Terms-of-Use, "Use/Reproduction of Singapore
legislation") and decided to operate under it. AGC grants permission to
use/reproduce Singapore legislation subject to conditions. Every condition
below is a build requirement, not a nice-to-have:

1. **Extraction window — 3 a.m. to 7 a.m. Singapore Time only (cl 13(d)(i)).**
   Automated extraction from SSO may only run 03:00–07:00 SGT (19:00–23:00
   UTC). Enforce this in TWO places:
   - GitHub Actions cron scheduled inside the window, with margin for
     Actions' notoriously late cron starts — e.g. `30 19 * * *` UTC
     (03:30 SGT), leaving ~3.5h of window.
   - A **runtime guard in the resource module itself**: before every batch
     and between batches, check current SGT time; if outside 03:00–07:00,
     save checkpoint and exit cleanly (`STATUTES_IGNORE_WINDOW=1` escape
     hatch for fixture work against cached/local files only — never against
     the live site). Hard-stop margin: stop issuing new requests after
     06:45 SGT so in-flight work finishes inside the window. This guard is
     what makes manual `workflow_dispatch` runs and local runs safe too.
2. **Attribution (cl 13(a)).** The published Datasette/`zeeker.toml` metadata
   and CLAUDE.md must state both required notices, substantively: (i) the
   legislation is subject to the copyright of the Singapore Government and is
   reproduced with the permission of the Attorney-General's Chambers; (ii)
   users may check SSO (https://sso.agc.gov.sg) for the latest version of the
   legislation. Set `license` to a short form of (i) and `license_url` to the
   Terms of Use page; put the full two-part notice in the project
   `description` or Datasette metadata so it renders on the site.
3. **No suggestion of AGC affiliation/endorsement (cl 13(c)(i)).** Metadata
   and any UI copy must not imply AGC/SSO endorses or is affiliated with the
   project. Phrase attribution as "reproduced with the permission of AGC
   under the SSO Terms of Use", nothing stronger.
4. **Non-abusive, non-deceptive, non-disruptive (cl 13(c)(ii), 13(d)(ii)–(iii)).**
   Keep the honest `ZeekerBot/1.0 (+https://data.zeeker.sg)` UA (no browser
   impersonation), keep the polite pacing and circuit breaker, never attempt
   to bypass any anti-bot/security measure SSO presents — if a route is
   blocked, that route is out (this constrains Phase 0: do not "circumvent"
   anything; only use endpoints the site itself calls openly).
5. **Accuracy responsibility (cl 13(b)).** The producers (us) own accuracy.
   Mitigations: every row links back to its SSO `source_url`; `current_as_at`
   and `extracted_at` are stored and surfaced; the published metadata carries
   the "check SSO for the latest version" notice; extraction tests against
   fixtures guard the parser.

## Open items

- None blocking. Steady-state cadence recommendation is below (the ToU window
  constrains *when*, not *how often*).

---

## Site intelligence

### Listing (discovery)

- URL: `https://sso.agc.gov.sg/Browse/Act/Current/All?PageSize=500&SortBy=Title&SortOrder=ASC`
- Server-rendered HTML, no JS needed for the listing. 525 current Acts;
  PageSize supports 10/20/50/100/250/500, so the full catalog is **2 requests**.
- Each row contains:
  - Act title and a link to the detail page: `/Act/{SHORTHAND}` where the
    shorthand is like `IA1965`, `AA2004`, `ACRAA2022`. This is the natural
    **work-level identifier** (still mint the opaque hash id from source_url
    per Zeeker convention; store the shorthand as a content field).
  - A PDF link of the form `/Act/{SHORTHAND}?ViewType=Pdf&_=20260407144124`.
    **HYPOTHESIS:** the `_` cache-buster is a last-modified timestamp
    (YYYYMMDDHHMMSS). If true, diffing it against the stored value detects
    amended Acts with just the 2 listing requests. Verify in Phase 0 by
    cross-checking a few Acts' `_` values against their timeline dates.
  - A per-Act Amendments RSS feed: `/Act/{SHORTHAND}?ViewType=Rss` — the
    authoritative change signal and the fallback if the timestamp hypothesis
    fails.
  - Presence/absence of a "Subsidiary Legislation" action → boolean flag.
- Quick-link facets exist via query params (`SubType=Constitutional|Private|
  Imperial|EnglishCommercial`). Capture subtype if derivable; otherwise leave
  nullable and backfill from the four SubType-filtered listings (4 extra
  requests).

### Detail page (per Act)

`https://sso.agc.gov.sg/Act/{SHORTHAND}` is server-rendered and contains:

- Status banner: "Current version as at {DD Mon YYYY}".
- Full table of contents: Long Title → Parts (e.g. "Part 2 GENERAL PROVISIONS
  REGARDING WRITTEN LAW") → numbered provisions with headings (including
  letter-suffixed insertions like 2A, 9A, 41E) → Schedules → Legislative
  History → Abbreviations.
- **Version timeline**: a list of every point-in-time version, each with a
  validity date, the amending Act citation (e.g. "Amended by Act 19 of 2025")
  or revised-edition marker ("2020 RevEd"), a historical URL of the form
  `/Act/{SHORTHAND}/Historical/{VALIDDATE}?DocDate=...&ValidDate=...`, and a
  version-specific PDF link. This feeds `act_versions` at zero extra request
  cost — it's on the page we already fetch.

### The lazy-load landmine (Phase 0's main job)

SSO **lazy-loads body provisions via AJAX** for long Acts: the initial HTML
reliably carries front matter, TOC, and timeline, but section text streams in
through XHR calls (a `Details/GetLazyLoadContent`-style endpoint keyed by
provision/series IDs embedded in the page). Candidate full-text routes, in
order of preference:

1. **Replicate the lazy-load XHR calls** — returns the same semantic HTML the
   site renders; provision-scoped, which maps perfectly onto fragments.
2. **Print view** (`/Act/{SHORTHAND}?ViewType=Print`, possibly with a
   whole-document parameter) — may return the entire Act in one response.
3. **PDF via Docling Serve** — last resort; destroys provision structure and
   adds an infra dependency (`DOCLING_SERVE_URL`).

Phase 0 must pick one route, document the exact request pattern (params,
headers, any anti-bot behaviour), and check `robots.txt`.

### Publication streams — the "AS PUBLISHED" supplements (key to rolling updates)

SSO publishes three append-only, date-ordered streams under Browse → AS
PUBLISHED, all on the same listing template:

- **SL Supplement** `/Browse/SL-Supp/Published/{year}` — every subsidiary
  legislation instrument as gazetted. 2026 had 362 items by early June
  (~70/month, ~3 per working day — this is the "new legislation every day"
  channel). Rows carry: title, sequential gazette number ("S 355/2026"),
  publication date in the URL
  (`/SL-Supp/S355-2026/Published/20260529?DocDate=20260529`), and an
  as-published PDF. Sortable by Number.
- **Acts Supplement** `/Browse/Acts-Supp/Published/{year}` — new Acts as
  enacted ("Act 19 of 2025"), same shape.
- **Bills Supplement** `/Browse/Bills-Supp/Published/{year}` — same shape;
  out of scope for now but identical machinery.

Two properties make these the backbone of rolling updates: items are
**immutable once published** (as-published documents never change), and the
gazette number is **sequential within a year** — a natural monotonic cursor.
Incremental ingestion is therefore RSS-like: fetch page 1 of the current year
sorted by Number DESC, insert everything above the stored high-water mark,
stop. One request per stream per night, regardless of corpus size. Bonus:
most amendments to consolidated works are announced here *before* the
consolidated listing changes, so the streams double as a change-notification
channel for the catalogs. (`/What's-New/New-Legislation` exists as a further
cross-check signal.)

### Subsidiary Legislation browse (recon for the SL catalog)

`/Browse/SL/Current` is the exact same template as the Acts browse (same
PageSize options up to 500, same Actions menu, same SubType quick links plus
`Interpretation` and `Awards`). The difference is scale: letter "A" alone has
202 current instruments across 11 pages, so the current SL corpus is
plausibly 3,000–4,500 works. Catalog discovery is still only ~7–9 listing
requests at PageSize=500, but text backfill is a multi-month nightly queue.
SL detail pages share the Acts anatomy (status banner, version timeline,
lazy-loaded body); the SL work-id format in `/SL/{id}` URLs needs Phase-0-style
verification when SL goes live (it appears to combine the parent Act
shorthand with a gazette or revised-edition number).

---

## Rolling update architecture (how the DB stays current daily)

There are two kinds of change on SSO, and they get two different mechanisms.
Conceptually: the supplement streams are an **event log**; the Act/SL
catalogs are **materialized state** rebuilt lazily from those events.

**Stream ingestion (new legislation — daily, cheap, exact).** Each nightly
run reads page 1 of SL-Supp and Acts-Supp for the current year, sorted by
Number DESC (2 requests; +1 per stream around New Year to catch the year
rollover). Anything above the per-(series, year) high-water mark stored in
the DB is inserted into the `gazette_stream` table. Then each new event is
interpreted — with one hard rule: **stream events never create catalog
rows.** Supplement items carry gazette numbers ("19 of 2025", "S 355/2026"),
not the shorthand work-ids our catalog keys derive from, so a stream-created
stub would have to invent a key. Instead:

- Acts-Supp item → recorded in the stream; the catalog reconciliation below
  discovers the new Act (with its shorthand) when it appears in the Current
  or Uncommenced listing, and principal resolution then links the waiting
  stream row.
- SL-Supp item that is a wholly new instrument → same: recorded now, linked
  when the SL catalog reconciliation (Phase 5) sees it.
- SL-Supp or Acts-Supp item whose title marks it as amending/revoking
  ("(Amendment)", "(Revocation)", "Amendment Act") → parse the principal
  work's name from the title, look it up in the catalog, **invalidate it**
  (set `content_text = NULL`, clear its extraction cache) so the nightly
  extraction queue re-pulls its consolidated text and refreshed timeline.
  Title-based principal matching won't always resolve; unresolved events are
  flagged in the stream row (`principal_resolved = false`) and the
  listing-diff safety net below catches the consolidation anyway.

**Catalog reconciliation (consolidations — the safety net).** After stream
ingestion, re-fetch the catalog listings (2 requests for Acts; ~7–9 for SL
when live) and diff: new ids, vanished ids (repeal candidates — flag, never
delete), and changed `listing_stamp` values → invalidate those works. This
catches everything the stream interpretation missed, including commencements
where an already-published amendment only now takes effect (SSO consolidates
on the *in-force* date, which can be long after gazettal). If the
`listing_stamp` hypothesis dies in Phase 0, the per-work RSS feeds replace
this diff.

**Extraction queue (bounded nightly work).** The remainder of the 03:00–07:00
window drains the queue of works with `content_text IS NULL` — newly created
stubs and invalidated works first (order by invalidation recency), then any
remaining backfill. Steady state for Acts is light (a few invalidations a
week); the queue mechanism is identical during backfill and steady state, so
there is no mode switch — the backfill simply *is* a long queue.

Why this stays correct: stream ingestion can't miss items (monotonic cursor
on an append-only series), reconciliation can't miss consolidation changes
(it diffs ground truth), and extraction is idempotent (re-extracting a work
replaces its row and fragments). A missed night just means a longer queue the
next night.

---

## Schema

### Identity and keys (read before implementing any table)

Three kinds of identifier with strictly separated jobs:

1. **Primary keys** = opaque deterministic hashes (`id`, SHA-256 first 12
   hex). All FKs (`act_versions.act_id`, `gazette_stream.principal_id`,
   `*_fragments.act_id`) point at these and only these.
2. **Citation/natural identifiers** (`shorthand`, `provision_id`,
   `sl_number`, gazette numbers) are content fields — for humans, search,
   display. NEVER keys, never FK targets. They may be renumbered or
   reformatted by AGC without touching our key space.
3. **Dedup is not a check — it's a property of key derivation.** Because
   the PK is a deterministic hash of a stable source attribute, re-running
   discovery produces the same `id` and `sqlite_utils` upsert-on-pk
   collapses duplicates structurally. Nothing scans for duplicates.

Pinned hash inputs (do not improvise; changing these is a migration):

| table | hash input | rationale |
|---|---|---|
| acts | `act:{shorthand}` | survives any URL path/query reshuffling by AGC; shorthand is the most stable attribute on the site |
| sl | `sl:{sl_work_id}` (format per SL spike) | same logic |
| gazette_stream | `{series}:{year}:{number_seq}` | the gazette number IS the published identity of the item |
| act_versions / sl_versions | `{parent_id}:{valid_date}` | one expression per work per validity date |
| *_fragments | `{parent_id}:{provision_id}` | unique within a work; cross-run stability NOT required — re-extraction replaces a work's fragments wholesale (delete-then-insert), so renumbering regenerating fragment ids is harmless by construction |

**Tripwires:** put unique indexes on the natural identifiers —
`acts.shorthand`, `gazette_stream (series, year, number_seq)`,
`act_versions (act_id, valid_date)`, `*_fragments (parent_id,
provision_id)` — so that if key derivation ever drifts (e.g. an AGC URL
migration changing what a parser extracts), the build fails loudly instead
of silently inserting near-duplicates. A failed unique constraint here is
a signal to investigate the source, never to relax the index.

Three tables. Conceptual grounding: Akoma Ntoso's FRBR split — the **Work**
(the Act as an abstract thing, identified by the SSO shorthand) vs the
**Expression** (its text as at a date) — and AKN's `eId` idea of stable
structural identifiers for provisions. The NZ LENZ DTDs confirm the anatomy
worth modelling: front matter / body-as-hierarchy-of-parts-and-provisions /
schedules. We store relational rows, not XML, but provision ids should be
minted AKN-style (`{shorthand}/pt{n}/s{num}`) so they stay stable across
re-extractions and could later anchor cross-references.

### `acts` (catalog; one row per Work)

| column | notes |
|---|---|
| id | opaque SHA-256 of `act:{shorthand}` (first 12 hex) — see Identity and keys |
| shorthand | e.g. `IA1965`. Content field, never the primary id |
| title | e.g. "Interpretation Act 1965" |
| year | int, parsed from shorthand/title |
| status | constant `current` for now; future-proofs repealed/uncommenced |
| subtype | nullable: constitutional / private / imperial / english_commercial |
| current_as_at | ISO date from the status banner (detail page) |
| listing_stamp | the `_` value from the listing PDF link — change-detection key |
| source_url | `/Act/{SHORTHAND}` absolute |
| pdf_url | absolute, without the `_` param |
| rss_url | per-Act amendments feed |
| has_sl | bool: Subsidiary Legislation action present on listing |
| long_title | from detail page front matter |
| content_text | full body text; NULL = not yet extracted, "" = extraction ran, empty |
| has_content, fragment_count, extracted_at | as in judgements |
| created_at | first import |

FTS: `title`, `long_title`. Facets: `year`, `subtype`, `has_content`.
Sort: `title asc`.

### `acts_fragments` (one row per provision of the current expression)

**Fragment unit = the section** (regulation/rule for SL), not the subclause.
Subsections and paragraphs are sentence fragments that only parse under
their chapeau; one-row-per-subclause produces orphaned, misleading text
(an accuracy hazard under ToU cl 13(b)), degrades FTS ranking, and forces
reassembly at read time. A section is the natural citation, search, and
reading unit — and conveniently the unit SSO's own lazy-load endpoint
serves.

Sub-structure is preserved without subclause rows, via two mechanisms:

1. `content_text` keeps every structural label verbatim — `(1)`, `(a)`,
   `(i)` — with newlines at structural boundaries, so pinpoint cites are
   FTS-findable and the text re-splits deterministically.
2. `structure_json`: the parsed subtree per fragment —
   `{num, kind: subsection|paragraph|subparagraph|proviso|illustration,
   char_start, char_end, children: [...]}` with offsets into
   `content_text`. The parser builds this tree anyway while flattening;
   storing it future-proofs pinpoint anchors, subclause-level version
   diffing, and citation-grade retrieval without a fourth table.

**Oversize rule:** if a section's flattened text exceeds
`STATUTES_MAX_FRAGMENT_CHARS` (default ~9,000), split at the next
structural level down, prefixing each child fragment with a breadcrumb
(section number + heading + chapeau) so every fragment remains
self-contained. Definition sections split per defined term
(`provision_id` like `IA1965/s2/def-act`); Schedules split per
schedule-paragraph. Splits are structural, never blind character windows.

Columns: standard Zeeker fragments table plus `provision_id` (AKN-eId-
flavoured, human-readable: `IA1965/s9A`, `IA1965/pt2/s10`,
`IA1965/sch1/para3`, `IA1965/longtitle`), `provision_num` ("9A"),
`unit_kind` (`section` | `schedule_para` | `definition` | `longtitle` |
`preamble`), `heading`, `part_num` / `part_heading` (nullable),
`division` (nullable), `seq` (document order), `content_text`,
`structure_json`. FTS on `content_text` and `heading`. Facets:
`unit_kind`.

Identifier rules: fragment primary key stays an opaque hash (Zeeker
convention); `provision_id` is a content field, deterministic from
structure — re-extraction after an amendment regenerates stable ids for
unchanged provisions. If Parliament renumbers a provision the id changes
with it; tracking identity across renumbering (AKN's `wId`) is explicitly
deferred until point-in-time text (Phase 4) exists to need it. Amendment
history annotations in the source text (e.g. "[Act 19 of 2025 wef
05/12/2025]") are captured into a `source_notes` field on the fragment
rather than polluting `content_text`/FTS.

### `gazette_stream` (event log; SHIPS IN FIRST RELEASE — it powers updates)

One row per as-published item across the supplement series. Cheap to build
(page-1 polls), valuable on its own ("what was gazetted this week"), and the
substrate of the rolling-update architecture above.

| column | notes |
|---|---|
| id | opaque hash of `{series}:{year}:{number_seq}` — see Identity and keys |
| series | `sl_supp` \| `acts_supp` (later `bills_supp`) |
| number | gazette/act number as shown, e.g. "S 355/2026", "19 of 2025" |
| number_seq | integer part of the number — the cursor (with `year`) |
| year | publication year |
| title | as published |
| published_date | ISO, from the `/Published/{date}` URL segment |
| source_url, pdf_url | absolute; strip the `_` param from pdf_url |
| event_kind | parsed from title: `new` \| `amendment` \| `revocation` \| `commencement` \| `corrigenda` \| `other` |
| principal_title | parsed principal work name for amendment/revocation events, nullable |
| principal_id | resolved FK into acts/sl catalog, nullable |
| principal_resolved | bool — false rows are the reconciliation safety net's job |
| created_at | ingestion timestamp |

FTS: `title`. Facets: `series`, `year`, `event_kind`, `principal_resolved`.
Sort: `published_date desc`. No fragments; as-published *text* is out of
scope (the consolidated works carry the text — the stream carries events and
PDF pointers).

### SL schema scaffold (deferred build, designed now)

Mirror images of the Acts tables, so the extraction/queue machinery is
shared, with these deltas:

- **`sl`** (catalog; one row per instrument-work): as `acts`, plus
  `sl_number` ("S 81/2026" or revised-edition cap number), **`parent_act_id`
  / `parent_act_shorthand` / `parent_act_title`** (the empowering Act — the
  defining extra dimension of SL; facet on it), `instrument_type` parsed
  from the title suffix (`Regulations` \| `Rules` \| `Order` \| `Notification`
  \| `Proclamation` \| ...; facet). The NZ LENZ schema models regulations as
  the same cover/front/body/end anatomy as acts, with an `empowering-prov`
  slot — `parent_act_id` plus an optional `empowering_provision` column
  ("s 67(1)") is our relational equivalent.
- **`sl_fragments`**: identical design to `acts_fragments` (section-unit
  rule, structure_json, oversize rule all apply); the unit is a
  regulation/rule/paragraph instead of a section but the parser is the same
  hierarchy walk (`provision_id` like `AMA2017-S81-2026/r5`,
  `.../r5` splits to `.../r5(2)`-style children only under the oversize
  rule).
- **`sl_versions`**: identical to `act_versions`.

Verify before building: the SL work-id format in `/SL/{id}` URLs; whether SL
detail pages expose the parent Act as structured data (the Acts pages link
SL via `?ViewType=Sl`, which may be the cleaner join direction — enumerate
each Act's SL from the Act side).

### `act_versions` (timeline metadata; no text in this release)

`id` (hash of `{act_id}:{valid_date}` — see Identity and keys), `act_id`
(FK to acts.id), `shorthand`,
`valid_date`, `doc_date`, `amended_by` (e.g. "Act 19 of 2025", or
"2020 RevEd"), `historical_url`, `pdf_url`, `is_current` (bool),
`created_at`. Facets: `valid_date`. This table is append-mostly: each
incremental run that detects a change appends the new timeline rows it hasn't
seen.

Fill in `[resource.*.columns]` descriptions in `zeeker.toml` for every column,
judgements-style.

---

## State management in ephemeral CI (differs from judgements — read this)

GitHub Actions wipes the filesystem between runs; the only durable state is
the SQLite database itself (round-tripped via `zeeker build --sync-from-s3`
and `zeeker deploy`). Therefore:

- **All cursors are derived from the DB, never from checkpoint files.**
  Stream high-water mark = `SELECT max(number_seq) FROM gazette_stream WHERE
  series=? AND year=?`. Extraction queue = `content_text IS NULL`. Listing
  diff baseline = the stored `listing_stamp` column. A fresh runner
  reconstructs the entire crawl state from the synced DB.
- The `.cache/` extraction cache only needs to survive *within* a single
  build (it bridges zeeker's module reload between main-table and fragments
  phases) — ephemeral disk is fine.
- The per-work failure/quarantine tracker is the one piece of state that is
  acceptable to lose between runs: losing it just means a failed work gets
  retried sooner. Optionally persist it with `actions/cache`; do not block
  on it.
- Judgements-style `checkpoint_*.json` resume files are NOT used for
  cursors. If a run dies mid-queue, the DB already reflects completed work
  and the next night resumes naturally.

---

## Repo bootstrap and ops

**Bootstrap** (new repo, suggested `zeeker-sg/statutes`):

```
uv run zeeker init statutes
uv add httpx beautifulsoup4 lxml tenacity click
uv add --dev pytest
# copy .claude/skills/zeeker-source-creator/ from the judgements repo

uv run zeeker add acts --fragments \
  --description "Singapore Acts in force — consolidated current text with per-provision fragments and version timeline metadata, from Singapore Statutes Online." \
  --fts-fields title --fts-fields long_title \
  --fragments-fts-fields content_text --fragments-fts-fields heading \
  --facets year --facets subtype --facets has_content \
  --sort "title asc" --size 25

uv run zeeker add act_versions \
  --description "Version timeline metadata for each Act: every point-in-time expression with validity date, amending Act, and historical URLs. No historical text in this release." \
  --facets valid_date --sort "valid_date desc"

uv run zeeker add gazette_stream \
  --description "Append-only event log of legislation as published: SL Supplement and Acts Supplement items with gazette numbers, publication dates, and parsed amendment/revocation events. Powers rolling updates." \
  --fts-fields title \
  --facets series --facets year --facets event_kind --facets principal_resolved \
  --sort "published_date desc"
```

Then fill `zeeker.toml` `[project]` (source, the ToU-compliant licence
strings) and every `[resource.*.columns]` description.

**Ops hardening:**

- Workflow `concurrency: { group: nightly, cancel-in-progress: false }` so a
  late-running build never overlaps the next cron fire.
- Post-build sanity assertions, failing the workflow loudly: per-table row
  counts never decrease vs the synced DB (append-mostly invariant; catalog
  rows are flagged, never deleted); every `has_content = true` act has
  `fragment_count > 0`; unique-index tripwires intact.
- GitHub Actions failure notifications on; a red nightly run is the
  monitoring system.

**AI summaries: DEFERRED (user decision, no current use case).** No
`summary` columns in the schema — Zeeker convention is no speculative
fields. If a use case appears later, judgements' Phase-3 machinery
(OpenAI-compatible endpoint, cache, checkpoint) is reusable and the
columns are purely additive; nothing in this build depends on them.

---

## Build phases

### Phase 0 — Spike (timebox: half a day of work, run across 1–2 nights)

ToU constraints apply to the spike too: all scripted fetches against the live
site run inside the 03:00–07:00 SGT window (a handful of one-off manual
checks in a browser are fine anytime, but `httpx` scripts wait for the
window). Build the window guard FIRST — it's ~20 lines and everything else
depends on it. And per cl 13(c)/(d): if SSO presents an anti-bot or security
measure on any route, do not attempt to bypass it; drop that route.

1. Fetch one short Act (e.g. `AA1928`) and one long Act (e.g. `IA1965`) with
   plain httpx; diff what's in the initial HTML vs the rendered site.
2. Identify the lazy-load endpoint from the page's embedded JS/IDs; reproduce
   it with httpx (only endpoints the site itself calls openly — observe via
   browser devtools, don't probe). If painful, test `?ViewType=Print`. Decide
   the route.
3. Verify the `_` timestamp hypothesis across ~5 Acts (compare against their
   latest timeline dates; re-fetch the listing on two different days if
   possible).
4. Fetch one `?ViewType=Rss` feed; record its XML shape.
5. Check `https://sso.agc.gov.sg/robots.txt` and note any crawl-delay.
6. Write findings to `docs/phase0-findings.md`. **Stop and surface findings
   if route (1) and (2) both fail** — Docling fallback changes the plan.

Save fixture HTML under `tests/fixtures/` (fetch via a
`scripts/fetch_fixtures.py`, as judgements does) covering the structural
edge cases the provision parser must survive: one short Act, one long Act,
one deeply nested section (subsections → paragraphs → sub-paragraphs, ideally
with a proviso or illustration), one fat definitions section (s 2 of a major
Act), and one Act with substantial Schedules. Write extraction tests against
them before writing the crawler.

### Phase 1 — Catalog

`uv run zeeker add acts --fragments` with description, FTS fields, facets, and
sort per the schema above (skill reference: "Metadata: Fill in Everything You
Can"). Implement discovery: 2 listing requests (PageSize=500) → upsert
`acts` rows with listing-derived columns. Reuse judgements' crawl block
(jittered 1.5s±0.5s delay, 3-attempt tenacity backoff, circuit breaker after 5
consecutive failures, `ZeekerBot/1.0 (+https://data.zeeker.sg)` UA). No
checkpointing needed at 2 requests. Env-overridable knobs prefixed
`STATUTES_`.

### Phase 2 — Full-text backfill (current expressions)

Enrichment pass over `acts` where `content_text IS NULL`, capped at
`STATUTES_EXTRACT_MAX_PER_RUN` (default 10–15; Acts are large — hundreds of
KB — and some need many lazy-load calls each, so the cap is per-Act, with the
delay applied per-request). For each Act:

1. Fetch detail page → parse status banner, long title, TOC, timeline.
2. Fetch body via the Phase 0 route → parse provisions into fragments.
3. Archive raw HTML under `.cache/statutes_html/` and parsed output under
   `.cache/statutes_extractions/` with atomic writes — this disk cache is the
   source of truth for `fetch_fragments_data`, because zeeker reloads the
   module between the main-table and fragments phases (same reason as
   judgements; read that module's docstring).
4. `existing_table.update()` the row; insert/refresh `act_versions` rows.

Failure tracking per Act follows the judgements quarantine pattern (failure
count, last error, retry-after) but per the State-management section it may
live in ephemeral disk or `actions/cache` — losing it between runs is
acceptable. `STATUTES_EXTRACT_ENABLED=0` disables enrichment for
discovery-only runs.

**Backfill timing under the ToU window:** all live extraction happens inside
the nightly 03:00–07:00 SGT window, one run per night. The per-run cap is
therefore time-based as much as count-based: with ~3.5h of usable window and
polite per-request pacing, 30–60 Acts/night is realistic, so set
`STATUTES_EXTRACT_MAX_PER_RUN` generously (e.g. 40) and let the runtime
window guard be the true stop condition. 525 Acts ≈ **2 weeks of nightly
runs**. There is no "run it in a loop to finish in a day" option — the window
forbids it. Daytime development uses cached fixtures, never the live site.

### Phase 3 — Rolling updates (implements the architecture section above)

Build the nightly run as three ordered steps inside one `fetch_data` cycle:

1. **Stream ingestion** — poll page 1 of SL-Supp and Acts-Supp (current
   year, Number DESC); insert rows above the (series, year) cursor into
   `gazette_stream`; classify `event_kind` from the title; resolve
   principals against the `acts` catalog (SL principals stay unresolved
   until the SL catalog exists — that's fine, the rows wait); invalidate
   resolved principals.
2. **Catalog reconciliation** — the 2-request Acts listing diff (new ids /
   vanished ids / changed `listing_stamp` → invalidate). Fallbacks if the
   timestamp hypothesis fails: per-Act RSS feeds (~15 min for all 525, fits
   the window) or the What's New page.
3. **Queue drain** — extraction of NULL-content works until the per-run cap
   or the 06:45 SGT hard stop, whichever first.

Because new legislation lands daily, cadence is **Tier 1 daily,
permanently** — not just during backfill. The nightly run is cheap when
nothing happened (4 requests, no extractions) and self-balancing when a
busy gazette day queues a dozen works. Cron `30 19 * * *` UTC (03:30 SGT),
job timeout ending before 07:00 SGT as belt-and-braces alongside the
in-code window guard. Encode the cadence header per the skill's
scraping-strategy reference; workflow builds with `--sync-from-s3` and
`--setup-fts`, deploys to S3 (secrets: `S3_BUCKET`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`). Around 1 Jan, poll both the old and new year's
stream pages for a couple of weeks to catch the cursor rollover.

### Phase 4 (deferred) — Historical expression text

Point-in-time text keyed by (act_id, valid_date), extracted from
`historical_url` pages with the same machinery. Decide scope later (all
versions ≈ 10–20× current volume; "since 2020 RevEd only" is a sane middle).

### Phase 5 (deferred) — Subsidiary Legislation, repealed & uncommenced Acts

SL schema is already scaffolded above; the build sequence when it goes live:
(1) SL-specific spike (work-id format, parent-Act linkage — consider
enumerating each Act's SL via the Act page's `?ViewType=Sl` for clean joins);
(2) catalog discovery (~7–9 listing requests); (3) text backfill — at
30–60 works/night for 3,000–4,500 works this is a 2–4 month nightly queue
sharing the window with Acts maintenance, so add simple queue priorities
(invalidated works > new works > backfill); (4) flip on SL principal
resolution in stream ingestion, which has been accumulating SL events in
`gazette_stream` since release one. Repealed/uncommenced Acts are trivial by
comparison: same listing template, `status` column already exists.

---

## Definition of done (first release)

- `uv run zeeker build` from a clean checkout produces a DB where: all 525
  current Acts have catalog rows; ≥1 backfill batch has full text with
  per-provision fragments and FTS hits on provision text (e.g. searching
  "purposive interpretation" returns IA1965 s 9A); `act_versions` is populated
  for every extracted Act.
- Incremental run on an unchanged site is a no-op beyond the 4
  stream/listing requests; a simulated `listing_stamp` change re-queues
  exactly that Act; a fixture stream page with one item above the cursor
  produces exactly one `gazette_stream` row, and an "(Amendment)" title
  whose principal exists in the catalog invalidates that work.
- Extraction tests pass against committed fixtures — including: a deeply
  nested provision round-trips (`structure_json` offsets slice
  `content_text` back into the exact subsection/paragraph texts), and an
  oversize definitions section or Schedule splits into breadcrumbed,
  self-contained child fragments; `uv run black --check .`
  and `uv run ruff check .` clean.
- `zeeker.toml` has full column descriptions; CLAUDE.md documents source,
  cadence, env vars, and the Phase 0 findings link.
- **ToU compliance verified:** unit test proves the window guard refuses live
  fetches outside 03:00–07:00 SGT and checkpoints cleanly at the 06:45 SGT
  hard stop; workflow cron is `30 19 * * *` UTC with a job timeout ending
  before 07:00 SGT; published metadata renders both cl 13(a) notices
  (Singapore Government copyright / reproduced with permission of AGC, and
  "check SSO for the latest version") plus a no-affiliation disclaimer; UA is
  the honest ZeekerBot string.
