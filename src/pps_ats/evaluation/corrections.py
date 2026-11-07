"""Extract teacher corrections from a corrected PDF (read-only).

Correction channels, from most to least machine-readable:

=================  ===========================================================  ===========
channel            how it is read                                               review
=================  ===========================================================  ===========
text markup        Highlight / Underline / StrikeOut / Squiggly annotations:    auto if the
                   quoted words come from the annotation's quad points; the     comment maps
                   comment from the annotation or its reply                     to one label
note               Text (sticky note) / FreeText annotations: comment only,     auto if one
                   location = the note position                                 label
red text           red spans in the page content that do NOT occur in the      always
                   student's original (evidence of being added). Red text that   manual
                   also occurs in the original is the student's own styling
                   and is listed as ignored
drawing            Ink / Square / Circle / Line / Polygon annotations and red   always
                   vector drawings: only a region and nearby words              manual
scanned page       a page with no text layer (handwritten corrections)          always
                                                                                manual
=================  ===========================================================  ===========

Nothing is inferred beyond the evidence: a label is only *suggested*, and any candidate
whose meaning depends on interpretation is marked ``needs_review``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from pps_ats.config import CorrectionImportSettings

MARKUP = {"Highlight", "Underline", "StrikeOut", "Squiggly"}
NOTES = {"Text", "FreeText"}
SHAPES = {"Ink", "Square", "Circle", "Line", "Polygon", "PolyLine"}
IGNORED_TYPES = {"Popup", "Link", "Widget"}
MARK_RE = re.compile(
    r"(?P<what>content|relevance|structure|format\w*|language|tone|total|overall|marks?|score)?"
    r"[^0-9\n]{0,15}?(?P<got>\d{1,2}(?:\.\d)?)\s*(?:/|out of)\s*(?P<max>10|30)\b",
    re.IGNORECASE,
)


@dataclass
class Candidate:
    """One possible teacher correction, with every piece of evidence kept."""

    channel: str  # markup | note | red_text | drawing | scanned_page
    page: int
    annotation_type: str | None = None
    quoted_text: str | None = None  # resume text the correction refers to
    comment: str | None = None  # the teacher's words
    correction: str | None = None  # replacement text, if the teacher gave one
    author: str | None = None
    created: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    context: str | None = None  # nearby resume text (for manual review)
    evidence: list[str] = field(default_factory=list)
    marks_mentioned: list[str] = field(default_factory=list)  # never imported automatically


@dataclass
class ExtractionResult:
    candidates: list[Candidate]
    page_count: int
    scanned_pages: list[int]
    ignored: list[str]  # e.g. student's own red text, link annotations
    problems: list[str]


def _is_red(rgb: tuple[float, ...] | None, cfg: CorrectionImportSettings) -> bool:
    if not rgb or len(rgb) < 3:
        return False
    r, g, b = rgb[:3]
    return r >= cfg.red_min and g <= cfg.red_max_other and b <= cfg.red_max_other


def _int_rgb(color: int) -> tuple[float, float, float]:
    return ((color >> 16) & 255) / 255, ((color >> 8) & 255) / 255, (color & 255) / 255


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def _clean(text: str | None) -> str | None:
    text = " ".join((text or "").split())
    return text or None


def _quads(annot: pymupdf.Annot) -> list[pymupdf.Rect]:
    pts = annot.vertices or []
    rects = []
    for i in range(0, len(pts) - 3, 4):
        xs = [p[0] for p in pts[i : i + 4]]
        ys = [p[1] for p in pts[i : i + 4]]
        rects.append(pymupdf.Rect(min(xs), min(ys), max(xs), max(ys)))
    return rects or [pymupdf.Rect(annot.rect)]


def _words_in(words: list[tuple], rects: list[pymupdf.Rect]) -> str | None:
    """Words whose box lies mostly inside one of ``rects`` (reading order)."""
    picked = []
    for w in words:
        box = pymupdf.Rect(w[:4])
        area = max(box.get_area(), 1e-6)
        if any((box & r).get_area() / area > 0.5 for r in rects if not (box & r).is_empty):
            picked.append(w)
    picked.sort(key=lambda w: (w[5], w[6], w[7]))  # block, line, word
    return _clean(" ".join(w[4] for w in picked))


def _near(words: list[tuple], rect: pymupdf.Rect, pad: float = 12) -> str | None:
    zone = rect + (-pad, -pad, pad, pad)
    return _words_in(words, [zone])


def marks_in(text: str | None) -> list[str]:
    return [m.group(0).strip(" ,;:.-") for m in MARK_RE.finditer(text or "")]


def _inside_annotation(rect: pymupdf.Rect, annot_rects: list[pymupdf.Rect]) -> bool:
    """PyMuPDF also reports annotation appearances (strike lines, ink, FreeText text) as
    page drawings/text; those are already captured as annotations."""
    centre = pymupdf.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
    return any(centre in (r + (-2, -2, 2, 2)) for r in annot_rects)


def original_text_by_page(path: Path) -> tuple[list[str], str]:
    """Normalised text per page (PDF) and for the whole document (PDF or DOCX)."""
    if path.suffix.lower() == ".pdf":
        with pymupdf.open(path) as doc:
            pages = [_norm(p.get_text()) for p in doc]
        return pages, " ".join(pages)
    from docx import Document  # DOCX originals: whole-document comparison only

    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        parts += [cell.text for row in table.rows for cell in row.cells]
    return [], _norm(" ".join(parts))


def extract(
    corrected: Path, original: Path | None, cfg: CorrectionImportSettings
) -> ExtractionResult:
    candidates: list[Candidate] = []
    ignored: list[str] = []
    problems: list[str] = []
    scanned: list[int] = []
    orig_pages, orig_all = ([], "")
    if original is not None:
        try:
            orig_pages, orig_all = original_text_by_page(original)
        except Exception as exc:  # unreadable original: red text cannot be verified
            problems.append(f"original unreadable ({type(exc).__name__}); red text unverified")
    with pymupdf.open(corrected) as doc:
        if doc.is_encrypted:
            return ExtractionResult([], 0, [], [], ["corrected PDF is encrypted"])
        if orig_pages and len(orig_pages) != doc.page_count:
            problems.append(
                f"page count differs (original {len(orig_pages)}, corrected {doc.page_count}); "
                "check page numbers"
            )
        for page in doc:
            no = page.number + 1
            words = page.get_text("words")
            if not words:
                scanned.append(no)
            annots = list(page.annots())
            annot_rects = [pymupdf.Rect(a.rect) for a in annots]
            replies: dict[int, list[str]] = {}
            for a in annots:
                if a.irt_xref and a.info.get("content"):
                    replies.setdefault(a.irt_xref, []).append(a.info["content"])
            for a in annots:
                kind = a.type[1]
                if kind in IGNORED_TYPES:
                    continue
                if a.irt_xref:  # replies are attached to their parent annotation
                    continue
                info = a.info
                comment = _clean(" ".join([info.get("content", ""), *replies.get(a.xref, [])]))
                base = dict(
                    page=no,
                    annotation_type=kind,
                    comment=comment,
                    author=_clean(info.get("title")),
                    created=_clean(info.get("creationDate")),
                    bbox=tuple(round(v, 1) for v in a.rect),
                    marks_mentioned=marks_in(comment),
                )
                if kind in MARKUP:
                    quoted = _words_in(words, _quads(a))
                    ev = [f"{kind} annotation with quad points"]
                    if quoted is None:
                        ev.append("no text under the markup")
                    elif orig_pages or orig_all:
                        in_page = no <= len(orig_pages) and _norm(quoted) in orig_pages[no - 1]
                        if in_page:
                            ev.append("quote found on the same page of the original")
                        elif _norm(quoted) in orig_all:
                            ev.append(
                                "quote found in the original (other page)"
                                if orig_pages
                                else "quote found in the original document (DOCX: no pages)"
                            )
                        else:
                            ev.append("quote NOT found in the original")
                    candidates.append(
                        Candidate(channel="markup", quoted_text=quoted, evidence=ev, **base)
                    )
                elif kind in NOTES:
                    candidates.append(
                        Candidate(
                            channel="note",
                            context=_near(words, pymupdf.Rect(a.rect), pad=40),
                            evidence=[f"{kind} annotation (no quoted text)"],
                            correction=comment if kind == "FreeText" else None,
                            **base,
                        )
                    )
                elif kind in SHAPES:
                    candidates.append(
                        Candidate(
                            channel="drawing",
                            context=_near(words, pymupdf.Rect(a.rect)),
                            evidence=[f"{kind} annotation: a drawn mark, meaning needs a person"],
                            **base,
                        )
                    )
                else:
                    ignored.append(f"page {no}: {kind} annotation not interpreted")
            # red text in the page content
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = _clean(span.get("text"))
                        if not text or not _is_red(_int_rgb(span.get("color", 0)), cfg):
                            continue
                        rect = pymupdf.Rect(span["bbox"])
                        if _inside_annotation(rect, annot_rects):
                            continue
                        if orig_all and _norm(text) in (
                            orig_pages[no - 1] if no <= len(orig_pages) else orig_all
                        ):
                            ignored.append(
                                f"page {no}: red text {text[:40]!r} also in the original "
                                "(student's own styling)"
                            )
                            continue
                        ev = (
                            ["red text absent from the original (added after submission)"]
                            if orig_all
                            else ["red text; no original to compare (cannot tell who added it)"]
                        )
                        candidates.append(
                            Candidate(
                                channel="red_text",
                                page=no,
                                correction=text,
                                comment=text,
                                bbox=tuple(round(v, 1) for v in rect),
                                context=_near(words, rect),
                                evidence=ev,
                                marks_mentioned=marks_in(text),
                            )
                        )
            # red vector drawings (strike lines, circles drawn into the content)
            for d in page.get_drawings():
                if _is_red(d.get("color"), cfg) and d.get("rect") is not None:
                    rect = pymupdf.Rect(d["rect"])
                    if (rect.width < 1 and rect.height < 1) or _inside_annotation(
                        rect, annot_rects
                    ):
                        continue
                    candidates.append(
                        Candidate(
                            channel="drawing",
                            page=no,
                            bbox=tuple(round(v, 1) for v in rect),
                            context=_near(words, rect),
                            evidence=["red vector drawing in the page content"],
                        )
                    )
            if not words and not annots:
                candidates.append(
                    Candidate(
                        channel="scanned_page",
                        page=no,
                        evidence=[
                            "page has no text layer: handwritten/scanned corrections "
                            "must be transcribed manually"
                        ],
                    )
                )
        page_count = doc.page_count
    return ExtractionResult(candidates, page_count, scanned, ignored, problems)


def suggest_label(
    text: str | None, cfg: CorrectionImportSettings
) -> tuple[str | None, list[str], str]:
    """(label or None, all matching labels, how) from the teacher's comment."""
    t = _norm(text)
    if not t:
        return None, [], "no comment text"
    hits = [
        label
        for label, words in cfg.keywords.items()
        if any(re.search(rf"(?<![a-z]){re.escape(w.casefold())}", t) for w in words)
    ]
    if len(hits) == 1:
        return hits[0], hits, "one keyword label"
    if not hits:
        return None, [], "no keyword matched"
    return None, hits, "several labels matched"


def suggest_severity(text: str | None, cfg: CorrectionImportSettings) -> tuple[str, bool]:
    """(severity, explicit?) — medium when the comment gives no clear signal."""
    t = _norm(text)
    high = any(re.search(rf"\b{re.escape(w)}\b", t) for w in cfg.high_severity_words)
    low = any(re.search(rf"\b{re.escape(w)}\b", t) for w in cfg.low_severity_words)
    if high and not low:
        return "high", True
    if low and not high:
        return "low", True
    return "medium", False
