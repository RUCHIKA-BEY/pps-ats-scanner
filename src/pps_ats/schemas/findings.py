"""Milestone 2: evidence-based findings and the analysis result.

``ScanResult`` (Milestone 1) is unchanged; ``AnalysisResult`` is a separate, versioned
document that embeds the scan's metadata and validation verbatim.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from pps_ats.schemas.common import BBox, ProcessingError
from pps_ats.schemas.result import DocumentMetadata, ValidationResult

ANALYSIS_SCHEMA_VERSION = "1.0.0"


class FindingClass(StrEnum):
    DEFINITE_DEFECT = "definite_defect"  # evidence-backed error (placeholder, empty section)
    POTENTIAL_ISSUE = "potential_issue"  # suspicious / incomplete / uncertain
    RECOMMENDATION = "recommendation"  # optional improvement


class FindingSeverity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingCategory(StrEnum):
    PLACEHOLDER = "placeholder"
    SECTION_COMPLETENESS = "section_completeness"
    FORMATTING = "formatting"
    LAYOUT = "layout"
    IDENTITY = "identity"
    CONTACT = "contact"
    CONTENT_QUALITY = "content_quality"
    SPELLING = "spelling"
    GRAMMAR = "grammar"


class RubricCriterion(StrEnum):
    """Faculty rubric criterion a finding relates to. ``none`` = not a rubric matter
    (e.g. identity checks, ATS-only parsing risks)."""

    CONTENT_RELEVANCE = "content_relevance"
    STRUCTURE_FORMATTING = "structure_formatting"
    LANGUAGE_TONE = "language_professional_tone"
    NONE = "none"


class DetectionMethod(StrEnum):
    PATTERN = "pattern"  # configurable regex / phrase list
    STRUCTURE = "structure"  # section / entry segmentation
    GEOMETRY = "geometry"  # measured coordinates (PDF)
    FORMAT_METADATA = "format_metadata"  # font / paragraph properties
    DICTIONARY = "dictionary"  # local spelling dictionary + allow-list
    COMPARISON = "comparison"  # identity cross-check


class LocationPrecision(StrEnum):
    """How exactly ``bounding_box`` encloses ``detected_text``. Never inferred."""

    SPAN = "span"  # union of the PDF text span(s) containing the text (may include neighbours)
    LINE = "line"
    BLOCK = "block"
    REGION = "region"  # a measured page region (e.g. a whitespace gap)
    PAGE = "page"
    NONE = "none"  # no coordinates (DOCX, or document-level finding)


class Finding(BaseModel):
    issue_id: str
    rule_id: str
    category: FindingCategory
    finding_class: FindingClass
    severity: FindingSeverity
    rubric_criterion: RubricCriterion
    ats_impact: bool = False  # may affect ATS parsing (separate from the faculty grade)
    detected_text: str | None = None
    explanation: str
    suggested_correction: str | None = None
    section: str | None = None
    page_number: int | None = None
    bounding_box: BBox | None = None
    location_precision: LocationPrecision = LocationPrecision.NONE
    block_id: str | None = None
    span_id: str | None = None  # "<block_id>:l<line>:s<span>" index into the scan content
    confidence: float = Field(ge=0.0, le=1.0)
    detection_method: DetectionMethod
    review_required: bool = False
    evidence: dict[str, float | int | str | bool | None | list[str]] = Field(default_factory=dict)


class CompletenessStatus(StrEnum):
    PRESENT = "present"  # present and populated
    MISSING = "missing"
    EMPTY = "empty"
    PLACEHOLDER_ONLY = "placeholder_only"
    UNCERTAIN = "uncertain"


class SectionImportance(StrEnum):
    REQUIRED = "required"
    RECOMMENDED = "recommended"
    OPTIONAL = "optional"


class SectionAssessment(BaseModel):
    name: str
    importance: SectionImportance
    status: CompletenessStatus
    scan_status: str  # Milestone 1 SectionStatus value
    heading_text: str | None = None
    heading_block_id: str | None = None
    page: int | None = None
    bbox: BBox | None = None
    content_char_count: int = 0
    placeholder_lines: int = 0
    evidence: list[str] = Field(default_factory=list)


class IdentityStatus(StrEnum):
    CONFIRMED_MATCH = "confirmed_match"
    POSSIBLE_MISMATCH = "possible_mismatch"
    INSUFFICIENT_INFORMATION = "insufficient_information"


class ExtractedIdentity(BaseModel):
    candidate_name: str | None = None
    name_confidence: float = 0.0
    name_block_id: str | None = None
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    student_ids: list[str] = Field(default_factory=list)
    profile_urls: list[str] = Field(default_factory=list)


class IdentityReference(BaseModel):
    source: str  # "manifest" | "filename"
    authoritative: bool
    name: str | None = None
    name_tokens: list[str] = Field(default_factory=list)
    student_ids: list[str] = Field(default_factory=list)
    email: str | None = None


class IdentityResult(BaseModel):
    status: IdentityStatus = IdentityStatus.INSUFFICIENT_INFORMATION
    extracted: ExtractedIdentity = Field(default_factory=ExtractedIdentity)
    reference: IdentityReference | None = None
    matched_on: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    note: str = "Identity checks only flag documents for manual review; they never change marks."


class ModuleRun(BaseModel):
    name: str
    status: str  # "completed" | "skipped" | "failed"
    finding_count: int = 0
    detail: str | None = None


class FindingSummary(BaseModel):
    total: int = 0
    review_required: int = 0
    by_class: dict[str, int] = Field(default_factory=dict)
    by_severity: dict[str, int] = Field(default_factory=dict)
    by_category: dict[str, int] = Field(default_factory=dict)
    by_rubric_criterion: dict[str, int] = Field(default_factory=dict)


class GradingGate(BaseModel):
    """Whether a later scoring stage may grade this document. No marks are computed here."""

    eligible_for_grading: bool
    graded: bool = False
    blocked_by: list[str] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    analysis_schema_version: str = ANALYSIS_SCHEMA_VERSION
    scan_schema_version: str
    engine_version: str
    generated_at: str
    processing_ms: int
    config_source: str
    analysis_status: str  # "completed" | "partial" | "skipped"
    skipped_reason: str | None = None
    document: DocumentMetadata
    validation: ValidationResult  # preserved verbatim from the scan
    grading: GradingGate
    review_required: bool
    review_reasons: list[str] = Field(default_factory=list)
    sections: list[SectionAssessment] = Field(default_factory=list)
    identity: IdentityResult | None = None
    findings: list[Finding] = Field(default_factory=list)
    summary: FindingSummary = Field(default_factory=FindingSummary)
    modules: list[ModuleRun] = Field(default_factory=list)
    errors: list[ProcessingError] = Field(default_factory=list)
