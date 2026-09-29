"""Plain-text reduction for third-party strings that reach a trading prompt (§7.18).

Calendar titles, venue notices and summarizer catalysts are external text. Before
any of it is rendered into the prompt it is reduced to a conservative character
set (no markup, braces, brackets or quotes that could imitate prompt structure or
JSON) and length-capped.
"""

from __future__ import annotations

import re

_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9 /%.,()&+:'\-]")
_TAG_RE = re.compile(r"<[^>]*>")


def safe_label(text: str, limit: int = 120) -> str:
    """Tags dropped, unusual characters blanked, whitespace collapsed, ``limit`` chars."""
    cleaned = _SAFE_LABEL_RE.sub(" ", _TAG_RE.sub(" ", text))
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit].rstrip()
