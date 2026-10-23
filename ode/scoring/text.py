"""Small text helpers shared by scoring, collectors and reports."""

from __future__ import annotations

import re

_DISAMBIG = re.compile(r"\s*\([^)]*\)\s*$")


def clean_query(name: str) -> str:
    """'Obsidian (software)' -> 'Obsidian' (a search-friendly query)."""
    return _DISAMBIG.sub("", name).strip() or name
