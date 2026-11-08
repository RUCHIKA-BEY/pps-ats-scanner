"""DOCX extraction with python-docx (+ direct OOXML reads where python-docx is silent).

DOCX has no fixed layout: page numbers and coordinates are not available without
rendering, so every block/span carries ``bbox=None`` and ``page=None``.
"""

from __future__ import annotations

import contextlib
import logging
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import Any

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from lxml import etree

from pps_ats.config import Settings
from pps_ats.parsers.errors import CorruptedDocumentError
from pps_ats.parsers.text_utils import compute_text_stats
from pps_ats.schemas.common import FileFormat
from pps_ats.schemas.content import (
    Block,
    BlockKind,
    DocxSection,
    ExtractedContent,
    ExtractionDiagnostics,
    FontSource,
    ImageInfo,
    Line,
    LinkInfo,
    ParagraphFormat,
    Region,
    Span,
    Table,
)

log = logging.getLogger(__name__)

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}
_EMU_PER_PT = 12700
_TWIPS_PER_PT = 20

# Runs that contribute visible text, including ones python-docx's Paragraph.runs skips
# (tracked insertions, smart tags, simple fields, inline content controls).
_RUNS_XPATH = etree.XPath(
    "./w:r | ./w:hyperlink/w:r | ./w:ins/w:r | ./w:smartTag/w:r | ./w:fldSimple/w:r"
    " | ./w:sdt/w:sdtContent/w:r | ./w:customXml/w:r | ./w:ins/w:hyperlink/w:r",
    namespaces=NS,
)
_TEXTBOX_XPATH = etree.XPath(".//w:txbxContent[not(ancestor::mc:Fallback)]", namespaces=NS)
_DRAWING_XPATH = etree.XPath(
    ".//wp:inline[not(ancestor::mc:Fallback)] | .//wp:anchor[not(ancestor::mc:Fallback)]",
    namespaces=NS,
)
_ALL_TEXT_XPATH = etree.XPath(".//w:t[not(ancestor::mc:Fallback)]", namespaces=NS)
_FALSE_VALUES = {"0", "false", "off", "none"}


@dataclass
class DocxParseOutput:
    content: ExtractedContent
    metadata: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- formatting


def _val(element: Any, tag: str, attr: str = "w:val") -> str | None:
    if element is None:
        return None
    child = element.find(qn(tag))
    if child is None:
        return None
    return child.get(qn(attr))


def _on_off(element: Any, tag: str) -> bool | None:
    if element is None:
        return None
    child = element.find(qn(tag))
    if child is None:
        return None
    value = child.get(qn("w:val"))
    return value is None or value.lower() not in _FALSE_VALUES


def _style_chain(style: Any) -> Iterator[Any]:
    seen: set[int] = set()
    while style is not None and id(style) not in seen:
        seen.add(id(style))
        yield style
        style = style.base_style


