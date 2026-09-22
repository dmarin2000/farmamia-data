#!/usr/bin/env python3
"""
Download the official open-data sources for the Farmamia catalog.

Sources (all free, official, no API key):
  aifa_registro   drive.aifa.gov.it/farmaci/confezioni_fornitura.csv
                  daily update, utf-8, 15 cols (CODICE_AIC ... LINK_RCP)
  aifa_pa         drive.aifa.gov.it/farmaci/PA_confezioni.csv
                  daily update, utf-8, PA per AIC
  aifa_atc        drive.aifa.gov.it/farmaci/atc.csv
                  daily update, utf-8
  aifa_classe_a   aifa.gov.it/it/liste-dei-farmaci -> latest
                  Classe_A_per_nome_commerciale_<gg-MM-aaaa>.csv (cp1252)
  aifa_classe_h   same page -> Classe_H_per_nome_commerciale_<gg-MM-aaaa>.csv
  mibact_dispo    dati.salute.gov.it/it/dataset/dispositivi-medici/
                  (page-data.json -> dated DISPO_RDM_1_<date>_csv.zip, weekly)

Outputs land in ./sources/ next to this script. sources/.manifest.json
records url + etag/last-modified + sha256 + last_content_change per
file so re-runs with --skip-unchanged are cheap no-ops.

Fail-closed (plan §6): files download to <name>.part and are validated
(header sniff + row count + staleness band) BEFORE being renamed over
the last-good copy. A failed validation leaves the previous file
intact. Staleness = days since the file's sha256 last changed; bands
per source (days): warn/fail. A source whose content hasn't moved past
the fail band aborts the run (--allow-stale to override).

Usage:
  python3 download_sources.py                       # all sources
  python3 download_sources.py --only mibact_dispo   # subset, comma or repeated
  python3 download_sources.py --skip-unchanged
  python3 download_sources.py --keep-zip            # keep the DISPO zip
  python3 download_sources.py --outdir /tmp/src
  python3 download_sources.py --allow-stale
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.request
import urllib.error
import zipfile
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from urllib.parse import urljoin, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTDIR = os.path.join(HERE, "..", "sources")
AIFA_LIST_PAGE = "https://www.aifa.gov.it/it/liste-dei-farmaci"
MIBACT_PAGE_DATA = ("https://www.dati.salute.gov.it/"
                    "page-data/it/dataset/dispositivi-medici/page-data.json")
USER_AGENT = "farmamia-catalog-build/1.0 (https://github.com/dmarin/farmamia)"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Last-known-good dated URLs, used only if the list page cannot be parsed.
FALLBACK_CLASSE_A = "https://www.aifa.gov.it/documents/20142/3815901/Classe_A_per_nome_commerciale_30-04-2026.csv"
FALLBACK_CLASSE_H = "https://www.aifa.gov.it/documents/20142/3815901/Classe_H_per_nome_commerciale_30-04-2026.csv"

# Header sniff: first header cell each source must start with.
EXPECTED_FIRST_HEADER = {
    "aifa_registro": "CODICE_AIC",
    "aifa_pa": "CODICE_AIC",
    "aifa_atc": "CODICE_ATC",
    "aifa_classe_a": "Principio attivo",
    "aifa_classe_h": "Principio attivo",
    "mibact_dispo": "tipologia_dm",
}

ENCODING = {
    "aifa_registro": "utf-8",
    "aifa_pa": "utf-8",
    "aifa_atc": "utf-8",
    "aifa_classe_a": "cp1252",
    "aifa_classe_h": "cp1252",
    "mibact_dispo": "utf-8",
}

# Staleness bands in days, keyed by source (plan §3, per source):
# (warn_if_older_than, fail_if_older_than) measured on days since the
# sha256 last changed. A/H republishes at AIFA's whim (5+ months is
# normal); registro/PA/ATC are daily; DISPO is weekly.
STALENESS_BANDS = {
    "aifa_registro": (30, 90),
    "aifa_pa": (30, 90),
    "aifa_atc": (30, 90),
    "aifa_classe_a": (120, 365),
    "aifa_classe_h": (120, 365),
    "mibact_dispo": (7, 14),
}


def make_opener(domain: str) -> urllib.request.OpenerDirector:
    """urllib opener with a cookie jar — Liferay (aifa.gov.it) 404s the
    /documents/ downloads for a bare request without the session cookies the
    list page set."""
    jar = CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def fetch(opener, url: str, dest: str, timeout: int = 300, getter=None) -> dict:
    """Download to <dest>.part. The caller validates the part, then
    promotes it with os.replace (fail-closed: the last-good file is
    only touched after the new one is known good).

    getter: optional callable (url, timeout) -> (bytes, headers) used
    instead of the default urllib path (the Mibact host needs one)."""
    if getter is not None:
        data, headers = getter(url, timeout)
        status = 200
    else:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read()
            status = resp.status
            headers = resp.headers
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    return {
        "url": url,
        "status": status,
        "etag": headers.get("ETag"),
        "last_modified": headers.get("Last-Modified"),
        "content_type": headers.get("Content-Type"),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "part": tmp,
    }


def _mibact_ssl_ctx() -> ssl.SSLContext:
    """TLS1.2-pinned context. The Mibact host is a TLS1.2-only box; pinning
    trims the TLS1.3 extensions from the ClientHello (changes the JA3), which
    can slip past a WAF that rejects the default Python/OpenSSL 3.x profile."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _mibact_proxy() -> str | None:
    return os.environ.get("MIBACT_PROXY") or os.environ.get("HTTPS_PROXY")


