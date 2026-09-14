# Farmamia — Data Sources & Lookups: Refactoring Report

**Date:** 2026-09-10
**Scope:** `farmamia-data/` (source data) + `../farmamia` (app) data pipeline and search/lookup logic
**Trigger:** code **934480195** (shown `A934480195` on a Maalox Reflu Rapid pack) not found by the app

---

## 1. The trigger case

A user holds **Maalox Reflu RAPID** (aluminium hydroxide + magnesium hydroxide +
dimethicone, 20 single-dose sachets 10 ml, Opella Healthcare Italy S.r.l. / Sanofi)
and sees code `A934480195` / `934480195` on the pack. farmamia search fails on it.

**Why:** the product is **no longer a medicine**. It was reclassified as a
**dispositivo medico** (medical device) and left the AIFA medicinal list. Its
`934480195` is a **legacy AIC** code, kept on the pack for traceability.

### Identity map of the same object

| Representation | Value | Meaning |
|---|---|---|
| Legacy AIC | `934480195` | old authorization code, 9 digits, printed on pack |
| Pharmacy-system display | `A934480195` | `A` prefix + AIC (convention of pharmacy software, e.g. dpfarma) |
| Retail display | `IT934480195` | country prefix + AIC (e.g. redcare.it) |
| **Barcode (CIP, base-32)** | **`VV62B3`** | 6-char code on pack; farmamia's `Barcode.conv32To10` decodes it to `934480195` (round-trip verified against the app's algorithm, incl. its double-precision path) |
| EAN-13 (20-sachet pack) | `2005187210851` | retail barcode — the only barcode the ML Kit scanner sees by default |
| Mibact device registration | CND `0301650209019-MR`, current progressivo `1963804` | device registry identity (see §3C) |

**Consequence for lookups:** every key type (AIC digits, A/IT-prefixed text, base-32
barcode, EAN) must be normalized, and the catalog must answer for **both** medicines
and devices. Today only `med_classea` (AIC key) exists.

---

## 2. Current state (as found)

### 2.1 `farmamia-data/` contents

| File | Origin | Size | Rows | Fetched | Status |
|---|---|---|---|---|---|
| `confezioni_fornitura.csv` | AIFA Anagrafica (see 3A) | 82 MB | 159,901 | 2026-08-29 | feeds the app DB today |
| `confezioni_fornitura.csv.zip` | local zip of the above (macOS Finder, `__MACOSX` inside) | 3.3 MB | — | 2026-09-10 | convenience copy |
| `PA_confezioni.csv` | AIFA (Principi Attivi per confezione) | 11.5 MB | 338,181 | 2026-08-29 | **not used by app** |
| `atc.csv` | AIFA (ATC registry, 5th level) | 198 KB | 7,211 | 2026-08-29 | **not used by app** |
| `Classe_A_per_nome_commerciale_30-04-2026.csv` | AIFA transparency list | 1.5 MB | 10,711 | 2026-09-07 | **legacy** (superseded by Anagrafica); **cp1252 encoded** (`€` = byte 0x80 — not UTF-8) |
| `Classe_H_per_nome_commerciale_30-04-2026.csv` | AIFA transparency list | 413 KB | 2,465 | 2026-09-07 | **legacy**, same encoding caveat |
| `DISPO_RDM_1_20260907.csv` | Mibact device registry (see 3C) | **520 MB** | **2,414,848** | 2026-09-07 | **not imported yet** |
| `DISPO_RDM_1_20260907_csv.zip` | same, zipped | 57 MB | — | 2026-09-07 | — |
| `icons/` | app icons | — | — | — | — |

Encoding/format summary (needed by the import script):

| File | Dialect |
|---|---|
| `confezioni_fornitura.csv` | `;`-delimited, all fields double-quoted, **UTF-8** |
| `PA_confezioni.csv`, `atc.csv` | `;`-delimited, unquoted, UTF-8 |
| `Classe_A/H_*.csv` | `;`-delimited, unquoted, **cp1252** |
| `DISPO_RDM_1_*.csv` | `;`-delimited, unquoted, UTF-8, 16 columns |

### 2.2 App catalog (what the app actually ships)

- `app/src/main/assets/farmamia_ref.db` — **160 MB**, single catalog table
  `med_classea` with **159,901 rows** (full AIFA Anagrafica snapshot 2026-08-29) +
  empty legacy `archivio` / `promemoria` tables.
  (Old 2016 app shipped 19,570-row / 4.4 MB `med_classea` from the Classe-A list.)
- `scripts/import_csv_to_db.py` — `confezioni_fornitura.csv` → `med_classea`
  (20 columns: 5 legacy-mapped + 15 official), unique index on `AIC`, name + PA indexes.
- `CatalogDb.kt` — copies asset to files dir on first run; staleness check =
  "does the on-device copy have columns FORNITURA/PA_ASSOCIATI" (re-copy if not).
