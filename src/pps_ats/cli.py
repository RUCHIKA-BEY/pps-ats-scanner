"""Command-line interface.

python -m pps_ats scan FILE [--output result.json] [--no-content]
python -m pps_ats inspect FILE            # metadata/validation/sections, no document text
python -m pps_ats analyze FILE [--output analysis.json] [--manifest students.csv]
python -m pps_ats score FILE [--output score.json] [--audit-log audit/ca1.jsonl]
python -m pps_ats review score.json --reviewer F01 --reason "..." --override content_relevance=7
python -m pps_ats annotate FILE --output annotated.pdf
python -m pps_ats batch INPUT_DIR --output PRIVATE_OUTPUT_DIR [--manifest m.csv] [--timeout 120]
python -m pps_ats import-corrections INTAKE --output DATASET
python -m pps_ats finalize-annotations DATASET
python -m pps_ats eval-split DATASET
python -m pps_ats evaluate DATASET --output PRIVATE_REPORT_DIR [--variant A|B]
python -m pps_ats benchmark-ai DATASET --output PRIVATE_REPORT_DIR
python -m pps_ats ai-check
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
    batch.add_argument(
        "--timeout",
        type=float,
        help="active seconds per document before its worker is stopped (0 = off)",
    )

    ev = sub.add_parser("evaluate", help="compare scanner output with faculty references")
    ev.add_argument("dataset", type=Path, help="folder with documents.csv [+ annotations.csv]")
    ev.add_argument("-o", "--output", type=Path, required=True, help="private report folder")
    ev.add_argument("--split", default="evaluation", choices=["evaluation", "calibration"])
    ev.add_argument("--variant", default="A", choices=["A", "B"], help="B = with AI")
    ev.add_argument("--documents", type=Path, help="root for document paths (default: dataset)")
    ev.add_argument("--allow-synced-output", action="store_true")

    bench = sub.add_parser("benchmark-ai", help="A (scanner) vs B (scanner + AI) on references")
    bench.add_argument("dataset", type=Path)
    bench.add_argument("-o", "--output", type=Path, required=True)
    bench.add_argument("--split", default="evaluation", choices=["evaluation", "calibration"])
    bench.add_argument("--documents", type=Path)
    bench.add_argument("--allow-synced-output", action="store_true")

    split = sub.add_parser("eval-split", help="assign and freeze calibration/evaluation splits")
    split.add_argument("dataset", type=Path)
    split.add_argument("--eval-fraction", type=float, default=0.3)
    split.add_argument("--seed", type=int, default=13)

    sub.add_parser(
        "ai-check", help="check the AI provider policy and test it on a FICTIONAL resume"
    )
    imp = sub.add_parser(
        "import-corrections", help="teacher-corrected PDFs -> reference dataset draft + review CSV"
    )
    imp.add_argument("intake", type=Path, help="folder with originals/, corrected/, marks.csv")
    imp.add_argument("-o", "--output", type=Path, required=True, help="dataset folder (private)")
    imp.add_argument("--force", action="store_true", help="re-import (old files kept as .bak)")
    imp.add_argument("--allow-synced-output", action="store_true")

    fin = sub.add_parser(
        "finalize-annotations", help="write annotations.csv from the reviewed candidates"
    )
    fin.add_argument("dataset", type=Path)
    fin.add_argument(
        "--allow-unresolved",
        action="store_true",
        help="leave out rows still awaiting a decision (they are counted, not guessed)",
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
        timeout_s=args.timeout,
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


def _private_output(path: Path, allow: bool, settings) -> str | None:  # type: ignore[no-untyped-def]
    from pps_ats.batch.runner import synced_location

    synced = synced_location(path, settings.batch.synced_markers)
    if synced and not allow:
        return (
            f"output folder is inside a cloud-synced location ({synced}); evaluation reports "
            "contain student text. Use a local folder or --allow-synced-output."
        )
    return None


def _run_evaluate(args: argparse.Namespace, settings) -> int:  # type: ignore[no-untyped-def]
    from pps_ats.evaluation.dataset import DatasetError, load_dataset
    from pps_ats.evaluation.runner import run_benchmark, run_evaluation
    from pps_ats.scoring.llm import AIConfigError

    problem = _private_output(args.output, args.allow_synced_output, settings)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    try:
        dataset = load_dataset(args.dataset, args.documents, set(settings.evaluation.labels))
        if args.command == "benchmark-ai":
            decision = run_benchmark(dataset, settings, args.output, args.split)
            print(
                f"benchmark: {decision['decision']} ({'; '.join(decision['reasons'])}) "
                f"-> {args.output / 'benchmark.md'}",
                file=sys.stderr,
            )
            return 0
        report = run_evaluation(dataset, settings, args.output, args.split, args.variant)
    except (DatasetError, AIConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"evaluation {report.variant} on {report.split}: {report.documents} documents, "
        f"total MAE={report.total_error.mae} (n={report.total_error.n}), "
        f"issue recall={report.issue_overall.recall}, validation errors="
        f"{len(report.validation_errors)} -> {args.output / 'evaluation.md'}",
        file=sys.stderr,
    )
    return 0


def _run_import(args: argparse.Namespace, settings) -> int:  # type: ignore[no-untyped-def]
    from pps_ats.evaluation.importer import ImportError_, import_corrections

    problem = _private_output(args.output, args.allow_synced_output, settings)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    try:
        s = import_corrections(args.intake, args.output, settings, force=args.force)
    except (ImportError_, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"imported {s.documents} document(s) ({s.with_corrections} with corrections): "
        f"{s.candidates} candidate(s), {s.auto_mapped} auto-mapped, {s.needs_review} need "
        f"review; faculty marks imported {s.marks_imported}, rejected {len(s.marks_rejected)}; "
        f"problems {len(s.problems)} -> {args.output / 'import_report.md'}",
        file=sys.stderr,
    )
    return 0


def _run_finalize(args: argparse.Namespace, settings) -> int:  # type: ignore[no-untyped-def]
    from pps_ats.evaluation.importer import finalize_annotations

    try:
        s = finalize_annotations(args.dataset, settings, args.allow_unresolved)
    except (OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for problem in s.invalid:
        print(f"invalid: {problem}", file=sys.stderr)
    if s.invalid or (s.unresolved and not args.allow_unresolved):
        print(
            f"not written: {len(s.unresolved)} row(s) await a reviewer_decision, "
            f"{len(s.invalid)} invalid row(s)",
            file=sys.stderr,
        )
        return 1
    print(
        f"annotations.csv: {s.written} accepted, {s.rejected} rejected, "
        f"{len(s.unresolved)} left out (undecided)",
        file=sys.stderr,
    )
    return 0


def _run_eval_split(args: argparse.Namespace) -> int:
    from pps_ats.evaluation.dataset import DatasetError, freeze_split

    try:
        counts = freeze_split(args.dataset, args.eval_fraction, args.seed)
    except (DatasetError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"split frozen: {counts} -> {args.dataset / 'split_lock.json'}", file=sys.stderr)
    return 0


def _run_ai_check(settings) -> int:  # type: ignore[no-untyped-def]
    """Policy check + one request with a fictional resume (never student data)."""
    from pps_ats.scoring.ai import AIEvaluationRequest, verify_response
    from pps_ats.scoring.llm import AIConfigError, OpenAICompatibleEvaluator

    cfg = settings.scoring.ai
    print(f"enabled={cfg.enabled} provider={cfg.provider} model={cfg.model!r}", file=sys.stderr)
    try:
        evaluator = OpenAICompatibleEvaluator(cfg)
    except AIConfigError as exc:
        print(f"policy: REFUSED - {exc}", file=sys.stderr)
        return 2
    print(f"policy: OK ({evaluator.endpoint})", file=sys.stderr)
    request = AIEvaluationRequest(
        rubric_version=settings.scoring.rubric_version,
        sections={
            "summary": "Seeking a job in a good company where I can grow my skills.",
            "projects": "Library App - made an app for library. Used Java.",
        },
        deterministic_scores={c: 8.0 for c in settings.scoring.criteria},
        finding_rules=["CQ_PROJECT_VAGUE"],
    )
    try:
        response = evaluator.evaluate(request)
    except RuntimeError as exc:
        print(f"request: FAILED - {exc}", file=sys.stderr)
        return 1
    kept, observations, rejected = verify_response(
        response, request, set(settings.scoring.criteria), cfg.min_evidence_chars
    )
    print(
        f"request: OK model={response.model_id} proposals={len(response.proposals)} "
        f"(evidence-backed {len(kept)}) observations={len(response.observations)} "
        f"(evidence-backed {len(observations)}) rejected={len(rejected)}",
        file=sys.stderr,
    )
    for reason in rejected:
        print(f"  rejected: {reason}", file=sys.stderr)
    return 0


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
        if args.command in ("evaluate", "benchmark-ai"):
            return _run_evaluate(args, settings)
        if args.command == "eval-split":
            return _run_eval_split(args)
        if args.command == "ai-check":
            return _run_ai_check(settings)
        if args.command == "import-corrections":
            return _run_import(args, settings)
        if args.command == "finalize-annotations":
            return _run_finalize(args, settings)
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
