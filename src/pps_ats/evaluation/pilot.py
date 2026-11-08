"""Partial calibration pilot from a partly reviewed reference workbook.

Uses only CALIBRATION documents whose every correction row has a valid, completed
reviewer decision (accept/reject; accepted rows with final_label, faculty_intent and
reviewer_id) and whose scanner input is clean. Everything else - partially reviewed or
unreviewed documents, ineligible (image-only / needs-review) documents and every held-out
evaluation document - is excluded and counted.

Nothing in the reference dataset is modified: the master workbook, split lock and
documents are only read (their hashes are checked before and after). The pilot writes a
separate dataset and report to its own output folder.

Metrics are issue-detection recall only, by default:

* faculty corrections are split into *detectable errors* (``correction_required`` with a
  label the scanner has rules for), *recommendations*, *outside scanner scope* (labels
  with no rule) and *intent unclear*; only detectable errors enter recall;
* scanner findings no faculty annotation explains are reported as *unverified*, never as
  false positives, because the faculty were not asked to mark every issue; precision and
  F1 are therefore not computed;
* there are no faculty marks, so no score agreement is computed.
"""

from __future__ import annotations

import csv
import json
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pps_ats import __version__
from pps_ats.config import Settings
from pps_ats.evaluation.compare import EvaluationReport
from pps_ats.evaluation.dataset import SPLIT_LOCK, load_dataset
from pps_ats.evaluation.importer import _write_csv
from pps_ats.evaluation.reference import (
    ANNOTATION_FIELDS,
    CANDIDATES,
    REVIEW_BOOK,
    REVIEW_DIR,
    ReferenceError,
    _check_split,
    _decision,
    _rows,
    read_review,
    sha256,
)
from pps_ats.evaluation.report import write_report
from pps_ats.evaluation.runner import evaluate
from pps_ats.evaluation.topics import topic_of

DOC_FIELDS = [
    "document_id", "file", "split", "expected_validation", "marks_source",
    "faculty_content_relevance", "faculty_structure_formatting",
    "faculty_language_professional_tone", "faculty_total", "annotations_complete",
    "grader_id", "notes", "scanner_input_status",
]  # fmt: skip


@dataclass
class DocumentReview:
    document_id: str
    split: str
    eligible: bool  # scanner input is clean
    rows: int
    status: str  # complete | partial | unreviewed | no_corrections
    counts: dict = field(default_factory=dict)


def assess(ref: Path, settings: Settings, workbook: Path | None = None) -> list[DocumentReview]:
    """Per-document review completeness (counts only)."""
    ref = Path(ref)
    rows = read_review(ref, workbook)
    docs = _rows(ref / "documents.csv")
    per_doc: Counter[str] = Counter(c["document_id"] for c in _rows(ref / CANDIDATES))
    out = []
    for d in docs:
        doc_id, split = d["document_id"], d["split"]
        counts, _ = _check_split(
            ref, settings, split, [r for r in rows if r["split"] == split], {doc_id}
        )
        decided = counts["confirmed"] + counts["rejected"]
        if not per_doc[doc_id]:
            status = "no_corrections"  # nothing to review: faculty coverage unknown
        elif counts["ready_to_finalize"]:
            status = "complete"
        elif decided or counts["unclear"] or counts["invalid"]:
            status = "partial"
        else:
            status = "unreviewed"
        out.append(
            DocumentReview(
                document_id=doc_id,
                split=split,
                eligible=d.get("scanner_input_status", "clean") == "clean",
                rows=per_doc[doc_id],
                status=status,
                counts=counts,
            )
        )
    return out


def _group(row: dict, out_of_scope: set[str]) -> str:
    intent = row.get("intent") or ""
    if intent == "recommendation":
        return "recommendation"
    if intent == "unclear":
        return "intent_unclear"
    if row["label"] in out_of_scope:
        return "outside_scanner_scope"
    return "detectable_error"


