"""Import of teacher-corrected PDFs into the reference format.

Synthetic data only (devtools/synthetic_corrections.py): these tests check that each
correction channel is read faithfully and that nothing is guessed. They do not show how
the importer behaves on real faculty files, which have not been supplied yet.
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import pytest

from pps_ats.cli import main
from pps_ats.config import load_settings
from pps_ats.devtools.synthetic_corrections import build_intake
from pps_ats.evaluation.corrections import extract, marks_in, suggest_label
from pps_ats.evaluation.dataset import freeze_split, load_dataset
from pps_ats.evaluation.importer import (
    CANDIDATES,
    ImportError_,
    finalize_annotations,
    import_corrections,
)
from pps_ats.evaluation.runner import evaluate


def read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def hashes(folder: Path) -> dict[Path, str]:
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob("*") if p.is_file()}


@pytest.fixture(scope="module")
def imported(tmp_path_factory):
    root = tmp_path_factory.mktemp("import")
    intake = build_intake(root / "intake")
    before = hashes(intake)
    settings = load_settings()
    summary = import_corrections(intake, root / "dataset", settings)
    return intake, root / "dataset", summary, before


def cands(dataset: Path, doc: str | None = None) -> list[dict[str, str]]:
    rows = read(dataset / CANDIDATES)
    return [r for r in rows if doc is None or r["document_id"] == doc]


# --------------------------------------------------------------------------- extraction


def test_text_markup_keeps_page_quote_comment_and_author(imported):
    _, dataset, _, _ = imported
    by_quote = {r["quoted_text"]: r for r in cands(dataset, "S001") if r["channel"] == "markup"}
    assert by_quote["Devloped"]["teacher_comment"] == "Spelling: should be Developed"
    assert by_quote["Devloped"]["page"] == "1"
    assert by_quote["Devloped"]["author"] == "Prof. Synthetic (F01)"
    assert "same page of the original" in by_quote["Devloped"]["evidence"]
    # a reply to an annotation is merged into its parent, not a separate correction
    assert by_quote["dashbord"]["teacher_comment"] == "spelling mistake should be dashboard"
    assert by_quote["Pyhton"]["annotation_type"] == "Underline"
    assert by_quote["budget planning,"]["annotation_type"] == "StrikeOut"


def test_red_text_needs_evidence_and_is_never_auto_accepted(imported):
    _, dataset, summary, _ = imported
    red = [r for r in cands(dataset) if r["channel"] == "red_text"]
    assert [r["teacher_correction"] for r in red] == ["Write a specific objective"]
    assert "absent from the original" in red[0]["evidence"]
    assert red[0]["review_status"] == "needs_review"
    # the student's own red headings in S004 are listed as ignored, not as corrections
    s004 = next(d for d in summary.per_document if d["document_id"] == "S004")
    assert any("student's own styling" in i for i in s004["ignored"])
    assert not [r for r in cands(dataset, "S004") if r["channel"] == "red_text"]


def test_red_text_without_an_original_cannot_be_attributed(imported, settings):
    intake, _, _, _ = imported
    result = extract(intake / "corrected" / "S002-corrected.pdf", None, settings.evaluation.import_)
    red = [c for c in result.candidates if c.channel == "red_text"]
    assert red and "cannot tell who added it" in red[0].evidence[0]


def test_annotation_appearances_are_not_double_counted(imported):
    _, dataset, _, _ = imported
    s002 = cands(dataset, "S002")
    assert sum(r["teacher_comment"] == "Add a project description" for r in s002) == 1
    assert [r["annotation_type"] for r in s002 if r["channel"] == "drawing"] == ["Ink"]
    assert not [r for r in cands(dataset, "S001") if r["channel"] == "drawing"]


def test_scanned_corrections_go_to_manual_review(imported):
    _, dataset, _, _ = imported
    (row,) = cands(dataset, "S006")
    assert row["channel"] == "scanned_page" and row["review_status"] == "needs_review"


# --------------------------------------------------------------------------- mapping


def test_label_mapping_flags_uncertainty(imported, settings):
    _, dataset, _, _ = imported
    rows = {r["candidate_id"]: r for r in cands(dataset)}
    assert rows["S001-001"]["suggested_label"] == "spelling"
    assert rows["S001-001"]["review_status"] == "auto_mapped"
    squiggly = next(r for r in rows.values() if r["annotation_type"] == "Squiggly")
    assert squiggly["suggested_label"] == "" and squiggly["review_status"] == "needs_review"
    assert squiggly["label_matches"] == "grammar, formatting"
    strike = next(r for r in rows.values() if r["annotation_type"] == "StrikeOut")
    assert strike["review_status"] == "needs_review" and strike["label_reason"] == "no comment text"
    assert suggest_label("nice work", settings.evaluation.import_)[0] is None
    severity = next(r for r in rows.values() if r["quoted_text"] == "City, State")
    assert severity["suggested_severity"] == "high" and severity["severity_explicit"] == "yes"


def test_marks_in_comments_are_flagged_not_imported(imported):
    _, dataset, _, _ = imported
    note = next(r for r in cands(dataset, "S001") if r["channel"] == "note")
    assert "Total 24/30" in note["marks_mentioned"] and "content 7/10" in note["marks_mentioned"]
    assert note["review_status"] == "needs_review"
    docs = {r["document_id"]: r for r in read(dataset / "documents.csv")}
    assert docs["S001"]["faculty_total"] == "21"  # from marks.csv, not from the comment
    assert marks_in("Structure 8 / 10") == ["Structure 8 / 10"]


# --------------------------------------------------------------------------- marks + pairing


def test_only_clearly_attributable_faculty_marks_are_imported(imported):
    _, dataset, summary, _ = imported
    docs = {r["document_id"]: r for r in read(dataset / "documents.csv")}
    assert {d for d, r in docs.items() if r["marks_source"] == "faculty"} == {"S001", "S005"}
    assert docs["S001"]["grader_id"] == "F01"
    for doc in ("S002", "S003", "S004", "S006"):
        assert docs[doc]["faculty_total"] == "" and docs[doc]["marks_source"] == ""
    reasons = " ".join(summary.marks_rejected)
    for expected in ("grader_id is empty", "!= sum of criteria", "'ai' is not faculty",
                     "S777"):  # fmt: skip
        assert expected in reasons
    assert all(r["annotations_complete"] == "no" for r in docs.values())


def test_pairing_and_intake_untouched(imported):
    intake, dataset, summary, before = imported
    assert summary.documents == 6 and summary.with_corrections == 5
    assert any("S999" in p for p in summary.problems)
    assert hashes(intake) == before  # the intake is only read
    assert (dataset / "files" / "S003.docx").is_file()
    assert (dataset / "corrected" / "S004.pdf").is_file()  # "_reviewed" suffix matched
    assert (dataset / "import_report.md").read_text(encoding="utf-8").startswith("# Import")


def test_explicit_intake_mapping(tmp_path, settings):
    intake = build_intake(tmp_path / "intake")
    (intake / "intake.csv").write_text(
        "document_id,original_file,corrected_file\nA1,S001.pdf,S001_corrected.pdf\n",
        encoding="utf-8",
    )
    summary = import_corrections(intake, tmp_path / "ds", settings)
    assert summary.documents == 1
    assert {r["document_id"] for r in read(tmp_path / "ds" / CANDIDATES)} == {"A1"}


def test_reimport_protects_review_work(imported, settings, tmp_path):
    intake, _, _, _ = imported
    ds = tmp_path / "ds"
    import_corrections(intake, ds, settings)
    with pytest.raises(ImportError_, match="--force"):
        import_corrections(intake, ds, settings)
    import_corrections(intake, ds, settings, force=True)
    assert list(ds.glob("annotation_candidates.csv.*.bak"))


# --------------------------------------------------------------------------- review -> dataset


def test_finalize_requires_decisions_then_feeds_the_evaluator(imported, settings, tmp_path):
    intake, _, _, _ = imported
    ds = tmp_path / "ds"
    import_corrections(intake, ds, settings)
    first = finalize_annotations(ds, settings)
    assert first.unresolved and not (ds / "annotations.csv").exists()

    rows = read(ds / CANDIDATES)
    for r in rows:
        if r["review_status"] == "needs_review":
            if r["annotation_type"] == "Squiggly":
                r.update(reviewer_decision="accept", final_label="grammar", final_severity="medium")
            else:
                r["reviewer_decision"] = "reject"
    write(ds / CANDIDATES, rows)
    bad = [dict(r) for r in rows]
    bad[0]["final_label"] = "typo"  # not a canonical label
    write(ds / "bad.csv", bad)
    done = finalize_annotations(ds, settings)
    assert done.written == 8 and not done.invalid and not done.unresolved
    ann = read(ds / "annotations.csv")
    assert {a["source"] for a in ann} == {"faculty"}
    assert next(a for a in ann if a["text"] == "Devloped")["label"] == "spelling"

    freeze_split(ds, eval_fraction=1.0)
    dataset = load_dataset(ds, labels=set(settings.evaluation.labels))
    assert sum(len(d.annotations) for d in dataset.documents) == 8
    report = evaluate(dataset, settings, "evaluation", "A")
    assert report.documents == 6 and report.issue_metrics["spelling"].tp == 3


def test_invalid_review_rows_block_finalize(imported, settings, tmp_path):
    intake, _, _, _ = imported
    ds = tmp_path / "ds"
    import_corrections(intake, ds, settings)
    rows = read(ds / CANDIDATES)
    rows[0]["final_label"] = "typo"
    write(ds / CANDIDATES, rows)
    result = finalize_annotations(ds, settings, allow_unresolved=True)
    assert any("not canonical" in i for i in result.invalid)
    assert not (ds / "annotations.csv").exists()


def test_cli(imported, tmp_path):
    intake, _, _, _ = imported
    assert main(["import-corrections", str(intake), "-o", str(tmp_path / "ds")]) == 0
    assert main(["finalize-annotations", str(tmp_path / "ds")]) == 1  # undecided rows
    assert main(["finalize-annotations", str(tmp_path / "ds"), "--allow-unresolved"]) == 0
    synced = tmp_path / "OneDrive - College" / "ds"
    assert main(["import-corrections", str(intake), "-o", str(synced)]) == 2
