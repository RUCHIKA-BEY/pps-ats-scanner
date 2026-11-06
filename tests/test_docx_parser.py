from __future__ import annotations

import pytest

from pps_ats.parsers import CorruptedDocumentError, parse_docx
from pps_ats.schemas import BlockKind, FileFormat, FontSource, Region


@pytest.fixture(scope="module")
def docx_content(samples, settings):
    return parse_docx(samples["resume.docx"], settings).content


def test_docx_has_no_fake_coordinates(docx_content):
    assert docx_content.format is FileFormat.DOCX
    assert docx_content.diagnostics.coordinates_available is False
    assert docx_content.diagnostics.coordinate_unit is None
    assert docx_content.pages == []
    for block in docx_content.blocks:
        assert block.bbox is None and block.page is None
        assert all(span.bbox is None for line in block.lines for span in line.spans)


def test_docx_paragraphs_styles_and_fonts(docx_content):
    headings = [b for b in docx_content.blocks if b.style_name == "Heading 1"]
    assert [b.text for b in headings][:3] == ["Summary", "Education", "Technical Skills"]

    for block in docx_content.blocks:
        for span in block.lines[0].spans:
            assert span.font, "effective font should be resolved"
            assert span.font_source is not FontSource.UNKNOWN
            assert span.size and span.size > 0

    name = docx_content.blocks[0]
    assert name.lines[0].spans[0].bold is True
    assert name.lines[0].spans[0].size == 20.0  # explicit run size

    bullets = [b for b in docx_content.blocks if b.style_name == "List Bullet"]
    assert len(bullets) == 2 and all(b.format.is_list_item for b in bullets)
    assert all(b.format.alignment for b in docx_content.blocks)


def test_docx_tables_textboxes_headers_footers(docx_content):
    table = docx_content.tables[0]
    assert (table.rows, table.cols) == (2, 2)
    assert table.cells[1][1] == "Git, Docker, PostgreSQL, Linux"
    cells = [b for b in docx_content.blocks if b.kind is BlockKind.TABLE_CELL]
    assert {b.table_cell for b in cells} == {(0, 0), (0, 1), (1, 0), (1, 1)}

    textboxes = [b for b in docx_content.blocks if b.kind is BlockKind.TEXTBOX]
    assert [b.text for b in textboxes] == ["Portfolio: example.com/asha"]

    header = [b for b in docx_content.blocks if b.region is Region.HEADER]
    footer = [b for b in docx_content.blocks if b.region is Region.FOOTER]
    assert header[0].text == "Curriculum Vitae" and footer[0].text == "Page 1"
    assert "Curriculum Vitae" not in docx_content.full_text
    assert "Curriculum Vitae" in docx_content.header_footer_text
    assert docx_content.diagnostics.unextracted_text_chars == 0


def test_docx_section_properties(docx_content):
    section = docx_content.docx_sections[0]
    assert section.page_width_pt == 612.0 and section.page_height_pt == 792.0
    assert section.orientation == "portrait"
    assert section.margin_left_pt and section.margin_left_pt > 0


def test_docx_inline_images(samples, settings):
    content = parse_docx(samples["image_only.docx"], settings).content
    assert content.stats.char_count == 0
    assert content.diagnostics.image_count == 1
    assert content.images[0].placement == "inline"
    assert content.images[0].display_width_pt == pytest.approx(420.0, abs=0.5)


def test_corrupted_docx_raises(samples, settings):
    with pytest.raises(CorruptedDocumentError):
        parse_docx(samples["corrupted.docx"], settings)