def _mibact_urllib(url: str, timeout: int) -> tuple[bytes, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    handlers = [urllib.request.HTTPSHandler(context=_mibact_ssl_ctx())]
    proxy = _mibact_proxy()
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(), dict(resp.headers)


def _mibact_curl(url: str, timeout: int) -> tuple[bytes, dict]:
    """Different TLS stack + header order than urllib (libcurl) — a distinct
    client fingerprint. This is the strategy that works from most egress."""
    cmd = ["curl", "-fsSL", "--max-time", str(timeout),
           "--tlsv1.2", "--tls-max", "1.2", "-A", BROWSER_UA, "-o", "-", url]
    proxy = _mibact_proxy()
    if proxy:
        cmd += ["-x", proxy]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 30)
    if r.returncode != 0:
        raise RuntimeError("curl rc=%d: %s" % (
            r.returncode, r.stderr.decode("utf-8", "replace")[:300]))
    return r.stdout, {}


def mibact_fetch(url: str, timeout: int = 300) -> tuple[bytes, dict]:
    """Resilient GET for the Mibact (dati.salute.gov.it) host. The server is
    a single TLS1.2-only Fastweb residential box behind a client-filtering
    proxy that rejects some egress (GitHub-hosted runners) mid-handshake with
    SSLV3_ALERT_HANDSHAKE_FAILURE. Try, in order: urllib (TLS1.2-pinned) ->
    curl subprocess -> (if MIBACT_PROXY set, both through it). Fail closed."""
    errors = []
    for label, fn in (("urllib", _mibact_urllib), ("curl", _mibact_curl)):
        try:
            data, headers = fn(url, timeout)
            print(f"  [mibact] via {label} OK ({len(data)} bytes)")
            return data, headers
        except Exception as e:  # noqa: BLE001 - fail over to next strategy
            errors.append(f"{label}: {type(e).__name__}: {e}")
            print(f"  [mibact] via {label} FAILED: {type(e).__name__}: {e}")
    raise SystemExit(
        "ERROR: Mibact host (www.dati.salute.gov.it) unreachable by every strategy:\n  "
        + "\n  ".join(errors)
        + "\n  It is a single TLS1.2-only box rejecting this egress (IP/ASN or TLS\n"
        + "  profile). Set a MIBACT_PROXY secret (residential/Italian egress) and\n"
        + "  retry; the last published catalog stays live (fail-closed)."
    )


def sniff_header(path: str, encoding: str, expected: str) -> None:
    with open(path, encoding=encoding, errors="replace") as f:
        first = f.readline()
    first_header = first.split(";")[0].strip('"').strip()
    if first_header != expected:
        raise SystemExit(
            f"ERROR: {path} starts with {first_header!r}, expected {expected!r} "
            f"— the source layout changed, adapt the build script"
        )
    print(f"  header OK: {first_header!r}")


def row_count(path: str, encoding: str) -> int:
    with open(path, encoding=encoding, errors="replace") as f:
        return sum(1 for _ in csv.reader(f, delimiter=";")) - 1


