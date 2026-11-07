"""Milestone 5: reference dataset import, comparison report, split protection, A/B decision.

The reference data are synthetic (see devtools/synthetic_reference.py): these tests check
the evaluation mechanics, not the scanner's real-world accuracy.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from pps_ats.cli import main
from pps_ats.config import load_settings
from pps_ats.devtools.synthetic_reference import build_reference
from pps_ats.evaluation.compare import (
    Detection,
    ErrorStats,
    EvaluationReport,
    IssueCounts,
    error_stats,
    match_document,
    similarity,
)
from pps_ats.evaluation.dataset import (
    DatasetError,
    ReferenceAnnotation,
    ReferenceDocument,
    freeze_split,
    load_dataset,
)
from pps_ats.evaluation.runner import decide, evaluate, run_benchmark, run_evaluation
from pps_ats.scoring.ai import AIEvaluationResponse, CriterionProposal


@pytest.fixture(scope="module")
def reference(tmp_path_factory) -> Path:
    return build_reference(tmp_path_factory.mktemp("ref") / "ref")


@pytest.fixture(scope="module")
def report_a(reference, settings) -> EvaluationReport:
    dataset = load_dataset(reference, labels=set(settings.evaluation.labels))
    return evaluate(dataset, settings, "evaluation", "A")


# --------------------------------------------------------------------------- dataset


def test_ai_generated_marks_and_annotations_are_not_ground_truth(reference, settings):
    dataset = load_dataset(reference, labels=set(settings.evaluation.labels))
    r008 = next(d for d in dataset.documents if d.document_id == "R008")
    assert r008.marks_source == "ai" and not r008.has_faculty_marks
    assert dataset.excluded_ai_annotations == 1
    r003 = next(d for d in dataset.documents if d.document_id == "R003")
    assert all(a.source == "faculty" for a in r003.annotations)
    assert any("not ground truth" in w for w in dataset.warnings)


def _write_docs(root: Path, rows: list[dict]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with (root / "documents.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [
                {"document_id": "A", "file": "a.pdf", "split": "evaluation"},
                {"document_id": "A", "file": "b.pdf", "split": "evaluation"},
            ],
            "duplicate",
        ),
        ([{"document_id": "A", "file": "a.pdf", "split": "train"}], "split"),
        (
            [
                {
                    "document_id": "A",
                    "file": "a.pdf",
                    "split": "evaluation",
                    "faculty_content_relevance": "12",
                }
            ],
            "outside",
        ),
    ],
)
def test_invalid_reference_data_is_rejected(tmp_path, rows, message):
    _write_docs(tmp_path, rows)
    with pytest.raises(DatasetError, match=message):
        load_dataset(tmp_path)


def test_split_is_frozen_and_cannot_be_moved(tmp_path):
    _write_docs(
        tmp_path,
        [{"document_id": f"D{i:03d}", "file": f"d{i}.pdf", "split": ""} for i in range(40)]
        + [{"document_id": "FIXED", "file": "f.pdf", "split": "calibration"}],
    )
    counts = freeze_split(tmp_path, eval_fraction=0.3, seed=1)
    assert sum(counts.values()) == 41 and counts["evaluation"] > 0
    assert (tmp_path / "documents.csv.bak").is_file()
    first = (tmp_path / "documents.csv").read_text(encoding="utf-8")
    freeze_split(tmp_path, eval_fraction=0.9, seed=99)  # re-running never re-assigns
    assert (tmp_path / "documents.csv").read_text(encoding="utf-8") == first
    dataset = load_dataset(tmp_path)
    assert next(d for d in dataset.documents if d.document_id == "FIXED").split == "calibration"
    # moving an evaluation document into calibration after the freeze is refused
    moved = next(d for d in dataset.documents if d.split == "evaluation").document_id
    lines = [
        line.replace("evaluation", "calibration") if line.startswith(moved + ",") else line
        for line in first.splitlines()
    ]
    (tmp_path / "documents.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(DatasetError, match="changed split"):
        load_dataset(tmp_path)


# --------------------------------------------------------------------------- matching


def test_similarity_and_matching():
    assert similarity("Pyhton", "budget planning, with charts written in Pyhton") == 1.0
    assert similarity("City, State", "Pune, Maharashtra") == 0.0
    doc = ReferenceDocument(
        document_id="X",
        file="x.pdf",
        split="evaluation",
        path=Path("x.pdf"),
        annotations_complete=True,
        annotations=[
            ReferenceAnnotation("X", "spelling", "medium", "Pyhton", 1),
            ReferenceAnnotation("X", "spelling", "medium", "Devloped", 1),
            ReferenceAnnotation("X", "placeholder", "high", None, 1),
        ],
    )
    found = [
        Detection("spelling", "LANG_SPELLING", "Pyhton", 1, "medium", False),
        Detection("spelling", "LANG_SPELLING", "recieve", 1, "medium", False),
        Detection("placeholder", "PH_CITY_STATE", "City, State", 1, "high", True),
        Detection("formatting", "FMT_LARGE_GAP", "EXPERIENCE", 1, "medium", False),
    ]
    rows = match_document(doc, found, 0.5)
    outcomes = sorted((r["label"], r["outcome"]) for r in rows)
    assert outcomes == [
        ("formatting", "fp"),
        ("placeholder", "tp"),  # no faculty quote: matched by label + page
        ("spelling", "fn"),  # Devloped missed
        ("spelling", "fp"),  # recieve not annotated
        ("spelling", "tp"),
    ]
    doc.annotations_complete = False
    assert {r["outcome"] for r in match_document(doc, found, 0.5)} >= {"fp_unverified"}


def test_error_stats():
    st = error_stats([(8, 7), (6, 8), (9, 9)], 1.0)
    assert st.n == 3 and st.mae == 1.0 and st.bias == pytest.approx(-0.333, abs=1e-3)
    assert st.max_abs == 2 and st.within == pytest.approx(0.667, abs=1e-3)


# --------------------------------------------------------------------------- report


def test_comparison_report_contents(report_a):
    r = report_a
    assert r.held_out and r.split == "evaluation" and r.documents == 6
    assert r.validation_confusion["invalid"] == {"invalid": 1}  # grade card rejected
    assert r.validation_confusion["needs_review"] == {"needs_review": 1}
    # AI marks excluded, invalid doc has no marks, scan was graded by faculty only
    assert r.score_exclusions == {
        "ai_or_unknown_marks": 1,
        "no_faculty_marks": 1,
        "not_graded_by_scanner": 1,
    }
    assert r.total_error.n == 3
    for row in r.score_rows:
        assert row["diff_total"] == pytest.approx(row["scanner_total"] - row["faculty_total"])
    spelling = r.issue_metrics["spelling"]
    assert spelling.tp == 3 and spelling.fn == 0
    assert r.issue_metrics["grammar"].fn == 1  # contextual grammar the scanner cannot see
    assert r.high_severity.fn >= 1
    assert any(c["document_id"] == "R006" for c in r.uncertain_cases)  # scanned resume
    assert r.fine_tuning_readiness["sufficient_for_fine_tuning"] is False


def test_calibration_split_is_labelled_in_sample(reference, settings):
    dataset = load_dataset(reference, labels=set(settings.evaluation.labels))
    report = evaluate(dataset, settings, "calibration", "A")
    assert not report.held_out and report.warnings[0].startswith("IN-SAMPLE")


def test_report_files(reference, settings, tmp_path):
    dataset = load_dataset(reference, labels=set(settings.evaluation.labels))
    run_evaluation(dataset, settings, tmp_path / "rep")
    data = json.loads((tmp_path / "rep" / "evaluation.json").read_text(encoding="utf-8"))
    assert data["variant"].startswith("A")
    wb = load_workbook(tmp_path / "rep" / "evaluation.xlsx")
    assert wb.sheetnames == [
        "Summary",
        "Validation Errors",
        "Issue Metrics",
        "Issue Detail",
        "Criterion Errors",
        "Score Detail",
        "Uncertain Cases",
    ]
    md = (tmp_path / "rep" / "evaluation.md").read_text(encoding="utf-8")
    assert "Document validation" in md and "high severity" in md


# --------------------------------------------------------------------------- A vs B


class ScriptedModel:
    """Stands in for a model: proposes moving each criterion towards a target."""

    model_id = "scripted"

    def __init__(self, delta: float, quote: str = "Python") -> None:
        self.delta, self.quote = delta, quote

    def evaluate(self, request):  # type: ignore[no-untyped-def]
        return AIEvaluationResponse(
            model_id=self.model_id,
            proposals=[
                CriterionProposal(
                    criterion=c, adjustment=self.delta, confidence=0.9, evidence=[self.quote]
                )
                for c in request.deterministic_scores
            ],
        )


def test_benchmark_keeps_ai_disabled_without_enough_evidence(reference, settings, tmp_path):
    dataset = load_dataset(reference, labels=set(settings.evaluation.labels))
    decision = run_benchmark(dataset, settings, tmp_path / "bench", evaluator=ScriptedModel(-1))
    assert decision["decision"] == "keep_ai_disabled"
    assert any("faculty-marked documents" in r for r in decision["reasons"])
    assert (tmp_path / "bench" / "benchmark.md").is_file()
    assert (tmp_path / "bench" / "A_deterministic" / "evaluation.xlsx").is_file()
    assert (tmp_path / "bench" / "B_with_ai" / "evaluation.json").is_file()
    assert not load_settings().scoring.ai.enabled  # never switched on by the benchmark


def _report(errors: list[float], crit_mae: float, fn: int = 0, held_out: bool = True):  # type: ignore[no-untyped-def]
    rep = EvaluationReport(
        variant="x",
        split="evaluation" if held_out else "calibration",
        held_out=held_out,
        dataset_fingerprint="f",
        created_at="t",
        engine_version="0",
        rubric_version="r",
        scoring_fingerprint="s",
    )
    rep.score_rows = [{"document_id": f"D{i}", "diff_total": e} for i, e in enumerate(errors)]
    rep.total_error = error_stats([(e, 0.0) for e in errors], 2.0)
    rep.criterion_errors = {
        c: ErrorStats(n=len(errors), mae=crit_mae)
        for c in ("content_relevance", "structure_formatting", "language_professional_tone")
    }
    rep.high_severity = IssueCounts(fn=fn)
    return rep


def test_decision_rule(settings):
    a = _report([3.0, -2.5, 2.0, -3.0] * 10, crit_mae=1.0)  # 40 documents
    better = _report([1.0, -1.0, 0.5, -1.5] * 10, crit_mae=0.6)
    assert decide(a, better, settings)["decision"] == "eligible_for_faculty_pilot"
    worse = _report([3.5, -3.0, 2.5, -3.0] * 10, crit_mae=1.2)
    assert decide(a, worse, settings)["decision"] == "keep_ai_disabled"
    missed = _report([1.0, -1.0, 0.5, -1.5] * 10, crit_mae=0.6, fn=3)
    assert "more high-severity issues missed" in decide(a, missed, settings)["reasons"]
    in_sample = _report([3.0, -2.5, 2.0, -3.0] * 10, crit_mae=1.0, held_out=False)
    assert "not a held-out split" in decide(in_sample, better, settings)["reasons"]


def test_cli_evaluate_and_split(reference, tmp_path):
    assert main(["eval-split", str(reference)]) == 0
    assert (reference / "split_lock.json").is_file()
    assert main(["evaluate", str(reference), "-o", str(tmp_path / "ev")]) == 0
    assert (tmp_path / "ev" / "evaluation.md").is_file()
    # variant B without a configured provider is refused, not silently run as A
    assert main(["evaluate", str(reference), "-o", str(tmp_path / "b"), "--variant", "B"]) == 2
    assert main(["evaluate", str(reference), "-o", str(tmp_path / "OneDrive - X" / "o")]) == 2
