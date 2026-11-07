"""SYNTHETIC corrected-only PDFs (no separate originals). All people and text invented.

Student text uses Times ("tiro"); faculty text uses red Helvetica, as observed in the
macOS-Preview FreeText corrections of the first real batch.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

RED = (1.0, 0.149, 0.0)  # #ff2600, the colour Preview used
STUDENT_LINES = [
    ("RIYA SYNTHETIC", 18, True),
    ("riya.synthetic@example.com | +91 90000 44444", 10, False),
    ("EDUCATION", 12.5, True),
    ("B.Tech in Computer Engineering, Example Institute of Technology, 2022 - 2026", 10, False),
    ("SKILLS", 12.5, True),
    ("Python, Java, SQL, Git, Docker", 10, False),
    ("PROJECTS", 12.5, True),
    ("Library Tracker - Flask application that tracks book loans for 500 users.", 10, False),
    ("EXPERIENCE", 12.5, True),
    ("Intern, Example Analytics, May 2025 - July 2025: built REST endpoints.", 10, False),
]
STUDENT_SENTENCE = "Flask application that tracks book loans for 500 users"


def _resume(doc: pymupdf.Document, heading_color=(0, 0, 0)) -> dict[str, pymupdf.Rect]:
    page = doc.new_page()
    y, where = 60.0, {}
    for text, size, bold in STUDENT_LINES:
        y += 6 if bold and size < 18 else 0
        color = heading_color if bold and size < 18 else (0, 0, 0)
        page.insert_text((50, y), text, fontsize=size, fontname="tibo" if bold else "tiro",
                         color=color)  # fmt: skip
        where[text] = page.search_for(text)[0]
        y += size + 6
    return where


def _freetext(page: pymupdf.Page, rect: pymupdf.Rect, text: str) -> None:
    annot = page.add_freetext_annot(rect, text, fontsize=7, fontname="helv", text_color=RED)
    annot.set_info(content=text, title="Faculty (synthetic)")
    annot.update()


def build_corrected_only(root: Path) -> Path:
    """Write the synthetic intake (flat folder of PDFs) and return it."""
    root.mkdir(parents=True, exist_ok=True)

    # C01: Preview-style FreeText annotations, two of them on the same line
    doc = pymupdf.open()
    w = _resume(doc)
    page = doc[0]
    y = w["SKILLS"].y0
    _freetext(page, pymupdf.Rect(150, y, 290, y + 12), "title as technical skills")
    _freetext(page, pymupdf.Rect(330, y, 560, y + 12), "add 12th and 10th")
    e = w["EDUCATION"]
    _freetext(page, pymupdf.Rect(e.x1 + 20, e.y0, e.x1 + 300, e.y1 + 4),
              "add career objective - a concise 2-3 line statement")  # fmt: skip
    doc.save(root / "C01_corrected.pdf")
    doc.close()

    # C02: student's own red headings, no corrections at all
    doc = pymupdf.open()
    _resume(doc, heading_color=(0.85, 0.0, 0.0))
    doc.save(root / "C02_corrected.pdf")
    doc.close()

    # C03: flattened red instructions in the page content (not annotations):
    #      two separate corrections on one line + one next to a section heading
    doc = pymupdf.open()
    w = _resume(doc)
    page = doc[0]
    sk = w["Python, Java, SQL, Git, Docker"]
    page.insert_text((sk.x1 + 15, sk.y1 - 2), "add Excel", fontsize=7, fontname="helv", color=RED)
    page.insert_text((420, sk.y1 - 2), "pls ad sumary of tools", fontsize=7, fontname="helv",
                     color=RED)  # fmt: skip
    ex = w["EDUCATION"]
    page.insert_text((ex.x1 + 20, ex.y1 - 2), "add 12th and 10th marks", fontsize=7,
                     fontname="helv", color=RED)  # fmt: skip
    doc.save(root / "C03_corrected.pdf")
    doc.close()

    # C04: red text written ON TOP of the student's text (overlay): cannot be separated
    doc = pymupdf.open()
    w = _resume(doc)
    page = doc[0]
    pr = w["Library Tracker - Flask application that tracks book loans for 500 users."]
    page.insert_text((pr.x0 + 40, pr.y1 - 3), "needs clearer individual contribution",
                     fontsize=9, fontname="helv", color=RED)  # fmt: skip
    doc.save(root / "C04_corrected.pdf")
    doc.close()

    # C05: scanned / flattened image only (handwritten corrections)
    src = pymupdf.open()
    _resume(src)
    pix = src[0].get_pixmap(dpi=60)
    src.close()
    doc = pymupdf.open()
    doc.new_page().insert_image(doc[0].rect, pixmap=pix)
    doc.save(root / "C05_corrected.pdf")
    doc.close()

    # C06: a red word that is ambiguous (not a heading, no instruction wording)
    doc = pymupdf.open()
    w = _resume(doc)
    page = doc[0]
    x = w["Intern, Example Analytics, May 2025 - July 2025: built REST endpoints."]
    page.insert_text((x.x1 + 10, x.y1 - 2), "Remote", fontsize=10, fontname="tiro", color=RED)
    doc.save(root / "C06_corrected.pdf")
    doc.close()
    return root