def pilot_metrics(report: EvaluationReport, settings: Settings) -> dict:
    out_of_scope = {k for k, v in settings.evaluation.labels.items() if not v}
    faculty = [r for r in report.issue_rows if r["outcome"] in ("tp", "fn")]
    scanner_only = [r for r in report.issue_rows if r["outcome"] in ("fp", "fp_unverified")]
    groups: dict[str, dict] = {}
    for name in ("detectable_error", "recommendation", "outside_scanner_scope", "intent_unclear"):
        rows = [r for r in faculty if _group(r, out_of_scope) == name]
        tp = sum(r["outcome"] == "tp" for r in rows)
        groups[name] = {
            "faculty_annotations": len(rows),
            "matched_by_scanner": tp,
            "missed_by_scanner": len(rows) - tp,
            "recall": round(tp / len(rows), 3) if rows and name == "detectable_error" else None,
        }
    groups["outside_scanner_scope"]["note"] = "no scanner rule exists: not counted as misses"
    groups["recommendation"]["note"] = "optional advice: not an ATS defect, excluded from recall"
    per_label = {}
    for label in sorted(
        {r["label"] for r in faculty if _group(r, out_of_scope) == "detectable_error"}
    ):
        rows = [
            r for r in faculty
            if r["label"] == label and _group(r, out_of_scope) == "detectable_error"
        ]  # fmt: skip
        tp = sum(r["outcome"] == "tp" for r in rows)
        per_label[label] = {"annotations": len(rows), "matched": tp,
                            "recall": round(tp / len(rows), 3)}  # fmt: skip
    detectable = groups["detectable_error"]
    return {
        "detectable_error_recall": detectable["recall"],
        "detectable_error_annotations": detectable["faculty_annotations"],
        "groups": groups,
        "per_label_recall": per_label,
        "scanner_findings_unexplained": len(scanner_only),
        "scanner_findings_unexplained_by_label": dict(
            Counter(r["label"] for r in scanner_only).most_common()
        ),
        "precision": None,
        "f1": None,
        "precision_note": (
            "not computed: faculty annotation completeness is not established, so scanner "
            "findings without a faculty annotation are unverified, not false positives"
        ),
        "marks_agreement": None,
        "marks_note": "not computed: no genuine faculty marks exist",
        "matching_note": (
            "faculty comments carry no quoted resume text, so a match means the same label "
            "on the same page (location-level), not the same words"
        ),
    }


