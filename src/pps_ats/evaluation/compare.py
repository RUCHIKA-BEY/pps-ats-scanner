"""Compare scanner predictions with faculty references.

Issue matching (per document and label): a faculty annotation and a scanner detection
match when their texts overlap (containment, or word-overlap >= ``text_match_threshold``);
an annotation without a quote matches a detection on the same page (or any page if
either page is unknown). Each item is matched at most once, best pairs first.

Outcomes: ``tp`` matched; ``fn`` faculty issue the scanner missed; ``fp`` scanner
detection on a document whose faculty annotations are complete; ``fp_unverified`` the
same on a document with partial annotations (cannot be counted as an error).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from fnmatch import fnmatch

from pydantic import BaseModel, Field

from pps_ats.config import EvaluationSettings, ScoringSettings
from pps_ats.evaluation.dataset import CRITERIA, ReferenceDocument
from pps_ats.schemas.findings import AnalysisResult
from pps_ats.schemas.scoring import ScoringResult
from pps_ats.scoring.engine import band_for

_WORD = re.compile(r"[a-z0-9]+")


@dataclass
class Prediction:
    document: ReferenceDocument
    analysis: AnalysisResult | None = None
    scoring: ScoringResult | None = None
    error: str | None = None


@dataclass
class Detection:
    label: str
    source: str  # rule id or "AI:<aspect>"
    text: str | None
    page: int | None
    severity: str
    high: bool


# ---------------------------------------------------------------- report schema


class ErrorStats(BaseModel):
    n: int = 0
    mae: float | None = None
    bias: float | None = None  # mean (scanner - faculty); > 0 = scanner too generous
    rmse: float | None = None
    max_abs: float | None = None
    within: float | None = None  # share with |error| <= tolerance
    tolerance: float = 1.0
    band_agreement: float | None = None


class IssueCounts(BaseModel):
    tp: int = 0
    fn: int = 0
    fp: int = 0
    fp_unverified: int = 0
    precision: float | None = None  # from verified fp only
    recall: float | None = None


class EvaluationReport(BaseModel):
    variant: str
    split: str
    held_out: bool
    dataset_fingerprint: str
    created_at: str
    engine_version: str
    rubric_version: str
    scoring_fingerprint: str
    ai_model: str | None = None
    documents: int = 0
    documents_missing: list[str] = Field(default_factory=list)
    documents_failed: list[str] = Field(default_factory=list)
    validation_confusion: dict[str, dict[str, int]] = Field(default_factory=dict)
    validation_errors: list[dict] = Field(default_factory=list)
    issue_metrics: dict[str, IssueCounts] = Field(default_factory=dict)
    issue_overall: IssueCounts = Field(default_factory=IssueCounts)
    high_severity: IssueCounts = Field(default_factory=IssueCounts)
    issue_rows: list[dict] = Field(default_factory=list)
    criterion_errors: dict[str, ErrorStats] = Field(default_factory=dict)
    total_error: ErrorStats = Field(default_factory=ErrorStats)
    score_rows: list[dict] = Field(default_factory=list)
    score_exclusions: dict[str, int] = Field(default_factory=dict)
    uncertain_cases: list[dict] = Field(default_factory=list)
    fine_tuning_readiness: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- helpers


def _norm(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def similarity(a: str | None, b: str | None) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    wa, wb = set(_WORD.findall(na)), set(_WORD.findall(nb))
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def label_for(source: str, labels: dict[str, list[str]]) -> str | None:
    for label, patterns in labels.items():
        if any(fnmatch(source, p) for p in patterns):
            return label
    return None


def detections(pred: Prediction, cfg: EvaluationSettings, include_ai: bool) -> list[Detection]:
    out: list[Detection] = []
    if pred.analysis is not None:
        for f in pred.analysis.findings:
            label = label_for(f.rule_id, cfg.labels) or f"unmapped:{f.rule_id}"
            high = (
                f.severity.value in cfg.high_severity_levels
                or f.finding_class.value in cfg.high_severity_classes
            )
            out.append(
                Detection(label, f.rule_id, f.detected_text, f.page_number, f.severity.value, high)
            )
    out = _merge_same_issue(out)
    if include_ai and pred.scoring is not None:
        for o in pred.scoring.ai_observations:
            source = f"AI:{o.aspect}"
            label = label_for(source, cfg.labels) or f"unmapped:{source}"
            out.append(
                Detection(
                    label,
                    source,
                    o.evidence,
                    None,
                    o.severity,
                    o.severity in cfg.high_severity_levels,
                )
            )
    return out


def _merge_same_issue(found: list[Detection]) -> list[Detection]:
    """Several scanner rules can report one issue (e.g. SEC_PLACEHOLDER_ONLY and
    PH_BRACKETED_INSTRUCTION on the same line). Keep one detection per label and text,
    preferring a high-severity one, so faculty are compared with distinct issues."""
    kept: list[Detection] = []
    for det in sorted(found, key=lambda d: not d.high):
        if det.text and any(
            k.label == det.label and similarity(k.text, det.text) == 1.0 for k in kept
        ):
            continue
        kept.append(det)
    return kept


def match_document(doc: ReferenceDocument, found: list[Detection], threshold: float) -> list[dict]:
    """Rows with outcome tp / fn / fp / fp_unverified for one document."""
    pairs: list[tuple[float, int, int]] = []
    for ai, ann in enumerate(doc.annotations):
        for di, det in enumerate(found):
            if det.label != ann.label:
                continue
            if ann.text:
                score = similarity(ann.text, det.text)
            else:
                same_page = ann.page is None or det.page is None or ann.page == det.page
                score = 0.6 if same_page else 0.0
            if score >= threshold:
                pairs.append((score, ai, di))
    pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
    used_a: set[int] = set()
    used_d: set[int] = set()
    rows: list[dict] = []

    def row(outcome: str, ann=None, det=None, score=None) -> dict:  # type: ignore[no-untyped-def]
        return {
            "document_id": doc.document_id,
            "label": (ann or det).label,
            "outcome": outcome,
            "faculty_severity": ann.severity if ann else None,
            "faculty_text": ann.text if ann else None,
            "scanner_source": det.source if det else None,
            "scanner_severity": det.severity if det else None,
            "scanner_text": det.text if det else None,
            "page": (ann.page if ann else None) or (det.page if det else None),
            "similarity": round(score, 2) if score is not None else None,
            "high_severity": bool(
                (ann and ann.severity == "high") or (det and det.high and not ann)
            ),
        }

    for score, ai, di in pairs:
        if ai in used_a or di in used_d:
            continue
        used_a.add(ai)
        used_d.add(di)
        rows.append(row("tp", doc.annotations[ai], found[di], score))
    for ai, ann in enumerate(doc.annotations):
        if ai not in used_a:
            rows.append(row("fn", ann=ann))
    fp = "fp" if doc.annotations_complete else "fp_unverified"
    for di, det in enumerate(found):
        if di not in used_d:
            rows.append(row(fp, det=det))
    return rows


def _counts(rows: list[dict]) -> IssueCounts:
    c = IssueCounts()
    for r in rows:
        setattr(c, r["outcome"], getattr(c, r["outcome"]) + 1)
    c.precision = round(c.tp / (c.tp + c.fp), 3) if c.tp + c.fp else None
    c.recall = round(c.tp / (c.tp + c.fn), 3) if c.tp + c.fn else None
    return c


def error_stats(
    pairs: list[tuple[float, float]], tolerance: float, scoring: ScoringSettings | None = None
) -> ErrorStats:
    if not pairs:
        return ErrorStats(tolerance=tolerance)
    errors = [p - f for p, f in pairs]
    n = len(errors)
    bands = None
    if scoring is not None:
        bands = round(sum(band_for(p, scoring) == band_for(f, scoring) for p, f in pairs) / n, 3)
    return ErrorStats(
        n=n,
        mae=round(sum(abs(e) for e in errors) / n, 3),
        bias=round(sum(errors) / n, 3),
        rmse=round(math.sqrt(sum(e * e for e in errors) / n), 3),
        max_abs=round(max(abs(e) for e in errors), 3),
        within=round(sum(abs(e) <= tolerance + 1e-9 for e in errors) / n, 3),
        tolerance=tolerance,
        band_agreement=bands,
    )


def _predicted_validation(pred: Prediction) -> str:
    if pred.analysis is None:
        return "error"
    return pred.analysis.validation.status.value


# ---------------------------------------------------------------- main comparison


def compare(
    predictions: list[Prediction],
    report: EvaluationReport,
    cfg: EvaluationSettings,
    scoring: ScoringSettings,
    include_ai: bool,
) -> EvaluationReport:
    issue_rows: list[dict] = []
    crit_pairs: dict[str, list[tuple[float, float]]] = {c: [] for c in CRITERIA}
    total_pairs: list[tuple[float, float]] = []
    exclusions = {"ai_or_unknown_marks": 0, "no_faculty_marks": 0, "not_graded_by_scanner": 0}
    confusion: dict[str, dict[str, int]] = {}

    for pred in predictions:
        doc = pred.document
        predicted = _predicted_validation(pred)
        if pred.error:
            report.documents_failed.append(f"{doc.document_id}: {pred.error}")
        # ---- validation
        if doc.expected_validation:
            row = confusion.setdefault(doc.expected_validation, {})
            row[predicted] = row.get(predicted, 0) + 1
            if predicted != doc.expected_validation:
                issues = [i.code for i in pred.analysis.validation.issues] if pred.analysis else []
                report.validation_errors.append(
                    {
                        "document_id": doc.document_id,
                        "file": doc.file,
                        "expected": doc.expected_validation,
                        "predicted": predicted,
                        "scanner_issue_codes": ", ".join(issues),
                        "kind": "wrongly rejected"
                        if predicted == "invalid"
                        else "wrongly accepted"
                        if doc.expected_validation == "invalid"
                        else "review status differs",
                    }
                )
        # ---- issues (only documents the scanner analysed and faculty annotated)
        if doc.annotations or doc.annotations_complete:
            issue_rows += match_document(
                doc, detections(pred, cfg, include_ai), cfg.text_match_threshold
            )
        # ---- marks
        if not doc.faculty_scores:
            exclusions["no_faculty_marks"] += 1
        elif not doc.has_faculty_marks:
            exclusions["ai_or_unknown_marks"] += 1
        elif pred.scoring is None or pred.scoring.ca1 is None:
            exclusions["not_graded_by_scanner"] += 1
        else:
            got = {c.criterion: c.final_score for c in pred.scoring.ca1.criteria}
            entry = {"document_id": doc.document_id, "file": doc.file}
            for c in CRITERIA:
                crit_pairs[c].append((got[c], doc.faculty_scores[c]))
                entry[f"faculty_{c}"] = doc.faculty_scores[c]
                entry[f"scanner_{c}"] = got[c]
                entry[f"diff_{c}"] = round(got[c] - doc.faculty_scores[c], 2)
            total = pred.scoring.ca1.total
            assert doc.faculty_total is not None
            total_pairs.append((total, doc.faculty_total))
            entry.update(
                faculty_total=doc.faculty_total,
                scanner_total=total,
                diff_total=round(total - doc.faculty_total, 2),
                scanner_status=pred.scoring.status.value,
            )
            report.score_rows.append(entry)
        # ---- uncertain / ungradable
        reasons = []
        if pred.scoring is not None:
            if pred.scoring.status.value in ("needs_review", "not_graded"):
                reasons.append(pred.scoring.status.value)
                reasons += pred.scoring.review_reasons[:5]
            if pred.scoring.ca1 and any(c.near_band_boundary for c in pred.scoring.ca1.criteria):
                reasons.append("near a band boundary")
        if pred.analysis and pred.analysis.validation.classification.document_class.value in (
            "uncertain",
            "unknown",
        ):
            reasons.append("classification uncertain")
        if pred.error:
            reasons.append(f"processing error: {pred.error}")
        if reasons:
            report.uncertain_cases.append(
                {
                    "document_id": doc.document_id,
                    "file": doc.file,
                    "scanner_status": pred.scoring.status.value if pred.scoring else "error",
                    "faculty_marks": doc.faculty_total if doc.has_faculty_marks else None,
                    "expected_validation": doc.expected_validation,
                    "reasons": "; ".join(reasons),
                }
            )

    report.validation_confusion = confusion
    report.issue_rows = issue_rows
    labels = sorted({r["label"] for r in issue_rows})
    report.issue_metrics = {
        label: _counts([r for r in issue_rows if r["label"] == label]) for label in labels
    }
    report.issue_overall = _counts(
        [r for r in issue_rows if not r["label"].startswith("unmapped:")]
    )
    report.high_severity = _counts([r for r in issue_rows if r["high_severity"]])
    report.criterion_errors = {
        c: error_stats(pairs, 1.0, scoring) for c, pairs in crit_pairs.items()
    }
    report.total_error = error_stats(total_pairs, 2.0)
    report.score_exclusions = exclusions
    return report


def fine_tuning_readiness(docs: list[ReferenceDocument], cfg: EvaluationSettings) -> dict:
    paired = [
        d
        for d in docs
        if d.split == "calibration" and d.has_faculty_marks and d.annotations_complete
    ]
    held_out = [d for d in docs if d.split == "evaluation" and d.has_faculty_marks]
    need = cfg.fine_tuning
    ready = (
        len(paired) >= need.min_paired_documents and len(held_out) >= need.min_evaluation_documents
    )
    return {
        "calibration_paired_documents": len(paired),
        "required_paired_documents": need.min_paired_documents,
        "held_out_documents": len(held_out),
        "required_held_out_documents": need.min_evaluation_documents,
        "sufficient_for_fine_tuning": ready,
        "note": "Fine-tuning is not performed by this tool. Even when sufficient, it needs "
        "an explicit decision and must be evaluated only on the held-out split.",
    }
