"""End-to-end validation outcomes through the pipeline."""

from __future__ import annotations

import pytest

from pps_ats.pipeline import scan_file
from pps_ats.schemas import DocumentClass, Severity, ValidationStatus

V, R, INV = ValidationStatus.VALID, ValidationStatus.NEEDS_REVIEW, ValidationStatus.INVALID


@pytest.mark.parametrize(
    ("sample", "status", "doc_class", "code"),
    [
        ("resume.pdf", V, DocumentClass.RESUME, None),
        ("resume_two_column.pdf", V, DocumentClass.RESUME, None),
        ("resume.docx", V, DocumentClass.RESUME, None),
        ("resume_image_heavy.pdf", V, DocumentClass.RESUME, "IMAGE_HEAVY"),
        ("blank.pdf", INV, DocumentClass.UNKNOWN, "EMPTY_DOCUMENT"),
        ("empty.docx", INV, DocumentClass.UNKNOWN, "EMPTY_DOCUMENT"),
        ("zero_bytes.pdf", INV, DocumentClass.UNKNOWN, "EMPTY_FILE"),
        ("corrupted.pdf", INV, DocumentClass.UNKNOWN, "CORRUPTED_FILE"),
        ("corrupted.docx", INV, DocumentClass.UNKNOWN, "CORRUPTED_FILE"),
        ("encrypted.pdf", INV, DocumentClass.UNKNOWN, "ENCRYPTED_FILE"),
        ("password_protected.docx", INV, DocumentClass.UNKNOWN, "ENCRYPTED_FILE"),
        ("notes.txt", INV, DocumentClass.UNKNOWN, "UNSUPPORTED_FORMAT"),
        ("photo.pdf", INV, DocumentClass.UNKNOWN, "UNSUPPORTED_FORMAT"),
        ("legacy.doc", INV, DocumentClass.UNKNOWN, "UNSUPPORTED_FORMAT"),
        ("resume_scanned_image_only.pdf", R, DocumentClass.UNKNOWN, "IMAGE_ONLY"),
        ("image_only.docx", R, DocumentClass.UNKNOWN, "IMAGE_ONLY"),
        ("grade_card.pdf", INV, DocumentClass.NON_RESUME, "NOT_A_RESUME"),
        ("essay.pdf", INV, DocumentClass.NON_RESUME, "NOT_A_RESUME"),
        ("docx_named_pdf.pdf", R, DocumentClass.RESUME, "EXTENSION_MISMATCH"),
    ],
)
def test_validation_outcomes(samples, settings, sample, status, doc_class, code):
    result = scan_file(samples[sample], settings)
    validation = result.validation
    assert validation.status is status, validation.review_reasons
    assert validation.classification.document_class is doc_class
    if code:
        assert code in {i.code for i in validation.issues}
    else:
        assert not [i for i in validation.issues if i.severity is not Severity.INFO]
    # Grading gate invariant.
    assert validation.eligible_for_grading is (status is V and doc_class is DocumentClass.RESUME)
    if status is not V:
        assert validation.review_reasons


def test_image_heavy_resume_is_not_invalidated(samples, settings):
    result = scan_file(samples["resume_image_heavy.pdf"], settings)
    heavy = next(i for i in result.validation.issues if i.code == "IMAGE_HEAVY")
    assert heavy.severity is Severity.INFO
    assert result.validation.eligible_for_grading


def test_filename_does_not_drive_classification(samples, settings):
    grade_card = scan_file(samples["misleading_resume_name.pdf"], settings)
    resume = scan_file(samples["misleading_gradecard_name.pdf"], settings)
    assert grade_card.validation.classification.document_class is DocumentClass.NON_RESUME
    assert resume.validation.classification.document_class is DocumentClass.RESUME


def test_non_resume_signals_are_explained(samples, settings):
    signals = scan_file(samples["grade_card.pdf"], settings).validation.classification.signals
    assert {"grade card", "seat no", "sgpa"} <= set(signals["negative_phrases"])


def test_mismatched_extension_still_parsed_by_content(samples, settings):
    result = scan_file(samples["docx_named_pdf.pdf"], settings)
    assert result.document.detected_format.value == "docx"
    assert result.content is not None and result.content.format.value == "docx"


def test_missing_file_raises(tmp_path, settings):
    with pytest.raises(FileNotFoundError):
        scan_file(tmp_path / "nope.pdf", settings)


def test_too_little_text_needs_review(tmp_path, settings):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((50, 60), "Asha Example - Skills: Python and Java", fontsize=11)
    path = tmp_path / "tiny.pdf"
    doc.save(path)
    result = scan_file(path, settings)
    assert result.validation.status is not V
    assert "LOW_TEXT_CONTENT" in {i.code for i in result.validation.issues}
    assert not result.validation.eligible_for_grading


@pytest.mark.parametrize("sample", ["resume_scanned_image_only.pdf", "image_only.docx"])
def test_image_only_goes_to_review_not_rejection(samples, settings, sample):
    """Milestone 2 correction: unreadable but possibly genuine resumes need a human,
    they are neither rejected automatically nor graded."""
    result = scan_file(samples[sample], settings)
    assert result.validation.status is R
    assert not result.validation.eligible_for_grading
    assert result.sections == []  # nothing readable to segment
    assert any(r.startswith("IMAGE_ONLY") for r in result.validation.review_reasons)
