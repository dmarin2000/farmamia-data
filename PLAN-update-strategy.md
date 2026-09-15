# Farmamia — data update strategy (plan, not yet implemented)

Status: **planning** (2026-09-14). No code. Sibling files:
`REPORT-datasources-2026-09-10.md` (what the asset contains),
`scripts/build_catalog.py` (current single-shot build),
`curated/codici_legacy.csv` (manual glue).

---

## 1. Goal

The app ships a frozen 111.5 MB snapshot (schema 3). Sources move:
new drugs register, ex-OTCs delist (the Maalox/Iridil class), DISPO
re-registers (HEDRIN 200 mL not yet inscribed). Today, freshness =
"whenever we rebuild + ship a new APK". Plan a mechanism where

- content stays fresh **without** a 100 MB download or a store review,
- offline-first is preserved (last known state always usable),
- every applied change is verifiable (provenance in the DB),
- curation work (bridge rows) is triggered, not remembered.

Non-goals: real-time data, full AIFA mirrors, in-app editing of the
catalog (the app stays read-only consumer of `farmamia_ref.db`).

---

## 2. Current state (what exists today)

| piece | where | behaviour |
|---|---|---|
| asset DB | `app/src/main/assets/farmamia_ref.db` | frozen at build time |
| `CatalogDb` | app | copies asset → `files/farmamia_ref.db` when `catalog_meta.schema_version < EXPECTED_SCHEMA_VERSION` or table missing |
| `CatalogRepository` | app | read-only SQL over the copy (byCode / byEan / search) |
| `AifaRepository` | app | OkHttp single-lookup fallback (9-digit AIC miss), no key |
| `build_catalog.py` | data repo | one-shot: 3 CSVs + curated CSV → asset; writes `catalog_meta` (file names, dates, schema_version, row counts) |
| source fetch | manual | curl/git, URLs in REPORT appendix |

So today **everything** (structure + content + curation) moves only via
APK release. That is the gap.

---

## 3. Source inventory — cadence, format, what changes

| source | URL | cadence | format / size | what changes | failure mode |
|---|---|---|---|---|---|
| AIFA registro | `drive.aifa.gov.it/farmaci/confezioni_fornitura.csv` | daily | CSV 79 MB raw (159,901 AICs) | new AICs, **delistings** (Maalox class), name changes | file layout drift, delay, drive 403 vs curl (probe in P1) |
| AIFA Classe A/H | `www.aifa.gov.it/it/liste-dei-farmaci` links dated `/documents/…/Classe_{A,H}_per_nome_commerciale_<DD-MM-YYYY>.csv` | **republication at AIFA's whim — last 30-04-2026 (5 months); repo copy byte-identical (sha256 verified 2026-09-14) → repo in sync, source cadence is the slowness** | CSV 1.4 MB + 403 KB, cp1252, 9-digit AIC | membership flips (A↔H, out) | dated href = poll signal; per-source staleness band 120/365d (not 30/90) |
| Mibact DISPO | via `dati.salute.gov.it/page-data/it/dataset/dispositivi-medici/page-data.json` → `DISPO_RDM_1_<yyyymmdd>_csv.zip` | weekly | zip 57 MB → CSV ~500 MB (2.4M rows) | inscriptions, revocations, CND changes | page-data.json key drift |
| PA / ATC | `drive.aifa.gov.it/farmaci/PA_confezioni.csv` (11 MB), `atc.csv` (194 KB) | daily | CSV | (PA unused by app today) | same as registro |
| fogliettoillustrativo delisted pages (foglio "410") | per-product pages, 1,619 known | crawl on demand | HTML | **new delistings appear** = reclassification candidates | site redesign (cost us a parser once) |
| `curated/codici_legacy.csv` | local | manual, case-driven | CSV, 9 rows | brand glue (Maalox/Iridil/HEDRIN…) | human forgetting |

Key asymmetry: **rows change a little, often** (dozens to low hundreds
per week), while the *repertory shape* is stable. That drives the whole
design below.

---

## 4. Core model: three planes, three cadences

