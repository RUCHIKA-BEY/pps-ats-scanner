"""Milestone 5: per-document timeout, worker crashes, sleep/standby tolerance and retry."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from openpyxl import load_workbook

from pps_ats.batch import supervisor as sup
from pps_ats.batch.discovery import make_key
from pps_ats.batch.journal import Journal
from pps_ats.batch.runner import JOURNAL, WORKBOOK, BatchOptions, _KeepAwake, run_batch
from pps_ats.devtools import synthetic

FAULTS = "pps_ats.devtools.faults:process_with_faults"


def quiet(_: str) -> None:
    pass


def rows(path: Path, name: str) -> list[dict]:
    wb = load_workbook(path, read_only=True)
    data = list(wb[name].iter_rows(values_only=True))
    wb.close()
    width = len(data[0])
    return [dict(zip(data[0], (*r, *[None] * (width - len(r))), strict=True)) for r in data[1:]]


@pytest.fixture()
def folder(tmp_path: Path) -> Path:
    src = tmp_path / "in"
    for name in ("a.pdf", "HANG_b.pdf", "c.pdf"):
        synthetic.resume_pdf(src / name)
    return src


def test_stuck_document_times_out_and_batch_continues(folder, tmp_path, settings):
    out = tmp_path / "out"
    summary = run_batch(
        BatchOptions(folder, out, timeout_s=3, process_target=FAULTS), settings, quiet
    )
    assert summary.state == "completed" and summary.isolation == "process"
    assert summary.processed == 2 and summary.timed_out == 1
    record = Journal(out / JOURNAL).latest[make_key("HANG_b.pdf")]
    assert record.status.value == "timeout" and record.result_file is None
    assert record.error_type == "DocumentTimeout"
    # the stuck stack was captured (code locations only) before the worker was stopped
    trace = (out / record.diagnostics_file).read_text(encoding="utf-8")
    assert "process_with_faults" in trace
    grades = {r["Relative Path"]: r for r in rows(out / WORKBOOK, "CA1 Grades")}
    assert len(grades) == 3
    hung = grades["HANG_b.pdf"]
    assert hung["Grading Status"] == "processing_timeout" and hung["Total /30"] is None
    assert hung["Processing Status"] == "timeout"
    review = [
        r for r in rows(out / WORKBOOK, "Manual Review") if r["Relative Path"] == "HANG_b.pdf"
    ]
    assert review[0]["Review Category"] == "processing_timeout"
    assert not any(p.name.endswith((".tmp", ".part")) for p in out.rglob("*"))


def test_timed_out_document_is_kept_then_retried(folder, tmp_path, settings):
    out = tmp_path / "out"
    opts = dict(timeout_s=3, process_target=FAULTS)
    run_batch(BatchOptions(folder, out, **opts), settings, quiet)

    kept = run_batch(BatchOptions(folder, out, **opts), settings, quiet)
    assert kept.processed == 0 and kept.failed_kept == 1 and kept.reused == 2

    # retry with a normal worker (e.g. after fixing the cause or raising the timeout)
    retried = run_batch(BatchOptions(folder, out, timeout_s=30, retry_failed=True), settings, quiet)
    assert retried.processed == 1 and retried.timed_out == 0 and retried.failed == 0
    journal = Journal(out / JOURNAL)
    record = journal.latest[make_key("HANG_b.pdf")]
    assert record.status.value == "done" and record.attempts == 2
    assert journal.unreadable_lines == 0
    grades = rows(out / WORKBOOK, "CA1 Grades")
    assert len(grades) == 3 and all(r["Total /30"] is not None for r in grades)
    assert len({r["Relative Path"] for r in grades}) == 3


def test_worker_crash_is_recorded_and_replaced(tmp_path, settings):
    src = tmp_path / "in"
    for name in ("a.pdf", "DIE_b.pdf", "c.pdf"):
        synthetic.resume_pdf(src / name)
    out = tmp_path / "out"
    summary = run_batch(
        BatchOptions(src, out, timeout_s=30, process_target=FAULTS), settings, quiet
    )
    assert summary.processed == 2 and summary.worker_crashes == 1 and summary.failed == 1
    record = Journal(out / JOURNAL).latest[make_key("DIE_b.pdf")]
    assert record.status.value == "failed" and record.error_type == "WorkerCrashed"
    assert not (out / "diagnostics").exists() or not any((out / "diagnostics").iterdir())


def test_sleep_or_standby_is_not_charged_to_the_document(tmp_path, settings, monkeypatch):
    """Simulate the PC sleeping for 10 minutes while a 3-second document is processed."""
    src = tmp_path / "in"
    synthetic.resume_pdf(src / "SLOW_a.pdf")
    real = sup.time.monotonic
    state = {"calls": 0}

    def clock() -> float:
        state["calls"] += 1
        return real() + (600.0 if state["calls"] > 3 else 0.0)  # one 600 s jump

    monkeypatch.setattr(sup.time, "monotonic", clock)
    summary = run_batch(
        BatchOptions(src, tmp_path / "out", timeout_s=10, process_target=FAULTS), settings, quiet
    )
    assert summary.processed == 1 and summary.timed_out == 0
    assert any("suspension" in e for e in summary.events)
    assert summary.timing["suspended_s"] >= 600


def test_refuses_to_spawn_when_main_cannot_be_reimported(monkeypatch):
    fake = types.ModuleType("__main__")
    fake.__file__ = "<stdin>"
    monkeypatch.setitem(sys.modules, "__main__", fake)
    with pytest.raises(sup.WorkerStartError, match="re-import"):
        sup._check_spawnable()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows power API")
def test_keep_awake_request_is_released():
    awake = _KeepAwake(True)
    assert awake.active
    awake.release()
    assert not awake.active
    assert not _KeepAwake(False).active
