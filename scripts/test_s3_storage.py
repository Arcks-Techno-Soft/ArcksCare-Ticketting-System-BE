"""Standalone test for the S3 storage backend (no AWS account needed).

Runs against moto's in-memory S3. Usage:
    pip install "moto[s3]"    # dev-only dependency
    python scripts/test_s3_storage.py

Verifies: factory selection, save()/save_bytes() metadata + key convention,
50 MB limit, presigned public_url() shape + TTL, cleanup() prefix deletion,
and that stored keys are byte-identical to the Supabase convention
("{reference}/{filename}") so no DB migration is needed.
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Configure BEFORE importing the app modules (Settings reads env at import use).
os.environ.update(
    {
        "STORAGE_BACKEND": "s3",
        "S3_BUCKET": "test-skposcare-uploads",
        "S3_REGION": "ap-south-1",
        "S3_SIGNED_URL_TTL_SECONDS": "604800",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "ap-south-1",
    }
)

from moto import mock_aws  # noqa: E402

import boto3  # noqa: E402
from fastapi import HTTPException, UploadFile  # noqa: E402

from app.services import storage as st  # noqa: E402

PASS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        raise SystemExit(f"Test failed: {name} {extra}")
    PASS += 1


def upload_file(name: str, content: bytes, ctype: str) -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=name, headers={"content-type": ctype})


@mock_aws
def main() -> None:
    s3 = boto3.client("s3", region_name="ap-south-1")
    s3.create_bucket(
        Bucket="test-skposcare-uploads",
        CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
    )

    st.reset_storage_cache()
    backend = st.get_storage()
    check("factory returns S3Storage", type(backend).__name__ == "S3Storage")

    # ---- save() ----
    meta = st.save_uploads([upload_file("photo.jpg", b"jpegdata" * 100, "image/jpeg")], "AC-2026-00042")[0]
    check("save(): key convention matches Supabase", meta["storage_url"] == "AC-2026-00042/photo.jpg", meta["storage_url"])
    check("save(): size recorded", meta["size_bytes"] == 800)
    check("save(): content_type recorded", meta["content_type"] == "image/jpeg")
    obj = s3.get_object(Bucket="test-skposcare-uploads", Key="AC-2026-00042/photo.jpg")
    check("save(): object actually in S3", obj["Body"].read() == b"jpegdata" * 100)
    check("save(): ContentType stored", obj["ContentType"] == "image/jpeg")

    # ---- save_bytes() (signature PNGs / generated PDFs) ----
    meta2 = backend.save_bytes(b"%PDF-1.4 fake", "application/pdf", "AC-2026-00042", "resolution.pdf")
    check("save_bytes(): key convention", meta2["storage_url"] == "AC-2026-00042/resolution.pdf")

    # ---- save_document() prefixing ----
    meta3 = st.save_document(upload_file("bill.pdf", b"%PDF-1.4", "application/pdf"), "INST-2026-00007")
    check("save_document(): invoice- prefix", meta3["storage_url"] == "INST-2026-00007/invoice-bill.pdf", meta3["storage_url"])

    # ---- 50 MB limit ----
    try:
        backend.save_bytes(b"x" * (st.MAX_FILE_BYTES + 1), "image/png", "AC-2026-00042", "big.png")
        check("50MB limit enforced", False)
    except HTTPException as e:
        check("50MB limit enforced", e.status_code == 413, f"status={e.status_code}")

    # ---- presigned URL ----
    url = backend.public_url("AC-2026-00042/photo.jpg")
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    check("public_url(): https + bucket host", parsed.scheme == "https" and "test-skposcare-uploads" in parsed.netloc, parsed.netloc)
    check("public_url(): SigV4 signature present", "X-Amz-Signature" in qs and "X-Amz-Credential" in qs)
    check("public_url(): 7-day TTL", qs.get("X-Amz-Expires", ["0"])[0] == "604800", str(qs.get("X-Amz-Expires")))
    check("public_url(): key preserved", parsed.path.endswith("/AC-2026-00042/photo.jpg"), parsed.path)

    # ---- cleanup() ----
    backend.cleanup("AC-2026-00042")
    left = s3.list_objects_v2(Bucket="test-skposcare-uploads", Prefix="AC-2026-00042/")
    check("cleanup(): ticket prefix emptied", left.get("KeyCount", 0) == 0)
    other = s3.list_objects_v2(Bucket="test-skposcare-uploads", Prefix="INST-2026-00007/")
    check("cleanup(): other references untouched", other.get("KeyCount", 0) == 1)

    # ---- cleanup on missing prefix is a silent no-op ----
    backend.cleanup("AC-0000-00000")
    check("cleanup(): missing prefix is safe", True)

    print(f"\nAll {PASS} checks passed.")


if __name__ == "__main__":
    main()
