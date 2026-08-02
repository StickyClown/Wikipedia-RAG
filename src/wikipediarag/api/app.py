from __future__ import annotations

from typing import cast

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ExceptionHandler

from wikipediarag.api import handlers
from wikipediarag.api.routers import (
    admin,
    auth,
    chat,
    documents,
    health,
    ingestion_jobs,
    knowledge_bases,
    query_runs,
    search,
    sources,
    uploads,
)
from wikipediarag.auth import AuthorizationError
from wikipediarag.auth_service import AuthenticationError

ROUTERS = (
    health.router,
    auth.router,
    admin.router,
    knowledge_bases.router,
    sources.router,
    ingestion_jobs.router,
    uploads.router,
    documents.router,
    search.router,
    chat.router,
    query_runs.router,
)


def create_app() -> FastAPI:
    """Create the public FastAPI app and attach domain routers and safe error handlers."""
    app = FastAPI(title="WikipediaRag API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(HTTPException, cast(ExceptionHandler, handlers.http_exception_handler))
    app.add_exception_handler(
        RequestValidationError,
        cast(ExceptionHandler, handlers.request_validation_exception_handler),
    )
    app.add_exception_handler(AuthenticationError, cast(ExceptionHandler, handlers.authentication_exception_handler))
    app.add_exception_handler(AuthorizationError, cast(ExceptionHandler, handlers.authorization_exception_handler))
    app.router.add_event_handler("startup", handlers.startup)
    for router in ROUTERS:
        app.include_router(router)
    return app


app = create_app()


def main() -> None:
    """Run the API service with uvicorn for the console script entrypoint."""
    uvicorn.run("wikipediarag.api_app:app", host="0.0.0.0", port=8000)  # noqa: S104