class FormatResolver:
    """Resolves effective run/paragraph properties through the OOXML inheritance chain."""

    def __init__(self, document: Any) -> None:
        styles_el = document.styles.element
        defaults = styles_el.find(qn("w:docDefaults"))
        self.default_rpr = None
        self.default_ppr = None
        if defaults is not None:
            rpr_default = defaults.find(qn("w:rPrDefault"))
            ppr_default = defaults.find(qn("w:pPrDefault"))
            if rpr_default is not None:
                self.default_rpr = rpr_default.find(qn("w:rPr"))
            if ppr_default is not None:
                self.default_ppr = ppr_default.find(qn("w:pPr"))
        self.theme_fonts = self._theme_fonts(document)

    @staticmethod
    def _theme_fonts(document: Any) -> dict[str, str]:
        fonts: dict[str, str] = {}
        try:
            for rel in document.part.rels.values():
                if rel.reltype.endswith("/theme") and not rel.is_external:
                    root = etree.fromstring(rel.target_part.blob)
                    for kind in ("major", "minor"):
                        latin = root.find(f".//a:{kind}Font/a:latin", NS)
                        if latin is not None and latin.get("typeface"):
                            fonts[kind] = latin.get("typeface")
        except Exception:  # theme is optional
            pass
        return fonts

    def _rpr_chain(self, run: Run, paragraph: Paragraph) -> list[tuple[Any, FontSource]]:
        chain: list[tuple[Any, FontSource]] = [(run._r.rPr, FontSource.EXPLICIT)]
        try:
            run_style = (
                run.style if run._r.rPr is not None and run._r.rPr.rStyle is not None else None
            )
        except Exception:
            run_style = None
        for style in _style_chain(run_style):
            chain.append((style.element.rPr, FontSource.STYLE))
        try:
            para_style = paragraph.style
        except Exception:
            para_style = None
        for style in _style_chain(para_style):
            chain.append((style.element.rPr, FontSource.STYLE))
        chain.append((self.default_rpr, FontSource.DOC_DEFAULT))
        return [(rpr, src) for rpr, src in chain if rpr is not None]

    def span(self, run: Run, paragraph: Paragraph) -> Span:
        chain = self._rpr_chain(run, paragraph)
        font, font_source = None, FontSource.UNKNOWN
        for rpr, source in chain:
            rfonts = rpr.find(qn("w:rFonts"))
            if rfonts is None:
                continue
            name = rfonts.get(qn("w:ascii")) or rfonts.get(qn("w:hAnsi"))
            if name:
                font, font_source = name, source
                break
            theme = rfonts.get(qn("w:asciiTheme")) or rfonts.get(qn("w:hAnsiTheme"))
            if theme:
                kind = "major" if theme.startswith("major") else "minor"
                font, font_source = self.theme_fonts.get(kind, f"theme:{theme}"), FontSource.THEME
                break

        size = None
        for rpr, _ in chain:
            half_points = _val(rpr, "w:sz")
            if half_points and half_points.isdigit():
                size = int(half_points) / 2
                break

        def first_flag(tag: str) -> bool | None:
            for rpr, _ in chain:
                flag = _on_off(rpr, tag)
                if flag is not None:
                    return flag
            return False

        underline = False
        for rpr, _ in chain:
            u = _val(rpr, "w:u")
            if u is not None:
                underline = u.lower() != "none"
                break
        color = None
        for rpr, _ in chain:
            c = _val(rpr, "w:color")
            if c is not None:
                color = f"#{c.lower()}" if c.lower() != "auto" else "auto"
                break

        return Span(
            text=run.text,
            font=font,
            font_source=font_source,
            size=size,
            bold=first_flag("w:b"),
            italic=first_flag("w:i"),
            underline=underline,
            color=color,
        )

    def paragraph_format(self, paragraph: Paragraph) -> ParagraphFormat:
        chain = [paragraph._p.pPr]
        with contextlib.suppress(Exception):
            chain.extend(style.element.pPr for style in _style_chain(paragraph.style))
        chain.append(self.default_ppr)
        chain = [p for p in chain if p is not None]

        def first_attr(tag: str, attrs: tuple[str, ...]) -> tuple[str, str] | None:
            for ppr in chain:
                el = ppr.find(qn(tag))
                if el is None:
                    continue
                for attr in attrs:
                    value = el.get(qn(attr))
                    if value is not None:
                        return attr, value
            return None

        fmt = ParagraphFormat()
        jc = first_attr("w:jc", ("w:val",))
        fmt.alignment = jc[1] if jc else "left"
        before = first_attr("w:spacing", ("w:before",))
        after = first_attr("w:spacing", ("w:after",))
        fmt.space_before_pt = _twips(before[1]) if before else None
        fmt.space_after_pt = _twips(after[1]) if after else None

        for ppr in chain:
            spacing = ppr.find(qn("w:spacing"))
            if spacing is not None and spacing.get(qn("w:line")) is not None:
                line = _int(spacing.get(qn("w:line")))
                rule = spacing.get(qn("w:lineRule")) or "auto"
                if line is not None:
                    if rule == "auto":
                        fmt.line_spacing, fmt.line_spacing_unit = round(line / 240, 3), "multiple"
                    else:
                        fmt.line_spacing, fmt.line_spacing_unit = line / _TWIPS_PER_PT, "pt"
                break

        left = first_attr("w:ind", ("w:left", "w:start"))
        fmt.left_indent_pt = _twips(left[1]) if left else None
        first = first_attr("w:ind", ("w:firstLine", "w:hanging"))
        if first:
            value = _twips(first[1])
            fmt.first_line_indent_pt = -value if first[0] == "w:hanging" and value else value

        for ppr in chain:
            num_pr = ppr.find(qn("w:numPr"))
            if num_pr is not None:
                num_id = _val(num_pr, "w:numId")
                if num_id not in (None, "0"):
                    fmt.is_list_item = True
                    level = _val(num_pr, "w:ilvl")
                    fmt.list_level = _int(level) if level is not None else 0
                break
        return fmt


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _twips(value: str | None) -> float | None:
    number = _int(value)
    return round(number / _TWIPS_PER_PT, 2) if number is not None else None


# --------------------------------------------------------------------------- extraction


