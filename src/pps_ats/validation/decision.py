"""Combine issues + classification into the final validation status.

Rules:
  any ERROR issue                         -> invalid
  any WARNING issue or class != resume    -> needs_review
  (image-only / outline-text documents raise a WARNING, not an ERROR: a possibly genuine
  but unreadable resume goes to manual review instead of automatic rejection)
  otherwise                               -> valid
Only ``valid`` documents classified as ``resume`` are eligible for grading.
"""

from __future__ import annotations

from pps_ats.schemas.common import DocumentClass, Issue, Severity, ValidationStatus
from pps_ats.schemas.result import ClassificationResult, ValidationResult


def classification_issues(result: ClassificationResult) -> list[Issue]:
    signals = result.signals
    if result.document_class is DocumentClass.NON_RESUME:
        found = signals.get("negative_phrases") or []
        detail = f" Indicators: {', '.join(found)}." if found else ""
        return [
            Issue(
                code="NOT_A_RESUME",
                severity=Severity.ERROR,
                message=f"Document does not appear to be a resume.{detail}",
                details={
                    "resume_score": result.resume_score,
                    "non_resume_score": result.non_resume_score,
                },
            )
        ]
    if result.document_class is DocumentClass.UNCERTAIN:
        return [
            Issue(
                code="CLASSIFICATION_UNCERTAIN",
                severity=Severity.WARNING,
                message="Could not confirm the document is a resume; manual review required.",
                details={
                    "resume_score": result.resume_score,
                    "non_resume_score": result.non_resume_score,
                },
            )
        ]
    return []


def decide(issues: list[Issue], classification: ClassificationResult) -> ValidationResult:
    severities = {issue.severity for issue in issues}
    if Severity.ERROR in severities:
        status = ValidationStatus.INVALID
    elif (
        Severity.WARNING in severities or classification.document_class is not DocumentClass.RESUME
    ):
        status = ValidationStatus.NEEDS_REVIEW
    else:
        status = ValidationStatus.VALID

    review_reasons = [
        f"{issue.code}: {issue.message}" + (f" (page {issue.page})" if issue.page else "")
        for issue in issues
        if issue.severity in (Severity.ERROR, Severity.WARNING)
    ]
    return ValidationResult(
        status=status,
        eligible_for_grading=(
            status is ValidationStatus.VALID
            and classification.document_class is DocumentClass.RESUME
        ),
        issues=issues,
        review_reasons=review_reasons,
        classification=classification,
    )