- `CatalogRepository.kt` — `searchByName` (`DenominazioneConfezione LIKE ?%`),
  `byAic` (`AIC = ?`), `byAics`; maps rows to `Medicine(aic, nome, formato, ditta,
  principioAttivo, paAssociati, forNatura)`.
- `SearchViewModel.kt` — routing: `<3` chars → alert; **all-digits → `byAic`**;
  else name prefix. Barcode: ML Kit → **6-char base-32 only** (`Barcode.kt`,
  alphabet `0123456789BCDFGHJKLMNPQRSTUVWXYZ`) → AIC → `byAic`. Miss → "Nessun risultato!".
- The old **EAN** path never existed; EAN-13 scans are silently dropped by the
  6-char length gate (that is why a Maalox pack scan already fails *before* the
  AIC lookup, and a 9-digit AIC typed with an `A`/`IT` prefix fails the
  all-digits test).

---

## 3. Verified data sources (what exists, what each contains)

All four were downloaded and grep-tested on 2026-09-10 for the Maalox case.

### 3A. AIFA Anagrafica dei farmaci — **the current drug source** ✅ keep

- **Page:** `https://www.aifa.gov.it/it/liste-dei-farmaci` (Liferay; the download
  links are server-rendered inside the page body)
- **Files (stable URLs, no date in name):**
  - `https://drive.aifa.gov.it/farmaci/confezioni_fornitura.csv` — "Anagrafica Farmaci AIFA"
  - `https://drive.aifa.gov.it/farmaci/PA_confezioni.csv` — active ingredients per pack
  - `https://drive.aifa.gov.it/farmaci/atc.csv` — ATC registry to 5th level
- **Update cadence:** "Giorno Precedente" = **daily**
- **Format:** 15 columns: `CODICE_AIC;COD_FARMACO;COD_CONFEZIONE;DENOMINAZIONE;DESCRIZIONE;CODICE_DITTA;RAGIONE_SOCIALE;STATO_AMMINISTRATIVO;TIPO_PROCEDURA;FORMA;CODICE_ATC;PA_ASSOCIATI;FORNITURA;LINK_FI;LINK_RCP`
- **Contents (2026-09-10 snapshot, 82 MB):** 159,901 rows, all `CODICE_AIC` unique;
  `STATO_AMMINISTRATIVO` = **Autorizzata 159,868 + Sospesa 33 only** — it is a
  *supply* list: revoked/withdrawn AICs are **not** present (a reclassified AIC
  disappears within a day of reclassification).
- **Maalox test:** 51 rows for other Maalox brands (MAALOX, MAALOX NAUSEA, MAALOX
  REFLUSSO, AIC `020702/033013/038856-58/041056/041417/044038/047458/047521`…);
  **`934480195` absent** (0 matches; also no `9344801*` at all).
- **Embedded API hint:** `LINK_FI`/`LINK_RCP` point at `https://api.aifa.gov.it/aifa-bdf-eif-be/1.0.0/…`.

**Verdict:** stays the drug backbone. It is **structurally unable** to contain
reclassified products — that is a data-model fact, not a defect.

### 3B. AIFA medicinali portal + REST API — on-demand lookup (optional enhancement)

- Portal: `https://medicinali.aifa.gov.it` (Angular `aifa_bds_esp_info_farm_fe`)
- Config: `/it/assets/config/config.json` → `baseUrl = https://api.aifa.gov.it/aifa-bdf-eif-be/1.0.0/`
  (+ `baseUrlCached = https://api.aifa.gov.it/bds-ms/`)
- Endpoints (extracted from the app bundle, probed live):
  - `GET {baseUrl}formadosaggio/ricerca?query=<text>` — full search, paged JSON
    (results: per pack `aic`, `statoAmministrativo`, `classeRimborsabilita`, carenza fields, `categoriaMedicinale`…)
  - `GET {baseUrl}autocomplete?query=<text>&nos=5` — search suggestions
  - `GET {baseUrl}formadosaggio/{id}?lang=it` — product detail
  - `GET {baseUrl}organizzazione/{orgId}/farmaci/{aic6}/stampati?ts=FI|RCP` — bugiardino/RCP PDF links
- **Maalox test:** `query=reflu` → 0 results; `query=reflurapid` → 0;
  `query=maalox` → 10 (all medicines). `autocomplete?query=934480195` → empty.
- **Verdict:** same coverage as the Anagrafica (medicines only) — useful for
  *runtime refresh* of a found AIC, useless for the device case.

### 3C. Mibact Repertorio dei Dispositivi Medici — **the missing device source** ✅ add

- **Page:** `https://www.dati.salute.gov.it/it/dataset/dispositivi-medici/`
  (Gatsby; metadata exposed at `https://www.dati.salute.gov.it/page-data/it/dataset/dispositivi-medici/page-data.json`
  — **use this to resolve the dated filename**, it changes weekly)