### Plane 1 — structure (rare)
Table schemas. Bumps `catalog_meta.schema_version` (current: 3).
Still APK-only: the bundled asset must match the app's
`EXPECTED_SCHEMA_VERSION`. Cadence: months.

### Plane 2 — content (weekly, automated)
Row-level churn in `med_classea` + `dispositivi_medici`, recomputed
subset. Cadence: **when an input hash changes** (registro daily poll,
Classe A/H filename poll, DISPO weekly). Delivered as a small **delta**,
not a new APK (see §6).

### Plane 3 — curation (event-driven, human + bot)
`codici_legacy` rows + forced-includes. Triggered by:
- the delisting crawl (§7),
- user-visible misses (feedback loop, see §8.5),
- new DISPO registrations of a bridged brand's family.
Cadence: days–weeks per event.

`catalog_meta` (plane 2+3 bookkeeping) always updates with them: add
`content_version` (monotonic build number) and per-source `*_date` /
`*_sha256` keys, surfaced in-app as "dati aggiornati al…".

---

## 5. Delivery: baseline + delta

App-side storage split (both in `files/`, writable):

```
files/farmamia_ref.db        ← copied from asset (baseline, schema N)
files/farmamia_content.db    ← small overlay DB, downloaded/applied (content M ≥ N)
```

`CatalogRepository` reads merged views (one per table):

- overlay tables hold the **current value of changed rows** plus a
  `tombstone_<table>` set of deleted keys,
- view = `SELECT * FROM overlay_x UNION SELECT * FROM base_x WHERE key
  NOT IN (overlay_x keys) AND key NOT IN (tombstone_x)`.
  Upsert = overlay row (hides the base copy implicitly); delete =
  tombstone row. Keys: AIC / progressivo_dm_ass / legacy_aic+ean.
- base file opened `mode=ro` — immutable by construction; the overlay
  file gets the same few indexes (small either way), the base keeps its
  shipped indexes (usable from the views across ATTACH).
- **schema coupling**: the overlay file stores the baseline
  `schema_version` it was built against; an app upgrade that bumps
  `EXPECTED_SCHEMA_VERSION` discards the overlay (new baseline, new
  chain).
- **Overlay lifecycle (Q3)**: the overlay is always **built from the
  baseline** by the pipeline — it is the cumulative diff since that
  baseline version, never an append-log of everything. When a new
  baseline ships (schema bump, or the cap below), the pipeline ships a
  fresh overlay built against it (usually near-empty). Cap: overlay
  > ~10 MB or > ~50 versions → publish a same-schema content re-bake as
  the new baseline; overlay resets. No overlay ever merges onto a
  different baseline.

**Why overlay instead of patching in place:** the baseline stays
byte-stable → trivial to verify (sha256 vs manifest) and trivial to
replace on the next app release; deltas never rewrite 111 MB.

**Delta format** — **SQLite overlay file** (Q2; conventions per Q8 =
same as the asset: no WAL, `journal_mode=delete`, header comment
block):

- `upserts`: full rows (all NOT NULL columns) per table,
- `deletes`: key values (AIC / progressivo / legacy_aic+ean),
- `content_version` target,
- per-table counts + input hashes for provenance.

Typical weekly delta: < 1–2 MB (91k-row table, ~50 changed rows;
57k-row device table, ~100; legacy: 0–2 rows; pa: dozens).

**Baseline re-ship (Q5)**: a baseline whose *schema* changed is
released **in lockstep with the app** that expects it (the APK *is* the
baseline channel for structure — no standalone schema-baseline
downloads, no split-brain). Same-schema content re-bakes (the Q3 cap)
may ship on their own; the app accepts them only if
`schema_version == EXPECTED_SCHEMA_VERSION` (§8.4).

---

## 6. Pipeline (server side)

`scripts/pipeline/` in farmamia-data:

