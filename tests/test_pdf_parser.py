from __future__ import annotations

import pytest

from pps_ats.parsers import CorruptedDocumentError, EncryptedDocumentError, parse_pdf
from pps_ats.schemas import BlockKind, FileFormat


def _block(content, text):
    return next(b for b in content.blocks if b.text.strip().startswith(text))


def test_readable_pdf_text_pages_and_coordinates(samples, settings):
    out = parse_pdf(samples["resume.pdf"], settings)
    content = out.content

    assert out.page_count == 1
    assert content.format is FileFormat.PDF
    page = content.pages[0]
    assert (page.width_pt, page.height_pt) == (595.0, 842.0)
    assert page.char_count > 500

    diag = content.diagnostics
    assert diag.coordinates_available is True and diag.coordinate_unit == "pt"
    assert "EDUCATION" in content.full_text and "Weather Data Pipeline" in content.full_text

    # Every block, line and span carries coordinates inside the page.
    for block in content.blocks:
        assert block.kind is BlockKind.PDF_TEXT and block.page == 1
        x0, y0, x1, y1 = block.bbox
        assert 0 <= x0 < x1 <= page.width_pt and 0 <= y0 < y1 <= page.height_pt
        for line in block.lines:
            assert line.bbox is not None
            for span in line.spans:
                assert span.bbox is not None and span.font and span.size

    heading_span = _block(content, "EDUCATION").lines[0].spans[0]
    body_span = _block(content, "B.Tech").lines[0].spans[0]
    assert heading_span.bold is True and body_span.bold is False
    assert heading_span.size > body_span.size
    assert content.stats.body_font_size == pytest.approx(10.0)

    orders = [b.reading_order for b in content.blocks]
    assert orders == list(range(len(orders)))
    # Original text order preserved: name before education before projects.
    text = content.full_text
    assert text.index("ASHA EXAMPLE") < text.index("EDUCATION") < text.index("PROJECTS")


def test_two_column_reading_order(samples, settings):
    content = parse_pdf(samples["resume_two_column.pdf"], settings).content
    layout = content.pages[0].layout
    assert layout.column_count == 2 and len(layout.gutters_x) == 1

    skills, education = _block(content, "SKILLS"), _block(content, "EDUCATION")
    summary, experience = _block(content, "SUMMARY"), _block(content, "EXPERIENCE")
    assert skills.column == 0 and summary.column == 1
    # Left column is read completely before the right column.
    assert skills.reading_order < education.reading_order < summary.reading_order
    assert summary.reading_order < experience.reading_order
    # The full-width contact line spans the gutter.
    assert _block(content, "asha.example").column is None


def test_images_are_reported_with_bbox_and_coverage(samples, settings):
    content = parse_pdf(samples["resume_image_heavy.pdf"], settings).content
    assert content.diagnostics.image_count == 1
    image = content.images[0]
    assert image.page == 1 and image.bbox is not None and image.width_px == 200
    assert content.pages[0].image_coverage_ratio >= 0.5
    assert content.stats.char_count > 500  # still readable


def test_image_only_pdf_has_no_text_but_images(samples, settings):
    content = parse_pdf(samples["resume_scanned_image_only.pdf"], settings).content
    assert content.stats.char_count == 0
    assert content.diagnostics.image_count == 1
    assert content.diagnostics.pages_without_text == [1]


def test_encrypted_pdf_raises(samples, settings):
    with pytest.raises(EncryptedDocumentError):
        parse_pdf(samples["encrypted.pdf"], settings)


def test_corrupted_pdf_raises(samples, settings):
    with pytest.raises(CorruptedDocumentError):
        parse_pdf(samples["corrupted.pdf"], settings)


def test_tables_are_detected_when_present(samples, settings):
    # Detection is best effort; the call must succeed and return structured tables.
    content = parse_pdf(samples["grade_card.pdf"], settings).content
    for table in content.tables:
        assert table.rows >= 1 and table.cols >= 1 and table.bbox is not None
