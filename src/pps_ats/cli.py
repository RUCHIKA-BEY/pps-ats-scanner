"""Command-line interface.

python -m pps_ats scan FILE [--output result.json] [--no-content]
python -m pps_ats inspect FILE            # metadata/validation/sections, no document text
python -m pps_ats analyze FILE [--output analysis.json] [--manifest students.csv]
python -m pps_ats score FILE [--output score.json] [--audit-log audit/ca1.jsonl]
python -m pps_ats review score.json --reviewer F01 --reason "..." --override content_relevance=7
python -m pps_ats annotate FILE --output annotated.pdf
python -m pps_ats batch INPUT_DIR --output PRIVATE_OUTPUT_DIR [--manifest m.csv] [--workers 2]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from pps_ats import __version__
from pps_ats.config import load_settings
from pps_ats.pipeline import scan_file
from pps_ats.schemas.result import ScanResult


def _summary(result: ScanResult) -> dict:
    """Metadata-only view: no extracted text is included."""
    content = result.content
    return {
        "schema_version": result.schema_version,
        "document": result.document.model_dump(mode="json"),
        "validation": {
            "status": result.validation.status.value,
            "eligible_for_grading": result.validation.eligible_for_grading,
            "document_class": result.validation.classification.document_class.value,
            "classification_confidence": result.validation.classification.confidence,
            "resume_score": result.validation.classification.resume_score,
            "non_resume_score": result.validation.classification.non_resume_score,
            "issues": [
                {"code": i.code, "severity": i.severity.value, "page": i.page}
                for i in result.validation.issues
            ],
        },
        "sections": {s.name: s.status.value for s in result.sections},
        "extraction": None
        if content is None
        else {
            "parser": content.diagnostics.parser,
            "coordinates_available": content.diagnostics.coordinates_available,
            "pages": [
                {
                    "number": p.number,
                    "size_pt": [p.width_pt, p.height_pt],
                    "chars": p.char_count,
                    "images": p.image_count,
                    "image_coverage": p.image_coverage_ratio,
                    "columns": p.layout.column_count,
                }
                for p in content.pages
            ],
            "docx_sections": len(content.docx_sections),
            "blocks": len(content.blocks),
            "tables": len(content.tables),
            "images": len(content.images),
            "stats": content.stats.model_dump(mode="json"),
            "warnings": content.diagnostics.warnings,
        },
        "errors": [e.model_dump(mode="json") for e in result.errors],
        "processing_ms": result.processing_ms,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pps_ats", description="PPS ATS resume scanner")
    parser.add_argument("--version", action="version", version=f"pps_ats {__version__}")
    parser.add_argument("--config", help="TOML file overriding config/default.toml")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="validate + parse a PDF/DOCX and write full JSON")
    scan.add_argument("file", type=Path)
    scan.add_argument("-o", "--output", type=Path, help="JSON output path (default: stdout)")
    scan.add_argument(
        "--no-content",
        action="store_true",
        help="omit extracted content (blocks/spans/text) from the JSON",
    )
    scan.add_argument("--compact", action="store_true", help="no indentation")

    inspect = sub.add_parser("inspect", help="print metadata/validation summary (no text)")
    inspect.add_argument("file", type=Path)

    analyze = sub.add_parser(
        "analyze", help="validate + parse + run the deterministic findings analysis"
    )
    analyze.add_argument("file", type=Path)
    analyze.add_argument("-o", "--output", type=Path, help="JSON output path (default: stdout)")
    analyze.add_argument(
        "--manifest",
        type=Path,
        help="optional CSV (file_name, student_name, student_id, email) used as the "
        "authoritative identity reference",
    )
    analyze.add_argument("--compact", action="store_true", help="no indentation")

    score = sub.add_parser(
        "score", help="analyse + provisional CA1 rubric marks and ATS score (no AI by default)"
    )
    score.add_argument("file", type=Path)
    score.add_argument("-o", "--output", type=Path, help="scoring JSON (default: stdout)")
    score.add_argument("--analysis-output", type=Path, help="also write the analysis JSON")
    score.add_argument("--manifest", type=Path, help="optional identity manifest CSV")
    score.add_argument("--audit-log", type=Path, help="append the scoring event to this JSONL")

    review = sub.add_parser(
        "review", help="faculty override / approval / manual grade of a scoring JSON"
    )
    review.add_argument("score_json", type=Path)
    review.add_argument("--reviewer", required=True, help="faculty id")
    review.add_argument("--reason", required=True, help="justification (stored in the audit)")
    action = review.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--override",
        action="append",
        metavar="CRITERION=SCORE",
        help="e.g. content_relevance=7 (repeatable)",
    )
    action.add_argument("--approve", action="store_true", help="accept the provisional marks")
    action.add_argument(
        "--manual", metavar="C=S,C=S,C=S", help="grade an ungraded document: all 3 criteria"
    )
    review.add_argument("-o", "--output", type=Path, help="updated JSON (default: in place)")
    review.add_argument("--audit-log", type=Path, help="append review events to this JSONL")

    annotate = sub.add_parser(
        "annotate", help="annotated PDF copy (PDF) or findings report (DOCX) for one file"
    )
    annotate.add_argument("file", type=Path)
    annotate.add_argument("-o", "--output", type=Path, required=True, help="output PDF path")
    annotate.add_argument("--manifest", type=Path, help="optional identity manifest CSV")

    batch = sub.add_parser(
        "batch", help="resumable batch: results JSON, annotated PDFs, Excel workbook"
    )
    batch.add_argument("input_dir", type=Path)
    batch.add_argument("-o", "--output", type=Path, required=True, help="private output folder")
    batch.add_argument("--manifest", type=Path, help="identity manifest CSV (student IDs)")
    batch.add_argument("--workers", type=int, help="parallel processes (bounded by config)")
    batch.add_argument("--retry-failed", action="store_true", help="re-run failed files")
    batch.add_argument("--force", action="store_true", help="re-process unchanged files too")
    batch.add_argument("--limit", type=int, help="process at most N files in this run")
    batch.add_argument(
        "--report-only", action="store_true", help="rebuild the workbook; process nothing"
    )
    batch.add_argument(
        "--allow-synced-output",
        action="store_true",
        help="allow an output folder inside OneDrive/Dropbox/... (not recommended)",
    )
    batch.add_argument(
        "--break-lock", action="store_true", help="remove a stale lock left by a killed run"
    )
    return parser


def _pairs(values: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for value in values:
        name, sep, number = value.partition("=")
        if not sep:
            raise ValueError(f"expected CRITERION=SCORE, got '{value}'")
        out[name.strip()] = float(number)
    return out


def _run_score(args: argparse.Namespace, settings) -> int:  # type: ignore[no-untyped-def]
    from pps_ats.analysis.identity import load_manifest
    from pps_ats.scoring.engine import score_file
    from pps_ats.scoring.review import AuditLog

    manifest = load_manifest(args.manifest) if args.manifest else None
    analysis, result = score_file(args.file, settings, manifest)
    if args.analysis_output:
        args.analysis_output.parent.mkdir(parents=True, exist_ok=True)
        args.analysis_output.write_text(analysis.model_dump_json(indent=2), encoding="utf-8")
    if args.audit_log:
        log = AuditLog(args.audit_log)
        for entry in result.audit:
            log.append(result, entry)
    payload = result.model_dump_json(indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        marks = (
            " ".join(f"{c.criterion}={c.final_score:g}" for c in result.ca1.criteria)
            + f" total={result.ca1.total:g}/{result.ca1.max_total:g}"
            if result.ca1
            else "no marks"
        )
        ats = f"{result.ats.score:g}" if result.ats else "n/a"
        print(
            f"{result.file_name}: status={result.status.value} {marks} ats={ats} "
            f"releasable={result.releasable} -> {args.output}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(payload + "\n")
    return 0


def _run_review(args: argparse.Namespace, settings) -> int:  # type: ignore[no-untyped-def]
    from pps_ats.schemas.scoring import ScoringResult
    from pps_ats.scoring import review as rv

    result = ScoringResult.model_validate_json(args.score_json.read_text(encoding="utf-8"))
    before = len(result.audit)
    cfg = settings.scoring
    try:
        if args.approve:
            result = rv.approve(result, args.reviewer, args.reason, cfg)
        elif args.manual:
            result = rv.manual_grade(
                result, _pairs(args.manual.split(",")), args.reviewer, args.reason, cfg
            )
        else:
            for criterion, value in _pairs(args.override).items():
                result = rv.apply_override(
                    result, criterion, value, args.reviewer, args.reason, cfg
                )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.audit_log:
        log = rv.AuditLog(args.audit_log)
        for entry in result.audit[before:]:
            log.append(result, entry)
    target = args.output or args.score_json
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    total = f"{result.ca1.total:g}" if result.ca1 else "n/a"
    print(
        f"{result.file_name}: status={result.status.value} total={total} -> {target}",
        file=sys.stderr,
    )
    return 0


def _run_annotate(args: argparse.Namespace, settings) -> int:  # type: ignore[no-untyped-def]
    from pps_ats.analysis.identity import load_manifest
    from pps_ats.reports.annotate import annotate_pdf
    from pps_ats.reports.findings_report import write_findings_report
    from pps_ats.schemas.common import FileFormat
    from pps_ats.scoring.engine import score_file

    manifest = load_manifest(args.manifest) if args.manifest else None
    analysis, scoring = score_file(args.file, settings, manifest)
    if analysis.analysis_status == "skipped":
        print(f"{args.file.name}: not analysed ({analysis.skipped_reason})", file=sys.stderr)
        return 1
    if analysis.document.detected_format is FileFormat.PDF:
        report = annotate_pdf(args.file, analysis, args.output, scoring, settings.reports)
        print(
            f"{args.file.name}: annotated copy -> {args.output} {report.counts} "
            f"original_unchanged={report.source_unchanged}",
            file=sys.stderr,
        )
    else:
        write_findings_report(analysis, scoring, args.output)
        print(f"{args.file.name}: findings report -> {args.output}", file=sys.stderr)
    return 0


def _run_batch(args: argparse.Namespace, settings) -> int:  # type: ignore[no-untyped-def]
    from pps_ats.batch.runner import BatchError, BatchOptions, run_batch

    opts = BatchOptions(
        input_dir=args.input_dir,
        output_dir=args.output,
        manifest=args.manifest,
        workers=args.workers,
        retry_failed=args.retry_failed,
        force=args.force,
        limit=args.limit,
        allow_synced_output=args.allow_synced_output,
        report_only=args.report_only,
        break_lock=args.break_lock,
    )
    try:
        summary = run_batch(opts, settings)
    except BatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"run {summary.run_id} {summary.state}: total={summary.total} "
        f"processed={summary.processed} reused={summary.reused} failed={summary.failed} "
        f"not_processed={summary.not_processed} -> {args.output / (summary.workbook or '')}",
        file=sys.stderr,
    )
    return 130 if summary.state == "interrupted" else 0


def _run_analyze(args: argparse.Namespace, settings) -> int:  # type: ignore[no-untyped-def]
    from pps_ats.analysis.identity import load_manifest
    from pps_ats.analysis.orchestrator import analyze_file

    manifest = None
    if args.manifest:
        try:
            manifest = load_manifest(args.manifest)
        except (OSError, ValueError) as exc:
            print(f"error: manifest: {exc}", file=sys.stderr)
            return 2
    try:
        result = analyze_file(args.file, settings, manifest)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    payload = result.model_dump_json(indent=None if args.compact else 2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        counts = result.summary.by_class
        print(
            f"{result.document.file_name}: status={result.validation.status.value} "
            f"analysis={result.analysis_status} findings={result.summary.total} "
            f"(defects={counts.get('definite_defect', 0)}, "
            f"potential={counts.get('potential_issue', 0)}, "
            f"recommendations={counts.get('recommendation', 0)}) "
            f"review={result.review_required} -> {args.output}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(payload + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        settings = load_settings(args.config)
        if args.command == "analyze":
            return _run_analyze(args, settings)
        if args.command == "score":
            return _run_score(args, settings)
        if args.command == "review":
            return _run_review(args, settings)
        if args.command == "annotate":
            return _run_annotate(args, settings)
        if args.command == "batch":
            return _run_batch(args, settings)
        result = scan_file(args.file, settings)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "inspect":
        print(json.dumps(_summary(result), indent=2, ensure_ascii=False))
        return 0

    exclude = {"content"} if args.no_content else None
    payload = result.model_dump_json(indent=None if args.compact else 2, exclude=exclude)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(
            f"{result.document.file_name}: status={result.validation.status.value} "
            f"class={result.validation.classification.document_class.value} "
            f"eligible={result.validation.eligible_for_grading} -> {args.output}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(payload + "\n")
    return 0
