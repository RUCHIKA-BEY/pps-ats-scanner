"""Command-line interface.

python -m pps_ats scan FILE [--output result.json] [--no-content]
python -m pps_ats inspect FILE            # metadata/validation/sections, no document text
python -m pps_ats analyze FILE [--output analysis.json] [--manifest students.csv]
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
    return parser


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
