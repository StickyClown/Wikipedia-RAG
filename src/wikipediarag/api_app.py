from __future__ import annotations

from wikipediarag.api.app import app, create_app, main
from wikipediarag.api.handlers import *  # noqa: F403
from wikipediarag.api.handlers import run_debug_search as search_debug  # noqa: F401
from wikipediarag.api.handlers import stream_chat_response as chat  # noqa: F401

__all__ = ["app", "create_app", "main"]

if __name__ == "__main__":
    main()
