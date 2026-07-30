from __future__ import annotations

from typing import Any

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

from wikipediarag.document_ingestion import extract_metadata_local

app = FastAPI(title="WikipediaRag Metadata Service")


class MetadataExtractRequest(BaseModel):
    text: str = Field(max_length=20000)
    filename: str = Field(default="", max_length=240)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/metadata:extract")
async def extract(payload: MetadataExtractRequest) -> dict[str, Any]:
    return extract_metadata_local(payload.text).model_dump(mode="json")


def main() -> None:
    uvicorn.run("wikipediarag.metadata_app:app", host="0.0.0.0", port=8090)  # noqa: S104


if __name__ == "__main__":
    main()
