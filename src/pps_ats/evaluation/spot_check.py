"""Faculty spot-check package for provisional CA1 marks, and comparison of the returned review.

``build_package`` copies the clean original and the scanner-annotated PDF of each queued
document into a new private folder, writes a per-document criterion breakdown and a review
workbook (one row per scanner finding, with its evidence), and records a manifest that
fingerprints every scanner-provided cell and file. Faculty columns are always left empty:
nothing is pre-filled, suggested or inferred.

``compare_review`` reads the returned workbook, refuses it if any scanner-provided cell,
row or file changed, and reports agreement and score differences (pseudonymous ids only).

Marks remain provisional either way; this module never changes a score, rule or weight.
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill, Protection
from openpyxl.worksheet.datavalidation import DataValidation

from pps_ats.config import Settings
from pps_ats.schemas.batch import DocumentResult
from pps_ats.schemas.findings import Finding

PACKAGE_VERSION = "1"
BOOK = "faculty_spot_check.xlsx"
MANIFEST = "package_manifest.json"
DOCS_DIR = "documents"
DECISIONS = ("agree", "disagree", "needs_discussion")

OBJECTIVE = "objective_defect"
SUBJECTIVE = "subjective_judgement"
ZERO_POINT = "zero_point_recommendation"

# Mechanical checks whose evidence can be verified by looking at the text (a placeholder,
# a missing heading, a dictionary miss, an incomplete entry). Everything else is a heuristic
# or a judgement. This only labels rows for the reviewer; it does not change detection.
OBJECTIVE_RULES = frozenset(
    {
        "SEC_REQUIRED_MISSING",
        "SEC_EMPTY",
        "SEC_PLACEHOLDER_ONLY",
        "SEC_CONTACT_MISSING",
        "CONTACT_EMAIL_MISSING",
        "CONTACT_PHONE_MISSING",
        "CONTACT_EMAIL_MALFORMED",
        "CONTACT_PHONE_INCOMPLETE",
        "CONTACT_PLACEHOLDER_LINK",
        "LANG_SPELLING",
        "LANG_LOWERCASE_I",
        "LANG_REPEATED_WORD",
        "LANG_SPACE_BEFORE_PUNCT",
        "LANG_NO_SPACE_AFTER_PUNCT",
        "FMT_GARBLED_TEXT",
        "CQ_REPEATED_LINE",
        "CQ_EDUCATION_INCOMPLETE",
        "CQ_CERTIFICATION_DETAILS",
        "CQ_PROJECT_NO_DESCRIPTION",
    }
)
OBJECTIVE_PREFIXES = ("PH_",)
MIN_OBJECTIVE_CONFIDENCE = 0.6

CRITERIA = ("content_relevance", "structure_formatting", "language_professional_tone")
FACULTY_SCORE_COLUMNS = {
    "faculty_content_relevance": 10.0,
    "faculty_structure_formatting": 10.0,
    "faculty_language_professional_tone": 10.0,
    "faculty_ca1_total": 30.0,
}

# (column, header, kind): kind is "scanner" (fingerprinted, protected) or "faculty" (empty).
DOC_COLUMNS = [
    ("document_id", "Document (pseudonym)", "scanner"),
    ("score_status", "Scanner status", "scanner"),
    ("scanner_content_relevance", "Scanner Content & Relevance /10", "scanner"),
    ("scanner_structure_formatting", "Scanner Structure & Formatting /10", "scanner"),
    ("scanner_language_professional_tone", "Scanner Language & Tone /10", "scanner"),
    ("scanner_ca1_total", "Scanner CA1 /30 (PROVISIONAL)", "scanner"),
    ("deducted_marks", "Marks deducted (all criteria)", "scanner"),
    ("deductions", "Deducting findings", "scanner"),
    ("ceiling_note", "Deterministic ceiling note", "scanner"),
    ("review_reasons", "Scanner review reasons", "scanner"),
    ("spot_check_reasons", "Why selected for spot check", "scanner"),
    ("original_pdf", "Clean original PDF", "scanner"),
    ("annotated_pdf", "Scanner-annotated PDF", "scanner"),
    ("breakdown", "Criterion breakdown", "scanner"),
    ("faculty_content_relevance", "FACULTY Content & Relevance /10 (optional)", "faculty"),
    ("faculty_structure_formatting", "FACULTY Structure & Formatting /10 (optional)", "faculty"),
    ("faculty_language_professional_tone", "FACULTY Language & Tone /10 (optional)", "faculty"),
    ("faculty_ca1_total", "FACULTY CA1 /30 (optional)", "faculty"),
    ("faculty_reviewer", "Reviewer initials", "faculty"),
    ("faculty_comment", "Faculty comment", "faculty"),
]
FINDING_COLUMNS = [
    ("row_id", "Row id", "scanner"),
    ("document_id", "Document (pseudonym)", "scanner"),
    ("scanner_ca1_total", "Assigned CA1 /30 (PROVISIONAL)", "scanner"),
    ("criterion", "Criterion", "scanner"),
    ("category", "Category", "scanner"),
    ("kind", "Kind", "scanner"),
    ("rule_id", "Rule id", "scanner"),
    ("deducted_marks", "Marks deducted", "scanner"),
    ("nominal_points", "Nominal points (before caps)", "scanner"),
    ("scoring_outcome", "Scoring outcome", "scanner"),
    ("reason", "Reason", "scanner"),
    ("evidence", "Evidence (text flagged)", "scanner"),
    ("suggestion", "Scanner suggestion", "scanner"),
    ("section", "Section", "scanner"),
    ("page", "Page", "scanner"),
    ("location", "Location on page (x0, y0, x1, y1 pt)", "scanner"),
    ("highlighted", "Highlighted in annotated PDF", "scanner"),
    ("confidence", "Confidence", "scanner"),
    ("faculty_decision", "FACULTY decision", "faculty"),
    ("faculty_comment", "Faculty comment", "faculty"),
]
_HEADER_FILL = {"scanner": "1F3864", "faculty": "7F6000"}
_FACULTY_FILL = "FFF2CC"
_KIND_FILL = {OBJECTIVE: "DDEBF7", SUBJECTIVE: "FCE4D6", ZERO_POINT: "EDEDED"}


class SpotCheckError(ValueError):
    pass


@dataclass
class Queued:
    document_id: str
    reasons: str
    result: DocumentResult


# --------------------------------------------------------------------------- classification


def finding_kind(finding: Finding, settings: Settings) -> str:
    rule = settings.scoring.rules.get(finding.rule_id)
    if rule is not None and rule.points == 0:
        return ZERO_POINT
    objective = finding.rule_id in OBJECTIVE_RULES or finding.rule_id.startswith(OBJECTIVE_PREFIXES)
    if objective and not finding.review_required and finding.confidence >= MIN_OBJECTIVE_CONFIDENCE:
        return OBJECTIVE
    return SUBJECTIVE


def category(rule_id: str) -> str:
    if rule_id.startswith("CQ_PROJECT_"):
        return "project descriptions"
    if rule_id == "CQ_CERTIFICATION_DETAILS":
        return "certification details"
    if rule_id.startswith("CQ_EDUCATION"):
        return "education details"
    if rule_id.startswith(("FMT_", "LAYOUT_")):
        return "formatting and layout"
    if rule_id.startswith("LANG_") or rule_id in {"CQ_WEAK_VERB", "CQ_FILLER_PHRASE"}:
        return "language"
    if rule_id.startswith(("SEC_", "PH_")):
        return "sections and placeholders"
    if rule_id.startswith("CONTACT_"):
        return "contact details"
    if rule_id.startswith("CQ_OBJECTIVE"):
        return "career objective"
    return "other"


# --------------------------------------------------------------------------- helpers


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cell_text(value: Any) -> str:
    """Canonical text of a cell, identical before saving and after an Excel round trip."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value).replace(chr(13) + chr(10), chr(10)).strip()


