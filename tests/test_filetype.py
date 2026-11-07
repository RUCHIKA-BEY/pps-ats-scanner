from __future__ import annotations

import pytest

from pps_ats.schemas import FileFormat
from pps_ats.validation.filetype import detect_format


@pytest.mark.parametrize(
    ("sample", "expected", "mismatch"),
    [
        ("resume.pdf", FileFormat.PDF, False),
        ("resume.docx", FileFormat.DOCX, False),
        ("docx_named_pdf.pdf", FileFormat.DOCX, True),
        ("photo.pdf", FileFormat.PNG, True),
        ("notes.txt", FileFormat.TEXT, False),
        ("zero_bytes.pdf", FileFormat.EMPTY_FILE, False),
        ("legacy.doc", FileFormat.DOC_LEGACY, False),
        ("password_protected.docx", FileFormat.ENCRYPTED_OFFICE, False),
        ("corrupted.docx", FileFormat.DOCX, False),  # broken zip, routed to parser by ext
        ("corrupted.pdf", FileFormat.PDF, False),
    ],
)
def test_detects_format_from_content(samples, sample, expected, mismatch):
    probe = detect_format(samples[sample])
    assert probe.detected_format is expected
    assert probe.extension_mismatch is mismatch
    assert len(probe.sha256) == 64


def test_docx_uncompressed_size_is_recorded(samples):
    probe = detect_format(samples["resume.docx"])
    assert probe.docx_uncompressed_bytes and probe.docx_uncompressed_bytes > probe.size_bytes
