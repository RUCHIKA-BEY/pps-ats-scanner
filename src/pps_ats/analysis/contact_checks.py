"""Contact presentation checks (added after the faculty calibration pilot).

All rules report only what the document evidences:

* ``CONTACT_LINKEDIN_MISSING`` / ``CONTACT_CODE_PROFILE_MISSING`` (recommendations): no
  LinkedIn, or - for a resume with a Projects section - no code/portfolio profile, in the
  text or the PDF/DOCX hyperlinks.
* ``CONTACT_LINK_TEXT_ONLY``: a profile is named ("LinkedIn", "GitHub", "Portfolio") in the
  contact area with no URL on that line and no hyperlink over it, so neither an ATS nor a
  reader of a printed copy can follow it.
* ``CONTACT_PLACEHOLDER_LINK``: a hyperlink target or written URL with no real handle
  (``linkedin.com/in/``, ``github.com/yourname``, ``example.com``, ``#``).
* ``CONTACT_EMAIL_MALFORMED`` / ``CONTACT_PHONE_INCOMPLETE``: contact details that look
  incomplete (``name@gmail``; a labelled phone number with too few digits).
* ``CONTACT_DETAILS_MULTILINE`` (optional style recommendation, outside the CA1 rubric):
  contact items spread over several lines. Some faculty prefer one line; it is not an ATS
  defect.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from pps_ats.analysis.context import (
    AnalysisContext,
    Location,
    TextLine,
    locate,
    make_finding,
)
from pps_ats.analysis.sections import EMAIL_RE, find_phone_numbers
from pps_ats.schemas.findings import (
    DetectionMethod,
    Finding,
    FindingCategory,
    FindingClass,
    FindingSeverity,
    LocationPrecision,
    RubricCriterion,
)

SF = RubricCriterion.STRUCTURE_FORMATTING
_URLISH = re.compile(r"(https?://|www\.|\b[\w-]+\.(com|in|io|dev|me|org|net|app|ai)\b/?)", re.I)
_EMAILISH = re.compile(
    r"(?<![\w.+-])(?P<local>[\w.+-]+)(?P<s1>\s?)@(?P<s2>\s?)(?P<domain>[\w-]+(?:[.,][\w-]+)*)"
)
_PHONE_LABEL = re.compile(
    r"\b(?:phone|mobile|mob|mob\.|tel|telephone|contact\s*(?:no|number)|ph)\b\.?\s*[:\-]?\s*"
    r"(?P<num>\+?[\d][\d\s().-]{3,})",
    re.I,
)


def _has_url(text: str) -> bool:
    """A web address on the line (e-mail domains do not count)."""
    return bool(_URLISH.search(EMAIL_RE.sub(" ", text)))


def contact_area(ctx: AnalysisContext, limit: int = 12) -> list[TextLine]:
    """Lines before the first section heading or in a Contact section, plus headers and
    footers; falls back to the first ``limit`` body lines."""
    area = [t for t in ctx.lines if t.section in (None, "contact")][: limit * 2]
    if not area:
        area = ctx.lines[:limit]
    return area + ctx.hf_lines


def _profile_hosts(ctx: AnalysisContext) -> tuple[set[str], str]:
    """Profile kinds evidenced anywhere (text or hyperlinks), and the combined text."""
    text = f"{ctx.content.full_text}\n{ctx.content.header_footer_text}".casefold()
    uris = " ".join(link.uri.casefold() for link in ctx.content.links)
    blob = f"{text}\n{uris}"
    cfg = ctx.settings.analysis.contact
    found = {kind for kind, hosts in cfg.profile_hosts.items() if any(h in blob for h in hosts)}
    return found, blob


def _placeholder_uri(uri: str, cfg) -> str | None:  # type: ignore[no-untyped-def]
    raw = uri.strip()
    if raw in ("#", "") or raw.lower() in ("http://", "https://", "about:blank"):
        return "empty link target"
    if raw.lower().startswith(("javascript:", "file:")):
        return "link target is not a web address"
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").removeprefix("www.")
    path = [p for p in parsed.path.split("/") if p]
    if host in cfg.placeholder_hosts:
        return f"placeholder host '{host}'"
    if host.endswith("linkedin.com"):
        handle = path[1] if len(path) > 1 and path[0] in ("in", "pub") else None
        if handle is None:
            return "LinkedIn link without a profile handle"
    elif host in ("github.com", "gitlab.com"):
        handle = path[0] if path else None
        if handle is None:
            return f"{host} link without a user name"
    else:
        return None
    if handle and handle.casefold().strip("-_") in cfg.placeholder_handles:
        return f"template handle '{handle}'"
    return None


def _finding(rule, cls, severity, explanation, correction, location, text, evidence,
             rubric=SF, confidence=0.75):  # type: ignore[no-untyped-def]  # fmt: skip
    return make_finding(
        rule_id=rule,
        category=FindingCategory.CONTACT,
        finding_class=cls,
        severity=severity,
        rubric=rubric,
        explanation=explanation,
        correction=correction,
        method=DetectionMethod.PATTERN,
        confidence=confidence,
        detected_text=text,
        location=location,
        section="contact",
        evidence=evidence,
    )


def check_contact(ctx: AnalysisContext) -> list[Finding]:
    cfg = ctx.settings.analysis.contact
    if not cfg.enabled:
        return []
    findings: list[Finding] = []
    area = contact_area(ctx)
    anchor = next((t for t in area if EMAIL_RE.search(t.text)), area[0] if area else None)
    anchor_loc = locate(anchor) if anchor is not None else Location()
    kinds, _ = _profile_hosts(ctx)
    links = ctx.content.links

    # ---- missing profile links (recommendations)
    if "linkedin" not in kinds:
        findings.append(
            _finding(
                "CONTACT_LINKEDIN_MISSING",
                FindingClass.RECOMMENDATION,
                FindingSeverity.LOW,
                "No LinkedIn profile is given in the text or as a hyperlink.",
                "Add your LinkedIn profile URL (linkedin.com/in/<your-handle>) next to your "
                "e-mail and phone.",
                anchor_loc,
                None,
                {"hyperlinks": len(links), "profiles_found": sorted(kinds)},
            )
        )
    has_projects = ctx.sections.get("projects") is not None and ctx.sections[
        "projects"
    ].status.value in ("detected", "uncertain")
    if has_projects and "code" not in kinds:
        findings.append(
            _finding(
                "CONTACT_CODE_PROFILE_MISSING",
                FindingClass.RECOMMENDATION,
                FindingSeverity.LOW,
                "The resume lists projects but gives no GitHub, GitLab or portfolio link.",
                "If your project code or portfolio is public, add the link (e.g. "
                "github.com/<your-handle>).",
                anchor_loc,
                None,
                {"hyperlinks": len(links), "profiles_found": sorted(kinds)},
            )
        )

    # ---- profile named but not linked
    label_re = re.compile(r"\b(" + "|".join(map(re.escape, cfg.profile_labels)) + r")\b", re.I)
    for tl in area:
        for m in label_re.finditer(tl.text):
            if _has_url(tl.text):
                continue
            # Linked when a hyperlink points to that service (LinkedIn -> linkedin.com);
            # matching by address avoids guessing which word a link box covers.
            host_hint = cfg.label_hosts.get(m.group(1).casefold(), "")
            if host_hint:
                linked = any(host_hint in link.uri.casefold() for link in links)
            else:  # 'Portfolio': any web link that is not a known profile service
                known = tuple(cfg.label_hosts.values())
                linked = any(
                    link.uri.casefold().startswith(("http", "www"))
                    and not any(k in link.uri.casefold() for k in known)
                    for link in links
                )
            if linked:
                continue
            findings.append(
                _finding(
                    "CONTACT_LINK_TEXT_ONLY",
                    FindingClass.POTENTIAL_ISSUE,
                    FindingSeverity.MEDIUM,
                    f"'{m.group(1)}' is named but no address or hyperlink is given, so it "
                    "cannot be followed from a printed copy or by an ATS.",
                    f"Write the full {m.group(1)} URL (or at least the profile path).",
                    locate(tl, m.start(), m.end()),
                    m.group(1),
                    {"hyperlink_over_text": False, "url_on_line": False},
                    confidence=0.7,
                )
            )

    # ---- placeholder / handle-less links (hyperlink targets and written URLs)
    seen: set[str] = set()
    for link in links:
        reason = _placeholder_uri(link.uri, cfg)
        if reason and link.uri not in seen:
            seen.add(link.uri)
            location = (
                Location(link.page, link.bbox, LocationPrecision.REGION)
                if link.bbox
                else Location(link.page)
            )
            findings.append(
                _finding(
                    "CONTACT_PLACEHOLDER_LINK",
                    FindingClass.DEFINITE_DEFECT,
                    FindingSeverity.MEDIUM,
                    f"A hyperlink points to an unusable address ({reason}).",
                    "Point the link to your real profile URL, or remove it.",
                    location,
                    link.uri,
                    {"source": "hyperlink target", "reason": reason},
                    confidence=0.85,
                )
            )
    url_re = re.compile(r"(?:https?://)?(?:www\.)?(?:linkedin\.com/in/?|github\.com/?)(?=\s|$|[|,;])",
                        re.I)  # fmt: skip
    for tl in area:
        for m in url_re.finditer(tl.text):
            findings.append(
                _finding(
                    "CONTACT_PLACEHOLDER_LINK",
                    FindingClass.DEFINITE_DEFECT,
                    FindingSeverity.MEDIUM,
                    "A profile URL is written without the profile handle.",
                    "Complete the URL with your profile name (e.g. linkedin.com/in/<handle>).",
                    locate(tl),
                    m.group(0),
                    {"source": "written URL", "reason": "no handle"},
                    confidence=0.8,
                )
            )

    # ---- malformed e-mail / incomplete phone
    for tl in area:
        for m in _EMAILISH.finditer(tl.text):
            candidate = m.group(0)
            spaced = bool(m.group("s1") or m.group("s2"))
            domain = m.group("domain")
            if not spaced and EMAIL_RE.fullmatch(candidate):
                continue  # a valid address
            if spaced and "." not in domain and "," not in domain:
                continue  # "@handle" mentions are not e-mail addresses
            if (
                not spaced
                and "." not in domain
                and "," not in domain
                and not re.search(
                    r"(gmail|yahoo|outlook|hotmail|rediff|icloud|proton)", domain, re.I
                )
            ):
                continue  # not evidently an e-mail
            findings.append(
                _finding(
                    "CONTACT_EMAIL_MALFORMED",
                    FindingClass.POTENTIAL_ISSUE,
                    FindingSeverity.MEDIUM,
                    "This e-mail address looks incomplete or malformed.",
                    "Write the full address without spaces, e.g. name@domain.com.",
                    locate(tl),
                    candidate,
                    {"valid_pattern": False},
                    confidence=0.7,
                )
            )
        for m in _PHONE_LABEL.finditer(tl.text):
            digits = re.sub(r"\D", "", m.group("num"))
            if find_phone_numbers(m.group("num")) or not 5 <= len(digits) <= 9:
                continue
            findings.append(
                _finding(
                    "CONTACT_PHONE_INCOMPLETE",
                    FindingClass.POTENTIAL_ISSUE,
                    FindingSeverity.MEDIUM,
                    f"The phone number has only {len(digits)} digits.",
                    "Write the complete number with country code, e.g. +91 98765 43210.",
                    locate(tl),
                    m.group("num").strip(),
                    {"digits": len(digits)},
                    confidence=0.7,
                )
            )

    # ---- optional style: contact details over several lines
    item_lines = [
        t
        for t in area
        if EMAIL_RE.search(t.text) or find_phone_numbers(t.text) or label_re.search(t.text)
        or _has_url(t.text)
    ]  # fmt: skip
    if len(item_lines) >= cfg.multiline_min_lines:
        findings.append(
            _finding(
                "CONTACT_DETAILS_MULTILINE",
                FindingClass.RECOMMENDATION,
                FindingSeverity.LOW,
                f"Contact details are spread over {len(item_lines)} lines. Optional: some "
                "reviewers prefer them on one line (separated by '|'); this is a style "
                "choice, not an ATS problem.",
                "Consider putting e-mail, phone and profile links on a single line.",
                locate(item_lines[0]),
                None,
                {"lines": len(item_lines), "style_preference": True},
                rubric=RubricCriterion.NONE,
                confidence=0.6,
            )
        )
    return findings