```
fetch_sources.py    # 1. GET all sources → raw/
                    #    built into the catalog: registro, classe A/H,
                    #    DISPO, curated CSV, and PA (Q6: pa_confezioni
                    #    table). Fetched + hash-tracked, not built:
                    #    atc (no table yet).
                    #    Classe A/H discovery (Q4 resolved): GET
                    #    www.aifa.gov.it/it/liste-dei-farmaci (200, no
                    #    auth) → regex hrefs
                    #    Classe_[AH]_per_nome_commerciale_(date)\.csv,
                    #    take latest date → download.
                    # 2. validate: row counts in sanity bands, required
                    #    columns present, AIC shape, CND shape, dates
                    #    parseable. Staleness bands **per source**
                    #    (A/H republishes at AIFA's whim — 5 months is
                    #    normal: warn >120d, fail >365d; registro 30/90;
                    #    DISPO 7/14). Fail → keep last good, alert.
                    #    Known quirk: Classe H row 1 = shifted header
                    #    remnant; build locates AIC column by header
                    #    name + 9-digit filter → already robust.
build_catalog.py    # existing; gains: --content-version, --baseline-ref,
                    # --delta-out, writes content_version + source
                    # dates/shas into catalog_meta
diff_catalog.py     # baseline_prev vs baseline_new (or vs overlay
                    # state) → delta file + manifest entry
publish.py          # upload baseline (if schema/quarterly) + deltas +
                    # manifest.json → static host
```

`manifest.json` (few KB):

```json
{
  "schema_version": 3,
  "content_version": 41,
  "baseline": {"url": ".../baselines/farmamia_ref_v3.db",
               "sha256": "...", "size": 116700000},
  "deltas": [
    {"from": 40, "to": 41, "url": ".../deltas/d40-41.sqlite",
     "sha256": "...", "size": 900000,
     "sources": {"registro": "2026-09-14", "dispo": "2026-09-07", ...}}
  ],
  "generated_at": "2026-09-14T06:00:00Z"
}
```

**Host**: Cloudflare R2 (files) + GitHub (Actions + Releases) — concrete
design in §6.1. No VPS, no database server on the catalog path.

### 6.1 Host — concrete design (Cloudflare + GitHub, no VPS)

A proposed split was evaluated (Workers+Cron → AIFA job, R2 → CSV
archive, optional CDN, Supabase/Postgres → database). Verdict: R2 and
Workers fit; the build does not fit in a Worker; Supabase does not fit
the catalog path. Final architecture:

```
CF Cron (free, daily 06:00 CET)
  └→ Worker (free: 10 ms CPU, 128 MB) — thin only:
       GitHub `repository_dispatch` (1 subrequest) → GH Actions
       serves GET /manifest.json, /deltas/*, /baselines/* from the R2 bucket
         └→ Actions job (≤ 10 min; public repo = free minutes):
              fetch_sources → validate → build_catalog → diff_catalog
              → upload baseline/deltas/manifest via S3 API (scoped R2 token)
              → GitHub Release mirror (redundancy)
App ── GET https://catalog.<domain>/…
       (Worker→R2, edge-cached; fallback: Release URL)
```

**Division of labour (why):**

- **Workers free = 10 ms CPU/invocation, 128 MB, 100k req/day.**
  Fits: cron tick, manifest passthrough, health. Does **not** fit the
  build (streaming a 500 MB DISPO CSV + 79 MB registro through
  `sqlite3` needs minutes of CPU). Build stays in Actions — fits free
  either way (public: unlimited; private 2,000 min/mo vs our ~10 min).
- **R2 = single source of truth for files**: original CSVs (provenance
  archive), baselines, deltas, manifest. Free tier (verified 2026):
  10 GB storage, 1 M Class-A, 10 M Class-B ops, **zero egress**.
  Our growth ≈ 1.1 GB/yr → free indefinitely. S3-compatible API ⇒
  upload from Actions with a scoped token (ready-made actions:
  r2drop / ryand56 / magicwallet); pipeline needs no CF-specific code.
- **CDN/cache**: R2 via Worker rides the CF edge. Deltas/baselines are
  content-addressed-ish (immutable per version) → `cache-control:
  immutable`; manifest → `max-age=300, must-revalidate` (a new
  version must propagate within 5 min, not days).
