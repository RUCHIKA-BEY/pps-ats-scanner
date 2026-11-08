"""Root-cause audit of a partial calibration pilot (calibration documents only).

For every faculty annotation that counts towards recall (``correction_required`` with a
label the scanner has rules for), the scanner's findings on that document are re-derived
and the outcome is explained:

misses (``fn``)
    ``one_to_one_matching``      a same-label finding on that page was used by another
                                 faculty annotation
    ``page_association``         a same-label finding exists, but on another page
    ``label_mapping_confirmed``  a meaning-equivalent rule fired under another label AND the
                                 faculty comment's topic agrees (a mapping-bug candidate)
    ``label_mapping_unconfirmed`` an equivalent-looking rule fired under another label but
                                 the comment's topic does not support it (not mapped)
    ``section_not_detected``     the scanner did not recognise the label's section
    ``no_finding``               the section was found and the label has rules, but no
                                 finding of that kind fired (detection gap)

matches (``tp``) are checked for validity, because matching is location-level (same label
on the same page - faculty comments carry no quoted resume text):
    ``valid``, ``different_section`` (scanner section differs from the comment's section and
    from the label's natural section), ``topic_mismatch`` / ``topic_unverifiable``
    (formatting/layout: the rule family vs the comment's topic words).

Topic checks read the faculty comment *locally* with keyword lists (a heuristic); only
aggregate counts and pseudonymous ids are written; no comment or resume text leaves the
master workbook. Faculty decisions, the split and the original pilot are never modified.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path

from pps_ats.config import Settings
from pps_ats.evaluation.compare import Detection, Prediction, _relevant, detections
from pps_ats.evaluation.dataset import ReferenceDocument
from pps_ats.evaluation.reference import (
    CANDIDATES,
    ReferenceError,
    _decision,
    _rows,
    read_review,
    sha256,
)
from pps_ats.evaluation.topics import NATURAL_SECTION, rule_family, topic_of

# Rules that would mean the same thing as the faculty label but are mapped to another
# canonical label. "RULE@section" restricts to findings in that scanner section.
EQUIVALENT_CANDIDATES = {
    "career_objective": ["SEC_*_MISSING@summary", "SEC_EMPTY@summary",
                         "SEC_PLACEHOLDER_ONLY@summary"],
    "contact": ["PH_GENERIC_EMAIL", "PH_GENERIC_PHONE", "PH_GENERIC_PROFILE_URL",
                "LAYOUT_CONTACT_IN_HEADER"],
    "layout": ["FMT_LARGE_GAP", "FMT_NARROW_MARGIN", "FMT_WIDE_MARGIN", "FMT_PAGE_UNDERFILLED",
               "FMT_MIXED_ALIGNMENT", "FMT_INCONSISTENT_ALIGNMENT"],
    "formatting": ["LAYOUT_*"],
    "project_description": ["SEC_EMPTY@projects", "SEC_PLACEHOLDER_ONLY@projects"],
    "education_details": ["SEC_EMPTY@education", "SEC_PLACEHOLDER_ONLY@education"],
    "certification_details": ["SEC_EMPTY@certifications"],
}  # fmt: skip


def _equivalent_family(label: str, det: Detection) -> str:
    """Topic of an equivalent-candidate finding: its section for section-scoped
    patterns (e.g. SEC_EMPTY@summary), else the rule family."""
    for pattern in EQUIVALENT_CANDIDATES.get(label, []):
        if _hits(pattern, det):
            section = pattern.partition("@")[2]
            return section or rule_family(det.source)
    return "other"


def _hits(pattern: str, det: Detection) -> bool:
    rule, _, section = pattern.partition("@")
    return fnmatch(det.source, rule) and (not section or (det.section or "") == section)


@dataclass
class Annotation:
    candidate_id: str
    document_id: str
    label: str
    intent: str
    page: int | None
    section: str
    text: str  # local only; never written out


def _same_place(a: Annotation, d: Detection) -> bool:
    return a.page is None or d.page is None or a.page == d.page


@dataclass
class _Ref:
    """The fields the evaluation matcher reads from an annotation (topics, not text)."""

    label: str
    section: str | None
    topics: frozenset[str]


def audit_document(
    anns: list[Annotation],
    dets: list[Detection],
    scanner_sections: dict[str, str],
    settings: Settings,
    strict: bool = True,
) -> list[dict]:
    """``strict`` reproduces the evaluation matcher (section/topic relevance required);
    ``strict=False`` reproduces the earlier location-level matching (label + page)."""
    """One row per faculty annotation: outcome, group, cause (no text)."""
    out_of_scope = {k for k, v in settings.evaluation.labels.items() if not v}
    used: set[int] = set()
    match: dict[int, int] = {}
    blocked: dict[int, str] = {}
    for ai, a in enumerate(anns):  # same greedy order as compare.match_document
        ref = _Ref(a.label, a.section or None, frozenset(topic_of(a.text)))
        for di, d in enumerate(dets):
            if di in used or d.label != a.label or not _same_place(a, d):
                continue
            if strict:
                ok, why = _relevant(ref, d)
                if not ok:
                    blocked.setdefault(ai, (why or "").replace(" ", "_"))
                    continue
            used.add(di)
            match[ai] = di
            break
    rows = []
    for ai, a in enumerate(anns):
        if a.intent == "recommendation":
            group = "recommendation"
        elif a.intent == "unclear":
            group = "intent_unclear"
        elif a.label in out_of_scope:
            group = "outside_scanner_scope"
        else:
            group = "detectable_error"
        row = {
            "candidate_id": a.candidate_id,
            "document_id": a.document_id,
            "label": a.label,
            "group": group,
            "outcome": "tp" if ai in match else "fn",
            "cause": "",
            "scanner_rule": "",
            "topic_heuristic": ",".join(sorted(topic_of(a.text))),
        }
        if group != "detectable_error":
            row["cause"] = "not scored"
        elif ai in match:
            d = dets[match[ai]]
            row["scanner_rule"] = d.source
            natural = NATURAL_SECTION.get(a.label)
            if d.section and a.section and d.section != a.section and d.section != natural:
                row["cause"] = "different_section"
            elif a.label in ("formatting", "layout"):
                topics = topic_of(a.text)
                family = rule_family(d.source)
                if not topics:
                    row["cause"] = "topic_unverifiable"
                elif family in topics:
                    row["cause"] = "valid"
                else:
                    row["cause"] = "topic_mismatch"
            else:
                row["cause"] = "valid"
        else:
            same_label = [d for d in dets if d.label == a.label]
            here = [d for d in same_label if _same_place(a, d)]
            equivalent = [
                d
                for d in dets
                if _same_place(a, d)
                and d.label != a.label
                and any(_hits(p, d) for p in EQUIVALENT_CANDIDATES.get(a.label, []))
            ]
            natural = NATURAL_SECTION.get(a.label)
            if ai in blocked:
                row["cause"] = f"not_credited_{blocked[ai]}"
            elif here and all(dets.index(d) in used for d in here):
                row["cause"] = "one_to_one_matching"
            elif here:
                row["cause"] = "not_credited_other"
            elif same_label:
                row["cause"] = "page_association"
            elif equivalent:
                rule = equivalent[0].source
                family = _equivalent_family(a.label, equivalent[0])
                row["scanner_rule"] = rule
                row["cause"] = (
                    "label_mapping_confirmed"
                    if family in topic_of(a.text)
                    else "label_mapping_unconfirmed"
                )
            elif natural and scanner_sections.get(natural) == "missing":
                row["cause"] = "section_not_detected"
            else:
                row["cause"] = "no_finding"
        rows.append(row)
    return rows


def pilot_workbook(ref: Path, recorded_sha: str) -> Path:
    """The workbook the pilot actually read: the master if unchanged, else the backup in
    ``review/backups`` with the recorded hash. Refuses when neither matches."""
    master = Path(ref) / "review" / "annotation_review.xlsx"
    if master.is_file() and sha256(master) == recorded_sha:
        return master
    for backup in sorted((Path(ref) / "review" / "backups").glob("*.xlsx")):
        if sha256(backup) == recorded_sha:
            return backup
    raise ReferenceError(
        "the workbook the pilot read is no longer available (master changed and no backup "
        "has the recorded hash); re-run partial-pilot before auditing"
    )


def run_pilot_audit(
    ref: Path, pilot: Path, output: Path, settings: Settings, strict: bool = True
) -> dict:
    from pps_ats.analysis.orchestrator import analyze_scan
    from pps_ats.pipeline import scan_file

    ref, pilot, output = Path(ref), Path(pilot), Path(output)
    if output.exists() and any(output.iterdir()):
        raise ReferenceError(f"{output} is not empty; an audit is never overwritten")
    if pilot.resolve() in output.resolve().parents or output.resolve() == pilot.resolve():
        raise ReferenceError("write the audit outside the original pilot folder")
    pilot_files = sorted(p for p in pilot.rglob("*") if p.is_file())
    before = {str(p.relative_to(pilot)): sha256(p) for p in pilot_files}
    original = json.loads((pilot / "pilot_summary.json").read_text(encoding="utf-8"))
    workbook = pilot_workbook(ref, original["master_workbook_sha256"])
    included = [r["document_id"] for r in _rows(pilot / "included_documents.csv")]
    docs = {d["document_id"]: d for d in _rows(ref / "documents.csv")}
    if any(docs[d]["split"] != "calibration" for d in included):
        raise ReferenceError("the pilot contains non-calibration documents")
    cands = {c["candidate_id"]: c for c in _rows(ref / CANDIDATES)}
    anns: dict[str, list[Annotation]] = defaultdict(list)
    for r in read_review(ref, workbook):
        if r["split"] != "calibration" or r["document_id"] not in included:
            continue  # held-out rows are never read beyond their split value
        if _decision(r["decision"]) != "accept":
            continue
        c = cands[r["candidate_id"]]
        anns[r["document_id"]].append(
            Annotation(
                candidate_id=r["candidate_id"],
                document_id=r["document_id"],
                label=r["final_label"].strip().lower(),
                intent=r["faculty_intent"].strip().lower(),
                page=int(c["page"]) if c["page"].isdigit() else None,
                section=c.get("associated_section", ""),
                text=c.get("teacher_comment", ""),
            )
        )
    rows: list[dict] = []
    for doc_id in included:
        scan = scan_file(pilot / "dataset" / "files" / f"{doc_id}.pdf", settings)
        analysis = analyze_scan(scan, settings)
        doc = ReferenceDocument(doc_id, "", "calibration", Path("."))
        dets = detections(Prediction(doc, analysis, None), settings.evaluation, False)
        sections = {s.name: s.status.value for s in scan.sections}
        rows += audit_document(anns[doc_id], dets, sections, settings, strict)

    detectable = [r for r in rows if r["group"] == "detectable_error"]
    tps = [r for r in detectable if r["outcome"] == "tp"]
    fns = [r for r in detectable if r["outcome"] == "fn"]
    valid = [r for r in tps if r["cause"] == "valid"]
    unverifiable = [r for r in tps if r["cause"] == "topic_unverifiable"]
    n = len(detectable)
    orig = original["metrics"]["groups"]["detectable_error"]
    summary = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "pilot": str(pilot),
        "documents": len(included),
        "matching": "strict (section/topic relevance)" if strict else "location-level",
        "workbook_used": "master (unchanged)"
        if workbook.parent.name == "review"
        else f"backup {workbook.name} (master changed since the pilot)",
        "workbook_sha256": original["master_workbook_sha256"],
        "detectable_annotations": n,
        "reproduces_pilot_matching": (
            n == orig["faculty_annotations"] and len(tps) == orig["matched_by_scanner"]
        ),
        "misses_by_cause": dict(Counter(r["cause"] for r in fns).most_common()),
        "misses_by_label_and_cause": {
            label: dict(Counter(r["cause"] for r in fns if r["label"] == label))
            for label in sorted({r["label"] for r in fns})
        },
        "matches_by_validity": dict(Counter(r["cause"] for r in tps).most_common()),
        "matches_by_label_and_validity": {
            label: dict(Counter(r["cause"] for r in tps if r["label"] == label))
            for label in sorted({r["label"] for r in tps})
        },
        "mapping_candidates": dict(
            Counter(
                f"{r['label']} <- {r['scanner_rule']} ({r['cause']})"
                for r in fns
                if r["cause"].startswith("label_mapping")
            )
        ),
        "recall": {
            "pilot_reported": {
                "matched": orig["matched_by_scanner"],
                "of": orig["faculty_annotations"],
                "recall": orig["recall"],
            },
            "strict_valid_matches_only": {
                "matched": len(valid),
                "of": n,
                "recall": round(len(valid) / n, 3) if n else None,
            },
            "strict_plus_unverifiable": {
                "matched": len(valid) + len(unverifiable),
                "of": n,
                "recall": round((len(valid) + len(unverifiable)) / n, 3) if n else None,
            },
        },
        "notes": [
            "Calibration documents only; held-out evaluation annotations were not read.",
            "Topic checks are keyword heuristics on faculty comments, run locally.",
            "No faculty decision, mapping, weight or split was changed by this audit.",
        ],
    }
    after = {str(p.relative_to(pilot)): sha256(p) for p in pilot_files}
    summary["original_pilot_unchanged"] = after == before
    output.mkdir(parents=True, exist_ok=True)
    (output / "audit_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "audit_summary.md").write_text(_markdown(summary), encoding="utf-8")
    fields = ["candidate_id", "document_id", "label", "group", "outcome", "cause",
              "scanner_rule", "topic_heuristic"]  # fmt: skip
    with (output / "audit_rows.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return summary


def _markdown(s: dict) -> str:
    r = s["recall"]
    lines = [
        "# Calibration pilot audit (aggregate)",
        "",
        f"Documents: {s['documents']} (calibration only); applicable faculty corrections: "
        f"{s['detectable_annotations']}; reproduces original matching: "
        f"{s['reproduces_pilot_matching']}; original pilot unchanged: "
        f"{s['original_pilot_unchanged']}",
        "",
        "## Misses by root cause",
        "",
        "| cause | count |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in s["misses_by_cause"].items()],
        "",
        "| label | causes |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in s["misses_by_label_and_cause"].items()],
        "",
        "## Validity of the original matches",
        "",
        "| validity | count |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in s["matches_by_validity"].items()],
        "",
        "## Recall",
        "",
        "| method | matched | of | recall |",
        "|---|---|---|---|",
        *[f"| {k} | {v['matched']} | {v['of']} | {v['recall']} |" for k, v in r.items()],
        "",
        f"Mapping candidates: {s['mapping_candidates'] or 'none'}",
        "",
        *[f"- {n}" for n in s["notes"]],
    ]
    return "\n".join(lines) + "\n"
