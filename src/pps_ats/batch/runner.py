"""Resumable batch processing of a folder of submissions (Milestone 4).

Output folder layout::

    manifest.jsonl              processing journal (hash, status, attempts, outputs)
    results/<key>.json          analysis + scoring + annotation outcome per submission
    annotated/<key>.annotated.pdf
    findings_reports/<key>.findings.pdf
    ca1_results.xlsx            consolidated workbook (rebuilt at the end of every run)
    batch_summary.json          counts and timings of the latest run
    runs/<run_id>.json          history of runs

Every discovered file gets exactly one workbook row. Unchanged files whose result exists
for the same settings are reused, not processed again. Input files are only read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pps_ats import __version__
from pps_ats.analysis.context import ManifestRecord
from pps_ats.analysis.identity import load_manifest
from pps_ats.batch import worker
from pps_ats.batch.discovery import Submission, discover
from pps_ats.batch.journal import Journal, write_json_atomic
from pps_ats.batch.supervisor import (
    DEFAULT_TARGET,
    Supervisor,
    WorkerStartError,
    resolve_target,
)
from pps_ats.config import Settings
from pps_ats.reports.annotate import sha256_file
from pps_ats.reports.excel import Entry, WorkbookWriter
from pps_ats.schemas.batch import (
    BATCH_SCHEMA_VERSION,
    DocumentResult,
    ProcessingStatus,
    SubmissionRecord,
)

log = logging.getLogger(__name__)

WORKBOOK = "ca1_results.xlsx"
JOURNAL = "manifest.jsonl"
LOCK = ".batch.lock"


class BatchError(RuntimeError):
    pass


@dataclass
class BatchOptions:
    input_dir: Path
    output_dir: Path
    manifest: Path | None = None
    workers: int | None = None
    retry_failed: bool = False
    force: bool = False
    limit: int | None = None  # process at most N files this run (others stay "not processed")
    allow_synced_output: bool = False
    report_only: bool = False  # rebuild the workbook from existing results, process nothing
    break_lock: bool = False
    timeout_s: float | None = None  # overrides [batch] document_timeout_s
    process_target: str = DEFAULT_TARGET  # "module:function" run for each document


@dataclass
class BatchSummary:
    run_id: str
    started_at: str
    finished_at: str | None = None
    state: str = "running"  # "completed" | "interrupted"
    input_dir: str = ""
    output_dir: str = ""
    fingerprint: str = ""
    total: int = 0
    processed: int = 0
    reused: int = 0
    failed: int = 0
    failed_kept: int = 0  # failed/timed out earlier, not retried in this run
    timed_out: int = 0  # stopped by the per-document timeout in this run
    worker_crashes: int = 0
    isolation: str = "inline"  # "inline" | "process"
    document_timeout_s: float = 0.0
    events: list[str] = field(default_factory=list)  # sleep/standby, timeouts, crashes
    not_processed: int = 0
    ignored: list[str] = field(default_factory=list)
    workbook: str | None = None
    timing: dict[str, Any] = field(default_factory=dict)
    journal_unreadable_lines: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def synced_location(path: Path, markers: list[str]) -> str | None:
    """Name of the cloud-sync folder that contains ``path``, if any."""
    resolved = str(path.resolve()).casefold()
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        root = os.environ.get(var)
        if root and _within(path.resolve(), Path(root).resolve()):
            return var
    return next((m for m in markers if m.casefold() in resolved), None)


def check_paths(opts: BatchOptions, settings: Settings) -> None:
    src, out = opts.input_dir.resolve(), opts.output_dir.resolve()
    if not src.is_dir():
        raise BatchError(f"input folder not found: {opts.input_dir}")
    if _within(out, src) or _within(src, out):
        raise BatchError("the output folder must not be inside the input folder (or vice versa)")
    synced = synced_location(out, settings.batch.synced_markers)
    if synced and not opts.allow_synced_output:
        raise BatchError(
            f"output folder is inside a cloud-synced location ({synced}). Private student "
            "results should go to an approved local folder; pass --allow-synced-output to "
            "override deliberately."
        )


def fingerprint(settings: Settings, manifest_path: Path | None) -> str:
    """Results are reused only when this matches: engine + settings + manifest."""
    digest = hashlib.sha256()
    digest.update(f"{__version__}|{BATCH_SCHEMA_VERSION}|".encode())
    digest.update(settings.model_dump_json(exclude={"source", "batch", "evaluation"}).encode())
    if manifest_path is not None:
        digest.update(sha256_file(manifest_path).encode())
    return digest.hexdigest()[:16]


class _Lock:
    def __init__(self, path: Path, break_lock: bool) -> None:
        self.path = path
        if break_lock and path.exists():
            path.unlink()
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise BatchError(
                f"another batch run may be using this output folder ({path.name} exists). "
                "If no other run is active, re-run with --break-lock."
            ) from exc
        with os.fdopen(fd, "w") as handle:
            handle.write(f"pid={os.getpid()} started={now_iso()}\n")

    def release(self) -> None:
        if self.path.exists():
            self.path.unlink()


def _load_result(out: Path, record: SubmissionRecord | None) -> DocumentResult | None:
    if record is None or record.result_file is None:
        return None
    path = out / record.result_file
    if not path.is_file():
        return None
    try:
        return DocumentResult.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def batch_flags(
    subs: list[Submission], records: dict[str, SubmissionRecord]
) -> dict[str, list[tuple[str, str]]]:
    """Duplicate files and several files for one student (from the journal only)."""
    flags: dict[str, list[tuple[str, str]]] = defaultdict(list)
    by_hash: dict[str, list[Submission]] = defaultdict(list)
    by_student: dict[str, list[Submission]] = defaultdict(list)
    for sub in subs:
        rec = records.get(sub.key)
        if rec is None or rec.status is not ProcessingStatus.DONE:
            continue
        if rec.sha256:
            by_hash[rec.sha256].append(sub)
        if rec.student_id and rec.student_id_source == "manifest":
            by_student[rec.student_id.casefold()].append(sub)
    for group in by_hash.values():
        if len(group) > 1:
            for sub in group:
                others = ", ".join(o.rel_path for o in group if o is not sub)
                flags[sub.key].append(("duplicate_submission", f"identical content to: {others}"))
    for group in by_student.values():
        if len(group) > 1:
            for sub in group:
                flags[sub.key].append(
                    ("multiple_submissions", f"{len(group)} files for this student ID")
                )
    return flags


def _progress_line(done: int, total: int, counts: dict[str, int], started: float) -> str:
    elapsed = time.perf_counter() - started
    rate = counts["processed"] / elapsed if elapsed > 0 and counts["processed"] else 0.0
    remaining = counts["queued"] - counts["processed"] - counts["failed"] - counts["timeout"]
    eta = f"{remaining / rate / 60:.1f} min" if rate else "-"
    return (
        f"[{done:>{len(str(total))}}/{total}] processed={counts['processed']} "
        f"reused={counts['reused']} failed={counts['failed']} timeout={counts['timeout']} "
        f"| {rate:.2f} docs/s | eta {eta}"
    )


def run_batch(
    opts: BatchOptions,
    settings: Settings,
    progress: Callable[[str], None] | None = None,
) -> BatchSummary:
    progress = progress or (lambda line: print(line, file=sys.stderr, flush=True))
    check_paths(opts, settings)
    out = opts.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
    summary = BatchSummary(
        run_id=run_id,
        started_at=now_iso(),
        input_dir=str(opts.input_dir.resolve()),
        output_dir=str(out),
    )
    lock = _Lock(out / LOCK, opts.break_lock)
    t_start = time.perf_counter()
    try:
        manifest: dict[str, ManifestRecord] | None = (
            load_manifest(opts.manifest) if opts.manifest else None
        )
        if manifest is not None:  # also allow lookups by relative path
            manifest = {k.replace("\\", "/"): v for k, v in manifest.items()}
        fp = fingerprint(settings, opts.manifest)
        summary.fingerprint = fp
        journal = Journal(out / JOURNAL)
        summary.journal_unreadable_lines = journal.unreadable_lines
        subs, ignored = discover(opts.input_dir, settings.batch)
        summary.total, summary.ignored = len(subs), ignored
        progress(f"discovered {len(subs)} submission(s); {len(ignored)} system file(s) ignored")

        # ---- plan: hash every file; reuse unchanged results; keep failures unless retrying.
        t = time.perf_counter()
        hashes = {s.key: sha256_file(s.path) for s in subs}
        summary.timing["hashing_s"] = round(time.perf_counter() - t, 3)
        queue: list[Submission] = []
        status: dict[str, str] = {}
        for sub in subs:
            prev = journal.latest.get(sub.key)
            same = prev is not None and prev.sha256 == hashes[sub.key] and prev.fingerprint == fp
            if opts.report_only:
                status[sub.key] = (
                    "reused"
                    if prev and prev.status is ProcessingStatus.DONE
                    else (
                        _FAILED_STATES.get(prev.status, "not_processed")
                        if prev
                        else "not_processed"
                    )
                )
            elif (
                not opts.force
                and same
                and prev.status is ProcessingStatus.DONE
                and (_load_result(out, prev) is not None)
            ):
                status[sub.key] = "reused"
            elif (
                not opts.force
                and same
                and prev.status in _FAILED_STATES
                and (not opts.retry_failed)
            ):
                status[sub.key] = _FAILED_STATES[prev.status]
                summary.failed_kept += 1
            else:
                queue.append(sub)
                status[sub.key] = "not_processed"
        if opts.limit is not None:
            queue = queue[: max(0, opts.limit)]
        summary.reused = sum(1 for v in status.values() if v == "reused")
        if summary.failed_kept:
            progress(
                f"{summary.failed_kept} file(s) failed in an earlier run and are kept as failed; "
                "use --retry-failed to process them again"
            )

        # ---- process.
        counts = {
            "processed": 0,
            "failed": 0,
            "timeout": 0,
            "reused": summary.reused,
            "queued": len(queue),
        }
        workers = max(1, min(opts.workers or settings.batch.workers, settings.batch.max_workers))
        if settings.scoring.ai.enabled and workers > 1:
            progress("AI evaluation is enabled: using 1 worker (one model at a time)")
            workers = 1
        timeout = settings.batch.document_timeout_s if opts.timeout_s is None else opts.timeout_s
        if timeout > 0 and settings.scoring.ai.enabled and settings.scoring.ai.provider != "none":
            timeout += settings.scoring.ai.request_timeout_s  # allow one model call per document
        summary.document_timeout_s = timeout
        summary.isolation = "process" if (timeout > 0 or workers > 1) else "inline"
        _sweep_partials(out)
        awake = _KeepAwake(settings.batch.prevent_sleep)
        t = time.perf_counter()
        try:
            _process_queue(
                queue,
                hashes,
                journal,
                out,
                fp,
                run_id,
                settings,
                manifest,
                workers,
                status,
                counts,
                progress,
                len(subs) - len(queue),
                timeout,
                opts.process_target,
                summary,
            )
            summary.state = "completed"
        except KeyboardInterrupt:
            summary.state = "interrupted"
            progress("interrupted: completed documents are saved; run the same command to resume")
        finally:
            awake.release()
        elapsed = time.perf_counter() - t
        summary.processed, summary.timed_out = counts["processed"], counts["timeout"]
        summary.failed = counts["failed"] + counts["timeout"] + summary.failed_kept
        summary.timing.update(
            {
                "workers": workers,
                "processing_s": round(elapsed, 3),
                "isolation": summary.isolation,
                "documents_processed": _attempted(counts),
                "seconds_per_document": round(elapsed / _attempted(counts), 3)
                if _attempted(counts)
                else None,
            }
        )

        # ---- workbook: one row per discovered submission.
        t = time.perf_counter()
        flags = batch_flags(subs, journal.latest)
        writer = WorkbookWriter(settings)
        for sub in subs:
            rec = journal.latest.get(sub.key)
            state = status[sub.key]
            result = _load_result(out, rec) if state in ("reused", "processed") else None
            if state in ("reused", "processed") and result is None:
                state = "not_processed"
            writer.add(
                Entry(
                    rel_path=sub.rel_path,
                    file_name=sub.file_name,
                    key=sub.key,
                    processing_status=state,
                    record=rec,
                    result=result,
                    batch_flags=flags.get(sub.key, []),
                )
            )
        summary.not_processed = sum(1 for v in status.values() if v == "not_processed")
        summary.finished_at = now_iso()
        summary.timing["workbook_s"] = round(time.perf_counter() - t, 3)
        summary.timing["total_s"] = round(time.perf_counter() - t_start, 3)
        workbook = writer.save(out / WORKBOOK, summary.as_dict())
        summary.workbook = workbook.name
        journal.compact()
        write_json_atomic(out / "batch_summary.json", summary.as_dict())
        write_json_atomic(out / "runs" / f"{run_id}.json", summary.as_dict())
        return summary
    finally:
        lock.release()


def _attempted(counts: dict[str, int]) -> int:
    return counts["processed"] + counts["failed"] + counts["timeout"]


_FAILED_STATES = {ProcessingStatus.FAILED: "failed", ProcessingStatus.TIMEOUT: "timeout"}
_PARTIAL_DIRS = (worker.RESULTS_DIR, worker.ANNOTATED_DIR, worker.REPORTS_DIR)


def _sweep_partials(out: Path) -> None:
    """Remove half-written files left by a stopped worker (only our own temp names)."""
    for name in _PARTIAL_DIRS:
        folder = out / name
        if folder.is_dir():
            for path in folder.iterdir():
                if path.is_file() and path.name.endswith((".tmp", ".part")):
                    path.unlink()


class _KeepAwake:
    """Windows: ask the OS not to idle-sleep while the batch runs. Closing a laptop lid
    still suspends the PC; the timeout does not charge that time to a document."""

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __init__(self, enabled: bool) -> None:
        self.active = False
        if enabled and sys.platform == "win32":
            import ctypes

            self.kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
            flags = self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED
            self.active = bool(self.kernel.SetThreadExecutionState(flags))

    def release(self) -> None:
        if self.active:
            self.kernel.SetThreadExecutionState(self.ES_CONTINUOUS)
            self.active = False


def _process_queue(
    queue: list[Submission],
    hashes: dict[str, str],
    journal: Journal,
    out: Path,
    fp: str,
    run_id: str,
    settings: Settings,
    manifest: dict[str, ManifestRecord] | None,
    workers: int,
    status: dict[str, str],
    counts: dict[str, int],
    progress: Callable[[str], None],
    already: int,
    timeout: float,
    target: str,
    summary: BatchSummary,
) -> None:
    total = already + len(queue)
    started = time.perf_counter()
    begun: dict[str, SubmissionRecord] = {}

    def start(sub: Submission) -> worker.Job:
        prev = journal.latest.get(sub.key)
        record = SubmissionRecord(
            key=sub.key,
            rel_path=sub.rel_path,
            file_name=sub.file_name,
            size_bytes=sub.size_bytes,
            mtime_ns=sub.mtime_ns,
            sha256=hashes[sub.key],
            fingerprint=fp,
            status=ProcessingStatus.PROCESSING,
            attempts=(prev.attempts if prev else 0) + 1,
            run_id=run_id,
            started_at=now_iso(),
        )
        journal.append(record)
        begun[sub.key] = record
        return worker.Job(str(sub.path), sub.rel_path, sub.key, str(out))

    def finish(outcome: worker.Outcome) -> None:
        record = begun.pop(outcome.key)
        if outcome.ok:
            state, key = ProcessingStatus.DONE, "processed"
        elif outcome.timed_out:
            state, key = ProcessingStatus.TIMEOUT, "timeout"
        else:
            state, key = ProcessingStatus.FAILED, "failed"
        update = {
            "status": state,
            "finished_at": now_iso(),
            "duration_ms": outcome.duration_ms,
            "result_file": outcome.result_file if outcome.ok else None,
            "student_id": outcome.student_id,
            "student_id_source": outcome.student_id_source,
            "error_type": outcome.error_type,
            "error_message": outcome.error_message,
            "diagnostics_file": outcome.diagnostics,
        }
        if outcome.sha256:
            update["sha256"] = outcome.sha256
        journal.append(record.model_copy(update=update))
        if not outcome.ok:  # a stopped worker may have left half-written outputs
            _sweep_partials(out)
        counts[key] += 1
        status[outcome.key] = key
        progress(_progress_line(already + _attempted(counts), total, counts, started))

    if timeout <= 0 and workers == 1:  # no isolation: documents run in this process
        process = worker.process if target == DEFAULT_TARGET else resolve_target(target)
        for sub in queue:
            job = start(sub)
            finish(process(job, settings, manifest))
        return

    supervisor = Supervisor(
        workers,
        settings,
        manifest,
        timeout,
        out,
        target=target,
        suspend_gap_s=settings.batch.suspend_gap_s,
    )
    try:
        supervisor.run(iter(queue), start, finish)
    except WorkerStartError as exc:
        raise BatchError(str(exc)) from exc
    finally:
        st = supervisor.stats
        summary.worker_crashes += st.crashes
        summary.events += st.events
        if st.suspended_s:
            summary.timing["suspended_s"] = round(st.suspended_s, 1)
        for event in st.events:
            progress(event)


def load_summary(output_dir: Path) -> dict[str, Any]:
    return json.loads((output_dir / "batch_summary.json").read_text(encoding="utf-8"))
