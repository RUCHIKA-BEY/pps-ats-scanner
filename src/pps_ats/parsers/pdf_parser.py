"""PDF extraction with PyMuPDF: text blocks/lines/spans with coordinates, images, tables,
column hints and reading order."""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

from pps_ats.config import Settings
from pps_ats.parsers.errors import CorruptedDocumentError, EncryptedDocumentError
from pps_ats.parsers.layout import LayoutBlock, band_of, column_of, find_gutters, reading_order
from pps_ats.parsers.text_utils import compute_text_stats, rgb_int_to_hex, round_bbox
from pps_ats.schemas.common import FileFormat
from pps_ats.schemas.content import (
    Block,
    BlockKind,
    ColumnLayout,
    ExtractedContent,
    ExtractionDiagnostics,
    FontSource,
    ImageInfo,
    Line,
    LinkInfo,
    PageInfo,
    Region,
    Span,
    Table,
)

log = logging.getLogger(__name__)

_FLAG_ITALIC = 2
_FLAG_BOLD = 16
_TEXT_FLAGS = (
    pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP
)
_PAGE_NUMBER_RE = re.compile(r"^\s*(page\s*)?\d+(\s*(of|/)\s*\d+)?\s*$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d+")


@dataclass
class PdfParseOutput:
    content: ExtractedContent
    metadata: dict[str, Any] = field(default_factory=dict)
    page_count: int = 0
    encrypted: bool = False
    repaired: bool = False


def _span_model(raw: dict[str, Any]) -> Span:
    font = raw.get("font") or None
    flags = int(raw.get("flags", 0))
    font_lower = (font or "").lower()
    return Span(
        text=raw.get("text", ""),
        font=font,
        font_source=FontSource.EMBEDDED if font else FontSource.UNKNOWN,
        size=round(float(raw.get("size", 0.0)), 2),
        bold=bool(flags & _FLAG_BOLD) or "bold" in font_lower or "black" in font_lower,
        italic=bool(flags & _FLAG_ITALIC) or "italic" in font_lower or "oblique" in font_lower,
        underline=None,  # PDF has no underline attribute; lines are vector drawings
        color=rgb_int_to_hex(raw.get("color")),
        bbox=round_bbox(raw["bbox"]),
    )


def _image_coverage(page_rect: pymupdf.Rect, rects: list[pymupdf.Rect]) -> float:
    """Approximate share of the page covered by images (union via coarse grid)."""
    if not rects or page_rect.is_empty:
        return 0.0
    cols, rows = 60, 80
    cw, ch = page_rect.width / cols, page_rect.height / rows
    covered = 0
    for r in range(rows):
        cy = page_rect.y0 + (r + 0.5) * ch
        for c in range(cols):
            cx = page_rect.x0 + (c + 0.5) * cw
            if any(rect.x0 <= cx <= rect.x1 and rect.y0 <= cy <= rect.y1 for rect in rects):
                covered += 1
    return round(covered / (cols * rows), 4)


def _extract_tables(page: pymupdf.Page, page_no: int, warnings: list[str]) -> list[Table]:
    tables: list[Table] = []
    try:
        finder = page.find_tables()
    except Exception as exc:  # table detection is best-effort
        warnings.append(f"page {page_no}: table detection failed ({type(exc).__name__})")
        return tables
    for index, table in enumerate(finder.tables):
        try:
            cells = table.extract()
        except Exception:
            cells = []
        tables.append(
            Table(
                id=f"p{page_no}-t{index}",
                page=page_no,
                bbox=round_bbox(table.bbox),
                rows=table.row_count,
                cols=table.col_count,
                cells=cells,
                source="pymupdf_find_tables",
            )
        )
    return tables


def _open(path: Path) -> pymupdf.Document:
    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception as exc:  # FileDataError, RuntimeError, ...
        raise CorruptedDocumentError(f"PDF could not be opened: {type(exc).__name__}") from exc
    if doc.needs_pass:
        doc.close()
        raise EncryptedDocumentError("PDF is password protected")
    if doc.page_count == 0:
        doc.close()
        raise CorruptedDocumentError("PDF has no pages")
    return doc


