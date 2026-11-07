"""Import teacher-corrected resumes into the reference dataset format (``dataset.py``).

Intake folder (supplied by faculty; private, local)::

    INTAKE/
      originals/<document_id>.pdf|.docx      the student's submission as graded
      corrected/<document_id>_corrected.pdf  the teacher's annotated copy (PDF)
      marks.csv                              optional: faculty-assigned marks
      intake.csv                             optional: explicit id -> file mapping

Step 1 ``import-corrections INTAKE -o DATASET`` (read-only on INTAKE) writes::

    DATASET/
      files/<document_id>.<ext>          copies of the originals (what the scanner evaluates)
      corrected/<document_id>.pdf        copies of the teacher PDFs (traceability)
      documents.csv                      draft: one row per original, marks if attributable
      annotation_candidates.csv          every extracted correction, for review
      import_report.md / .json           what was found per document, and problems

Step 2: a faculty member reviews ``annotation_candidates.csv`` (fills
``reviewer_decision`` = accept / reject, corrects ``final_label`` / ``final_severity`` /
``final_text``) and sets ``annotations_complete`` in documents.csv.

Step 3 ``finalize-annotations DATASET`` writes ``annotations.csv`` from accepted rows and
refuses while rows that need a person are undecided.
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pps_ats.config import Settings
from pps_ats.evaluation.corrections import (
    Candidate,
    extract,
    suggest_label,
    suggest_severity,
)

ORIGINAL_EXT = (".pdf", ".docx")
CANDIDATES = "annotation_candidates.csv"
CANDIDATE_FIELDS = [
    "candidate_id",
    "document_id",
    "page",
    "channel",
    "annotation_type",
    "quoted_text",
    "teacher_comment",
    "teacher_correction",
    "author",
    "created",
    "bbox",
    "context",
    "evidence",
    "suggested_label",
    "label_matches",
    "label_reason",
    "suggested_severity",
    "severity_explicit",
    "marks_mentioned",
    "review_status",
    "review_reason",
    "reviewer_decision",
    "final_label",
    "final_severity",
    "final_text",
    "reviewer_note",
]
DOC_FIELDS = [
    "document_id",
    "file",
    "split",
    "expected_validation",
    "marks_source",
    "faculty_content_relevance",
    "faculty_structure_formatting",
    "faculty_language_professional_tone",
    "faculty_total",
    "annotations_complete",
    "grader_id",
    "notes",
]
MARK_ALIASES = {
    "content_relevance": ("content_relevance", "content", "faculty_content_relevance"),
    "structure_formatting": ("structure_formatting", "structure", "faculty_structure_formatting"),
    "language_professional_tone": (
        "language_professional_tone",
        "language",
        "faculty_language_professional_tone",
    ),
}


class ImportError_(ValueError):  # noqa: N801 - avoid shadowing the builtin
    pass


@dataclass
class IntakeItem:
    document_id: str
    original: Path | None
    corrected: Path | None


@dataclass
class ImportSummary:
    documents: int = 0
    with_corrections: int = 0
    candidates: int = 0
    auto_mapped: int = 0
    needs_review: int = 0
    ignored: int = 0
    marks_imported: int = 0
    marks_rejected: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    per_document: list[dict] = field(default_factory=list)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return [
            {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in reader
        ]


def _strip_suffix(stem: str, suffixes: list[str]) -> str:
    lower = stem.casefold()
    for suffix in sorted(suffixes, key=len, reverse=True):
        if lower.endswith(suffix.casefold()):
            return stem[: len(stem) - len(suffix)]
    return stem


def pair_files(intake: Path, settings: Settings) -> tuple[list[IntakeItem], list[str]]:
    """Match originals and corrected PDFs by document id. Returns (items, problems)."""
    problems: list[str] = []
    originals_dir, corrected_dir = intake / "originals", intake / "corrected"
    if not originals_dir.is_dir():
        raise ImportError_(f"{originals_dir} not found (put the students' originals there)")
    explicit = intake / "intake.csv"
    if explicit.is_file():
        items = []
        for row in _rows(explicit):
            doc_id = row.get("document_id", "")
            orig = originals_dir / row["original_file"] if row.get("original_file") else None
            corr = corrected_dir / row["corrected_file"] if row.get("corrected_file") else None
            for label, path in (("original", orig), ("corrected", corr)):
                if path is not None and not path.is_file():
                    problems.append(f"{doc_id}: {label} file not found: {path.name}")
            items.append(
                IntakeItem(
                    doc_id,
                    orig if orig and orig.is_file() else None,
                    corr if corr and corr.is_file() else None,
                )
            )
        return items, problems
    originals: dict[str, Path] = {}
    for path in sorted(originals_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in ORIGINAL_EXT:
            if path.stem in originals:
                problems.append(f"{path.stem}: two originals with the same id; both skipped")
                originals[path.stem] = None  # type: ignore[assignment]
            else:
                originals[path.stem] = path
        elif path.is_file() and not path.name.startswith(("~$", ".")):
            problems.append(f"originals/{path.name}: not a PDF/DOCX, skipped")
    corrected: dict[str, Path] = {}
    if corrected_dir.is_dir():
        for path in sorted(corrected_dir.iterdir()):
            if not path.is_file() or path.name.startswith(("~$", ".")):
                continue
            if path.suffix.lower() != ".pdf":
                problems.append(f"corrected/{path.name}: corrections must be a PDF, skipped")
                continue
            doc_id = _strip_suffix(path.stem, settings.evaluation.import_.corrected_suffixes)
            if doc_id in corrected:
                problems.append(
                    f"{doc_id}: more than one corrected PDF; using {corrected[doc_id].name}"
                )
                continue
            corrected[doc_id] = path
    for doc_id in sorted(set(corrected) - set(originals)):
        problems.append(
            f"corrected/{corrected[doc_id].name}: no original with id {doc_id!r}; skipped"
        )
    items = [
        IntakeItem(doc_id, path, corrected.get(doc_id))
        for doc_id, path in sorted(originals.items())
        if path is not None
    ]
    return items, problems


def load_marks(intake: Path, known: set[str]) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Faculty marks from ``marks.csv``. A row is imported only when it names a known
    document, has all three criteria within 0-10, a grader, and (if present) source =
    faculty. Everything else is reported, never guessed."""
    path = intake / "marks.csv"
    if not path.is_file():
        return {}, []
    accepted: dict[str, dict[str, str]] = {}
    rejected: list[str] = []
    seen: set[str] = set()
    for line, row in enumerate(_rows(path), start=2):
        doc_id = row.get("document_id", "")
        where = f"marks.csv line {line} ({doc_id or 'no id'})"
        if not doc_id or doc_id not in known:
            rejected.append(f"{where}: document_id not in originals/")
            continue
        if doc_id in seen:
            rejected.append(f"{where}: duplicate row; neither row is used")
            accepted.pop(doc_id, None)
            continue
        seen.add(doc_id)
        source = (row.get("source") or row.get("marks_source") or "faculty").lower()
        if source != "faculty":
            rejected.append(f"{where}: source {source!r} is not faculty (e.g. old AI grades)")
            continue
        grader = row.get("grader_id", "")
        if not grader:
            rejected.append(f"{where}: grader_id is empty, marks are not attributable")
            continue
        values: dict[str, float] = {}
        for crit, aliases in MARK_ALIASES.items():
            raw = next((row[a] for a in aliases if row.get(a)), "")
            try:
                values[crit] = float(raw)
            except ValueError:
                break
            if not 0 <= values[crit] <= 10:
                break
        if len(values) != 3 or not all(0 <= v <= 10 for v in values.values()):
            rejected.append(f"{where}: needs all three criterion marks between 0 and 10")
            continue
        total = round(sum(values.values()), 2)
        stated = row.get("total") or row.get("faculty_total") or ""
        if stated:
            try:
                if abs(float(stated) - total) > 0.01:
                    rejected.append(f"{where}: total {stated} != sum of criteria {total:g}")
                    continue
            except ValueError:
                rejected.append(f"{where}: total {stated!r} is not a number")
                continue
        accepted[doc_id] = {
            "faculty_content_relevance": f"{values['content_relevance']:g}",
            "faculty_structure_formatting": f"{values['structure_formatting']:g}",
            "faculty_language_professional_tone": f"{values['language_professional_tone']:g}",
            "faculty_total": f"{total:g}",
            "grader_id": grader,
        }
    return accepted, rejected


