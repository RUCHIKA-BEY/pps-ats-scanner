"""Faculty-corrected reference dataset: loading, validation and split protection.

Dataset folder::

    documents.csv     one row per resume (required)
    annotations.csv   faculty issue annotations (optional)
    split_lock.json   frozen calibration/evaluation split (written by ``eval-split``)
    <files>           the resumes, at the paths given in documents.csv

documents.csv columns: ``document_id, file, split`` (required); ``expected_validation``
(valid | needs_review | invalid), ``marks_source`` (faculty | ai | unknown),
``faculty_content_relevance, faculty_structure_formatting,
faculty_language_professional_tone, faculty_total``, ``annotations_complete`` (yes | no),
``grader_id, notes`` (optional).

annotations.csv columns: ``document_id, label`` (required); ``severity`` (high | medium |
low), ``text`` (quote from the resume), ``page``, ``criterion``, ``source`` (faculty | ai),
``annotator_id, comment`` (optional).

Only ``marks_source = faculty`` marks and ``source = faculty`` annotations are used as
ground truth; AI-generated marks or annotations are loaded but excluded and counted.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

SPLITS = ("calibration", "evaluation")
VALIDATION_VALUES = ("valid", "needs_review", "invalid")
CRITERIA = ("content_relevance", "structure_formatting", "language_professional_tone")
SPLIT_LOCK = "split_lock.json"


class DatasetError(ValueError):
    pass


@dataclass
class ReferenceAnnotation:
    document_id: str
    label: str
    severity: str = "medium"
    text: str | None = None
    page: int | None = None
    criterion: str | None = None
    source: str = "faculty"
    annotator_id: str | None = None
    comment: str | None = None


@dataclass
class ReferenceDocument:
    document_id: str
    file: str
    split: str
    path: Path
    expected_validation: str | None = None
    marks_source: str = "unknown"
    faculty_scores: dict[str, float] = field(default_factory=dict)
    faculty_total: float | None = None
    annotations_complete: bool = False
    grader_id: str | None = None
    notes: str | None = None
    annotations: list[ReferenceAnnotation] = field(default_factory=list)

    @property
    def has_faculty_marks(self) -> bool:
        return self.marks_source == "faculty" and len(self.faculty_scores) == len(CRITERIA)


@dataclass
class ReferenceDataset:
    root: Path
    documents: list[ReferenceDocument]
    fingerprint: str
    warnings: list[str] = field(default_factory=list)
    excluded_ai_annotations: int = 0
    unknown_labels: dict[str, int] = field(default_factory=dict)

    def select(self, split: str) -> list[ReferenceDocument]:
        return [d for d in self.documents if d.split == split]


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return []
        return [
            {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in reader
        ]


def _num(value: str, what: str, doc: str, low: float, high: float) -> float | None:
    if value == "":
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise DatasetError(f"{doc}: {what} is not a number: {value!r}") from exc
    if not low <= number <= high:
        raise DatasetError(f"{doc}: {what} = {number:g} is outside {low:g}-{high:g}")
    return number


def _fingerprint(*paths: Path) -> str:
    digest = hashlib.sha256()
    for path in paths:
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def load_dataset(
    root: Path, documents_root: Path | None = None, labels: set[str] | None = None
) -> ReferenceDataset:
    root = Path(root)
    docs_csv, ann_csv = root / "documents.csv", root / "annotations.csv"
    if not docs_csv.is_file():
        raise DatasetError(f"{docs_csv} not found")
    base = Path(documents_root) if documents_root else root
    warnings: list[str] = []
    documents: dict[str, ReferenceDocument] = {}
    for line, row in enumerate(_rows(docs_csv), start=2):
        doc_id = row.get("document_id", "")
        if not doc_id:
            raise DatasetError(f"documents.csv line {line}: document_id is empty")
        if doc_id in documents:
            raise DatasetError(f"documents.csv: duplicate document_id {doc_id!r}")
        split = row.get("split", "").lower()
        if split not in SPLITS:
            raise DatasetError(f"{doc_id}: split must be one of {SPLITS} (run eval-split)")
        expected = row.get("expected_validation", "").lower() or None
        if expected is not None and expected not in VALIDATION_VALUES:
            raise DatasetError(f"{doc_id}: expected_validation must be one of {VALIDATION_VALUES}")
        source = row.get("marks_source", "").lower() or "unknown"
        scores = {}
        for c in CRITERIA:
            value = _num(row.get(f"faculty_{c}", ""), f"faculty_{c}", doc_id, 0, 10)
            if value is not None:
                scores[c] = value
        total = _num(row.get("faculty_total", ""), "faculty_total", doc_id, 0, 30)
        if len(scores) == len(CRITERIA):
            computed = round(sum(scores.values()), 2)
            if total is not None and abs(total - computed) > 0.01:
                warnings.append(
                    f"{doc_id}: faculty_total {total:g} != sum of criteria {computed:g}; "
                    "the sum is used"
                )
            total = computed
        elif scores:
            warnings.append(f"{doc_id}: only {len(scores)} of 3 criterion marks; marks ignored")
            scores, total = {}, None
        if scores and source != "faculty":
            warnings.append(f"{doc_id}: marks_source is {source!r}; marks are not ground truth")
        rel = row.get("file", "")
        if not rel:
            raise DatasetError(f"{doc_id}: file is empty")
        documents[doc_id] = ReferenceDocument(
            document_id=doc_id,
            file=rel,
            split=split,
            path=base / rel,
            expected_validation=expected,
            marks_source=source,
            faculty_scores=scores,
            faculty_total=total,
            annotations_complete=row.get("annotations_complete", "").lower()
            in ("yes", "y", "true", "1"),
            grader_id=row.get("grader_id") or None,
            notes=row.get("notes") or None,
        )
    excluded = 0
    unknown: dict[str, int] = {}
    if ann_csv.is_file():
        for line, row in enumerate(_rows(ann_csv), start=2):
            doc_id, label = row.get("document_id", ""), row.get("label", "").lower()
            if doc_id not in documents:
                raise DatasetError(f"annotations.csv line {line}: unknown document_id {doc_id!r}")
            if not label:
                raise DatasetError(f"annotations.csv line {line}: label is empty")
            source = row.get("source", "").lower() or "faculty"
            if source != "faculty":
                excluded += 1
                continue
            if labels is not None and label not in labels:
                unknown[label] = unknown.get(label, 0) + 1
            page = row.get("page", "")
            documents[doc_id].annotations.append(
                ReferenceAnnotation(
                    document_id=doc_id,
                    label=label,
                    severity=(row.get("severity", "") or "medium").lower(),
                    text=row.get("text") or None,
                    page=int(page) if page.isdigit() else None,
                    criterion=row.get("criterion") or None,
                    source=source,
                    annotator_id=row.get("annotator_id") or None,
                    comment=row.get("comment") or None,
                )
            )
    dataset = ReferenceDataset(
        root=root,
        documents=list(documents.values()),
        fingerprint=_fingerprint(docs_csv, ann_csv),
        warnings=warnings,
        excluded_ai_annotations=excluded,
        unknown_labels=unknown,
    )
    check_split_lock(dataset)
    return dataset


# ---------------------------------------------------------------- split protection


def check_split_lock(dataset: ReferenceDataset) -> None:
    """Refuse a dataset whose documents moved between splits after the split was frozen
    (e.g. evaluation documents re-labelled as calibration after seeing results)."""
    lock_path = dataset.root / SPLIT_LOCK
    if not lock_path.is_file():
        dataset.warnings.append(
            "no split_lock.json: run `eval-split` to freeze the calibration/evaluation split"
        )
        return
    frozen: dict[str, str] = json.loads(lock_path.read_text(encoding="utf-8"))["splits"]
    moved = [
        d.document_id for d in dataset.documents if frozen.get(d.document_id, d.split) != d.split
    ]
    if moved:
        raise DatasetError(
            f"{len(moved)} document(s) changed split since it was frozen: " + ", ".join(moved[:10])
        )


def _bucket(document_id: str, seed: int) -> float:
    digest = hashlib.sha256(f"{seed}:{document_id}".encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def freeze_split(root: Path, eval_fraction: float = 0.3, seed: int = 13) -> dict[str, int]:
    """Assign a split to rows without one (deterministic by document_id hash), keep every
    existing assignment, and write ``documents.csv`` and ``split_lock.json``.

    The original ``documents.csv`` is kept as ``documents.csv.bak`` the first time."""
    root = Path(root)
    path = root / "documents.csv"
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    if "split" not in fields:
        fields.insert(2 if len(fields) >= 2 else len(fields), "split")
    lock_path = root / SPLIT_LOCK
    frozen: dict[str, str] = (
        json.loads(lock_path.read_text(encoding="utf-8"))["splits"] if lock_path.is_file() else {}
    )
    counts = {s: 0 for s in SPLITS}
    for row in rows:
        doc_id = (row.get("document_id") or "").strip()
        split = (row.get("split") or "").strip().lower() or frozen.get(doc_id, "")
        if split not in SPLITS:
            split = "evaluation" if _bucket(doc_id, seed) < eval_fraction else "calibration"
        if doc_id in frozen and frozen[doc_id] != split:
            raise DatasetError(
                f"{doc_id}: split {split!r} differs from the frozen {frozen[doc_id]!r}"
            )
        row["split"] = split
        frozen[doc_id] = split
        counts[split] += 1
    backup = root / "documents.csv.bak"
    if not backup.exists():
        backup.write_bytes(path.read_bytes())
    tmp = path.with_name("documents.csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)
    lock_path.write_text(
        json.dumps({"seed": seed, "eval_fraction": eval_fraction, "splits": frozen}, indent=2),
        encoding="utf-8",
    )
    return counts