def parse_pdf(path: Path, settings: Settings) -> PdfParseOutput:
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.reset_mupdf_warnings()
    doc = _open(path)
    try:
        return _parse_open_document(doc, settings)
    finally:
        doc.close()


def _parse_open_document(doc: pymupdf.Document, settings: Settings) -> PdfParseOutput:
    layout_cfg = settings.layout
    warnings: list[str] = []
    pages: list[PageInfo] = []
    blocks: list[Block] = []
    tables: list[Table] = []
    images: list[ImageInfo] = []
    links: list[LinkInfo] = []
    all_spans: list[Span] = []

    max_pages = settings.limits.max_pages
    pages_to_parse = min(doc.page_count, max_pages)
    if doc.page_count > max_pages:
        warnings.append(f"only the first {max_pages} of {doc.page_count} pages were parsed")

    for page_index in range(pages_to_parse):
        page_no = page_index + 1
        try:
            page = doc.load_page(page_index)
            raw = page.get_text("dict", flags=_TEXT_FLAGS, sort=False)
        except Exception as exc:
            warnings.append(f"page {page_no}: text extraction failed ({type(exc).__name__})")
            continue
        try:
            for link in page.get_links():
                uri = link.get("uri")
                if uri:
                    links.append(
                        LinkInfo(uri=uri[:500], page=page_no, bbox=round_bbox(link["from"]))
                    )
        except Exception:  # links are optional evidence; never fail the parse
            pass

        rect = page.rect
        page_blocks: list[Block] = []
        for raw_block in raw.get("blocks", []):
            if raw_block.get("type") != 0:
                continue
            lines: list[Line] = []
            for raw_line in raw_block.get("lines", []):
                spans = [_span_model(s) for s in raw_line.get("spans", []) if s.get("text")]
                text = "".join(s.text for s in spans)
                if not text.strip():
                    continue
                direction = raw_line.get("dir")
                lines.append(
                    Line(
                        text=text,
                        bbox=round_bbox(raw_line["bbox"]),
                        direction=(round(direction[0], 3), round(direction[1], 3))
                        if direction
                        else None,
                        spans=spans,
                    )
                )
            if not lines:
                continue
            page_blocks.append(
                Block(
                    id=f"p{page_no}-b{raw_block.get('number', len(page_blocks))}",
                    kind=BlockKind.PDF_TEXT,
                    page=page_no,
                    bbox=round_bbox(raw_block["bbox"]),
                    reading_order=0,  # assigned below
                    text="\n".join(line.text for line in lines),
                    lines=lines,
                )
            )

        # Header/footer bands and column gutters (computed on body-band blocks only).
        header_ids, footer_ids = [], []
        for block in page_blocks:
            band = band_of(
                block.bbox, rect.height, layout_cfg.header_band_ratio, layout_cfg.footer_band_ratio
            )
            if band == "header":
                header_ids.append(block.id)
            elif band == "footer":
                footer_ids.append(block.id)

        layout_input = [LayoutBlock(i, b.bbox) for i, b in enumerate(page_blocks)]
        body_region = (
            rect.height * layout_cfg.header_band_ratio,
            rect.height * (1 - layout_cfg.footer_band_ratio),
        )
        gutters = find_gutters(
            layout_input, rect.width, layout_cfg.min_gutter_width_pt, body_region
        )
        order = reading_order(layout_input, gutters)
        ordered_blocks = [page_blocks[i] for i in order]
        for block in ordered_blocks:
            block.column = column_of(block.bbox, gutters) if gutters else 0

        # Images (metadata only, image bytes are never stored).
        image_rects: list[pymupdf.Rect] = []
        page_images = 0
        try:
            for info in page.get_image_info(xrefs=True):
                bbox = pymupdf.Rect(info["bbox"]) & rect
                if bbox.is_empty:
                    continue
                image_rects.append(bbox)
                page_images += 1
                images.append(
                    ImageInfo(
                        page=page_no,
                        bbox=round_bbox(bbox),
                        width_px=info.get("width"),
                        height_px=info.get("height"),
                        display_width_pt=round(bbox.width, 2),
                        display_height_pt=round(bbox.height, 2),
                    )
                )
        except Exception as exc:
            warnings.append(f"page {page_no}: image inspection failed ({type(exc).__name__})")

        page_text = "\n".join(b.text for b in ordered_blocks)
        page_spans = [s for b in ordered_blocks for line in b.lines for s in line.spans]
        all_spans.extend(page_spans)
        stats = compute_text_stats(page_text)

        drawing_count = None
        if stats.char_count == 0:
            try:
                drawing_count = len(page.get_drawings())
            except Exception:
                drawing_count = None

        if layout_cfg.detect_tables and stats.char_count > 0:
            tables.extend(_extract_tables(page, page_no, warnings))

        pages.append(
            PageInfo(
                number=page_no,
                width_pt=round(rect.width, 2),
                height_pt=round(rect.height, 2),
                rotation=page.rotation,
                char_count=stats.char_count,
                word_count=stats.word_count,
                image_count=page_images,
                image_coverage_ratio=_image_coverage(rect, image_rects),
                drawing_count=drawing_count,
                layout=ColumnLayout(column_count=len(gutters) + 1, gutters_x=gutters),
                header_block_ids=header_ids,
                footer_block_ids=footer_ids,
            )
        )
        blocks.extend(ordered_blocks)

    _mark_running_headers(blocks, pages)
    for order, block in enumerate(blocks):
        block.reading_order = order

    body_text = "\n".join(b.text for b in blocks if b.region is Region.BODY)
    header_footer_text = "\n".join(b.text for b in blocks if b.region is not Region.BODY)
    line_count = sum(len(b.lines) for b in blocks)

    for message in (pymupdf.TOOLS.mupdf_warnings() or "").splitlines()[:20]:
        warnings.append(f"mupdf: {message[:200]}")

    diagnostics = ExtractionDiagnostics(
        parser="pymupdf",
        parser_version=pymupdf.__version__,
        coordinates_available=True,
        coordinate_unit="pt",
        reading_order_method="y-sorted blocks; column-by-column within bands when gutters found",
        pages_parsed=len(pages),
        pages_skipped=doc.page_count - len(pages),
        image_count=len(images),
        image_area_ratio=round(sum(p.image_coverage_ratio for p in pages) / len(pages), 4)
        if pages
        else 0.0,
        pages_without_text=[p.number for p in pages if p.char_count == 0],
        warnings=warnings,
    )
    content = ExtractedContent(
        format=FileFormat.PDF,
        full_text=body_text,
        header_footer_text=header_footer_text,
        pages=pages,
        blocks=blocks,
        tables=tables,
        images=images,
        links=links,
        stats=compute_text_stats(body_text, all_spans, line_count),
        diagnostics=diagnostics,
    )
    meta = doc.metadata or {}
    return PdfParseOutput(
        content=content,
        metadata=meta,
        page_count=doc.page_count,
        encrypted=bool(doc.is_encrypted),
        repaired=bool(getattr(doc, "is_repaired", False)),
    )


def _mark_running_headers(blocks: list[Block], pages: list[PageInfo]) -> None:
    """Mark band blocks as header/footer only when they look like real running content:
    page numbers, or text repeated (digits ignored) on at least two pages."""
    header_ids = {bid for p in pages for bid in p.header_block_ids}
    footer_ids = {bid for p in pages for bid in p.footer_block_ids}
    candidates = [b for b in blocks if b.id in header_ids or b.id in footer_ids]
    signature = Counter(_DIGITS_RE.sub("#", b.text.strip().lower()) for b in candidates)
    page_numbers = {b.page for b in candidates}
    for block in candidates:
        repeated = (
            len(page_numbers) > 1 and signature[_DIGITS_RE.sub("#", block.text.strip().lower())] > 1
        )
        if repeated or _PAGE_NUMBER_RE.match(block.text):
            block.region = Region.HEADER if block.id in header_ids else Region.FOOTER
