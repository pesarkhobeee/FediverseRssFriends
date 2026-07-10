"""OPML parsing — safe against XXE/billion-laughs via defusedxml.

Accepts both nested (category > feeds) and flat (feeds at root) OPMLs.
Returns a flat list of (category, xmlUrl, title, htmlUrl).
"""

from dataclasses import dataclass

import defusedxml.ElementTree as ET


@dataclass(frozen=True)
class OpmlEntry:
    category: str
    xml_url: str
    title: str
    html_url: str


def parse_opml(content: bytes | str) -> list[OpmlEntry]:
    """Return a list of OpmlEntry. Never raises for individual bad outlines;
    raises ValueError only on unparseable XML."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise ValueError(f"invalid OPML: {e}") from e

    body = root.find("body")
    if body is None:
        return []

    out: list[OpmlEntry] = []
    _walk(body, "", out)
    # Dedup across the file itself by xml_url — keep first occurrence's category.
    seen: set[str] = set()
    uniq: list[OpmlEntry] = []
    for e in out:
        if e.xml_url in seen:
            continue
        seen.add(e.xml_url)
        uniq.append(e)
    return uniq


def _walk(node, current_category: str, out: list[OpmlEntry]) -> None:
    for outline in node.findall("outline"):
        xml_url = (outline.get("xmlUrl") or outline.get("xmlurl") or "").strip()
        if xml_url:
            out.append(
                OpmlEntry(
                    category=current_category or "Imported",
                    xml_url=xml_url,
                    title=(outline.get("text") or outline.get("title") or "").strip(),
                    html_url=(outline.get("htmlUrl") or outline.get("htmlurl") or "").strip(),
                )
            )
            # A feed outline may nest garbage — don't recurse into it as a category.
            continue
        # Category outline: recurse with its label as the new category.
        label = (outline.get("text") or outline.get("title") or "").strip()
        _walk(outline, label or current_category, out)
