from __future__ import annotations

from typing import Literal

ResearchToolName = Literal[
    "extended_search",
    "document_section_lookup",
    "search_within_document",
    "table_csv_lookup",
    "metadata_lookup",
]
ResearchToolMode = Literal["all_local_tools", "extended_search_only", "search_plus_document_tools"]

ALL_RESEARCH_TOOLS: tuple[ResearchToolName, ...] = (
    "extended_search",
    "document_section_lookup",
    "search_within_document",
    "table_csv_lookup",
    "metadata_lookup",
)
DOCUMENT_RESEARCH_TOOLS = frozenset(
    {"document_section_lookup", "search_within_document", "table_csv_lookup", "metadata_lookup"}
)
DEFAULT_RESEARCH_TOOL_MODE: ResearchToolMode = "all_local_tools"
TOOL_MODE_ALLOWLISTS: dict[ResearchToolMode, tuple[ResearchToolName, ...]] = {
    "all_local_tools": ALL_RESEARCH_TOOLS,
    "extended_search_only": ("extended_search",),
    "search_plus_document_tools": (
        "extended_search",
        "document_section_lookup",
        "search_within_document",
    ),
}
ALLOWED_RESEARCH_TOOLS = frozenset(ALL_RESEARCH_TOOLS)


def normalize_research_tool_mode(tool_mode: str | None) -> ResearchToolMode:
    normalized = str(tool_mode or DEFAULT_RESEARCH_TOOL_MODE).strip() or DEFAULT_RESEARCH_TOOL_MODE
    if normalized not in TOOL_MODE_ALLOWLISTS:
        raise ValueError(f"unknown deep research tool_mode: {normalized}")
    return normalized


def allowed_research_tools_for_mode(tool_mode: str | None) -> tuple[ResearchToolName, ...]:
    return TOOL_MODE_ALLOWLISTS[normalize_research_tool_mode(tool_mode)]


def normalize_allowed_research_tools(
    allowed_tools: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None,
) -> tuple[ResearchToolName, ...]:
    if allowed_tools is None:
        return ALL_RESEARCH_TOOLS
    normalized: list[ResearchToolName] = []
    seen: set[str] = set()
    for tool_name in allowed_tools:
        if tool_name not in ALLOWED_RESEARCH_TOOLS:
            raise ValueError(f"research tool is not allowed: {tool_name}")
        if tool_name in seen:
            continue
        normalized.append(tool_name)
        seen.add(tool_name)
    if not normalized:
        raise ValueError("allowed research tool list cannot be empty")
    return tuple(normalized)


def is_document_research_tool(tool_name: str) -> bool:
    return tool_name in DOCUMENT_RESEARCH_TOOLS
