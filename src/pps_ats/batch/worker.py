"""Process one submission: scan -> analyse -> score -> annotate -> results JSON.

Runs in the main process (``workers = 1``) or in a worker process. The input file is only
read. Every exception is converted into a ``failed`` outcome so the batch continues.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from pps_ats.analysis.context import ManifestRecord
from pps_ats.analysis.orchestrator import analyze_scan
from pps_ats.batch.journal import write_json_atomic
from pps_ats.config import Settings
from pps_ats.pipeline import scan_file
from pps_ats.reports.annotate import annotate_pdf, report_only
from pps_ats.reports.findings_report import write_findings_report
from pps_ats.schemas.batch import AnnotationReport, AnnotationStatus, DocumentResult
from pps_ats.schemas.common import FileFormat
from pps_ats.scoring.engine import score_analysis

log = logging.getLogger(__name__)

RESULTS_DIR = "results"
ANNOTATED_DIR = "annotated"
REPORTS_DIR = "findings_reports"


@dataclass(frozen=True)
class Job:
    path: str
    rel_path: str
    key: str
    output_dir: str


@dataclass
class Outcome:
    key: str
    ok: bool
    sha256: str | None = None
    result_file: str | None = None
    student_id: str | None = None
    student_id_source: str = "none"
    error_type: str | None = None
    error_message: str | None = None
    duration_ms: int = 0
    timed_out: bool = False
    diagnostics: str | None = None  # stall traceback (code locations only)


def manifest_record(
    manifest: dict[str, ManifestRecord] | None, rel_path: str, file_name: str
) -> ManifestRecord | None:
    """Match the relative path first (unique), then the bare file name."""
    if not manifest:
        return None
    return manifest.get(rel_path.casefold()) or manifest.get(file_name.casefold())


def _annotate(job: Job, analysis, scoring, settings: Settings, out: Path) -> AnnotationReport:  # type: ignore[no-untyped-def]
    cfg = settings.reports
    fmt = analysis.document.detected_format
    if analysis.analysis_status == "skipped":
        return AnnotationReport(kind="none")
    if fmt is FileFormat.PDF and cfg.annotate_pdf:
        rel = f"{ANNOTATED_DIR}/{job.key}.annotated.pdf"
        try:
            report = annotate_pdf(job.path, analysis, out / rel, scoring, cfg)
            report.output_file = rel
        except Exception as exc:  # annotation is an output; it never blocks the marks
            log.warning("annotation failed doc=%s error=%s", job.key[-10:], type(exc).__name__)
            report = report_only(analysis, AnnotationStatus.NOT_ANNOTATED)
            report.error = f"{type(exc).__name__}: {str(exc)[:200]}"
    elif fmt is FileFormat.DOCX:
        report = report_only(analysis, AnnotationStatus.REPORT_ONLY)
    else:
        report = report_only(analysis, AnnotationStatus.NOT_ANNOTATED)
    want = cfg.findings_report == "all" or (
        cfg.findings_report == "docx" and fmt is FileFormat.DOCX
    )
    if want:
        rel = f"{REPORTS_DIR}/{job.key}.findings.pdf"
        try:
            write_findings_report(analysis, scoring, out / rel, report, job.rel_path)
            report.findings_report_file = rel
            if report.kind == "none":
                report.kind = "findings_report"
        except Exception as exc:
            report.error = f"findings report: {type(exc).__name__}: {str(exc)[:200]}"
    return report


def process(job: Job, settings: Settings, manifest: dict[str, ManifestRecord] | None) -> Outcome:
    started = time.perf_counter()
    out = Path(job.output_dir)
    try:
        timings: dict[str, int] = {}
        t = time.perf_counter()
        scan = scan_file(job.path, settings)
        timings["scan"] = int((time.perf_counter() - t) * 1000)
        record = manifest_record(manifest, job.rel_path, Path(job.path).name)
        t = time.perf_counter()
        analysis = analyze_scan(scan, settings, record)
        scoring = score_analysis(analysis, settings, scan=scan, evaluator=_evaluator(settings))
        timings["analyse_score"] = int((time.perf_counter() - t) * 1000)
        t = time.perf_counter()
        annotation = _annotate(job, analysis, scoring, settings, out)
        timings["annotate"] = int((time.perf_counter() - t) * 1000)

        student_id, source = None, "none"
        if record and record.student_id:
            student_id, source = record.student_id, "manifest"
        elif analysis.identity and analysis.identity.extracted.student_ids:
            student_id, source = analysis.identity.extracted.student_ids[0], "document"
        result = DocumentResult(
            key=job.key,
            rel_path=job.rel_path,
            file_name=Path(job.path).name,
            student_id=student_id,
            student_id_source=source,
            analysis=analysis,
            scoring=scoring,
            annotation=annotation,
            timings_ms=timings,
        )
        rel = f"{RESULTS_DIR}/{job.key}.json"
        write_json_atomic(out / rel, result.model_dump_json())
        return Outcome(
            key=job.key,
            ok=True,
            sha256=analysis.document.sha256,
            result_file=rel,
            student_id=student_id,
            student_id_source=source,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except Exception as exc:
        log.warning("document failed doc=%s error=%s", job.key[-10:], type(exc).__name__)
        return Outcome(
            key=job.key,
            ok=False,
            error_type=type(exc).__name__,
            error_message=str(exc)[:300],
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


_EVALUATORS: dict[str, object] = {}


def _evaluator(settings: Settings):  # type: ignore[no-untyped-def]
    """The optional AI evaluator (``None`` unless [scoring.ai] enables one), built once."""
    cfg = settings.scoring.ai
    if not cfg.enabled or cfg.provider == "none":
        return None
    key = cfg.model_dump_json()
    if key not in _EVALUATORS:
        from pps_ats.scoring.llm import build_evaluator

        _EVALUATORS[key] = build_evaluator(cfg)
    return _EVALUATORS[key]


# ---------------------------------------------------------------- worker-process plumbing

_STATE: dict[str, object] = {}


def init_worker(settings_json: str, manifest: dict[str, ManifestRecord] | None) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    _STATE["settings"] = Settings.model_validate_json(settings_json)
    _STATE["manifest"] = manifest


def process_in_worker(job: Job) -> Outcome:
    return process(job, _STATE["settings"], _STATE["manifest"])  # type: ignore[arg-type]
