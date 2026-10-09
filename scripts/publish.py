#!/usr/bin/env python3
"""
Stage and publish a catalog release (PLAN-update-strategy §6).

Reads the built baseline db + overlay file, content-addresses them, and
writes dist/manifest.json (the only file the app GETs on every check):

  dist/
    baselines/<sha12>.db        content-addressed, immutable
    overlays/overlay_<cv>.sqlite
    manifest.json

With --upload the staged files go to the R2 bucket (S3 API, AWS SigV4,
stdlib only — no boto needed). R2 object keys mirror the URL paths the
catalog Worker serves; the GitHub Releases mirror is a second host the
app falls back to (kept in sync by the Actions workflow).

Environment for --upload:
  R2_ENDPOINT        https://<account>.r2.cloudflarestorage.com
                     (or any S3-compatible endpoint, e.g. https://s3.cubbit.eu)
  R2_ACCESS_KEY_ID   scoped S3 API token
  R2_SECRET_ACCESS_KEY
  R2_BUCKET
  S3_REGION          optional; SigV4 signing region. "auto" (default) for R2,
                     "eu-west-1" for Cubbit DS3

Usage:
  python3 publish.py --baseline build/candidate.db --overlay build/overlay.sqlite \
      --base-url https://catalog.<account>.workers.dev [--upload]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import shutil
import urllib.request

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def s3_put(endpoint: str, bucket: str, key: str, data: bytes,
           cache_control: str | None = None) -> None:
    """PUT one object via the S3 API (SigV4, stdlib only)."""
    access = os.environ["R2_ACCESS_KEY_ID"]
    secret = os.environ["R2_SECRET_ACCESS_KEY"]
    host = endpoint.split("//", 1)[1].split("/")[0]
    # R2 ignores the region; other S3-compatible stores need their own
    # (Cubbit DS3 = "eu-west-1"). Empty/unset -> "auto" (R2 behavior).
    region = os.environ.get("S3_REGION") or "auto"
    service = "s3"
    now = dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    payload_hash = hashlib.sha256(data).hexdigest()
    canonical_uri = f"/{bucket}/{key}"
    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if cache_control:
        headers["cache-control"] = cache_control
    canon_hdr = "".join(f"{k}:{v}\n" for k, v in sorted(headers.items()))
    signed = ";".join(sorted(headers))
    canonical = "\n".join(["PUT", canonical_uri, "",
                           canon_hdr, signed, payload_hash])
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                                hashlib.sha256(canonical.encode()).hexdigest()])

    def _mac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k = _mac(_mac(_mac(_mac((f"AWS4{secret}").encode(), date_stamp), region),
                  service), "aws4_request")
    signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()

    req = urllib.request.Request(f"{endpoint}/{bucket}/{key}",
                                 data=data, method="PUT")
    for h, v in headers.items():
        req.add_header(h, v)
    req.add_header("Authorization",
                   f"AWS4-HMAC-SHA256 Credential={access}/{scope}, "
                   f"SignedHeaders={signed}, Signature={signature}")
    with urllib.request.urlopen(req, timeout=600) as resp:
        if resp.status not in (200, 201):
            raise SystemExit(f"ERROR: S3 PUT {key} -> HTTP {resp.status}")
    print(f"  uploaded s3://{bucket}/{key} ({len(data)/1e6:.1f} MB)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--overlay", required=True)
    ap.add_argument("--dist", default=os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "..", "dist"))
    ap.add_argument("--base-url", default="https://catalog.example.invalid",
                    help="public base URL the manifest/overlay live under")
    ap.add_argument("--baseline-url", default=None,
                    help="full URL of the baseline object (default: "
                         "<base-url>/<content-addressed key>). Use when the "
                         "baseline is hosted elsewhere, e.g. a GitHub Release "
                         "asset, because the ~124 MB db exceeds the 100 MB "
                         "git push limit of the Pages branch")
    ap.add_argument("--upload", action="store_true",
                    help="PUT staged files to R2 (S3 API)")
    ap.add_argument("--keep-dist", action="store_true")
    args = ap.parse_args()

    import sqlite3
    bmeta = dict(sqlite3.connect(
        f"file:{args.baseline}?mode=ro", uri=True).execute(
        "SELECT key, value FROM catalog_meta").fetchall())
    ometa = dict(sqlite3.connect(
        f"file:{args.overlay}?mode=ro", uri=True).execute(
        "SELECT key, value FROM content_meta").fetchall())

    if bmeta.get("schema_version") != ometa.get("schema_version"):
        raise SystemExit("ERROR: baseline and overlay schema_version differ")

    dist = os.path.normpath(args.dist)
    base_sha = _sha256_file(args.baseline)
    ovl_sha = _sha256_file(args.overlay)
    cv = ometa.get("content_version", bmeta.get("content_version", "1"))

    base_key = f"baselines/{base_sha[:12]}.db"
    ovl_key = f"overlays/overlay_{cv}.sqlite"

    # stage
    if os.path.isdir(dist) and not args.keep_dist:
        shutil.rmtree(dist)
    for sub in ("baselines", "overlays"):
        os.makedirs(os.path.join(dist, sub), exist_ok=True)
    shutil.copy2(args.baseline, os.path.join(dist, base_key))
    shutil.copy2(args.overlay, os.path.join(dist, ovl_key))

    # Source dates: prefer the overlay's content_meta (provenance of the
    # CANDIDATE build = newest) over the baseline's catalog_meta (frozen at
    # the last rebase). The app reads its own DB meta, not this block, so
    # this only keeps the manifest honest.
    def src_date(overlay_key: str, baseline_key: str) -> str:
        return ometa.get(overlay_key) or bmeta.get(baseline_key, "")

    manifest = {
        "schema_version": bmeta["schema_version"],
        "content_version": cv,
        "baseline": {
            "url": args.baseline_url or f"{args.base_url}/{base_key}",
            "sha256": base_sha,
            "size": os.path.getsize(os.path.join(dist, base_key)),
        },
        "overlay": {
            "url": f"{args.base_url}/{ovl_key}",
            "sha256": ovl_sha,
            "size": os.path.getsize(os.path.join(dist, ovl_key)),
            "baseline_sha256": base_sha,
        },
        "sources": {
            "registro": src_date("registro_date", "registro_date"),
            "dispo": src_date("dispo_date", "dispo_date"),
            "classe_ah": src_date("classa_date", "classa_date"),
            "pa": src_date("pa_date", "pa_date"),
        },
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    mpath = os.path.join(dist, "manifest.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"staged {dist}:\n" + json.dumps(manifest, indent=2))

    if args.upload:
        for env in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID",
                    "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
            if not os.environ.get(env):
                raise SystemExit(f"ERROR: env {env} not set for --upload")
        endpoint = os.environ["R2_ENDPOINT"]
        bucket = os.environ["R2_BUCKET"]
        with open(os.path.join(dist, base_key), "rb") as f:
            s3_put(endpoint, bucket, base_key, f.read())
        with open(os.path.join(dist, ovl_key), "rb") as f:
            s3_put(endpoint, bucket, ovl_key, f.read())
        with open(mpath, "rb") as f:
            s3_put(endpoint, bucket, "manifest.json", f.read(),
                   cache_control="max-age=300, must-revalidate")
        if not args.keep_dist:
            shutil.rmtree(dist)


if __name__ == "__main__":
    main()
