"""Live local Gateway verification for the active immutable model revision."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import asyncpg
import pytest

from wikipediarag.config import Settings
from wikipediarag.model_client import chat_completion, embeddings, rerank


def _settings() -> Settings:
    return Settings(
        database_url=os.getenv(
            "WIKIPEDIARAG_FUNCTIONAL_DATABASE_URL",
            "postgresql+asyncpg://rag:change-me-local-only@localhost:5432/rag",
        ),
        model_gateway_url=os.getenv("WIKIPEDIARAG_FUNCTIONAL_GATEWAY", "http://localhost:8081"),
    )


def _snapshot(connection_id: str) -> dict[str, Any]:
    aliases = {
        "mock_generator_main": {
            "alias": "mock_generator_main",
            "provider": "mock",
            "provider_model": "generator_fast",
            "operation": "chat",
            "connection_id": connection_id,
            "request_adapter": {},
            "request_defaults": {},
            "model_defaults": {},
        },
        "mock_embed_default": {
            "alias": "mock_embed_default",
            "provider": "mock",
            "provider_model": "embed_default",
            "operation": "embedding",
            "dimensions": 64,
            "connection_id": connection_id,
            "request_adapter": {},
            "request_defaults": {},
            "model_defaults": {},
        },
        "mock_rerank_default": {
            "alias": "mock_rerank_default",
            "provider": "mock",
            "provider_model": "rerank_default",
            "operation": "rerank",
            "connection_id": connection_id,
            "request_adapter": {},
            "request_defaults": {},
            "model_defaults": {},
        },
    }
    return {"source": "functional-test", "aliases": aliases, "stages": {}}


@pytest.mark.asyncio
async def test_active_revision_is_the_observable_gateway_contract() -> None:
    settings = _settings()
    revision_id = str(uuid.uuid4())
    config_hash = "functional-" + uuid.uuid4().hex
    connection = await asyncpg.connect(settings.database_url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        connection_id = await connection.fetchval(
            "SELECT id FROM model_provider_connections WHERE name='bootstrap-mock' AND enabled=true"
        )
        if connection_id is None:
            pytest.fail("BLOCKED: local mock connection is unavailable")
        snapshot = _snapshot(str(connection_id))
        previous_rows = await connection.fetch(
            "SELECT id, status FROM model_configuration_revisions WHERE status='active'"
        )
        await connection.execute("UPDATE model_configuration_revisions SET status='archived' WHERE status='active'")
        number = int(
            await connection.fetchval("SELECT COALESCE(MAX(revision), 0) + 1 FROM model_configuration_revisions")
        )
        await connection.execute(
            "INSERT INTO model_configuration_revisions(id, revision, status, config_hash, resolved_snapshot) "
            "VALUES ($1, $2, 'active', $3, $4::jsonb)",
            revision_id,
            number,
            config_hash,
            json.dumps(snapshot),
        )
    finally:
        await connection.close()
    try:
        chat = await chat_completion(
            [{"role": "user", "content": "functional config marker"}], settings, alias="mock_generator_main"
        )
        vectors, embedding_usage = await embeddings(["functional config marker"], settings, alias="mock_embed_default")
        ranked = await rerank("marker", ["marker", "other"], settings, alias="mock_rerank_default")
        assert vectors and ranked.get("results")
        for metadata in (
            chat["_gateway_metadata"],
            embedding_usage["_gateway_metadata"],
            ranked["_gateway_metadata"],
        ):
            runtime = metadata["runtime_config"]
            assert runtime["resolution_source"] == "database_revision"
            assert runtime["config_revision_id"] == revision_id
            assert runtime["config_hash"] == config_hash
            assert runtime["provider"] == "mock"
    finally:
        connection = await asyncpg.connect(settings.database_url.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            await connection.execute("DELETE FROM model_configuration_revisions WHERE id=$1", revision_id)
            for row in previous_rows:
                await connection.execute(
                    "UPDATE model_configuration_revisions SET status=$1 WHERE id=$2", row["status"], row["id"]
                )
        finally:
            await connection.close()
