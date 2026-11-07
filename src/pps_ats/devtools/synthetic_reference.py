"""A SYNTHETIC reference dataset for testing the evaluation workflow.

The "faculty" marks and annotations below are invented to exercise every code path
(matches, misses, false detections, validation errors, AI-sourced marks). They say
nothing about real accuracy and must never be reported as such.
"""

from __future__ import annotations

import csv
from pathlib import Path

from pps_ats.devtools import synthetic
from pps_ats.devtools import synthetic_analysis as sa

DOC_FIELDS = [
    "document_id",
    "file",
    "split",
    "expected_validation",
    "marks_source",
    "faculty_content_relevance",
    "faculty_structure_formatting",
    "faculty_language_professional_tone",
    "faculty_total",
    "annotations_complete",
    "grader_id",
    "notes",
]
ANN_FIELDS = ["document_id", "label", "severity", "text", "page", "criterion", "source"]


def build_reference(root: Path, split_all: str | None = None) -> Path:
    """Write documents + documents.csv + annotations.csv under ``root``; returns ``root``."""
    files = root / "files"
    builders = {
        "R001": ("clean.pdf", synthetic.resume_pdf),
        "R002": ("template.pdf", sa.template_resume_pdf),
        "R003": ("spelling.pdf", sa.spelling_resume_pdf),
        "R004": ("template.docx", sa.template_resume_docx),
        "R005": ("grade_card.pdf", synthetic.grade_card_pdf),
        "R006": ("scan.pdf", synthetic.image_only_pdf),
        "R007": ("mixed_fonts.pdf", sa.mixed_fonts_pdf),
        "R008": ("clean_ai_marked.pdf", synthetic.resume_pdf),
    }
    for _, (name, build) in builders.items():
        build(files / name)
    split = {
        "R001": "evaluation", "R002": "evaluation", "R003": "evaluation", "R004": "calibration",
        "R005": "evaluation", "R006": "evaluation", "R007": "calibration", "R008": "evaluation",
    }  # fmt: skip
    marks = {  # (content, structure, language, source)
        "R001": (8, 9, 9, "faculty"),
        "R002": (4, 8, 9, "faculty"),
        "R003": (8, 9, 6, "faculty"),
        "R004": (4, 7, 8, "faculty"),
        "R006": (6, 6, 7, "faculty"),  # faculty graded the scan manually
        "R007": (8, 7, 9, "faculty"),
        "R008": (9, 9, 9, "ai"),  # AI-generated marks: must be excluded
    }
    expected = {
        "R001": "valid", "R002": "valid", "R003": "valid", "R004": "valid",
        "R005": "invalid", "R006": "needs_review", "R007": "valid", "R008": "valid",
    }  # fmt: skip
    root.mkdir(parents=True, exist_ok=True)
    with (root / "documents.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DOC_FIELDS)
        writer.writeheader()
        for doc_id, (name, _) in builders.items():
            c, s, lang, source = marks.get(doc_id, ("", "", "", ""))
            writer.writerow(
                {
                    "document_id": doc_id,
                    "file": f"files/{name}",
                    "split": split_all or split[doc_id],
                    "expected_validation": expected[doc_id],
                    "marks_source": source,
                    "faculty_content_relevance": c,
                    "faculty_structure_formatting": s,
                    "faculty_language_professional_tone": lang,
                    "faculty_total": (c + s + lang) if source else "",
                    "annotations_complete": "yes" if doc_id in ("R002", "R003") else "no",
                    "grader_id": "F-SYNTH",
                    "notes": "synthetic test data",
                }
            )
    annotations = [
        ("R002", "placeholder", "high", "City, State", 1, "content_relevance", "faculty"),
        ("R002", "placeholder", "high", "[Write a brief career objective here]", 1, "", "faculty"),
        ("R002", "placeholder", "medium", "linkedin.com/in/yourname", 1, "", "faculty"),
        ("R002", "project_description", "high", "Smart Attendance System", 1, "", "faculty"),
        ("R002", "career_objective", "high", "", 1, "content_relevance", "faculty"),
        ("R003", "spelling", "medium", "Devloped", 1, "language_professional_tone", "faculty"),
        ("R003", "spelling", "medium", "dashbord", 1, "language_professional_tone", "faculty"),
        ("R003", "spelling", "medium", "Pyhton", 1, "language_professional_tone", "faculty"),
        ("R003", "grammar", "high", "charts written in Pyhton for analysis", 1, "", "faculty"),
        ("R003", "weak_language", "low", "interested in backend development", 1, "", "ai"),
        ("R001", "weak_language", "low", "seeking a software engineering", 1, "", "faculty"),
    ]  # fmt: skip
    with (root / "annotations.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(ANN_FIELDS)
        writer.writerows(annotations)
    return root
