"""Synthetic test documents. All names/contacts are fictional (example.com, fake numbers).

Used by the test-suite and ``scripts/generate_samples.py``; nothing here is real student data.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pymupdf
from docx import Document
from docx.oxml import parse_xml
from docx.shared import Pt

NAME = "ASHA EXAMPLE"
CONTACT = "asha.example@example.com | +91 98765 43210 | linkedin.com/in/asha-example"

# (text, kind) where kind in {"name", "heading", "body"}
RESUME_LINES: list[tuple[str, str]] = [
    (NAME, "name"),
    (CONTACT, "body"),
    ("SUMMARY", "heading"),
    ("Final-year computer engineering student focused on backend development", "body"),
    ("and reliable data pipelines, seeking a software engineering internship.", "body"),
    ("EDUCATION", "heading"),
    ("B.Tech in Computer Engineering, Example Institute of Technology, 2022 - 2026", "body"),
    ("CGPA 8.4 / 10; coursework in databases, operating systems and networks.", "body"),
    ("Higher Secondary Certificate, Example Junior College, 2022, 88 percent.", "body"),
    ("SKILLS", "heading"),
    ("Languages: Python, Java, SQL, JavaScript", "body"),
    ("Tools: Git, Docker, PostgreSQL, Linux, Flask, REST APIs", "body"),
    ("PROJECTS", "heading"),
    ("Campus Library Tracker - Flask web application that tracks book loans for", "body"),
    ("five hundred users with role based access and automated overdue reminders.", "body"),
    ("Weather Data Pipeline - Python ETL job that loads hourly sensor readings", "body"),
    ("into PostgreSQL and publishes a daily summary dashboard.", "body"),
    ("EXPERIENCE", "heading"),
    ("Software Intern, Example Analytics Pvt Ltd, May 2025 - July 2025", "body"),
    ("Built REST endpoints and automated weekly reporting, reducing manual effort.", "body"),
]

ESSAY = (
    "Climate patterns across coastal regions have shifted noticeably during the last few "
    "decades. Researchers observe warmer average temperatures, longer dry spells and more "
    "intense rainfall when storms do arrive. These changes affect agriculture, fisheries and "
    "the daily routines of millions of people who live near the sea. Farmers adapt by "
    "changing sowing dates and choosing crop varieties that tolerate heat, while fishing "
    "communities track migrating stocks further offshore. Local governments invest in "
    "drainage, early warning systems and mangrove restoration to reduce flood damage. "
    "Community groups organise awareness campaigns so that households understand the risks "
    "and prepare emergency kits before the monsoon season begins. Long term planning "
    "requires reliable data, cooperation between neighbouring districts and steady funding. "
    "Without these foundations, short term relief measures repeatedly absorb resources that "
    "could otherwise build lasting resilience for the next generation of coastal residents."
)


# --------------------------------------------------------------------------- PDF


def _write_lines(page: pymupdf.Page, lines: list[tuple[str, str]], x: float, y: float) -> float:
    for text, kind in lines:
        if kind == "name":
            page.insert_text((x, y), text, fontsize=18, fontname="hebo")
            y += 26
        elif kind == "heading":
            y += 6
            page.insert_text((x, y), text, fontsize=12.5, fontname="hebo")
            y += 17
        else:
            page.insert_text((x, y), text, fontsize=10, fontname="helv")
            y += 14
    return y


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def _png_bytes(width: int = 120, height: int = 120, shade: int = 180) -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    pix.clear_with(shade)
    return pix.tobytes("png")


def _save(doc: pymupdf.Document, path: Path, **kwargs: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path, **kwargs)
    doc.close()
    return path


def resume_pdf(path: Path, *, extra_lines: list[tuple[str, str]] | None = None) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()  # A4
    _write_lines(page, RESUME_LINES + (extra_lines or []), 50, 60)
    return _save(doc, path)


def two_column_resume_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 50), NAME, fontsize=18, fontname="hebo")
    page.insert_text((50, 70), CONTACT, fontsize=9, fontname="helv")
    left = [
        ("SKILLS", "heading"),
        ("Python, Java, SQL", "body"),
        ("Git, Docker, Linux", "body"),
        ("Flask, REST APIs", "body"),
        ("EDUCATION", "heading"),
        ("B.Tech Computer Engg.", "body"),
        ("Example Institute", "body"),
        ("2022 - 2026, CGPA 8.4", "body"),
    ]
    right = [
        ("SUMMARY", "heading"),
        ("Engineering student building backend", "body"),
        ("services and data pipelines.", "body"),
        ("PROJECTS", "heading"),
        ("Campus Library Tracker: Flask app", "body"),
        ("tracking loans for 500 users.", "body"),
        ("Weather Data Pipeline: hourly ETL", "body"),
        ("into PostgreSQL with a dashboard.", "body"),
        ("EXPERIENCE", "heading"),
        ("Software Intern, Example Analytics", "body"),
        ("Built REST endpoints and reports.", "body"),
    ]
    _write_lines(page, left, 50, 120)
    _write_lines(page, right, 330, 120)
    return _save(doc, path)


def image_heavy_resume_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    # Large decorative background image covering most of the page, text on top.
    page.insert_image(
        pymupdf.Rect(0, 0, page.rect.width, page.rect.height * 0.75),
        stream=_png_bytes(200, 200, 235),
    )
    _write_lines(page, RESUME_LINES, 50, 60)
    return _save(doc, path)


def image_only_pdf(path: Path) -> Path:
    """A 'scanned' resume: the page is a raster image, no text layer."""
    source = pymupdf.open()
    _write_lines(source.new_page(), RESUME_LINES, 50, 60)
    pix = source[0].get_pixmap(dpi=72)
    source.close()
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, pixmap=pix)
    return _save(doc, path)


def blank_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    doc.new_page()
    return _save(doc, path)


def encrypted_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    _write_lines(doc.new_page(), RESUME_LINES, 50, 60)
    return _save(
        doc, path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="student", owner_pw="owner"
    )


def grade_card_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    lines = [
        ("EXAMPLE UNIVERSITY", "name"),
        ("GRADE CARD - Statement of Marks", "heading"),
        ("Semester V Examination, November 2025", "body"),
        ("Name of Student: Asha Example        Seat No: 4512398", "body"),
        ("Subject Code   Course Title                 Credits   Grade   Grade Points", "body"),
        ("CE501          Database Systems             4         A       9", "body"),
        ("CE502          Operating Systems            4         B+      8", "body"),
        ("CE503          Computer Networks            3         A       9", "body"),
        ("CE504          Software Engineering         3         O       10", "body"),
        ("CE505          Mini Project Lab             2         A+      9", "body"),
        ("Credits Earned: 16      SGPA: 8.81      Result: PASS", "body"),
        ("Controller of Examinations", "body"),
    ]
    _write_lines(page, lines, 50, 60)
    return _save(doc, path)


def essay_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    lines = [("Coastal Climate Adaptation", "heading")] + [
        (line, "body") for line in _wrap(ESSAY, 90)
    ]
    _write_lines(page, lines, 50, 60)
    return _save(doc, path)


def corrupted_pdf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.7\n" + bytes(range(256)) * 8 + b"\n%%EOF")
    return path


# --------------------------------------------------------------------------- DOCX

_VML_TEXTBOX = (
    '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:v="urn:schemas-microsoft-com:vml"><w:pict><v:shape style="width:220pt;height:40pt">'
    "<v:textbox><w:txbxContent><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:txbxContent>"
    "</v:textbox></v:shape></w:pict></w:r>"
)


def resume_docx(
    path: Path,
    *,
    empty_section: bool = True,
    placeholder_section: bool = True,
    textbox_text: str | None = "Portfolio: example.com/asha",
) -> Path:
    doc = Document()
    name = doc.add_paragraph()
    run = name.add_run(NAME.title())
    run.bold = True
    run.font.size = Pt(20)
    contact = doc.add_paragraph(CONTACT)
    if textbox_text:  # templates often place contact/portfolio details in a text box
        contact._p.append(parse_xml(_VML_TEXTBOX.format(text=textbox_text)))

    def heading(text: str) -> None:
        doc.add_heading(text, level=1)

    heading("Summary")
    doc.add_paragraph(
        "Final-year computer engineering student focused on backend development and reliable "
        "data pipelines, seeking a software engineering internship."
    )
    heading("Education")
    doc.add_paragraph(
        "B.Tech in Computer Engineering, Example Institute of Technology, 2022 - 2026, CGPA 8.4."
    )
    heading("Technical Skills")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Languages"
    table.cell(0, 1).text = "Python, Java, SQL, JavaScript"
    table.cell(1, 0).text = "Tools"
    table.cell(1, 1).text = "Git, Docker, PostgreSQL, Linux"
    heading("Projects")
    doc.add_paragraph(
        "Campus Library Tracker - Flask web application tracking book loans for five hundred "
        "users with automated reminders.",
        style="List Bullet",
    )
    doc.add_paragraph(
        "Weather Data Pipeline - Python ETL job loading hourly readings into PostgreSQL.",
        style="List Bullet",
    )
    heading("Experience")
    doc.add_paragraph(
        "Software Intern, Example Analytics Pvt Ltd, May 2025 - July 2025. Built REST "
        "endpoints and automated weekly reporting."
    )
    if empty_section:
        heading("Certifications")
    if placeholder_section:
        heading("Achievements")
        doc.add_paragraph("[Add your achievements here]")

    section = doc.sections[0]
    section.header.paragraphs[0].text = "Curriculum Vitae"
    section.footer.paragraphs[0].text = "Page 1"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


def empty_docx(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Document().save(path)
    return path


def image_only_docx(path: Path) -> Path:
    doc = Document()
    doc.add_picture(BytesIO(_png_bytes(400, 560, 200)), width=Pt(420))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


def corrupted_docx(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PK\x03\x04" + b"\x00garbage-not-a-zip" * 64)
    return path


def legacy_doc(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 1024)
    return path


def encrypted_docx(path: Path) -> Path:
    """Mimics the OLE container Word uses for password-protected .docx files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512 + "EncryptedPackage".encode("utf-16-le")
    )
    return path


ALL_SAMPLES = {
    "resume.pdf": resume_pdf,
    "resume_two_column.pdf": two_column_resume_pdf,
    "resume_image_heavy.pdf": image_heavy_resume_pdf,
    "resume_scanned_image_only.pdf": image_only_pdf,
    "blank.pdf": blank_pdf,
    "encrypted.pdf": encrypted_pdf,
    "grade_card.pdf": grade_card_pdf,
    "essay.pdf": essay_pdf,
    "corrupted.pdf": corrupted_pdf,
    "resume.docx": resume_docx,
    "empty.docx": empty_docx,
    "image_only.docx": image_only_docx,
    "corrupted.docx": corrupted_docx,
    "legacy.doc": legacy_doc,
    "password_protected.docx": encrypted_docx,
}


def generate_all(directory: Path) -> list[Path]:
    return [builder(directory / name) for name, builder in ALL_SAMPLES.items()]
