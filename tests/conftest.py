"""Shared fixtures. All documents are synthetic and generated into a temp dir per session."""

from __future__ import annotations

from pathlib import Path

import pytest

from pps_ats.config import Settings, load_settings
from pps_ats.devtools import synthetic


@pytest.fixture(scope="session")
def settings() -> Settings:
    return load_settings()


@pytest.fixture(scope="session")
def samples(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    directory = tmp_path_factory.mktemp("samples")
    paths = {name: builder(directory / name) for name, builder in synthetic.ALL_SAMPLES.items()}

    extra = {
        "notes.txt": b"Just some plain text notes, not a resume.",
        "photo.pdf": synthetic._png_bytes(),  # PNG bytes with a .pdf extension
        "zero_bytes.pdf": b"",
    }
    for name, data in extra.items():
        (directory / name).write_bytes(data)
        paths[name] = directory / name

    # Real DOCX content behind a misleading .pdf extension.
    paths["docx_named_pdf.pdf"] = synthetic.resume_docx(directory / "renamed.docx").rename(
        directory / "docx_named_pdf.pdf"
    )
    # Filenames must not influence classification.
    paths["misleading_resume_name.pdf"] = synthetic.grade_card_pdf(
        directory / "Final_Resume_ATS.pdf"
    )
    paths["misleading_gradecard_name.pdf"] = synthetic.resume_pdf(
        directory / "grade_card_sem5_marksheet.pdf"
    )
    return paths
