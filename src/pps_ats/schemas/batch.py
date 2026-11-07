"""Milestone 4: annotation outcomes, per-document batch results and the processing journal."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from pps_ats.schemas.common import BBox
from pps_ats.schemas.findings import AnalysisResult
from pps_ats.schemas.scoring import ScoringResult

BATCH_SCHEMA_VERSION = "1.0.0"


class AnnotationStatus(StrEnum):
    WORD = "word"  # the detected text was found inside the finding's span box and verified
    AREA = "area"  # box drawn from the parser's own span/line/block/region coordinates
    PAGE_NOTE = "page_note"  # page-level finding: a note on that page, no box
    NOT_LOCATED = "not_located"  # no reliable coordinates: listed in the summary note/report
    REPORT_ONLY = "report_only"  # DOCX: findings report only (no rendered coordinates)
    NOT_ANNOTATED = "not_annotated"  # annotation disabled, skipped document, or failure


class FindingAnnotation(BaseModel):
    issue_id: str
    rule_id: str
    status: AnnotationStatus
    method: str | None = None  # "span_clip_search" | "unique_on_page" | "parser_bbox" | ...
    page: int | None = None
    rects: list[BBox] = Field(default_factory=list)
    verified: bool = False  # text under the highlight re-read from the page and matched
    note: str | None = None


class AnnotationReport(BaseModel):
    kind: str  # "annotated_pdf" | "findings_report" | "none"
    output_file: str | None = None  # relative to the batch output folder
    findings_report_file: str | None = None
    source_unchanged: bool | None = None  # original hash re-checked after annotating
    findings: list[FindingAnnotation] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    error: str | None = None

    def status_for(self, issue_id: str) -> AnnotationStatus:
        for item in self.findings:
            if item.issue_id == issue_id:
                return item.status
        return AnnotationStatus.NOT_ANNOTATED


class ProcessingStatus(StrEnum):
    PROCESSING = "processing"  # started; a crash leaves this and the file is re-run
    DONE = "done"
    FAILED = "failed"
    TIMEOUT = "timeout"  # stopped after [batch] document_timeout_s of active processing


class SubmissionRecord(BaseModel):
    """One journal line. The latest line per ``key`` is the current state."""

    key: str  # stable id derived from the relative path
    rel_path: str
    file_name: str
    size_bytes: int
    mtime_ns: int
    sha256: str | None = None
    fingerprint: str  # settings + manifest + engine: reuse a result only if unchanged
    status: ProcessingStatus
    attempts: int = 0
    run_id: str
    result_file: str | None = None
    student_id: str | None = None  # for batch-level duplicate checks without re-reading results
    student_id_source: str = "none"
    error_type: str | None = None
    error_message: str | None = None
    diagnostics_file: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None


class DocumentResult(BaseModel):
    """Everything produced for one submission (``results/<key>.json``)."""

    batch_schema_version: str = BATCH_SCHEMA_VERSION
    key: str
    rel_path: str
    file_name: str
    student_id: str | None = None
    student_id_source: str = "none"  # "manifest" | "document" | "none"
    analysis: AnalysisResult
    scoring: ScoringResult
    annotation: AnnotationReport
    timings_ms: dict[str, int] = Field(default_factory=dict)
