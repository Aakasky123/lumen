"""Length caps that never cut silently mid-value."""

from __future__ import annotations

import re
from typing import Any

TRUNCATION_MARKER = " …[truncated]"


def trim_marked(value: Any, limit: int) -> str:
    """Whitespace-normalize and cap at ``limit`` chars; an over-long value is cut at a word boundary
    and ends with an explicit truncation marker, so a reader never takes a cut value for the whole."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    limit = max(0, int(limit or 0))
    if len(text) <= limit:
        return text
    room = limit - len(TRUNCATION_MARKER)
    if room <= 0:
        return text[:limit]
    head = text[:room]
    if not text[room].isspace():
        cut = head.rfind(" ")
        if cut > room // 2:
            head = head[:cut]
    return head.rstrip() + TRUNCATION_MARKER
