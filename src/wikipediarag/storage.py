from __future__ import annotations

import boto3

from wikipediarag.config import Settings, get_settings


def put_text(key: str, content: str, settings: Settings | None = None) -> str:
    resolved = settings or get_settings()
    client = boto3.client(
        "s3",
        endpoint_url=resolved.minio_endpoint,
        aws_access_key_id=resolved.minio_access_key,
        aws_secret_access_key=resolved.minio_secret_key,
        region_name="us-east-1",
    )
    existing = [bucket["Name"] for bucket in client.list_buckets().get("Buckets", [])]
    if resolved.minio_bucket not in existing:
        client.create_bucket(Bucket=resolved.minio_bucket)
    client.put_object(
        Bucket=resolved.minio_bucket,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="application/json; charset=utf-8" if key.endswith(".jsonl") else "text/plain",
    )
    return f"s3://{resolved.minio_bucket}/{key}"
