"""Pluggable file storage layer.

Three backends ship out of the box:
  - "local"    — writes to LOCAL_UPLOAD_DIR on disk; served via /uploads
  - "supabase" — uploads to a Supabase Storage bucket (private), serves files
                 via short-lived signed URLs
  - "s3"       — uploads to a private Amazon S3 bucket, serves files via
                 presigned URLs (same object-key convention as "supabase")

Switch via the `STORAGE_BACKEND` env variable. The rest of the app uses the
`save_uploads()` and `cleanup_ticket_files()` helpers at the bottom of this
file — same signatures regardless of backend.
"""
from __future__ import annotations

import abc
import logging
import os
import re
import shutil
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import HTTPException, UploadFile, status

from ..config import get_settings

logger = logging.getLogger("skposcare.storage")

MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB
ALLOWED_EXT = {"jpg", "jpeg", "png", "gif", "mp4", "mov"}
ALLOWED_MIME_PREFIX = ("image/", "video/")

# Invoice documents accept PDFs in addition to images (no video).
ALLOWED_DOC_EXT = {"pdf", "jpg", "jpeg", "png"}
ALLOWED_DOC_MIME = {"application/pdf"}
ALLOWED_DOC_MIME_PREFIX = ("image/",)

# Public URL prefix that maps to LOCAL_UPLOAD_DIR (must match main.py mount).
LOCAL_URL_PREFIX = "/uploads"


# --------------------------------------------------------------------------- #
# Validation helpers                                                          #
# --------------------------------------------------------------------------- #

def _safe_filename(name: str) -> str:
    """Strip path components and dangerous chars from a user-provided filename."""
    name = os.path.basename(name)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name[:120] or "file"


def _ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def validate_upload(file: UploadFile) -> None:
    """Raise HTTPException(422) if the file looks unacceptable."""
    ext = _ext_of(file.filename or "")
    mime = (file.content_type or "").lower()
    if ext not in ALLOWED_EXT and not mime.startswith(ALLOWED_MIME_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported file type: {file.filename}",
        )


def validate_document_upload(file: UploadFile) -> None:
    """Like validate_upload but for invoice documents — PDF or image only."""
    ext = _ext_of(file.filename or "")
    mime = (file.content_type or "").lower()
    ok = (
        ext in ALLOWED_DOC_EXT
        or mime in ALLOWED_DOC_MIME
        or mime.startswith(ALLOWED_DOC_MIME_PREFIX)
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Only PDF or image files are allowed (got {file.filename}).",
        )


# --------------------------------------------------------------------------- #
# Storage backends                                                            #
# --------------------------------------------------------------------------- #

class Storage(abc.ABC):
    """Pluggable file storage backend."""

    @abc.abstractmethod
    def save(self, file: UploadFile, ticket_reference: str, filename: str) -> dict:
        """Save a single file. Returns metadata for TicketAttachment."""
        ...

    @abc.abstractmethod
    def save_bytes(self, content: bytes, content_type: str, ticket_reference: str, filename: str) -> dict:
        """Save raw bytes (e.g. a signature PNG or generated PDF)."""
        ...

    @abc.abstractmethod
    def public_url(self, stored: str) -> str:
        """Convert the stored value back into a viewable URL."""
        ...

    def cleanup(self, ticket_reference: str) -> None:
        """Best-effort cleanup of a ticket's files. Default: no-op."""
        pass


# --------------------------- Local disk ------------------------------------ #

class LocalStorage(Storage):
    """Writes files under LOCAL_UPLOAD_DIR. Served via the /uploads static mount."""

    def save(self, file: UploadFile, ticket_reference: str, filename: str) -> dict:
        settings = get_settings()
        base = Path(settings.local_upload_dir) / ticket_reference
        base.mkdir(parents=True, exist_ok=True)

        dest = base / filename
        # Don't clobber same-name uploads inside the same ticket.
        i = 1
        stem, dot, ext = filename.rpartition(".")
        while dest.exists():
            new_name = f"{stem}-{i}.{ext}" if dot else f"{filename}-{i}"
            dest = base / new_name
            i += 1

        size = 0
        with dest.open("wb") as fp:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    fp.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"File too large: {file.filename} (max 50 MB)",
                    )
                fp.write(chunk)

        return {
            "filename": dest.name,
            "content_type": file.content_type or "application/octet-stream",
            "size_bytes": size,
            "storage_url": f"{LOCAL_URL_PREFIX}/{ticket_reference}/{dest.name}",
        }

    def save_bytes(self, content: bytes, content_type: str, ticket_reference: str, filename: str) -> dict:
        settings = get_settings()
        base = Path(settings.local_upload_dir) / ticket_reference
        base.mkdir(parents=True, exist_ok=True)
        dest = base / filename
        size = len(content)
        if size > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File too large: {filename} (max 50 MB)",
            )
        dest.write_bytes(content)
        return {
            "filename": filename,
            "content_type": content_type,
            "size_bytes": size,
            "storage_url": f"{LOCAL_URL_PREFIX}/{ticket_reference}/{filename}",
        }

    def public_url(self, stored: str) -> str:
        # In local mode the stored value already IS the URL path.
        return stored

    def cleanup(self, ticket_reference: str) -> None:
        settings = get_settings()
        target = Path(settings.local_upload_dir) / ticket_reference
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)


