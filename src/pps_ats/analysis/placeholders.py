"""Unfinished-template detection (configurable rules in ``[analysis.placeholders]``).

Two kinds of evidence:
* pattern rules - template text such as "City, State", "linkedin.com/in/yourname",
  "[Insert your skills]", "Lorem ipsum". ``standalone`` rules (e.g. "Project Title") only
  fire when the label is the *whole* line, so "Project Title: Smart Irrigation" is fine,
  and a DOCX table cell that is a label for a filled neighbouring cell is fine.
* empty value labels - "Description:" / "Technologies:" with no value on the same line
  or the next line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pps_ats.analysis.context import AnalysisContext, TextLine, locate, make_finding
from pps_ats.schemas.findings import (
    DetectionMethod,
    Finding,
    FindingCategory,
    FindingClass,
    FindingSeverity,
    RubricCriterion,
)

_STRIP_RE = re.compile(r"^[\s•·▪◦‣●■►▸➢➤✓✔*>\-–—|]+|[\s:|\-–—]+$")


@dataclass
class _Rule:
    id: str
    regex: re.Pattern[str]
    standalone: bool
    finding_class: FindingClass
    severity: FindingSeverity
    confidence: float
    label: str
    correction: str


@dataclass
class _Hit:
    rule: _Rule
    start: int
    end: int


def _compile_rules(ctx: AnalysisContext) -> list[_Rule]:
    rules = []
    for raw in ctx.settings.analysis.placeholders.rules:
        flags = re.IGNORECASE if raw.ignore_case else 0
        rules.append(
            _Rule(
                id=raw.id,
                regex=re.compile(raw.pattern, flags),
                standalone=raw.mode == "standalone",
                finding_class=FindingClass(raw.finding_class),
                severity=FindingSeverity(raw.severity),
                confidence=raw.confidence,
                label=raw.label,
                correction=raw.correction,
            )
        )
    return rules


def core_text(text: str) -> str:
    """Line text without bullets, separators and a trailing colon."""
    return _STRIP_RE.sub("", text)


def _letters(text: str) -> int:
    return sum(1 for c in text if c.isalnum())


def _table_label_has_value(ctx: AnalysisContext, tl: TextLine, rules: list[_Rule]) -> bool:
    """A DOCX table cell that labels a filled neighbour cell (or a filled column)."""
    block = tl.block
    if block.table_id is None or block.table_cell is None:
        return False
    table = next((t for t in ctx.content.tables if t.id == block.table_id), None)
    if table is None:
        return False
    row, col = block.table_cell

    def filled(value: str | None) -> bool:
        if not value or _letters(value) < 2:
            return False
        stripped = core_text(value)
        return not any(r.regex.fullmatch(stripped) or r.regex.search(value) for r in rules)

    cells = table.cells
    if row < len(cells) and col + 1 < len(cells[row]) and filled(cells[row][col + 1]):
        return True
    return row == 0 and any(col < len(r) and filled(r[col]) for r in cells[1:])


def _line_hits(tl: TextLine, rules: list[_Rule], ctx: AnalysisContext) -> list[_Hit]:
    hits: list[_Hit] = []
    text = tl.text
    core = core_text(text)
    for rule in rules:
        if rule.standalone:
            if core and rule.regex.fullmatch(core) and not _table_label_has_value(ctx, tl, rules):
                start = text.find(core)
                hits.append(_Hit(rule, start, start + len(core)))
            continue
        hits.extend(_Hit(rule, m.start(), m.end()) for m in rule.regex.finditer(text))
    # Overlapping hits on the same text: keep the most confident / longest.
    hits.sort(key=lambda h: (-h.rule.confidence, -(h.end - h.start), h.start))
    kept: list[_Hit] = []
    for hit in hits:
        if all(hit.end <= k.start or hit.start >= k.end for k in kept):
            kept.append(hit)
    return sorted(kept, key=lambda h: h.start)


def detect_placeholders(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.placeholders
    rules = _compile_rules(ctx)
    findings: list[Finding] = []
    placeholder_lines: set[tuple[str, int]] = set()
    coverage: dict[tuple[str, int], int] = {}

    for tl in ctx.all_lines():
        for hit in _line_hits(tl, rules, ctx):
            rule = hit.rule
            key = (tl.block_id, tl.line_index)
            placeholder_lines.add(key)
            coverage[key] = coverage.get(key, 0) + _letters(tl.text[hit.start : hit.end])
            matched = tl.text[hit.start : hit.end]
            findings.append(
                make_finding(
                    rule_id=rule.id,
                    category=FindingCategory.PLACEHOLDER,
                    finding_class=rule.finding_class,
                    severity=rule.severity,
                    rubric=RubricCriterion.CONTENT_RELEVANCE,
                    explanation=(
                        f"{rule.label}: '{matched}' looks like unedited template text"
                        + (f" in the {tl.section} section." if tl.section else ".")
                    ),
                    correction=rule.correction,
                    method=DetectionMethod.PATTERN,
                    confidence=rule.confidence,
                    detected_text=matched,
                    location=locate(tl, hit.start, hit.end),
                    section=tl.section,
                    evidence={"line_text": tl.text[:200], "pattern_id": rule.id},
                )
            )

    # Lines that are *only* placeholder text (used by other modules to avoid double reports).
    ctx.cache["placeholder_lines"] = placeholder_lines
    ctx.cache["placeholder_only_lines"] = {
        key
        for key in placeholder_lines
        if (tl := _line_by_key(ctx, key)) is not None
        and _letters(tl.text) - coverage.get(key, 0) < cfg.min_value_chars + 2
    }
    findings.extend(_empty_value_labels(ctx, rules))
    return findings


def _line_by_key(ctx: AnalysisContext, key: tuple[str, int]) -> TextLine | None:
    index = ctx.cache.get("_line_index")
    if index is None:
        index = {(t.block_id, t.line_index): t for t in ctx.all_lines()}
        ctx.cache["_line_index"] = index
    return index.get(key)


def _empty_value_labels(ctx: AnalysisContext, rules: list[_Rule]) -> list[Finding]:
    cfg = ctx.settings.analysis.placeholders
    if not cfg.value_labels:
        return []
    labels = sorted({re.escape(v.lower()) for v in cfg.value_labels}, key=len, reverse=True)
    label_re = re.compile(
        rf"^(?P<label>{'|'.join(labels)})\s*(?P<sep>[:\-–])?\s*(?P<value>.*)$", re.IGNORECASE
    )
    only_placeholder = ctx.cache.get("placeholder_only_lines", set())
    findings: list[Finding] = []
    lines = ctx.lines
    for i, tl in enumerate(lines):
        if tl.is_heading:
            continue
        match = label_re.match(tl.body_text.rstrip(" |"))
        if match is None:
            continue
        value = match.group("value") or ""
        has_sep = bool(match.group("sep"))
        if _letters(value) >= cfg.min_value_chars:
            continue
        if not has_sep and tl.block.table_id is not None:
            continue  # bare table-header words ("Role", "Duration") are layout, not fields
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if nxt is not None and nxt.section == tl.section and not nxt.is_heading:
            next_is_label = label_re.match(nxt.body_text.rstrip(" |")) is not None
            next_placeholder = (nxt.block_id, nxt.line_index) in only_placeholder
            if (
                not next_is_label
                and not next_placeholder
                and _letters(nxt.text) >= max(cfg.min_value_chars, 8)
            ):
                continue  # value on the following line
        # A bare word followed by more (non-label) lines is a sub-heading, not a field.
        if (
            not has_sep
            and nxt is not None
            and not nxt.is_heading
            and nxt.section == tl.section
            and label_re.match(nxt.body_text.rstrip(" |")) is None
        ):
            continue
        label = match.group("label")
        descriptive = label.lower() in {
            "description",
            "project description",
            "responsibilities",
            "key responsibilities",
            "objective",
        }
        findings.append(
            make_finding(
                rule_id="PH_EMPTY_FIELD",
                category=FindingCategory.PLACEHOLDER,
                finding_class=FindingClass.DEFINITE_DEFECT,
                severity=FindingSeverity.HIGH if descriptive else FindingSeverity.MEDIUM,
                rubric=RubricCriterion.CONTENT_RELEVANCE,
                explanation=(
                    f"The field '{label}' has no content after it"
                    + (f" in the {tl.section} section." if tl.section else ".")
                ),
                correction=(
                    f"Write the {label.lower()} for this entry, or remove the empty "
                    f"'{label}' label."
                ),
                method=DetectionMethod.STRUCTURE,
                confidence=0.85 if has_sep else 0.7,
                detected_text=tl.text,
                location=locate(tl),
                section=tl.section,
                evidence={"label": label, "next_line": (nxt.text[:120] if nxt else None)},
            )
        )
    return findings
