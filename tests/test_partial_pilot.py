"""Partial calibration pilot: only completely reviewed calibration documents are used.

Synthetic data only: distinct resumes with Preview-style FreeText corrections.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pymupdf
import pytest
from openpyxl import load_workbook

from pps_ats.cli import main
from pps_ats.config import load_settings
from pps_ats.devtools import synthetic
from pps_ats.devtools import synthetic_analysis as sa
from pps_ats.evaluation.corrected_only import import_corrected_only
from pps_ats.evaluation.dataset import freeze_split
from pps_ats.evaluation.pilot import assess, run_partial_pilot
from pps_ats.evaluation.reference import (
    ReferenceError,
    build_review_workbook,
    prepare_reference,
)

RED = (1.0, 0.149, 0.0)
COMMENTS = {
    "A_resume": ["add 12th and 10th", "title as technical skills", "Keep bullets concise"],
    "B_template": ["add 12th and 10th", "add linkedin profile", "title as projects"],
    "C_missing": ["add 12th and 10th", "add contact details"],
    "D_spelling": ["add 12th and 10th", "title as skills"],
}
BUILDERS = {
    "A_resume": synthetic.resume_pdf,
    "B_template": sa.template_resume_pdf,
    "C_missing": sa.missing_sections_pdf,
    "D_spelling": sa.spelling_resume_pdf,
}


def annotate(path: Path, comments: list[str]) -> None:
    doc = pymupdf.open(path)
    page = doc[0]
    for i, text in enumerate(comments):
        rect = pymupdf.Rect(330, 120 + 40 * i, 560, 140 + 40 * i)
        a = page.add_freetext_annot(rect, text, fontsize=7, fontname="helv", text_color=RED)
        a.set_info(content=text, title="Faculty (synthetic)")
        a.update()
    tmp = path.with_name(path.stem + "_tmp.pdf")
    doc.save(tmp)
    doc.close()
    tmp.replace(path)


def build(tmp: Path) -> tuple[Path, dict[str, str]]:
    intake = tmp / "intake"
    for name, builder in BUILDERS.items():
        annotate(builder(intake / f"{name}.pdf"), COMMENTS[name])
    scan = synthetic.image_only_pdf(intake / "E_scan.pdf")
    annotate(scan, ["add 12th and 10th"])
    settings = load_settings()
    import_corrected_only(intake, tmp / "imported", settings)
    ref = tmp / "reference"
    prepare_reference(tmp / "imported", ref, tmp / "key", settings)
    with (tmp / "key" / "id_key.csv").open(encoding="utf-8") as handle:
        pid = {r["source_document_id"]: r["pseudonym"] for r in csv.DictReader(handle)}
    # held-out: D_spelling; everything else calibration (written before freezing)
    path = ref / "documents.csv"
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for r in rows:
        r["split"] = "evaluation" if r["document_id"] == pid["D_spelling"] else "calibration"
    with path.open("w", newline="", encoding="utf-8") as handle:
        w = csv.DictWriter(handle, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    freeze_split(ref)
    build_review_workbook(ref, settings)
    return ref, pid


def review(ref: Path, pid: dict[str, str]) -> None:
    """A: complete (one recommendation); B: partly reviewed; C: accepted rows, one without
    reviewer_id; E (image-only): complete but ineligible; D (held-out): fully reviewed."""
    path = ref / "review" / "annotation_review.xlsx"
    wb = load_workbook(path)
    for sheet in ("calibration", "evaluation"):
        ws = wb[sheet]
        header = [c.value for c in ws[1]]
        col = {h: i + 1 for i, h in enumerate(header)}
        seen: dict[str, int] = {}
        for r in range(2, ws.max_row + 1):
            doc = ws.cell(row=r, column=col["document_id"]).value
            n = seen[doc] = seen.get(doc, 0) + 1
            text = ws.cell(row=r, column=col["faculty_correction"]).value or ""

            def put(name, value, r=r, ws=ws, col=col):  # type: ignore[no-untyped-def]
                ws.cell(row=r, column=col[name]).value = value

            label = (
                "education_details" if "12th" in text
                else "section_title" if text.startswith("title")
                else "contact" if "linkedin" in text or "contact" in text
                else "weak_language"
            )  # fmt: skip
            accept = {"decision": "accept", "final_label": label,
                      "faculty_intent": "correction_required", "reviewer_id": "R1"}  # fmt: skip
            if doc == pid["A_resume"]:
                if "concise" in text:
                    accept["faculty_intent"] = "recommendation"
                for k, v in accept.items():
                    put(k, v)
            elif doc == pid["B_template"] and n == 1:
                for k, v in accept.items():
                    put(k, v)
            elif doc == pid["C_missing"]:
                for k, v in accept.items():
                    put(k, v)
                if n == 2:
                    put("reviewer_id", None)
            elif doc in (pid["E_scan"], pid["D_spelling"]):
                for k, v in accept.items():
                    put(k, v)
    wb.save(path)
    wb.close()


@pytest.fixture()
def reviewed(tmp_path):
    ref, pid = build(tmp_path)
    review(ref, pid)
    return ref, pid


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- tests


def test_assessment_per_document(reviewed):
    ref, pid = reviewed
    by_id = {d.document_id: d for d in assess(ref, load_settings())}
    assert by_id[pid["A_resume"]].status == "complete" and by_id[pid["A_resume"]].eligible
    assert by_id[pid["B_template"]].status == "partial"
    assert by_id[pid["C_missing"]].status == "partial"  # accepted without reviewer_id
    assert by_id[pid["E_scan"]].status == "complete" and not by_id[pid["E_scan"]].eligible
    assert by_id[pid["D_spelling"]].split == "evaluation"


def test_pilot_uses_only_complete_eligible_calibration_documents(reviewed, tmp_path):
    ref, pid = reviewed
    settings = load_settings()
    protected = ["review/annotation_review.xlsx", "split_lock.json", "documents.csv",
                 "annotation_candidates.csv"]  # fmt: skip
    before = {p: sha(ref / p) for p in protected}
    out = tmp_path / "pilot"
    s = run_partial_pilot(ref, out, settings)
    assert {p: sha(ref / p) for p in protected} == before  # master + split untouched
    assert s["usable_documents"] == 1 and s["calibration_documents"] == 4
    assert s["excluded"]["calibration_partially_reviewed"] == 2
    assert s["excluded"]["calibration_complete_but_ineligible"] == 1
    assert s["excluded"]["evaluation_documents_never_used"] == 1
    assert s["excluded"]["evaluation_rows_already_decided"] == 2  # reviewed but never used
    with (out / "dataset" / "documents.csv").open(encoding="utf-8") as handle:
        docs = list(csv.DictReader(handle))
    assert [d["document_id"] for d in docs] == [pid["A_resume"]]
    assert all(d["faculty_total"] == "" and d["annotations_complete"] == "no" for d in docs)
    with (out / "dataset" / "annotations.csv").open(encoding="utf-8") as handle:
        ann = list(csv.DictReader(handle))
    assert len(ann) == s["verified_annotations"] == 3
    assert {a["document_id"] for a in ann} == {pid["A_resume"]}
    assert all(a["reviewer_id"] == "R1" and a["comment"] == "" for a in ann)


def test_metrics_separate_groups_and_withhold_precision(reviewed, tmp_path):
    ref, _ = reviewed
    s = run_partial_pilot(ref, tmp_path / "pilot", load_settings())
    m = s["metrics"]
    g = m["groups"]
    assert g["recommendation"]["faculty_annotations"] == 1
    assert g["recommendation"]["recall"] is None
    assert g["outside_scanner_scope"]["faculty_annotations"] == 1  # section_title
    assert g["detectable_error"]["faculty_annotations"] == 1  # education_details
    assert m["detectable_error_recall"] is not None
    assert m["precision"] is None and m["f1"] is None and m["marks_agreement"] is None
    assert "unverified" in m["precision_note"]
    report = json.loads((tmp_path / "pilot" / "report" / "evaluation.json").read_text("utf-8"))
    assert report["total_error"]["n"] == 0
    assert report["warnings"][0].startswith("NO FACULTY MARKS") or any(
        "PARTIAL CALIBRATION PILOT" in w for w in report["warnings"]
    )
    md = (tmp_path / "pilot" / "pilot_summary.md").read_text(encoding="utf-8")
    assert "NOT false positives" in md and "add 12th" not in md


def test_unreviewed_rows_are_never_auto_accepted(tmp_path):
    ref, _ = build(tmp_path)
    with pytest.raises(ReferenceError, match="no calibration document is completely reviewed"):
        run_partial_pilot(ref, tmp_path / "pilot", load_settings())


def test_pilot_never_overwrites_and_stays_outside_the_reference(reviewed, tmp_path):
    ref, _ = reviewed
    settings = load_settings()
    run_partial_pilot(ref, tmp_path / "pilot", settings)
    with pytest.raises(ReferenceError, match="never overwritten"):
        run_partial_pilot(ref, tmp_path / "pilot", settings)
    with pytest.raises(ReferenceError, match="outside the reference"):
        run_partial_pilot(ref, ref / "pilot", settings)


def test_cli_partial_pilot(reviewed, tmp_path, capsys):
    ref, _ = reviewed
    assert main(["partial-pilot", str(ref), "-o", str(tmp_path / "p")]) == 0
    err = capsys.readouterr().err
    assert "1 usable documents" in err and "precision/F1 not computed" in err
    for secret in ("12th", "linkedin", "ASHA", "MEERA"):
        assert secret not in err
    assert main(["partial-pilot", str(ref), "-o", str(tmp_path / "OneDrive - X" / "p")]) == 2
