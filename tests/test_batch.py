"""Milestone 4: resumable batch processing and the consolidated Excel workbook."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

from pps_ats.batch import worker
from pps_ats.batch.discovery import make_key
from pps_ats.batch.journal import Journal
from pps_ats.batch.runner import JOURNAL, WORKBOOK, BatchError, BatchOptions, run_batch
from pps_ats.cli import main
from pps_ats.devtools import synthetic
from pps_ats.devtools.synthetic_batch import build_batch
from pps_ats.reports.annotate import sha256_file
from pps_ats.reports.excel import Entry, WorkbookWriter
from pps_ats.schemas.batch import DocumentResult, ProcessingStatus

INVALID_OR_UNREADABLE = {
    "div_B/Broken_Resume.pdf",
    "div_B/Nikhil_Demo_Resume.pdf",
    "div_B/Scan_Resume.pdf",
}
SCORE_COLS = (
    "Content & Relevance /10",
    "Structure & Formatting /10",
    "Language & Professional Tone /10",
)


def sheet(path: Path, name: str) -> list[dict]:
    wb = load_workbook(path, read_only=True)
    rows = list(wb[name].iter_rows(values_only=True))
    wb.close()  # read-only workbooks keep the file open (and locked on Windows)
    width = len(rows[0])  # read-only mode drops trailing empty cells
    return [dict(zip(rows[0], (*r, *[None] * (width - len(r))), strict=True)) for r in rows[1:]]


def opts(src: Path, out: Path, manifest: Path | None = None, **kw) -> BatchOptions:  # type: ignore[no-untyped-def]
    return BatchOptions(input_dir=src, output_dir=out, manifest=manifest, **kw)


def quiet(_: str) -> None:
    pass


@pytest.fixture(scope="module")
def batch_inputs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    return build_batch(tmp_path_factory.mktemp("batch_src"))


@pytest.fixture(scope="module")
def completed(batch_inputs, tmp_path_factory, settings):
    src, manifest = batch_inputs
    hashes = {p: sha256_file(p) for p in src.rglob("*") if p.is_file()}
    out = tmp_path_factory.mktemp("batch_out") / "run"
    summary = run_batch(opts(src, out, manifest), settings, quiet)
    return src, manifest, out, summary, hashes


# --------------------------------------------------------------------------- full run


def test_one_row_per_submission(completed):
    src, _, out, summary, _ = completed
    grades = sheet(out / WORKBOOK, "CA1 Grades")
    inputs = sorted(
        p.relative_to(src).as_posix() for p in src.rglob("*") if not p.name.startswith("~$")
    )
    inputs = [p for p in inputs if (src / p).is_file()]
    assert sorted(r["Relative Path"] for r in grades) == inputs
    assert summary.total == len(inputs) == len(grades)
    assert summary.state == "completed" and summary.failed == 0
    # the Word lock file is not a submission but is listed, not silently skipped
    assert any("~$Kiran" in name for name in summary.ignored)
    summary_sheet = {
        r[0]: r[1]
        for r in load_workbook(out / WORKBOOK)["Processing Summary"].iter_rows(values_only=True)
    }
    assert summary_sheet["Total input submissions"] == len(inputs)
    assert summary_sheet["Ignored system/lock files"] == 1


def test_invalid_documents_get_no_marks(completed):
    _, _, out, _, _ = completed
    grades = {r["Relative Path"]: r for r in sheet(out / WORKBOOK, "CA1 Grades")}
    review = sheet(out / WORKBOOK, "Manual Review")
    for rel in INVALID_OR_UNREADABLE:
        row = grades[rel]
        assert row["Grading Status"] == "not_graded"
        assert all(row[c] is None for c in (*SCORE_COLS, "Total /30", "ATS Score /100"))
        assert row["Manual Review Required"] == "Yes"
        categories = {r["Review Category"] for r in review if r["Relative Path"] == rel}
        assert categories & {"invalid", "unreadable", "not_a_resume"}
    assert grades["div_B/Broken_Resume.pdf"]["Validation Status"] == "invalid"
    unreadable = [r for r in review if r["Relative Path"] == "div_B/Scan_Resume.pdf"]
    assert unreadable[0]["Review Category"] == "unreadable"
    assert "IMAGE_ONLY" in unreadable[0]["Specific Reasons"]


def test_identity_mismatch_enters_review_queue(completed):
    _, _, out, _, _ = completed
    review = [
        r
        for r in sheet(out / WORKBOOK, "Manual Review")
        if r["Relative Path"] == "div_B/Priya_Desai_Resume.pdf"
    ]
    assert [r["Review Category"] for r in review] == ["identity_mismatch"]
    assert "Priya Desai" in review[0]["Specific Reasons"]
    row = next(
        r
        for r in sheet(out / WORKBOOK, "CA1 Grades")
        if r["Relative Path"] == "div_B/Priya_Desai_Resume.pdf"
    )
    # marks are kept (identity never changes marks) but are not releasable
    assert row["Total /30"] is not None and row["Releasable"] == "No"
    assert row["Grading Status"] == "needs_review"


def test_excel_totals_match_individual_scores(completed):
    _, _, out, _, _ = completed
    grades = sheet(out / WORKBOOK, "CA1 Grades")
    findings = sheet(out / WORKBOOK, "Detailed Findings")
    graded = [r for r in grades if r["Total /30"] is not None]
    assert len(graded) >= 6
    journal = Journal(out / JOURNAL)
    for row in graded:
        assert row["Total /30"] == pytest.approx(sum(row[c] for c in SCORE_COLS))
        key = make_key(row["Relative Path"])
        result = DocumentResult.model_validate_json(
            (out / journal.latest[key].result_file).read_text(encoding="utf-8")
        )
        assert row["Total /30"] == result.scoring.ca1.total
        own = [f for f in findings if f["Relative Path"] == row["Relative Path"]]
        assert len(own) == len(result.analysis.findings)
        assert row["Definite Defects"] == sum(f["Class"] == "definite_defect" for f in own)
        deducted = sum(f["Points Deducted"] for f in own)
        expected = sum(c.deductions for c in result.scoring.ca1.criteria)
        assert deducted == pytest.approx(expected, abs=0.01)
    # every finding row says whether it affected the score and how it was annotated
    assert {f["Affected Score"] for f in findings} <= {"Yes", "No"}
    assert all(f["Annotation Status"] for f in findings)


def test_duplicate_filenames_and_repeated_submissions(completed):
    _, _, out, _, _ = completed
    grades = {r["Relative Path"]: r for r in sheet(out / WORKBOOK, "CA1 Grades")}
    a, b = grades["div_A/resume.pdf"], grades["div_B/resume.pdf"]
    assert a["Student ID"] == "PPS003" and b["Student ID"] == "PPS009"
    assert a["Document SHA-256"] != b["Document SHA-256"]
    assert a["Annotated PDF"] != b["Annotated PDF"]
    original = grades["div_A/Asha_Example_Resume.pdf"]
    copy = grades["div_B/Asha_Example_Resume_copy.pdf"]
    assert original["Document SHA-256"] == copy["Document SHA-256"]
    for row in (original, copy):
        assert "[duplicate_submission]" in row["Review Reason"]
        assert row["Releasable"] == "No"


def test_outputs_and_originals(completed):
    src, _, out, _, hashes = completed
    assert {p: sha256_file(p) for p in hashes} == hashes  # inputs untouched
    assert not any(p.suffix in (".part", ".tmp") for p in out.rglob("*"))
    grades = sheet(out / WORKBOOK, "CA1 Grades")
    for row in grades:
        if row["Relative Path"].endswith(".docx") and row["Grading Status"] != "not_graded":
            assert row["Findings Report"] and (out / row["Findings Report"]).is_file()
            assert row["Annotated PDF"] is None  # DOCX: no claimed page annotations
        elif row["Annotated PDF"]:
            assert (out / row["Annotated PDF"]).is_file()
    status = {f["Annotation Status"] for f in sheet(out / WORKBOOK, "Detailed Findings")}
    assert "report_only" in status and "word" in status
    data = json.loads((out / "batch_summary.json").read_text(encoding="utf-8"))
    assert data["state"] == "completed" and data["total"] == len(grades)


def test_rerun_reuses_unchanged_files_without_duplicate_rows(completed, settings):
    src, manifest, out, first, _ = completed
    again = run_batch(opts(src, out, manifest), settings, quiet)
    assert again.processed == 0 and again.reused == first.total
    assert len(sheet(out / WORKBOOK, "CA1 Grades")) == first.total


# --------------------------------------------------------------------------- resume / errors


def test_interruption_and_restart(batch_inputs, tmp_path, settings, monkeypatch):
    src, manifest = batch_inputs
    out = tmp_path / "out"
    real = worker.process
    calls = {"n": 0}

    def flaky(job, *args):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 4:
            raise KeyboardInterrupt  # simulated Ctrl+C during the 4th document
        return real(job, *args)

    monkeypatch.setattr(worker, "process", flaky)
    first = run_batch(opts(src, out, manifest, timeout_s=0), settings, quiet)
    assert first.state == "interrupted" and first.processed == 3
    grades = sheet(out / WORKBOOK, "CA1 Grades")
    assert len(grades) == first.total  # unprocessed files still have a row
    assert sum(r["Processing Status"] == "not_processed" for r in grades) == first.total - 3
    journal = Journal(out / JOURNAL)
    states = [r.status for r in journal.latest.values()]
    assert states.count(ProcessingStatus.DONE) == 3
    assert states.count(ProcessingStatus.PROCESSING) == 1  # the interrupted document
    assert not (out / ".batch.lock").exists()

    monkeypatch.setattr(worker, "process", real)
    second = run_batch(opts(src, out, manifest, timeout_s=0), settings, quiet)
    assert second.state == "completed"
    assert second.reused == 3 and second.processed == first.total - 3
    grades = sheet(out / WORKBOOK, "CA1 Grades")
    assert len(grades) == first.total
    assert {r["Processing Status"] for r in grades} == {"processed", "reused"}


def test_torn_journal_line_is_ignored(batch_inputs, tmp_path, settings):
    src, manifest = batch_inputs
    out = tmp_path / "out"
    first = run_batch(opts(src, out, manifest, limit=2), settings, quiet)
    with (out / JOURNAL).open("a", encoding="utf-8") as handle:
        handle.write('{"key": "half-written')  # crash while appending
    second = run_batch(opts(src, out, manifest), settings, quiet)
    assert second.journal_unreadable_lines == 1
    assert second.reused == 2 and second.processed == first.total - 2


def test_failures_do_not_stop_the_batch_and_can_be_retried(
    batch_inputs, tmp_path, settings, monkeypatch
):
    src, manifest = batch_inputs
    out = tmp_path / "out"
    real_scan = worker.scan_file

    def broken(path, *args):  # type: ignore[no-untyped-def]
        if Path(path).name == "Kiran_Demo_Resume.docx":
            raise RuntimeError("parser crashed")
        return real_scan(path, *args)

    monkeypatch.setattr(worker, "scan_file", broken)
    first = run_batch(opts(src, out, manifest, timeout_s=0), settings, quiet)
    assert first.state == "completed" and first.failed == 1
    assert first.processed == first.total - 1
    row = next(
        r
        for r in sheet(out / WORKBOOK, "CA1 Grades")
        if r["Relative Path"].endswith("Kiran_Demo_Resume.docx")
    )
    assert row["Grading Status"] == "processing_failed" and row["Total /30"] is None
    review = sheet(out / WORKBOOK, "Manual Review")
    assert any(
        r["Review Category"] == "processing_failed" and "parser crashed" in r["Specific Reasons"]
        for r in review
    )

    monkeypatch.setattr(worker, "scan_file", real_scan)
    kept = run_batch(opts(src, out, manifest, timeout_s=0), settings, quiet)
    assert kept.failed == 1 and kept.failed_kept == 1 and kept.processed == 0
    retried = run_batch(opts(src, out, manifest, retry_failed=True, timeout_s=0), settings, quiet)
    assert retried.failed == 0 and retried.processed == 1
    assert Journal(out / JOURNAL).latest[make_key("div_B/Kiran_Demo_Resume.docx")].attempts == 2


def test_changed_file_is_processed_again(tmp_path, settings):
    src = tmp_path / "in"
    synthetic.resume_pdf(src / "a.pdf")
    synthetic.resume_pdf(src / "b.pdf")
    out = tmp_path / "out"
    run_batch(opts(src, out), settings, quiet)
    synthetic.resume_pdf(src / "b.pdf", extra_lines=[("CERTIFICATIONS", "heading")])
    again = run_batch(opts(src, out), settings, quiet)
    assert again.processed == 1 and again.reused == 1


def test_parallel_workers(tmp_path, settings):
    src = tmp_path / "in"
    for i in range(4):
        synthetic.resume_pdf(src / f"s{i}.pdf")
    synthetic.corrupted_pdf(src / "bad.pdf")
    summary = run_batch(opts(src, tmp_path / "out", workers=2), settings, quiet)
    assert summary.state == "completed" and summary.processed == 5
    assert summary.timing["workers"] == 2
    assert len(sheet(tmp_path / "out" / WORKBOOK, "CA1 Grades")) == 5


# --------------------------------------------------------------------------- safety


def test_unsafe_output_locations_are_refused(tmp_path, settings):
    src = tmp_path / "in"
    synthetic.resume_pdf(src / "a.pdf")
    with pytest.raises(BatchError, match="inside the input"):
        run_batch(opts(src, src / "out"), settings, quiet)
    with pytest.raises(BatchError, match="cloud-synced"):
        run_batch(opts(src, tmp_path / "OneDrive - College" / "out"), settings, quiet)
    ok = run_batch(
        opts(src, tmp_path / "OneDrive - College" / "out", allow_synced_output=True),
        settings,
        quiet,
    )
    assert ok.processed == 1


def test_concurrent_run_is_refused(tmp_path, settings):
    src = tmp_path / "in"
    synthetic.resume_pdf(src / "a.pdf")
    out = tmp_path / "out"
    out.mkdir()
    (out / ".batch.lock").write_text("pid=1", encoding="utf-8")
    with pytest.raises(BatchError, match="another batch run"):
        run_batch(opts(src, out), settings, quiet)
    assert run_batch(opts(src, out, break_lock=True), settings, quiet).processed == 1


def test_resume_text_is_never_written_as_a_formula(tmp_path, settings):
    writer = WorkbookWriter(settings)
    writer.add(Entry('=HYPERLINK("x")', "=cmd|' /C calc'!A0", "k1", "not_processed"))
    path = writer.save(tmp_path / "w.xlsx", {"run_id": "t"})
    ws = load_workbook(path)["CA1 Grades"]
    cell = ws.cell(row=2, column=4)
    assert cell.value == "=cmd|' /C calc'!A0" and cell.data_type == "s"
    with pytest.raises(ValueError):
        writer.add(Entry("x", "x", "k1", "not_processed"))  # never two rows per submission


def test_cli_batch(batch_inputs, tmp_path):
    src, manifest = batch_inputs
    out = tmp_path / "cli_out"
    assert (
        main(["batch", str(src), "-o", str(out), "--manifest", str(manifest), "--limit", "2"]) == 0
    )
    assert len(sheet(out / WORKBOOK, "CA1 Grades")) == len(
        [p for p in src.rglob("*") if p.is_file() and not p.name.startswith("~$")]
    )
    assert main(["batch", str(src), "-o", str(src / "x")]) == 2
    single = tmp_path / "single.pdf"
    shutil.copyfile(src / "div_A" / "Meera_Testcase_Resume.pdf", single)
    assert main(["annotate", str(single), "-o", str(tmp_path / "single.annotated.pdf")]) == 0
    assert (tmp_path / "single.annotated.pdf").is_file()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file locking")
def test_workbook_open_elsewhere_falls_back_to_run_copy(completed, settings):
    src, manifest, out, _, _ = completed
    with (out / WORKBOOK).open("rb"):  # e.g. the workbook is open in Excel
        summary = run_batch(opts(src, out, manifest, report_only=True), settings, quiet)
    assert summary.workbook != WORKBOOK and (out / summary.workbook).is_file()
    assert summary.processed == 0


def test_module_entry_point_with_worker_processes(tmp_path):
    """Spawned workers re-import ``pps_ats.__main__``; it must not re-run the CLI."""
    src = tmp_path / "in"
    for i in range(3):
        synthetic.resume_pdf(src / f"s{i}.pdf")
    out = tmp_path / "out"
    cmd = [sys.executable, "-m", "pps_ats", "batch", str(src), "-o", str(out), "--workers", "2"]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-500:]
    assert json.loads((out / "batch_summary.json").read_text(encoding="utf-8"))["processed"] == 3


def test_scanner_code_change_invalidates_reuse(tmp_path, settings, monkeypatch):
    from pps_ats.batch import runner

    src = tmp_path / "in"
    synthetic.resume_pdf(src / "a.pdf")
    out = tmp_path / "out"
    run_batch(opts(src, out, timeout_s=0), settings, quiet)
    assert run_batch(opts(src, out, timeout_s=0), settings, quiet).reused == 1
    monkeypatch.setattr(runner, "_CODE_FINGERPRINT", "a-different-scanner-build")
    again = run_batch(opts(src, out, timeout_s=0), settings, quiet)
    assert again.processed == 1 and again.reused == 0
