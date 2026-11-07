"""Shared enums and small models used across the schemas."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0.0"

BBox = tuple[float, float, float, float]
"""(x0, y0, x1, y1) in PDF points, origin top-left, as reported by PyMuPDF."""


class FileFormat(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    DOC_LEGACY = "doc_legacy"
    ENCRYPTED_OFFICE = "encrypted_office"
    ODT = "odt"
    RTF = "rtf"
    PNG = "png"
    JPEG = "jpeg"
    GIF = "gif"
    ZIP_OTHER = "zip_other"
    TEXT = "text"
    EMPTY_FILE = "empty_file"
    UNKNOWN = "unknown"


SUPPORTED_FORMATS = frozenset({FileFormat.PDF, FileFormat.DOCX})


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"  # processed, but a human should look (=> needs_review)
    ERROR = "error"  # cannot be processed/graded (=> invalid)


class ValidationStatus(StrEnum):
    VALID = "valid"
    NEEDS_REVIEW = "needs_review"
    INVALID = "invalid"


class DocumentClass(StrEnum):
    RESUME = "resume"
    NON_RESUME = "non_resume"
    UNCERTAIN = "uncertain"
    UNKNOWN = "unknown"  # classification not attempted (e.g. unreadable file)


class SectionStatus(StrEnum):
    DETECTED = "detected"  # heading found with meaningful content
    EMPTY = "empty"  # heading found, no content or placeholder-only content
    UNCERTAIN = "uncertain"  # weak heading evidence or very little content
    MISSING = "missing"  # no evidence of the section


class Issue(BaseModel):
    """A validation finding. ``code`` is stable and machine-readable."""

    code: str
    severity: Severity
    message: str
    page: int | None = None
    details: dict[str, float | int | str | bool | None] = Field(default_factory=dict)


class ProcessingError(BaseModel):
    stage: str
    error_type: str
    message: str