- **Supabase (PostgreSQL) — out of P1**: the catalog ships as *files*;
  a row store would force an HTTP round-trip per search and break the
  offline-first core. It is a legitimate P2 candidate for the *write*
  paths (miss reports, curation queue, auth/dashboard — free 500 MB DB,
  auto-pauses after 1 week idle, cold start acceptable at that traffic).
  Zero-infra alternative tried first: curation queue = GitHub issues
  (dashboard + notifications for free), miss reports = jsonl lines in
  R2 via the same Worker.
- **Redundancy**: R2 primary, GitHub Releases mirror; app tries in
  order. Public-read, no auth on the GET path.

**Sub-decisions (resolved 2026-09-14):**

- **Domain: CF subdomain** (no custom domain purchase). Served from
  the Worker's `<name>.<account>.workers.dev`.
  *Gotcha:* `workers.dev` subdomains are first-come-first-served and
  squat-able by anyone — **deploy a stub Worker early to reserve the
  name** before the app ships with the URL hardcoded.
- **The same Worker hosts `POST /report` (P2)**: app miss-reporting
  (§7.5/§8.6) → Worker appends a jsonl line to R2. No auth in v1
  (small bodies); per-IP rate limit in the Worker. Together with the
  manifest GETs this is the entire request surface — comfortably
  inside the 100k req/day free tier at any realistic DAU.

Content-version rule: `content_version` bumps only when *content*
changed (any table diff non-empty); a no-op source re-download bumps
nothing and publishes nothing. Schema bumps (plane 1) reset the delta
chain (new baseline, chain restarts). **Deltas are immutable:** the
manifest lists the full chain, the app requests whatever it's missing;
pruning happens only when a new baseline ships. (One weekly 1–2 MB
delta is trivial to keep; ~500 chains/yr is still < 1 GB.)

Budget: DISPO 2.4M-row parse + 79 MB registro built locally in ~2 min;
expect ≤ 10 min on a free Actions runner (300 CPU-min) — measure once
in P1 before committing.

---

## 7. Curation automation (plane 3)

1. **Delisting crawler** (weekly): re-crawl the fogliettoillustrativo
   "delisted" page set (1,619 known; parser already built — the one
   that cost hours). Diff vs previous run → *newly delisted* drug names.
2. For each new name: auto-grep DISPO (name/brand).
   - matches → draft queue entry in `curated/queue.md` with the
     candidate progressivi. Realistically 1:N (Maalox alone yields 3
     name rows): the human picks pack/size per the Iridil rule,
   - CND in included set → no action needed (name search covers it),
   - CND in excluded set + famous brand → queue bridge + force-include.
3. **Force-include rule** (build): every `codici_legacy.progressivo_dm`
   is copied into `dispositivi_medici` even if its CND is excluded or
   expired — bridges never dangle. (Currently true for CND, make it
   explicit for expiry.)
4. Human verifies queued drafts (10 min each: box/photo or two web
   hits) → flip to verified → next content build ships them in the
   overlay.
5. **Feedback loop** (later): app "not found" events → one-tap
   "segnala" (§8.6) → `POST /report` on the catalog Worker (same host,
   §6.1) → jsonl line in R2 → the crawler's check list.

---

## 8. App-side behaviour

1. **When to check**: app launch + every 24 h + on entering search
   (any of the three, debounced). GET `manifest.json` only (KB).
2. **What it does**:
   - `local content_version == manifest.content_version` → done.
   - gap small (delta chain exists, total < ~10 MB) → download deltas,
     verify sha256, apply in a transaction, rebuild views.
   - gap large or baseline newer → blue/green swap (Q5): download new
     baseline to `files/farmamia_ref_new.db` → sha256 → open + read
     `schema_version` → rename active to `.old`, promote new → delete
     the old after first successful use. The active file is never
     modified in place; any step failing leaves blue untouched.
     Same dance applies to the overlay file.
3. **Where**: background executor, network-aware (Wi-Fi by default for
   the big one; deltas fine on mobile). No main-thread DB work.
4. **Reject rules**: downloaded baseline whose `schema_version` ≠
   app `EXPECTED_SCHEMA_VERSION` → discard (delete the `_new` file),
   keep current state, suggest app update (Q5: new-schema baselines
   only arrive with the app that expects them, so a mismatch = the
   user is on an old app build). Overlay whose stored baseline schema
   ≠ current baseline → discard (§5 coupling).
