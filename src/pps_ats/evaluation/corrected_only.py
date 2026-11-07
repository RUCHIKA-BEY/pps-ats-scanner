"""Corrected-only intake: faculty-corrected PDFs without separate student originals.

Observed on the first real batch (5 PDFs, macOS Preview): every faculty comment is a
**FreeText annotation** (red Helvetica); removing the annotation objects leaves the
student's page content intact. This module therefore:

1. reads corrections from **annotation objects** (one annotation = one correction; separate
   comments on one line are never merged);
2. builds a **clean scanner input**: a copy with faculty annotation objects deleted (the
   text is removed from the file, not covered). Red text that is part of the *page
   content* (flattened corrections or the student's own styling) is classified:

   * ``likely_student``  heading-like, names a resume section, no instruction wording:
     kept as student content;
   * ``likely_faculty``  instruction wording and/or a font distinct from the resume, not
     overlapping other text: removed from the copy with text redaction (glyphs deleted),
     and the removal is verified;
   * ``ambiguous``       anything else, or faculty text on top of student text: the
     document is marked ``needs_review`` and is not used as clean student text;

3. verifies the copy: no annotations left, and the student's (non-red) characters are
   identical page by page to the corrected PDF without its annotations;
4. marks image-only pages ``manual_transcription``.

The corrected PDF itself is never written to.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pymupdf

from pps_ats.config import Settings
from pps_ats.evaluation.corrections import Candidate, extract, marks_in
from pps_ats.evaluation.importer import (
    CANDIDATE_FIELDS,
    CANDIDATES,
    DOC_FIELDS,
    ImportError_,
    _strip_suffix,
    _write_csv,
)

FACULTY_ANNOTS = {
    "FreeText", "Text", "Highlight", "Underline", "StrikeOut", "Squiggly", "Ink", "Square",
    "Circle", "Line", "Polygon", "PolyLine", "Popup", "Caret", "Stamp",
}  # fmt: skip
INSTRUCTION = re.compile(
    r"^\s*(?:pl(?:ease|s|z)\s+)?(add|ad|mention|remove|rename|title|keep|use|write|include|change|"
    r"move|replace|avoid|make|specify|highlight|quantify|elaborate|shorten|reduce|correct|fix|"
    r"align|put|give|delete|follow|list|state|show|describe|explain|start|merge|split|drop|"
    r"up to|should|need|needs|must|try|consider)\b"
    r"|\b(should|needs? to|must|instead of|is missing|not required|not needed)\b",
    re.IGNORECASE,
)
EXTRA_FIELDS = ["source_type", "faculty_likelihood", "associated_section", "section_basis"]
REVIEW_FIELDS = [
    *CANDIDATE_FIELDS[:13],
    *EXTRA_FIELDS,
    *CANDIDATE_FIELDS[13:],
]


# ---------------------------------------------------------------- red text in page content


@dataclass
class RedUnit:
    page: int
    text: str
    rects: list[pymupdf.Rect]
    font: str
    size: float
    overlaps_text: bool = False
    classification: str = "ambiguous"  # likely_student | likely_faculty | ambiguous
    evidence: list[str] = field(default_factory=list)

    @property
    def bbox(self) -> pymupdf.Rect:
        r = pymupdf.Rect(self.rects[0])
        for x in self.rects[1:]:
            r |= x
        return r


def _rgb(color: int) -> tuple[float, float, float]:
    return ((color >> 16) & 255) / 255, ((color >> 8) & 255) / 255, (color & 255) / 255


def _red(color: int, settings: Settings) -> bool:
    cfg = settings.evaluation.import_
    r, g, b = _rgb(color)
    return r >= cfg.red_min and g <= cfg.red_max_other and b <= cfg.red_max_other


def _family(font: str) -> str:
    return re.split(r"[-,+]", font.split("+")[-1])[0].casefold()


def _section_names(settings: Settings) -> dict[str, str]:
    out = {}
    for name, synonyms in settings.sections.canonical.items():
        for s in [name, *synonyms]:
            out[" ".join(s.casefold().split())] = name
    return out


def red_units(page: pymupdf.Page, settings: Settings) -> list[RedUnit]:
    """Group red spans of the page *content* into units. Spans on one line join only when
    adjacent (gap < 0.8 x font size, same font, no other text in between); a following
    line joins only when directly below and left-aligned with the same style."""
    units: list[RedUnit] = []
    others: list[pymupdf.Rect] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            current: RedUnit | None = None
            for span in line["spans"]:
                text = span["text"]
                rect = pymupdf.Rect(span["bbox"])
                if not text.strip():
                    continue
                if not _red(span["color"], settings):
                    others.append(rect)
                    current = None  # other text between red spans separates them
                    continue
                same_style = current is not None and (
                    current.font == span["font"] and abs(current.size - span["size"]) < 0.5
                )
                gap = rect.x0 - current.rects[-1].x1 if current is not None else 1e9
                if same_style and gap < 0.8 * span["size"]:
                    current.text += (" " if gap > 0.15 * span["size"] else "") + text.strip()
                    current.rects.append(rect)
                else:
                    current = RedUnit(page.number + 1, text.strip(), [rect], span["font"],
                                      span["size"])  # fmt: skip
                    units.append(current)
    merged: list[RedUnit] = []
    for unit in units:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.font == unit.font
            and abs(prev.size - unit.size) < 0.5
            and 0 <= unit.bbox.y0 - prev.bbox.y1 < 0.6 * unit.size
            and abs(unit.bbox.x0 - prev.bbox.x0) < 2 * unit.size
        ):
            prev.text += " " + unit.text
            prev.rects += unit.rects
        else:
            merged.append(unit)
    for unit in merged:
        unit.overlaps_text = any(
            (r & o).get_area() > 0.1 * min(r.get_area(), o.get_area())
            for r in unit.rects
            for o in others
            if r.intersects(o)
        )
    return merged


def classify(unit: RedUnit, body_family: str | None, settings: Settings) -> None:
    sections = _section_names(settings)
    words = unit.text.split()
    norm = " ".join(unit.text.casefold().strip(" :-").split())
    instruction = bool(INSTRUCTION.search(unit.text))
    heading_like = len(words) <= 4 and norm in sections
    distinct = body_family is not None and _family(unit.font) != body_family
    if instruction:
        unit.evidence.append("instruction wording")
    if heading_like:
        unit.evidence.append(f"reads as the section heading '{sections[norm]}'")
    unit.evidence.append(
        "font differs from the resume body" if distinct else "same font family as the resume body"
    )
    if unit.overlaps_text:
        unit.evidence.append("drawn on top of other text")
    if heading_like and not instruction and not distinct:
        unit.classification = "likely_student"
    elif instruction and not unit.overlaps_text and (distinct or len(words) >= 3):
        unit.classification = "likely_faculty"
    else:
        unit.classification = "ambiguous"


def _body_family(doc: pymupdf.Document, settings: Settings) -> str | None:
    fonts: Counter[str] = Counter()
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip() and not _red(span["color"], settings):
                        fonts[_family(span["font"])] += len(span["text"])
    return fonts.most_common(1)[0][0] if fonts else None


def _non_red_chars(doc: pymupdf.Document, settings: Settings) -> list[str]:
    """Per page: the non-red characters (whitespace removed) in reading order."""
    pages = []
    for page in doc:
        chars = []
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if _red(span["color"], settings):
                        continue
                    chars += [c["c"] for c in span["chars"] if not c["c"].isspace()]
        pages.append("".join(chars))
    return pages


# ---------------------------------------------------------------- clean scanner input


@dataclass
class CleanResult:
    status: str  # clean | needs_review | manual_transcription | failed
    removed_annotations: int = 0
    removed_red_units: int = 0
    kept_student_red: int = 0
    ambiguous_red: int = 0
    image_only_pages: list[int] = field(default_factory=list)
    student_text_preserved: bool = False
    residual_red_chars: int = 0
    reasons: list[str] = field(default_factory=list)
    units: list[RedUnit] = field(default_factory=list)


def _without_annotations(src: Path) -> tuple[pymupdf.Document, int]:
    doc = pymupdf.open(src)  # in memory; ``src`` is never saved
    removed = 0
    for page in doc:
        for annot in list(page.annots()):
            if annot.type[1] in FACULTY_ANNOTS:
                page.delete_annot(annot)
                removed += 1
    return doc, removed


def build_clean_copy(src: Path, dst: Path, settings: Settings) -> CleanResult:
    doc, removed = _without_annotations(src)
    result = CleanResult(status="clean", removed_annotations=removed)
    try:
        body = _body_family(doc, settings)
        reference = _non_red_chars(doc, settings)  # student text after annotation removal
        to_redact: dict[int, list[pymupdf.Rect]] = {}
        for page in doc:
            if not page.get_text().strip():
                result.image_only_pages.append(page.number + 1)
            for unit in red_units(page, settings):
                classify(unit, body, settings)
                result.units.append(unit)
                if unit.classification == "likely_student":
                    result.kept_student_red += 1
                elif unit.classification == "likely_faculty":
                    to_redact.setdefault(page.number, []).extend(unit.rects)
                else:
                    result.ambiguous_red += 1
        for number, rects in to_redact.items():
            page = doc[number]
            for rect in rects:
                page.add_redact_annot(rect + (0.3, 0.3, -0.3, -0.3), fill=False, cross_out=False)
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )
            result.removed_red_units += sum(
                1
                for u in result.units
                if u.page == number + 1 and u.classification == "likely_faculty"
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        doc.save(tmp, garbage=3, deflate=True)
    finally:
        doc.close()
    tmp.replace(dst)
    # verification on the written file
    with pymupdf.open(dst) as check:
        leftover = sum(1 for p in check for a in p.annots() if a.type[1] in FACULTY_ANNOTS)
        result.student_text_preserved = _non_red_chars(check, settings) == reference
        student_red = sum(
            len(u.text.replace(" ", ""))
            for u in result.units
            if u.classification != "likely_faculty"
        )
        all_red = sum(
            len(s["text"].replace(" ", ""))
            for p in check
            for b in p.get_text("dict")["blocks"]
            for ln in b.get("lines", [])
            for s in ln["spans"]
            if _red(s["color"], settings)
        )
        result.residual_red_chars = max(0, all_red - student_red)
    if leftover:
        result.reasons.append(f"{leftover} annotation(s) could not be removed")
    if not result.student_text_preserved:
        result.reasons.append("student (non-red) text changed while removing faculty text")
    if result.residual_red_chars:
        result.reasons.append("faculty red text still extractable after removal")
    if result.ambiguous_red:
        result.reasons.append(
            f"{result.ambiguous_red} red text item(s) could be faculty or student"
        )
    if result.image_only_pages:
        result.status = "manual_transcription"
        result.reasons.append(f"image-only page(s) {result.image_only_pages}: transcribe manually")
    elif leftover or not result.student_text_preserved or result.residual_red_chars:
        result.status = "failed"
    elif result.ambiguous_red or result.removed_red_units:
        result.status = "needs_review"  # removal of flattened text must be confirmed
        if result.removed_red_units:
            result.reasons.append(
                f"{result.removed_red_units} flattened red instruction(s) removed: confirm"
            )
    return result


# ---------------------------------------------------------------- sections + labels


def section_for(rect: tuple | None, page: int, headings: list[tuple[int, pymupdf.Rect, str]],
                text: str, settings: Settings) -> tuple[str, str]:  # fmt: skip
    """(section, basis). Named in the comment wins; else the nearest heading above or on
    the same line on that page."""
    low = " " + " ".join(text.casefold().split()) + " "
    named = [
        name
        for syn, name in sorted(_section_names(settings).items(), key=lambda x: -len(x[0]))
        if f" {syn} " in low or f" {syn}," in low or f" {syn}." in low
    ]
    if len(set(named)) == 1:
        return named[0], "named in the comment"
    if rect is None:
        return "", "no position"
    r = pymupdf.Rect(rect)
    above = [(r.y0 - h.y0, name) for p, h, name in headings if p == page and h.y0 <= r.y1 + 2]
    if not above:
        return "", "no heading above"
    return min(above)[1], "nearest heading above"


def _headings(scan) -> list[tuple[int, pymupdf.Rect, str]]:  # type: ignore[no-untyped-def]
    return [
        (s.page, pymupdf.Rect(s.bbox), s.name)
        for s in scan.sections
        if s.page and s.bbox and s.status.value != "missing"
    ]


def suggest(text: str | None, settings: Settings) -> tuple[str | None, list[str], str]:
    """Pattern rules for instruction-style comments first, then the keyword lists."""
    from pps_ats.evaluation.corrections import suggest_label

    cfg = settings.evaluation.import_
    t = " ".join((text or "").casefold().split())
    hits = [label for label, pats in cfg.patterns.items() if any(re.search(p, t) for p in pats)]
    hits = list(dict.fromkeys(hits))
    if len(hits) == 1:
        return hits[0], hits, "one instruction pattern"
    if len(hits) > 1:
        return None, hits, "several instruction patterns matched"
    return suggest_label(text, cfg)


# ---------------------------------------------------------------- import


@dataclass
class CorrectedOnlySummary:
    documents: int = 0
    candidates: int = 0
    auto_mapped: int = 0
    needs_review: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_source: dict[str, int] = field(default_factory=dict)
    by_label: dict[str, int] = field(default_factory=dict)
    marks_imported: int = 0
    marks_rejected: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    per_document: list[dict] = field(default_factory=list)


def _source_type(c: Candidate) -> str:
    if c.annotation_type:
        return f"pdf_annotation:{c.annotation_type}"
    return {"scanned_page": "image_only_page", "drawing": "red_drawing"}.get(c.channel, c.channel)


def document_id_for(path: Path, settings: Settings) -> str:
    """Document id derived from a corrected PDF's file name (suffixes stripped)."""
    doc_id = _strip_suffix(path.stem, settings.evaluation.import_.corrected_suffixes).strip()
    return (
        re.sub(r"[^\w.-]+", "_", doc_id).strip("_")
        or hashlib.sha1(path.name.encode()).hexdigest()[:8]
    )


