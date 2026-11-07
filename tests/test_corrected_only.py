"""Corrected-only intake: faculty comments are separated from the student's content.

Synthetic PDFs only (devtools/synthetic_corrected_only.py). Faculty text is red Helvetica
(as observed in macOS-Preview FreeText corrections); student text is Times.
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import pymupdf
import pytest

from pps_ats.cli import main
from pps_ats.config import load_settings
from pps_ats.devtools.synthetic_corrected_only import STUDENT_SENTENCE, build_corrected_only
from pps_ats.evaluation.corrected_only import build_clean_copy, import_corrected_only
from pps_ats.evaluation.dataset import freeze_split, load_dataset
from pps_ats.evaluation.importer import CANDIDATES, ImportError_, finalize_annotations
from pps_ats.evaluation.runner import evaluate
from pps_ats.pipeline import scan_file
from pps_ats.scoring.engine import score_file


def read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    root = tmp_path_factory.mktemp("co")
    intake = build_corrected_only(root / "intake")
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in intake.iterdir()}
    summary = import_corrected_only(intake, root / "dataset", load_settings())
    return intake, root / "dataset", summary, before


def doc(summary, doc_id):  # type: ignore[no-untyped-def]
    return next(d for d in summary.per_document if d["document_id"] == doc_id)


def rows(dataset: Path, doc_id: str) -> list[dict[str, str]]:
    return [r for r in read(dataset / CANDIDATES) if r["document_id"] == doc_id]


def text_of(path: Path) -> str:
    with pymupdf.open(path) as d:
        return " ".join(" ".join(p.get_text().split()) for p in d)


# --------------------------------------------------------------------------- detection


def test_freetext_annotations_are_separate_corrections(run):
    _, dataset, summary, _ = run
    r = rows(dataset, "C01")
    assert [x["teacher_comment"] for x in r] == [
        "title as technical skills",
        "add 12th and 10th",  # same line as the previous one: not merged
        "add career objective - a concise 2-3 line statement",
    ]
    assert {x["source_type"] for x in r} == {"pdf_annotation:FreeText"}
    assert all(x["bbox"] and x["page"] == "1" for x in r)
    assert r[0]["suggested_label"] == "section_title" and r[0]["review_status"] == "auto_mapped"
    assert r[0]["associated_section"] == "skills"
    assert r[2]["review_status"] == "needs_review"  # two labels: missing_section, weak_language
    assert doc(summary, "C01")["scanner_input_status"] == "clean"


def test_students_red_heading_is_kept_as_student_content(run):
    _, dataset, summary, _ = run
    assert rows(dataset, "C02") == []
    d = doc(summary, "C02")
    assert d["kept_student_red"] == 4 and d["scanner_input_status"] == "clean"
    assert "SKILLS" in text_of(dataset / "files" / "C02.pdf")


def test_multiple_red_corrections_on_one_line_stay_separate(run):
    _, dataset, _, _ = run
    texts = [x["teacher_comment"] for x in rows(dataset, "C03")]
    assert "add Excel" in texts and "pls ad sumary of tools" in texts


def test_red_correction_near_a_section_heading(run):
    _, dataset, _, _ = run
    near = next(x for x in rows(dataset, "C03") if x["teacher_comment"].startswith("add 12th"))
    assert near["associated_section"] == "education"
    assert near["faculty_likelihood"] == "likely_faculty"
    assert near["suggested_label"] == "education_details"
    assert near["review_status"] == "needs_review"  # flattened text: always confirmed


def test_image_only_page_needs_manual_transcription(run):
    _, dataset, summary, _ = run
    assert doc(summary, "C05")["scanner_input_status"] == "manual_transcription"
    (row,) = rows(dataset, "C05")
    assert row["source_type"] == "image_only_page" and row["teacher_comment"] == ""


def test_ambiguous_overlay_requires_review(run):
    _, dataset, summary, _ = run
    d = doc(summary, "C04")
    assert d["scanner_input_status"] == "needs_review" and d["ambiguous_red"] == 1
    (row,) = rows(dataset, "C04")
    assert row["faculty_likelihood"] == "ambiguous" and "on top of other text" in row["evidence"]
    assert doc(summary, "C06")["scanner_input_status"] == "needs_review"  # bare red word


# --------------------------------------------------------------------------- clean input


def test_faculty_text_is_excluded_from_scanner_input(run):
    intake, dataset, _, _ = run
    clean = text_of(dataset / "files" / "C01.pdf")
    for comment in ("title as technical skills", "add 12th and 10th", "add career objective"):
        assert comment not in clean
    # removed from the file, not hidden: no annotation objects remain at all
    with pymupdf.open(dataset / "files" / "C01.pdf") as d:
        assert sum(1 for p in d for _ in p.annots()) == 0
    # flattened instructions in C03 are deleted as glyphs (not covered by a box)
    c03 = text_of(dataset / "files" / "C03.pdf")
    assert "add Excel" not in c03 and "add 12th and 10th marks" not in c03
    assert "add Excel" in text_of(intake / "C03_corrected.pdf")


def test_same_document_retains_the_student_content(run):
    intake, dataset, summary, before = run
    for doc_id in ("C01", "C03"):
        assert STUDENT_SENTENCE in text_of(dataset / "files" / f"{doc_id}.pdf")
        assert doc(summary, doc_id)["student_text_preserved"] is True
    # the corrected originals are untouched (intake and the dataset's own copy)
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in intake.iterdir()}
    assert after == before
    assert (
        hashlib.sha256((dataset / "corrected" / "C01.pdf").read_bytes()).hexdigest()
        == before["C01_corrected.pdf"]
    )


def test_faculty_comments_are_not_scored_as_student_content(run, settings):
    intake, dataset, _, _ = run
    # Without separation the scanner reads the faculty's "sumary" as a student misspelling.
    raw_analysis, _ = score_file(intake / "C03_corrected.pdf", settings)
    clean_analysis, _ = score_file(dataset / "files" / "C03.pdf", settings)
    raw_spelling = {f.detected_text for f in raw_analysis.findings if f.rule_id == "LANG_SPELLING"}
    clean_spelling = {
        f.detected_text for f in clean_analysis.findings if f.rule_id == "LANG_SPELLING"
    }
    assert "sumary" in raw_spelling and "sumary" not in clean_spelling
    raw = scan_file(intake / "C01_corrected.pdf", settings).content.full_text
    clean = scan_file(dataset / "files" / "C01.pdf", settings).content.full_text
    assert "add 12th and 10th" in raw and "add 12th and 10th" not in clean


def test_clean_copy_never_overwrites_its_source(tmp_path, settings):
    intake = build_corrected_only(tmp_path / "in")
    src = intake / "C01_corrected.pdf"
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    result = build_clean_copy(src, tmp_path / "clean.pdf", settings)
    assert result.status == "clean" and result.removed_annotations == 3
    assert hashlib.sha256(src.read_bytes()).hexdigest() == digest


# --------------------------------------------------------------------------- marks + flow


def test_import_without_faculty_marks_keeps_scores_empty(run):
    _, dataset, summary, _ = run
    assert summary.marks_imported == 0
    for row in read(dataset / "documents.csv"):
        assert row["marks_source"] == "" and row["faculty_total"] == ""
        criteria = ("content_relevance", "structure_formatting", "language_professional_tone")
        assert all(row[f"faculty_{c}"] == "" for c in criteria)


def test_only_clean_documents_reach_the_evaluation(run, settings, tmp_path):
    intake, _, _, _ = run
    ds = tmp_path / "ds"
    import_corrected_only(intake, ds, settings)
    path = ds / CANDIDATES
    reviewed = read(path)
    for r in reviewed:
        if r["review_status"] == "needs_review":
            r["reviewer_decision"] = "reject"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(reviewed[0]))
        writer.writeheader()
        writer.writerows(reviewed)
    assert finalize_annotations(ds, settings).written == 2
    freeze_split(ds, eval_fraction=1.0)
    dataset = load_dataset(ds, labels=set(settings.evaluation.labels))
    assert {d.document_id for d in dataset.documents} == {"C01", "C02"}
    assert sum("excluded until reviewed" in w for w in dataset.warnings) == 4
    report = evaluate(dataset, settings, "evaluation", "A")
    assert report.total_error.n == 0  # no faculty marks: no grading accuracy is claimed
    assert report.issue_metrics["section_title"].fn == 1  # no scanner rule for it yet


def test_cli_corrected_only(run, tmp_path):
    intake, _, _, _ = run
    out = tmp_path / "ds"
    args = ["import-corrections", str(intake), "-o", str(out), "--corrected-only"]
    assert main(args) == 0
    assert main(args) == 2  # refuses to overwrite review work
    assert (out / "import_report.md").is_file()
    with pytest.raises(ImportError_):
        import_corrected_only(intake, intake / "inside", load_settings())
