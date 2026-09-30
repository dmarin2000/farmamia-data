# Shortlist — deploy (GitHub Pages edition)

Repo: `farmamia-data` (questo). Remote esiste già: `dmarin2000/farmamia-data` (public).

## 1. GitHub — 1 repo, 1 secret opzionale

- Il remote `origin` è già configurato e `main` è già pushato.
- **Nessun secret obbligatorio.** Opzionale: `MIBACT_PROXY` — l'host Mibact DISPO
  (`www.dati.salute.gov.it`) è una scatola TLS1.2-only che a volte rifiuta l'egress dei
  runner GitHub; se anche la fallback curl fallisce, un proxy con egress residenziale/IT
  lo fa passare. Senza: il run fallisce a fetch e pubblica nulla (fail-closed, l'ultima
  release resta live).
- Pipeline = stdlib Python 3.11, nessuna dipendenza da installare.

## 2. Host catalogo = GitHub Pages (sostituisce Cloudflare R2 + Worker)

Zero account in più, zero carta di credito, zero bucket, zero costo egress.

- **Setup una tantum (manuale, ~30s):** repo → **Settings → Pages → Build and
  deployment → Source: "Deploy from a branch"** → branch `gh-pages`, cartella `/ (root)`.
- **URL stabile:** `https://dmarin2000.github.io/farmamia-data/` (dipende solo dal tuo
  account GitHub — non occupabile, non in scadenza).
- Il workflow pusha su `gh-pages` (branch singolo-commit, force-push ad ogni publish:
  dimensione costante, niente history bloat) solo `manifest.json` + l'overlay corrente
  (~200 KB).
- Note tecniche:
  - `github.io` invia un fisso `Cache-Control: max-age=600` (non personalizzabile) —
    inoffensivo: i file sono content-addressed e l'app verifica sha256; worst case il
    manifest ha ~10 min di latenza.
  - CORS: irrilevante (app nativa, non browser).
  - Limite push git **100 MB/file** → il baseline (~124 MB) non può stare qui: lo
    gestisce il passo 3.

## 3. Baseline = GitHub Release (sostituisce il bucket)

- Il workflow carica il baseline come **release asset**, tag content-addressed
  `baseline-<sha12>` (es. `baseline-894124ba9081`), **solo quando lo sha cambia**
  (primo publish + re-bake manuale).
- Limiti verificati (GitHub docs): asset < **2 GiB**, **nessun limite di banda** sui
  download delle release. 124 MB ci sta.
- URL nel manifest:
  `https://github.com/dmarin2000/farmamia-data/releases/download/baseline-<sha12>/<sha12>.db`
- **Perché il baseline è stabile (fix pipeline):** l'overlay è la diff **cumulativa**
  contro l'ultimo baseline pubblicato (quello che l'app tiene in locale). Il vecchio
  workflow ripubblicava il *candidate* come baseline a ogni giorno di contenuto → ogni
  app scaricava 124 MB + overlay ogni giorno. Ora: il baseline cambia solo a re-bake;
  nei giorni di contenuto si pubblica solo l'overlay (KB).
- **Re-bake** (schema bump / periodico, Q3): Actions → *catalog-pipeline* →
  *Run workflow* → spunta **rebase** → il candidate diventa il nuovo baseline.
  Si fa in lockstep con un nuovo APK (Q5: l'app ignora manifest con schema diverso).

## 4. App: una riga

`app/src/main/java/it/dmarin/farmamia/data/CatalogRemote.kt`:

```kotlin
const val DEFAULT_BASE_URL = "https://dmarin2000.github.io/farmamia-data"
```

- Nessuna crepa per Worker, nessun secret, nessun nome da riservare (github.io è
  account-bound).
- I test emulatore restano parametrizzati su `10.0.2.2:8080` (locale), nessuna
  dipendenza dal default.
- Nota: il baseline bundled nell'APK (sha `894124ba9081…`) è lo stesso del primo
  publish → al primo avvio l'app scarica solo l'overlay, non i 124 MB.

## 5. Primi run veri

1. Push di `main` (questo commit) → le modifiche al workflow sono attive.
2. Setup Pages (passo 2, una tantum).
3. Actions → **catalog-pipeline → Run workflow** (oppure aspetta il cron 06:00 UTC).
   - Run 1 (nessun manifest su Pages): "first publish" → baseline = candidate
     (release `baseline-<sha12>` creata una sola volta), overlay corrente su `gh-pages`.
   - Run N (06:00 UTC, cron): se i sorgenti AIFA sono invariati → diff exit 42 →
     "no content change, nothing published" (run green, nessun push); se ci sono
     novità → nuovo overlay (KB) su Pages, baseline intoccato.
4. Verifica manuale: `curl https://dmarin2000.github.io/farmamia-data/manifest.json`
   dopo ~2 min dal push.
5. Nota edge: un run manuale entro ~2 min da un publish (Pages non ancora deployato)
   legge "no manifest" → path di first publish; auto-ripara (content-addressed,
   stessi sha, nessuna release duplicata).

## Gotchas

- Repo **public** = minuti Actions illimitati e gratis; privato = 2000 min/mese
  (un run ≈ 10 min).
- Le URL sorgenti AIFA/Mibact sono state probe da questa macchina; se il WAF di AIFA
  403 il cron, il workflow fallisce chiuso e l'ultima release resta live (by design).
- I sorgenti (csv ~500 MB) restano local/giornalizzati in `sources/` (gitignored):
  non entrano mai nel git — l'incidente del push >100 MB era dovuto a un ramo
  temporaneo che li aveva tracciati (branch eliminato, `.gitignore` riattivo su `main`).

## Alternative: bucket S3 + Worker (se Pages non bastasse)

Se un giorno serve hosting non-GitHub (es. banda/latenza, o baseline > 2 GiB):

- **Backblaze B2** (miglior bucket): 10 GB free, no carta, bucket pubblico =
  unsigned GET diretto, egress free fino a 3× lo storage.
- **Cubbit DS3 Composer**: 3 TB free, no carta, endpoint `https://s3.cubbit.eu`,
  region `eu-west-1`; letture anonime pubbliche verificate live il 2026-09-24
  (bucket `cubbit-public`, 200 sia su `s3.cubbit.eu` che virtual-host).
  Attivazione via form (non self-serve istantaneo).
- **Cloudflare R2**: 10 GB free ma richiede account Cloudflare (c/o bloccato).
- `publish.py --upload` funziona già con qualsiasi endpoint S3: env
  `R2_ENDPOINT` + `R2_ACCESS_KEY_ID` + `R2_SECRET_ACCESS_KEY` + `R2_BUCKET` +
  `S3_REGION` (`auto` per R2, `eu-west-1` per Cubbit). Il Worker di pass-through
  resterebbe un fetch su URL pubblici del bucket.