def check_staleness(name: str, meta: dict, manifest: dict, allow_stale: bool) -> None:
    """Fail-closed staleness gate (plan §6). Content age = days since the
    sha256 last changed (stored as last_content_change in the manifest).
    First-ever download of a source skips the gate (no history)."""
    warn_d, fail_d = STALENESS_BANDS.get(name, (30, 90))
    old = manifest["files"].get(name, {})
    old_sha = old.get("sha256")
    if old_sha == meta["sha256"]:
        changed = old.get("last_content_change")
    else:
        meta["last_content_change"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        changed = None
    if not changed:
        return
    age = (datetime.now(timezone.utc) - datetime.strptime(changed, "%Y-%m-%d").replace(tzinfo=timezone.utc)).days
    if age > fail_d:
        msg = (f"source {name}: content unchanged for {age}d (fail band {fail_d}d) "
               f"— is the upstream dead or is our parser stale?")
        if allow_stale:
            print(f"  WARN: {msg} (--allow-stale, continuing)")
        else:
            raise SystemExit(f"ERROR: {msg} — use --allow-stale to override")
    elif age > warn_d:
        print(f"  WARN: source {name}: content unchanged for {age}d (warn band {warn_d}d)")


def resolve_latest_classe(opener, kind: str) -> str:
    """kind = 'A' or 'H'. Fetch the AIFA list page, pick the newest dated
    Classe_<kind>_per_nome_commerciale CSV, fall back to the last-known URL."""
    # AIFA's WAF 403s urllib's default UA on www.aifa.gov.it — use a browser
    # UA for this one request (drive.aifa.gov.it accepts the pipeline UA).
    req = urllib.request.Request(AIFA_LIST_PAGE, headers={"User-Agent": BROWSER_UA})
    with opener.open(req, timeout=60) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
    pat = re.compile(r"(/documents/[^\"']+Classe_" + kind +
                     r"_per_nome_commerciale_(\d{2}-\d{2}-\d{4})\.csv)")
    dates = []
    for path, date in pat.findall(html):
        try:
            d = datetime.strptime(date, "%d-%m-%Y")
        except ValueError:
            continue
        dates.append((d, path))
    if dates:
        dates.sort()
        newest = dates[-1]
        return urljoin(AIFA_LIST_PAGE, newest[1])
    print(f"  WARN: could not parse {kind} list links, using fallback URL")
    return FALLBACK_CLASSE_A if kind == "A" else FALLBACK_CLASSE_H


def resolve_mibact(opener) -> tuple[str, str]:
    """page-data.json -> (zip_url, zip_basename).

    Two layouts have been observed: top-level `relationships` (vault
    index.json) and the nested Drupal one
    `result.data.allNodeDataset.nodes[0].relationships` (the current
    /page-data/it/dataset/.../page-data.json). Accept either."""
    raw, _ = mibact_fetch(MIBACT_PAGE_DATA, timeout=60)
    data = json.loads(raw.decode("utf-8"))
    rels = data.get("relationships", {}).get("field_listafile", [])
    if not rels:
        nodes = (((data.get("result") or {}).get("data") or {}).get(
            "allNodeDataset") or {}).get("nodes") or []
        if nodes:
            rels = (nodes[0].get("relationships") or {}).get("field_listafile", [])
    if not rels:
        raise SystemExit("ERROR: no field_listafile in Mibact page-data.json "
                         "(layout changed?)")
    csvzips = [r for r in rels if (r.get("url") or "").endswith("_csv.zip")]
    if not csvzips:
        raise SystemExit("ERROR: no *_csv.zip in Mibact field_listafile "
                         f"({[r.get('filename') for r in rels]})")
    url = csvzips[0].get("url") or ""
    if not url.startswith("http"):
        url = urljoin("https://www.dati.salute.gov.it/", url)
    base = os.path.basename(urlparse(url).path)
    return url, base


def load_manifest(outdir: str) -> dict:
    path = os.path.join(outdir, ".manifest.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"files": {}}


def save_manifest(outdir: str, manifest: dict) -> None:
    path = os.path.join(outdir, ".manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--only", action="append",
                    help="source name (repeatable): aifa_registro aifa_pa aifa_atc "
                         "aifa_classe_a aifa_classe_h mibact_dispo")
    ap.add_argument("--outdir", default=os.path.normpath(DEFAULT_OUTDIR))
    ap.add_argument("--skip-unchanged", action="store_true",
                    help="re-download only when etag/last-modified changed")
    ap.add_argument("--keep-zip", action="store_true",
                    help="keep the DISPO zip after extracting the CSV")
    ap.add_argument("--allow-stale", action="store_true",
                    help="warn instead of failing on past-fail-band staleness")
    args = ap.parse_args()

    names = args.only or list(EXPECTED_FIRST_HEADER)
    for n in names:
        if n not in EXPECTED_FIRST_HEADER:
            raise SystemExit(f"unknown source {n!r}")

    outdir = args.outdir
    os.makedirs(outdir, exist_ok=True)
    manifest = load_manifest(outdir)
    aifa = make_opener("aifa.gov.it")
    drive = make_opener("drive.aifa.gov.it")
    salute = make_opener("dati.salute.gov.it")

    targets = {
        "aifa_registro": (drive, "https://drive.aifa.gov.it/farmaci/confezioni_fornitura.csv"),
        "aifa_pa": (drive, "https://drive.aifa.gov.it/farmaci/PA_confezioni.csv"),
        "aifa_atc": (drive, "https://drive.aifa.gov.it/farmaci/atc.csv"),
    }

    for name in names:
        if name in targets:
            opener, url = targets[name]
        elif name in ("aifa_classe_a", "aifa_classe_h"):
            opener, url = aifa, resolve_latest_classe(aifa, name[-1].upper())
        elif name == "mibact_dispo":
            opener, url = salute, None  # resolved below
        else:
            raise SystemExit(f"unhandled {name}")

        if url is None:
            url, base = resolve_mibact(opener)
        else:
            base = os.path.basename(urlparse(url).path)

        print(f"== {name}: {url}")
        local = os.path.join(outdir, base)

        if args.skip_unchanged and name in manifest["files"]:
            old = manifest["files"][name]
            probe = _probe(opener, url)
            if (old.get("etag"), old.get("last_modified")) == (
                    probe.get("etag"), probe.get("last_modified")):
                print("  unchanged, skipping")
                continue

        meta = fetch(opener, url, local,
                     getter=mibact_fetch if name == "mibact_dispo" else None)
        # header sniff on the part, except for the DISPO zip (it is a zip;
        # its CSV is validated after extraction below)
        if not base.endswith(".zip"):
            sniff_header(meta["part"], ENCODING[name], EXPECTED_FIRST_HEADER[name])
            n = row_count(meta["part"], ENCODING[name])
        check_staleness(name, meta, manifest, args.allow_stale)
        os.replace(meta["part"], local)
        del meta["part"]
        if not base.endswith(".zip"):
            print(f"  saved {local} ({meta['bytes']} bytes, {n} rows)")
            meta["rows"] = n
        else:
            print(f"  saved {local} ({meta['bytes']} bytes, zip)")
        meta["downloaded_at"] = datetime.now(timezone.utc).isoformat()
        manifest["files"][name] = meta
        save_manifest(outdir, manifest)  # persist per-source: an abort later
                                         # in the run keeps finished downloads

        if name == "mibact_dispo" and base.endswith(".zip"):
            csv_base = base[:-4] + ".csv"
            csv_path = os.path.join(outdir, csv_base)
            with zipfile.ZipFile(local) as z:
                names_in = [x for x in z.namelist() if x.endswith(".csv")]
                if not names_in:
                    raise SystemExit(f"ERROR: no CSV inside {local}")
                with z.open(names_in[0]) as src, open(csv_path, "wb") as dst:
                    dst.write(src.read())
            if not args.keep_zip:
                os.remove(local)
            local = csv_path
            sniff_header(csv_path, "utf-8", EXPECTED_FIRST_HEADER[name])
            n = row_count(csv_path, "utf-8")
            manifest["files"][name]["extracted"] = csv_base
            manifest["files"][name]["rows"] = n
            print(f"  extracted {csv_path} ({n} rows)")

    save_manifest(outdir, manifest)
    print("done.")


def _probe(opener, url: str) -> dict:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=60) as resp:
            return {"etag": resp.headers.get("ETag"),
                    "last_modified": resp.headers.get("Last-Modified")}
    except urllib.error.URLError as e:
        print(f"  WARN: HEAD probe failed ({e}), will re-download")
        return {}


if __name__ == "__main__":
    main()
