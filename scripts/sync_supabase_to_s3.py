#!/usr/bin/env python3
"""Copy every object from the Supabase Storage bucket to the S3 uploads bucket.

Idempotent: objects already present in S3 with the same byte size are skipped,
so the first run does the bulk copy and the cutover-night run is a fast delta
sync. Keys are preserved exactly ("AC-2026-00001/photo.jpg"), so database rows
need no rewriting.

Usage (from the VM or a laptop with python3):
    pip install httpx boto3
    export SUPABASE_URL=https://<project>.supabase.co
    export SUPABASE_SERVICE_KEY=eyJ...          # service_role secret
    export SUPABASE_BUCKET=sk-pos-care-uploads
    export S3_BUCKET=<uploads bucket>
    export S3_REGION=ap-south-1
    export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
    python3 sync_supabase_to_s3.py            # add --dry-run to preview
"""
from __future__ import annotations

import os
import sys

import boto3
import httpx

DRY = "--dry-run" in sys.argv


def env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"Missing env var: {name}")
    return v


SUPABASE_URL = env("SUPABASE_URL").rstrip("/")
SERVICE_KEY = env("SUPABASE_SERVICE_KEY")
SB_BUCKET = env("SUPABASE_BUCKET")
S3_BUCKET = env("S3_BUCKET")
S3_REGION = os.environ.get("S3_REGION", "ap-south-1")

HEADERS = {"Authorization": f"Bearer {SERVICE_KEY}", "apikey": SERVICE_KEY}
s3 = boto3.client("s3", region_name=S3_REGION)
http = httpx.Client(timeout=120.0)


def list_prefix(prefix: str) -> list[dict]:
    """List one folder level in Supabase Storage (paginated)."""
    out, offset = [], 0
    while True:
        resp = http.post(
            f"{SUPABASE_URL}/storage/v1/object/list/{SB_BUCKET}",
            headers=HEADERS,
            json={
                "prefix": prefix,
                "limit": 1000,
                "offset": offset,
                "sortBy": {"column": "name", "order": "asc"},
            },
        )
        resp.raise_for_status()
        batch = resp.json()
        out.extend(batch)
        if len(batch) < 1000:
            return out
        offset += 1000


def walk(prefix: str = "") -> list[tuple[str, int, str]]:
    """Recursively collect (key, size, content_type) for every object."""
    files: list[tuple[str, int, str]] = []
    for entry in list_prefix(prefix):
        name = entry.get("name") or ""
        full = f"{prefix}/{name}".strip("/") if prefix else name
        if entry.get("id") is None:  # folder
            files.extend(walk(full))
        else:
            meta = entry.get("metadata") or {}
            files.append((full, int(meta.get("size") or 0), meta.get("mimetype") or "application/octet-stream"))
    return files


def s3_sizes() -> dict[str, int]:
    sizes: dict[str, int] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET):
        for o in page.get("Contents", []):
            sizes[o["Key"]] = o["Size"]
    return sizes


def main() -> None:
    print(f"Listing supabase://{SB_BUCKET} ...")
    files = walk()
    print(f"  {len(files)} objects, {sum(s for _, s, _ in files)/1e6:.1f} MB total")
    existing = s3_sizes()
    print(f"Already in s3://{S3_BUCKET}: {len(existing)} objects")

    copied = skipped = failed = 0
    for i, (key, size, ctype) in enumerate(files, 1):
        if existing.get(key) == size and size > 0:
            skipped += 1
            continue
        if DRY:
            print(f"  [dry] would copy {key} ({size} B, {ctype})")
            copied += 1
            continue
        try:
            r = http.get(f"{SUPABASE_URL}/storage/v1/object/{SB_BUCKET}/{key}", headers=HEADERS)
            r.raise_for_status()
            s3.put_object(
                Bucket=S3_BUCKET,
                Key=key,
                Body=r.content,
                ContentType=r.headers.get("content-type", ctype),
            )
            copied += 1
            print(f"  [{i}/{len(files)}] copied {key} ({len(r.content)} B)")
        except Exception as e:  # noqa: BLE001 — report and continue
            failed += 1
            print(f"  [{i}/{len(files)}] FAILED {key}: {e}")

    print(f"\nDone. copied={copied} skipped(existing)={skipped} failed={failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