def _row_digest(values: dict[str, Any], columns: list[tuple[str, str, str]]) -> str:
    text = "\x1f".join(_cell_text(values.get(c)) for c, _, kind in columns if kind == "scanner")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return ILLEGAL_CHARACTERS_RE.sub("", value)[:30000]  # Excel cell limit is 32,767
    return value


def _cleaned(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _clean(v) for k, v in row.items()}


def _load_results(batch_dir: Path) -> dict[str, DocumentResult]:
    """Results by document id: the file stem (the pseudonym), or the batch key when two
    submissions share a stem."""
    results = [
        DocumentResult.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted((batch_dir / "results").glob("*.json"))
    ]
    if not results:
        raise SpotCheckError(f"no batch results in {batch_dir / 'results'}")
    stems = Counter(Path(r.rel_path).stem for r in results)
    return {
        (Path(r.rel_path).stem if stems[Path(r.rel_path).stem] == 1 else r.key): r for r in results
    }


def _held_out(reference: Path | None) -> set[str]:
    """Evaluation-split ids from the frozen split lock (assignments only, no annotations)."""
    if reference is None:
        return set()
    lock = Path(reference) / "split_lock.json"
    if not lock.is_file():
        raise SpotCheckError(f"no split_lock.json in {reference}")
    splits: dict[str, str] = json.loads(lock.read_text(encoding="utf-8"))["splits"]
    return {doc for doc, split in splits.items() if split != "calibration"}


