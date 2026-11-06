"""Scan every file in a folder: one JSON per file + summary.csv (no document text).

Usage:  python scripts/scan_folder.py INPUT_DIR --output outputs/run1 [--no-content]

This is a Milestone-1 smoke tool for reviewing validation on real submissions.
It is NOT the resumable batch processor (planned for a later milestone), but it skips
files whose JSON already exists unless --force is given.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from pps_ats.config import load_settings
from pps_ats.pipeline import scan_file

FIELDS = [
    "file_name",
    "detected_format",
    "status",
    "eligible_for_grading",
    "document_class",
    "confidence",
    "resume_score",
    "non_resume_score",
    "issue_codes",
    "required_sections_not_detected",
    "page_count",
    "word_count",
    "processing_ms",
    "sha256",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan a folder of PDF/DOCX files")
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("--output", type=Path, default=Path("outputs/scan"))
    parser.add_argument("--config", help="TOML override file")
    parser.add_argument("--no-content", action="store_true")
    parser.add_argument("--force", action="store_true", help="re-scan files already done")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    settings = load_settings(args.config)
    json_dir = args.output / "json"
    json_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in args.input_dir.rglob("*") if p.is_file())
    rows = []
    for index, path in enumerate(files, 1):
        rel = path.relative_to(args.input_dir)
        target = json_dir / (str(rel).replace("\\", "__").replace("/", "__") + ".json")
        if target.exists() and not args.force:
            continue
        result = scan_file(path, settings)
        exclude = {"content"} if args.no_content else None
        target.write_text(result.model_dump_json(indent=2, exclude=exclude), encoding="utf-8")
        v = result.validation
        rows.append(
            {
                "file_name": str(rel),
                "detected_format": result.document.detected_format.value,
                "status": v.status.value,
                "eligible_for_grading": v.eligible_for_grading,
                "document_class": v.classification.document_class.value,
                "confidence": v.classification.confidence,
                "resume_score": v.classification.resume_score,
                "non_resume_score": v.classification.non_resume_score,
                "issue_codes": ";".join(i.code for i in v.issues),
                "required_sections_not_detected": ";".join(
                    s.name for s in result.sections if s.required and s.status.value != "detected"
                ),
                "page_count": result.document.page_count,
                "word_count": result.content.stats.word_count if result.content else 0,
                "processing_ms": result.processing_ms,
                "sha256": result.document.sha256,
            }
        )
        print(f"[{index}/{len(files)}] {rel}: {v.status.value}", file=sys.stderr)

    summary = args.output / "summary.csv"
    write_header = not summary.exists() or args.force
    with summary.open("w" if write_header else "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)} file(s) scanned -> {summary}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
