"""Candidate identity (name / e-mail / phone / student ID) and cross-checks.

Reference, in order of preference:
  1. an optional authoritative manifest row (CSV: file_name, student_name, student_id, email)
  2. tokens in the file name - NOT authoritative (students rename files, use templates)

Outcomes: confirmed_match / possible_mismatch / insufficient_information.
A possible mismatch only raises ``review_required``; it never changes marks.
Extracted values are written to the JSON result only; they are never logged.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from pathlib import Path

from pps_ats.analysis.context import AnalysisContext, ManifestRecord, TextLine, locate, make_finding
from pps_ats.analysis.sections import EMAIL_RE, PROFILE_URL_RE, find_phone_numbers
from pps_ats.schemas.findings import (
    DetectionMethod,
    ExtractedIdentity,
    Finding,
    FindingCategory,
    FindingClass,
    FindingSeverity,
    IdentityReference,
    IdentityResult,
    IdentityStatus,
    RubricCriterion,
)

_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")
_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
_NAME_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z.'’-]*$")
_NAME_LABEL_RE = re.compile(r"^\s*(?:full\s+)?name\s*[:\-]\s*(.+)$", re.IGNORECASE)

_MANIFEST_COLUMNS = {
    "file_name": ("file_name", "filename", "file", "submission", "submission_file"),
    "student_name": ("student_name", "name", "full_name", "candidate_name"),
    "student_id": (
        "student_id",
        "prn",
        "roll_no",
        "roll_number",
        "id",
        "enrollment_no",
        "enrolment_no",
        "registration_no",
    ),
    "email": ("email", "e-mail", "email_id", "mail"),
}


# --------------------------------------------------------------------------- helpers


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def name_tokens(text: str, min_len: int = 1) -> list[str]:
    tokens = [t.lower() for t in _SPLIT_RE.split(_ascii(_CAMEL_RE.sub(" ", text))) if t]
    return [t for t in tokens if t.isalpha() and len(t) >= min_len]


def _tokens_match(doc_tokens: list[str], ref_tokens: list[str], min_len: int) -> list[str]:
    """Overlapping name tokens; tolerates concatenated names ('ruchikapatil')."""
    matched = []
    for d in doc_tokens:
        if len(d) < min_len:
            continue
        for r in ref_tokens:
            if d == r or (len(r) >= 4 and len(d) >= 4 and (d in r or r in d)):
                matched.append(d)
                break
    return matched


def load_manifest(path: str | Path) -> dict[str, ManifestRecord]:
    """Read a CSV manifest. Keys are lower-cased file names."""
    records: dict[str, ManifestRecord] = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return records
        header = {h.strip().lower(): h for h in reader.fieldnames if h}
        columns = {
            field: next((header[a] for a in aliases if a in header), None)
            for field, aliases in _MANIFEST_COLUMNS.items()
        }
        if columns["file_name"] is None:
            raise ValueError("manifest needs a 'file_name' column")
        for row in reader:

            def get(field: str, row: dict = row) -> str | None:
                col = columns[field]
                value = (row.get(col) or "").strip() if col else ""
                return value or None

            file_name = get("file_name")
            if file_name:
                records[file_name.lower()] = ManifestRecord(
                    file_name=file_name,
                    student_name=get("student_name"),
                    student_id=get("student_id"),
                    email=get("email"),
                )
    return records


# --------------------------------------------------------------------------- extraction


def _name_candidate(ctx: AnalysisContext) -> tuple[str | None, float, TextLine | None]:
    cfg = ctx.settings.analysis.identity
    stop = {s.lower() for s in cfg.name_stopwords}
    head = [t for t in ctx.lines if t.section is None][: cfg.name_search_lines]
    if not head:
        head = ctx.lines[:2]
    best: tuple[float, str, TextLine] | None = None
    sizes = [t.size for t in ctx.lines if t.size]
    largest = max(sizes) if sizes else None
    placeholder_lines = ctx.cache.get("placeholder_lines", set())
    for rank, tl in enumerate(head):
        if (tl.block_id, tl.line_index) in placeholder_lines:
            continue  # "Your Name" is template text, not a name
        text = tl.text.strip()
        labelled = _NAME_LABEL_RE.match(text)
        if labelled:
            text = labelled.group(1).strip()
        text = re.split(r"\s[|•·,]\s|\s{3,}", text)[0].strip()
        words = text.split()
        if not 2 <= len(words) <= 5 or any(c.isdigit() or c == "@" for c in text):
            continue
        if not all(_NAME_TOKEN_RE.match(w) for w in words):
            continue
        if any(w.lower().strip(".") in stop for w in words):
            continue
        score = 1.0 - rank * 0.1
        if labelled:
            score += 0.5
        if tl.size and largest and tl.size >= largest - 0.1:
            score += 0.6
        if tl.bold_ratio >= 0.6:
            score += 0.2
        if best is None or score > best[0]:
            best = (score, text, tl)
    if best is None:
        return None, 0.0, None
    return best[1], round(min(0.95, 0.35 + best[0] / 3), 2), best[2]


def extract_identity(ctx: AnalysisContext) -> tuple[ExtractedIdentity, TextLine | None]:
    cfg = ctx.settings.analysis.identity
    text = f"{ctx.content.full_text}\n{ctx.content.header_footer_text}"
    name, confidence, line = _name_candidate(ctx)
    ids: list[str] = []
    for pattern in cfg.student_id_patterns:
        for match in re.finditer(pattern, text):
            value = match.group(1) if match.groups() else match.group(0)
            if value not in ids:
                ids.append(value)
    return (
        ExtractedIdentity(
            candidate_name=name,
            name_confidence=confidence,
            name_block_id=line.block_id if line else None,
            emails=list(dict.fromkeys(EMAIL_RE.findall(text))),
            phones=list(dict.fromkeys(find_phone_numbers(text))),
            student_ids=ids,
            profile_urls=list(dict.fromkeys(PROFILE_URL_RE.findall(text))),
        ),
        line,
    )


def filename_reference(file_name: str, ctx: AnalysisContext) -> IdentityReference:
    cfg = ctx.settings.analysis.identity
    noise = {t.lower() for t in cfg.filename_noise_tokens}
    id_re = re.compile(cfg.filename_id_pattern, re.IGNORECASE)
    stem = Path(file_name).stem
    raw_tokens = [t for t in _SPLIT_RE.split(_CAMEL_RE.sub(" ", _ascii(stem))) if t]
    ids = [t for t in raw_tokens if id_re.match(t)]
    names = [
        t.lower() for t in raw_tokens if t.isalpha() and t.lower() not in noise and len(t) >= 2
    ]
    return IdentityReference(
        source="filename",
        authoritative=False,
        name=" ".join(names) or None,
        name_tokens=names,
        student_ids=ids,
    )


def manifest_reference(record: ManifestRecord) -> IdentityReference:
    return IdentityReference(
        source="manifest",
        authoritative=True,
        name=record.student_name,
        name_tokens=name_tokens(record.student_name or ""),
        student_ids=[record.student_id] if record.student_id else [],
        email=record.email,
    )


# --------------------------------------------------------------------------- comparison


def _is_common_word(token: str) -> bool:
    from pps_ats.analysis.language import _spellchecker

    spell = _spellchecker("en")
    return spell is not None and token.lower() in spell


def compare(
    extracted: ExtractedIdentity,
    reference: IdentityReference,
    document_text: str,
    min_len: int,
) -> tuple[IdentityStatus, list[str], list[str]]:
    matched: list[str] = []
    conflicts: list[str] = []
    norm_text = re.sub(r"\s+", "", document_text).lower()

    if reference.student_ids:
        if any(re.sub(r"\s+", "", i).lower() in norm_text for i in reference.student_ids):
            matched.append("student_id")
        elif extracted.student_ids:
            conflicts.append("student_id")

    doc_tokens = name_tokens(extracted.candidate_name or "")
    ref_tokens = [t for t in reference.name_tokens if len(t) >= min_len]
    # A non-authoritative file name often contains ordinary words ("two_column",
    # "data_science"). Those may confirm a match but are not evidence of a conflict.
    conflict_tokens = (
        ref_tokens if reference.authoritative else [t for t in ref_tokens if not _is_common_word(t)]
    )
    if ref_tokens:
        if doc_tokens:
            if _tokens_match(doc_tokens, ref_tokens, min_len):
                matched.append("name")
            elif conflict_tokens:
                conflicts.append("name")
        else:
            locals_ = [e.split("@")[0].lower() for e in extracted.emails]
            if any(t in loc for t in ref_tokens if len(t) >= min_len for loc in locals_):
                matched.append("email_local_part")

    if reference.email and extracted.emails:
        if reference.email.lower() in {e.lower() for e in extracted.emails}:
            matched.append("email")
        elif not matched:
            conflicts.append("email")

    if conflicts:
        return IdentityStatus.POSSIBLE_MISMATCH, matched, conflicts
    if matched:
        return IdentityStatus.CONFIRMED_MATCH, matched, conflicts
    return IdentityStatus.INSUFFICIENT_INFORMATION, matched, conflicts


def analyze_identity(ctx: AnalysisContext) -> tuple[IdentityResult, list[Finding]]:
    cfg = ctx.settings.analysis.identity
    extracted, name_line = extract_identity(ctx)
    reference = (
        manifest_reference(ctx.manifest)
        if ctx.manifest is not None
        else filename_reference(ctx.scan.document.file_name, ctx)
    )
    text = f"{ctx.content.full_text}\n{ctx.content.header_footer_text}"
    has_reference = bool(reference.name_tokens or reference.student_ids or reference.email)
    if has_reference:
        status, matched, conflicts = compare(extracted, reference, text, cfg.min_name_token_len)
    else:
        status, matched, conflicts = IdentityStatus.INSUFFICIENT_INFORMATION, [], []
    result = IdentityResult(
        status=status,
        extracted=extracted,
        reference=reference,
        matched_on=matched,
        conflicts=conflicts,
    )

    findings: list[Finding] = []
    if status is IdentityStatus.POSSIBLE_MISMATCH:
        source = "the student manifest" if reference.authoritative else "the file name"
        details = []
        if "name" in conflicts:
            details.append(
                f"the name at the top of the document ('{extracted.candidate_name}') does not "
                f"match {source} ('{reference.name}')"
            )
        if "student_id" in conflicts:
            details.append(
                f"the student ID in the document ({', '.join(extracted.student_ids)}) differs "
                f"from {source} ({', '.join(reference.student_ids)})"
            )
        if "email" in conflicts:
            details.append(f"the e-mail address differs from {source}")
        findings.append(
            make_finding(
                rule_id="IDENTITY_POSSIBLE_MISMATCH",
                category=FindingCategory.IDENTITY,
                finding_class=FindingClass.POTENTIAL_ISSUE,
                severity=FindingSeverity.MEDIUM,
                rubric=RubricCriterion.NONE,
                explanation=(
                    "Possible identity mismatch: " + "; ".join(details) + ". This can be a "
                    "renamed file, a reused template or a mix-up; please verify manually. "
                    "No marks are changed by this check."
                ),
                correction=None,
                method=DetectionMethod.COMPARISON,
                confidence=0.8 if reference.authoritative else 0.6,
                review_required=True,
                detected_text=extracted.candidate_name,
                location=locate(name_line) if name_line else None,
                evidence={
                    "reference_source": reference.source,
                    "matched_on": matched,
                    "conflicts": conflicts,
                },
            )
        )

    if extracted.candidate_name is None:
        findings.append(
            make_finding(
                rule_id="IDENTITY_NAME_NOT_FOUND",
                category=FindingCategory.CONTACT,
                finding_class=FindingClass.POTENTIAL_ISSUE,
                severity=FindingSeverity.LOW,
                rubric=RubricCriterion.STRUCTURE_FORMATTING,
                explanation="No clear candidate name was found at the top of the resume.",
                correction="Put your full name as the first, most prominent line.",
                method=DetectionMethod.STRUCTURE,
                confidence=0.5,
            )
        )

    # Contact completeness (complete absence is reported by section completeness).
    if extracted.phones and not extracted.emails:
        findings.append(
            _contact_finding(
                "CONTACT_EMAIL_MISSING",
                "e-mail address",
                FindingClass.POTENTIAL_ISSUE,
                FindingSeverity.MEDIUM,
            )
        )
    if extracted.emails and not extracted.phones:
        findings.append(
            _contact_finding(
                "CONTACT_PHONE_MISSING",
                "phone number",
                FindingClass.RECOMMENDATION,
                FindingSeverity.LOW,
            )
        )
    return result, findings


def _contact_finding(rule: str, what: str, cls: FindingClass, severity: FindingSeverity) -> Finding:
    return make_finding(
        rule_id=rule,
        category=FindingCategory.CONTACT,
        finding_class=cls,
        severity=severity,
        rubric=RubricCriterion.STRUCTURE_FORMATTING,
        explanation=f"No {what} was found in the resume.",
        correction=f"Add your {what} to the contact details at the top.",
        method=DetectionMethod.PATTERN,
        confidence=0.75,
        section="contact",
    )