# --------------------------- Supabase Storage ------------------------------ #

class SupabaseStorage(Storage):
    """Uploads to Supabase Storage (private bucket) and serves via signed URLs.

    Why signed URLs? The bucket is private so anyone with the URL needs a
    short-lived token to view the file. Bucket policy stays "private" → safer
    by default; we just mint a fresh signed URL each time the file is fetched
    (TTL controlled by SUPABASE_SIGNED_URL_TTL_SECONDS).
    """

    def __init__(self) -> None:
        s = get_settings()
        self.url = (s.supabase_url or "").rstrip("/")
        self.bucket = s.supabase_bucket or ""
        self.service_key = s.supabase_service_key or ""
        self.ttl = s.supabase_signed_url_ttl_seconds
        if not self.url or not self.service_key or not self.bucket:
            raise RuntimeError(
                "STORAGE_BACKEND=supabase requires SUPABASE_URL, "
                "SUPABASE_SERVICE_KEY, and SUPABASE_BUCKET to be set."
            )

    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.service_key}",
            "apikey": self.service_key,
        }

    def save(self, file: UploadFile, ticket_reference: str, filename: str) -> dict:
        # Read into memory (max 50MB enforced) - simple and reliable.
        body = file.file.read()
        size = len(body)
        if size > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"File too large: {file.filename} (max 50 MB)",
            )

        key = f"{ticket_reference}/{filename}"
        upload_url = f"{self.url}/storage/v1/object/{self.bucket}/{key}"
        try:
            resp = httpx.post(
                upload_url,
                headers={
                    **self._auth_headers(),
                    "Content-Type": file.content_type or "application/octet-stream",
                    "x-upsert": "true",
                },
                content=body,
                timeout=60.0,
            )
        except httpx.HTTPError as e:
            logger.exception("Supabase upload network error")
            raise HTTPException(status_code=502, detail=f"Storage upload failed: {e}")

        if resp.status_code >= 400:
            logger.error("Supabase upload failed: %s %s", resp.status_code, resp.text[:300])
            raise HTTPException(
                status_code=502,
                detail=f"Supabase upload failed ({resp.status_code}). Check bucket name and service key.",
            )

        logger.info("Uploaded %s to supabase://%s/%s (%d bytes)", filename, self.bucket, key, size)
        return {
            "filename": filename,
            "content_type": file.content_type or "application/octet-stream",
            "size_bytes": size,
            "storage_url": key,  # We store the object key; signed URL is minted on demand.
        }

    def save_bytes(self, content: bytes, content_type: str, ticket_reference: str, filename: str) -> dict:
        size = len(content)
        if size > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File too large: {filename} (max 50 MB)",
            )
        key = f"{ticket_reference}/{filename}"
        upload_url = f"{self.url}/storage/v1/object/{self.bucket}/{key}"
        try:
            resp = httpx.post(
                upload_url,
                headers={
                    **self._auth_headers(),
                    "Content-Type": content_type,
                    "x-upsert": "true",
                },
                content=content,
                timeout=60.0,
            )
        except httpx.HTTPError as e:
            logger.exception("Supabase upload network error")
            raise HTTPException(status_code=502, detail=f"Storage upload failed: {e}")

        if resp.status_code >= 400:
            logger.error("Supabase upload failed: %s %s", resp.status_code, resp.text[:300])
            raise HTTPException(
                status_code=502,
                detail=f"Supabase upload failed ({resp.status_code}).",
            )

        return {
            "filename": filename,
            "content_type": content_type,
            "size_bytes": size,
            "storage_url": key,
        }

    def public_url(self, stored_key: str) -> str:
        """Mint a fresh signed URL for the given object key."""
        sign_url = f"{self.url}/storage/v1/object/sign/{self.bucket}/{stored_key}"
        try:
            resp = httpx.post(
                sign_url,
                headers=self._auth_headers(),
                json={"expiresIn": self.ttl},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            # Supabase has used both spellings across versions.
            signed = data.get("signedURL") or data.get("signedUrl") or ""
            if not signed:
                logger.warning("Supabase sign response missing URL: %s", data)
                return ""
            # Returned value is a path; prefix with the storage base.
            if signed.startswith("/"):
                return f"{self.url}/storage/v1{signed}"
            return signed
        except Exception:
            logger.exception("Failed to mint signed URL for %s", stored_key)
            # Return a non-working URL rather than crashing the request.
            return f"{self.url}/storage/v1/object/{self.bucket}/{stored_key}"


# --------------------------- Amazon S3 ------------------------------------- #

class S3Storage(Storage):
    """Uploads to a private S3 bucket and serves via presigned URLs.

    Mirrors SupabaseStorage 1:1: the DB stores the bare object key
    ("{reference}/{filename}"), and a fresh presigned GET URL is minted each
    time the file is fetched (TTL controlled by S3_SIGNED_URL_TTL_SECONDS).
    Rows migrated from Supabase therefore keep working with no data rewrite.
    """

    def __init__(self) -> None:
        s = get_settings()
        self.bucket = s.s3_bucket or ""
        self.ttl = s.s3_signed_url_ttl_seconds
        if not self.bucket:
            raise RuntimeError("STORAGE_BACKEND=s3 requires S3_BUCKET to be set.")
        try:
            import boto3
            from botocore.config import Config
        except ImportError as e:
            raise RuntimeError(
                "STORAGE_BACKEND=s3 requires boto3 (pip install -r requirements.txt)."
            ) from e
        # Explicit SigV4 + virtual addressing: the default config presigns
        # against the legacy global endpoint, which rejects SigV4 URLs signed
        # for ap-south-1.
        kwargs: dict = {
            "region_name": s.s3_region,
            "config": Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        }
        if s.aws_access_key_id and s.aws_secret_access_key:
            kwargs["aws_access_key_id"] = s.aws_access_key_id
            kwargs["aws_secret_access_key"] = s.aws_secret_access_key
        self.client = boto3.client("s3", **kwargs)

    def _put(self, body: bytes, content_type: str, key: str) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
            )
        except Exception as e:
            logger.exception("S3 upload failed for %s", key)
            raise HTTPException(status_code=502, detail=f"Storage upload failed: {e}")

    def save(self, file: UploadFile, ticket_reference: str, filename: str) -> dict:
        # Read into memory (max 50MB enforced) - simple and reliable.
        body = file.file.read()
        size = len(body)
        if size > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"File too large: {file.filename} (max 50 MB)",
            )
        content_type = file.content_type or "application/octet-stream"
        key = f"{ticket_reference}/{filename}"
        self._put(body, content_type, key)
        logger.info("Uploaded %s to s3://%s/%s (%d bytes)", filename, self.bucket, key, size)
        return {
            "filename": filename,
            "content_type": content_type,
            "size_bytes": size,
            "storage_url": key,  # We store the object key; presigned URL is minted on demand.
        }

    def save_bytes(self, content: bytes, content_type: str, ticket_reference: str, filename: str) -> dict:
        size = len(content)
        if size > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File too large: {filename} (max 50 MB)",
            )
        key = f"{ticket_reference}/{filename}"
        self._put(content, content_type, key)
        return {
            "filename": filename,
            "content_type": content_type,
            "size_bytes": size,
            "storage_url": key,
        }

    def public_url(self, stored_key: str) -> str:
        """Mint a fresh presigned GET URL for the given object key."""
        try:
            return self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": stored_key},
                ExpiresIn=self.ttl,
            )
        except Exception:
            logger.exception("Failed to presign URL for %s", stored_key)
            return ""

    def cleanup(self, ticket_reference: str) -> None:
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{ticket_reference}/"):
                keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                if keys:
                    self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})
        except Exception:
            logger.exception("S3 cleanup failed for %s (ignoring)", ticket_reference)


