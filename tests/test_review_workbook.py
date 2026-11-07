"""Review workbook: layout, controlled values, round-trips and completeness checks.

Synthetic corrected-only PDFs only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from pps_ats.cli import main
from pps_ats.config import load_settings
from pps_ats.devtools.synthetic_corrected_only import build_corrected_only
from pps_ats.evaluation.dataset import freeze_split, load_dataset
from pps_ats.evaluation.importer import ImportError_, finalize_annotations
from pps_ats.evaluation.reference import (
    ReferenceError,
    build_review_workbook,
    finalize_review,
    review_status,
)
from pps_ats.evaluation.runner import run_evaluation
from test_reference import make_reference

BOOK = Path("review") / "annotation_review.xlsx"


@pytest.fixture()
def ref(tmp_path):
    _, reference, _ = make_reference(tmp_path)
    freeze_split(reference, seed=7, eval_count=0)  # everything in calibration
    build_review_workbook(reference, load_settings())
    return reference


def sheet_rows(ref: Path, name: str = "calibration") -> tuple[list[str], list[list]]:
    wb = load_workbook(ref / BOOK)
    ws = wb[name]
    header = [c.value for c in ws[1]]
    rows = [[c.value for c in r] for r in ws.iter_rows(min_row=2)]
    wb.close()
    return header, rows


def edit(ref: Path, change, name: str = "calibration") -> None:  # type: ignore[no-untyped-def]
    """Open, change and save the workbook like a reviewer session would."""
    path = ref / BOOK
    wb = load_workbook(path)
    ws = wb[name]
    header = [c.value for c in ws[1]]
    change(ws, {h: i + 1 for i, h in enumerate(header)})
    wb.save(path)
    wb.close()


def decide_all(ws, col, decision="accept"):  # type: ignore[no-untyped-def]
    for r in range(2, ws.max_row + 1):
        ws.cell(row=r, column=col["decision"], value=decision)
        if decision == "accept":
            ws.cell(row=r, column=col["final_label"], value="other_feedback")
            ws.cell(row=r, column=col["faculty_intent"], value="recommendation")
            ws.cell(row=r, column=col["reviewer_id"], value="R1")


# --------------------------------------------------------------------------- layout


def test_layout_headers_filters_freeze_and_controlled_values(ref):
    wb = load_workbook(ref / BOOK)
    for name in ("calibration", "evaluation"):
        ws = wb[name]
        header = [c.value for c in ws[1]]
        assert header[:4] == ["candidate_id", "document_id", "page", "faculty_correction"]
        assert ws.freeze_panes == "D2" and ws.auto_filter.ref.startswith("A1:")
        validated = {}
        for dv in ws.data_validations.dataValidation:
            column = header[ws[str(dv.sqref).split(":")[0]].column - 1]
            validated[column] = dv
            assert dv.showErrorMessage and dv.errorStyle == "stop" and dv.prompt
        assert set(validated) == {"decision", "final_label", "faculty_intent", "final_severity"}
        assert validated["decision"].formula1 == '"accept,reject,unclear"'
        assert validated["faculty_intent"].formula1 == (
            '"correction_required,recommendation,unclear"'
        )
        assert validated["final_label"].formula1.startswith("labels!")
    ws = wb["calibration"]
    header = [c.value for c in ws[1]]
    fills = {h: ws.cell(row=1, column=i + 1).fill.fgColor.rgb for i, h in enumerate(header)}
    # suggestions (grey) are visually distinct from the reviewer's columns (gold)
    assert fills["suggested_label"] != fills["final_label"] != fills["faculty_correction"]
    body = ws.cell(row=2, column=header.index("faculty_correction") + 1)
    assert body.alignment.wrap_text and ws.column_dimensions["D"].width >= 40
    assert isinstance(ws.cell(row=2, column=3).value, int)  # page is a number
    wb.close()


def test_rows_have_unique_ids_and_no_prefilled_decisions(ref):
    header, rows = sheet_rows(ref)
    ids = [r[header.index("candidate_id")] for r in rows]
    assert ids and len(ids) == len(set(ids)) and all(ids)
    for column in ("decision", "final_label", "faculty_intent", "reviewer_id"):
        assert all(r[header.index(column)] in (None, "") for r in rows)
    assert any(r[header.index("suggested_label")] for r in rows)  # hints are shown


# --------------------------------------------------------------------------- decisions


def test_accept_reject_unclear_and_status_counts(ref):
    settings = load_settings()
    total = review_status(ref, settings).by_split["calibration"]["total"]
    assert total >= 3

    def three(ws, col):  # type: ignore[no-untyped-def]
        ws.cell(row=2, column=col["decision"], value="Accept ")  # Excel-style capitalisation
        ws.cell(row=2, column=col["final_label"], value="other_feedback")
        ws.cell(row=2, column=col["faculty_intent"], value="correction_required")
        ws.cell(row=2, column=col["reviewer_id"], value="R1")
        ws.cell(row=3, column=col["decision"], value="reject")
        ws.cell(row=4, column=col["decision"], value="unclear")

    edit(ref, three)
    st = review_status(ref, settings).by_split["calibration"]
    assert (st["confirmed"], st["rejected"], st["unclear"]) == (1, 1, 1)
    assert st["unreviewed"] == total - 3 and st["remaining"] == total - 2
    assert not st["ready_to_finalize"] and not st["finalized"]
    with pytest.raises(ReferenceError, match="not ready"):
        finalize_review(ref, settings, "calibration")


def test_decisions_survive_save_reopen_and_sorting_then_finalize(ref):
    settings = load_settings()
    edit(ref, lambda ws, col: decide_all(ws, col, "reject"))
    # a later session: reopen, sort rows (reverse) and change one decision to accept
    path = ref / BOOK
    wb = load_workbook(path)
    ws = wb["calibration"]
    header = [c.value for c in ws[1]]
    values = [[c.value for c in r] for r in ws.iter_rows(min_row=2)]
    for r, row in enumerate(reversed(values), 2):
        for c, v in enumerate(row, 1):
            ws.cell(row=r, column=c).value = v  # (ws.cell(value=None) would not clear)
    col = {h: i + 1 for i, h in enumerate(header)}
    ws.cell(row=2, column=col["decision"], value="accept")
    ws.cell(row=2, column=col["final_label"], value="section_title")
    ws.cell(row=2, column=col["faculty_intent"], value="recommendation")
    ws.cell(row=2, column=col["reviewer_id"], value="R1")
    wb.save(path)
    wb.close()
    status = review_status(ref, settings)
    st = status.by_split["calibration"]
    assert st["confirmed"] == 1 and st["ready_to_finalize"], status.invalid
    done = finalize_review(ref, settings, "calibration")
    assert done["accepted"] == 1 and done["recommendations"] == 1
    assert review_status(ref, settings).by_split["calibration"]["finalized"]


# --------------------------------------------------------------------------- safety


def _delete_row(ws, col):  # type: ignore[no-untyped-def]
    ws.delete_rows(3)


def _edit_comment(ws, col):  # type: ignore[no-untyped-def]
    ws.cell(row=2, column=col["faculty_correction"]).value = "changed"


def _duplicate_id(ws, col):  # type: ignore[no-untyped-def]
    ws.cell(row=3, column=col["candidate_id"]).value = ws.cell(
        row=2, column=col["candidate_id"]
    ).value


def _clear_id(ws, col):  # type: ignore[no-untyped-def]
    ws.cell(row=2, column=col["candidate_id"]).value = None


def _bad_decision(ws, col):  # type: ignore[no-untyped-def]
    ws.cell(row=2, column=col["decision"]).value = "maybe"


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (_delete_row, "missing"),
        (_edit_comment, "edited"),
        (_duplicate_id, "more than once"),
        (_clear_id, "no candidate_id"),
        (_bad_decision, "not accept/reject"),
    ],
)
def test_incomplete_or_damaged_review_cannot_be_finalised(ref, damage, message):
    settings = load_settings()
    edit(ref, lambda ws, col: decide_all(ws, col, "reject"))
    assert review_status(ref, settings).by_split["calibration"]["ready_to_finalize"]
    edit(ref, damage)
    st = review_status(ref, settings)
    assert not st.by_split["calibration"]["ready_to_finalize"]
    assert any(message in p for p in st.invalid)
    with pytest.raises(ReferenceError):
        finalize_review(ref, settings, "calibration")
    assert not (ref / "annotations.csv").exists()


def test_accepted_rows_need_label_intent_and_reviewer(ref):
    settings = load_settings()
    edit(ref, lambda ws, col: decide_all(ws, col, "reject"))
    edit(ref, lambda ws, col: ws.cell(row=2, column=col["decision"], value="accept"))
    st = review_status(ref, settings)
    assert any("final_label" in p for p in st.invalid)


def test_unreviewed_data_never_reaches_evaluation(ref, tmp_path):
    settings = load_settings()
    dataset = load_dataset(ref, labels=set(settings.evaluation.labels))
    with pytest.raises(ValueError, match="calibration review is not finalised"):
        run_evaluation(dataset, settings, tmp_path / "cal", split="calibration")
    with pytest.raises(ImportError_, match="frozen reference"):
        finalize_annotations(ref, settings, accept_suggestions=True)
    assert not (ref / "annotations.csv").exists()


def test_rebuild_protects_reviewer_input(ref):
    settings = load_settings()
    with pytest.raises(ReferenceError, match="exists"):
        build_review_workbook(ref, settings)
    build_review_workbook(ref, settings, force=True)  # untouched: allowed
    edit(ref, lambda ws, col: ws.cell(row=2, column=col["reviewer_notes"], value="note"))
    with pytest.raises(ReferenceError, match="discard"):
        build_review_workbook(ref, settings, force=True)
    build_review_workbook(ref, settings, force=True, discard_decisions=True)
    _, rows = sheet_rows(ref)
    assert all(v in (None, "") for r in rows for v in r[10:16])


def test_review_status_cli_reports_counts_without_text(tmp_path, capsys):
    intake = build_corrected_only(tmp_path / "intake")
    from pps_ats.evaluation.corrected_only import import_corrected_only

    import_corrected_only(intake, tmp_path / "imported", load_settings())
    ref = tmp_path / "ref"
    assert main(["reference-prepare", str(tmp_path / "imported"), "-o", str(ref),
                 "--key-dir", str(tmp_path / "key")]) == 0  # fmt: skip
    assert main(["eval-split", str(ref), "--eval-count", "2", "--seed", "3"]) == 0
    assert main(["review-sheet", str(ref)]) == 0
    capsys.readouterr()
    assert main(["review-status", str(ref)]) == 0
    err = capsys.readouterr().err
    assert "calibration" in err and "remaining" in err and "not reviewed" in err
    for secret in ("title as technical skills", "riya", "RIYA", "add 12th"):
        assert secret not in err