def import_corrected_only(
    intake: Path, dataset: Path, settings: Settings, force: bool = False
) -> CorrectedOnlySummary:
    from pps_ats.evaluation.importer import load_marks
    from pps_ats.pipeline import scan_file

    intake, dataset = Path(intake), Path(dataset)
    folder = intake / "corrected" if (intake / "corrected").is_dir() else intake
    if dataset.resolve() == intake.resolve() or intake.resolve() in dataset.resolve().parents:
        raise ImportError_("the dataset folder must not be inside the intake folder")
    if (dataset / CANDIDATES).exists() and not force:
        raise ImportError_(f"{dataset / CANDIDATES} exists; use --force to re-import")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for name in (CANDIDATES, "documents.csv"):
        if (dataset / name).exists():
            shutil.copyfile(dataset / name, dataset / f"{name}.{stamp}.bak")
    summary = CorrectedOnlySummary()
    pdfs: dict[str, Path] = {}
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.name.startswith(("~$", ".")):
            continue
        if path.suffix.lower() != ".pdf":
            summary.problems.append(f"{path.name}: not a PDF, skipped")
            continue
        doc_id = document_id_for(path, settings)
        if doc_id in pdfs:
            summary.problems.append(f"{path.name}: duplicate document id {doc_id!r}, skipped")
            continue
        pdfs[doc_id] = path
    marks, rejected = load_marks(intake, set(pdfs))
    summary.marks_rejected = rejected
    (dataset / "corrected").mkdir(parents=True, exist_ok=True)
    (dataset / "files").mkdir(exist_ok=True)
    rows: list[dict] = []
    doc_rows: list[dict] = []
    for doc_id, path in pdfs.items():
        corrected_copy = dataset / "corrected" / f"{doc_id}.pdf"
        shutil.copyfile(path, corrected_copy)  # the intake file is only read
        clean_path = dataset / "files" / f"{doc_id}.pdf"
        info: dict = {
            "document_id": doc_id,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        try:
            clean = build_clean_copy(path, clean_path, settings)
            extracted = extract(path, None, settings.evaluation.import_)
        except Exception as exc:
            summary.problems.append(f"{doc_id}: unreadable ({type(exc).__name__})")
            info["status"] = "failed"
            summary.per_document.append(info)
            summary.by_status["failed"] = summary.by_status.get("failed", 0) + 1
            continue
        headings = []
        if clean.status not in ("failed", "manual_transcription"):
            try:
                headings = _headings(scan_file(clean_path, settings))
            except Exception:  # section association is a convenience only
                headings = []
        cands: list[tuple[Candidate, str, str]] = []
        for c in extracted.candidates:
            if c.channel == "red_text":
                continue  # page-content red text is handled by the unit classifier below
            if c.annotation_type:
                c.evidence.insert(0, "PDF annotation object: added on top of the page, "
                                     "not part of the student's content")  # fmt: skip
            cands.append(
                (c, _source_type(c), "faculty_annotation" if c.annotation_type else "unknown")
            )
        for u in clean.units:
            if u.classification == "likely_student":
                continue
            r = u.bbox
            cands.append(
                (
                    Candidate(
                        channel="red_text",
                        page=u.page,
                        comment=u.text,
                        correction=u.text,
                        bbox=(round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1)),
                        evidence=["red text in the page content", *u.evidence],
                        marks_mentioned=marks_in(u.text),
                    ),
                    "page_content_red_text",
                    u.classification,
                )
            )
        doc_candidates = []
        for i, (c, source, likelihood) in enumerate(cands, 1):
            label, matches, how = suggest(c.comment, settings)
            section, basis = section_for(c.bbox, c.page, headings, c.comment or "", settings)
            if likelihood != "faculty_annotation":
                status, reason = "needs_review", f"{source}: {likelihood.replace('_', ' ')}"
            elif c.channel in ("drawing", "scanned_page") or not c.comment:
                status, reason = "needs_review", "no text: meaning needs a person"
            elif label is None:
                status, reason = "needs_review", how
            elif c.marks_mentioned:
                status, reason = "needs_review", "comment mentions marks (never imported)"
            else:
                status, reason = "auto_mapped", how
            row = {
                "candidate_id": f"{doc_id}-{i:03d}",
                "document_id": doc_id,
                "page": str(c.page),
                "channel": c.channel,
                "annotation_type": c.annotation_type or "",
                "quoted_text": c.quoted_text or "",
                "teacher_comment": c.comment or "",
                "teacher_correction": c.correction or "",
                "author": c.author or "",
                "created": c.created or "",
                "bbox": ",".join(f"{v:g}" for v in c.bbox) if c.bbox else "",
                "context": c.context or "",
                "evidence": "; ".join(c.evidence),
                "source_type": source,
                "faculty_likelihood": likelihood,
                "associated_section": section,
                "section_basis": basis,
                "suggested_label": label or "",
                "label_matches": ", ".join(matches),
                "label_reason": how,
                "suggested_severity": "",
                "severity_explicit": "no",
                "marks_mentioned": "; ".join(c.marks_mentioned),
                "review_status": status,
                "review_reason": reason,
                "reviewer_decision": "",
                "final_label": label or "",
                "final_severity": "",
                "final_text": c.quoted_text or "",
                "reviewer_note": "",
            }
            doc_candidates.append(row)
        rows += doc_candidates
        summary.by_status[clean.status] = summary.by_status.get(clean.status, 0) + 1
        info.update(
            pages=extracted.page_count,
            scanner_input_status=clean.status,
            removed_annotations=clean.removed_annotations,
            removed_flattened_red=clean.removed_red_units,
            kept_student_red=clean.kept_student_red,
            ambiguous_red=clean.ambiguous_red,
            student_text_preserved=clean.student_text_preserved,
            residual_red_chars=clean.residual_red_chars,
            image_only_pages=clean.image_only_pages,
            reasons=clean.reasons,
            candidates=len(doc_candidates),
            needs_review=sum(r["review_status"] == "needs_review" for r in doc_candidates),
        )
        summary.per_document.append(info)
        mark = marks.get(doc_id, {})
        doc_rows.append(
            {
                "document_id": doc_id,
                "file": f"files/{doc_id}.pdf",
                "split": "",
                "expected_validation": "",
                "marks_source": "faculty" if mark else "",
                "faculty_content_relevance": mark.get("faculty_content_relevance", ""),
                "faculty_structure_formatting": mark.get("faculty_structure_formatting", ""),
                "faculty_language_professional_tone": mark.get(
                    "faculty_language_professional_tone", ""
                ),
                "faculty_total": mark.get("faculty_total", ""),
                "annotations_complete": "no",
                "grader_id": mark.get("grader_id", ""),
                "notes": "corrected-only: scanner input = corrected PDF minus faculty text",
                "scanner_input_status": clean.status,
            }
        )
    summary.documents = len(summary.per_document)
    summary.candidates = len(rows)
    summary.auto_mapped = sum(r["review_status"] == "auto_mapped" for r in rows)
    summary.needs_review = sum(r["review_status"] == "needs_review" for r in rows)
    summary.marks_imported = len(marks)
    summary.by_source = dict(Counter(r["source_type"] for r in rows))
    summary.by_label = dict(Counter(r["suggested_label"] or "(needs review)" for r in rows))
    _write_csv(dataset / CANDIDATES, REVIEW_FIELDS, rows)
    _write_csv(dataset / "documents.csv", [*DOC_FIELDS, "scanner_input_status"], doc_rows)
    report = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": "corrected-only",
        **summary.__dict__,
    }
    (dataset / "import_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (dataset / "import_report.md").write_text(_report_md(summary), encoding="utf-8")
    return summary


def _report_md(s: CorrectedOnlySummary) -> str:
    lines = [
        "# Corrected-only import",
        "",
        f"- Documents: {s.documents}; scanner-input status: {s.by_status}",
        f"- Correction candidates: {s.candidates} (auto-mapped {s.auto_mapped}, needs review "
        f"{s.needs_review}); by source: {s.by_source}",
        f"- Faculty marks imported: {s.marks_imported}; rejected rows: {len(s.marks_rejected)}",
        "",
        "| document | pages | input status | annotations removed | flattened red removed | "
        "student red kept | ambiguous red | student text preserved | candidates | review |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for d in s.per_document:
        lines.append(
            f"| {d['document_id']} | {d.get('pages', '')} | "
            f"{d.get('scanner_input_status', 'failed')} | "
            f"{d.get('removed_annotations', '')} | {d.get('removed_flattened_red', '')} | "
            f"{d.get('kept_student_red', '')} | {d.get('ambiguous_red', '')} | "
            f"{d.get('student_text_preserved', '')} | {d.get('candidates', '')} | "
            f"{d.get('needs_review', '')} |"
        )
    reasons = [
        f"- {d['document_id']}: {'; '.join(d['reasons'])}"
        for d in s.per_document
        if d.get("reasons")
    ]
    if reasons:
        lines += ["", "## Documents needing attention", "", *reasons]
    if s.marks_rejected:
        lines += ["", "## Marks not imported", "", *[f"- {m}" for m in s.marks_rejected]]
    if s.problems:
        lines += ["", "## Problems", "", *[f"- {p}" for p in s.problems]]
    lines += [
        "",
        "Only documents with scanner input status `clean` are used as student content in "
        "evaluation; others are excluded until reviewed.",
    ]
    return "\n".join(lines) + "\n"
