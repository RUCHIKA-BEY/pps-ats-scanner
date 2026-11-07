"""Milestone 5: optional AI provider - policy, structured output, evidence checks."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from pps_ats.config import load_settings
from pps_ats.devtools import synthetic
from pps_ats.devtools import synthetic_analysis as sa
from pps_ats.schemas.scoring import ScoreStatus
from pps_ats.scoring.engine import score_file
from pps_ats.scoring.llm import (
    AIConfigError,
    OpenAICompatibleEvaluator,
    build_evaluator,
    check_policy,
)

REAL_QUOTE = "focused on backend development"
FAKE_QUOTE = "won the national hackathon in 2024"


class _Model(BaseHTTPRequestHandler):
    reply: str = "{}"
    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        payload = {"choices": [{"message": {"role": "assistant", "content": type(self).reply}}]}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # keep test output clean
        pass


@pytest.fixture()
def local_model():
    _Model.requests = []
    server = HTTPServer(("127.0.0.1", 0), _Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, _Model
    server.shutdown()


def ai_settings(server, **extra):  # type: ignore[no-untyped-def]
    settings = load_settings()
    settings.scoring.ai = settings.scoring.ai.model_copy(
        update={
            "enabled": True,
            "provider": "local",
            "model": "test-model",
            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            **extra,
        }
    )
    return settings


def model_reply(**kw) -> str:  # type: ignore[no-untyped-def]
    return json.dumps(
        {
            "proposals": kw.get(
                "proposals",
                [
                    {
                        "criterion": "content_relevance",
                        "adjustment": 1,
                        "confidence": 0.9,
                        "rationale": "clear, specific goal",
                        "evidence": [REAL_QUOTE],
                    },
                    {
                        "criterion": "language_professional_tone",
                        "adjustment": -1,
                        "confidence": 0.95,
                        "rationale": "claims an award",
                        "evidence": [FAKE_QUOTE],
                    },
                ],
            ),
            "observations": kw.get(
                "observations",
                [
                    {
                        "aspect": "career_objective",
                        "section": "summary",
                        "evidence": "seeking a software engineering internship",
                        "issue": "Objective does not name a target domain.",
                        "suggestion": "Name the kind of backend role you want.",
                        "severity": "low",
                        "confidence": 0.7,
                    },
                    {
                        "aspect": "project_description",
                        "evidence": FAKE_QUOTE,
                        "issue": "invented",
                        "severity": "high",
                        "confidence": 0.9,
                    },
                ],
            ),
        }
    )


@pytest.fixture(scope="module")
def resume(tmp_path_factory) -> Path:
    return synthetic.resume_pdf(tmp_path_factory.mktemp("ai") / "resume.pdf")


# --------------------------------------------------------------------------- policy


def test_disabled_by_default_builds_no_evaluator(settings):
    assert not settings.scoring.ai.enabled and settings.scoring.ai.provider == "none"
    assert build_evaluator(settings.scoring.ai) is None


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"provider": "local", "base_url": "http://10.0.0.5:11434/v1"}, "loopback"),
        ({"provider": "local", "base_url": "http://models.example.com/v1"}, "loopback"),
        ({"provider": "external", "base_url": "https://api.example.com/v1"}, "disabled"),
        (
            {
                "provider": "external",
                "base_url": "http://api.example.com/v1",
                "allow_external": True,
                "external_authorised_by": "HoD, 2026-11-07",
            },
            "https",
        ),
        (
            {
                "provider": "external",
                "base_url": "https://api.example.com/v1",
                "allow_external": True,
                "external_authorised_by": "HoD, 2026-11-07",
                "api_key_env": "PPS_ATS_TEST_KEY_NOT_SET",
            },
            "not set",
        ),
        ({"provider": "local", "model": ""}, "model is empty"),
    ],
)
def test_policy_refuses_unapproved_destinations(settings, update, message):
    cfg = settings.scoring.ai.model_copy(update={"enabled": True, "model": "m", **update})
    with pytest.raises(AIConfigError, match=message):
        check_policy(cfg)


def test_authorised_external_provider_passes_policy(settings, monkeypatch):
    monkeypatch.setenv("PPS_ATS_AI_API_KEY", "test-key")
    cfg = settings.scoring.ai.model_copy(
        update={
            "enabled": True,
            "provider": "external",
            "model": "m",
            "base_url": "https://api.example.com/v1",
            "allow_external": True,
            "external_authorised_by": "Dean of Academics, 2026-11-07",
        }
    )
    check_policy(cfg)
    assert OpenAICompatibleEvaluator(cfg).model_id == "external:m"


# --------------------------------------------------------------------------- behaviour


def test_evidence_backed_claims_kept_unsupported_rejected(local_model, resume, settings):
    server, model = local_model
    model.reply = model_reply()
    base_analysis, base = score_file(resume, settings)
    analysis, result = score_file(resume, ai_settings(server))
    content = next(c for c in result.ca1.criteria if c.criterion == "content_relevance")
    language = next(c for c in result.ca1.criteria if c.criterion == "language_professional_tone")
    assert content.ai is not None and content.ai.accepted and content.ai.evidence == [REAL_QUOTE]
    assert language.ai is None  # the only quote was invented: the proposal is rejected
    assert language.final_score == next(
        c.final_score for c in base.ca1.criteria if c.criterion == "language_professional_tone"
    )
    assert any("no verifiable evidence" in r for r in result.ai_rejected)
    assert [o.aspect for o in result.ai_observations] == ["career_objective"]
    assert any("evidence not found" in r for r in result.ai_rejected)
    # deterministic findings and validity are untouched
    assert [f.issue_id for f in analysis.findings] == [f.issue_id for f in base_analysis.findings]
    assert analysis.validation == base_analysis.validation
    # structured JSON was requested, deterministically
    body = model.requests[0]
    assert body["response_format"] == {"type": "json_object"} and body["temperature"] == 0


def test_request_is_redacted(local_model, resume):
    server, model = local_model
    model.reply = model_reply(proposals=[], observations=[])
    score_file(resume, ai_settings(server))
    sent = json.dumps(model.requests[0])
    for secret in ("asha.example@example.com", "98765 43210", "ASHA", "EXAMPLE"):
        assert secret not in sent


def test_invalid_document_never_reaches_the_model(local_model, tmp_path):
    server, model = local_model
    model.reply = model_reply()
    doc = synthetic.grade_card_pdf(tmp_path / "grade_card.pdf")
    _, result = score_file(doc, ai_settings(server))
    assert result.status is ScoreStatus.NOT_GRADED and result.ca1 is None
    assert model.requests == []


def test_malformed_output_keeps_deterministic_marks(local_model, resume, settings):
    server, model = local_model
    model.reply = "Sure! The resume looks great, 10/10."
    _, base = score_file(resume, settings)
    _, result = score_file(resume, ai_settings(server))
    assert [c.final_score for c in result.ca1.criteria] == [
        c.final_score for c in base.ca1.criteria
    ]
    assert any(e.action == "ai_failed" for e in result.audit)


def test_model_cannot_exceed_bounds_or_remove_review(local_model, tmp_path):
    server, model = local_model
    model.reply = model_reply(
        proposals=[
            {
                "criterion": "content_relevance",
                "adjustment": 9,
                "confidence": 1.0,
                "evidence": ["Smart Attendance System"],
            }
        ],
        observations=[],
    )
    doc = sa.template_resume_pdf(tmp_path / "template.pdf")
    _, base = score_file(doc, load_settings())
    _, result = score_file(doc, ai_settings(server, max_adjustment=1.0))
    before = base.ca1.criteria[0].final_score
    after = result.ca1.criteria[0].final_score
    assert after <= before + 1.0
    assert result.status is ScoreStatus.NEEDS_REVIEW  # a large proposal requires review