def _read_queue(queue: Path) -> list[tuple[str, str]]:
    with Path(queue).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    ids = [r.get("document", "").strip() for r in rows]
    if not ids or any(not i for i in ids):
        raise SpotCheckError("the queue needs a non-empty 'document' column")
    if len(set(ids)) != len(ids):
        raise SpotCheckError("the queue lists a document more than once")
    return [(r["document"].strip(), r.get("reasons", "").strip()) for r in rows]


def _location(finding: Finding) -> str:
    if not finding.bounding_box:
        return ""
    box = ", ".join(f"{v:.0f}" for v in finding.bounding_box)
    return f"({box}) {finding.location_precision.value}"


# --------------------------------------------------------------------------- package


def _finding_rows(item: Queued, settings: Settings) -> list[dict[str, Any]]:
    result = item.result
    contrib = {c.issue_id: c for c in result.scoring.contributions}
    total = result.scoring.ca1.total
    rows = []
    for f in result.analysis.findings:
        c = contrib.get(f.issue_id)
        status = result.annotation.status_for(f.issue_id).value
        rows.append(
            {
                "row_id": f"{item.document_id}:{f.issue_id}",
                "document_id": item.document_id,
                "scanner_ca1_total": total,
                "criterion": f.rubric_criterion.value,
                "category": category(f.rule_id),
                "kind": finding_kind(f, settings),
                "rule_id": f.rule_id,
                "deducted_marks": round(c.applied_points, 2) if c else 0.0,
                "nominal_points": round(c.points, 2) if c else 0.0,
                "scoring_outcome": (
                    f"{c.status.value}: {c.reason}"
                    if c and c.reason
                    else (c.status.value if c else "not scored")
                ),
                "reason": f.explanation,
                "evidence": f.detected_text or "",
                "suggestion": f.suggested_correction or "",
                "section": f.section or "",
                "page": f.page_number if f.page_number is not None else "",
                "location": _location(f),
                "highlighted": status,
                "confidence": round(f.confidence, 2),
            }
        )
    rows.sort(key=lambda r: -r["deducted_marks"])  # deducting first; stable otherwise
    return [_cleaned(r) for r in rows]


def _doc_row(item: Queued, files: dict[str, str]) -> dict[str, Any]:
    s = item.result.scoring
    by = {c.criterion: c for c in s.ca1.criteria}
    deducting = [c for c in s.contributions if c.applied_points > 0]
    return _cleaned(
        {
            "document_id": item.document_id,
            "score_status": s.status.value,
            **{f"scanner_{k}": by[k].final_score if k in by else "" for k in CRITERIA},
            "scanner_ca1_total": s.ca1.total,
            "deducted_marks": round(sum(c.applied_points for c in deducting), 2),
            "deductions": len(deducting),
            "ceiling_note": " ".join(c.ceiling_note for c in s.ca1.criteria if c.ceiling_note),
            "review_reasons": "; ".join(s.review_reasons),
            "spot_check_reasons": item.reasons,
            **files,
        }
    )


