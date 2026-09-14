#!/usr/bin/env python3
"""
Build the overlay file (PLAN-update-strategy §5): the cumulative diff of a
candidate catalog build against the baseline the app currently ships with.

The overlay is a small standalone SQLite DB the app downloads, then opens
with the baseline ATTACHed under the alias `farmabase` (see CatalogDb.kt).
It contains, per table:

  overlay_<table>     base columns + a synthetic __key TEXT column; rows
                      added or changed vs the baseline
  tombstone_<table>   keys deleted vs the baseline

plus a `content_meta` key/value table (content_version, the baseline's
sha256, per-source dates/shas).

No permanent views in the file: modern SQLite (>=3.4x, verified 3.51)
rejects a permanent CREATE VIEW that references an attached database
("view v cannot reference objects in database X") but accepts a TEMP
view. So the merge views are created by whoever opens the overlay, as
TEMP views, immediately after ATTACHing the baseline as `farmabase` —
the app does this in CatalogDb.kt with the same SQL template this
script uses (TABLES below).

The app's repository keeps querying the plain table names
(`med_classea`, …) — the temp views provide them.

File conventions match the baseline asset (Q8): no WAL, plain file,
user_version 0.

Usage:
  python3 diff_catalog.py --prev /path/baseline.db --cand /path/candidate.db \
      --out /path/overlay.sqlite --baseline-sha <hex> [--content-version N]

  --prev none    first publish: overlay with no rows (views only).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

# per table: indexes into the SELECT * tuple that form the key, and the SQL
# expression that reproduces the same key from a base row (single value or
# concatenated for composite keys).
TABLES = {
    # AIC is NUMERIC affinity in the base (leading-zero AICs store as ints);
    # CAST normalises both sides of the NOT IN to text.
    "med_classea": (4, "CAST(AIC AS TEXT)"),
    "dispositivi_medici": (1, "progressivo_dm_ass"),
    "codici_legacy": ((0, 1),
                      "(COALESCE(legacy_aic,'')||char(1)||COALESCE(ean,''))"),
    "pa_confezioni": ((0, 1, 2, 3),
                      "(COALESCE(CODICE_AIC,'')||char(1)||COALESCE(PRINCIPIO_ATTIVO,'')"
                      "||char(1)||COALESCE(QUANTITA,'')||char(1)||COALESCE(UNITA_MISURA,''))"),
}


def load_rows(path: str, table: str, key_spec) -> dict:
    """key(str) -> full row tuple (values normalised to str/None), one table."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cur = conn.execute(f"SELECT * FROM {table}")
        ki = key_spec if isinstance(key_spec, tuple) else (key_spec,)
        out = {}
        for row in cur.fetchall():
            vals = tuple(None if v is None else str(v) for v in row)
            key = "\x01".join(v if v is not None else "" for v in
                              [vals[i] for i in ki])
            out[key] = vals
        return out
    finally:
        conn.close()