- **File (2026-09-07):** `https://www.dati.salute.gov.it/sites/default/files/opendata/DISPO_RDM_1_20260907_csv.zip`
  (57 MB → **520 MB** CSV; an 80 MB → 2.3 GB XML twin exists, **same 16 fields**)
- **Cadence:** weekly full snapshot (the file is the complete repertorio, not a delta)
- **Format (16 columns):**
  `tipologia_dm;progressivo_dm_ass;data_prima_pubblicazione;dm_riferimento;gruppo_dm_simili;iscrizione_repertorio;data_inizio_validita;data_fine_validita;fabbricante_assemblatore;cod_fiscale;PARTITAIVA_VATNUMBER_MAND;cod_catalogo_fabbr_ass;denominazione_commerciale;classificazione_cnd;descrizione_cnd;data_fine_commercio`
- **Scale:** 2,414,848 rows (2,413,098 currently valid, i.e. validity filtering
  removes almost nothing — the file is not trimmable by date).
  `iscrizione_repertorio`: S 1,721,759 / N 693,089.
  No EAN column, no legacy-AIC column. `classificazione_cnd` mixes short codes
  (`G99`, `V9004`, `A060299`) with full paths (`0301650209019`) + suffixes (`-MR`).
- **Maalox test — FOUND** (6 rows):
  | progressivo | registered | valid from | valid until / fine_commercio | maker (VAT) | CND | name |
  |---|---|---|---|---|---|---|
  | 1201069 | 2014-08-28 | 2016-02-17 | 2017-07-27 | BIOFARMA S.P.A. (00812680304) | `0301650209019-MR` | MAALOX REFLURAPID |
  | 1725310 | 2018-07-21 | 2018-07-21 | 2020-11-22 | BIOFARMA S.P.A. (02895910301) | `0301650209019-MR` | MAALox REFLURAPID |
  | **1963804** | 2020-06-28 | 2020-06-28 | **open** | BIOFARMA S.R.L. (02895910301) | `0301650209019-MR` | MAALOX REFLURAPID |
  | 1988119 | 2020-08-26 | 2020-08-26 | open | BIOFARMA S.R.L. | `GS - MAALOX RS` | MAALOX RS |
  | 1418425 | 2016-05-12 | 2016-05-12 | open | SIIT SRL (00820090157) | `FT025` | MAALOX EVOLUZIONE NATURA |
  | 2958174 | 2026-02-14 | 2026-02-14 | open | BIOFARMA S.R.L. | `G0401` | MAALOX REFLURAPID ADVANCE |

  (The `cod_fiscale`/VAT shown is the repertorio registrant; current marketer of
  the brand is Opella — the app should display the repertorio value verbatim and
  label it "registrante".)
