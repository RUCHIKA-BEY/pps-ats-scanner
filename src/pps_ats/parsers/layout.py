"""Page-layout heuristics for PDF pages: column gutters, reading order, header/footer bands.

These are deliberately simple and explainable. Results are hints for later milestones,
not ground truth; the raw block coordinates are always kept alongside them.
"""

from __future__ import annotations

from dataclasses import dataclass

from pps_ats.schemas.common import BBox


@dataclass
class LayoutBlock:
    key: int  # index into the caller's block list
    bbox: BBox


def _height(blocks: list[LayoutBlock]) -> float:
    return sum(b.bbox[3] - b.bbox[1] for b in blocks)


def _vertical_overlap(a: list[LayoutBlock], b: list[LayoutBlock]) -> float:
    """Length of y-range where both groups have text (pairwise interval overlap, merged)."""
    ranges = sorted(
        (max(x.bbox[1], y.bbox[1]), min(x.bbox[3], y.bbox[3]))
        for x in a
        for y in b
        if min(x.bbox[3], y.bbox[3]) > max(x.bbox[1], y.bbox[1])
    )
    total, cur_start, cur_end = 0.0, None, None
    for start, end in ranges:
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_end is not None:
        total += cur_end - cur_start
    return total


def find_gutters(
    blocks: list[LayoutBlock], page_width: float, min_gutter: float, region: tuple[float, float]
) -> list[float]:
    """Detect a two-column split and return the x of its gutter ([] for single column).

    A candidate split at x (the left edge of some block) is accepted when:
      * blocks entirely left (ending >= ``min_gutter`` before x) and entirely right of x
        each carry >= 15% of the body height of text,
      * the two sides sit side by side for >= 10% of the body height, and
      * blocks crossing x (full-width lines, e.g. a contact bar) are a minority.
    Crossing blocks are later treated as band separators by ``reading_order``.
    Only blocks whose vertical centre is inside ``region`` (the body band) are considered.
    """
    top, bottom = region
    body = [b for b in blocks if top <= (b.bbox[1] + b.bbox[3]) / 2 <= bottom]
    if len(body) < 2:
        return []
    body_height = bottom - top
    best: tuple[float, float] | None = None  # (score, gutter_x)
    for x in sorted({round(b.bbox[0], 1) for b in body}):
        if not 0.15 * page_width <= x <= 0.85 * page_width:
            continue
        left = [b for b in body if b.bbox[2] <= x - min_gutter]
        right = [b for b in body if b.bbox[0] >= x - 0.5]
        crossing = [b for b in body if b not in left and b not in right]
        left_h, right_h = _height(left), _height(right)
        if min(left_h, right_h) < 0.15 * body_height:
            continue
        if _height(crossing) >= min(left_h, right_h):
            continue
        if _vertical_overlap(left, right) < 0.10 * body_height:
            continue
        gutter = round((max(b.bbox[2] for b in left) + x) / 2, 2)
        score = min(left_h, right_h)
        if best is None or score > best[0]:
            best = (score, gutter)
    return [best[1]] if best else []


def column_of(bbox: BBox, gutters: list[float]) -> int | None:
    """Column index for a block, or None if it spans a gutter (full-width block)."""
    for gutter in gutters:
        if bbox[0] < gutter < bbox[2]:
            return None
    return sum(1 for g in gutters if bbox[0] >= g)


def reading_order(blocks: list[LayoutBlock], gutters: list[float]) -> list[int]:
    """Order blocks top-to-bottom; within multi-column bands, column by column.

    Full-width blocks (spanning a gutter) split the page into horizontal bands. Inside
    each band, columns are read left to right, each top to bottom.
    """
    by_y = sorted(blocks, key=lambda b: (round(b.bbox[1], 1), b.bbox[0]))
    if not gutters:
        return [b.key for b in by_y]

    ordered: list[int] = []
    band: list[LayoutBlock] = []

    def flush() -> None:
        band.sort(key=lambda b: (column_of(b.bbox, gutters) or 0, b.bbox[1], b.bbox[0]))
        ordered.extend(b.key for b in band)
        band.clear()

    for block in by_y:
        if column_of(block.bbox, gutters) is None:
            flush()
            ordered.append(block.key)
        else:
            band.append(block)
    flush()
    return ordered


def band_of(bbox: BBox, page_height: float, header_ratio: float, footer_ratio: float) -> str:
    """'header' / 'footer' if the block lies entirely in the top/bottom band, else 'body'."""
    if bbox[3] <= page_height * header_ratio:
        return "header"
    if bbox[1] >= page_height * (1 - footer_ratio):
        return "footer"
    return "body"