def _breakdown(item: Queued, rows: list[dict[str, Any]]) -> str:
    s = item.result.scoring
    lines = [
        f"# {item.document_id}: CA1 criterion breakdown (PROVISIONAL, NOT FACULTY APPROVED)",
        "",
        f"Scanner status: {s.status.value}. Rubric {s.ca1.rubric_version}, config "
        f"{s.config_hash[:12]}. Selected because: {item.reasons or '-'}.",
        "",
        "| criterion | max | deductions | deterministic ceiling | scanner mark |",
        "|---|---|---|---|---|",
    ]
    for c in s.ca1.criteria:
        lines.append(
            f"| {c.label} | {c.max_points:g} | {c.deductions:g} | {c.ceiling:g} | "
            f"{c.final_score:g} |"
        )
    lines += ["", f"**Total: {s.ca1.total:g} / {s.ca1.max_total:g}**", ""]
    for c in s.ca1.criteria:
        if c.ceiling_note:
            lines += [f"* Ceiling ({c.label}): {c.ceiling_note}"]
    lines += ["", "## Deduction groups", "", "| criterion | group | raw | cap | applied |"]
    lines.append("|---|---|---|---|---|")
    for c in s.ca1.criteria:
        for g in c.groups:
            cap = " (cap reached)" if g.capped else ""
            lines.append(
                f"| {c.criterion} | {g.group} | {g.raw_points:g} | {g.max_points:g} | "
                f"{g.applied_points:g}{cap} |"
            )
    lines += ["", "## Findings and evidence", ""]
    for r in rows:
        where = f"p.{r['page']} {r['location']}".strip() if r["page"] != "" else "no location"
        lines += [
            f"### {r['row_id']}: {r['rule_id']} ({r['kind']}, {r['deducted_marks']:g} marks)",
            f"* Criterion: {r['criterion']}; outcome: {r['scoring_outcome']}",
            f"* Reason: {r['reason']}",
            f"* Evidence: {r['evidence'] or '-'}",
            f"* Where: {where}; highlighted: {r['highlighted']}",
            "",
        ]
    return "\n".join(lines) + "\n"


def recurring_summary(
    results: dict[str, DocumentResult], settings: Settings, queued: set[str]
) -> list[dict[str, Any]]:
    """Deductions by category and kind over the whole batch and over the queued documents."""
    acc: dict[tuple[str, str], Counter] = defaultdict(Counter)
    docs: dict[tuple[str, str], set[str]] = defaultdict(set)
    for doc, result in results.items():
        contrib = {c.issue_id: c for c in result.scoring.contributions}
        for f in result.analysis.findings:
            c = contrib.get(f.issue_id)
            key = (category(f.rule_id), finding_kind(f, settings))
            pts = c.applied_points if c else 0.0
            acc[key]["findings"] += 1
            acc[key]["marks"] += pts
            if doc in queued:
                acc[key]["queued_findings"] += 1
                acc[key]["queued_marks"] += pts
            if pts > 0:
                docs[key].add(doc)
    out = []
    for (cat, kind), c in sorted(acc.items(), key=lambda kv: -kv[1]["marks"]):
        out.append(
            {
                "category": cat,
                "kind": kind,
                "findings": c["findings"],
                "documents_with_deductions": len(docs[(cat, kind)]),
                "marks_deducted": round(c["marks"], 2),
                "queued_findings": c["queued_findings"],
                "queued_marks_deducted": round(c["queued_marks"], 2),
            }
        )
    return out


def _sheet(wb: Workbook, title: str, columns, rows, validations=()) -> None:  # type: ignore[no-untyped-def]
    ws = wb.create_sheet(title)
    for col, (_, header, kind) in enumerate(columns, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=_HEADER_FILL[kind])
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.column_dimensions[cell.column_letter].width = 14 if kind == "faculty" else 18
    for r, values in enumerate(rows, 2):
        for col, (name, _, kind) in enumerate(columns, 1):
            cell = ws.cell(row=r, column=col, value=None if kind == "faculty" else values.get(name))
            if isinstance(cell.value, str):
                cell.data_type = "s"  # resume text is never a formula
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if kind == "faculty":
                cell.fill = PatternFill("solid", fgColor=_FACULTY_FILL)
                cell.protection = Protection(locked=False)
            elif name == "kind" and values.get(name) in _KIND_FILL:
                cell.fill = PatternFill("solid", fgColor=_KIND_FILL[values[name]])
    last = max(2, len(rows) + 1)
    names = {name: col for col, (name, _, _) in enumerate(columns, 1)}
    for name, dv in validations:
        letter = ws.cell(row=1, column=names[name]).column_letter
        dv.add(f"{letter}2:{letter}{last}")
        ws.add_data_validation(dv)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{ws.cell(row=1, column=len(columns)).column_letter}{last}"
    ws.protection.sheet = True  # scanner cells: read-only (no password; prevents slips only)
    ws.protection.autoFilter = False
    ws.protection.sort = False
    ws.protection.formatColumns = False


