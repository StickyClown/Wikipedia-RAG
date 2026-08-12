from __future__ import annotations

from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from wikipediarag.config import Settings, get_settings


def _client(settings: Settings, *, endpoint_url: str | None = None) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url or settings.minio_endpoint,
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        region_name="us-east-1",
        # Older MinIO deployments require Content-MD5 for multi-object delete.
        config=Config(request_checksum_calculation="when_required"),
    )


def ensure_bucket(settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    client = _client(resolved)
    existing = [bucket["Name"] for bucket in client.list_buckets().get("Buckets", [])]
    if resolved.minio_bucket not in existing:
        client.create_bucket(Bucket=resolved.minio_bucket)
    try:
        client.put_bucket_cors(
            Bucket=resolved.minio_bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedMethods": ["PUT", "GET"],
                        "AllowedOrigins": ["*"],
                        "AllowedHeaders": ["*"],
                        "ExposeHeaders": ["ETag"],
                        "MaxAgeSeconds": 3000,
                    }
                ]
            },
        )
    except ClientError:
        return


def put_text(key: str, content: str, settings: Settings | None = None) -> str:
    resolved = settings or get_settings()
    ensure_bucket(resolved)
    client = _client(resolved)
    client.put_object(
        Bucket=resolved.minio_bucket,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="application/json; charset=utf-8"
        if key.endswith((".json", ".jsonl"))
        else "text/plain; charset=utf-8",
    )
    return f"s3://{resolved.minio_bucket}/{key}"


def put_bytes(
    key: str,
    content: bytes,
    *,
    content_type: str = "application/octet-stream",
    settings: Settings | None = None,
) -> str:
    resolved = settings or get_settings()
    ensure_bucket(resolved)
    client = _client(resolved)
    client.put_object(Bucket=resolved.minio_bucket, Key=key, Body=content, ContentType=content_type)
    return f"s3://{resolved.minio_bucket}/{key}"


def get_bytes(key: str, settings: Settings | None = None) -> bytes:
    resolved = settings or get_settings()
    client = _client(resolved)
    response = client.get_object(Bucket=resolved.minio_bucket, Key=key)
    body = response["Body"].read()
    return bytes(body)


def head_object(key: str, settings: Settings | None = None) -> dict[str, Any]:
    resolved = settings or get_settings()
    client = _client(resolved)
    response = client.head_object(Bucket=resolved.minio_bucket, Key=key)
    return {
        "content_length": int(response.get("ContentLength") or 0),
        "content_type": str(response.get("ContentType") or ""),
        "etag": str(response.get("ETag") or "").strip('"'),
        "last_modified": response.get("LastModified"),
    }


def delete_objects(keys: list[str], settings: Settings | None = None) -> int:
    resolved = settings or get_settings()
    unique_keys = sorted({key for key in keys if key})
    if not unique_keys:
        return 0
    client = _client(resolved)
    deleted = 0
    for index in range(0, len(unique_keys), 1000):
        batch = unique_keys[index : index + 1000]
        try:
            response = client.delete_objects(
                Bucket=resolved.minio_bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
        except ClientError as exc:
            # Some older MinIO versions require Content-MD5 for the XML body of
            # DeleteObjects, while boto3 does not add it for this operation.
            # A single-object delete has no XML body and is portable across the
            # supported S3-compatible stores.
            if str(exc.response.get("Error", {}).get("Code") or "") != "MissingContentMD5":
                raise
            for key in batch:
                client.delete_object(Bucket=resolved.minio_bucket, Key=key)
            deleted += len(batch)
            continue
        errors = response.get("Errors", [])
        blocking_errors = [error for error in errors if error.get("Code") not in {"NoSuchKey", "NotFound"}]
        if blocking_errors:
            first = blocking_errors[0]
            raise ClientError(
                {
                    "Error": {
                        "Code": str(first.get("Code") or "DeleteObjectsFailed"),
                        "Message": "object deletion failed",
                    }
                },
                "DeleteObjects",
            )
        deleted += len(batch) - len(blocking_errors)
    return deleted


def create_presigned_put_url(
    key: str,
    *,
    content_type: str,
    expires_seconds: int,
    settings: Settings | None = None,
) -> str:
    resolved = settings or get_settings()
    ensure_bucket(resolved)
    client = _client(resolved, endpoint_url=resolved.minio_public_endpoint)
    return str(
        client.generate_presigned_url(
            "put_object",
            Params={"Bucket": resolved.minio_bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires_seconds,
        )
    )
