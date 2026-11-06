"""Synthetic documents for Milestone 2 analysis tests.

All names, e-mails, numbers and organisations are fictional (example.com, 90000 numbers).
Each builder reproduces one problem observed during faculty review, or a legitimate
pattern that must NOT be flagged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf
from docx import Document
from docx.shared import Pt

from pps_ats.devtools.synthetic import CONTACT, NAME, RESUME_LINES, _save

Item = tuple[str, str] | tuple[str, str, dict[str, Any]]

_KINDS = {
    "name": ("hebo", 18.0, 26.0),
    "heading": ("hebo", 12.5, 17.0),
    "entry": ("hebo", 10.0, 14.0),  # bold entry title (project / job)
    "body": ("helv", 10.0, 14.0),
}


def write_pdf(path: Path, items: list[Item], x: float = 50, y: float = 60) -> Path:
    """Write lines top-down. Options: font, size, gap (extra space before), x."""
    doc = pymupdf.open()
    page = doc.new_page()  # A4
    for item in items:
        text, kind = item[0], item[1]
        opts: dict[str, Any] = item[2] if len(item) > 2 else {}  # type: ignore[misc]
        font, size, advance = _KINDS[kind]
        font = opts.get("font", font)
        size = opts.get("size", size)
        y += opts.get("gap", 6.0 if kind == "heading" else 0.0)
        if y > page.rect.height - 50:
            page = doc.new_page()
            y = 60
        page.insert_text((opts.get("x", x), y), text, fontsize=size, fontname=font)
        y += advance
    return _save(doc, path)


# --------------------------------------------------------------------------- problems

TEMPLATE_RESUME: list[Item] = [
    ("MEERA TESTCASE", "name"),
    ("meera.testcase@example.com | +91 90000 11111 | City, State", "body"),
    ("linkedin.com/in/yourname | github.com/meera-testcase", "body"),
    ("CAREER OBJECTIVE", "heading"),
    ("[Write a brief career objective here]", "body"),
    ("EDUCATION", "heading"),
    ("B.Tech in Computer Engineering, Example Institute of Technology, 2022 - 2026", "body"),
    ("CGPA 8.1 / 10", "body"),
    ("SKILLS", "heading"),
    ("Languages: Python, Java, SQL", "body"),
    ("Frameworks: React, Django, Flask", "body"),
    ("PROJECTS", "heading"),
    ("Smart Attendance System", "entry"),
    ("Description:", "body"),
    ("Library Portal", "entry"),
    ("Developed a Django web portal for issuing and returning books, using PostgreSQL", "body"),
    ("and REST APIs; used by three hundred students during the semester.", "body"),
    ("Project Title: Inventory Tracker - Built with Flask and SQLite to track lab", "body"),
    ("equipment usage across four departments with weekly email reports.", "body"),
    ("Description of the system architecture is included in the project report.", "body"),
]


def template_resume_pdf(path: Path) -> Path:
    """Unfinished template: City/State, generic LinkedIn, placeholder objective, a project
    with an empty description, plus legitimate 'Project Title:' / 'Description of' text."""
    return write_pdf(path, TEMPLATE_RESUME)


def missing_sections_pdf(path: Path) -> Path:
    """Recognisable resume without Education and Projects sections."""
    return write_pdf(
        path,
        [
            (NAME, "name"),
            (CONTACT, "body"),
            ("SUMMARY", "heading"),
            (
                "Computer engineering student seeking a backend development internship where I can",
                "body",
            ),
            ("apply Python and database skills to real production systems.", "body"),
            ("SKILLS", "heading"),
            ("Python, Java, SQL, Git, Docker, Linux, PostgreSQL", "body"),
            ("EXPERIENCE", "heading"),
            ("Software Intern, Example Analytics Pvt Ltd, May 2025 - July 2025", "body"),
            (
                "Implemented REST endpoints and automated weekly reporting for the sales team.",
                "body",
            ),
            ("CERTIFICATIONS", "heading"),
            ("Python for Data Science - NPTEL, 2024", "body"),
        ],
    )


def mixed_fonts_pdf(path: Path) -> Path:
    """Body text of the Projects and Experience sections set in Times instead of Helvetica."""
    items: list[Item] = []
    in_mixed = False
    for text, kind in RESUME_LINES:
        if kind == "heading":
            in_mixed = text in ("PROJECTS", "EXPERIENCE")
        if kind == "body" and in_mixed:
            items.append((text, kind, {"font": "tiro"}))
        else:
            items.append((text, kind))
    return write_pdf(path, items)


def excessive_spacing_pdf(path: Path) -> Path:
    """A large empty area before the Experience section."""
    items: list[Item] = []
    for text, kind in RESUME_LINES:
        if text == "EXPERIENCE":
            items.append((text, kind, {"gap": 230.0}))
        else:
            items.append((text, kind))
    return write_pdf(path, items)


def repeated_bullets_pdf(path: Path) -> Path:
    bullet = "• Built REST endpoints for the attendance module using Flask and PostgreSQL."
    return write_pdf(
        path,
        [
            *RESUME_LINES,
            (
                "Campus Event Manager - Django application for registering students to events",
                "body",
            ),
            (bullet, "body"),
            ("• Designed the database schema and wrote unit tests with Pytest.", "body"),
            (bullet, "body"),
        ],
    )


def spelling_resume_pdf(path: Path) -> Path:
    """Correct technology names, URLs and e-mails plus genuine misspellings."""
    return write_pdf(
        path,
        [
            ("MEERA TESTCASE", "name"),
            ("meera.testcase@example.com | +91 90000 22222 | github.com/meera-testcase", "body"),
            ("SUMMARY", "heading"),
            ("Engineering student interested in backend development, cloud platforms and", "body"),
            ("data engineering, seeking an internship in software development.", "body"),
            ("EDUCATION", "heading"),
            (
                "B.Tech in Information Technology, Example Institute of Technology, 2022 - 2026,",
                "body",
            ),
            ("CGPA 8.3 / 10", "body"),
            ("SKILLS", "heading"),
            (
                "Python, JavaScript, TypeScript, React, Node.js, Django, FastAPI, PostgreSQL,",
                "body",
            ),
            (
                "MongoDB, GitHub, Docker, Kubernetes, TensorFlow, PyTorch, NumPy, Pandas, OpenCV,",
                "body",
            ),
            ("scikit-learn, Tailwind, Firebase, Postman, Jupyter, MySQL, AWS, Linux", "body"),
            ("PROJECTS", "heading"),
            (
                "Expense Tracker - Devloped a dashbord using React and PostgreSQL for monthly",
                "body",
            ),
            ("budget planning, with charts written in Pyhton for analysis reports.", "body"),
        ],
    )


def template_resume_docx(path: Path) -> Path:
    """DOCX template leftovers: 'Your Name', 'City, State', a standalone 'Project Title'
    line and lorem ipsum, plus a legitimate label/value table that must not be flagged."""
    doc = Document()
    run = doc.add_paragraph().add_run("Your Name")
    run.bold = True
    run.font.size = Pt(20)
    doc.add_paragraph("youremail@email.com | +91 90000 33333 | City, State")
    doc.add_heading("Education", level=1)
    doc.add_paragraph(
        "B.Tech in Mechanical Engineering, Example Institute of Technology, 2022 - 2026, CGPA 7.9"
    )
    doc.add_heading("Skills", level=1)
    doc.add_paragraph("AutoCAD, SolidWorks, MATLAB, Python, ANSYS")
    doc.add_heading("Projects", level=1)
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Project Title"
    table.cell(0, 1).text = "Solar Powered Water Pump"
    table.cell(1, 0).text = "Technologies"
    table.cell(1, 1).text = "MATLAB, Simulink, Arduino"
    doc.add_paragraph("Project Title")
    doc.add_paragraph("Lorem ipsum dolor sit amet, consectetur adipiscing elit.")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


ANALYSIS_SAMPLES = {
    "template_resume.pdf": template_resume_pdf,
    "missing_sections.pdf": missing_sections_pdf,
    "mixed_fonts.pdf": mixed_fonts_pdf,
    "excessive_spacing.pdf": excessive_spacing_pdf,
    "repeated_bullets.pdf": repeated_bullets_pdf,
    "spelling_resume.pdf": spelling_resume_pdf,
    "template_resume.docx": template_resume_docx,
}