def _decision_validation() -> DataValidation:
    return DataValidation(
        type="list",
        formula1='"' + ",".join(DECISIONS) + '"',
        allow_blank=True,
        showErrorMessage=True,
        errorStyle="stop",
        errorTitle="Not an allowed value",
        error="Pick agree, disagree or needs_discussion.",
        showInputMessage=True,
        promptTitle="Your decision",
        prompt="Do you agree with this scanner finding and its deduction?",
    )


def _score_validation(maximum: float) -> DataValidation:
    return DataValidation(
        type="decimal",
        operator="between",
        formula1="0",
        formula2=f"{maximum:g}",
        allow_blank=True,
        showErrorMessage=True,
        errorStyle="stop",
        errorTitle="Out of range",
        error=f"Enter a mark from 0 to {maximum:g}, or leave blank.",
        showInputMessage=True,
        promptTitle="Optional faculty mark",
        prompt=f"Your own mark out of {maximum:g}. Leave blank if you do not want to mark.",
    )


INSTRUCTIONS = [
    "FACULTY SPOT CHECK - PROVISIONAL CA1 MARKS, NOT FACULTY APPROVED. Do not share with students.",
    "",
    "1. Open the 'documents' sheet. For each document open the clean original PDF and the "
    "scanner-annotated PDF (paths are relative to this workbook's folder).",
    "2. Optionally enter your own marks: each criterion out of 10 and/or the CA1 total out of 30. "
    "Leave blank if you prefer not to mark. If you give all three criteria and a total, the total "
    "must equal their sum.",
    "3. Open the 'findings' sheet. For every row choose a FACULTY decision: agree (the finding and "
    "its deduction are right), disagree (wrong or unfair) or needs_discussion. Add a comment when "
    "you disagree. Rows with 0 marks deducted still deserve a decision: they show what the "
    "scanner noticed.",
    "4. Kind: objective_defect = mechanical, verifiable evidence; subjective_judgement = heuristic "
    "or wording judgement; zero_point_recommendation = advice that never changes marks.",
    "5. Only yellow cells can be edited. Do not add, delete, re-order or edit scanner rows; the "
    "comparison refuses a workbook whose scanner cells changed. Sorting and filtering are fine.",
    "6. Content & Relevance is capped at 9 by the scanner by design (tailoring to the role cannot "
    "be checked mechanically). Award 10 in your own mark when it is deserved.",
    "7. Nothing in this workbook is pre-filled for you. Save as .xlsx with the same name.",
]


