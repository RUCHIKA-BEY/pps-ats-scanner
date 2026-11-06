"""Milestone 3: provisional CA1 scoring, ATS score, AI interface, faculty review, audit.

Assertions are relational (worse evidence => lower marks, invariants, caps), not exact
marks for a fixture, so recalibrating the provisional weights does not break them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pps_ats.cli import main
from pps_ats.config import load_settings
from pps_ats.devtools import synthetic
from pps_ats.devtools import synthetic_analysis as sa
from pps_ats.schemas.scoring import ContributionStatus, ScoreStatus
from pps_ats.scoring.ai import AIEvaluationResponse, CriterionProposal, redact
from pps_ats.scoring.engine import score_analysis, score_file
from pps_ats.scoring.review import AuditLog, ReviewError, apply_override, approve, manual_grade

CRITERIA = ("content_relevance", "structure_formatting", "language_professional_tone")


@pytest.fixture(scope="session")
def docs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("scoring")
    paths = {name: build(d / name) for name, build in sa.ANALYSIS_SAMPLES.items()}
    paths["resume.pdf"] = synthetic.resume_pdf(d / "resume.pdf")
    paths["two_column.pdf"] = synthetic.two_column_resume_pdf(d / "two_column.pdf")
    paths["grade_card.pdf"] = synthetic.grade_card_pdf(d / "grade_card.pdf")
    paths["image_only.pdf"] = synthetic.image_only_pdf(d / "image_only.pdf")
    paths["mismatch"] = synthetic.resume_pdf(d / "Priya_Desai_Resume.pdf")
    return paths


@pytest.fixture(scope="session")
def scored(docs, settings):
    cache: dict[str, tuple] = {}

    def get(name: str):
        if name not in cache:
            cache[name] = score_file(docs[name], settings)
        return cache[name]

    return get


def marks(result) -> dict[str, float]:
    return {c.criterion: c.final_score for c in result.ca1.criteria}


# --------------------------------------------------------------------------- invariants


@pytest.mark.parametrize(
    "name", ["resume.pdf", "template_resume.pdf", "mixed_fonts.pdf", "template_resume.docx"]
)
def test_score_invariants(scored, settings, name):
    _, result = scored(name)
    assert result.ca1 is not None
    assert [c.criterion for c in result.ca1.criteria] == list(CRITERIA)
    for c in result.ca1.criteria:
        crit = settings.scoring.criteria[c.criterion]
        assert crit.floor <= c.final_score <= crit.max_points
        assert c.deterministic_score <= crit.ceiling
        assert (c.final_score / settings.scoring.rounding) % 1 == 0
    assert result.ca1.total == pytest.approx(sum(marks(result).values()))
    assert result.ca1.max_total == 30
    assert 0 <= result.ats.score <= 100


def test_clean_resume_scores_high_but_content_needs_judgement(scored, settings):
    _, result = scored("resume.pdf")
    assert result.status is ScoreStatus.PROVISIONAL and result.releasable
    m = marks(result)
    assert m["structure_formatting"] >= 9 and m["language_professional_tone"] >= 9
    content = result.ca1.criteria[0]
    # Tailoring to a role cannot be verified deterministically: capped below 10.
    assert content.final_score <= settings.scoring.criteria["content_relevance"].ceiling
    assert content.ceiling_note


def test_defects_lower_the_right_criterion(scored):
    clean = marks(scored("resume.pdf")[1])
    assert marks(scored("template_resume.pdf")[1])["content_relevance"] < clean["content_relevance"]
    assert (
        marks(scored("missing_sections.pdf")[1])["content_relevance"] < clean["content_relevance"]
    )
    mixed = marks(scored("mixed_fonts.pdf")[1])
    assert mixed["structure_formatting"] < clean["structure_formatting"]
    assert mixed["language_professional_tone"] == clean["language_professional_tone"]
    spelling = marks(scored("spelling_resume.pdf")[1])
    assert spelling["language_professional_tone"] < clean["language_professional_tone"]


def test_two_column_resume_not_penalised_for_layout(scored):
    _, result = scored("two_column.pdf")
    assert marks(result)["structure_formatting"] == 10
    assert result.ats.score == 100


# --------------------------------------------------------------------------- duplicates


def test_section_level_finding_absorbs_line_level_placeholder(scored):
    analysis, result = scored("template_resume.pdf")
    status = {c.rule_id: c for c in result.contributions}
    assert status["SEC_PLACEHOLDER_ONLY"].status is ContributionStatus.APPLIED
    absorbed = status["PH_BRACKETED_INSTRUCTION"]
    assert absorbed.status is ContributionStatus.ABSORBED
    assert absorbed.applied_points == 0 and "covered by" in absorbed.reason
    # Every finding appears at most once in the contributions.
    ids = [c.issue_id for c in result.contributions]
    assert len(ids) == len(set(ids))


def test_distinct_errors_on_one_line_both_count(scored):
    _, result = scored("spelling_resume.pdf")
    applied = [
        c
        for c in result.contributions
        if c.rule_id == "LANG_SPELLING" and c.status is ContributionStatus.APPLIED
    ]
    assert len(applied) >= 2  # "Devloped" and "dashbord" share a PDF line


def test_group_cap_limits_one_problem_type(scored, settings):
    _, result = scored("template_resume.docx")
    content = result.ca1.criteria[0]
    placeholder = next(g for g in content.groups if g.group == "placeholder")
    assert placeholder.applied_points <= settings.scoring.groups["placeholder"].max_points
    assert placeholder.capped and placeholder.raw_points > placeholder.applied_points


def test_ats_only_findings_do_not_change_ca1(docs, settings):
    analysis, result = score_file(docs["resume.pdf"], settings)
    layout = type(analysis.findings[0]).model_validate(
        {
            **analysis.findings[0].model_dump(),
            "issue_id": "LAYOUT_TEXT_BOX-test",
            "rule_id": "LAYOUT_TEXT_BOX",
            "category": "layout",
            "rubric_criterion": "none",
            "ats_impact": True,
            "severity": "medium",
        }
    )
    with_layout = analysis.model_copy(update={"findings": [*analysis.findings, layout]})
    rescored = score_analysis(with_layout, settings)
    assert marks(rescored) == marks(result)
    assert rescored.ats.score < result.ats.score


def test_weights_are_configurable(docs, tmp_path):
    override = tmp_path / "strict.toml"
    override.write_text(
        "[scoring.rules.CQ_PROJECT_NO_DESCRIPTION]\npoints = 3.0\n", encoding="utf-8"
    )
    default = marks(score_file(docs["template_resume.pdf"], load_settings())[1])
    strict_settings = load_settings(override)
    strict = score_file(docs["template_resume.pdf"], strict_settings)[1]
    assert marks(strict)["content_relevance"] < default["content_relevance"]
    assert strict.config_hash != score_file(docs["template_resume.pdf"])[1].config_hash


def test_scoring_is_deterministic(docs, settings):
    a = score_file(docs["template_resume.pdf"], settings)[1]
    b = score_file(docs["template_resume.pdf"], settings)[1]
    assert marks(a) == marks(b) and a.config_hash == b.config_hash
    assert [c.model_dump() for c in a.contributions] == [c.model_dump() for c in b.contributions]


# --------------------------------------------------------------------------- status


def test_invalid_and_unreadable_documents_get_no_marks(scored):
    for name in ("grade_card.pdf", "image_only.pdf"):
        _, result = scored(name)
        assert result.status is ScoreStatus.NOT_GRADED
        assert result.ca1 is None and result.ats is None and not result.releasable
        assert result.review_reasons


def test_identity_mismatch_needs_review_not_deduction(scored):
    _, mismatch = scored("mismatch")
    _, clean = scored("resume.pdf")
    assert mismatch.status is ScoreStatus.NEEDS_REVIEW and not mismatch.releasable
    assert marks(mismatch) == marks(clean)  # no arbitrary mark deduction
    assert any("IDENTITY_POSSIBLE_MISMATCH" in r for r in mismatch.review_reasons)


# --------------------------------------------------------------------------- AI


class FakeEvaluator:
    model_id = "fake-local-1"

    def __init__(self, proposals):
        self.proposals = proposals
        self.requests = []

    def evaluate(self, request):
        self.requests.append(request)
        return AIEvaluationResponse(model_id=self.model_id, proposals=self.proposals)


def _ai_settings(**ai):
    settings = load_settings()
    settings.scoring.ai = settings.scoring.ai.model_copy(update={"enabled": True, **ai})
    return settings


def test_ai_disabled_by_default_is_never_called(docs, settings):
    evaluator = FakeEvaluator(
        [CriterionProposal(criterion="content_relevance", adjustment=1, confidence=1)]
    )
    _, result = score_file(docs["resume.pdf"], settings, evaluator=evaluator)
    assert evaluator.requests == [] and not result.ai_enabled
    assert all(c.ai is None for c in result.ca1.criteria)


def test_ai_adjustment_is_bounded_and_confidence_gated(docs):
    settings = _ai_settings(max_adjustment=1.0, min_confidence=0.7, review_disagreement=1.5)
    evaluator = FakeEvaluator(
        [
            CriterionProposal(
                criterion="content_relevance", adjustment=3.0, confidence=0.9, rationale="tailored"
            ),
            CriterionProposal(
                criterion="language_professional_tone", adjustment=-1.0, confidence=0.4
            ),
        ]
    )
    _, base = score_file(docs["resume.pdf"], load_settings())
    _, result = score_file(docs["resume.pdf"], settings, evaluator=evaluator)
    content = result.ca1.criteria[0]
    assert content.ai.accepted and "clipped" in content.ai.reason
    assert content.final_score <= base.ca1.criteria[0].final_score + 1.0
    assert content.final_score <= 10  # AI may exceed the deterministic ceiling, not the max
    language = result.ca1.criteria[2]
    assert not language.ai.accepted and language.final_score == base.ca1.criteria[2].final_score
    assert result.status is ScoreStatus.NEEDS_REVIEW  # |3.0| >= review_disagreement
    assert any(e.action == "ai_evaluated" for e in result.audit)


def test_ai_request_is_redacted(docs):
    evaluator = FakeEvaluator([])
    score_file(docs["template_resume.pdf"], _ai_settings(), evaluator=evaluator)
    payload = evaluator.requests[0].model_dump_json()
    for secret in ("meera.testcase@example.com", "90000 11111", "MEERA", "TESTCASE"):
        assert secret not in payload
    assert redact("Call +91 90000 11111", [], []) == "Call [PHONE]"


def test_ai_failure_keeps_deterministic_marks(docs, settings):
    class Broken:
        model_id = "broken"

        def evaluate(self, request):
            raise RuntimeError("model offline")

    _, base = score_file(docs["resume.pdf"], settings)
    _, result = score_file(docs["resume.pdf"], _ai_settings(), evaluator=Broken())
    assert marks(result) == marks(base)
    assert any(e.action == "ai_failed" for e in result.audit)


# --------------------------------------------------------------------------- faculty


def test_override_records_audit_and_recomputes_total(scored, settings):
    _, result = scored("mismatch")
    updated = apply_override(
        result,
        "content_relevance",
        7,
        "F042",
        "Identity verified; objective generic",
        settings.scoring,
    )
    content = updated.ca1.criteria[0]
    assert (
        content.final_score == 7
        and content.override.previous_score == result.ca1.criteria[0].final_score
    )
    assert content.deterministic_score == result.ca1.criteria[0].deterministic_score
    assert updated.ca1.total == pytest.approx(sum(marks(updated).values()))
    assert updated.status is ScoreStatus.FACULTY_REVIEWED and updated.releasable
    assert updated.audit[-1].action == "override" and updated.audit[-1].actor == "faculty:F042"
    assert result.status is ScoreStatus.NEEDS_REVIEW  # original object untouched


@pytest.mark.parametrize(
    ("criterion", "score", "reviewer", "reason"),
    [
        ("content_relevance", 11, "F1", "too high"),
        ("content_relevance", 5, "", "no reviewer"),
        ("content_relevance", 5, "F1", ""),
        ("creativity", 5, "F1", "unknown criterion"),
    ],
)
def test_invalid_overrides_rejected(scored, settings, criterion, score, reviewer, reason):
    _, result = scored("resume.pdf")
    with pytest.raises(ReviewError):
        apply_override(result, criterion, score, reviewer, reason, settings.scoring)


def test_manual_grade_for_ungraded_document(scored, settings):
    _, result = scored("image_only.pdf")
    with pytest.raises(ReviewError):
        apply_override(result, "content_relevance", 5, "F1", "scan reviewed", settings.scoring)
    graded = manual_grade(
        result, {c: 6 for c in CRITERIA}, "F1", "Scanned resume read manually", settings.scoring
    )
    assert graded.ca1.total == 18 and graded.status is ScoreStatus.FACULTY_REVIEWED
    assert all(c.deterministic_score is None for c in graded.ca1.criteria)
    with pytest.raises(ReviewError):
        manual_grade(result, {"content_relevance": 6}, "F1", "incomplete", settings.scoring)


def test_approve_resolves_review(scored, settings):
    _, result = scored("mismatch")
    approved = approve(result, "F7", "Identity confirmed with student", settings.scoring)
    assert approved.status is ScoreStatus.FACULTY_REVIEWED and approved.releasable
    assert marks(approved) == marks(result)


def test_audit_log_hash_chain_detects_tampering(scored, settings, tmp_path):
    _, result = scored("mismatch")
    updated = apply_override(
        result, "language_professional_tone", 8, "F042", "Tone is informal", settings.scoring
    )
    log = AuditLog(tmp_path / "audit" / "ca1.jsonl")
    for entry in updated.audit:
        log.append(updated, entry)
    assert log.verify() is None
    lines = log.path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[-1])
    tampered["details"]["new"] = 10
    lines[-1] = json.dumps(tampered)
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert log.verify() == len(lines) - 1


# --------------------------------------------------------------------------- CLI


def test_cli_score_and_review(docs, tmp_path, capsys):
    out = tmp_path / "s.json"
    audit = tmp_path / "audit.jsonl"
    assert (
        main(["score", str(docs["template_resume.pdf"]), "-o", str(out), "--audit-log", str(audit)])
        == 0
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ca1"]["max_total"] == 30 and data["status"] in ("provisional", "needs_review")
    assert "total=" in capsys.readouterr().err
    assert (
        main(
            [
                "review",
                str(out),
                "--reviewer",
                "F9",
                "--reason",
                "Checked manually",
                "--override",
                "content_relevance=6",
                "--audit-log",
                str(audit),
            ]
        )
        == 0
    )
    reviewed = json.loads(out.read_text(encoding="utf-8"))
    assert reviewed["status"] == "faculty_reviewed"
    assert reviewed["ca1"]["criteria"][0]["final_score"] == 6
    assert AuditLog(audit).verify() is None
    assert len(audit.read_text(encoding="utf-8").splitlines()) == 2
    assert (
        main(
            [
                "review",
                str(out),
                "--reviewer",
                "F9",
                "--reason",
                "bad",
                "--override",
                "content_relevance=12",
            ]
        )
        == 2
    )


def test_cli_score_ungraded(docs, tmp_path):
    out = tmp_path / "g.json"
    assert main(["score", str(docs["grade_card.pdf"]), "-o", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["status"] == "not_graded" and data["ca1"] is None
