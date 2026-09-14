#!/usr/bin/env python3
"""
Build farmamia_ref.db, the SQLite asset the app ships.

Tables
  med_classea         same 20-column schema as the old import_csv_to_db.py
                      (the app's CatalogRepository reads it by column name,
                      so the schema must stay byte-compatible)
  dispositivi_medici  Mibact DISPO repertorio, consumer CND subset (see
                      DEVICE_CND_PREFIXES: GI + ocular + ENT + skin/wound
                      care + vaginal + lice/oral/enema dosable groups)
  codici_legacy       curated bridge: legacy AIC / EAN -> Mibact progressivo
                      (curated/codici_legacy.csv — hand-maintained, see the
                      REPORT for the Maalox provenance)
  pa_confezioni       AIFA PA-per-confezione (CODICE_AIC, PRINCIPIO_ATTIVO,
                      QUANTITA, UNITA_MISURA); N.D. rows filtered; 4-tuple
                      is unique in the source (verified 2026-09)
  catalog_meta        key/value provenance; schema_version drives the app's
                      staleness check (CatalogDb.kt), content_version the
                      overlay chain (PLAN-update-strategy §5)
  archivio, promemoria  legacy parity with the 2016 db (kept, empty)

Bundle policy for med_classea (--meds-mode, option D of the REPORT):
  otc_ah   (default)  OTC rows (FORNITURA "non soggetti a prescrizione")
                      + AICs from the AIFA Classe A / Classe H lists
  ah_only               only Classe A / Classe H AICs (smallest asset)
  full                  every registro row (the old 160 MB behaviour)

Inputs (default: ./sources/, else the repo-root copies this project already
has lying around):
  aifa registro CSV  (CODICE_AIC header)
  Classe_A/H CSVs    (cp1252)
  Mibact DISPO CSV   (tipologia_dm header)
  AIFA PA CSV        (CODICE_AIC header; PA per confezione)

Content provenance (plan §6): --content-version N is written to
 catalog_meta together with per-source date/sha256 pulled from
 sources/.manifest.json — this is what diff_catalog.py and the app's
 "dati aggiornati al…" read.

Usage:
  python3 build_catalog.py
  python3 build_catalog.py --meds-mode ah_only
  python3 build_catalog.py --db /tmp/x.db --registro /path/reg.csv --dispo /path/d.csv
  python3 build_catalog.py --content-version 7   # monotonic content build number
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
SOURCES = os.path.join(ROOT, "sources")
DEFAULT_DB = os.path.join(ROOT, "..", "farmamia", "app", "src", "main",
                          "assets", "farmamia_ref.db")
CURATED_LEGACY = os.path.join(ROOT, "curated", "codici_legacy.csv")

SCHEMA_VERSION = 4  # 4: + pa_confezioni table (PLAN-update-strategy P1)

# Consumer CND groups (2-char or 5-char prefixes, matched with startswith).
# The full DISPO repertory is 2.4M rows; the app ships the consumer subset:
#  G0/G9    GI incl. G99 (Maalox, glycerine suppositories, senna, paraffin)
#  Q0203/Q0299 ocular (Iridil & co)
#  Q0301/Q0304/Q0399 ENT consumables (nasal sprays, nasal/ear irrigation,
#          snoring sprays, FLUIMUCIL HERBAL; excludes Q0303 surgical blades)
#  M9       cryo/sprays/gels/dressings (ice packs, AXOL, DEXERYL, INADINE-co)
#  M04/M05  wound care: dressings, plasters, antiseptics, wart pens (J&J,
#          Scholl, Compeed herpes; excludes compression M03 and insoles Y0)
#  D9       antiseptics/disinfectants (1.8k)
#  H9001    suture strips (434)
#  U0803/U0899 vaginal solutions/creams/ovules (SAUGELLA & co)
#  U1101    condoms (Durex & co, 524)
#  V9010    lice treatments (HEDRIN, NOPID, BIOSCALIN pidok)
#  V9017    oral preparations (161)
#  V9002    enemas (98)
# Deliberately excluded (non-dosable or hospital): Q0 dental, Y0 insoles,
# Y2 lenses, M03 compression, V9099 catch-all (11.9k, too mixed), R/F/U0
# hospital lines, W lab, Z gases. Delisted-ex-drug brands that fall outside
# ride along via curated codici_legacy bridge rows.
DEVICE_CND_PREFIXES = (
    "G0", "G9",
    "Q0203", "Q0299",
    "Q0301", "Q0304", "Q0399",
    "M9", "M04", "M05",
    "D9", "H9001",
    "U0803", "U0899", "U1101",
    "V9010", "V9017", "V9002",
)

OTC_FORNITURA_PREFIX = "Medicinali non soggetti a prescrizione"

# 16 Mibact DISPO columns, verbatim (order as published).
DISPO_COLS = [
    "tipologia_dm", "progressivo_dm_ass", "data_prima_pubblicazione",
    "dm_riferimento", "gruppo_dm_simili", "iscrizione_repertorio",
    "data_inizio_validita", "data_fine_validita", "fabbricante_assemblatore",
    "cod_fiscale", "PARTITAIVA_VATNUMBER_MAND", "cod_catalogo_fabbr_ass",
    "denominazione_commerciale", "classificazione_cnd", "descrizione_cnd",
    "data_fine_commercio",
]


def find_input(name: str, hints: list[str]) -> str:
    """Newest file in ./sources/ or the repo root matching a hint (fnmatch)."""
    import fnmatch
    for base in (SOURCES, ROOT):
        if not os.path.isdir(base):
            continue
        for hint in hints:
            cands = sorted(f for f in os.listdir(base)
                           if fnmatch.fnmatch(f, hint) and not f.startswith("."))
            if cands:
                return os.path.join(base, cands[-1])
    raise SystemExit(f"ERROR: cannot find input {name!r} in {SOURCES} or {ROOT}; "
                     f"run scripts/download_sources.py first")


def is_current_dispo(row: dict, today: str, bridge: set[str]) -> bool:
    if row["progressivo_dm_ass"].strip() in bridge:
        return True  # curated bridge rows always ride along
    end = (row.get("data_fine_validita") or "").strip()
    if end in ("", "0000-00-00"):
        return True
    return end >= today


def read_med_rows(path: str, mode: str, a_ah_aics: set[str],
                  otc_only: bool) -> list[tuple]:
    """Yield the 20 med_classea column tuples for the bundle policy."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter=";"):
            aic = (r.get("CODICE_AIC") or "").strip()
            if not aic:
                continue
            forNatura = (r.get("FORNITURA") or "").strip()
            if mode == "full":
                pass
            elif mode == "ah_only":
                if aic not in a_ah_aics:
                    continue
            else:  # otc_ah
                if not forNatura.startswith(OTC_FORNITURA_PREFIX) and aic not in a_ah_aics:
                    continue

            den = (r.get("DENOMINAZIONE") or "").strip()
            desc = (r.get("DESCRIZIONE") or "").strip()
            if desc:
                den_conf = f"{den}*{desc}"
                desc_grp = f"{den} {desc}"
            else:
                den_conf = den
                desc_grp = den
            pa = (r.get("PA_ASSOCIATI") or "").strip()
            rows.append((
                pa,                                   # PrincipioAttivo
                desc_grp,                             # DescGruppoEq
                den_conf,                             # DenominazioneConfezione
                (r.get("RAGIONE_SOCIALE") or "").strip(),  # Ditta
                aic,                                  # AIC (NUMERIC affinity)
                aic,                                  # CODICE_AIC
                (r.get("COD_FARMACO") or "").strip(),
                (r.get("COD_CONFEZIONE") or "").strip(),
                den,
                desc,
                (r.get("CODICE_DITTA") or "").strip(),
                (r.get("RAGIONE_SOCIALE") or "").strip(),
                (r.get("STATO_AMMINISTRATIVO") or "").strip(),
                (r.get("TIPO_PROCEDURA") or "").strip(),
                (r.get("FORMA") or "").strip(),
                (r.get("CODICE_ATC") or "").strip(),
                pa,
                forNatura,
                (r.get("LINK_FI") or "").strip(),
                (r.get("LINK_RCP") or "").strip(),
            ))
    return rows