- **CND-group sizes** (subset planning): `G99*` 1,651 rows (Maalox's group),
  `G0401` 262, all `G0*` 13,900. Top repertorio groups are hospital-grade
  (P0 594k, Q0 245k, L0 210k…) — a consumer subset is 1–2 orders of magnitude
  smaller than the file.
- **Verdict:** the **only official open data source containing the product as a
  device**. Cost: 520 MB raw / ~2.4 M rows if bundled whole.

### 3D. fogliettoillustrativo.net — third-party mirror (code-lookup glue, no bulk)

- Page: `https://fogliettoillustrativo.net/bugiardino/maalox-reflurapid-20bust-934480195`
  — image alt `…bugiardino cod: 934480195`; text: *"Maalox RefluRAPID è un
  dispositivo medico"*; footer: *"Dati regolatori dalla banca dati pubblica
  dell'AIFA; raccolta, elaborazione e aggiornamento a cura di FogliettoIllustrativo.net"*
  (last update 18/08/2026). Same for dpfarma.shop (`A934480195`).
- **Significance:** it is the **only readily inspectable source that still pairs
  the legacy AIC `934480195` with the reclassified product** — evidence that a
  legacy-AIC↔device bridge exists out there, but no bulk feed. Treat as
  scraping/curation source, not primary.

### 3E. Excluded / dead ends

- **data.gov.it CKAN** (`v1./v2.data.gov.it`) — unreachable in this environment
  (`getaddrinfo ENOTFOUND`); the Mibact dataset lives on `dati.salute.gov.it` anyway.
- **AIFA classe A/H lists** (files in repo) — superseded by the Anagrafica
  (subset of it, quarterly); keep only for historical reference, or drop.
- **Medicinali portal scraping** — covered by 3B API; no device data.

---

## 4. Gap analysis — why the Maalox case fails today

| # | Step in app | Outcome today |
|---|---|---|
| G1 | Scan pack → ML Kit reads EAN-13 `2005187210851` | 13 chars ≠ 6 → `scanner_bad_code` popup (EAN never considered) |
| G2 | Scan the 6-char CIP code → `VV62B3` → `934480195` → `byAic` | 0 rows → "Nessun risultato!" (AIC left the drug list) |
| G3 | Type `934480195` | all-digits → `byAic` → 0 rows |
| G4 | Type `A934480195` / `IT934480195` | not all-digits → name prefix `A93448…%` → 0 rows |
| G5 | Type `reflu` / `reflu rapid` | name prefix on `DenominazioneConfezione` → 0 rows (drug list has `MAALOX*` names only; the *device* name `MAALOX REFLURAPID` is in the Mibact file, not imported) |
| G6 | Type `maalox` | 51 rows, all *other* Maalox medicines — user can't tell the box's product is absent |
| G7 | UI model `Medicine` is drug-shaped (AIC, PA, fornitura) | nowhere to render a device's CND / registrant / "dispositivo medico" fact |

Root cause: **single drug-shaped catalog + three unhandled key formats**
(EAN, A/IT prefix, and the missing device corpus).

---

## 5. Refactoring plan

### 5.1 Data pipeline (`farmamia-data/`)

**New: `scripts/download_sources.py`** (or .sh) — one command refreshes everything:

1. GET the 3 AIFA files from `drive.aifa.gov.it/farmaci/` (names stable; no date)
   → write `confezioni_fornitura.csv`, `PA_confezioni.csv`, `atc.csv` with a
   `SOURCE_INFO` sidecar (url, http last-modified, row count, fetched-at).
2. GET `https://www.dati.salute.gov.it/page-data/it/dataset/dispositivi-medici/page-data.json`
   → read `relationships.field_listafile[0].url` (dated filename) → GET the zip →
   unzip to `dispo_rdm_<date>.csv` (keep only the 2 latest).
3. Validate each file: expected header row, row-count delta sanity vs previous,
   spot-check that a known AIC (e.g. `020702`) still resolves.
4. Print a freshness table (days since fetch per source).

**New: `scripts/build_catalog.py`** — replaces/extends `import_csv_to_db.py`
(single importer, all tables, into one `farmamia_ref.db`):

| Table | Source | Filter | Est. rows | Est. SQLite size |
|---|---|---|---|---|
| `med_classea` (keep name for compat) | Anagrafica CSV | as today (all Autorizzata+Sospesa) | 159,901 | ~160 MB (unchanged) |
| `dispositivi_medici` (new) | DISPO CSV | **consumer CND subset** — config-driven allow-list of CND prefixes (start: `G99`, `G04`, `G0` groups; extend with B/A/H consumer classes after review); keep only rows currently valid (`data_fine_validita = 9999-12-31` and no `data_fine_commercio`) | ~15–30k | **≤ 10 MB** |
| `codici_legacy` (new) | curated + auto | reclassified products: `legacy_aic TEXT PK, name, device_progressivo, cnd, source` | seed with Maalox Reflu Rapid 934480195; grow by joining DISPO rows against a growing list of ex-AICs (or by scraping fogliettoillustrativo for `cod: NNNNNNNNN`) | tens–hundreds |
| `catalog_meta` (new) | importer | one row: `key, value` — source URLs, fetched-at, row counts, schema version | 8 | ~1 KB |

Column choices:

- `dispositivi_medici`: all 16 repertorio columns verbatim + derived
  `denominazione_commerciale_upper` (indexed, for prefix search) + composite
  unique `(progressivo_dm_ass, data_inizio_validita)`.
- Legacy AIC column types: keep `AIC`/`CODICE_AIC` **TEXT** (leading-zero AICs
  like `000367045` break NUMERIC affinity). `byAic` must compare textually.

Size budget: current asset 160 MB + ≤ 10 MB ≈ **170 MB** — still under the
200 MB single-APK Play limit, but leaves little headroom; the big lever remains
`med_classea` (see 5.3, option B).

### 5.2 App changes (`../farmamia`)

1. **`CatalogDb.kt`**
   - Register `dispositivi_medici`, `codici_legacy`, `catalog_meta`.
   - Replace column-sniffing staleness check with **`catalog_meta.schema_version`**
     (importer stamps N; on-device < N → re-copy). Column-sniffing is brittle
     (it only knows two legacy columns).
   - Expose `dbMeta()` for the Settings "data sources" screen.
2. **`CatalogRepository.kt`** — one search path over both tables:
   ```
   fun search(term: String): List<CatalogEntry>   // unified
     - normalize: trim; strip leading "A"/"IT" before digits; uppercase
   fun byCode(code: String): List<CatalogEntry>   // single entry point
     1. code is 9 digits → med_classea.AIC (text compare)
     2. same code → codici_legacy.legacy_aic → resolve dispositivi_medici row
     3. code matches CND/Mibact progressivo pattern → dispositivi_medici
   fun byEan(ean13: String): List<CatalogEntry>   // reserved; empty for now,
     // implemented via codici_legacy.ean once a source provides EANs
   ```
   Name search = `UNION` of the two prefix searches, capped (e.g. 50), interleaved.
   Device name search uses `denominazione_commerciale_upper LIKE ?%`.
3. **Model** — replace the drug-shaped `Medicine` as the *search result* type with:
   ```kotlin
   sealed interface CatalogEntry { val nome: String; val codice: String }
   data class MedicineEntry(...) : CatalogEntry   // existing Medicine fields
   data class DeviceEntry(
     val nome: String,            // denominazione_commerciale
     val formato: String = "",    // "" (repertorio has no pack size)
     val ditta: String,           // fabbricante_assemblatore ("registrante")
     val cnd: String, val cndDesc: String, val progressivo: String,
     val inizioValidita: String, val fineCommercio: String?,
   ) : CatalogEntry               // codice = legacy_aic if present else CND
   ```
   Cabinet/promemoria storage: the `aic` column becomes **`codice` TEXT + `tipo`
   (`MEDICINALE`/`DISPOSITIVO`)**; calendar-event notes print the right code.
   (Room user-DB migration: `aic`→`codice` is additive-safe: keep `aic` for
   medicine rows, add `codice`+`tipo` columns with backfill.)
4. **`SearchViewModel.kt`** routing changes:
   - all-digits length 9 → `byCode` (not only `byAic`)
   - all-digits length 13 → treat as EAN → `byEan` (today silently "bad code")
   - 6-char base-32 → unchanged → AIC → `byCode`
   - text ≥ 3 chars → unified `search` (drug ∪ device)
   - "Nessun risultato!" stays; add a second-line hint when the query looked
     like a code: "Il codice può riferirsi a un dispositivo medico."
5. **UI** — result row + insert sheet: device badge "Dispositivo medico"
   (icon from `icons/`), detail shows CND code + registrant + validity window;
   expiry-scan (ExpiryScannerView) unchanged.
6. **Settings** — new "Dati" section: per-source fetched-at + row counts +
   schema version (from `catalog_meta`) + attribution
   ("AIFA — drive.aifa.gov.it; Ministero della Salute — dati.salute.gov.it").
7. **`Barcode.kt`** — no algorithm change; length policy becomes
   `{6 → base32; 9 → AIC; 13 → EAN}` with per-type fallback messages.

### 5.3 Size options for the drug table (decide explicitly)

| Option                                                                                                                                     | Asset | Trade-off |
|--------------------------------------------------------------------------------------------------------------------------------------------|---|---|
| A. keep 159,901 rows (today)                                                                                                               | ~160–170 MB | full coverage incl. Sospesa; APK near Play 200 MB limit |
| B. drop `Sospesa` (33 rows) — no gain                                                                                                      | same | cosmetic only |
| C. **subset to supplies actually sold**: keep only rows whose `FORNI]escrizione medica" (OTC) + classe A/H — the *original* 2016 app scope | ~5–8 MB | matches old app's 19,570-row spirit; prescription-only products unfindable (they need a search → API path 3B) |
| D. split: bundle C, fetch rest from AIFA API on demand                                                                                     | ~10 MB + network | best of both; needs the 3B endpoint + offline-tolerance UI state |

Recommendation: **C or D** once the device tables exist, to keep total asset
≤ ~20 MB. (A is acceptable short-term; D is the end-state.)

---

## 6. Risks / open items

1. **Legacy-AIC → device bridge is not officially published.** Mibact rows carry
   CND (e.g. `0301650209019-MR`) but never `934480195`. `codici_legacy` must be
   built from third-party pages (fogliettoillustrativo/dpfarma) or manual entry.
   → Decide curation process (quarterly review of new reclassifications; AIFA
   publishes reclassification *provvedimenti* — their PDF list is the clean
   authoritative trigger source, worth parsing).
2. **Mibact weekly snapshot is a full 2.4 M-row file** (57 MB zip). CI cost is
   fine; on-device we never store it whole.
3. **CND codes are heterogeneous** (short `G99` vs full `03.01.65.02.09.019` +
   `-MR`). Index/compare on the raw string; never parse dots.
4. **`fabbricante_assemblatore` = registrant, not always marketer** (Maalox shows
   BIOFARMA). Label it "registrante" in UI to avoid asserting wrong ownership.
5. **cp1252 legacy CSVs** (`Classe_A/H`) — if kept at all, importer must open
   them with that encoding or `€` headers break (byte 0x80).
6. **Play 200 MB limit** — option A + devices leaves ~30 MB headroom only.
7. **Stale-copy check** (5.2.1) — the current column-sniff treats *any* on-device
   copy lacking FORNITURA as stale; with a version stamp, a user who disabled
   re-copy debug will still upgrade cleanly.
8. `PA_confezioni.csv` (338k rows) / `atc.csv` (7.2k) unused — wire PA into the
   detail sheet ("principi attivi + dosaggio") or drop from the pipeline to
   save 12 MB of fetch time.

---

## 7. Acceptance tests (Maalox Reflu Rapid)

After refactor, all of these must return the product (device badge):

| Input | Path |
|---|---|
| scan EAN-13 `2005187210851` | `byEan` → `codici_legacy`/device row |
| scan CIP 6-char `VV62B3` | base32 → `934480195` → `byCode` → device |
| type `934480195` | `byCode` |
| type `A934480195`, `IT934480195` | normalized → `byCode` |
| type `reflu` / `reflu rapid` | device name prefix |
| type `maalox` | 51 medicine rows **+** the device row, badge distinguishes |

Plus: `catalog_meta` shows both sources < 8 days old on a fresh install;
asset size ≤ budget of the chosen 5.3 option.

---

## 8. Implementation outcome (2026-09-12) — plan executed, all green

Option D was implemented in full (bundled subset + on-demand AIFA API +
Mibact device table + legacy bridge). Summary of what landed in `../farmamia`:

### 8.1 New reference asset

`app/src/main/assets/farmamia_ref.db` — **97.7 MB** (was 160 MB single-table):

| Table | Rows | Key |
|---|---:|---|
| `med_classea` | 91,603 | AIC (OTC 78,608 + Classe A/H 13,175; C/RNR → API fallback) |
| `dispositivi_medici` | 15,812 | progressivo / CND (consumer CND subset of Mibact DISPO, G99+G0*) |
| `codici_legacy` | 1 | legacy_aic → progressivo (Maalox: 934480195 → 1963804) |
| `catalog_meta` | 9 | source URLs + fetched-at timestamps (drives staleness check) |

### 8.2 App-side changes (key files)

- **`model/Medicine.kt`** — single `Medicine` with `tipo: TipoSanitario`
  (MEDICINALE / DISPOSITIVO_MEDICO) + `codiceRiferimento` (AIC or CND).
- **`util/CatalogCode.kt`** — normalizer: 9-digit AIC, legacy AIC, A/IT-prefixed
  text, 6-char base-32 CIP, 13-digit EAN-13 → `CatalogCode` sealed type.
- **`data/CatalogDb.kt` + `CatalogRepository.kt`** — opens the asset DB
  read-only (copy-on-first-launch), exposes `byCode(CatalogCode)`, `byEan()`,
  `search(query)` (name prefix + AIC + CND), staleness via `catalog_meta`.
- **`api/AifaApi.kt` + `data/AifaRepository.kt`** — Retrofit on-demand fallback
  (`formadosaggio/ricerca`, `autocomplete`) for codes not in the bundle
  (C/RNR, new AICs). `PlacesRepository` also hardened: `IOException` →
  empty result (no network = no crash).
- **`ui/vm/SearchViewModel.kt` / `SearchScreen.kt` / `InsertMedicineSheet.kt`** —
  catalog-first search with API fallback; device rows render with a
  DISPOSITIVO badge; both Maalox paths (EAN + CIP) resolve to the device card.
- **Cabinet (Room `farmamia_user.db`)** — v2 with `tipo` column
  (String + enum mapping) and an explicit `Migration(1,2)`; `ArchivioViewModel`
  switched to `byCode` so re-scanning a bridged device re-resolves.

### 8.3 Test suite — final state

`./gradlew :app:connectedDebugAndroidTest` (AVD `Medium_Phone_API_36.1`):

- **30/30 instrumented tests PASS** (6 classes: Archivio, Search, Promemoria,
  Storico, Guide, DataLayer).
- `:app:testDebugUnitTest` PASS (incl. `CatalogCodeTest` round-trips).
- `:app:assembleDebug` → 98.1 MB APK (well under the 200 MB Play limit).

### 8.4 Test-infrastructure lessons (cost several hours, kept for the record)

1. **First-launch guide auto-start** (2.1 s after composition) swallows taps and
   auto-navigates tabs. Every UI test class now writes
   `SettingsRepository.setGuideSeen(true)` in `reset()` **before** the test body;
   a `dismissGuideIfVisible()` backstop (tag-based `guide_skip` tap) covers
   races. **Lesson:** a delegated `by preferencesDataStore(...)` property is
   evaluated once — one shared DataStore per process; but the *flag write* must
   happen per test class, not "somewhere in setup".
2. **Long press in Compose UI tests:** an injected DOWN→(silence)→UP on a
   static screen is read as a *tap*. `TapGestureDetector.waitForLongPress`
   relies on `withTimeout` over pointer events, and on a quiescent screen the
   Choreographer produces no frames between DOWN and UP, so the timeout never
   elapses in the event-time domain. The framework's `TouchInjectionScope
   .longClick()` (a zero-distance swipe that advances event time past
   `longPressTimeoutMillis`) is the reliable primitive — `performTouchInput {
   longClick() }` fires `onLongClick` deterministically. Manual `MotionEvent`
   dispatch with jitter did **not** work.
3. **Dialog/sheet nodes live in separate windows**: `touchBoundsInRoot`
   coordinates there are window-local; dispatching a `MotionEvent` to the main
   `decorView` misses them. Use semantics `performClick()` (which routes to the
   right window) for `ModalBottomSheet`/`Dialog` content; coordinate taps are
   fine for main-window elements.
4. **`Event.Alert` popups are emitted after async work completes** (e.g. the
   calendar insert): `waitForIdle()` does not wait for ViewModel coroutines —
   poll for the alert text instead of asserting immediately.
5. Emulator here has **no outbound network**: network-dependent tests must
   either skip (`assumeTrue`) or the repositories must degrade gracefully.

---

## 9. Second trigger case — Iridil gocce oculari (2026-09-13)

User box "iridil gocce oculari rinfrescanti e lenitive" (Montefarmaco OTC,
Via IV Novembre 92, Bollate MI) found by **neither** the scanner (EAN
`8058363613735`) **nor** manual name search. Same class of bug as Maalox,
deeper:

### Root cause (verified against the 2026-09-07 repertory)

- Iridil is **not a drug**: absent from the 2026 AIFA
  `confezioni_fornitura` registro (0 rows) and from the Classe A/H lists.
  It is a **CE medical device** reclassified out of the drug registry
  (current inscribed repertory entry since 2023-09-13).
- Mibact DISPO carries it in CND **Q0299** ("ocular - altri") and
  **Q0203** ("ocular lubricants"), **not** in the consumer G0*/G9* subset
  the app ships → `dispositivi_medici` had no Iridil row.
- `codici_legacy` had only the Maalox row → no EAN bridge → `byEan()`
  matched nothing.
- Its "9-digit codes" are **Minsan** numbers (931468021 / 900031840), not
  AIC registry numbers (neither is in the registro), so no `byCode()` path
  either, and the AIFA API (drug repertory) doesn't know it.

### Fix (landed 2026-09-13)

1. `curated/codici_legacy.csv` — two Iridil bridge rows (EAN only, Minsan in
   the note: Minsan can theoretically collide with a real AIC):
   - `8058363613735 → 2022093` (IRIDIL PLURIDOSE, 10 mL — the user's box)
   - `8004995453003 → 1820787` (10 monodose 0,5 mL containers)
2. `scripts/build_catalog.py` — `DEVICE_CND_PREFIXES` gains **Q0203**
   (ocular lubricants, ~3.1k rows) and **Q0299** ("ocular - altri", ~6.3k
   rows); curated bridge rows ride along regardless of CND (existing
   behaviour). `SCHEMA_VERSION` 1 → 2 so installed copies re-copy the asset.
3. `CatalogDb.kt` — `EXPECTED_SCHEMA_VERSION` 1 → 2.

Rebuild: asset **100.9 MB** (was 97.7), `dispositivi_medici` **25,268**
(was 15,812), `codici_legacy` 3. Verified with the app's exact SQL:
`byEan(8058363613735)` → IRIDIL PLURIDOSE, `byEan(8004995453003)` →
monodose entry, name prefix `IRIDIL%` → 5 rows (both current + retired
variants), Maalox bridge unaffected. Full suite 30/30 green.

**General rule for future reclassified-OTC misses:** check Mibact DISPO by
name first (`classificazione_cnd` will tell the group), then either widen a
CND prefix (product family worth keeping) or add a curated EAN bridge row
(one-off product). The scanner needs the **bridge** (byEan joins on
`codici_legacy.ean`); name search only needs the row in `dispositivi_medici`.

---

## 10. Consumer-coverage sweep — schema 3 (2026-09-13)

Follow-up to §9: is the catalog good for *every* Maalox/Iridil-class case
(ex-OTC drugs and everyday pharmacy devices that users search or scan)?

### Method

1. Swept currently-valid DISPO rows whose name starts with a brand token of
   the 2026 drug registro (11,258 leading tokens) → 989 rows: real consumer
   families + hospital noise (lab reagents, dental, gases).
2. Re-checked the 1,619-product delisted-drug cohort (foglioillustrativo
   pages) against current DISPO → only 7 matched: **not** a reliable
   enumeration of reclassified products; category coverage is the control.
3. Surveyed CND group descriptions + row counts for every 2-char group;
   sized candidate consumer groups.

### DEVICE_CND_PREFIXES (18, `startswith` match)

| Group | CND | rows (valid) | why in / out |
|---|---|---|---|
| GI | G0, G9 | ~15.8k | Maalox class, glycerine/senna/paraffin (G99) |
| Ocular | Q0203, Q0299 | ~9.5k | Iridil class, artificial tears |
| ENT | Q0301, Q0304, Q0399 | ~4.5k | nasal sprays/irrigation, ear drops, snoring, FLUIMUCIL HERBAL; **Q0303 surgical blades out** |
| Skin | M9 | ~4.7k | cryo packs, sprays, gels (AXOL, DEXERYL, wart pens) |
| Wound | M04, M05 | ~17.9k | dressings/plasters/antiseptics (INADINE, J&J, Scholl, Compeed herpes); **M03 compression out** |
| Disinfectants | D9 | ~1.8k | home antiseptics |
| Suture strips | H9001 | ~434 | |
| Vaginal | U0803, U0899 | ~1.8k | SAUGELLA class |
| Condoms | U1101 | ~524 | Durex — cheap, avoids the obvious miss |
| Lice | V9010 | ~178 | HEDRIN, NOPID, BIOSCALIN pidok |
| Oral/enema | V9017, V9002 | ~259 | |

**Deliberately out** (non-dosable or hospital, or too mixed): Q0 dental,
Y0 insoles (Scholl feet), Y2 lenses, M03 compression, **V9099 catch-all
(~12k, too mixed)** — the brands that land there (BIOSCALIN shampoo, Saugella
lavanda) get individual bridge rows instead. R/F/U0 hospital lines, W lab, Z
gases.

**Result:** asset **111.5 MB**, `dispositivi_medici` **57,422** (was
25,268 in v2 / 15,812 in v1), `codici_legacy` **9** rows, schema **3**
(`CatalogDb.EXPECTED_SCHEMA_VERSION` 2 → 3 forces re-copy on installed
copies). Suite 30/30 green.

### Bridge rows (9) — every reclassified famous brand

| code (legacy_aic / ean) | → progressivo | product |
|---|---|---|
| 934480195 / 2005187210851 (+8000229115551) | 1963804 | MAALOX REFLURAPID 20×10 ml |
| — / 8058363613735 | 2022093 | IRIDIL pluridose 10 ml |
| — / 8004995453003 | 1820787 | IRIDIL monodose 10×0,5 ml |
| — / 8055732260019, 927170555 | 959205 | HEDRIN rapido gel |
| 938145265 | 297914 | BIOSCALIN PidoK.O. NEO 75 ml |
| 900141351 | 1615201 | SAUGELLA gel monodose 5×6 ml |
| 979605363 | 1742645 | COMPEED cerotti vesciche x5 |

All Minsans verified non-colliding with the 159,901 current AICs of the
2026 registro before being used as `legacy_aic` (Iridil keeps Minsans in
the note only — same collision rule, applied per-code).

### Verified end-to-end (asset SQL = app SQL)

- `byEan`: Maalox (both codes), Iridil box/mono, HEDRIN ✓
- `byCode`: 934480195, 927170555, 938145265, 900141351, 979605363 ✓
- name search: MACROLAX, SAUGELLA, INADINE, JALMA, FLUIMUCIL HERBAL, AXOL,
  DUREX, SCHOLL, NOPID, BIOSCALIN, SELLALAX, MASTER-AID, COMPEED, IRIDIL,
  MAALOX — all hit ✓

### Residual gaps (accepted)

- Scanner still needs a bridge row per product (EAN→device join is
curated). New categories fix **name search** for the whole family;
  scanning a specific box of a brand without a bridge → "not found,
  rescan" and the user falls back to name search.
- V9099 catch-all and other non-dosable groups excluded by design (asset
  size, noise); one-offs handled per-case via bridge rows.
- DISPO repertory lags new registrations (e.g. HEDRIN 200 ml not yet
  inscribed) — same staleness as every weekly-crawled source.

---

## Appendix — exact verified URLs

```
# AIFA (drugs) — stable names, daily
https://drive.aifa.gov.it/farmaci/confezioni_fornitura.csv
https://drive.aifa.gov.it/farmaci/PA_confezioni.csv
https://drive.aifa.gov.it/farmaci/atc.csv
https://www.aifa.gov.it/it/liste-dei-farmaci            # metadata page

# AIFA (drugs) — REST, on-demand
https://api.aifa.gov.it/aifa-bdf-eif-be/1.0.0/formadosaggio/ricerca?query=
https://api.aifa.gov.it/aifa-bdf-eif-be/1.0.0/autocomplete?query=&nos=5
https://api.aifa.gov.it/aifa-bdf-eif-be/1.0.0/formadosaggio/{id}?lang=it
https://api.aifa.gov.it/aifa-bdf-eif-be/1.0.0/organizzazione/{org}/farmaci/{aic6}/stampati?ts=FI|RCP
config: https://medicinali.aifa.gov.it/it/assets/config/config.json

# Mibact (devices) — weekly, dated filename via page-data.json
https://www.dati.salute.gov.it/page-data/it/dataset/dispositivi-medici/page-data.json
https://www.dati.salute.gov.it/sites/default/files/opendata/DISPO_RDM_1_20260907_csv.zip   # 57 MB → 520 MB
(page: https://www.dati.salute.gov.it/it/dataset/dispositivi-medici/)

# Third-party mirror (code→product glue, no bulk)
https://fogliettoillustrativo.net/bugiardino/maalox-reflurapid-20bust-934480195
```
