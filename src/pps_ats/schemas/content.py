"""Format-neutral representation of an extracted document (PDF and DOCX)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from pps_ats.schemas.common import BBox, FileFormat


class BlockKind(StrEnum):
    PDF_TEXT = "pdf_text"  # PyMuPDF text block
    PARAGRAPH = "paragraph"  # DOCX body paragraph
    TABLE_CELL = "table_cell"  # DOCX paragraph inside a table cell
    TEXTBOX = "textbox"  # DOCX paragraph inside a floating text box / shape
    HEADER = "header"  # DOCX header paragraph
    FOOTER = "footer"  # DOCX footer paragraph


class Region(StrEnum):
    BODY = "body"
    HEADER = "header"
    FOOTER = "footer"


class FontSource(StrEnum):
    EXPLICIT = "explicit"  # set directly on the span/run
    STYLE = "style"  # inherited from a paragraph/character style
    DOC_DEFAULT = "doc_default"  # document defaults (DOCX docDefaults)
    THEME = "theme"  # resolved from the DOCX theme font
    EMBEDDED = "embedded"  # PDF: name as embedded in the file
    UNKNOWN = "unknown"


class Span(BaseModel):
    text: str
    font: str | None = None
    font_source: FontSource = FontSource.UNKNOWN
    size: float | None = None
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    color: str | None = None  # "#rrggbb"
    bbox: BBox | None = None


class Line(BaseModel):
    """PDF: a physical text line. DOCX: one logical line per paragraph (real line breaks
    are unknown without rendering)."""

    text: str
    bbox: BBox | None = None
    direction: tuple[float, float] | None = None
    spans: list[Span] = Field(default_factory=list)


class ParagraphFormat(BaseModel):
    """DOCX paragraph properties (direct formatting, falling back to the style chain)."""

    alignment: str | None = None
    space_before_pt: float | None = None
    space_after_pt: float | None = None
    line_spacing: float | None = None  # multiple (e.g. 1.15) when line_spacing_unit == "multiple"
    line_spacing_unit: str | None = None  # "multiple" | "pt"
    left_indent_pt: float | None = None
    first_line_indent_pt: float | None = None
    is_list_item: bool = False
    list_level: int | None = None


class Block(BaseModel):
    id: str
    kind: BlockKind
    region: Region = Region.BODY
    page: int | None = None  # 1-based; None for DOCX (no reliable pagination)
    bbox: BBox | None = None
    reading_order: int
    column: int | None = None  # PDF: 0-based column index when a gutter is detected
    text: str
    style_name: str | None = None
    format: ParagraphFormat | None = None
    table_id: str | None = None
    table_cell: tuple[int, int] | None = None  # (row, col)
    section_index: int | None = None  # DOCX section the header/footer belongs to
    lines: list[Line] = Field(default_factory=list)


class ImageInfo(BaseModel):
    page: int | None = None
    bbox: BBox | None = None
    width_px: int | None = None
    height_px: int | None = None
    display_width_pt: float | None = None
    display_height_pt: float | None = None
    placement: str | None = None  # DOCX: "inline" | "anchor"
    region: Region = Region.BODY


class Table(BaseModel):
    id: str
    page: int | None = None
    bbox: BBox | None = None
    rows: int
    cols: int
    cells: list[list[str | None]] = Field(default_factory=list)
    source: str  # "docx" | "pymupdf_find_tables"


class ColumnLayout(BaseModel):
    column_count: int = 1
    gutters_x: list[float] = Field(default_factory=list)


class PageInfo(BaseModel):
    number: int  # 1-based
    width_pt: float
    height_pt: float
    rotation: int = 0
    char_count: int = 0
    word_count: int = 0
    image_count: int = 0
    image_coverage_ratio: float = 0.0
    drawing_count: int | None = None  # only computed for pages with no text
    layout: ColumnLayout = Field(default_factory=ColumnLayout)
    header_block_ids: list[str] = Field(default_factory=list)
    footer_block_ids: list[str] = Field(default_factory=list)


class DocxSection(BaseModel):
    index: int
    page_width_pt: float | None = None
    page_height_pt: float | None = None
    orientation: str | None = None
    margin_top_pt: float | None = None
    margin_bottom_pt: float | None = None
    margin_left_pt: float | None = None
    margin_right_pt: float | None = None
    start_type: str | None = None
    different_first_page: bool = False


class TextStats(BaseModel):
    char_count: int = 0  # non-whitespace characters
    word_count: int = 0
    line_count: int = 0
    garbled_char_ratio: float = 0.0
    alpha_ratio: float = 0.0
    body_font_size: float | None = None  # char-weighted median span size


class ExtractionDiagnostics(BaseModel):
    parser: str
    parser_version: str
    coordinates_available: bool
    coordinate_unit: str | None = None  # "pt" when coordinates_available
    reading_order_method: str
    pages_parsed: int | None = None
    pages_skipped: int = 0
    image_count: int = 0
    image_area_ratio: float = 0.0  # PDF: mean page coverage; DOCX: display area / page area
    pages_without_text: list[int] = Field(default_factory=list)
    unextracted_text_chars: int | None = None  # DOCX: text present in XML but not extracted
    warnings: list[str] = Field(default_factory=list)


class ExtractedContent(BaseModel):
    format: FileFormat
    full_text: str  # body text in reading order (blocks joined by newlines)
    header_footer_text: str = ""
    pages: list[PageInfo] = Field(default_factory=list)  # PDF only
    docx_sections: list[DocxSection] = Field(default_factory=list)  # DOCX only
    blocks: list[Block] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    images: list[ImageInfo] = Field(default_factory=list)
    stats: TextStats = Field(default_factory=TextStats)
    diagnostics: ExtractionDiagnostics
