from __future__ import annotations

import re
from dataclasses import dataclass

import mwparserfromhell

HEADING_RE = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$", re.MULTILINE)
TABLE_RE = re.compile(r"\{\|.*?\|\}", re.DOTALL)
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass(frozen=True)
class Section:
    path: tuple[str, ...]
    heading: str
    wikitext: str
    clean_text: str


def clean_wikitext(text: str) -> str:
    without_comments = COMMENT_RE.sub("", text)
    with_tables = TABLE_RE.sub(lambda match: _table_to_text(match.group(0)), without_comments)
    parsed = mwparserfromhell.parse(with_tables)
    stripped = parsed.strip_code(normalize=True, collapse=True)
    stripped = re.sub(r"\[https?://[^\s\]]+\s+([^\]]+)\]", r"\1", stripped)
    stripped = re.sub(r"\[https?://[^\s\]]+\]", "", stripped)
    stripped = stripped.replace("&nbsp;", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in stripped.splitlines()]
    kept = [line for line in lines if line and not line.startswith("__")]
    return "\n".join(kept)


def extract_sections(title: str, text: str) -> list[Section]:
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        clean = clean_wikitext(text)
        return [Section(path=(title,), heading=title, wikitext=text, clean_text=clean)] if clean else []

    sections: list[Section] = []
    intro = text[: matches[0].start()]
    intro_clean = clean_wikitext(intro)
    if intro_clean:
        sections.append(Section(path=(title,), heading=title, wikitext=intro, clean_text=intro_clean))

    stack: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        level = len(match.group(1))
        heading = clean_wikitext(match.group(2)) or match.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end]
        clean = clean_wikitext(body)
        if clean:
            sections.append(
                Section(
                    path=(title, *[item[1] for item in stack]),
                    heading=heading,
                    wikitext=body,
                    clean_text=clean,
                )
            )
    return sections


def _table_to_text(table: str) -> str:
    lines: list[str] = []
    for raw_line in table.splitlines():
        line = raw_line.strip()
        if line.startswith("|+") or line.startswith("!"):
            cells = re.split(r"!!|\|\|", line.lstrip("|+! "))
            lines.append(" | ".join(cell.strip() for cell in cells if cell.strip()))
        elif line.startswith("|") and not line.startswith("|-") and not line.startswith("|}"):
            cells = re.split(r"\|\|", line.lstrip("| "))
            lines.append(" | ".join(cell.strip() for cell in cells if cell.strip()))
    return "\n".join(lines)
