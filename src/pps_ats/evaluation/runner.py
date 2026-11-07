"""Run the scanner on a reference dataset and write the comparison report.

Variant ``A`` = deterministic scanner only. Variant ``B`` = scanner + AI assistance (the
configured evaluator, or an injected one for tests). The same documents, configuration
and split are used for both; only the AI step differs.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from pathlib import Path

from pps_ats import __version__
from pps_ats.analysis.orchestrator import analyze_scan
from pps_ats.config import Settings
from pps_ats.evaluation.compare import (
    EvaluationReport,
    Prediction,
    compare,
    fine_tuning_readiness,
)
from pps_ats.evaluation.dataset import CRITERIA, ReferenceDataset, ReferenceDocument
from pps_ats.evaluation.report import write_benchmark, write_report
from pps_ats.pipeline import scan_file
from pps_ats.scoring.ai import ContextualEvaluator
from pps_ats.scoring.engine import config_hash, score_analysis


def predict(
    docs: list[ReferenceDocument],
    settings: Settings,
    evaluator: ContextualEvaluator | None = None,
) -> list[Prediction]:
    out: list[Prediction] = []
    for doc in docs:
        if not doc.path.is_file():
            out.append(Prediction(doc, error="document file not found"))
            continue
        try:
            scan = scan_file(doc.path, settings)
            analysis = analyze_scan(scan, settings)
            scoring = score_analysis(analysis, settings, scan=scan, evaluator=evaluator)
            out.append(Prediction(doc, analysis, scoring))
        except Exception as exc:  # one bad document never stops an evaluation
            out.append(Prediction(doc, error=f"{type(exc).__name__}: {str(exc)[:200]}"))
    return out


def _ai_settings(settings: Settings, enabled: bool) -> Settings:
    copy = settings.model_copy(deep=True)
    copy.scoring.ai = copy.scoring.ai.model_copy(update={"enabled": enabled})
    return copy


def evaluate(
    dataset: ReferenceDataset,
    settings: Settings,
    split: str = "evaluation",
    variant: str = "A",
    evaluator: ContextualEvaluator | None = None,
    predictions: list[Prediction] | None = None,
) -> EvaluationReport:
    docs = dataset.select(split)
    use_ai = variant == "B"
    run_settings = _ai_settings(settings, use_ai)
    if use_ai and evaluator is None:
        from pps_ats.scoring.llm import build_evaluator

        evaluator = build_evaluator(run_settings.scoring.ai)
        if evaluator is None:
            raise ValueError("variant B needs an AI provider: set [scoring.ai] provider/model")
    if predictions is None:
        predictions = predict(docs, run_settings, evaluator if use_ai else None)
    report = EvaluationReport(
        variant="B (scanner + AI)" if use_ai else "A (deterministic scanner)",
        split=split,
        held_out=split == "evaluation",
        dataset_fingerprint=dataset.fingerprint,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        engine_version=__version__,
        rubric_version=settings.scoring.rubric_version,
        scoring_fingerprint=config_hash(run_settings.scoring),
        ai_model=getattr(evaluator, "model_id", None) if use_ai else None,
        documents=len(docs),
        documents_missing=[d.document_id for d in docs if not d.path.is_file()],
        warnings=list(dataset.warnings),
    )
    if not report.held_out:
        report.warnings.insert(
            0,
            "IN-SAMPLE: this split may have been used to tune scoring rules; "
            "the numbers are not a held-out accuracy estimate.",
        )
    if dataset.excluded_ai_annotations:
        report.warnings.append(
            f"{dataset.excluded_ai_annotations} AI-generated annotation(s) excluded "
            "from ground truth"
        )
    for label, count in dataset.unknown_labels.items():
        report.warnings.append(f"faculty label {label!r} ({count}x) has no scanner mapping")
    compare(predictions, report, settings.evaluation, settings.scoring, include_ai=use_ai)
    report.fine_tuning_readiness = fine_tuning_readiness(dataset.documents, settings.evaluation)
    return report


# ---------------------------------------------------------------- A vs B benchmark


def _abs_errors(report: EvaluationReport) -> dict[str, float]:
    return {r["document_id"]: abs(r["diff_total"]) for r in report.score_rows}


def paired_bootstrap(
    a: dict[str, float], b: dict[str, float], samples: int, seed: int
) -> tuple[float | None, float | None, float | None]:
    """Mean change in absolute total error (B - A) and its 95% bootstrap interval."""
    ids = sorted(set(a) & set(b))
    if not ids:
        return None, None, None
    diffs = [b[i] - a[i] for i in ids]
    mean = sum(diffs) / len(diffs)
    rng = random.Random(seed)
    stats = sorted(
        sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(max(1, samples))
    )
    lo = stats[int(0.025 * (len(stats) - 1))]
    hi = stats[int(0.975 * (len(stats) - 1))]
    return round(mean, 3), round(lo, 3), round(hi, 3)


def decide(a: EvaluationReport, b: EvaluationReport, settings: Settings) -> dict:
    rule = settings.evaluation.ai_adoption
    reasons: list[str] = []
    n = min(a.total_error.n, b.total_error.n)
    mean, lo, hi = paired_bootstrap(
        _abs_errors(a),
        _abs_errors(b),
        settings.evaluation.bootstrap_samples,
        settings.evaluation.seed,
    )
    if not a.held_out:
        reasons.append("not a held-out split")
    if n < rule.min_documents:
        reasons.append(f"only {n} faculty-marked documents (need {rule.min_documents})")
    improvement = (
        round(a.total_error.mae - b.total_error.mae, 3)
        if a.total_error.mae is not None and b.total_error.mae is not None
        else None
    )
    if improvement is None or improvement < rule.min_total_mae_improvement:
        reasons.append(
            f"total MAE improvement {improvement} < required {rule.min_total_mae_improvement}"
        )
    for c in CRITERIA:
        ea, eb = a.criterion_errors.get(c), b.criterion_errors.get(c)
        if (
            ea
            and eb
            and ea.mae is not None
            and eb.mae is not None
            and eb.mae - ea.mae > rule.max_criterion_mae_increase
        ):
            reasons.append(f"{c} MAE got worse ({ea.mae} -> {eb.mae})")
    if b.high_severity.fn - a.high_severity.fn > rule.max_high_severity_fn_increase:
        reasons.append("more high-severity issues missed")
    if b.high_severity.fp - a.high_severity.fp > rule.max_high_severity_fp_increase:
        reasons.append("more high-severity false detections")
    if rule.require_ci_excludes_zero and (hi is None or hi >= 0):
        reasons.append("95% interval of the error change does not exclude zero")
    return {
        "documents_compared": n,
        "total_mae_a": a.total_error.mae,
        "total_mae_b": b.total_error.mae,
        "total_mae_improvement": improvement,
        "abs_error_change_mean": mean,
        "abs_error_change_ci95": [lo, hi],
        "criterion_mae": {
            c: [a.criterion_errors[c].mae, b.criterion_errors[c].mae] for c in CRITERIA
        },
        "high_severity_fn": [a.high_severity.fn, b.high_severity.fn],
        "high_severity_fp": [a.high_severity.fp, b.high_severity.fp],
        "issue_recall": [a.issue_overall.recall, b.issue_overall.recall],
        "issue_precision": [a.issue_overall.precision, b.issue_overall.precision],
        "decision": "eligible_for_faculty_pilot" if not reasons else "keep_ai_disabled",
        "reasons": reasons or ["all adoption criteria met on the held-out split"],
        "note": "This decision never enables AI automatically; [scoring.ai] stays as configured.",
    }


def run_evaluation(
    dataset: ReferenceDataset,
    settings: Settings,
    output: Path,
    split: str = "evaluation",
    variant: str = "A",
    evaluator: ContextualEvaluator | None = None,
) -> EvaluationReport:
    report = evaluate(dataset, settings, split, variant, evaluator)
    write_report(report, output)
    return report


def run_benchmark(
    dataset: ReferenceDataset,
    settings: Settings,
    output: Path,
    split: str = "evaluation",
    evaluator: ContextualEvaluator | None = None,
) -> dict:
    a = evaluate(dataset, settings, split, "A")
    b = evaluate(dataset, settings, split, "B", evaluator)
    decision = decide(a, b, settings)
    write_report(a, output / "A_deterministic")
    write_report(b, output / "B_with_ai")
    write_benchmark(decision, a, b, output)
    (output / "benchmark.json").write_text(json.dumps(decision, indent=2), encoding="utf-8")
    return decision