def build_package(
    batch_dir: Path,
    input_dir: Path,
    queue: Path,
    out_dir: Path,
    settings: Settings,
    reference: Path | None = None,
) -> dict[str, Any]:
    batch_dir, input_dir, out_dir = Path(batch_dir), Path(input_dir), Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SpotCheckError(f"{out_dir} is not empty: use a new folder")
    results = _load_results(batch_dir)
    held_out = _held_out(reference)
    items: list[Queued] = []
    for doc, reasons in _read_queue(queue):
        if doc in held_out:
            raise SpotCheckError(f"{doc} is not a calibration document: refusing to package it")
        if doc not in results:
            raise SpotCheckError(f"{doc} is in the queue but not in the batch results")
        items.append(Queued(doc, reasons, results[doc]))

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "package_version": PACKAGE_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "marks_status": "PROVISIONAL - NOT FACULTY APPROVED",
        "batch_dir": str(batch_dir),
        "rubric_versions": sorted({i.result.scoring.ca1.rubric_version for i in items}),
        "config_hashes": sorted({i.result.scoring.config_hash for i in items}),
        "documents": {},
        "files": {},
        "doc_rows": {},
        "finding_rows": {},
    }
    doc_rows, finding_rows = [], []
    for item in items:
        folder = out_dir / DOCS_DIR / item.document_id
        folder.mkdir(parents=True)
        source = input_dir / item.result.rel_path
        if not source.is_file():
            raise SpotCheckError(f"clean original for {item.document_id} not found")
        if sha256(source) != item.result.scoring.document_sha256:
            raise SpotCheckError(f"{item.document_id}: input file differs from the one scored")
        original = folder / "original.pdf"
        shutil.copy2(source, original)
        files = {"original_pdf": original.relative_to(out_dir).as_posix()}
        annotated_rel = item.result.annotation.output_file
        if annotated_rel and (batch_dir / annotated_rel).is_file():
            annotated = folder / "annotated.pdf"
            shutil.copy2(batch_dir / annotated_rel, annotated)
            files["annotated_pdf"] = annotated.relative_to(out_dir).as_posix()
        else:
            files["annotated_pdf"] = ""
        rows = _finding_rows(item, settings)
        breakdown = folder / "breakdown.md"
        breakdown.write_text(_breakdown(item, rows), encoding="utf-8")
        files["breakdown"] = breakdown.relative_to(out_dir).as_posix()
        doc_row = _doc_row(item, files)
        doc_rows.append(doc_row)
        finding_rows.extend(rows)
        manifest["documents"][item.document_id] = {
            "document_sha256": item.result.scoring.document_sha256,
            "scanner_ca1_total": item.result.scoring.ca1.total,
            "scanner_criteria": {
                c.criterion: c.final_score for c in item.result.scoring.ca1.criteria
            },
            "status": item.result.scoring.status.value,
        }
        for rel in filter(None, files.values()):
            manifest["files"][rel] = sha256(out_dir / rel)
        manifest["doc_rows"][item.document_id] = _row_digest(doc_row, DOC_COLUMNS)
        for r in rows:
            manifest["finding_rows"][r["row_id"]] = _row_digest(r, FINDING_COLUMNS)

    summary = recurring_summary(results, settings, {i.document_id for i in items})
    wb = Workbook()
    info = wb.active
    info.title = "instructions"
    for r, text in enumerate(INSTRUCTIONS, 1):
        info.cell(row=r, column=1, value=text).alignment = Alignment(wrap_text=True)
    info["A1"].font = Font(bold=True, color="C00000")
    info.column_dimensions["A"].width = 120
    _sheet(
        wb,
        "documents",
        DOC_COLUMNS,
        doc_rows,
        [(name, _score_validation(top)) for name, top in FACULTY_SCORE_COLUMNS.items()],
    )
    _sheet(
        wb,
        "findings",
        FINDING_COLUMNS,
        finding_rows,
        [("faculty_decision", _decision_validation())],
    )
    rec = wb.create_sheet("recurring_deductions")
    header = list(summary[0]) if summary else ["category"]
    rec.append(
        [f"Batch of {len(results)} documents; queued = the {len(items)} spot-check documents"]
    )
    rec.append(header)
    for row in summary:
        rec.append([row[h] for h in header])
    book = out_dir / BOOK
    tmp = out_dir / (BOOK + ".tmp.xlsx")
    wb.save(tmp)
    tmp.replace(book)
    manifest["workbook_created_sha256"] = sha256(book)
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out_dir / "recurring_deductions.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    (out_dir / "README.md").write_text(_readme(len(items), len(finding_rows)), encoding="utf-8")
    return {
        "documents": len(items),
        "finding_rows": len(finding_rows),
        "deducting_rows": sum(1 for r in finding_rows if r["deducted_marks"]),
        "kinds": dict(Counter(r["kind"] for r in finding_rows)),
        "workbook": str(book),
        "summary": summary,
    }


