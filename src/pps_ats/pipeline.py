"""End-to-end scan of one file: detect -> validate file -> parse -> content checks ->
sections -> classify -> final status. Never raises for document problems; they are
reported in the result. Never logs document text."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from pps_ats import __version__
from pps_ats.analysis.sections import extract_sections
from pps_ats.config import Settings, default_settings
from pps_ats.parsers import DocumentParseError, parse_docx, parse_pdf
from pps_ats.schemas.common import FileFormat, Issue, ProcessingError, Severity
from pps_ats.schemas.content import ExtractedContent
from pps_ats.schemas.result import ClassificationResult, DocumentMetadata, ScanResult, Section
from pps_ats.validation import (
    NO_TEXT_CODES,
    classification_issues,
    classify,
    content_issues,
    decide,
    detect_format,
    file_issues,
)

log = logging.getLogger(__name__)


def _str_or_none(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def scan_file(path: str | Path, settings: Settings | None = None) -> ScanResult:
    settings = settings or default_settings()
    started = time.perf_counter()
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Not a file: {path}")

    probe = detect_format(path)
    metadata = DocumentMetadata(
        file_name=path.name,
        file_size_bytes=probe.size_bytes,
        sha256=probe.sha256,
        declared_extension=probe.declared_extension,
        detected_format=probe.detected_format,
        extension_mismatch=probe.extension_mismatch,
        encrypted=probe.detected_format is FileFormat.ENCRYPTED_OFFICE,
    )
    issues: list[Issue] = file_issues(probe, settings)
    errors: list[ProcessingError] = []
    content: ExtractedContent | None = None
    sections: list[Section] = []
    classification = ClassificationResult()

    if not any(i.severity is Severity.ERROR for i in issues):
        try:
            if probe.detected_format is FileFormat.PDF:
                parsed = parse_pdf(path, settings)
                metadata.page_count = parsed.page_count
                metadata.encrypted = parsed.encrypted
                metadata.repaired_on_open = parsed.repaired
                meta = parsed.metadata
                metadata.title = _str_or_none(meta.get("title"))
                metadata.author = _str_or_none(meta.get("author"))
                metadata.creator = _str_or_none(meta.get("creator"))
                metadata.producer = _str_or_none(meta.get("producer"))
                metadata.created = _str_or_none(meta.get("creationDate"))
                metadata.modified = _str_or_none(meta.get("modDate"))
            else:
                parsed_docx = parse_docx(path, settings)
                parsed = parsed_docx
                for key, value in parsed_docx.metadata.items():
                    setattr(metadata, key, value)
            content = parsed.content
        except DocumentParseError as exc:
            issues.append(Issue(code=exc.code, severity=Severity.ERROR, message=exc.message))
            if exc.code == "ENCRYPTED_FILE":
                metadata.encrypted = True
        except Exception as exc:  # unexpected parser failure: record, never crash a batch
            log.exception("parser failure file=%s", path.name)
            errors.append(
                ProcessingError(
                    stage="parse", error_type=type(exc).__name__, message=str(exc)[:300]
                )
            )
            issues.append(
                Issue(
                    code="PARSER_FAILURE",
                    severity=Severity.ERROR,
                    message="Unexpected error while parsing; manual review required.",
                )
            )

    if content is not None:
        content_problems = content_issues(content, settings)
        issues.extend(content_problems)
        if not any(
            i.severity is Severity.ERROR or i.code in NO_TEXT_CODES for i in content_problems
        ):
            try:
                analysis = extract_sections(content, settings.sections)
                sections = analysis.sections
                classification = classify(
                    content, analysis, settings.classifier, metadata.page_count
                )
                issues.extend(classification_issues(classification))
            except Exception as exc:
                log.exception("analysis failure file=%s", path.name)
                errors.append(
                    ProcessingError(
                        stage="analysis", error_type=type(exc).__name__, message=str(exc)[:300]
                    )
                )
                issues.append(
                    Issue(
                        code="ANALYSIS_FAILURE",
                        severity=Severity.WARNING,
                        message="Section/classification analysis failed; manual review required.",
                    )
                )

    validation = decide(issues, classification)
    result = ScanResult(
        engine_version=__version__,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        processing_ms=int((time.perf_counter() - started) * 1000),
        config_source=settings.source,
        document=metadata,
        validation=validation,
        sections=sections,
        content=content,
        errors=errors,
    )
    log.info(
        "scanned file=%s sha256=%s format=%s status=%s class=%s ms=%d",
        path.name,
        probe.sha256[:12],
        probe.detected_format.value,
        validation.status.value,
        classification.document_class.value,
        result.processing_ms,
    )
    return result
