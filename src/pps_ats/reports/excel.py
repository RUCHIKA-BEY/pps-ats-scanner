"""Consolidated CA1 workbook (openpyxl, streaming write-only mode).

Sheets: CA1 Grades, Detailed Findings, Manual Review, Processing Summary, Scoring
Configuration. One CA1 row per input submission. Documents without automatic marks
(invalid, unreadable, failed, not yet processed) get empty score cells, never 0.
Text from submissions is always written as a literal string (never a formula).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from pps_ats import __version__
from pps_ats.config import Settings
from pps_ats.schemas.batch import DocumentResult, SubmissionRecord
from pps_ats.schemas.common import ValidationStatus
from pps_ats.schemas.findings import IdentityStatus
from pps_ats.schemas.scoring import ContributionStatus, ScoreStatus
from pps_ats.scoring.engine import config_hash
from pps_ats.validation.checks import NO_TEXT_CODES

CRITERIA = ("content_relevance", "structure_formatting", "language_professional_tone")

GRADES = [
    ("Student ID", 14),
    ("Student ID Source", 11),
    ("Relative Path", 36),
    ("Filename", 30),
    ("Document SHA-256", 20),
    ("Validation Status", 13),
    ("Document Class", 12),
    ("Grading Status", 16),
    ("Content & Relevance /10", 11),
    ("Structure & Formatting /10", 11),
    ("Language & Professional Tone /10", 11),
    ("Total /30", 9),
    ("ATS Score /100", 9),
    ("ATS Band", 8),
    ("Definite Defects", 9),
    ("Potential Issues", 9),
    ("Recommendations", 9),
    ("Manual Review Required", 9),
    ("Review Reason", 60),
    ("Releasable", 9),
    ("Annotated PDF", 30),
    ("Findings Report", 30),
    ("Processing Status", 11),
    ("Rubric Version", 16),
    ("Scanner Version", 9),
    ("Scoring Fingerprint", 17),
]
FINDINGS = [
    ("Student ID", 14),
    ("Relative Path", 32),
    ("Filename", 26),
    ("Issue ID", 22),
    ("Rule", 26),
    ("Class", 14),
    ("Category", 14),
    ("Severity", 8),
    ("Rubric Criterion", 18),
    ("Detected Text", 40),
    ("Explanation", 60),
    ("Suggested Correction", 50),
    ("Page", 6),
    ("Section", 14),
    ("Location Precision", 10),
    ("Annotation Status", 12),
    ("Affected Score", 9),
    ("Points Deducted", 9),
    ("Score Note", 36),
    ("ATS Impact", 8),
    ("Review Required", 8),
    ("Confidence", 8),
]
REVIEW = [
    ("Student ID", 14),
    ("Relative Path", 36),
    ("Filename", 30),
    ("Validation Status", 13),
    ("Grading Status", 16),
    ("Review Category", 24),
    ("Specific Reasons", 90),
    ("Provisional Total /30", 10),
    ("Suggested Action", 50),
]
ACTIONS = {
    "processing_failed": "Open the file manually; re-run with --retry-failed after fixing.",
    "processing_timeout": "Check the file (very large or malformed?); --retry-failed, "
    "or grade manually.",
    "invalid": "Ask for a valid PDF/DOCX resubmission or grade manually.",
    "unreadable": "Scanned/image-only: read manually and use `review --manual`.",
    "not_a_resume": "Check that the correct document was submitted.",
    "uncertain_classification": "Confirm the document is a resume, then approve or grade.",
    "validation_warning": "Check the document renders fully, then approve or override.",
    "identity_mismatch": "Confirm the submission belongs to this student.",
    "duplicate_submission": "Identical file submitted more than once: confirm ownership.",
    "multiple_submissions": "Several files for one student: decide which one is graded.",
    "not_processed": "Run the batch again to process this file.",
    "needs_review": "Check the flagged findings, then approve or override.",
}

_HEADER_FILL = PatternFill("solid", fgColor="1F3864")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_WRAP = Alignment(wrap_text=True, vertical="top")


@dataclass
class Entry:
    """One input submission and whatever is known about it."""

    rel_path: str
    file_name: str
    key: str
    processing_status: str  # "processed" | "reused" | "failed" | "timeout" | "not_processed"
    record: SubmissionRecord | None = None
    result: DocumentResult | None = None
    batch_flags: list[tuple[str, str]] = field(default_factory=list)  # (category, reason)

    @property
    def student_id(self) -> str | None:
        if self.result is not None:
            return self.result.student_id
        return self.record.student_id if self.record else None

    @property
    def student_id_source(self) -> str:
        if self.result is not None:
            return self.result.student_id_source
        return self.record.student_id_source if self.record else "none"

    @property
    def sha256(self) -> str | None:
        if self.result is not None:
            return self.result.analysis.document.sha256
        return self.record.sha256 if self.record else None


def grading_status(entry: Entry) -> str:
    if entry.result is None:
        return {"failed": "processing_failed", "timeout": "processing_timeout"}.get(
            entry.processing_status, "not_processed"
        )
    status = entry.result.scoring.status
    if entry.batch_flags and status is ScoreStatus.PROVISIONAL:
        return ScoreStatus.NEEDS_REVIEW.value
    return status.value


def review_items(entry: Entry) -> list[tuple[str, str]]:
    """(category, specific reason) pairs; empty when no manual review is needed."""
    items: list[tuple[str, str]] = []
    if entry.result is None:
        if entry.processing_status in ("failed", "timeout") and entry.record is not None:
            reason = f"{entry.record.error_type}: {entry.record.error_message}"
            if entry.record.diagnostics_file:
                reason += f" (stack trace: {entry.record.diagnostics_file})"
            items.append((f"processing_{entry.processing_status}", reason))
        else:
            items.append(("not_processed", "not processed yet (interrupted or --limit)"))
        return items + entry.batch_flags
    analysis, scoring = entry.result.analysis, entry.result.scoring
    validation = analysis.validation
    codes = {i.code for i in validation.issues}
    issue_text = "; ".join(f"{i.code}: {i.message}" for i in validation.issues)
    doc_class = validation.classification.document_class.value
    if validation.status is ValidationStatus.INVALID:
        items.append(("invalid", issue_text or "invalid document"))
    elif codes & NO_TEXT_CODES:
        items.append(("unreadable", issue_text))
    elif doc_class == "non_resume":
        items.append(("not_a_resume", issue_text or "classified as not a resume"))
    elif doc_class == "uncertain" or "CLASSIFICATION_UNCERTAIN" in codes:
        items.append(("uncertain_classification", issue_text or "classification uncertain"))
    elif validation.status is ValidationStatus.NEEDS_REVIEW:
        items.append(("validation_warning", issue_text or "validation warning"))
    used = bool(items) and bool(issue_text)  # validation reasons already listed
    identity = analysis.identity
    if identity is not None and identity.status is IdentityStatus.POSSIBLE_MISMATCH:
        reasons = [r for r in scoring.review_reasons if r.startswith("IDENTITY_")]
        detail = "; ".join(reasons) or f"conflicting: {', '.join(identity.conflicts)}"
        items.append(("identity_mismatch", detail))
    if scoring.status in (ScoreStatus.NEEDS_REVIEW, ScoreStatus.NOT_GRADED):
        listed = " ".join(r for _, r in items)
        rest = [
            r
            for r in scoring.review_reasons
            if r not in listed and not (used and r.split(":")[0] in codes)
        ]
        if rest or not items:
            items.append(("needs_review", "; ".join(rest) or scoring.status.value))
    return items + entry.batch_flags


class _Sheet:
    def __init__(self, wb: Workbook, title: str, columns: list[tuple[str, int]], limit: int):
        self.ws = wb.create_sheet(title)
        self.limit = limit
        self.rows = 0
        for index, (_, width) in enumerate(columns, 1):
            self.ws.column_dimensions[get_column_letter(index)].width = width
        self.ws.freeze_panes = "A2"
        self.ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}1"
        header = []
        for name, _ in columns:
            cell = WriteOnlyCell(self.ws, value=name)
            cell.fill, cell.font, cell.alignment = _HEADER_FILL, _HEADER_FONT, _WRAP
            header.append(cell)
        self.ws.append(header)

    def cell(self, value: Any) -> Any:
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if not isinstance(value, str):
            return value
        text = ILLEGAL_CHARACTERS_RE.sub("", value)
        if len(text) > self.limit:
            text = text[: self.limit - 1] + "…"
        cell = WriteOnlyCell(self.ws, value=text)
        cell.data_type = "s"  # literal text: "=..." from a resume is never a formula
        return cell

    def append(self, values: list[Any]) -> None:
        self.ws.append([self.cell(v) for v in values])
        self.rows += 1


@dataclass
class _Stats:
    total: int = 0
    by_processing: dict[str, int] = field(default_factory=dict)
    by_grading: dict[str, int] = field(default_factory=dict)
    by_validation: dict[str, int] = field(default_factory=dict)
    review_rows: int = 0
    review_docs: int = 0
    findings: int = 0
    marked: list[dict[str, float]] = field(default_factory=list)  # docs with CA1 marks
    releasable: list[dict[str, float]] = field(default_factory=list)
    ats: list[float] = field(default_factory=list)

    def bump(self, table: dict[str, int], key: str) -> None:
        table[key] = table.get(key, 0) + 1


def _avg(rows: list[dict[str, float]], key: str) -> float | None:
    values = [r[key] for r in rows if key in r]
    return round(sum(values) / len(values), 2) if values else None


class WorkbookWriter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        limit = settings.reports.excel_text_limit
        self.wb = Workbook(write_only=True)
        self.grades = _Sheet(self.wb, "CA1 Grades", GRADES, limit)
        self.findings = _Sheet(self.wb, "Detailed Findings", FINDINGS, limit)
        self.review = _Sheet(self.wb, "Manual Review", REVIEW, 2000)
        self.summary_ws = self.wb.create_sheet("Processing Summary")
        self.config_ws = self.wb.create_sheet("Scoring Configuration")
        self.stats = _Stats()
        self.keys: set[str] = set()

    # ------------------------------------------------------------------ rows
    def add(self, entry: Entry) -> None:
        if entry.key in self.keys:  # one row per input submission, even if called twice
            raise ValueError(f"duplicate workbook row for {entry.key}")
        self.keys.add(entry.key)
        st = self.stats
        st.total += 1
        st.bump(st.by_processing, entry.processing_status)
        status = grading_status(entry)
        st.bump(st.by_grading, status)
        items = review_items(entry)
        result = entry.result
        scores: dict[str, float] = {}
        validation = doc_class = None
        ats = ats_band = None
        counts: dict[str, int] = {}
        rubric = fingerprint = None
        annotated = report = None
        releasable = False
        if result is not None:
            analysis, scoring = result.analysis, result.scoring
            validation = analysis.validation.status.value
            doc_class = analysis.validation.classification.document_class.value
            st.bump(st.by_validation, validation)
            counts = analysis.summary.by_class
            fingerprint = scoring.config_hash
            if scoring.ca1 is not None:
                rubric = scoring.ca1.rubric_version
                scores = {c.criterion: c.final_score for c in scoring.ca1.criteria}
                scores["total"] = scoring.ca1.total
                st.marked.append(scores)
            if scoring.ats is not None and scoring.ca1 is not None:
                ats, ats_band = scoring.ats.score, scoring.ats.band
                st.ats.append(ats)
            releasable = scoring.releasable and not entry.batch_flags
            if releasable and scoring.ca1 is not None:
                st.releasable.append(scores)
            annotated = result.annotation.output_file
            report = result.annotation.findings_report_file
            self._findings(entry)
        self.grades.append(
            [
                entry.student_id,
                entry.student_id_source,
                entry.rel_path,
                entry.file_name,
                entry.sha256,
                validation,
                doc_class,
                status,
                scores.get(CRITERIA[0]),
                scores.get(CRITERIA[1]),
                scores.get(CRITERIA[2]),
                scores.get("total"),
                ats,
                ats_band,
                counts.get("definite_defect", 0) if result else None,
                counts.get("potential_issue", 0) if result else None,
                counts.get("recommendation", 0) if result else None,
                bool(items),
                " | ".join(f"[{c}] {r}" for c, r in items),
                releasable,
                annotated,
                report,
                entry.processing_status,
                rubric or self.settings.scoring.rubric_version,
                __version__,
                fingerprint,
            ]
        )
        st.review_docs += bool(items)
        for category, reason in items:
            st.review_rows += 1
            self.review.append(
                [
                    entry.student_id,
                    entry.rel_path,
                    entry.file_name,
                    validation,
                    status,
                    category,
                    reason,
                    scores.get("total"),
                    ACTIONS.get(category, ""),
                ]
            )

    def _findings(self, entry: Entry) -> None:
        assert entry.result is not None
        result = entry.result
        contributions = {c.issue_id: c for c in result.scoring.contributions}
        for f in result.analysis.findings:
            c = contributions.get(f.issue_id)
            affected = bool(c and c.status is ContributionStatus.APPLIED and c.applied_points > 0)
            if c is None:
                note = "not part of CA1 marks (ATS/identity only)"
            elif affected:
                note = f"deducted from {c.criterion} (group {c.group})"
            else:
                note = c.reason or c.status.value
            self.stats.findings += 1
            self.findings.append(
                [
                    entry.student_id,
                    entry.rel_path,
                    entry.file_name,
                    f.issue_id,
                    f.rule_id,
                    f.finding_class.value,
                    f.category.value,
                    f.severity.value,
                    f.rubric_criterion.value,
                    f.detected_text,
                    f.explanation,
                    f.suggested_correction,
                    f.page_number,
                    f.section,
                    f.location_precision.value,
                    result.annotation.status_for(f.issue_id).value,
                    affected,
                    c.applied_points if affected and c else 0,
                    note,
                    f.ats_impact,
                    f.review_required,
                    f.confidence,
                ]
            )

    # ------------------------------------------------------------------ summary + config
    def _pairs(self, ws: Any, rows: list[tuple[Any, ...]]) -> None:
        ws.column_dimensions["A"].width = 44
        ws.column_dimensions["B"].width = 60
        for row in rows:
            if len(row) == 1:
                cell = WriteOnlyCell(ws, value=row[0])
                cell.font = Font(bold=True, size=12)
                ws.append([cell])
            else:
                ws.append([row[0], *(self.grades.cell(v) for v in row[1:])])

    def summary_rows(self, run: dict[str, Any]) -> list[tuple[Any, ...]]:
        st = self.stats
        g = st.by_grading
        processed = st.by_processing.get("processed", 0) + st.by_processing.get("reused", 0)
        rows: list[tuple[Any, ...]] = [
            ("Batch run",),
            ("Run ID", run.get("run_id")),
            ("Started (UTC)", run.get("started_at")),
            ("Finished (UTC)", run.get("finished_at")),
            ("Run state", run.get("state")),
            ("Input folder", run.get("input_dir")),
            ("Scanner version", __version__),
            ("Settings fingerprint (reuse key)", run.get("fingerprint")),
            ("Counts",),
            ("Total input submissions", st.total),
            ("Successfully processed", processed),
            ("  processed in this run", st.by_processing.get("processed", 0)),
            ("  reused from an earlier run (unchanged)", st.by_processing.get("reused", 0)),
            ("Failed (processing error)", st.by_processing.get("failed", 0)),
            ("Timed out (worker stopped)", st.by_processing.get("timeout", 0)),
            ("Per-document timeout (s, 0 = none)", run.get("document_timeout_s")),
            ("Worker crashes", run.get("worker_crashes", 0)),
            ("Not processed yet", st.by_processing.get("not_processed", 0)),
            ("Provisionally graded (releasable)", g.get("provisional", 0)),
            ("Graded, requiring review", g.get("needs_review", 0)),
            ("Faculty reviewed", g.get("faculty_reviewed", 0)),
            ("Not graded (no automatic marks)", g.get("not_graded", 0)),
            ("Invalid", st.by_validation.get("invalid", 0)),
            ("Submissions requiring manual review", st.review_docs),
            ("Manual review rows", st.review_rows),
            ("Findings listed", st.findings),
            ("Ignored system/lock files", len(run.get("ignored", []))),
            ("Average marks: all documents with provisional marks",),
            ("Documents averaged", len(st.marked)),
        ]
        labels = dict(
            zip(
                CRITERIA,
                (
                    "Content & Relevance /10",
                    "Structure & Formatting /10",
                    "Language & Professional Tone /10",
                ),
                strict=True,
            )
        )
        for key in (*CRITERIA, "total"):
            rows.append((labels.get(key, "Total /30"), _avg(st.marked, key)))
        rows.append(("ATS score /100", round(sum(st.ats) / len(st.ats), 2) if st.ats else None))
        rows += [
            ("Average marks: releasable provisional grades only",),
            ("Documents averaged", len(st.releasable)),
        ]
        for key in (*CRITERIA, "total"):
            rows.append((labels.get(key, "Total /30"), _avg(st.releasable, key)))
        timing = run.get("timing", {})
        if timing:
            rows.append(("Performance (this run)",))
            rows += [(k, v) for k, v in timing.items()]
        if run.get("ignored"):
            rows.append(("Ignored files",))
            rows += [("", name) for name in run["ignored"]]
        rows.append(("Note",))
        rows.append(
            (
                "Marks",
                "Provisional, from deterministic evidence with uncalibrated weights. "
                "Faculty review is the final authority.",
            )
        )
        return rows

    def config_rows(self) -> list[tuple[Any, ...]]:
        cfg = self.settings.scoring
        rows: list[tuple[Any, ...]] = [
            ("Versions",),
            ("Rubric version", cfg.rubric_version),
            ("Scanner version", __version__),
            ("Scoring-policy fingerprint", config_hash(cfg)),
            ("Configuration source", self.settings.source),
            ("Status", "PROVISIONAL: deduction weights are not yet calibrated with faculty"),
            ("General",),
            ("Rounding step", cfg.rounding),
            ("Band boundary margin", cfg.boundary_margin),
            ("Scale deductions by confidence", cfg.scale_by_confidence),
            ("Default group cap", cfg.default_group_cap),
            ("Criteria (label | max | floor | deterministic ceiling)",),
        ]
        for name, c in cfg.criteria.items():
            rows.append((name, f"{c.label} | {c.max_points:g} | {c.floor:g} | {c.ceiling:g}"))
        rows.append(("Bands (minimum score)",))
        rows += [(b.name, b.min_score) for b in cfg.bands]
        rows.append(("Points per finding class x severity",))
        for cls, table in cfg.class_points.items():
            rows.append((cls, ", ".join(f"{k}={v:g}" for k, v in table.items())))
        rows.append(("Group caps per criterion",))
        rows += [(name, g.max_points) for name, g in cfg.groups.items()]
        rows.append(("Rule settings",))
        for name, r in cfg.rules.items():
            rows.append((name, r.model_dump_json(exclude_none=True)))
        rows.append(("Supersedes (section-level finding covers)",))
        rows += [(name, ", ".join(p)) for name, p in cfg.supersedes.items()]
        rows.append(("AI evaluation",))
        rows.append(("Enabled", cfg.ai.enabled))
        rows.append(
            (
                "Max adjustment / min confidence",
                f"{cfg.ai.max_adjustment:g} / {cfg.ai.min_confidence:g}",
            )
        )
        rows.append(("ATS score",))
        rows.append(("Base", cfg.ats.base))
        rows.append(("Severity points", str(cfg.ats.severity_points)))
        rows.append(("Rule points", str(cfg.ats.rule_points)))
        rows.append(("Bands", str(cfg.ats.bands)))
        return rows

    def save(self, path: Path, run: dict[str, Any]) -> Path:
        self._pairs(self.summary_ws, self.summary_rows(run))
        self._pairs(self.config_ws, self.config_rows())
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.stem + ".tmp.xlsx")
        self.wb.save(tmp)
        try:
            os.replace(tmp, path)
        except PermissionError:  # the workbook is open in Excel: keep a run-specific copy
            fallback = path.with_name(f"{path.stem}_{run.get('run_id')}.xlsx")
            os.replace(tmp, fallback)
            return fallback
        return path
