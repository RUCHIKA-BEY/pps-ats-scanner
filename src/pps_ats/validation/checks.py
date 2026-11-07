"""File-level and content-level validation checks. Each returns a list of Issues."""

from __future__ import annotations

from pps_ats.config import Settings
from pps_ats.schemas.common import SUPPORTED_FORMATS, FileFormat, Issue, Severity
from pps_ats.schemas.content import ExtractedContent
from pps_ats.validation.filetype import FileProbe

# Content issues meaning "nothing readable to analyse" without proving the file is not a
# resume. They route to manual review (WARNING) but block section analysis and grading.
NO_TEXT_CODES = frozenset({"IMAGE_ONLY", "NO_EXTRACTABLE_TEXT"})

_UNSUPPORTED_MESSAGES = {
    FileFormat.DOC_LEGACY: "Legacy Word (.doc) files are not supported; submit PDF or DOCX.",
    FileFormat.ODT: "OpenDocument (.odt) files are not supported; submit PDF or DOCX.",
    FileFormat.RTF: "RTF files are not supported; submit PDF or DOCX.",
    FileFormat.PNG: "File is an image (PNG), not a PDF/DOCX document.",
    FileFormat.JPEG: "File is an image (JPEG), not a PDF/DOCX document.",
    FileFormat.GIF: "File is an image (GIF), not a PDF/DOCX document.",
    FileFormat.TEXT: "Plain text files are not supported; submit PDF or DOCX.",
    FileFormat.ZIP_OTHER: "File is a ZIP archive that is not a Word document.",
    FileFormat.UNKNOWN: "File type could not be identified from its content.",
}


def file_issues(probe: FileProbe, settings: Settings) -> list[Issue]:
    issues: list[Issue] = []
    fmt = probe.detected_format

    if fmt is FileFormat.EMPTY_FILE:
        return [Issue(code="EMPTY_FILE", severity=Severity.ERROR, message="File is 0 bytes.")]
    if fmt is FileFormat.ENCRYPTED_OFFICE:
        return [
            Issue(
                code="ENCRYPTED_FILE",
                severity=Severity.ERROR,
                message="Word document is password protected.",
            )
        ]
    if fmt not in SUPPORTED_FORMATS:
        message = _UNSUPPORTED_MESSAGES.get(fmt, "Unsupported file format.")
        if probe.detail:
            message = f"{message} ({probe.detail})"
        return [
            Issue(
                code="UNSUPPORTED_FORMAT",
                severity=Severity.ERROR,
                message=message,
                details={"detected_format": fmt.value},
            )
        ]

    max_bytes = settings.limits.max_file_size_mb * 1024 * 1024
    if probe.size_bytes > max_bytes:
        issues.append(
            Issue(
                code="FILE_TOO_LARGE",
                severity=Severity.ERROR,
                message=f"File exceeds {settings.limits.max_file_size_mb} MB.",
                details={"size_bytes": probe.size_bytes},
            )
        )
    max_uncompressed = settings.limits.max_docx_uncompressed_mb * 1024 * 1024
    if probe.docx_uncompressed_bytes and probe.docx_uncompressed_bytes > max_uncompressed:
        issues.append(
            Issue(
                code="SUSPICIOUS_ARCHIVE",
                severity=Severity.ERROR,
                message="DOCX expands to an unusually large size; not processed.",
                details={"uncompressed_bytes": probe.docx_uncompressed_bytes},
            )
        )
    if probe.extension_mismatch:
        issues.append(
            Issue(
                code="EXTENSION_MISMATCH",
                severity=Severity.WARNING,
                message=(
                    f"File extension '{probe.declared_extension or '(none)'}' does not match its "
                    f"content ({fmt.value}); processed according to content."
                ),
                details={"declared_extension": probe.declared_extension, "detected": fmt.value},
            )
        )
    return issues


def content_issues(content: ExtractedContent, settings: Settings) -> list[Issue]:
    """Checks on extracted content. An ERROR, or a code in ``NO_TEXT_CODES``, means there is
    nothing gradeable."""
    cfg = settings.text
    stats = content.stats
    diag = content.diagnostics
    issues: list[Issue] = []

    if stats.char_count < cfg.min_text_chars:
        details = {"char_count": stats.char_count, "image_count": diag.image_count}
        vector_pages = [p.number for p in content.pages if (p.drawing_count or 0) > 50]
        if diag.image_count > 0:
            # A scanned/screenshot resume may be a genuine submission: route it to manual
            # review instead of rejecting it. It is never graded automatically (no text).
            issues.append(
                Issue(
                    code="IMAGE_ONLY",
                    severity=Severity.WARNING,
                    message=(
                        "No extractable text: the document consists of images (scan/screenshot). "
                        "OCR is not performed; manual review required."
                    ),
                    details=details,
                )
            )
        elif vector_pages:
            issues.append(
                Issue(
                    code="NO_EXTRACTABLE_TEXT",
                    severity=Severity.WARNING,
                    message=(
                        "No extractable text: text appears to be drawn as vector outlines. "
                        "Manual review required."
                    ),
                    details=details,
                )
            )
        else:
            issues.append(
                Issue(
                    code="EMPTY_DOCUMENT",
                    severity=Severity.ERROR,
                    message="Document contains no meaningful text or images.",
                    details=details,
                )
            )
        return issues

    if stats.garbled_char_ratio > cfg.garbled_ratio or stats.alpha_ratio < cfg.min_alpha_ratio:
        issues.append(
            Issue(
                code="GARBLED_TEXT",
                severity=Severity.WARNING,
                message="Extracted text looks garbled (font encoding problem); verify manually.",
                details={
                    "garbled_char_ratio": stats.garbled_char_ratio,
                    "alpha_ratio": stats.alpha_ratio,
                },
            )
        )

    if stats.char_count < cfg.low_text_chars or stats.word_count < cfg.low_word_count:
        issues.append(
            Issue(
                code="LOW_TEXT_CONTENT",
                severity=Severity.WARNING,
                message="Very little text was extracted; content may be incomplete or image-based.",
                details={"char_count": stats.char_count, "word_count": stats.word_count},
            )
        )

    textless = set(diag.pages_without_text)
    for page in content.pages:
        if page.number not in textless:
            continue
        if page.image_count:
            issues.append(
                Issue(
                    code="IMAGE_ONLY_PAGE",
                    severity=Severity.WARNING,
                    message="Page has images but no extractable text (likely a scanned page).",
                    page=page.number,
                )
            )
        else:
            issues.append(
                Issue(
                    code="BLANK_PAGE",
                    severity=Severity.INFO,
                    message="Page has no text or images.",
                    page=page.number,
                )
            )

    if diag.image_area_ratio >= settings.images.image_heavy_ratio:
        issues.append(
            Issue(
                code="IMAGE_HEAVY",
                severity=Severity.INFO,
                message="Document is image-heavy but has readable text; not invalidated.",
                details={"image_area_ratio": diag.image_area_ratio},
            )
        )

    if diag.pages_skipped:
        issues.append(
            Issue(
                code="TOO_MANY_PAGES",
                severity=Severity.WARNING,
                message=f"Only the first {diag.pages_parsed} pages were parsed.",
                details={"pages_skipped": diag.pages_skipped},
            )
        )

    unextracted = diag.unextracted_text_chars or 0
    if unextracted > max(20, 0.1 * stats.char_count):
        issues.append(
            Issue(
                code="EXTRACTION_INCOMPLETE",
                severity=Severity.WARNING,
                message="Some document text could not be extracted (unusual DOCX structure).",
                details={"unextracted_chars": unextracted},
            )
        )
    return issues