@dataclass
class _Collector:
    resolver: FormatResolver
    blocks: list[Block] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)

    def add_paragraph(
        self,
        paragraph: Paragraph,
        kind: BlockKind,
        region: Region = Region.BODY,
        table_id: str | None = None,
        cell: tuple[int, int] | None = None,
        section_index: int | None = None,
    ) -> None:
        spans = [
            self.resolver.span(Run(r, paragraph), paragraph) for r in _RUNS_XPATH(paragraph._p)
        ]
        spans = [s for s in spans if s.text]
        text = "".join(s.text for s in spans)
        if text.strip():
            try:
                style_name = paragraph.style.name if paragraph.style is not None else None
            except Exception:
                style_name = None
            self.spans.extend(spans)
            self.blocks.append(
                Block(
                    id=f"d-b{len(self.blocks)}",
                    kind=kind,
                    region=region,
                    reading_order=len(self.blocks),
                    text=text,
                    style_name=style_name,
                    format=self.resolver.paragraph_format(paragraph),
                    table_id=table_id,
                    table_cell=cell,
                    section_index=section_index,
                    lines=[Line(text=text, spans=spans)],
                )
            )
        # Floating text boxes anchored in this paragraph (common in resume templates).
        if kind is not BlockKind.TEXTBOX:
            for txbx in _TEXTBOX_XPATH(paragraph._p):
                for p_el in txbx.findall(qn("w:p")):
                    self.add_paragraph(
                        Paragraph(p_el, paragraph._parent),
                        BlockKind.TEXTBOX,
                        region=region,
                        section_index=section_index,
                    )

    def add_table(self, table: DocxTable, region: Region, section_index: int | None = None) -> None:
        table_id = f"d-t{len(self.tables)}"
        # Keep the cell elements themselves (not id()s): lxml proxies are transient, so
        # ids can be recycled and wrongly mark distinct cells as merged.
        seen: list[Any] = []
        grid: list[list[str | None]] = []
        model = Table(id=table_id, rows=0, cols=0, source="docx")
        self.tables.append(model)  # reserve index before nested tables
        for r, row in enumerate(table.rows):
            row_cells: list[str | None] = []
            for c, cell in enumerate(row.cells):
                if any(cell._tc is tc for tc in seen):
                    row_cells.append(None)  # merged continuation
                    continue
                seen.append(cell._tc)
                row_cells.append(cell.text)
                kind = (
                    BlockKind.TABLE_CELL
                    if region is Region.BODY
                    else (BlockKind.HEADER if region is Region.HEADER else BlockKind.FOOTER)
                )
                for item_type, item in _iter_items(cell._tc, cell):
                    if item_type == "p":
                        self.add_paragraph(item, kind, region, table_id, (r, c), section_index)
                    else:
                        self.add_table(item, region, section_index)
            grid.append(row_cells)
        model.rows = len(grid)
        model.cols = max((len(row) for row in grid), default=0)
        model.cells = grid


def _iter_items(container: Any, parent: Any) -> Iterator[tuple[str, Any]]:
    """Paragraphs and tables of a body/cell/header element in document order."""
    for child in container.iterchildren():
        if child.tag == qn("w:p"):
            yield "p", Paragraph(child, parent)
        elif child.tag == qn("w:tbl"):
            yield "tbl", DocxTable(child, parent)
        elif child.tag == qn("w:sdt"):
            content = child.find(qn("w:sdtContent"))
            if content is not None:
                yield from _iter_items(content, parent)


def _images(element: Any, region: Region) -> list[ImageInfo]:
    images: list[ImageInfo] = []
    for drawing in _DRAWING_XPATH(element):
        if drawing.find(".//pic:pic", NS) is None:
            continue  # shapes/charts without a picture
        extent = drawing.find("wp:extent", NS)
        cx = _int(extent.get("cx")) if extent is not None else None
        cy = _int(extent.get("cy")) if extent is not None else None
        images.append(
            ImageInfo(
                display_width_pt=round(cx / _EMU_PER_PT, 2) if cx else None,
                display_height_pt=round(cy / _EMU_PER_PT, 2) if cy else None,
                placement="inline" if drawing.tag.endswith("inline") else "anchor",
                region=region,
            )
        )
    return images


def _length_pt(value: Any) -> float | None:
    return round(value.pt, 2) if value is not None else None


def _docx_links(document) -> list[LinkInfo]:  # type: ignore[no-untyped-def]
    """External hyperlink targets of the main document part (no positions)."""
    out: list[LinkInfo] = []
    try:
        for rel in document.part.rels.values():
            if rel.reltype.endswith("/hyperlink") and rel.is_external:
                out.append(LinkInfo(uri=str(rel.target_ref)[:500]))
    except Exception:  # optional evidence only
        return out
    return out