def _readme(docs: int, rows: int) -> str:
    return (
        "# Faculty spot-check package (PROVISIONAL CA1 marks, NOT FACULTY APPROVED)\n\n"
        f"{docs} documents, {rows} scanner findings. Private: do not share with students, "
        "upload or e-mail outside the faculty review.\n\n"
        + "\n".join(f"{t}" for t in INSTRUCTIONS[2:])
        + "\n\nFiles: `faculty_spot_check.xlsx` (fill in), `documents/<id>/original.pdf`, "
        "`annotated.pdf`, `breakdown.md`; `package_manifest.json` fingerprints every scanner "
        "cell and file (do not edit).\n"
    )


# --------------------------------------------------------------------------- comparison


def _read_sheet(wb, name: str, columns) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    rows = list(wb[name].iter_rows(values_only=True))
    expected = [h for _, h, _ in columns]
    header = [str(h) if h is not None else "" for h in rows[0]][: len(expected)]
    if header != expected:
        raise SpotCheckError(f"sheet '{name}': columns were added, removed or renamed")
    out = []
    for r in rows[1:]:
        if not r or all(v is None or str(v).strip() == "" for v in r):
            continue
        r = (*r, *[None] * (len(columns) - len(r)))
        out.append({c: r[i] for i, (c, _, _) in enumerate(columns)})
    return out


def _score(value: Any, top: float, where: str, problems: list[str]) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        problems.append(f"{where}: '{value}' is not a number")
        return None
    if not 0 <= number <= top:
        problems.append(f"{where}: {number:g} is outside 0-{top:g}")
        return None
    return number


def check_review(package: Path, workbook: Path | None = None) -> dict[str, Any]:
    """Integrity of a returned workbook: scanner cells, rows and files unchanged, faculty
    values valid. Returns parsed faculty input and a list of problems (empty = intact)."""
    package = Path(package)
    manifest = json.loads((package / MANIFEST).read_text(encoding="utf-8"))
    problems: list[str] = []
    for rel, digest in manifest["files"].items():
        path = package / rel
        if not path.is_file():
            problems.append(f"package file missing: {rel}")
        elif sha256(path) != digest:
            problems.append(f"package file changed: {rel}")
    wb = load_workbook(
        Path(workbook) if workbook else package / BOOK, read_only=True, data_only=True
    )
    try:
        docs = _read_sheet(wb, "documents", DOC_COLUMNS)
        findings = _read_sheet(wb, "findings", FINDING_COLUMNS)
    finally:
        wb.close()
    for rows, columns, key, expected, what in (
        (docs, DOC_COLUMNS, "document_id", manifest["doc_rows"], "document"),
        (findings, FINDING_COLUMNS, "row_id", manifest["finding_rows"], "finding"),
    ):
        seen = Counter(_cell_text(r[key]) for r in rows)
        for rid, n in seen.items():
            if n > 1:
                problems.append(f"{what} row {rid} appears {n} times")
            if rid not in expected:
                problems.append(f"unexpected {what} row {rid or '(blank id)'}")
        problems += [f"{what} row {rid} is missing" for rid in expected if rid not in seen]
        for r in rows:
            rid = _cell_text(r[key])
            if rid in expected and _row_digest(r, columns) != expected[rid]:
                problems.append(f"scanner cells of {what} row {rid} were edited")
    decisions: dict[str, dict[str, str]] = {}
    for r in findings:
        decision = _cell_text(r["faculty_decision"]).lower()
        if decision and decision not in DECISIONS:
            problems.append(f"finding {r['row_id']}: decision '{decision}' is not allowed")
            decision = ""
        decisions[_cell_text(r["row_id"])] = {
            "decision": decision,
            "comment": _cell_text(r["faculty_comment"]),
        }
    marks: dict[str, dict[str, float | None]] = {}
    for r in docs:
        doc = _cell_text(r["document_id"])
        given = {
            col: _score(r[col], top, f"{doc} {col}", problems)
            for col, top in FACULTY_SCORE_COLUMNS.items()
        }
        crit = [given[f"faculty_{c}"] for c in CRITERIA]
        stated = given["faculty_ca1_total"]
        if (
            stated is not None
            and all(v is not None for v in crit)
            and abs(sum(crit) - stated) > 0.01  # type: ignore[arg-type]
        ):
            problems.append(f"{doc}: faculty total does not equal the sum of the criteria")
        marks[doc] = given
    return {
        "manifest": manifest,
        "problems": problems,
        "decisions": decisions,
        "marks": marks,
        "findings": findings,
    }


