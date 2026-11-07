"""Optional AI providers for contextual assessment (Milestone 5).

One implementation covers both cases through the OpenAI-compatible chat API:

* ``local``    - a server on this computer (Ollama, llama.cpp ``llama-server``, LM Studio).
                 The base URL must be a loopback address; nothing leaves the machine.
* ``external`` - a remote provider. Refused unless ``allow_external = true``, an approver is
                 recorded in ``external_authorised_by`` and the API key is in the environment.

Only redacted section text is sent (see ``engine._ai_request``). The model must answer
with JSON; every claim must quote the text verbatim, which the engine verifies
(``ai.verify_response``). Output that is not valid JSON is treated as a model failure and
the deterministic marks are kept.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from ipaddress import ip_address
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError

from pps_ats.config import AIScoringSettings
from pps_ats.schemas.scoring import AIObservation
from pps_ats.scoring.ai import (
    AIEvaluationRequest,
    AIEvaluationResponse,
    ContextualEvaluator,
    CriterionProposal,
)

LOOPBACK_NAMES = {"localhost"}

SYSTEM_PROMPT = """You assist university faculty who grade student resumes with a rubric.
A deterministic scanner has already checked validity, sections, placeholders, formatting and
spelling. You only judge context the scanner cannot: career-objective clarity and relevance,
quality of project descriptions, professional tone and contextual grammar.

Rules:
- Use only the resume text provided. Never invent facts.
- Every claim needs evidence: copy an exact, contiguous quote (at least a few words) from the
  text. Claims without an exact quote are discarded.
- You do not decide validity or final marks. You may propose an adjustment between -1 and +1
  to a criterion's deterministic score, with a confidence between 0 and 1.
- Propose 0 when the deterministic score already fits.
- Answer with one JSON object only, using exactly this shape:
{"proposals": [{"criterion": "content_relevance|structure_formatting|language_professional_tone",
  "adjustment": 0.0, "confidence": 0.0, "rationale": "...", "evidence": ["exact quote"]}],
 "observations": [{
  "aspect": "career_objective|project_description|professional_tone|contextual_grammar",
  "section": "...", "evidence": "exact quote", "issue": "...", "suggestion": "...",
  "severity": "low|medium|high", "confidence": 0.0}]}"""


class AIConfigError(ValueError):
    pass


class _ModelOutput(BaseModel):
    proposals: list[CriterionProposal] = Field(default_factory=list)
    observations: list[AIObservation] = Field(default_factory=list)


def _is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").strip("[]").lower()
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def check_policy(cfg: AIScoringSettings) -> None:
    """Raise ``AIConfigError`` if the configuration would send data where it may not go."""
    if cfg.provider not in ("local", "external"):
        raise AIConfigError(f"unknown AI provider '{cfg.provider}' (use 'local' or 'external')")
    if not cfg.model:
        raise AIConfigError("[scoring.ai] model is empty")
    scheme = urlparse(cfg.base_url).scheme
    if scheme not in ("http", "https"):
        raise AIConfigError(f"unsupported URL scheme '{scheme}'")
    if cfg.provider == "local" and not _is_loopback(cfg.base_url):
        raise AIConfigError(
            "provider 'local' requires a loopback base_url (127.0.0.1 / localhost / ::1); "
            f"got {urlparse(cfg.base_url).hostname!r}"
        )
    if cfg.provider == "external":
        if not cfg.allow_external or not cfg.external_authorised_by.strip():
            raise AIConfigError(
                "external AI providers are disabled: set allow_external = true and record "
                "who approved it in external_authorised_by"
            )
        if scheme != "https":
            raise AIConfigError("external providers must use https")
        if not os.environ.get(cfg.api_key_env):
            raise AIConfigError(f"API key environment variable {cfg.api_key_env} is not set")


def _extract_json(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model reply contains no JSON object")
    return json.loads(text[start : end + 1])


def build_messages(request: AIEvaluationRequest) -> list[dict[str, str]]:
    payload = {
        "rubric_version": request.rubric_version,
        "rubric": request.rubric,
        "aspects": request.aspects,
        "deterministic_scores": request.deterministic_scores,
        "scanner_findings": request.finding_rules,
        "resume_sections": request.sections,
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


class OpenAICompatibleEvaluator:
    """``ContextualEvaluator`` for OpenAI-compatible chat endpoints."""

    def __init__(self, cfg: AIScoringSettings) -> None:
        check_policy(cfg)
        self.cfg = cfg
        self.model_id = f"{cfg.provider}:{cfg.model}"
        self.endpoint = cfg.base_url.rstrip("/") + "/chat/completions"

    def _post(self, body: dict) -> dict:
        headers = {"Content-Type": "application/json", "User-Agent": "pps-ats-scanner"}
        if self.cfg.provider == "external":
            headers["Authorization"] = f"Bearer {os.environ[self.cfg.api_key_env]}"
        req = urllib.request.Request(
            self.endpoint, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=self.cfg.request_timeout_s) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))

    def evaluate(self, request: AIEvaluationRequest) -> AIEvaluationResponse:
        body = {
            "model": self.cfg.model,
            "messages": build_messages(request),
            "temperature": self.cfg.temperature,
            "seed": self.cfg.seed,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        try:
            reply = self._post(body)
            content = reply["choices"][0]["message"]["content"]
            output = _ModelOutput.model_validate(_extract_json(content))
        except (urllib.error.URLError, KeyError, IndexError, ValueError, ValidationError) as exc:
            raise RuntimeError(f"AI provider error: {type(exc).__name__}") from exc
        return AIEvaluationResponse(
            model_id=self.model_id,
            proposals=output.proposals,
            observations=output.observations,
        )


def build_evaluator(cfg: AIScoringSettings) -> ContextualEvaluator | None:
    """The configured evaluator, or ``None`` when AI is disabled (the default)."""
    if not cfg.enabled or cfg.provider == "none":
        return None
    return OpenAICompatibleEvaluator(cfg)
