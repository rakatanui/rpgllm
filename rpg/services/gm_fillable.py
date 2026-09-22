"""Explicit author-controlled creative delegation for the model GM.

[[GM_FILLABLE]]...[[/GM_FILLABLE]] marks a narrow subject/source whose missing
pre-existing details may be invented by the model GM when needed. The markup is
control metadata for the GM and is stripped from player-facing model context.
"""
from __future__ import annotations

import re


GM_FILLABLE_OPEN = "[[GM_FILLABLE]]"
GM_FILLABLE_CLOSE = "[[/GM_FILLABLE]]"

_GM_FILLABLE_RE = re.compile(
    r"\[\[GM_FILLABLE\]\]([\s\S]*?)\[\[/GM_FILLABLE\]\]",
    flags=re.IGNORECASE,
)


def extract_gm_fillable_scopes(text: str) -> list[str]:
    return [
        match.group(1).strip()
        for match in _GM_FILLABLE_RE.finditer(text or "")
        if match.group(1).strip()
    ]


def has_gm_fillable_scope(text: str) -> bool:
    return bool(_GM_FILLABLE_RE.search(text or ""))


def strip_gm_fillable_markers(text: str) -> str:
    """Remove control markers while preserving the human-readable inner text."""
    return _GM_FILLABLE_RE.sub(lambda match: match.group(1), text or "")
