"""Root-cause audit of a calibration pilot. Synthetic data only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pps_ats.cli import main
from pps_ats.config import load_settings
from pps_ats.evaluation.compare import Detection
from pps_ats.evaluation.pilot import run_partial_pilot
from pps_ats.evaluation.pilot_audit import Annotation, audit_document, run_pilot_audit
from pps_ats.evaluation.reference import ReferenceError
from test_partial_pilot import build, review


def ann(label, text="", page=1, section="", intent="correction_required", cid="P001-001"):  # type: ignore[no-untyped-def]
    return Annotation(cid, "P001", label, intent, page, section, text)


def det(label, rule, page=1, section=None):  # type: ignore[no-untyped-def]
    return Detection(label, rule, None, page, "medium", False, section)


def causes(anns, dets, sections=None, strict=False):  # type: ignore[no-untyped-def]
    rows = audit_document(anns, dets, sections or {}, load_settings(), strict)
    return [(r["outcome"], r["cause"]) for r in rows]


# --------------------------------------------------------------------------- misses


def test_one_to_one_and_page_association():
    two = [ann("formatting", "fix fonts"), ann("formatting", "fix fonts", cid="P001-002")]
    assert causes(two, [det("formatting", "FMT_MIXED_BODY_FONTS")]) == [
        ("tp", "valid"),
        ("fn", "one_to_one_matching"),
    ]
    assert causes([ann("formatting", "font", page=1)],
                  [det("formatting", "FMT_MIXED_BODY_FONTS", page=2)]) == [
        ("fn", "page_association")
    ]  # fmt: skip


def test_label_mapping_needs_matching_meaning():
    missing_summary = det("missing_section", "SEC_RECOMMENDED_MISSING", page=None,
                          section="summary")  # fmt: skip
    confirmed = causes([ann("career_objective", "add a career objective")], [missing_summary])
    assert confirmed == [("fn", "label_mapping_confirmed")]
    # an equivalent-looking rule whose topic the faculty comment does not mention
    unconfirmed = causes([ann("formatting", "fix the alignment")],
                         [det("layout", "LAYOUT_TABLE")])  # fmt: skip
    assert unconfirmed == [("fn", "label_mapping_unconfirmed")]
    # an unrelated finding under another label is never counted
    assert causes([ann("career_objective", "objective is generic")],
                  [det("certification_details", "CQ_CERTIFICATION_DETAILS")]) == [
        ("fn", "no_finding")
    ]  # fmt: skip


def test_section_not_detected_vs_no_finding():
    a = [ann("project_description", "describe your contribution")]
    assert causes(a, [], {"projects": "missing"}) == [("fn", "section_not_detected")]
    assert causes(a, [], {"projects": "detected"}) == [("fn", "no_finding")]


# --------------------------------------------------------------------------- matches


def test_match_validity_by_section_and_topic():
    # different section from the comment and from the label's natural section
    assert causes([ann("formatting", "line spacing", section="projects")],
                  [det("formatting", "FMT_LINE_SPACING", section="skills")]) == [
        ("tp", "different_section")
    ]  # fmt: skip
    # education finding in the education section is valid even if the comment sat elsewhere
    assert causes([ann("education_details", "add 12th", section="skills")],
                  [det("education_details", "CQ_EDUCATION_INCOMPLETE", section="education")]) == [
        ("tp", "valid")
    ]  # fmt: skip
    rule = [det("formatting", "FMT_LINE_SPACING", section="projects")]
    assert causes([ann("formatting", "use one font", section="projects")], rule) == [
        ("tp", "topic_mismatch")
    ]
    assert causes([ann("formatting", "reduce line spacing", section="projects")], rule) == [
        ("tp", "valid")
    ]
    assert causes([ann("formatting", "fix this", section="projects")], rule) == [
        ("tp", "topic_unverifiable")
    ]


def test_recommendations_and_out_of_scope_are_not_scored():
    rows = audit_document(
        [ann("formatting", "font", intent="recommendation"), ann("section_title", "title")],
        [],
        {},
        load_settings(),
    )
    assert [(r["group"], r["cause"]) for r in rows] == [
        ("recommendation", "not scored"),
        ("outside_scanner_scope", "not scored"),
    ]


# --------------------------------------------------------------------------- end to end


def test_audit_reproduces_pilot_and_preserves_it(tmp_path):
    ref, pid = build(tmp_path)
    review(ref, pid)
    settings = load_settings()
    pilot = tmp_path / "pilot"
    run_partial_pilot(ref, pilot, settings)
    files = sorted(p for p in pilot.rglob("*") if p.is_file())
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    out = tmp_path / "audit"
    s = run_pilot_audit(ref, pilot, out, settings)
    assert s["reproduces_pilot_matching"] and s["original_pilot_unchanged"]
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files} == before
    r = s["recall"]
    assert r["strict_valid_matches_only"]["matched"] <= r["pilot_reported"]["matched"]
    text = "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
    for comment in ("add 12th and 10th", "title as", "Keep bullets", "linkedin"):
        assert comment not in text  # faculty comments never written out
    with pytest.raises(ReferenceError, match="never overwritten"):
        run_pilot_audit(ref, pilot, out, settings)
    with pytest.raises(ReferenceError, match="outside the original pilot"):
        run_pilot_audit(ref, pilot, pilot / "audit", settings)
    assert json.loads((out / "audit_summary.json").read_text(encoding="utf-8"))["documents"] == 1


def test_cli_pilot_audit(tmp_path, capsys):
    ref, pid = build(tmp_path)
    review(ref, pid)
    run_partial_pilot(ref, tmp_path / "pilot", load_settings())
    code = main(["pilot-audit", str(ref), "--pilot", str(tmp_path / "pilot"),
                 "-o", str(tmp_path / "audit")])  # fmt: skip
    assert code == 0
    err = capsys.readouterr().err
    assert "misses by cause" in err and "12th" not in err
    assert (Path(tmp_path / "audit") / "audit_rows.csv").is_file()


def test_audit_uses_the_workbook_the_pilot_read(tmp_path):
    import shutil

    from openpyxl import load_workbook

    ref, pid = build(tmp_path)
    review(ref, pid)
    settings = load_settings()
    run_partial_pilot(ref, tmp_path / "pilot", settings)
    master = ref / "review" / "annotation_review.xlsx"
    backups = ref / "review" / "backups"
    backups.mkdir()
    shutil.copyfile(master, backups / "annotation_review_t0.xlsx")
    wb = load_workbook(master)  # the reviewer keeps working after the pilot
    wb["calibration"].cell(row=2, column=16).value = "later note"
    wb.save(master)
    wb.close()
    s = run_pilot_audit(ref, tmp_path / "pilot", tmp_path / "audit", settings)
    assert "backup annotation_review_t0.xlsx" in s["workbook_used"]
    (backups / "annotation_review_t0.xlsx").unlink()
    with pytest.raises(ReferenceError, match="no longer available"):
        run_pilot_audit(ref, tmp_path / "pilot", tmp_path / "audit2", settings)


def test_strict_audit_reports_uncredited_findings():
    spacing = [ann("formatting", "use one font", section="projects")]
    rule = [det("formatting", "FMT_LINE_SPACING", section="projects")]
    assert causes(spacing, rule, strict=True) == [("fn", "not_credited_different_topic")]
    other = [ann("formatting", "line spacing", section="projects")]
    elsewhere = [det("formatting", "FMT_LINE_SPACING", section="skills")]
    assert causes(other, elsewhere, strict=True) == [("fn", "not_credited_different_section")]
    unknown = [ann("formatting", "fix this", section="projects")]
    assert causes(unknown, rule, strict=True) == [("fn", "not_credited_no_topic_information")]
    good = [ann("formatting", "reduce the line spacing", section="projects")]
    assert causes(good, rule, strict=True) == [("tp", "valid")]