5. **UX**: silent. One subtle signal: "dati aggiornati al 14/09/2026"
   in settings/about (from `catalog_meta`); optionally a one-time
   "database aggiornato" toast when a delta lands while the user is in
   search. No blocking spinner ever.
6. **Miss reporting** (phase 2): scanner "not found" card gains an
   optional "segnala" → stores (code, name typed, timestamp) locally,
   flushed next check via `POST /report` on the catalog Worker (jsonl
   in R2, §6.1); powers §7.5.
7. **Offline semantics**: unreachable manifest → keep current overlay,
   everything works, UI shows last-known dates. The *first* launch of a
   new install with no network works off the bundled baseline (it
   exists for exactly this).
8. **Integrity**: overlay and baseline are hash-verified against the
   manifest; mismatch → delete overlay (fall back to baseline), retry
   later. Never merge unverified data.

---

## 9. Per-table update matrix (the "when/how each DB updates" table)

| table | key | plane | trigger | cadence | delivery | app applies |
|---|---|---|---|---|---|---|
| `catalog_meta` | key/value | 1+2+3 | any build | with content | in delta or baseline | on overlay apply / asset copy |
| `med_classea` | AIC = registro `CODICE_AIC` (9-digit, Q7) | 2 | registro hash change (daily poll) + Classe A/H filename change | daily check, build on change | delta upserts/deletes (delistings = deletes; new approvals = upserts) | overlay view |
| `dispositivi_medici` | progressivo | 2+3 | DISPO weekly file; curation force-includes | weekly | delta; bridge-forced rows ride along | overlay view |
| `codici_legacy` | legacy_aic / ean | 3 | curation queue (crawler + reports) | event-driven | delta (tiny) | overlay view |
| `pa_confezioni` | (define in P1 — PA code/confezione key) | 2 | `PA_confezioni.csv` hash (daily poll) | daily check, build on change | delta | overlay view (Q6: table yes, UI later) |

Structure (any DDL) = plane 1 = new APK only.

Note on AICs (Q7): the `med_classea` key is the registro `CODICE_AIC`,
**9-digit**; the 13-digit 20-prefix values are a separate column.
The app's `CatalogCode` already accepts 6/9/13; `byCode` handles both.
The *semantics* of byCode/byEan/search are unchanged by the overlay —
the app change in P1 is the plumbing (views, manifest client, overlay
lifecycle, blue/green baseline swap, `pa_confezioni` read API), not the
lookup logic.

---

## 10. Risks → mitigations

| risk | mitigation |
|---|---|
| source layout drift (cost us: DISPO quoting, foglio parser) | pipeline validation bands (§6) fail closed: keep last-good, alert, never publish a broken build |
| AIFA naming: Classe A/H filename convention | poll the listing page, not a guessed URL; keep the dated-name regex as fallback |
| delta chain divergence after app skips versions | manifest always offers "reset to baseline" path; app requests baseline when chain > N or total size > 10 MB |
| APK size growth (asset 111 MB, APK ~100 MB) | Play: AAB + Play Asset Delivery (install-time vs fast-follow); Fe: check 150 MB cap. Independent of this plan but interacts — see §11 P3 |
| weekly DISPO lags real-world registrations (HEDRIN 200 mL case) | inherent to the source; AIFA-API single-lookup fallback stays for drugs; device misses → curation queue |
| static host availability | manifest+files on two hosts (e.g. GitHub Releases + NAS/Fe static); app tries in order |
| curation rot (bridges pointing at revoked progressivi) | force-include keeps rows present; revoked device row → bridge resolves to a row with end dates → app already renders end date; obvious to user |
| source staleness (Classe A/H copy 5 months old today) | staleness bands in validation (§6); `catalog_meta` dates visible in settings → users and we notice drift |
| AIFA drive flakiness/403 for scripted clients | P1 probe with the exact curl the app/pipeline uses; proxy fallback; fail-closed keeps last good |
| schema_version/content_version confusion | plane split is documented in `catalog_meta` itself (a `planes` key) and in the app constant comments |

---

## 11. Phased rollout

