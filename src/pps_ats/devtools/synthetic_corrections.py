"""SYNTHETIC teacher-corrected resumes for testing ``import-corrections``.

All students, teachers, comments and marks here are invented. They exercise each
correction channel the importer supports; they say nothing about how real faculty
annotate, and the importer has not been validated on real faculty files.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pymupdf

from pps_ats.devtools import synthetic
from pps_ats.devtools import synthetic_analysis as sa

TEACHER = "Prof. Synthetic (F01)"


def _note(annot: pymupdf.Annot, text: str, author: str = TEACHER) -> None:
    annot.set_info(content=text, title=author)
    annot.update()


def _quads(page: pymupdf.Page, text: str, nth: int = 0) -> list[pymupdf.Quad]:
    hits = page.search_for(text, quads=True)
    if len(hits) <= nth:
        raise ValueError(f"{text!r} not on page")
    return [hits[nth]]


def _red_heading_resume(path: Path) -> Path:
    """A student resume whose own section headings are red (must not count as corrections)."""
    doc = pymupdf.open()
    page = doc.new_page()
    y = 60.0
    for text, kind in synthetic.RESUME_LINES:
        if kind == "name":
            page.insert_text((50, y), text, fontsize=18, fontname="hebo")
            y += 26
        elif kind == "heading":
            y += 6
            page.insert_text((50, y), text, fontsize=12.5, fontname="hebo", color=(0.85, 0, 0))
            y += 17
        else:
            page.insert_text((50, y), text, fontsize=10, fontname="helv")
            y += 14
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    return path


def build_intake(root: Path) -> Path:
    """Create ``root/originals``, ``root/corrected`` and ``root/marks.csv``."""
    orig, corr = root / "originals", root / "corrected"
    corr.mkdir(parents=True, exist_ok=True)
    sa.spelling_resume_pdf(orig / "S001.pdf")
    sa.template_resume_pdf(orig / "S002.pdf")
    sa.template_resume_docx(orig / "S003.docx")
    _red_heading_resume(orig / "S004.pdf")
    synthetic.resume_pdf(orig / "S005.pdf")  # graded, but no corrected copy supplied
    synthetic.resume_pdf(orig / "S006.pdf")

    # S001: text markup with comments, a reply, a mark mention, an uncommented strike-out
    doc = pymupdf.open(orig / "S001.pdf")
    p = doc[0]
    _note(p.add_highlight_annot(_quads(p, "Devloped")), "Spelling: should be Developed")
    first = p.add_highlight_annot(_quads(p, "dashbord"))
    _note(first, "spelling mistake")
    reply = p.add_text_annot(first.rect.tl, "should be dashboard")
    reply.set_info(content="should be dashboard", title=TEACHER)
    reply.set_irt_xref(first.xref)
    reply.update()
    _note(p.add_underline_annot(_quads(p, "Pyhton")), "typo")
    _note(
        p.add_squiggly_annot(_quads(p, "with charts written in")),
        "grammar and font size issue here",
    )
    p.add_strikeout_annot(_quads(p, "budget planning"))
    _note(p.add_text_annot((520, 40), ""), "Overall decent. Total 24/30, content 7/10")
    doc.save(corr / "S001_corrected.pdf")
    doc.close()

    # S002: placeholder highlight, FreeText correction, red text typed into the page, ink circle
    doc = pymupdf.open(orig / "S002.pdf")
    p = doc[0]
    _note(
        p.add_highlight_annot(_quads(p, "City, State")),
        "Placeholder left from the template - must fix",
    )
    target = p.search_for("Smart Attendance System")[0]
    free = p.add_freetext_annot(
        pymupdf.Rect(target.x1 + 10, target.y0 - 2, target.x1 + 200, target.y1 + 10),
        "Add a project description",
        fontsize=8,
        text_color=(1, 0, 0),
    )
    _note(free, "Add a project description")
    line = p.search_for("[Write a brief career objective here]")[0]
    p.insert_text((line.x1 + 8, line.y1 - 2), "Write a specific objective", fontsize=8,
                  color=(1, 0, 0))  # fmt: skip
    url = p.search_for("linkedin.com/in/yourname")[0]
    ink = p.add_ink_annot(
        [[(url.x0 - 4, url.y1 + 2), (url.x1 + 4, url.y1 + 2), (url.x1 + 4, url.y0 - 2)]]
    )
    ink.set_colors(stroke=(1, 0, 0))
    ink.set_info(title=TEACHER)
    ink.update()
    doc.save(corr / "S002-corrected.pdf")
    doc.close()

    # S003 (DOCX original): teacher printed to PDF and added a sticky note
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((50, 60), "Your Name", fontsize=20)
    page.insert_text((50, 90), "Lorem ipsum dolor sit amet, consectetur adipiscing elit.")
    _note(
        page.add_highlight_annot(_quads(page, "Lorem ipsum")),
        "remove the lorem ipsum template text",
    )
    pdf.save(corr / "S003_corrected.pdf")
    pdf.close()

    # S004: student's own red headings (ignored) + one sticky note
    doc = pymupdf.open(orig / "S004.pdf")
    p = doc[0]
    _note(p.add_text_annot((300, 100), ""), "Career objective needs to name the target role")
    doc.save(corr / "S004_reviewed.pdf")
    doc.close()

    # S006: handwritten corrections, scanned (no text layer)
    src = pymupdf.open(orig / "S006.pdf")
    pix = src[0].get_pixmap(dpi=60)
    src.close()
    scan = pymupdf.open()
    scan.new_page().insert_image(scan[0].rect, pixmap=pix)
    scan.save(corr / "S006_corrected.pdf")
    scan.close()

    # A corrected file whose original was not supplied
    synthetic.resume_pdf(corr / "S999_corrected.pdf")

    with (root / "marks.csv").open("w", newline="", encoding="utf-8") as handle:
        w = csv.writer(handle)
        w.writerow(["document_id", "content_relevance", "structure_formatting",
                    "language_professional_tone", "total", "grader_id", "source"])  # fmt: skip
        w.writerow(["S001", "7", "8", "6", "21", "F01", "faculty"])
        w.writerow(["S002", "4", "7", "8", "", "", "faculty"])  # no grader: not attributable
        w.writerow(["S003", "5", "7", "7", "25", "F02", "faculty"])  # total != sum
        w.writerow(["S004", "9", "9", "9", "27", "AI-SHEET", "ai"])  # old AI grade
        w.writerow(["S005", "8", "9", "9", "26", "F02", ""])  # source blank = faculty
        w.writerow(["S777", "8", "8", "8", "24", "F01", "faculty"])  # unknown document
    return root