def parse_docx(path: Path, settings: Settings) -> DocxParseOutput:
    try:
        document = Document(str(path))
    except (
        PackageNotFoundError,
        zipfile.BadZipFile,
        KeyError,
        ValueError,
        etree.XMLSyntaxError,
    ) as exc:
        raise CorruptedDocumentError(f"DOCX could not be opened: {type(exc).__name__}") from exc
    except Exception as exc:  # anything else from a malformed package
        raise CorruptedDocumentError(f"DOCX could not be opened: {type(exc).__name__}") from exc

    warnings: list[str] = []
    collector = _Collector(FormatResolver(document))
    body = document.element.body

    for item_type, item in _iter_items(body, document._body):
        if item_type == "p":
            collector.add_paragraph(item, BlockKind.PARAGRAPH)
        else:
            collector.add_table(item, Region.BODY)
    body_block_count = len(collector.blocks)
    images = _images(body, Region.BODY)

    sections: list[DocxSection] = []
    for index, section in enumerate(document.sections):
        sections.append(
            DocxSection(
                index=index,
                page_width_pt=_length_pt(section.page_width),
                page_height_pt=_length_pt(section.page_height),
                orientation=section.orientation.name.lower()
                if section.orientation is not None
                else None,
                margin_top_pt=_length_pt(section.top_margin),
                margin_bottom_pt=_length_pt(section.bottom_margin),
                margin_left_pt=_length_pt(section.left_margin),
                margin_right_pt=_length_pt(section.right_margin),
                start_type=section.start_type.name.lower()
                if section.start_type is not None
                else None,
                different_first_page=bool(section.different_first_page_header_footer),
            )
        )
        for attr, region in (
            ("header", Region.HEADER),
            ("first_page_header", Region.HEADER),
            ("even_page_header", Region.HEADER),
            ("footer", Region.FOOTER),
            ("first_page_footer", Region.FOOTER),
            ("even_page_footer", Region.FOOTER),
        ):
            try:
                part = getattr(section, attr)
                if part.is_linked_to_previous:
                    continue
                kind = BlockKind.HEADER if region is Region.HEADER else BlockKind.FOOTER
                for item_type, item in _iter_items(part._element, part):
                    if item_type == "p":
                        collector.add_paragraph(item, kind, region, section_index=index)
                    else:
                        collector.add_table(item, region, section_index=index)
                images.extend(_images(part._element, region))
            except Exception as exc:
                warnings.append(f"section {index} {attr}: extraction failed ({type(exc).__name__})")

    blocks = collector.blocks
    body_blocks = blocks[:body_block_count]
    body_text = "\n".join(b.text for b in body_blocks)
    header_footer_text = "\n".join(b.text for b in blocks[body_block_count:])

    # Text present in the XML that the extractor did not capture (diagnostic only).
    xml_chars = sum(len("".join((t.text or "").split())) for t in _ALL_TEXT_XPATH(body))
    extracted_chars = len("".join(body_text.split()))
    unextracted = max(0, xml_chars - extracted_chars)
    if unextracted > 20:
        warnings.append(f"{unextracted} characters in document.xml were not extracted")

    page_area = None
    if sections and sections[0].page_width_pt and sections[0].page_height_pt:
        page_area = sections[0].page_width_pt * sections[0].page_height_pt
    image_area = sum(
        (i.display_width_pt or 0) * (i.display_height_pt or 0)
        for i in images
        if i.region is Region.BODY
    )

    core = document.core_properties
    metadata = {
        "title": core.title or None,
        "author": core.author or None,
        "creator": core.last_modified_by or None,
        "producer": None,
        "created": core.created.isoformat() if core.created else None,
        "modified": core.modified.isoformat() if core.modified else None,
    }

    diagnostics = ExtractionDiagnostics(
        parser="python-docx",
        parser_version=version("python-docx"),
        coordinates_available=False,
        coordinate_unit=None,
        reading_order_method=(
            "document order (body paragraphs, table cells row-major, text boxes after their "
            "anchor paragraph); headers/footers listed after the body"
        ),
        pages_parsed=None,
        image_count=len(images),
        image_area_ratio=round(min(1.0, image_area / page_area), 4) if page_area else 0.0,
        unextracted_text_chars=unextracted,
        warnings=warnings,
    )
    content = ExtractedContent(
        format=FileFormat.DOCX,
        full_text=body_text,
        header_footer_text=header_footer_text,
        docx_sections=sections,
        blocks=blocks,
        tables=collector.tables,
        images=images,
        links=_docx_links(document),
        stats=compute_text_stats(
            body_text,
            [s for b in body_blocks for line in b.lines for s in line.spans],
            line_count=len(body_blocks),
        ),
        diagnostics=diagnostics,
    )
    return DocxParseOutput(content=content, metadata=metadata)