def _review(c: Candidate, label: str | None, how: str) -> tuple[str, str]:
    """(review_status, reason). Only markup/notes with one clear label are auto-mapped."""
    if c.channel in ("red_text", "drawing", "scanned_page"):
        return "needs_review", f"{c.channel.replace('_', ' ')}: meaning needs a person"
    if c.channel == "markup" and not c.quoted_text:
        return "needs_review", "markup without text underneath"
    if c.channel == "markup" and any("NOT found" in e for e in c.evidence):
        return "needs_review", "quoted text not in the original (wrong file or page?)"
    if label is None:
        return "needs_review", how
    if c.marks_mentioned:
        return "needs_review", "comment mentions marks; check them against marks.csv"
    return "auto_mapped", how


def _candidate_rows(
    doc_id: str, candidates: list[Candidate], settings: Settings
) -> list[dict[str, str]]:
    cfg = settings.evaluation.import_
    rows = []
    for i, c in enumerate(candidates, 1):
        label, matches, how = suggest_label(c.comment, cfg)
        severity, explicit = suggest_severity(c.comment, cfg)
        status, reason = _review(c, label, how)
        rows.append(
            {
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
                "suggested_label": label or "",
                "label_matches": ", ".join(matches),
                "label_reason": how,
                "suggested_severity": severity,
                "severity_explicit": "yes" if explicit else "no",
                "marks_mentioned": "; ".join(c.marks_mentioned),
                "review_status": status,
                "review_reason": reason,
                "reviewer_decision": "",
                "final_label": label or "",
                "final_severity": severity if explicit else "",
                "final_text": c.quoted_text or "",
                "reviewer_note": "",
            }
        )
    return rows


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def import_corrections(
    intake: Path, dataset: Path, settings: Settings, force: bool = False
) -> ImportSummary:
    intake, dataset = Path(intake), Path(dataset)
    if dataset.resolve() == intake.resolve() or intake.resolve() in dataset.resolve().parents:
        raise ImportError_("the dataset folder must not be inside the intake folder")
    if (dataset / CANDIDATES).exists() and not force:
        raise ImportError_(
            f"{dataset / CANDIDATES} exists (it may contain review decisions); "
            "use --force to re-import (the old files are kept as .bak)"
        )
    items, problems = pair_files(intake, settings)
    marks, rejected = load_marks(intake, {i.document_id for i in items})
    summary = ImportSummary(problems=problems, marks_rejected=rejected)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for name in (CANDIDATES, "documents.csv"):
        if (dataset / name).exists():
            shutil.copyfile(dataset / name, dataset / f"{name}.{stamp}.bak")
    (dataset / "files").mkdir(parents=True, exist_ok=True)
    (dataset / "corrected").mkdir(exist_ok=True)
    cand_rows: list[dict] = []
    doc_rows: list[dict] = []
    for item in items:
        assert item.original is not None
        target = dataset / "files" / f"{item.document_id}{item.original.suffix.lower()}"
        shutil.copyfile(item.original, target)  # copy: the intake is never modified
        info: dict = {
            "document_id": item.document_id,
            "original": item.original.name,
            "original_sha256": _sha(item.original),
            "corrected": item.corrected.name if item.corrected else None,
        }
        rows: list[dict] = []
        if item.corrected is not None:
            shutil.copyfile(item.corrected, dataset / "corrected" / f"{item.document_id}.pdf")
            info["corrected_sha256"] = _sha(item.corrected)
            try:
                result = extract(item.corrected, item.original, settings.evaluation.import_)
            except Exception as exc:  # one unreadable PDF never stops the import
                result = None
                summary.problems.append(
                    f"{item.document_id}: corrected PDF unreadable ({type(exc).__name__})"
                )
            if result is not None:
                rows = _candidate_rows(item.document_id, result.candidates, settings)
                info.update(
                    pages=result.page_count,
                    scanned_pages=result.scanned_pages,
                    ignored=result.ignored,
                    problems=result.problems,
                    channels={
                        ch: sum(1 for c in result.candidates if c.channel == ch)
                        for ch in sorted({c.channel for c in result.candidates})
                    },
                )
                summary.ignored += len(result.ignored)
                summary.problems += [f"{item.document_id}: {p}" for p in result.problems]
        info["candidates"] = len(rows)
        info["needs_review"] = sum(r["review_status"] == "needs_review" for r in rows)
        info["marks"] = "imported" if item.document_id in marks else "none"
        summary.per_document.append(info)
        summary.with_corrections += item.corrected is not None
        cand_rows += rows
        mark = marks.get(item.document_id, {})
        doc_rows.append(
            {
                "document_id": item.document_id,
                "file": f"files/{target.name}",
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
                "notes": "" if item.corrected else "no corrected PDF supplied",
            }
        )
    summary.documents = len(items)
    summary.candidates = len(cand_rows)
    summary.auto_mapped = sum(r["review_status"] == "auto_mapped" for r in cand_rows)
    summary.needs_review = sum(r["review_status"] == "needs_review" for r in cand_rows)
    summary.marks_imported = len(marks)
    _write_csv(dataset / CANDIDATES, CANDIDATE_FIELDS, cand_rows)
    _write_csv(dataset / "documents.csv", DOC_FIELDS, doc_rows)
    report = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "intake": str(intake.resolve()),
        **summary.__dict__,
    }
    (dataset / "import_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (dataset / "import_report.md").write_text(_report_md(summary), encoding="utf-8")
    return summary


def _report_md(s: ImportSummary) -> str:
    lines = [
        "# Import of teacher corrections",
        "",
        f"- Documents (originals): {s.documents}, with a corrected PDF: {s.with_corrections}",
        f"- Correction candidates: {s.candidates} (auto-mapped {s.auto_mapped}, "
        f"needs review {s.needs_review}); ignored items: {s.ignored}",
        f"- Faculty marks imported: {s.marks_imported}; rejected rows: {len(s.marks_rejected)}",
        "",
        "Auto-mapped rows are suggestions too: a reviewer can still reject or relabel them.",
        "",
        "| document | corrected | candidates | needs review | channels | marks | notes |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in s.per_document:
        notes = "; ".join(d.get("problems", []) + d.get("ignored", []))
        if d.get("scanned_pages"):
            notes = f"scanned pages {d['scanned_pages']}; " + notes
        lines.append(
            f"| {d['document_id']} | {'yes' if d['corrected'] else 'no'} | {d['candidates']} | "
            f"{d['needs_review']} | {d.get('channels', {})} | {d['marks']} | {notes} |"
        )
    if s.marks_rejected:
        lines += ["", "## Marks not imported", ""] + [f"- {m}" for m in s.marks_rejected]
    if s.problems:
        lines += ["", "## Problems", ""] + [f"- {p}" for p in s.problems]
    lines += [
        "",
        "## Next steps",
        "",
        "1. Review `annotation_candidates.csv`: set `reviewer_decision` to `accept` or "
        "`reject` for every `needs_review` row; correct `final_label`, `final_severity`, "
        "`final_text` where needed.",
        "2. In `documents.csv` set `annotations_complete = yes` only for resumes where the "
        "teacher marked every issue, and fill `expected_validation` if known.",
        "3. Run `finalize-annotations`, then `eval-split`, then `evaluate`.",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- step 3


@dataclass
class FinalizeSummary:
    written: int = 0
    rejected: int = 0
    unresolved: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)


def finalize_annotations(
    dataset: Path,
    settings: Settings,
    allow_unresolved: bool = False,
    accept_suggestions: bool = False,
) -> FinalizeSummary:
    """Write ``annotations.csv`` from reviewed candidates. Only rows with
    ``reviewer_decision = accept`` are used, unless ``accept_suggestions`` is given
    explicitly (then undecided ``auto_mapped`` rows count as accepted)."""
    dataset = Path(dataset)
    rows = _rows(dataset / CANDIDATES)
    labels = set(settings.evaluation.labels)
    out: list[dict[str, str]] = []
    summary = FinalizeSummary()
    for r in rows:
        decision = r.get("reviewer_decision", "").lower()
        status = r.get("review_status", "")
        if decision == "reject":
            summary.rejected += 1
            continue
        if decision not in ("", "accept"):
            summary.invalid.append(f"{r['candidate_id']}: reviewer_decision {decision!r}")
            continue
        if decision == "" and not (accept_suggestions and status == "auto_mapped"):
            summary.unresolved.append(r["candidate_id"])
            continue
        label = r.get("final_label", "").lower()
        severity = (r.get("final_severity") or r.get("suggested_severity") or "medium").lower()
        if label not in labels:
            summary.invalid.append(f"{r['candidate_id']}: final_label {label!r} is not canonical")
            continue
        if severity not in ("high", "medium", "low"):
            summary.invalid.append(f"{r['candidate_id']}: final_severity {severity!r}")
            continue
        comment = " | ".join(
            x
            for x in (r.get("teacher_comment"), r.get("teacher_correction"), r.get("reviewer_note"))
            if x
        )
        out.append(
            {
                "document_id": r["document_id"],
                "label": label,
                "severity": severity,
                "text": r.get("final_text", ""),
                "page": r.get("page", ""),
                "criterion": "",
                "source": "faculty",
                "annotator_id": r.get("author", ""),
                "comment": comment,
            }
        )
    if summary.invalid or (summary.unresolved and not allow_unresolved):
        return summary  # nothing written until the review is complete and valid
    _write_csv(
        dataset / "annotations.csv",
        ["document_id", "label", "severity", "text", "page", "criterion", "source",
         "annotator_id", "comment"],
        out,
    )  # fmt: skip
    summary.written = len(out)
    return summary