**P1 — pipeline + overlay (the core loop)** ✅ DONE 2026-09-14
- `fetch_sources.py` + validation; `diff_catalog.py`; `publish.py`;
  Actions cron.
- Build gains the `pa_confezioni` table (Q6).
- App: manifest client (`CatalogRemote`), overlay ATTACH+TEMP views
  (SQLite ≥3.4x rejects permanent views on attached dbs), sha256+size
  verification, blue/green swap of both files (Q5), one-shot
  "Dati aggiornati al …" toast.
- `catalog_meta` gains `content_version` + per-source dates/shas; because
  the baseline gained a table (`pa_confezioni`) this *was* a schema bump:
  3 → 4 (build `SCHEMA_VERSION` + app `EXPECTED_SCHEMA_VERSION` +
  `DataLayerInstrumentedTest` in lockstep).
- Exit: two real content builds published + applied on the emulator ✅
  (cv1 empty pass-through + cv2 real delta; `CatalogRemoteInstrumentedTest`
  pulls the dist/ from 10.0.2.2:8080 on the AVD; suite 39/39 green).
  (offline-safe test: airplane mode keeps working).

**P2 — curation automation**
- delisting crawler + queue + force-include rule; scanner "segnala"
  feedback loop.
- Exit: a newly delisted famous product appears in-app within ~1 week
  of its source appearance, unaided.

**P3 — distribution economics (only if needed)**
- same-schema baseline re-bake per the Q3 cap (overlay size/version
  driven, not calendar) + Play Asset Delivery / Fe cap tuning;
  optionally "download on first launch" for new installs to shrink the
  APK (keeps a minimal seed asset for instant offline).
- Trigger: asset > ~130 MB or Play/Fe friction.

---

## 12. Decisions log (all 8 resolved 2026-09-14 → P1 is unblocked)

1. **Hosting** — **fully resolved** (§6.1): CF Worker (cron + manifest
   GET + P2 `POST /report`) + R2 (files) + Actions (build) + Releases
   (mirror); CF subdomain, **reserve the `workers.dev` name early**
   (squat-able); Supabase deferred to P2 if the reports/queue flow out
   grows the jsonl approach.
2. **Delta container** — **RESOLVED** (Q2): SQLite overlay file
   (single download, direct ATTACH); conventions per Q8. Confirm
   size/churn with one real diff in P1 before shipping the chain.
3. **Overlay lifecycle** — **RESOLVED** (Q3): overlay always built
   from the baseline by the pipeline (cumulative diff since that
   baseline); new baseline → fresh overlay; cap > ~10 MB / ~50 versions
   → same-schema content re-bake as new baseline (§5).
4. **Classe A/H discovery** — **RESOLVED** (2026-09-14): AIFA lists the
   dated files as plain hrefs on `www.aifa.gov.it/it/liste-dei-farmaci`
   (`/documents/<id>/<docid>/…`, no index, no auth, HTTP 200 with curl);
   regex + latest date = current version. Repo copy sha256-identical to
   the published 30-04-2026 file → source simply hasn't republished;
   wider 120/365d staleness band applies (§3).
5. **Baseline re-ship** — **RESOLVED** (Q5): new-schema baseline ships
   in lockstep with the app (APK = baseline channel for structure);
   same-schema re-bakes downloadable on their own, gated on
   `schema_version == EXPECTED`; app applies via blue/green swap
   (`_new` file → verify → promote, §8.2).
6. **`PA_confezioni.csv`** — **RESOLVED** (Q6): yes, a
   `pa_confezioni` table, same overlay treatment (§9); key + UI later.
7. **AIC width mix** — **RESOLVED** (2026-09-14): the asset key is the
   registro `CODICE_AIC` = 9-digit (old-style); 13-digit 20-prefix values
   are a separate column. Classe A/H transparency lists are 9-digit too →
   membership join is width-exact, no conversion. App `CatalogCode`/
   `byCode` already normalizes 6/9/13. Consequence for deltas:
   upsert/tombstone keys on `med_classea` are 9-digit.
8. **Overlay DB conventions** — **RESOLVED** (Q8): same as Q2/the
   asset — no WAL, `journal_mode=delete`, header comment block.
