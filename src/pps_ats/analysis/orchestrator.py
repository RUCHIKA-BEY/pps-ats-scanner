"""Analysis orchestrator: validated scan -> eligible modules -> deduplicated findings.

* Invalid documents and non-resumes are not analysed (``analysis_status = skipped``).
* Every module runs in isolation: a failure is recorded, the others still run, and the
  document is routed to manual review.
* Validation status and review reasons from Milestone 1 are preserved verbatim.
* No marks are computed. ``grading.eligible_for_grading`` only tells a later scoring stage
  whether it may grade the document without a human review first.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pps_ats import __version__
from pps_ats.analysis.completeness import assess_sections
from pps_ats.analysis.contact_checks import check_contact
from pps_ats.analysis.content_quality import check_content
from pps_ats.analysis.context import AnalysisContext, ManifestRecord, build_context
from pps_ats.analysis.formatting import check_formatting
from pps_ats.analysis.identity import analyze_identity
from pps_ats.analysis.language import check_grammar, check_spelling
from pps_ats.analysis.layout_checks import check_layout
from pps_ats.analysis.placeholders import detect_placeholders
from pps_ats.config import Settings, default_settings
from pps_ats.pipeline import scan_file
from pps_ats.schemas.common import DocumentClass, ProcessingError, ValidationStatus
from pps_ats.schemas.findings import (
    AnalysisResult,
    Finding,
    FindingClass,
    FindingSeverity,
    FindingSummary,
    GradingGate,
    IdentityResult,
    ModuleRun,
    SectionAssessment,
)
from pps_ats.schemas.result import ScanResult

log = logging.getLogger(__name__)

_CLASS_ORDER = {c: i for i, c in enumerate(FindingClass)}
_SEVERITY_ORDER = {s: i for i, s in enumerate(FindingSeverity)}
# A project-level "no description" finding already covers the empty "Description:" label.
_SUPERSEDES = {"CQ_PROJECT_NO_DESCRIPTION": {"PH_EMPTY_FIELD"}}
ANALYSABLE_CLASSES = {DocumentClass.RESUME, DocumentClass.UNCERTAIN}


class _State:
    def __init__(self) -> None:
        self.sections: list[SectionAssessment] = []
        self.identity: IdentityResult | None = None


def _run_sections(ctx: AnalysisContext, state: _State) -> list[Finding]:
    state.sections, findings = assess_sections(ctx)
    return findings


def _run_identity(ctx: AnalysisContext, state: _State) -> list[Finding]:
    state.identity, findings = analyze_identity(ctx)
    ctx.cache["name_block_id"] = state.identity.extracted.name_block_id
    return findings


# Order matters: placeholders feed completeness/content/spelling; identity feeds spelling.
MODULES: list[tuple[str, Callable[[AnalysisContext, _State], list[Finding]]]] = [
    ("placeholders", lambda ctx, _s: detect_placeholders(ctx)),
    ("section_completeness", _run_sections),
    ("identity", _run_identity),
    ("contact", lambda ctx, _s: check_contact(ctx)),
    ("formatting", lambda ctx, _s: check_formatting(ctx)),
    ("layout", lambda ctx, _s: check_layout(ctx)),
    ("content_quality", lambda ctx, _s: check_content(ctx)),
    ("spelling", lambda ctx, _s: check_spelling(ctx)),
    ("grammar", lambda ctx, _s: check_grammar(ctx)),
]


def _line_key(finding: Finding) -> str | None:
    if finding.span_id:
        return finding.span_id.rsplit(":s", 1)[0]
    return None


def _postprocess(findings: list[Finding], ctx: AnalysisContext) -> tuple[list[Finding], dict]:
    # 1. superseded findings
    covered: dict[str, set[str]] = {}
    for f in findings:
        for rule in _SUPERSEDES.get(f.rule_id, ()):
            covered.setdefault(rule, set()).update(f.evidence.get("entry_lines") or [])
    findings = [
        f for f in findings if not (f.rule_id in covered and _line_key(f) in covered[f.rule_id])
    ]

    # 2. exact duplicates
    unique: dict[tuple, Finding] = {}
    for f in findings:
        key = (
            f.rule_id,
            f.block_id,
            f.span_id,
            f.page_number,
            f.bounding_box,
            (f.detected_text or "").lower(),
            f.section,
        )
        unique.setdefault(key, f)
    findings = list(unique.values())

    # 3. per-rule cap
    cap = ctx.settings.analysis.max_findings_per_rule
    per_rule: Counter[str] = Counter()
    kept, truncated = [], Counter()
    for f in findings:
        per_rule[f.rule_id] += 1
        if per_rule[f.rule_id] <= cap:
            kept.append(f)
        else:
            truncated[f.rule_id] += 1

    # 4. stable order + deterministic ids
    order = {b.id: b.reading_order for b in ctx.content.blocks}
    kept.sort(
        key=lambda f: (
            _CLASS_ORDER[f.finding_class],
            _SEVERITY_ORDER[f.severity],
            f.page_number if f.page_number is not None else 10**6,
            order.get(f.block_id or "", 10**6),
            f.rule_id,
        )
    )
    seen_ids: Counter[str] = Counter()
    for f in kept:
        digest = hashlib.sha1(
            "|".join(
                str(x)
                for x in (
                    f.rule_id,
                    f.block_id,
                    f.span_id,
                    f.page_number,
                    f.bounding_box,
                    f.detected_text,
                    f.section,
                )
            ).encode()
        ).hexdigest()[:8]
        base = f"{f.rule_id}-{digest}"
        seen_ids[base] += 1
        f.issue_id = base if seen_ids[base] == 1 else f"{base}-{seen_ids[base]}"
    return kept, dict(truncated)


def summarize(findings: list[Finding]) -> FindingSummary:
    return FindingSummary(
        total=len(findings),
        review_required=sum(1 for f in findings if f.review_required),
        by_class=dict(Counter(f.finding_class.value for f in findings)),
        by_severity=dict(Counter(f.severity.value for f in findings)),
        by_category=dict(Counter(f.category.value for f in findings)),
        by_rubric_criterion=dict(Counter(f.rubric_criterion.value for f in findings)),
    )


def analyze_scan(
    scan: ScanResult,
    settings: Settings | None = None,
    manifest: ManifestRecord | None = None,
    started: float | None = None,
) -> AnalysisResult:
    settings = settings or default_settings()
    started = started if started is not None else time.perf_counter()
    validation = scan.validation
    doc_class = validation.classification.document_class

    skip_reason = None
    if validation.status is ValidationStatus.INVALID:
        skip_reason = "document is invalid; it is not analysed or graded"
    elif scan.content is None or not scan.sections:
        skip_reason = "no readable text to analyse (manual review)"
    elif doc_class not in ANALYSABLE_CLASSES:
        skip_reason = f"document class '{doc_class.value}' is not analysed"

    findings: list[Finding] = []
    modules: list[ModuleRun] = []
    errors: list[ProcessingError] = list(scan.errors)
    state = _State()
    truncated: dict = {}
    if skip_reason is None:
        ctx = build_context(scan, settings, manifest)
        for name, run in MODULES:
            try:
                produced = run(ctx, state)
            except Exception as exc:  # isolate module failures; never crash a batch
                log.exception(
                    "analysis module failed module=%s file=%s", name, scan.document.file_name
                )
                modules.append(
                    ModuleRun(
                        name=name, status="failed", detail=f"{type(exc).__name__}: {str(exc)[:200]}"
                    )
                )
                errors.append(
                    ProcessingError(
                        stage=f"analysis:{name}",
                        error_type=type(exc).__name__,
                        message=str(exc)[:300],
                    )
                )
                continue
            findings.extend(produced)
            modules.append(ModuleRun(name=name, status="completed", finding_count=len(produced)))
        findings, truncated = _postprocess(findings, ctx)
    else:
        modules = [
            ModuleRun(name=name, status="skipped", detail=skip_reason) for name, _ in MODULES
        ]

    failed = [m.name for m in modules if m.status == "failed"]
    flagged = [f for f in findings if f.review_required]
    review_reasons = list(validation.review_reasons)
    review_reasons += [f"{f.rule_id}: {f.explanation}" for f in flagged]
    review_reasons += [f"ANALYSIS_MODULE_FAILED: {name}" for name in failed]
    if truncated:
        review_reasons.append(
            "FINDINGS_TRUNCATED: " + ", ".join(f"{k} (+{v})" for k, v in sorted(truncated.items()))
        )

    blocked_by: list[str] = []
    if validation.status is not ValidationStatus.VALID:
        blocked_by.append(f"validation_status:{validation.status.value}")
    if doc_class is not DocumentClass.RESUME:
        blocked_by.append(f"document_class:{doc_class.value}")
    if skip_reason:
        blocked_by.append("analysis_skipped")
    blocked_by += [f"review:{f.rule_id}" for f in flagged]
    blocked_by += [f"module_failed:{name}" for name in failed]
    blocked_by = list(dict.fromkeys(blocked_by))

    status = "skipped" if skip_reason else ("partial" if failed else "completed")
    result = AnalysisResult(
        scan_schema_version=scan.schema_version,
        engine_version=__version__,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        processing_ms=int((time.perf_counter() - started) * 1000),
        config_source=settings.source,
        analysis_status=status,
        skipped_reason=skip_reason,
        document=scan.document,
        validation=validation,
        grading=GradingGate(
            eligible_for_grading=validation.eligible_for_grading and not blocked_by,
            graded=False,
            blocked_by=blocked_by,
        ),
        review_required=bool(validation.status is not ValidationStatus.VALID or flagged or failed),
        review_reasons=review_reasons,
        sections=state.sections,
        identity=state.identity,
        findings=findings,
        summary=summarize(findings),
        modules=modules,
        errors=errors,
    )
    log.info(
        "analysed file=%s status=%s findings=%d review=%s ms=%d",
        scan.document.file_name,
        status,
        len(findings),
        result.review_required,
        result.processing_ms,
    )
    return result


def analyze_file(
    path: str | Path,
    settings: Settings | None = None,
    manifest: dict[str, ManifestRecord] | None = None,
) -> AnalysisResult:
    settings = settings or default_settings()
    started = time.perf_counter()
    path = Path(path)
    scan = scan_file(path, settings)
    record = manifest.get(path.name.lower()) if manifest else None
    return analyze_scan(scan, settings, record, started)