def make_key(key_spec, vals: tuple) -> str:  # noqa: keep for callers/tests
    ki = key_spec if isinstance(key_spec, tuple) else (key_spec,)
    return "\x01".join(v if v is not None else "" for v in [vals[i] for i in ki])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--prev", required=True,
                    help="baseline db the overlay is built against (or 'none')")
    ap.add_argument("--cand", required=True, help="candidate full build db")
    ap.add_argument("--out", required=True, help="overlay sqlite path")
    ap.add_argument("--baseline-sha", required=True,
                    help="sha256 of the baseline file (hex)")
    ap.add_argument("--content-version", type=int, default=None,
                    help="default: read from candidate catalog_meta")
    args = ap.parse_args()

    if args.prev.lower() != "none" and not os.path.exists(args.prev):
        raise SystemExit(f"ERROR: prev db missing: {args.prev}")

    def meta_of(path: str) -> dict:
        return dict(sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                    .execute("SELECT key, value FROM catalog_meta").fetchall())

    cand_meta = meta_of(args.cand)
    cv = args.content_version or int(cand_meta.get("content_version", "1"))
    schema_v = cand_meta.get("schema_version", "?")
    if args.prev.lower() != "none" and \
            meta_of(args.prev).get("schema_version") != schema_v:
        raise SystemExit("ERROR: prev and candidate schema_version differ — "
                         "an overlay cannot bridge a schema bump "
                         "(publish a new baseline instead)")

    # ------------------------------------------------------------- diff
    upserts: dict[str, list] = {t: [] for t in TABLES}
    deletes: dict[str, list] = {t: [] for t in TABLES}
    for t, (key_spec, expr) in TABLES.items():
        cand = load_rows(args.cand, t, key_spec)
        # prev=none means "first publish": the baseline IS the candidate, so
        # diff against itself — the overlay must be near-empty (pass-through
        # views), not a full copy of the catalog.
        prev = load_rows(args.prev, t, key_spec) if args.prev.lower() != "none" else cand
        for key, row in cand.items():
            if key not in prev or prev[key] != row:
                upserts[t].append((key, row))
        for key in prev:
            if key not in cand:
                deletes[t].append(key)
        added = sum(1 for k, _ in upserts[t] if k not in prev)
        changed = len(upserts[t]) - added
        print(f"{t}: +{added} changed:{changed} -{len(deletes[t])}")

    if args.prev.lower() != "none" and \
            all(not upserts[t] and not deletes[t] for t in TABLES):
        print("NO CHANGE (no overlay published; run exits 42)")
        sys.exit(42)

    # ------------------------------------------------------------- overlay
    if os.path.exists(args.out):
        os.remove(args.out)
    conn = sqlite3.connect(args.out)
    conn.execute("PRAGMA journal_mode=DELETE")
    cur = conn.cursor()
    cur.execute("CREATE TABLE content_meta (key TEXT PRIMARY KEY, value TEXT)")

    base = sqlite3.connect(f"file:{args.cand}?mode=ro", uri=True)
    # views may only be created with their referenced db attached. ATTACH
    # takes a plain path here (the connection was opened without the URI
    # flag, so a 'file:' prefix would be taken literally).
    conn.execute(f"ATTACH DATABASE {os.path.abspath(args.cand)!r} AS farmabase")
    try:
        for t, (key_spec, expr) in TABLES.items():
            ddl_cols = base.execute(f"PRAGMA table_info({t})").fetchall()
            names = [c[1] for c in ddl_cols]
            defs = ", ".join(f"{n} {c[2] or 'TEXT'}" for n, c in zip(names, ddl_cols))
            cur.execute(f"CREATE TABLE overlay_{t} ({defs}, __key TEXT)")
            cur.execute(f"CREATE INDEX ind_ov_{t} ON overlay_{t}(__key)")
            cur.execute(f"CREATE TABLE tombstone_{t} (key TEXT)")
            cur.execute(f"CREATE INDEX ind_tb_{t} ON tombstone_{t}(key)")
            sel = ", ".join(names)
            # TEMP view (see module docstring): column resolution for the
            # smoke query below; the app recreates the same views.
            cur.execute(
                f"CREATE TEMP VIEW {t} AS "
                f"SELECT {sel} FROM overlay_{t} "
                f"UNION SELECT {sel} FROM \"farmabase\".{t} "
                f"WHERE {expr} NOT IN (SELECT __key FROM overlay_{t}) "
                f"AND {expr} NOT IN (SELECT key FROM tombstone_{t})")

            ph = ",".join("?" * (len(names) + 1))
            ins = f"INSERT INTO overlay_{t} VALUES ({ph})"
            for key, row in upserts[t]:
                cur.execute(ins, (*row, key))
            if deletes[t]:
                cur.executemany(f"INSERT INTO tombstone_{t} VALUES (?)",
                                [(k,) for k in deletes[t]])
    finally:
        base.close()
    conn.execute("DETACH DATABASE farmabase")

    # smoke: recreate the temp views the app would create (baseline attached
    # as `farmabase`) and check the invariant: merged row count == candidate
    # row count (every key appears exactly once: in the overlay, or in the
    # baseline without being hidden).
    smoke_base = args.prev if args.prev.lower() != "none" else args.cand
    conn.execute(f"ATTACH DATABASE {os.path.abspath(smoke_base)!r} AS farmabase")
    smokes = {}
    try:
        for t, (key_spec, expr) in TABLES.items():
            names = [c[1] for c in
                     conn.execute(f"PRAGMA table_info(overlay_{t})").fetchall()[:-1]]
            sel = ", ".join(names)
            c2 = conn.cursor()
            c2.execute(
                f"CREATE TEMP VIEW v_{t} AS "
                f"SELECT {sel} FROM overlay_{t} "
                f"UNION SELECT {sel} FROM \"farmabase\".{t} "
                f"WHERE {expr} NOT IN (SELECT __key FROM overlay_{t}) "
                f"AND {expr} NOT IN (SELECT key FROM tombstone_{t})")
            smokes[t] = c2.execute(f"SELECT COUNT(*) FROM v_{t}").fetchone()[0]
            c2.close()
        conn.commit()  # persist the inserts; also closes the read txn so the
                       # DETACH below is not lock-blocked
    finally:
        conn.execute("DETACH DATABASE farmabase")
    cand_conn = sqlite3.connect(f"file:{args.cand}?mode=ro", uri=True)
    for t in TABLES:
        want = cand_conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        got = smokes[t]
        if got != want:
            raise SystemExit(f"ERROR: view smoke {t}: got {got}, want {want}")
        print(f"  view {t}: {got} rows ok")
    cand_conn.close()

    # content_meta: version + provenance passthrough from the candidate
    meta = {
        "content_version": str(cv),
        "schema_version": schema_v,
        "baseline_sha256": args.baseline_sha,
    }
    for k in ("registro_date", "dispo_date", "classa_date", "classeh_date",
              "pa_date", "registro_sha256", "dispo_sha256"):
        if cand_meta.get(k):
            meta[k] = cand_meta[k]
    cur.executemany("INSERT INTO content_meta VALUES (?,?)", meta.items())

    conn.commit()

    size = os.path.getsize(args.out)
    print(f"\n=== {args.out}  ({size/1e6:.2f} MB, content_version {cv}, "
          f"baseline {args.baseline_sha[:12]}…)")
    for t in TABLES:
        u = cur.execute(f"SELECT COUNT(*) FROM overlay_{t}").fetchone()[0]
        d = cur.execute(f"SELECT COUNT(*) FROM tombstone_{t}").fetchone()[0]
        print(f"  {t}: overlay {u} rows, tombstones {d}")
    conn.close()


if __name__ == "__main__":
    main()