# --------------------------------------------------------------------------- #
# Factory + module-level public API                                           #
# --------------------------------------------------------------------------- #

_storage_instance: Optional[Storage] = None


def get_storage() -> Storage:
    """Return the storage backend selected by STORAGE_BACKEND. Cached."""
    global _storage_instance
    if _storage_instance is not None:
        return _storage_instance

    backend = (get_settings().storage_backend or "local").strip().lower()
    if backend == "supabase":
        _storage_instance = SupabaseStorage()
    elif backend == "s3":
        _storage_instance = S3Storage()
    elif backend == "local":
        _storage_instance = LocalStorage()
    else:
        raise RuntimeError(
            f"Unknown STORAGE_BACKEND={backend!r}. Use 'local', 'supabase' or 's3'."
        )
    logger.info("Storage backend: %s", backend)
    return _storage_instance


def reset_storage_cache() -> None:
    """Test helper: clear the cached storage instance so the next call rebuilds it."""
    global _storage_instance
    _storage_instance = None


def save_uploads(files: List[UploadFile], ticket_reference: str) -> List[dict]:
    """Persist files via the active backend; return metadata dicts for the DB."""
    if not files:
        return []
    storage = get_storage()
    out: List[dict] = []
    for f in files:
        validate_upload(f)
        safe = _safe_filename(f.filename or "file")
        out.append(storage.save(f, ticket_reference, safe))
    return out


def save_document(file: UploadFile, reference: str, *, prefix: str = "invoice") -> dict:
    """Validate + persist a single document (PDF/image) via the active backend.

    The stored filename is prefixed (e.g. "invoice-…") so it's recognisable in
    the file listing alongside worksite photos. Returns the metadata dict the
    caller stores on the row.
    """
    validate_document_upload(file)
    safe = _safe_filename(file.filename or "document")
    if prefix and not safe.startswith(f"{prefix}-"):
        safe = f"{prefix}-{safe}"
    return get_storage().save(file, reference, safe)


def cleanup_ticket_files(ticket_reference: str) -> None:
    """Remove a ticket's uploaded files (used when DB write fails post-save)."""
    try:
        get_storage().cleanup(ticket_reference)
    except Exception:
        logger.exception("Cleanup failed for %s (ignoring)", ticket_reference)