def run_partial_pilot(
    ref: Path, output: Path, settings: Settings, workbook: Path | None = None
) -> dict:
    """``workbook``: read this saved copy (e.g. the backup an earlier pilot used) instead of
    the master workbook, so a comparison can be repeated on the exact same review input."""
    ref, output = Path(ref), Path(output)
    if output.exists() and any(output.iterdir()):
        raise ReferenceError(f"{output} is not empty; a pilot is never overwritten")
    if output.resolve() == ref.resolve() or ref.resolve() in output.resolve().parents:
        raise ReferenceError("the pilot folder must be outside the reference folder")
    book = Path(workbook) if workbook else ref / REVIEW_DIR / REVIEW_BOOK
    protected = [book, ref / SPLIT_LOCK, ref / "documents.csv", ref / CANDIDATES]
    before = {p.name: sha256(p) for p in protected}

    reviews = assess(ref, settings, book)
    included = [
        d for d in reviews if d.split == "calibration" and d.status == "complete" and d.eligible
    ]
    if not included:
        raise ReferenceError("no calibration document is completely reviewed yet")
    keep = {d.document_id for d in included}

    # ---- partial dataset (copies only; the reference is not touched)
    data = output / "dataset"
    (data / "files").mkdir(parents=True)
    docs = {d["document_id"]: d for d in _rows(ref / "documents.csv")}
    cands = {c["candidate_id"]: c for c in _rows(ref / CANDIDATES)}
    doc_rows = []
    for doc_id in sorted(keep):
        d = docs[doc_id]
        shutil.copyfile(ref / d["file"], data / "files" / f"{doc_id}.pdf")
        doc_rows.append(
            {
                **{k: d.get(k, "") for k in DOC_FIELDS},
                "file": f"files/{doc_id}.pdf",
                "split": "calibration",
                "marks_source": "",
                "faculty_content_relevance": "",
                "faculty_structure_formatting": "",
                "faculty_language_professional_tone": "",
                "faculty_total": "",
                "annotations_complete": "no",  # completeness of faculty marking not established
                "notes": "partial calibration pilot",
            }
        )
    annotations = []
    review_rows = [r for r in read_review(ref, book) if r["split"] == "calibration"]
    for r in review_rows:
        if r["document_id"] not in keep or _decision(r["decision"]) != "accept":
            continue
        c = cands[r["candidate_id"]]
        annotations.append(
            {
                "document_id": r["document_id"],
                "label": r["final_label"].strip().lower(),
                "severity": (r.get("final_severity") or "medium").strip().lower(),
                "text": c.get("quoted_text", ""),
                "page": c.get("page", ""),
                "criterion": "",
                "source": "faculty",
                "annotator_id": c.get("author", ""),
                "comment": "",  # faculty text stays in the master workbook
                "intent": r["faculty_intent"].strip().lower(),
                "reviewer_id": r["reviewer_id"],
                "section": c.get("associated_section", ""),
                "topics": ",".join(sorted(topic_of(c.get("teacher_comment", "")))),
            }
        )
    _write_csv(data / "documents.csv", DOC_FIELDS, doc_rows)
    _write_csv(data / "annotations.csv", ANNOTATION_FIELDS, annotations)
    (data / "split_lock.json").write_text(
        json.dumps({"splits": {d: "calibration" for d in keep}, "note": "pilot subset"}, indent=2),
        encoding="utf-8",
    )

    # ---- evaluate (deterministic scanner only; AI settings untouched and unused)
    dataset = load_dataset(data, labels=set(settings.evaluation.labels))
    report = evaluate(dataset, settings, "calibration", "A")
    report.warnings.insert(
        0,
        f"PARTIAL CALIBRATION PILOT: {len(keep)} completely reviewed calibration documents; "
        "in-sample, not a held-out estimate.",
    )
    write_report(report, output / "report")
    metrics = pilot_metrics(report, settings)

    after = {p.name: sha256(p) for p in protected}
    if after != before:
        raise ReferenceError("a reference file changed during the pilot")  # should never happen
    status_counts = Counter((d.split, d.status, d.eligible) for d in reviews)
    cal = [d for d in reviews if d.split == "calibration"]
    summary = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "engine_version": __version__,
        "reference": str(ref),
        "master_workbook_sha256": before[book.name],
        "workbook_file": book.name,
        "reference_files_unchanged": True,
        "split_unchanged": True,
        "calibration_documents": len(cal),
        "usable_documents": len(keep),
        "excluded": {
            "calibration_partially_reviewed": sum(1 for d in cal if d.status == "partial"),
            "calibration_unreviewed": sum(1 for d in cal if d.status == "unreviewed"),
            "calibration_without_corrections": sum(1 for d in cal if d.status == "no_corrections"),
            "calibration_complete_but_ineligible": sum(
                1 for d in cal if d.status == "complete" and not d.eligible
            ),
            "evaluation_documents_never_used": sum(1 for d in reviews if d.split == "evaluation"),
            "evaluation_rows_already_decided": sum(
                d.counts["confirmed"] + d.counts["rejected"]
                for d in reviews
                if d.split == "evaluation"
            ),
        },
        "rows_in_usable_documents": sum(d.rows for d in included),
        "verified_annotations": len(annotations),
        "rejected_rows_in_usable_documents": sum(d.counts["rejected"] for d in included),
        "annotations_by_intent": dict(Counter(a["intent"] for a in annotations)),
        "annotations_by_label": dict(Counter(a["label"] for a in annotations).most_common()),
        "excluded_row_states_calibration": {
            k: sum(d.counts[k] for d in cal if d.document_id not in keep)
            for k in ("confirmed", "rejected", "unclear", "unreviewed", "invalid", "missing")
        },
        "metrics": metrics,
        "status_breakdown": {
            f"{s}/{st}/{'eligible' if e else 'ineligible'}": n
            for (s, st, e), n in sorted(status_counts.items())
        },  # fmt: skip
    }
    (output / "pilot_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "pilot_summary.md").write_text(_markdown(summary), encoding="utf-8")
    with (output / "included_documents.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["document_id", "rows", "accepted", "rejected"])
        for d in included:
            writer.writerow([d.document_id, d.rows, d.counts["confirmed"], d.counts["rejected"]])
    return summary


def _markdown(s: dict) -> str:
    m = s["metrics"]
    ex = s["excluded"]
    lines = [
        "# Partial calibration pilot",
        "",
        "In-sample pilot on completely reviewed calibration documents only. Not a held-out "
        "estimate; no faculty marks, so no score agreement.",
        "",
        f"- Usable documents: **{s['usable_documents']}** of {s['calibration_documents']} "
        "calibration documents",
        f"- Excluded: {ex['calibration_partially_reviewed']} partially reviewed, "
        f"{ex['calibration_unreviewed']} unreviewed, "
        f"{ex['calibration_complete_but_ineligible']} complete but ineligible (image-only / "
        f"needs review); {ex['evaluation_documents_never_used']} held-out documents never used",
        f"- Verified faculty annotations: **{s['verified_annotations']}** "
        f"(rejected rows in usable documents: {s['rejected_rows_in_usable_documents']})",
        f"- By intent: {s['annotations_by_intent']}",
        "",
        "## Issue detection",
        "",
        "| group | faculty annotations | matched by scanner | missed | recall |",
        "|---|---|---|---|---|",
    ]
    for name, g in m["groups"].items():
        recall = "–" if g["recall"] is None else f"{g['recall']:.3f}"
        lines.append(
            f"| {name} | {g['faculty_annotations']} | {g['matched_by_scanner']} | "
            f"{g['missed_by_scanner']} | {recall} |"
        )
    lines += ["", "Recall per label (detectable errors only):", "",
              "| label | annotations | matched | recall |", "|---|---|---|---|"]  # fmt: skip
    for label, v in m["per_label_recall"].items():
        lines.append(f"| {label} | {v['annotations']} | {v['matched']} | {v['recall']:.3f} |")
    lines += [
        "",
        f"Scanner findings without a faculty annotation: {m['scanner_findings_unexplained']} "
        "(unverified, NOT false positives).",
        "",
        f"- Precision / F1: {m['precision_note']}",
        f"- Marks: {m['marks_note']}",
        f"- Matching: {m['matching_note']}",
        "",
        f"Reference files unchanged: {s['reference_files_unchanged']} "
        f"(master workbook sha256 {s['master_workbook_sha256'][:16]}…)",
    ]
    return "\n".join(lines) + "\n"
