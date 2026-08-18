"""One-off sync of a Supabase Storage bucket into an S3 bucket.

Used for the AWS migration (see AWS_MIGRATION_PLAN.md §7 Phase 3):
  1. current prod uploads  — Supabase Pro project → arckscare-uploads-prod
  2. the 747 MB old-photo backlog still sitting in the dead free-tier project,
     copied straight to S3 (skipping Supabase Pro entirely)

Object keys are preserved exactly ("{reference}/{filename}"), so DB rows that
store bare object keys keep working with no data rewrite. Re-runnable: objects
already in S3 with the same byte size are skipped, so a final delta sync during
the cutover window is just a re-run.

Usage:
  python scripts/sync_supabase_storage_to_s3.py \
      --supabase-url https://<project>.supabase.co \
      --supabase-bucket <bucket> \
      --s3-bucket arckscare-uploads-prod \
      [--region ap-south-1] [--prefix TKT-2026] [--dry-run]

Credentials:
  SUPABASE_SERVICE_KEY  env var (service_role key of the SOURCE project)
  AWS credentials       via the default boto3 chain (env vars / ~/.aws)
"""
from __future__ import annotations

import argparse
import os
import sys

import boto3
import httpx

PAGE_SIZE = 1000


def list_supabase_objects(client: httpx.Client, base: str, bucket: str, prefix: str) -> list[str]:
    """Recursively list all object keys under `prefix` (Supabase lists per folder)."""
    keys: list[str] = []
    offset = 0
    while True:
        resp = client.post(
            f"{base}/storage/v1/object/list/{bucket}",
            json={
                "prefix": prefix,
                "limit": PAGE_SIZE,
                "offset": offset,
                "sortBy": {"column": "name", "order": "asc"},
            },
        )
        resp.raise_for_status()
        entries = resp.json()
        for entry in entries:
            name = entry.get("name") or ""
            full = f"{prefix}/{name}" if prefix else name
            if entry.get("id") is None:  # folders come back with id=None
                keys.extend(list_supabase_objects(client, base, bucket, full))
            else:
                keys.append(full)
        if len(entries) < PAGE_SIZE:
            return keys
        offset += PAGE_SIZE


def s3_existing_sizes(s3, bucket: str, prefix: str) -> dict[str, int]:
    sizes: dict[str, int] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            sizes[obj["Key"]] = obj["Size"]
    return sizes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--supabase-url", required=True)
    ap.add_argument("--supabase-bucket", required=True)
    ap.add_argument("--s3-bucket", required=True)
    ap.add_argument("--region", default="ap-south-1")
    ap.add_argument("--prefix", default="", help="only sync keys under this prefix")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not service_key:
        print("ERROR: set SUPABASE_SERVICE_KEY (service_role key of the source project)")
        return 1

    base = args.supabase_url.rstrip("/")
    client = httpx.Client(
        headers={"Authorization": f"Bearer {service_key}", "apikey": service_key},
        timeout=120.0,
    )
    s3 = boto3.client("s3", region_name=args.region)

    print(f"Listing supabase://{args.supabase_bucket}/{args.prefix} ...")
    keys = list_supabase_objects(client, base, args.supabase_bucket, args.prefix)
    print(f"  {len(keys)} objects in source")
    existing = s3_existing_sizes(s3, args.s3_bucket, args.prefix)
    print(f"  {len(existing)} objects already in s3://{args.s3_bucket}/{args.prefix}")

    copied = skipped = failed = 0
    total_bytes = 0
    for i, key in enumerate(keys, 1):
        try:
            resp = client.get(f"{base}/storage/v1/object/{args.supabase_bucket}/{key}")
            resp.raise_for_status()
            body = resp.content
            if existing.get(key) == len(body):
                skipped += 1
                continue
            if args.dry_run:
                print(f"[dry-run] would copy {key} ({len(body)} bytes)")
                copied += 1
                continue
            s3.put_object(
                Bucket=args.s3_bucket,
                Key=key,
                Body=body,
                ContentType=resp.headers.get("content-type", "application/octet-stream"),
            )
            copied += 1
            total_bytes += len(body)
            if copied % 50 == 0:
                print(f"  ... {copied} copied ({total_bytes / 1e6:.0f} MB), {i}/{len(keys)} scanned")
        except Exception as e:
            failed += 1
            print(f"FAILED {key}: {e}")

    print(f"\nDone: {copied} copied ({total_bytes / 1e6:.1f} MB), {skipped} skipped (already present), {failed} failed")
    if failed:
        print("Re-run to retry failures — already-copied objects are skipped.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