def read_pa_rows(path: str) -> list[tuple]:
    """PA_confezioni.csv (cp1252) -> (CODICE_AIC, PRINCIPIO_ATTIVO,
    QUANTITA, UNITA_MISURA). N.D./empty PA rows carry no information and
    are dropped (~15% of the file); the 4-tuple is unique in the source
    but is deduped defensively anyway."""
    out: dict[tuple, None] = {}
    with open(path, encoding="cp1252", errors="replace") as f:
        r = csv.reader(f, delimiter=";")
        hdr = [h.strip() for h in next(r)]
        if hdr[:4] != ["CODICE_AIC", "PRINCIPIO_ATTIVO", "QUANTITA", "UNITA_MISURA"]:
            raise SystemExit(f"ERROR: PA header changed: {hdr}")
        for row in r:
            if len(row) < 4:
                continue
            aic = row[0].strip()
            pa = row[1].strip()
            if not aic or pa in ("", "N.D.", "n.d."):
                continue
            out[(aic, pa, row[2].strip(), row[3].strip())] = None
    return list(out.keys())


def read_a_ah_aics(classa: str, classeh: str) -> set[str]:
    """AICs from the dated Classe A / Classe H transparency lists (cp1252).
    The AIC sits in a column whose header contains 'AIC' (its position
    differs between the two layouts, so locate it by name)."""
    out = set()
    for path in (classa, classeh):
        with open(path, encoding="cp1252") as f:
            r = csv.reader(f, delimiter=";")
            hdr = [h.replace("\n", "").strip() for h in next(r)]
            # last column whose header ends with "AIC": in the Classe A layout
            # that is the bare "AIC" col (after "Titolare AIC"), in Classe H
            # it is "Codice AIC".
            aic_i = None
            for i, h in enumerate(hdr):
                if h.upper().endswith("AIC"):
                    aic_i = i
            if aic_i is None:
                raise SystemExit(f"ERROR: no AIC column in {path}: {hdr}")
            for row in r:
                if len(row) > aic_i:
                    aic = row[aic_i].strip()
                    if aic.isdigit() and len(aic) == 9:
                        out.add(aic)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--db", default=os.path.normpath(DEFAULT_DB))
    ap.add_argument("--registro", default=None)
    ap.add_argument("--classa", default=None)
    ap.add_argument("--classeh", default=None)
    ap.add_argument("--dispo", default=None)
    ap.add_argument("--pa", default=None)
    ap.add_argument("--legacy", default=CURATED_LEGACY)
    ap.add_argument("--meds-mode", choices=["otc_ah", "ah_only", "full"],
                    default="otc_ah")
    ap.add_argument("--cnd-prefix", action="append", default=None,
                    help="CND group prefix to keep (repeatable; "
                         "default: consumer subset, see DEVICE_CND_PREFIXES)")
    ap.add_argument("--content-version", type=int, default=1,
                    help="monotonic content build number written to catalog_meta")
    args = ap.parse_args()

    cnd_prefixes = tuple(args.cnd_prefix or DEVICE_CND_PREFIXES)
    today = date.today().isoformat()

    registro = args.registro or find_input("aifa registro", ["*confezioni_fornitura.csv"])
    dispo = args.dispo or find_input("mibact dispo", ["*DISPO_RDM_1_*csv"])
    classa = args.classa or find_input("classe A", ["*Classe_A_per_nome_commerciale*"])
    classeh = args.classeh or find_input("classe H", ["*Classe_H_per_nome_commerciale*"])
    pa = args.pa or find_input("aifa pa", ["*PA_confezioni.csv"])
    for label, p in (("registro", registro), ("dispo", dispo),
                     ("classa", classa), ("classeh", classeh), ("pa", pa)):
        if not os.path.exists(p):
            raise SystemExit(f"ERROR: {label} file missing: {p}")
        print(f"{label}: {p}")

    # ------------------------------------------------------------- legacy bridge
    bridge_progressivi = set()
    legacy_rows = []
    if os.path.exists(args.legacy):
        with open(args.legacy, encoding="utf-8") as f:
            for r in csv.DictReader(f, delimiter=";"):
                row = {k: (r.get(k) or "").strip() for k in
                       ("legacy_aic", "ean", "progressivo_dm", "note")}
                if not row["legacy_aic"] and not row["ean"]:
                    continue
                # EAN-only (or AIC-only) rows: NULL keeps the UNIQUE indexes
                # happy (SQLite treats NULLs as distinct, '' would not).
                row["legacy_aic"] = row["legacy_aic"] or None
                row["ean"] = row["ean"] or None
                legacy_rows.append(tuple(row.values()))
                if row["progressivo_dm"]:
                    bridge_progressivi.add(row["progressivo_dm"])
    else:
        print(f"WARN: curated legacy file missing: {args.legacy}")
    print(f"codici_legacy: {len(legacy_rows)} rows, "
          f"{len(bridge_progressivi)} distinct progressivi")

    # ------------------------------------------------------------- devices
    dispo_rows = []
    with open(dispo, encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        hdr = [h.strip() for h in next(reader)]
        if hdr[:len(DISPO_COLS)] != DISPO_COLS[:len(hdr)]:
            raise SystemExit(f"ERROR: DISPO header changed: {hdr}")
        idx = {h: i for i, h in enumerate(hdr)}
        for raw in reader:
            if len(raw) < len(DISPO_COLS):
                continue
            row = {c: (raw[idx[c]] if idx[c] < len(raw) else "").strip()
                   for c in DISPO_COLS}
            cnd = row["classificazione_cnd"]
            if not any(cnd.startswith(p) for p in cnd_prefixes):
                if row["progressivo_dm_ass"] not in bridge_progressivi:
                    continue
            if not is_current_dispo(row, today, bridge_progressivi):
                continue
            vals = [row[c] for c in DISPO_COLS]
            vals.append(row["denominazione_commerciale"].upper())
            dispo_rows.append(tuple(vals))
    print(f"dispositivi_medici: {len(dispo_rows)} rows")

    # ------------------------------------------------------------- medicines
    a_ah = read_a_ah_aics(classa, classeh) if args.meds_mode in ("otc_ah", "ah_only") else set()
    print(f"Classe A/H AICs: {len(a_ah)}")
    med_rows = read_med_rows(registro, args.meds_mode, a_ah, otc_only=(args.meds_mode == "otc_ah"))
    print(f"med_classea ({args.meds_mode}): {len(med_rows)} rows")

    # ------------------------------------------------------------- PA
    pa_rows = read_pa_rows(pa)
    print(f"pa_confezioni: {len(pa_rows)} rows (N.D. filtered)")

    # ------------------------------------------------------------------- db
    if os.path.exists(args.db):
        os.remove(args.db)
    d = os.path.dirname(os.path.abspath(args.db))
    os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE med_classea (
            PrincipioAttivo TEXT,
            DescGruppoEq TEXT,
            DenominazioneConfezione TEXT,
            Ditta TEXT,
            AIC NUMERIC,
            CODICE_AIC TEXT,
            COD_FARMACO TEXT,
            COD_CONFEZIONE TEXT,
            DENOMINAZIONE TEXT,
            DESCRIZIONE TEXT,
            CODICE_DITTA TEXT,
            RAGIONE_SOCIALE TEXT,
            STATO_AMMINISTRATIVO TEXT,
            TIPO_PROCEDURA TEXT,
            FORMA TEXT,
            CODICE_ATC TEXT,
            PA_ASSOCIATI TEXT,
            FORNITURA TEXT,
            LINK_FI TEXT,
            LINK_RCP TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE dispositivi_medici (
            tipologia_dm TEXT,
            progressivo_dm_ass TEXT,
            data_prima_pubblicazione TEXT,
            dm_riferimento TEXT,
            gruppo_dm_simili TEXT,
            iscrizione_repertorio TEXT,
            data_inizio_validita TEXT,
            data_fine_validita TEXT,
            fabbricante_assemblatore TEXT,
            cod_fiscale TEXT,
            partita_iva TEXT,
            cod_catalogo_fabbr_ass TEXT,
            denominazione_commerciale TEXT,
            classificazione_cnd TEXT,
            descrizione_cnd TEXT,
            data_fine_commercio TEXT,
            DENOMINAZIONE_COMERCIALE_UPPER TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE codici_legacy (
            legacy_aic TEXT,
            ean TEXT,
            progressivo_dm TEXT,
            note TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE pa_confezioni (
            CODICE_AIC TEXT,
            PRINCIPIO_ATTIVO TEXT,
            QUANTITA TEXT,
            UNITA_MISURA TEXT
        )
    """)
    cur.execute("CREATE INDEX indPaAic ON pa_confezioni(CODICE_AIC)")

    cur.execute("CREATE TABLE catalog_meta (key TEXT PRIMARY KEY, value TEXT)")

    # legacy parity tables (empty; the 2016 db shipped them)
    cur.execute("""
        CREATE TABLE archivio (
            ts_insert TEXT,
            id INTEGER PRIMARY KEY,
            qta NUMERIC,
            scad TEXT,
            formato TEXT,
            farmaco TEXT,
            dove TEXT,
            chk_exp NUMERIC
        )
    """)
    cur.execute("""
        CREATE TABLE promemoria (
            ts_prome TEXT,
            ts_insert TEXT,
            id INTEGER PRIMARY KEY,
            aic NUMERIC,
            note TEXT,
            avvisa_ogni NUMERIC
        )
    """)

    BATCH = 5000
    buf = []
    for row in med_rows:
        buf.append(row)
        if len(buf) >= BATCH:
            cur.executemany("INSERT INTO med_classea VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", buf)
            buf = []
    if buf:
        cur.executemany("INSERT INTO med_classea VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", buf)

    cur.executemany(
        "INSERT INTO dispositivi_medici VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        dispo_rows)
    cur.executemany("INSERT INTO codici_legacy VALUES (?,?,?,?)", legacy_rows)
    cur.executemany("INSERT INTO pa_confezioni VALUES (?,?,?,?)", pa_rows)

    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "content_version": str(args.content_version),
        "meds_mode": args.meds_mode,
        "registro_file": os.path.basename(registro),
        "dispo_file": os.path.basename(dispo),
        "classa_file": os.path.basename(classa),
        "classeh_file": os.path.basename(classeh),
        "med_rows": str(len(med_rows)),
        "device_rows": str(len(dispo_rows)),
        "legacy_rows": str(len(legacy_rows)),
        "pa_rows": str(len(pa_rows)),
    }
    # pull provenance from the download manifest when present
    man_path = os.path.join(SOURCES, ".manifest.json")
    if os.path.exists(man_path):
        import json
        with open(man_path, encoding="utf-8") as f:
            man = json.load(f).get("files", {})
        for key, label in (("registro", "aifa_registro"), ("dispo", "mibact_dispo"),
                           ("classa", "aifa_classe_a"), ("classeh", "aifa_classe_h"),
                           ("pa", "aifa_pa")):
            entry = man.get(label)
            if entry:
                meta[f"{key}_url"] = entry.get("url", "")
                meta[f"{key}_date"] = (entry.get("downloaded_at") or "")[:10]
                meta[f"{key}_sha256"] = entry.get("sha256", "")
    else:
        print("WARN: no sources/.manifest.json — source dates/shas missing "
              "from catalog_meta (run download_sources.py first)")

    cur.executemany("INSERT INTO catalog_meta VALUES (?,?)",
                    [(k, v) for k, v in meta.items()])

    # ------------------------------------------------------------- indexes
    cur.execute("CREATE UNIQUE INDEX indMedClassea ON med_classea(AIC ASC)")
    cur.execute("CREATE INDEX indMedClasseaName ON med_classea(DenominazioneConfezione ASC)")
    cur.execute("CREATE INDEX indMedClasseaPA ON med_classea(PrincipioAttivo ASC)")
    cur.execute("CREATE INDEX indMedClasseaCodeAIC ON med_classea(CODICE_AIC ASC)")
    cur.execute("CREATE UNIQUE INDEX indDispoProg ON dispositivi_medici(progressivo_dm_ass)")
    cur.execute("CREATE INDEX indDispoName ON dispositivi_medici(DENOMINAZIONE_COMERCIALE_UPPER)")
    cur.execute("CREATE INDEX indDispoCnd ON dispositivi_medici(classificazione_cnd)")
    cur.execute("CREATE UNIQUE INDEX indLegacyAic ON codici_legacy(legacy_aic)")
    cur.execute("CREATE UNIQUE INDEX indLegacyEan ON codici_legacy(ean)")

    conn.commit()

    size = os.path.getsize(args.db)
    print(f"\n=== {args.db}")
    print(f"  {size/1e6:.1f} MB")
    for t in ("med_classea", "dispositivi_medici", "codici_legacy", "pa_confezioni"):
        print(f"  {t}: {cur.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]} rows")

    # smoke test: the Maalox bridge must resolve
    if bridge_progressivi:
        p = next(iter(bridge_progressivi))
        hit = cur.execute(
            "SELECT denominazione_commerciale FROM dispositivi_medici "
            "WHERE progressivo_dm_ass=?", (p,)).fetchone()
        print(f"  bridge smoke: progressivo {p} -> {hit[0] if hit else 'MISSING!'}")
        if not hit:
            print("  ERROR: a curated progressivo is not in dispositivi_medici")
            sys.exit(1)

    conn.close()
    print("done.")


if __name__ == "__main__":
    main()
