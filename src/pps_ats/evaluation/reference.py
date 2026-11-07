"""Freeze a faculty reference dataset and run its annotation review.

Workflow (all files local, outside Git and OneDrive)::

    reference-prepare  SOURCE_DATASET -o REFERENCE --key-dir KEY [--salvage-log LOG]
        pseudonymous copy (P001...), integrity checks, duplicate/identity grouping
    eval-split REFERENCE --eval-count 28 --stratify-by scanner_input_status --group-by group_id
    review-sheet REFERENCE            -> review/annotation_review.xlsx (one sheet per split)
    review-status REFERENCE           -> counts only (no text)
    finalize-review REFERENCE --split calibration
    freeze-calibration REFERENCE      -> records the calibration-time configuration
    evaluate REFERENCE --split calibration     (allowed after calibration review)
    finalize-review REFERENCE --split evaluation  (after independent review)
    evaluate REFERENCE                 (held-out; refused until both conditions hold)

Nothing here changes the scanner or the scoring weights. Suggested labels are never
accepted without an explicit reviewer decision.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pymupdf
from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from pps_ats import __version__
from pps_ats.config import Settings
from pps_ats.evaluation.importer import CANDIDATES, _write_csv

MANIFEST = "reference_manifest.json"
REVIEW_DIR = "review"
REVIEW_BOOK = "annotation_review.xlsx"
REVIEW_STATE = "review_state.json"
CAL_FREEZE = "calibration_freeze.json"
DECISIONS = ("accept", "reject", "unsure")
INTENTS = ("correction_required", "recommendation", "unclear")
SEVERITIES = ("high", "medium", "low")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?:\+?\d[\d\s-]{8,}\d)")
YEAR_RANGE_RE = re.compile(r"^(19|20)\d\d\s*[-–]?\s*(19|20)\d\d$")


def phone_numbers(text: str) -> set[str]:
    """Phone-like values with 10-13 digits (last 10 kept); year ranges are excluded."""
    out = set()
    for match in PHONE_RE.findall(text):
        digits = re.sub(r"\D", "", match)
        if 10 <= len(digits) <= 13 and not YEAR_RANGE_RE.match(match.strip()):
            out.add(digits[-10:])
    return out


ANNOTATION_FIELDS = [
    "document_id", "label", "severity", "text", "page", "criterion", "source",
    "annotator_id", "comment", "intent", "reviewer_id",
]  # fmt: skip
REVIEW_COLUMNS = [
    ("candidate_id", 12),
    ("document_id", 9),
    ("page", 5),
    ("faculty_correction", 50),
    ("associated_section", 14),
    ("suggested_label", 18),
    ("alternative_labels", 22),
    ("suggestion_reason", 28),
    ("source_type", 20),
    ("faculty_likelihood", 14),
    ("decision", 10),
    ("final_label", 20),
    ("faculty_intent", 18),
    ("final_severity", 10),
    ("reviewer_id", 10),
    ("reviewer_notes", 40),
    ("resume_context", 40),
]
EDITABLE = {"decision", "final_label", "faculty_intent", "final_severity", "reviewer_id",
            "reviewer_notes"}  # fmt: skip


class ReferenceError(ValueError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _pdf_text(path: Path) -> str:
    with pymupdf.open(path) as doc:
        return " ".join(page.get_text() for page in doc)


# ---------------------------------------------------------------- integrity


@dataclass
class IntegrityReport:
    documents: int = 0
    salvage_log_matches: int | None = None
    salvage_log_complete_entries: int | None = None
    corrected_copies_identical: int = 0
    clean_inputs: int = 0
    clean_without_annotations: int = 0
    clean_red_text_is_student_only: int = 0  # red left = red in the PDF minus annotations
    clean_student_text_identical: int = 0
    long_comments_checked: int = 0
    long_comments_found_in_clean_input: int = 0
    short_comments_not_checkable: int = 0
    ineligible: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)  # pseudonymous ids only


def _red_chars(doc: pymupdf.Document, settings: Settings) -> str:
    """All red characters (whitespace removed) in reading order."""
    from pps_ats.evaluation.corrected_only import _red

    return "".join(
        "".join(span["text"].split())
        for page in doc
        for block in page.get_text("dict")["blocks"]
        for line in block.get("lines", [])
        for span in line["spans"]
        if _red(span["color"], settings)
    )


def verify_reference(ref: Path, settings: Settings) -> IntegrityReport:
    """Re-check a prepared reference dataset (pseudonymous ids in the report)."""
    from pps_ats.evaluation.corrected_only import FACULTY_ANNOTS, _non_red_chars

    ref = Path(ref)
    manifest = json.loads((ref / MANIFEST).read_text(encoding="utf-8"))
    docs = _rows(ref / "documents.csv")
    cands = _rows(ref / CANDIDATES)
    report = IntegrityReport(documents=len(docs))
    report.salvage_log_matches = manifest.get("salvage_log_matches")
    report.salvage_log_complete_entries = manifest.get("salvage_log_complete_entries")
    expected = manifest["documents"]
    comments: dict[str, list[str]] = {}
    for c in cands:
        comments.setdefault(c["document_id"], []).append(c["teacher_comment"])
    for row in docs:
        pid, status = row["document_id"], row.get("scanner_input_status", "")
        corrected = ref / "corrected" / f"{pid}.pdf"
        if sha256(corrected) == expected[pid]["sha256"]:
            report.corrected_copies_identical += 1
        else:
            report.failures.append(f"{pid}: corrected copy hash differs from the recovered file")
        if status != "clean":
            report.ineligible[status] = report.ineligible.get(status, 0) + 1
            continue
        report.clean_inputs += 1
        clean = ref / row["file"]
        with pymupdf.open(clean) as doc:
            annots = sum(1 for p in doc for a in p.annots() if a.type[1] in FACULTY_ANNOTS)
            red = _red_chars(doc, settings)
            clean_chars = _non_red_chars(doc, settings)
        with pymupdf.open(corrected) as doc:
            for page in doc:
                for a in list(page.annots()):
                    if a.type[1] in FACULTY_ANNOTS:
                        page.delete_annot(a)
            source_chars = _non_red_chars(doc, settings)
            source_red = _red_chars(doc, settings)
        report.clean_without_annotations += annots == 0
        report.clean_red_text_is_student_only += red == source_red
        report.clean_student_text_identical += clean_chars == source_chars
        if annots or red != source_red or clean_chars != source_chars:
            report.failures.append(f"{pid}: clean input check failed")
        text = _norm(_pdf_text(clean))
        for comment in comments.get(pid, []):
            c = _norm(comment)
            if len(c.split()) >= 4 and len(c) >= 15:
                report.long_comments_checked += 1
                if c in text:
                    report.long_comments_found_in_clean_input += 1
                    report.failures.append(f"{pid}: a faculty comment is still in the clean input")
            elif c:
                report.short_comments_not_checkable += 1
    return report


# ---------------------------------------------------------------- duplicates


def _shingles(text: str, k: int = 5) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.casefold())
    return {" ".join(words[i : i + k]) for i in range(max(0, len(words) - k + 1))}


def duplicate_groups(
    texts: dict[str, str], hashes: dict[str, str], threshold: float = 0.6
) -> tuple[dict[str, str], dict]:
    """Group documents that are identical files, near-identical text (5-word shingle
    Jaccard >= threshold) or share an e-mail/phone (same student). Returns
    (document -> group id, statistics without any personal data)."""
    ids = sorted(texts)
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    stats = {"identical_files": 0, "near_duplicate_pairs": 0, "shared_contact_pairs": 0,
             "max_similarity_other_pairs": 0.0}  # fmt: skip
    by_hash: dict[str, list[str]] = {}
    for i in ids:
        by_hash.setdefault(hashes[i], []).append(i)
    for group in by_hash.values():
        for other in group[1:]:
            stats["identical_files"] += 1
            union(group[0], other)
    sh = {i: _shingles(texts[i]) for i in ids}
    contacts = {}
    for i in ids:
        found = {e.casefold() for e in EMAIL_RE.findall(texts[i])}
        found |= phone_numbers(texts[i])
        contacts[i] = {hashlib.sha256(v.encode()).hexdigest() for v in found}
    for n, a in enumerate(ids):
        for b in ids[n + 1 :]:
            if sh[a] and sh[b]:
                sim = len(sh[a] & sh[b]) / len(sh[a] | sh[b])
                if sim >= threshold:
                    stats["near_duplicate_pairs"] += 1
                    union(a, b)
                else:
                    stats["max_similarity_other_pairs"] = max(
                        stats["max_similarity_other_pairs"], round(sim, 3)
                    )
            if contacts[a] & contacts[b]:
                stats["shared_contact_pairs"] += 1
                union(a, b)
    groups = {i: f"G-{find(i)}" for i in ids}
    stats["groups_with_more_than_one_document"] = sum(
        1 for v in Counter(groups.values()).values() if v > 1
    )
    return groups, stats


# ---------------------------------------------------------------- prepare


def prepare_reference(
    source: Path,
    ref: Path,
    key_dir: Path,
    settings: Settings,
    salvage_log: Path | None = None,
    intake: Path | None = None,
    duplicate_threshold: float = 0.6,
) -> dict:
    """Copy a corrected-only dataset to ``ref`` with pseudonymous ids.

    The id key (pseudonym -> original id, file name, hash) goes to ``key_dir`` only,
    which must be a different folder so the reference can be shared for review without it.
    """
    source, ref, key_dir = Path(source), Path(ref), Path(key_dir)
    if ref.exists() and any(ref.iterdir()):
        raise ReferenceError(f"{ref} is not empty; a frozen reference is never overwritten")
    if key_dir.resolve() == ref.resolve() or ref.resolve() in key_dir.resolve().parents:
        raise ReferenceError("the key folder must be outside the reference folder")
    docs = _rows(source / "documents.csv")
    cands = _rows(source / CANDIDATES)
    report = json.loads((source / "import_report.json").read_text(encoding="utf-8"))
    per_doc = {d["document_id"]: d for d in report["per_document"]}
    hashes = {
        d["document_id"]: sha256(source / "corrected" / f"{d['document_id']}.pdf") for d in docs
    }
    for doc_id, digest in hashes.items():
        if per_doc[doc_id].get("sha256") != digest:
            raise ReferenceError("a corrected copy differs from the hash recorded at import")
    # pseudonyms: P001.. ordered by file hash (independent of names and file order)
    order = sorted(hashes, key=lambda d: (hashes[d], d))
    pseudo = {doc_id: f"P{n:03d}" for n, doc_id in enumerate(order, 1)}
    authors = sorted({c["author"] for c in cands if c.get("author")})
    annotator = {a: f"A{n}" for n, a in enumerate(authors, 1)}
    # salvage log cross-check (by hash)
    salvage_matches = salvage_complete = None
    intake_names: dict[str, str] = {}  # source document id -> intake file name
    if intake is not None:
        from pps_ats.evaluation.corrected_only import document_id_for

        folder = (
            Path(intake) / "corrected" if (Path(intake) / "corrected").is_dir() else Path(intake)
        )
        for path in folder.glob("*.pdf"):
            intake_names[document_id_for(path, settings)] = path.name
    if salvage_log is not None:
        log = [r for r in _rows(salvage_log) if r["status"] == "complete"]
        salvage_complete = len(log)
        logged = {r["sha256"] for r in log}
        salvage_matches = sum(1 for d in hashes.values() if d in logged)
    # duplicates / identity (text of the clean inputs; image-only: hash only)
    texts = {}
    for d in docs:
        clean = source / d["file"]
        texts[d["document_id"]] = (
            _pdf_text(clean) if d.get("scanner_input_status") == "clean" else ""
        )
    groups, dup_stats = duplicate_groups(texts, hashes, duplicate_threshold)
    # write reference
    (ref / "files").mkdir(parents=True)
    (ref / "corrected").mkdir()
    key_dir.mkdir(parents=True, exist_ok=True)
    key_rows, doc_rows, cand_rows, manifest_docs = [], [], [], {}
    for d in docs:
        old, new = d["document_id"], pseudo[d["document_id"]]
        shutil.copyfile(source / "corrected" / f"{old}.pdf", ref / "corrected" / f"{new}.pdf")
        shutil.copyfile(source / d["file"], ref / "files" / f"{new}.pdf")
        info = per_doc[old]
        key_rows.append(
            {
                "pseudonym": new,
                "source_document_id": old,
                "intake_file": intake_names.get(old, ""),
                "sha256": hashes[old],
            }
        )
        doc_rows.append(
            {
                **d,
                "document_id": new,
                "file": f"files/{new}.pdf",
                "split": "",
                "marks_source": "",
                "faculty_content_relevance": "",
                "faculty_structure_formatting": "",
                "faculty_language_professional_tone": "",
                "faculty_total": "",
                "grader_id": "",
                "group_id": f"G-{pseudo[groups[old][2:]]}",
                "notes": "corrected-only; no faculty marks"
                + (
                    ""
                    if d.get("scanner_input_status") == "clean"
                    else "; INELIGIBLE until resolved"
                ),
            }
        )
        manifest_docs[new] = {
            "sha256": hashes[old],
            "pages": info.get("pages"),
            "scanner_input_status": info.get("scanner_input_status"),
            "candidates": info.get("candidates"),
            "removed_annotations": info.get("removed_annotations"),
            "student_text_preserved": info.get("student_text_preserved"),
        }
    for c in cands:
        new = pseudo[c["document_id"]]
        cand_rows.append(
            {
                **c,
                "document_id": new,
                "candidate_id": new + c["candidate_id"][len(c["document_id"]) :],
                "author": annotator.get(c.get("author", ""), ""),
                "final_label": "",  # never pre-filled: a reviewer chooses
                "final_severity": "",
                "reviewer_decision": "",
            }
        )
    cand_rows.sort(key=lambda r: r["candidate_id"])
    doc_rows.sort(key=lambda r: r["document_id"])
    fields = list(doc_rows[0])
    _write_csv(ref / "documents.csv", fields, doc_rows)
    _write_csv(ref / CANDIDATES, list(cand_rows[0]), cand_rows)
    _write_csv(
        key_dir / "id_key.csv", list(key_rows[0]), sorted(key_rows, key=lambda r: r["pseudonym"])
    )
    (key_dir / "annotator_key.json").write_text(
        json.dumps({v: k for k, v in annotator.items()}, indent=2), encoding="utf-8"
    )
    manifest = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "engine_version": __version__,
        "gated": True,  # held-out evaluation requires review + calibration freeze
        "source": "corrected-only import (pseudonymised)",
        "documents_total": len(doc_rows),
        "eligible": sum(1 for r in doc_rows if r.get("scanner_input_status") == "clean"),
        "ineligible": dict(
            Counter(
                r["scanner_input_status"]
                for r in doc_rows
                if r.get("scanner_input_status") != "clean"
            )
        ),
        "candidates": len(cand_rows),
        "faculty_marks": 0,
        "salvage_log_complete_entries": salvage_complete,
        "salvage_log_matches": salvage_matches,
        "duplicates": dup_stats,
        "duplicate_threshold": duplicate_threshold,
        "documents": manifest_docs,
    }
    (ref / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


# ---------------------------------------------------------------- review workbook


def _labels(settings: Settings) -> list[str]:
    return sorted(settings.evaluation.labels)


def _out_of_scope(settings: Settings) -> set[str]:
    return {k for k, v in settings.evaluation.labels.items() if not v}


def _clean_cell(value: str) -> str:
    return ILLEGAL_CHARACTERS_RE.sub("", value or "")


def build_review_workbook(ref: Path, settings: Settings, force: bool = False) -> Path:
    ref = Path(ref)
    path = ref / REVIEW_DIR / REVIEW_BOOK
    if path.exists() and not force:
        raise ReferenceError(f"{path} exists and may hold review decisions; use --force")
    docs = {r["document_id"]: r for r in _rows(ref / "documents.csv")}
    if any(not d.get("split") for d in docs.values()):
        raise ReferenceError("freeze the split first (eval-split) so each sheet holds one split")
    cands = _rows(ref / CANDIDATES)
    wb = Workbook()
    info = wb.active
    info.title = "Instructions"
    lines = [
        "Faculty annotation review (private: keep outside Git and OneDrive)",
        "",
        "Review the 'calibration' sheet first. The 'evaluation' sheet must be reviewed",
        "independently (ideally by another reviewer) and only after calibration choices are",
        "frozen; do not tune keywords or rules on it.",
        "",
        "For every row:",
        "  decision        accept = this is a real faculty correction with the label below;",
        "                  reject = not a correction (noise, duplicate, student text);",
        "                  unsure = needs discussion (blocks finalisation).",
        "  final_label     choose from the list (see the 'labels' sheet). The suggestion is",
        "                  only a hint and is NOT accepted automatically. Use other_feedback",
        "                  for faculty advice that fits no label - it is preserved.",
        "  faculty_intent  correction_required = the faculty says something is wrong/missing;",
        "                  recommendation = advice/improvement, not a defect; unclear.",
        "  final_severity  high | medium | low (blank = medium).",
        "  reviewer_id     your initials or staff id (required for accepted rows).",
        "",
        "Labels marked 'outside scanner scope' have no scanner rule yet: they are kept as",
        "faculty feedback and reported separately, never counted as scanner errors.",
        "Do not edit any other column. Then run: python -m pps_ats finalize-review <dataset>",
    ]
    for n, line in enumerate(lines, 1):
        info.cell(row=n, column=1, value=line)
    info.column_dimensions["A"].width = 100
    label_ws = wb.create_sheet("labels")
    out = _out_of_scope(settings)
    label_ws.append(["label", "scanner rules", "scope"])
    for label in _labels(settings):
        rules = ", ".join(settings.evaluation.labels[label])
        label_ws.append([label, rules, "outside scanner scope" if label in out else "scanner"])
    label_ws.column_dimensions["A"].width = 24
    label_ws.column_dimensions["B"].width = 70
    label_ws.column_dimensions["C"].width = 22
    n_labels = len(settings.evaluation.labels) + 1
    for split in ("calibration", "evaluation"):
        ws = wb.create_sheet(split)
        for col, (name, width) in enumerate(REVIEW_COLUMNS, 1):
            cell = ws.cell(row=1, column=col, value=name)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F3864" if name not in EDITABLE else "7F6000")
            ws.column_dimensions[cell.column_letter].width = width
        rows = [c for c in cands if docs[c["document_id"]]["split"] == split]
        for r, c in enumerate(rows, 2):
            alternatives = [m.strip() for m in c.get("label_matches", "").split(",") if m.strip()]
            values = {
                "candidate_id": c["candidate_id"],
                "document_id": c["document_id"],
                "page": int(c["page"]) if c["page"].isdigit() else c["page"],
                "faculty_correction": c.get("teacher_comment", ""),
                "associated_section": c.get("associated_section", ""),
                "suggested_label": c.get("suggested_label", ""),
                "alternative_labels": ", ".join(
                    a for a in alternatives if a != c.get("suggested_label")
                ),
                "suggestion_reason": "; ".join(
                    x for x in (c.get("label_reason"), c.get("review_reason")) if x
                ),
                "source_type": c.get("source_type", ""),
                "faculty_likelihood": c.get("faculty_likelihood", ""),
                "resume_context": c.get("context", ""),
            }
            for col, (name, _) in enumerate(REVIEW_COLUMNS, 1):
                cell = ws.cell(row=r, column=col, value=_clean_cell(str(values.get(name, ""))))
                cell.data_type = "s" if name != "page" else cell.data_type
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        last = max(2, len(rows) + 1)
        cols = {name: col for col, (name, _) in enumerate(REVIEW_COLUMNS, 1)}

        for name, formula in (
            ("decision", '"' + ",".join(DECISIONS) + '"'),
            ("faculty_intent", '"' + ",".join(INTENTS) + '"'),
            ("final_severity", '"' + ",".join(SEVERITIES) + '"'),
            ("final_label", f"labels!$A$2:$A${n_labels}"),
        ):
            letter = ws.cell(row=1, column=cols[name]).column_letter
            dv = DataValidation(type="list", formula1=formula, allow_blank=True)
            dv.add(f"{letter}2:{letter}{last}")
            ws.add_data_validation(dv)
        ws.freeze_panes = "D2"
        ws.auto_filter.ref = f"A1:{ws.cell(row=1, column=len(REVIEW_COLUMNS)).column_letter}{last}"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp.xlsx")
    wb.save(tmp)
    tmp.replace(path)
    return path


def read_review(ref: Path) -> list[dict[str, str]]:
    wb = load_workbook(Path(ref) / REVIEW_DIR / REVIEW_BOOK, read_only=True, data_only=True)
    out = []
    for split in ("calibration", "evaluation"):
        rows = list(wb[split].iter_rows(values_only=True))
        header = [str(h) for h in rows[0]]
        for r in rows[1:]:
            if not r or r[0] is None:
                continue
            item = {
                h: ("" if v is None else str(v).strip()) for h, v in zip(header, r, strict=False)
            }
            item["split"] = split
            out.append(item)
    wb.close()
    return out


@dataclass
class ReviewStatus:
    by_split: dict[str, dict[str, int]] = field(default_factory=dict)
    invalid: list[str] = field(default_factory=list)  # candidate ids + reason (no text)


def _row_problem(row: dict[str, str], labels: set[str]) -> str | None:
    decision = row.get("decision", "").lower()
    if decision not in ("", *DECISIONS):
        return f"decision {decision!r} is not accept/reject/unsure"
    if decision != "accept":
        return None
    if row.get("final_label", "").lower() not in labels:
        return "accepted without a valid final_label"
    if row.get("faculty_intent", "").lower() not in INTENTS:
        return "accepted without faculty_intent"
    if row.get("final_severity", "").lower() not in ("", *SEVERITIES):
        return "final_severity is not high/medium/low"
    if not row.get("reviewer_id"):
        return "accepted without reviewer_id"
    return None


def review_status(ref: Path, settings: Settings) -> ReviewStatus:
    labels = set(settings.evaluation.labels)
    status = ReviewStatus()
    for row in read_review(ref):
        decision = row.get("decision", "").lower()
        key = {"accept": "confirmed", "reject": "rejected", "unsure": "uncertain"}.get(
            decision, "unreviewed" if not decision else "invalid"
        )
        problem = _row_problem(row, labels)
        if problem:
            key = "invalid"
            status.invalid.append(f"{row['candidate_id']}: {problem}")
        split = status.by_split.setdefault(
            row["split"],
            {"confirmed": 0, "rejected": 0, "uncertain": 0, "unreviewed": 0, "invalid": 0},
        )
        split[key] += 1
    return status


def finalize_review(ref: Path, settings: Settings, split: str) -> dict:
    """Write reference annotations for one split from explicitly accepted rows.

    Refused while any row of that split is unreviewed, uncertain or invalid. Annotations
    already finalised for the other split are kept unchanged."""
    ref = Path(ref)
    labels = set(settings.evaluation.labels)
    rows = [r for r in read_review(ref) if r["split"] == split]
    problems = []
    for r in rows:
        decision = r.get("decision", "").lower()
        if decision in ("", "unsure"):
            problems.append(f"{r['candidate_id']}: {'unreviewed' if not decision else 'unsure'}")
        elif (p := _row_problem(r, labels)) is not None:
            problems.append(f"{r['candidate_id']}: {p}")
    if problems:
        raise ReferenceError(
            f"{len(problems)} {split} row(s) not ready: "
            + "; ".join(problems[:8])
            + (" ..." if len(problems) > 8 else "")
        )
    docs = {d["document_id"]: d for d in _rows(ref / "documents.csv")}
    cands = {c["candidate_id"]: c for c in _rows(ref / CANDIDATES)}
    existing = _rows(ref / "annotations.csv") if (ref / "annotations.csv").is_file() else []
    keep = [a for a in existing if docs[a["document_id"]]["split"] != split]
    new = []
    for r in rows:
        if r["decision"].lower() != "accept":
            continue
        c = cands[r["candidate_id"]]
        comment = " | ".join(x for x in (c.get("teacher_comment"), r.get("reviewer_notes")) if x)
        new.append(
            {
                "document_id": r["document_id"],
                "label": r["final_label"].lower(),
                "severity": (r.get("final_severity") or "medium").lower(),
                "text": c.get("quoted_text", ""),
                "page": c.get("page", ""),
                "criterion": "",
                "source": "faculty",
                "annotator_id": c.get("author", ""),
                "comment": comment,
                "intent": r["faculty_intent"].lower(),
                "reviewer_id": r["reviewer_id"],
            }
        )
    _write_csv(ref / "annotations.csv", ANNOTATION_FIELDS, keep + new)
    state_path = ref / REVIEW_STATE
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    state[split] = {
        "finalized_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rows": len(rows),
        "accepted": len(new),
        "rejected": len(rows) - len(new),
        "reviewers": sorted({r["reviewer_id"] for r in rows if r.get("reviewer_id")}),
        "review_book_sha256": sha256(ref / REVIEW_DIR / REVIEW_BOOK),
    }
    state["annotations_sha256"] = sha256(ref / "annotations.csv")
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state[split]


# ---------------------------------------------------------------- calibration freeze + gate


def calibration_fingerprint(settings: Settings) -> str:
    from pps_ats.scoring.engine import config_hash

    payload = {
        "engine": __version__,
        "labels": settings.evaluation.labels,
        "import": settings.evaluation.import_.model_dump(),
        "evaluation": settings.evaluation.model_dump(exclude={"labels", "import_"}),
        "scoring": config_hash(settings.scoring),
        "analysis": hashlib.sha256(settings.analysis.model_dump_json().encode()).hexdigest(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[
        :16
    ]


def freeze_calibration(ref: Path, settings: Settings) -> dict:
    ref = Path(ref)
    state_path = ref / REVIEW_STATE
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    if "calibration" not in state:
        raise ReferenceError(
            "finalise the calibration review first (finalize-review --split calibration)"
        )
    if (ref / CAL_FREEZE).exists():
        raise ReferenceError(f"{ref / CAL_FREEZE} exists: calibration choices are already frozen")
    freeze = {
        "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "fingerprint": calibration_fingerprint(settings),
        "config_source": settings.source,
        "calibration_review": state["calibration"],
    }
    (ref / CAL_FREEZE).write_text(json.dumps(freeze, indent=2), encoding="utf-8")
    return freeze


def held_out_gate(ref: Path, settings: Settings) -> list[str]:
    """Reasons the held-out evaluation may not run yet (empty list = allowed). Only
    applies to datasets prepared by ``prepare_reference`` (``gated`` manifest)."""
    ref = Path(ref)
    manifest_path = ref / MANIFEST
    if not manifest_path.is_file():
        return []
    if not json.loads(manifest_path.read_text(encoding="utf-8")).get("gated"):
        return []
    reasons = []
    freeze_path = ref / CAL_FREEZE
    if not freeze_path.is_file():
        reasons.append("calibration choices are not frozen (freeze-calibration)")
    elif json.loads(freeze_path.read_text(encoding="utf-8"))[
        "fingerprint"
    ] != calibration_fingerprint(settings):
        reasons.append("configuration changed since the calibration freeze")
    state_path = ref / REVIEW_STATE
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    if "evaluation" not in state:
        reasons.append("evaluation labels are not independently reviewed and finalised")
    elif state.get("annotations_sha256") != sha256(ref / "annotations.csv"):
        reasons.append("annotations.csv changed after finalisation")
    return reasons
