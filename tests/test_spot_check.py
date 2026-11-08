"""Faculty spot-check package: workbook generation and review integrity.

Synthetic batch documents only.
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import pytest
from openpyxl import load_workbook

from pps_ats.batch.runner import BatchOptions, run_batch
from pps_ats.cli import main
from pps_ats.devtools.synthetic_batch import build_batch
from pps_ats.evaluation import spot_check as sc
from pps_ats.reports.annotate import sha256_file
from pps_ats.schemas.batch import DocumentResult
from pps_ats.schemas.scoring import ScoreStatus

FACULTY = [name for name, _, kind in sc.FINDING_COLUMNS if kind == "faculty"]
DOC_FACULTY = [name for name, _, kind in sc.DOC_COLUMNS if kind == "faculty"]


def _col(columns, name: str) -> int:  # type: ignore[no-untyped-def]
    return [c for c, _, _ in columns].index(name) + 1


@pytest.fixture(scope="module")
def batch(tmp_path_factory, settings):  # type: ignore[no-untyped-def]
    src, manifest = build_batch(tmp_path_factory.mktemp("sc_src"))
    out = tmp_path_factory.mktemp("sc_out") / "run"
    run_batch(BatchOptions(input_dir=src, output_dir=out, manifest=manifest), settings, print)
    results = [
        DocumentResult.model_validate_json(p.read_text(encoding="utf-8"))
        for p in (out / "results").glob("*.json")
    ]
    graded = [
        r for r in results if r.scoring.status != ScoreStatus.NOT_GRADED and r.analysis.findings
    ]
    assert len(graded) >= 3
    ids = sc._load_results(out)
    chosen = [doc for doc, r in ids.items() if r in graded][:3]
    queue = tmp_path_factory.mktemp("sc_queue") / "queue.csv"
    with queue.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["document", "provisional_total", "status", "reasons"])
        for doc in chosen:
            w.writerow([doc, ids[doc].scoring.ca1.total, "provisional", "synthetic test"])
    return src, out, queue, chosen, ids


@pytest.fixture()
def package(batch, tmp_path, settings):  # type: ignore[no-untyped-def]
    src, out, queue, _, _ = batch
    pkg = tmp_path / "pkg"
    summary = sc.build_package(out, src, queue, pkg, settings)
    return pkg, summary


def _fill(pkg: Path, decisions: dict[int, str] | None = None, marks: dict | None = None) -> Path:
    """Simulate a faculty reviewer editing only the yellow cells."""
    book = pkg / sc.BOOK
    wb = load_workbook(book)
    ws = wb["findings"]
    for row, value in (decisions or {}).items():
        ws.cell(row=row, column=_col(sc.FINDING_COLUMNS, "faculty_decision"), value=value)
    docs = wb["documents"]
    for (row, name), value in (marks or {}).items():
        docs.cell(row=row, column=_col(sc.DOC_COLUMNS, name), value=value)
    wb.save(book)
    return book


# --------------------------------------------------------------------------- generation


def test_package_contents_and_originals_unchanged(batch, package, settings):
    src, out, _, chosen, ids = batch
    pkg, summary = package
    before = {p: sha256_file(p) for p in src.rglob("*") if p.is_file()}
    assert summary["documents"] == len(chosen)
    for doc in chosen:
        folder = pkg / sc.DOCS_DIR / doc
        original = src / ids[doc].rel_path
        assert sha256_file(folder / "original.pdf") == sha256_file(original)
        assert (folder / "breakdown.md").read_text(encoding="utf-8").count("PROVISIONAL") >= 1
        if ids[doc].annotation.output_file:
            assert (folder / "annotated.pdf").is_file()
    manifest = json.loads((pkg / sc.MANIFEST).read_text(encoding="utf-8"))
    assert manifest["marks_status"].startswith("PROVISIONAL")
    for rel, digest in manifest["files"].items():
        assert sha256_file(pkg / rel) == digest
    assert {p: sha256_file(p) for p in src.rglob("*") if p.is_file()} == before


def test_one_row_per_finding_with_marks_matching_scoring(batch, package):
    _, _, _, chosen, ids = batch
    pkg, _ = package
    wb = load_workbook(pkg / sc.BOOK, read_only=True)
    rows = sc._read_sheet(wb, "findings", sc.FINDING_COLUMNS)
    docs = sc._read_sheet(wb, "documents", sc.DOC_COLUMNS)
    wb.close()
    assert sum(len(ids[d].analysis.findings) for d in chosen) == len(rows)
    assert len({r["row_id"] for r in rows}) == len(rows)
    for doc in chosen:
        mine = [r for r in rows if r["document_id"] == doc]
        applied = sum(c.applied_points for c in ids[doc].scoring.contributions)
        assert sum(float(r["deducted_marks"]) for r in mine) == pytest.approx(applied, abs=0.05)
        assert all(r["scanner_ca1_total"] == ids[doc].scoring.ca1.total for r in mine)
        row = next(d for d in docs if d["document_id"] == doc)
        assert row["scanner_ca1_total"] == ids[doc].scoring.ca1.total
        assert {r["rule_id"] for r in mine} == {f.rule_id for f in ids[doc].analysis.findings}


def test_faculty_cells_are_never_prefilled(package):
    pkg, _ = package
    wb = load_workbook(pkg / sc.BOOK)
    for sheet, columns, names in (
        ("findings", sc.FINDING_COLUMNS, FACULTY),
        ("documents", sc.DOC_COLUMNS, DOC_FACULTY),
    ):
        ws = wb[sheet]
        for name in names:
            col = _col(columns, name)
            values = [ws.cell(row=r, column=col).value for r in range(2, ws.max_row + 1)]
            assert all(v is None for v in values), (sheet, name)
            assert ws.cell(row=2, column=col).protection.locked is False
        assert ws.protection.sheet is True
        assert ws.cell(row=2, column=1).protection.locked is True


def test_dropdowns_and_score_ranges(package):
    pkg, _ = package
    wb = load_workbook(pkg / sc.BOOK)
    lists = [dv for dv in wb["findings"].data_validations.dataValidation if dv.type == "list"]
    assert len(lists) == 1
    assert lists[0].formula1 == '"agree,disagree,needs_discussion"'
    assert lists[0].showErrorMessage and lists[0].errorStyle == "stop"
    ranges = {
        str(dv.sqref).split(":")[0].rstrip("0123456789"): dv.formula2
        for dv in wb["documents"].data_validations.dataValidation
    }
    letters = {
        name: wb["documents"].cell(row=1, column=_col(sc.DOC_COLUMNS, name)).column_letter
        for name in sc.FACULTY_SCORE_COLUMNS
    }
    assert ranges[letters["faculty_ca1_total"]] == "30"
    assert ranges[letters["faculty_content_relevance"]] == "10"


def test_kinds_are_distinguished(settings):
    from pps_ats.schemas.findings import (
        DetectionMethod,
        Finding,
        FindingCategory,
        FindingClass,
        FindingSeverity,
        RubricCriterion,
    )

    def make(rule: str, **kw) -> Finding:  # type: ignore[no-untyped-def]
        return Finding(
            issue_id="i1",
            rule_id=rule,
            category=FindingCategory.CONTENT_QUALITY,
            finding_class=FindingClass.POTENTIAL_ISSUE,
            severity=FindingSeverity.LOW,
            rubric_criterion=RubricCriterion.CONTENT_RELEVANCE,
            explanation="x",
            confidence=kw.pop("confidence", 0.9),
            detection_method=DetectionMethod.PATTERN,
            **kw,
        )

    zero = next(k for k, v in settings.scoring.rules.items() if v.points == 0)
    assert sc.finding_kind(make(zero), settings) == sc.ZERO_POINT
    assert sc.finding_kind(make("SEC_REQUIRED_MISSING"), settings) == sc.OBJECTIVE
    assert sc.finding_kind(make("PH_EMPTY_FIELD"), settings) == sc.OBJECTIVE
    assert sc.finding_kind(make("CQ_WEAK_VERB"), settings) == sc.SUBJECTIVE
    assert sc.finding_kind(make("FMT_LINE_SPACING"), settings) == sc.SUBJECTIVE
    # objective rule, but uncertain evidence: the reviewer must judge it
    assert sc.finding_kind(make("SEC_CONTACT_MISSING", review_required=True), settings) == (
        sc.SUBJECTIVE
    )
    assert sc.finding_kind(make("LANG_SPELLING", confidence=0.4), settings) == sc.SUBJECTIVE
    assert sc.category("CQ_PROJECT_VAGUE") == "project descriptions"
    assert sc.category("CQ_CERTIFICATION_DETAILS") == "certification details"
    assert sc.category("FMT_LINE_SPACING") == "formatting and layout"
    assert sc.category("LANG_SPELLING") == sc.category("CQ_WEAK_VERB") == "language"


def test_zero_point_rows_deduct_nothing(package):
    pkg, summary = package
    wb = load_workbook(pkg / sc.BOOK, read_only=True)
    rows = sc._read_sheet(wb, "findings", sc.FINDING_COLUMNS)
    wb.close()
    assert all(float(r["deducted_marks"]) == 0 for r in rows if r["kind"] == sc.ZERO_POINT)
    assert all(s["marks_deducted"] == 0 for s in summary["summary"] if s["kind"] == sc.ZERO_POINT)


def test_resume_text_is_never_a_formula(batch, tmp_path, settings):
    src, out, queue, chosen, ids = batch
    copy = tmp_path / "batch"
    shutil.copytree(out, copy)
    path = copy / "results" / f"{ids[chosen[0]].key}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["analysis"]["findings"][0]["detected_text"] = '=HYPERLINK("http://x","y")'
    path.write_text(json.dumps(data), encoding="utf-8")
    pkg = tmp_path / "pkg"
    sc.build_package(copy, src, queue, pkg, settings)
    wb = load_workbook(pkg / sc.BOOK)
    ws = wb["findings"]
    col = _col(sc.FINDING_COLUMNS, "evidence")
    cells = [ws.cell(row=r, column=col) for r in range(2, ws.max_row + 1)]
    hit = [c for c in cells if str(c.value).startswith("=HYPERLINK")]
    assert hit and all(c.data_type == "s" for c in hit)
    assert sc.check_review(pkg)["problems"] == []


def test_refuses_held_out_and_reused_folders(batch, tmp_path, settings):
    src, out, queue, chosen, _ = batch
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "split_lock.json").write_text(
        json.dumps({"splits": {chosen[0]: "evaluation", chosen[1]: "calibration"}}),
        encoding="utf-8",
    )
    with pytest.raises(sc.SpotCheckError, match="not a calibration document"):
        sc.build_package(out, src, queue, tmp_path / "pkg", settings, ref)
    used = tmp_path / "used"
    used.mkdir()
    (used / "x.txt").write_text("x", encoding="utf-8")
    with pytest.raises(sc.SpotCheckError, match="not empty"):
        sc.build_package(out, src, queue, used, settings)
    bad = tmp_path / "bad.csv"
    bad.write_text("document,reasons\nNOPE,x\n", encoding="utf-8")
    with pytest.raises(sc.SpotCheckError, match="not in the batch"):
        sc.build_package(out, src, bad, tmp_path / "pkg2", settings)


# --------------------------------------------------------------------------- integrity


def test_untouched_workbook_is_intact_and_unreviewed(package, tmp_path):
    pkg, summary = package
    report = sc.compare_review(pkg, tmp_path / "cmp")
    assert report["intact"], report["integrity_problems"]
    assert report["decided_rows"] == 0 and not report["review_complete"]
    assert report["documents_with_faculty_total"] == 0
    assert report["finding_rows"] == summary["finding_rows"]


def test_filled_review_is_compared(batch, package, tmp_path):
    _, _, _, chosen, ids = batch
    pkg, summary = package
    n = summary["finding_rows"]
    decisions = {r: ("agree" if r % 2 else "disagree") for r in range(2, n + 2)}
    decisions[2] = "needs_discussion"
    first = ids[chosen[0]].scoring.ca1.total
    _fill(
        pkg,
        decisions,
        {
            (2, "faculty_ca1_total"): 20,
            (3, "faculty_content_relevance"): 8,
            (3, "faculty_structure_formatting"): 7,
            (3, "faculty_language_professional_tone"): 9,
        },
    )
    report = sc.compare_review(pkg, tmp_path / "cmp")
    assert report["intact"], report["integrity_problems"]
    assert report["review_complete"] and report["decided_rows"] == n
    assert report["documents_with_faculty_total"] == 2
    total = report["score_difference_total"]
    assert total["n"] == 2
    second = ids[chosen[1]].scoring.ca1.total
    expected = ((20 - first) + (24 - second)) / 2
    assert total["mean_faculty_minus_scanner"] == pytest.approx(expected, abs=0.01)
    assert report["score_difference_by_criterion"]["content_relevance"]["n"] == 1
    counts = sum(
        v.get("agree", 0) + v.get("disagree", 0) + v.get("needs_discussion", 0)
        for v in report["agreement_by_kind"].values()
    )
    assert counts == n
    assert (tmp_path / "cmp" / "disagreements.csv").is_file()


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ("scanner_cell", "were edited"),
        ("delete_row", "is missing"),
        ("duplicate_row", "appears 2 times"),
        ("bad_decision", "is not allowed"),
        ("score_range", "outside 0-30"),
        ("total_mismatch", "does not equal the sum"),
        ("package_file", "package file changed"),
    ],
)
def test_tampered_review_is_refused(package, tmp_path, edit, message):
    pkg, _ = package
    book = pkg / sc.BOOK
    wb = load_workbook(book)
    ws, docs = wb["findings"], wb["documents"]
    if edit == "scanner_cell":
        ws.cell(row=2, column=_col(sc.FINDING_COLUMNS, "deducted_marks"), value=0)
    elif edit == "delete_row":
        ws.delete_rows(2)
    elif edit == "duplicate_row":
        ws.append([c.value for c in ws[2]])
    elif edit == "bad_decision":
        ws.cell(row=2, column=_col(sc.FINDING_COLUMNS, "faculty_decision"), value="maybe")
    elif edit == "score_range":
        docs.cell(row=2, column=_col(sc.DOC_COLUMNS, "faculty_ca1_total"), value=31)
    elif edit == "total_mismatch":
        for name, value in (
            ("faculty_content_relevance", 8),
            ("faculty_structure_formatting", 8),
            ("faculty_language_professional_tone", 8),
            ("faculty_ca1_total", 25),
        ):
            docs.cell(row=2, column=_col(sc.DOC_COLUMNS, name), value=value)
    wb.save(book)
    if edit == "package_file":
        breakdown = next((pkg / sc.DOCS_DIR).glob("*/breakdown.md"))
        breakdown.write_text("changed", encoding="utf-8")
    report = sc.compare_review(pkg, tmp_path / "cmp")
    assert not report["intact"]
    assert any(message in p for p in report["integrity_problems"]), report["integrity_problems"]
    assert "agreement_by_kind" not in report  # nothing is computed from a tampered review


def test_cli_package_and_compare(batch, tmp_path):
    src, out, queue, chosen, _ = batch
    pkg = tmp_path / "pkg"
    assert (
        main(
            [
                "spot-check-package",
                str(out),
                "--input",
                str(src),
                "--queue",
                str(queue),
                "-o",
                str(pkg),
            ]
        )
        == 0
    )
    assert (pkg / sc.BOOK).is_file()
    assert main(["spot-check-compare", str(pkg), "-o", str(tmp_path / "cmp")]) == 0
    _fill(pkg, {2: "perhaps"})
    assert main(["spot-check-compare", str(pkg), "-o", str(tmp_path / "cmp2")]) == 1
