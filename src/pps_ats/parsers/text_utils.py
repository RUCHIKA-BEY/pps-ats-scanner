"""Text helpers. Original text is always preserved; normalisation is only for matching."""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections.abc import Iterable

from pps_ats.schemas.content import Span, TextStats

_WORD_RE = re.compile(r"[^\W_]+(?:[-'’.][^\W_]+)*", re.UNICODE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9+#&/ ]+")
_SPACE_RE = re.compile(r"\s+")


def normalize_for_match(text: str) -> str:
    """Lower-case, NFKC, punctuation stripped, whitespace collapsed. For comparisons only."""
    text = unicodedata.normalize("NFKC", text).lower().replace("&", " and ")
    text = _NON_ALNUM_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def count_words(text: str) -> int:
    return len(_WORD_RE.findall(text))


def _is_garbled_char(ch: str) -> bool:
    if ch == "�":
        return True
    code = ord(ch)
    if 0xE000 <= code <= 0xF8FF:  # private use area: typical of fonts without ToUnicode maps
        return True
    return unicodedata.category(ch) == "Cc" and ch not in "\n\r\t"


is_garbled_char = _is_garbled_char  # public name for the analysis layer


def compute_text_stats(text: str, spans: Iterable[Span] = (), line_count: int = 0) -> TextStats:
    visible = [ch for ch in text if not ch.isspace()]
    char_count = len(visible)
    garbled = sum(1 for ch in visible if _is_garbled_char(ch))
    alpha = sum(1 for ch in visible if ch.isalpha())

    weighted_sizes: list[float] = []
    for span in spans:
        if span.size and span.text.strip():
            # weight by length, capped to keep the list small
            weighted_sizes.extend([round(span.size, 1)] * min(len(span.text.strip()), 50))

    return TextStats(
        char_count=char_count,
        word_count=count_words(text),
        line_count=line_count,
        garbled_char_ratio=round(garbled / char_count, 4) if char_count else 0.0,
        alpha_ratio=round(alpha / char_count, 4) if char_count else 0.0,
        body_font_size=statistics.median(weighted_sizes) if weighted_sizes else None,
    )


def rgb_int_to_hex(color: int | None) -> str | None:
    if color is None:
        return None
    return f"#{color & 0xFFFFFF:06x}"


def round_bbox(bbox: Iterable[float]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = (round(float(v), 2) for v in bbox)
    return (x0, y0, x1, y1)
