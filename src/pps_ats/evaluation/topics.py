"""Topic families shared by the evaluation matcher and the pilot audit.

A faculty comment's topics are derived locally from its words (a keyword heuristic) and
stored as a short list such as ``fonts,spacing`` - the comment text itself is not copied.
A scanner rule belongs to one family. For ``formatting`` and ``layout`` annotations a
scanner finding is credited only when its family is one of the comment's topics.
"""

from __future__ import annotations

TOPIC_WORDS: dict[str, tuple[str, ...]] = {
    "fonts": ("font", "size", "bold", "italic", "style", "typeface"),
    "spacing": ("space", "spacing", "gap", "line"),
    "alignment": ("align", "indent", "justif", "center", "centre"),
    "margin": ("margin", "space", "white"),
    "structure": ("table", "column", "box", "header", "footer", "layout", "section"),
    "summary": ("objective", "summary", "profile", "about"),
    "contact": ("email", "e-mail", "mail", "phone", "mobile", "contact", "linkedin", "github",
                "profile", "link", "url"),
    "projects": ("project", "description", "detail"),
    "education": ("education", "10th", "12th", "cgpa", "degree", "marks", "percentage"),
    "certifications": ("certificat", "course"),
}  # fmt: skip

# Labels whose scanner findings must agree with the faculty comment's topic to match.
TOPIC_SCOPED_LABELS = frozenset({"formatting", "layout"})

# The section a label naturally refers to (a finding there is relevant wherever the
# faculty placed the comment on the page).
NATURAL_SECTION = {
    "career_objective": "summary",
    "education_details": "education",
    "certification_details": "certifications",
    "project_description": "projects",
    "contact": "contact",
}


def topic_of(text: str) -> set[str]:
    t = (text or "").casefold()
    return {family for family, words in TOPIC_WORDS.items() if any(w in t for w in words)}


def rule_family(rule: str) -> str:
    if "CONTACT" in rule or rule.startswith("PH_GENERIC_"):
        return "contact"
    if rule.startswith("LAYOUT_"):
        return "structure"
    if "ALIGN" in rule:
        return "alignment"
    if "MARGIN" in rule:
        return "margin"
    if any(k in rule for k in ("FONT", "SIZE", "SMALL_TEXT")):
        return "fonts"
    if any(k in rule for k in ("SPACING", "GAP", "UNDERFILLED")):
        return "spacing"
    return "other"
