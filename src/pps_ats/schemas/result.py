"""Top-level scan result and its validation/section parts."""

from __future__ import annotations

from pydantic import BaseModel, Field

from pps_ats.schemas.common import (
    SCHEMA_VERSION,
    BBox,
    DocumentClass,
    FileFormat,
    Issue,
    ProcessingError,
    SectionStatus,
    ValidationStatus,
)
from pps_ats.schemas.content import ExtractedContent


class DocumentMetadata(BaseModel):
    file_name: str
    file_size_bytes: int
    sha256: str
    declared_extension: str
    detected_format: FileFormat
    extension_mismatch: bool
    page_count: int | None = None  # PDF only; DOCX pagination needs rendering
    encrypted: bool = False
    repaired_on_open: bool = False
    title: str | None = None
    author: str | None = None
    creator: str | None = None
    producer: str | None = None
    created: str | None = None
    modified: str | None = None


class ClassificationResult(BaseModel):
    document_class: DocumentClass = DocumentClass.UNKNOWN
    confidence: float = 0.0
    resume_score: float = 0.0
    non_resume_score: float = 0.0
    signals: dict[str, float | int | str | bool | list[str]] = Field(default_factory=dict)
    note: str = "Filename is not used as a classification signal."


class ValidationResult(BaseModel):
    status: ValidationStatus
    eligible_for_grading: bool
    issues: list[Issue] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    classification: ClassificationResult = Field(default_factory=ClassificationResult)


class Section(BaseModel):
    name: str  # canonical name, e.g. "education"
    status: SectionStatus
    required: bool = False
    heading_text: str | None = None
    heading_block_id: str | None = None
    page: int | None = None
    bbox: BBox | None = None  # heading bbox (PDF only)
    content_text: str = ""
    content_char_count: int = 0
    content_block_ids: list[str] = Field(default_factory=list)
    occurrences: int = 0
    placeholder_hits: int = 0
    evidence: list[str] = Field(default_factory=list)


class ScanResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    engine_version: str
    generated_at: str
    processing_ms: int
    config_source: str
    document: DocumentMetadata
    validation: ValidationResult
    sections: list[Section] = Field(default_factory=list)
    content: ExtractedContent | None = None
    errors: list[ProcessingError] = Field(default_factory=list)