def compare_review(package: Path, out_dir: Path, workbook: Path | None = None) -> dict[str, Any]:
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise SpotCheckError(f"{out_dir} is not empty: use a new folder")
    checked = check_review(package, workbook)
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "marks_status": "PROVISIONAL - NOT FACULTY APPROVED",
        "integrity_problems": checked["problems"],
        "intact": not checked["problems"],
    }
    if checked["problems"]:
        (out_dir / "comparison.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
    manifest, decisions = checked["manifest"], checked["decisions"]
    by_kind: dict[str, Counter] = defaultdict(Counter)
    by_rule: dict[str, Counter] = defaultdict(Counter)
    disputed_marks = Counter()
    disagreements = []
    for r in checked["findings"]:
        rid = _cell_text(r["row_id"])
        d = decisions[rid]["decision"] or "undecided"
        kind, rule = _cell_text(r["kind"]), _cell_text(r["rule_id"])
        pts = float(r["deducted_marks"] or 0)
        by_kind[kind][d] += 1
        by_rule[rule][d] += 1
        if d == "disagree":
            disputed_marks[_cell_text(r["document_id"])] += pts
        if d in ("disagree", "needs_discussion"):
            disagreements.append(
                {
                    "row_id": rid,
                    "rule_id": rule,
                    "kind": kind,
                    "deducted_marks": pts,
                    "decision": d,
                    "comment": decisions[rid]["comment"],
                }
            )

    def rate(c: Counter) -> float | None:
        n = c["agree"] + c["disagree"]
        return round(c["agree"] / n, 3) if n else None

    scores = []
    for doc, info in manifest["documents"].items():
        given = checked["marks"].get(doc, {})
        crit = {c: given.get(f"faculty_{c}") for c in CRITERIA}
        total = given.get("faculty_ca1_total")
        if total is None and all(v is not None for v in crit.values()):
            total = sum(crit.values())  # type: ignore[arg-type]
        row = {
            "document_id": doc,
            "scanner_total": info["scanner_ca1_total"],
            "faculty_total": total,
            "disputed_marks": round(disputed_marks[doc], 2),
        }
        for c in CRITERIA:
            row[f"scanner_{c}"] = info["scanner_criteria"].get(c)
            row[f"faculty_{c}"] = crit[c]
        scores.append(row)

    def diffs(key_s: str, key_f: str) -> dict[str, Any]:
        pairs = [
            (r[key_s], r[key_f]) for r in scores if r[key_f] is not None and r[key_s] is not None
        ]
        if not pairs:
            return {"n": 0}
        d = [f - s for s, f in pairs]
        return {
            "n": len(d),
            "mean_faculty_minus_scanner": round(sum(d) / len(d), 2),
            "mean_absolute_difference": round(sum(abs(x) for x in d) / len(d), 2),
            "max_absolute_difference": round(max(abs(x) for x in d), 2),
            "within_1_mark": sum(abs(x) <= 1 for x in d),
        }

    total_rows = len(checked["findings"])
    decided = sum(1 for v in decisions.values() if v["decision"])
    report.update(
        {
            "documents": len(manifest["documents"]),
            "finding_rows": total_rows,
            "decided_rows": decided,
            "review_complete": decided == total_rows,
            "documents_with_faculty_total": sum(r["faculty_total"] is not None for r in scores),
            "agreement_by_kind": {
                k: {**dict(v), "agreement_rate": rate(v)} for k, v in by_kind.items()
            },
            "agreement_by_rule": {
                k: {**dict(v), "agreement_rate": rate(v)} for k, v in sorted(by_rule.items())
            },
            "score_difference_total": diffs("scanner_total", "faculty_total"),
            "score_difference_by_criterion": {
                c: diffs(f"scanner_{c}", f"faculty_{c}") for c in CRITERIA
            },
        }
    )
    (out_dir / "comparison.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for name, rows in (("score_comparison.csv", scores), ("disagreements.csv", disagreements)):
        with (out_dir / name).open("w", newline="", encoding="utf-8") as handle:
            if rows:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
    return report
