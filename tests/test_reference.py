"""Frozen faculty reference dataset: pseudonymisation, integrity, split, review, gating.

Synthetic corrected-only PDFs only.
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from pathlib import Path

import pymupdf
import pytest
from openpyxl import load_workbook

from pps_ats.cli import main
from pps_ats.config import load_settings
from pps_ats.devtools.synthetic_corrected_only import build_corrected_only
from pps_ats.evaluation.corrected_only import import_corrected_only
from pps_ats.evaluation.dataset import DatasetError, freeze_split, load_dataset
from pps_ats.evaluation.reference import (
    CANDIDATES,
    ReferenceError,
    build_review_workbook,
    finalize_review,
    freeze_calibration,
    held_out_gate,
    prepare_reference,
    review_status,
    verify_reference,
)
from pps_ats.evaluation.runner import run_evaluation


def read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def make_reference(tmp: Path) -> tuple[Path, Path, Path]:
    intake = build_corrected_only(tmp / "intake")
    # an identical resubmission under another name (must stay in the same split)
    shutil.copyfile(intake / "C01_corrected.pdf", intake / "C07_corrected.pdf")
    settings = load_settings()
    import_corrected_only(intake, tmp / "imported", settings)
    log = tmp / "salvage_log.csv"
    with log.open("w", newline="", encoding="utf-8") as handle:
        w = csv.writer(handle)
        w.writerow(["entry", "zip_path", "status", "sha256", "bytes"])
        for i, p in enumerate(sorted(intake.glob("*.pdf")), 1):
            w.writerow([i, p.name, "complete", hashlib.sha256(p.read_bytes()).hexdigest(), 1])
        w.writerow([99, "cut.pdf", "truncated (not extracted)", "", 0])
    ref, key = tmp / "reference", tmp / "key"
    prepare_reference(tmp / "imported", ref, key, settings, salvage_log=log, intake=intake)
    return intake, ref, key


@pytest.fixture()
def reference(tmp_path):
    return make_reference(tmp_path)


def fill(ref: Path, sheet: str, decide) -> None:  # type: ignore[no-untyped-def]
    path = ref / "review" / "annotation_review.xlsx"
    wb = load_workbook(path)
    ws = wb[sheet]
    header = [c.value for c in ws[1]]
    col = {h: i + 1 for i, h in enumerate(header)}
    for r in range(2, ws.max_row + 1):
        row = {h: ws.cell(row=r, column=col[h]).value for h in header}
        for key, value in decide(row).items():
            ws.cell(row=r, column=col[key], value=value)
    wb.save(path)


def accept_all(row):  # type: ignore[no-untyped-def]
    if row["source_type"] == "image_only_page" or row["faculty_likelihood"] == "ambiguous":
        return {"decision": "reject", "reviewer_id": "R1"}
    return {
        "decision": "accept",
        "final_label": row["suggested_label"] or "other_feedback",
        "faculty_intent": "correction_required",
        "reviewer_id": "R1",
    }


# --------------------------------------------------------------------------- prepare


def test_pseudonymised_copy_and_private_key(reference):
    intake, ref, key = reference
    docs = read(ref / "documents.csv")
    assert [d["document_id"] for d in docs] == [f"P{i:03d}" for i in range(1, 8)]
    blob = "".join(p.read_text(encoding="utf-8") for p in ref.glob("*.csv"))
    blob += json.dumps(json.loads((ref / "reference_manifest.json").read_text(encoding="utf-8")))
    for original in ("C01", "C02", "C03", "_corrected", "Faculty (synthetic)"):
        assert original not in blob
    assert sorted(p.name for p in (ref / "files").iterdir())[0] == "P001.pdf"
    keyrows = read(key / "id_key.csv")
    assert {r["intake_file"] for r in keyrows} == {p.name for p in intake.glob("*.pdf")}
    assert all(d["marks_source"] == "" and d["faculty_total"] == "" for d in docs)
    cands = read(ref / CANDIDATES)
    assert all(c["final_label"] == "" and c["reviewer_decision"] == "" for c in cands)
    assert {c["author"] for c in cands if c["author"]} == {"A1"}


def test_integrity_and_duplicate_grouping(reference):
    _, ref, _ = reference
    manifest = json.loads((ref / "reference_manifest.json").read_text(encoding="utf-8"))
    assert manifest["salvage_log_matches"] == 7 == manifest["salvage_log_complete_entries"]
    assert manifest["duplicates"]["identical_files"] == 1
    groups = [d["group_id"] for d in read(ref / "documents.csv")]
    assert len(groups) - len(set(groups)) >= 1  # the copy shares its original's group
    report = verify_reference(ref, load_settings())
    assert report.failures == [] and report.corrected_copies_identical == 7
    assert report.clean_without_annotations == report.clean_inputs == 3
    assert report.clean_red_text_is_student_only == 3  # incl. student's red headings
    assert report.long_comments_found_in_clean_input == 0
    assert report.ineligible == {"needs_review": 3, "manual_transcription": 1}


def test_verify_detects_a_contaminated_clean_input(reference):
    _, ref, _ = reference
    clean = next(d for d in read(ref / "documents.csv") if d["scanner_input_status"] == "clean")
    path = ref / clean["file"]
    doc = pymupdf.open(path)
    doc[0].insert_text((50, 800), "add career objective here", color=(1, 0.15, 0))
    doc.save(path.with_name("tmp.pdf"))
    doc.close()
    path.with_name("tmp.pdf").replace(path)
    assert verify_reference(ref, load_settings()).failures


def test_prepare_never_overwrites(reference, tmp_path):
    _, ref, key = reference
    with pytest.raises(ReferenceError, match="never overwritten"):
        prepare_reference(tmp_path / "imported", ref, key, load_settings())


# --------------------------------------------------------------------------- split


def test_exact_stratified_grouped_split_is_frozen(reference):
    _, ref, _ = reference
    counts = freeze_split(ref, seed=7, eval_count=3, stratify_by="scanner_input_status",
                          group_by="group_id")  # fmt: skip
    assert counts["evaluation"] >= 3 and sum(counts.values()) == 7
    docs = read(ref / "documents.csv")
    by_group: dict[str, set[str]] = {}
    for d in docs:
        by_group.setdefault(d["group_id"], set()).add(d["split"])
    assert all(len(s) == 1 for s in by_group.values())  # no leakage
    before = (ref / "documents.csv").read_text(encoding="utf-8")
    freeze_split(ref, seed=99, eval_count=6)  # cannot re-draw a frozen split
    assert (ref / "documents.csv").read_text(encoding="utf-8") == before
    lock = json.loads((ref / "split_lock.json").read_text(encoding="utf-8"))
    assert lock["seed"] == 7 and lock["eval_count"] == 3
    moved = docs[0]
    moved["split"] = "calibration" if moved["split"] == "evaluation" else "evaluation"
    with (ref / "documents.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(docs[0]))
        writer.writeheader()
        writer.writerows(docs)
    with pytest.raises(DatasetError, match="changed split"):
        load_dataset(ref)


# --------------------------------------------------------------------------- review + gate


def test_review_flow_and_held_out_gate(reference, tmp_path):
    _, ref, _ = reference
    settings = load_settings()
    freeze_split(ref, seed=7, eval_count=3, stratify_by="scanner_input_status", group_by="group_id")
    book = build_review_workbook(ref, settings)
    wb = load_workbook(book)
    assert wb.sheetnames == ["Instructions", "labels", "calibration", "evaluation"]
    header = [c.value for c in wb["calibration"][1]]
    for column in ("document_id", "page", "faculty_correction", "associated_section",
                   "suggested_label", "alternative_labels", "suggestion_reason", "decision",
                   "final_label", "reviewer_notes"):  # fmt: skip
        assert column in header
    assert all(
        r[header.index("final_label")].value is None for r in wb["calibration"].iter_rows(min_row=2)
    )
    wb.close()
    with pytest.raises(ReferenceError, match="exists"):
        build_review_workbook(ref, settings)

    # nothing is accepted automatically
    st = review_status(ref, settings)
    assert st.by_split["calibration"]["confirmed"] == 0
    assert st.by_split["calibration"]["unreviewed"] > 0
    with pytest.raises(ReferenceError, match="not ready"):
        finalize_review(ref, settings, "calibration")

    # an accepted row without a label/intent/reviewer is invalid
    fill(ref, "calibration", lambda row: {"decision": "accept"})
    assert review_status(ref, settings).invalid

    fill(ref, "calibration", accept_all)
    expected = sum(
        1
        for r in load_workbook(book, read_only=True)["calibration"].iter_rows(
            min_row=2, values_only=True
        )
        if r[0] and r[10] == "accept"
    )
    done = finalize_review(ref, settings, "calibration")
    assert done["accepted"] == expected and done["reviewers"] == ["R1"]
    ann = read(ref / "annotations.csv")
    assert len(ann) == expected
    assert all(a["source"] == "faculty" and a["intent"] and a["reviewer_id"] for a in ann)

    # calibration-only comparison is allowed now; held-out is not
    dataset = load_dataset(ref, labels=set(settings.evaluation.labels))
    cal = run_evaluation(dataset, settings, tmp_path / "cal", split="calibration")
    assert cal.total_error.n == 0 and cal.warnings[0].startswith("NO FACULTY MARKS")
    reasons = held_out_gate(ref, settings)
    assert any("not frozen" in r for r in reasons) and any("independently" in r for r in reasons)
    with pytest.raises(ValueError, match="not allowed yet"):
        run_evaluation(dataset, settings, tmp_path / "ev", split="evaluation")

    freeze_calibration(ref, settings)
    with pytest.raises(ReferenceError, match="already frozen"):
        freeze_calibration(ref, settings)
    fill(ref, "evaluation", lambda row: {**accept_all(row), "reviewer_id": "R2"})
    finalize_review(ref, settings, "evaluation")
    assert held_out_gate(ref, settings) == []
    report = run_evaluation(load_dataset(ref, labels=set(settings.evaluation.labels)), settings,
                            tmp_path / "ev", split="evaluation")  # fmt: skip
    assert report.held_out

    # changing the configuration after the freeze blocks the held-out evaluation
    changed = load_settings()
    changed.evaluation.import_.keywords["spelling"].append("speling")
    assert any("changed" in r for r in held_out_gate(ref, changed))
    # editing annotations after finalisation is detected
    (ref / "annotations.csv").write_text("document_id,label\n", encoding="utf-8")
    assert any("changed after" in r for r in held_out_gate(ref, settings))


def test_out_of_scope_feedback_and_recommendations_are_not_scanner_errors(reference, tmp_path):
    _, ref, _ = reference
    settings = load_settings()
    freeze_split(ref, seed=7, eval_count=0)
    build_review_workbook(ref, settings)

    def decide(row):  # type: ignore[no-untyped-def]
        out = accept_all(row)
        if out["decision"] == "accept":
            out["final_label"] = "section_title"
            out["faculty_intent"] = "recommendation"
            out["final_severity"] = "high"
        return out

    fill(ref, "calibration", decide)
    finalize_review(ref, settings, "calibration")
    dataset = load_dataset(ref, labels=set(settings.evaluation.labels))
    report = run_evaluation(dataset, settings, tmp_path / "r", split="calibration")
    assert report.outside_scanner_scope.get("section_title", 0) >= 1
    assert report.issue_overall.fn == 0  # not counted as scanner misses
    assert report.high_severity.fn == 0 and report.recommendations >= 1


def test_cli_reference_commands(tmp_path):
    intake = build_corrected_only(tmp_path / "intake")
    import_corrected_only(intake, tmp_path / "imported", load_settings())
    ref = tmp_path / "ref"
    assert main(["reference-prepare", str(tmp_path / "imported"), "-o", str(ref),
                 "--key-dir", str(tmp_path / "key")]) == 0  # fmt: skip
    assert main(["verify-reference", str(ref)]) == 0
    split = ["--stratify-by", "scanner_input_status", "--group-by", "group_id"]
    assert main(["eval-split", str(ref), "--eval-count", "2", "--seed", "5", *split]) == 0
    assert main(["review-sheet", str(ref)]) == 0
    assert main(["review-status", str(ref)]) == 0
    assert main(["finalize-review", str(ref), "--split", "calibration"]) == 2
    assert main(["freeze-calibration", str(ref)]) == 2
    assert main(["evaluate", str(ref), "-o", str(tmp_path / "ev")]) == 2  # gated


def test_identity_links_ignore_year_ranges():
    from pps_ats.evaluation.reference import duplicate_groups, phone_numbers

    assert phone_numbers("B.Tech 2022 - 2026, CGPA 8.1") == set()
    assert phone_numbers("+91 90000 44444") == {"9000044444"}
    texts = {
        "A": "Riya Example B.Tech 2022 - 2026 riya@example.com python flask sql " * 3,
        "B": "Arun Sample B.Tech 2022 - 2026 arun@example.com java spring docker " * 3,
        "C": "Same student second file riya@example.com other words entirely here " * 3,
    }
    groups, stats = duplicate_groups(texts, {"A": "1", "B": "2", "C": "3"})
    assert groups["A"] == groups["C"] != groups["B"]  # shared e-mail, not shared years
    assert stats["shared_contact_pairs"] == 1
