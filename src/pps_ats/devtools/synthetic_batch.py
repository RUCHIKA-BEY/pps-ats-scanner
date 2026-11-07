"""A synthetic submission folder for batch tests and demos (all data fictional).

Covers: clean and flawed resumes (PDF/DOCX), a non-resume, an image-only scan, corrupted
files, the same file name in two class folders, an identical resubmission, an identity
mismatch (via the manifest) and an Office lock file that must be listed as ignored.
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

from pps_ats.devtools import synthetic
from pps_ats.devtools import synthetic_analysis as sa

MANIFEST_FIELDS = ["file_name", "student_name", "student_id", "email"]


def build_batch(root: Path, copies: int = 1) -> tuple[Path, Path]:
    """Create ``root/input`` (+ ``copies`` extra clean resumes) and ``root/manifest.csv``."""
    src = root / "input"
    a, b = src / "div_A", src / "div_B"
    rows: list[dict[str, str]] = []

    def add(path: Path, name: str | None, sid: str | None, email: str = "") -> None:
        rows.append(
            {
                "file_name": path.relative_to(src).as_posix(),
                "student_name": name or "",
                "student_id": sid or "",
                "email": email,
            }
        )

    add(synthetic.resume_pdf(a / "Asha_Example_Resume.pdf"), "Asha Example", "PPS001")
    add(sa.template_resume_pdf(a / "Meera_Testcase_Resume.pdf"), "Meera Testcase", "PPS002")
    add(sa.spelling_resume_pdf(a / "resume.pdf"), "Meera Testcase", "PPS003")
    add(sa.template_resume_docx(a / "Rohan_Sample_Resume.docx"), "Rohan Sample", "PPS004")
    add(synthetic.resume_docx(b / "Kiran_Demo_Resume.docx"), "Asha Example", "PPS005")
    add(synthetic.grade_card_pdf(b / "Nikhil_Demo_Resume.pdf"), "Nikhil Demo", "PPS006")
    add(synthetic.image_only_pdf(b / "Scan_Resume.pdf"), "Tara Demo", "PPS007")
    add(synthetic.corrupted_pdf(b / "Broken_Resume.pdf"), "Vikram Demo", "PPS008")
    # Same file name as div_A/resume.pdf, different student and content.
    add(sa.mixed_fonts_pdf(b / "resume.pdf"), "Asha Example", "PPS009")
    # Identity mismatch: the manifest says this file belongs to someone else.
    add(synthetic.resume_pdf(b / "Priya_Desai_Resume.pdf"), "Priya Desai", "PPS010")
    # Identical resubmission of a file already in div_A.
    dup = b / "Asha_Example_Resume_copy.pdf"
    shutil.copyfile(a / "Asha_Example_Resume.pdf", dup)
    add(dup, "Asha Example", "PPS011")
    for i in range(copies - 1):
        path = sa.excessive_spacing_pdf(src / "extra" / f"Student_{i:04d}_Resume.pdf")
        add(path, f"Student {i:04d}", f"PPX{i:04d}")
    (b / "~$Kiran_Demo_Resume.docx").write_bytes(b"lock")  # Word lock file: ignored, listed
    manifest = root / "manifest.csv"
    with manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return src, manifest
