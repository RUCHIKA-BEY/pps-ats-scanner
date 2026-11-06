"""Milestone 2: evidence-based findings. Every fixture is synthetic and generated per run.

Expectations are about behaviour (rule fired / did not fire, location kind, class),
never about exact counts of unrelated findings, so fixtures are not hard-coded.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from pps_ats.analysis.context import ManifestRecord
from pps_ats.analysis.identity import filename_reference, load_manifest, name_tokens
from pps_ats.analysis.orchestrator import MODULES, analyze_file, analyze_scan
from pps_ats.cli import main
from pps_ats.devtools import synthetic
from pps_ats.devtools import synthetic_analysis as sa
from pps_ats.pipeline import scan_file
from pps_ats.schemas.findings import (
    ANALYSIS_SCHEMA_VERSION,
    CompletenessStatus,
    FindingClass,
    IdentityStatus,
    LocationPrecision,
)


@pytest.fixture(scope="session")
def docs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    directory = tmp_path_factory.mktemp("analysis")
    paths = {name: build(directory / name) for name, build in sa.ANALYSIS_SAMPLES.items()}
    paths["two_column.pdf"] = synthetic.two_column_resume_pdf(directory / "two_column.pdf")
    paths["resume.pdf"] = synthetic.resume_pdf(directory / "resume.pdf")
    paths["resume.docx"] = synthetic.resume_docx(directory / "resume.docx")
    paths["image_only.pdf"] = synthetic.image_only_pdf(directory / "image_only.pdf")
    # Identity fixtures: same document content, different file names.
    paths["mismatch"] = synthetic.resume_pdf(directory / "Priya_Desai_Resume.pdf")
    paths["match"] = synthetic.resume_pdf(directory / "AshaExample_Resume_Final.pdf")
    paths["no_name"] = synthetic.resume_pdf(directory / "Final_Resume_ATS.pdf")
    # A grade card submitted under a resume-like file name.
    paths["gradecard_named_resume"] = synthetic.grade_card_pdf(directory / "Resume_Final.pdf")
    return paths


@pytest.fixture(scope="session")
def results(docs, settings):
    cache: dict[str, object] = {}

    def get(name: str):
        if name not in cache:
            cache[name] = analyze_file(docs[name], settings)
        return cache[name]

    return get


def rules(result) -> set[str]:
    return {f.rule_id for f in result.findings}


def by_rule(result, rule_id: str) -> list:
    return [f for f in result.findings if f.rule_id == rule_id]


# --------------------------------------------------------------------------- placeholders


def test_city_state_placeholder_located_on_page(results):
    result = results("template_resume.pdf")
    [finding] = by_rule(result, "PH_CITY_STATE")
    assert finding.detected_text == "City, State"
    assert finding.finding_class is FindingClass.DEFINITE_DEFECT
    assert finding.page_number == 1
    assert finding.bounding_box is not None
    assert finding.location_precision is LocationPrecision.SPAN
    assert finding.block_id and finding.span_id and finding.span_id.startswith(finding.block_id)
    assert "City, State" in finding.suggested_correction


def test_generic_linkedin_flagged_but_real_profile_is_not(results):
    result = results("template_resume.pdf")
    texts = [f.detected_text for f in by_rule(result, "PH_GENERIC_PROFILE_URL")]
    assert texts == ["linkedin.com/in/yourname"]
    # The student's real-looking GitHub URL on the same line is not a placeholder.
    assert not any("meera-testcase" in (f.detected_text or "") for f in result.findings)


def test_placeholder_career_objective(results):
    result = results("template_resume.pdf")
    summary = next(s for s in result.sections if s.name == "summary")
    assert summary.status is CompletenessStatus.PLACEHOLDER_ONLY
    assert "SEC_PLACEHOLDER_ONLY" in rules(result)
    assert any(
        f.category.value == "placeholder" and f.section == "summary" for f in result.findings
    )


def test_empty_project_description(results):
    result = results("template_resume.pdf")
    [finding] = by_rule(result, "CQ_PROJECT_NO_DESCRIPTION")
    assert finding.detected_text == "Smart Attendance System"
    assert finding.finding_class is FindingClass.DEFINITE_DEFECT
    assert finding.bounding_box is not None
    # The project with a real description is not reported as empty.
    assert not any(
        "Library Portal" in (f.detected_text or "")
        for f in by_rule(result, "CQ_PROJECT_NO_DESCRIPTION")
    )


def test_legitimate_label_words_not_flagged(results):
    result = results("template_resume.pdf")
    flagged = " ".join(
        f.detected_text or "" for f in result.findings if f.category.value == "placeholder"
    )
    assert "Inventory Tracker" not in flagged  # "Project Title: Inventory Tracker - ..."
    assert "system architecture" not in flagged  # "Description of the system ..."
    assert "PH_TEMPLATE_LABEL" not in rules(result)


def test_docx_placeholders_without_coordinates(results):
    result = results("template_resume.docx")
    found = rules(result)
    assert {"PH_YOUR_FIELD", "PH_CITY_STATE", "PH_GENERIC_EMAIL", "PH_LOREM_IPSUM"} <= found
    assert len(by_rule(result, "PH_LOREM_IPSUM")) == 1
    for finding in result.findings:
        assert finding.bounding_box is None  # DOCX: never invented
        assert finding.location_precision is LocationPrecision.NONE
    # Standalone "Project Title" paragraph is flagged; the table label with a value is not.
    [label] = by_rule(result, "PH_TEMPLATE_LABEL")
    assert label.block_id and not label.block_id.startswith("d-t")
    assert result.identity.extracted.candidate_name is None  # "Your Name" is not a name


# --------------------------------------------------------------------------- sections


def test_missing_required_sections(results):
    result = results("missing_sections.pdf")
    status = {s.name: s.status for s in result.sections}
    assert status["education"] is CompletenessStatus.MISSING
    assert status["projects"] is CompletenessStatus.MISSING
    missing = {f.section for f in by_rule(result, "SEC_REQUIRED_MISSING")}
    assert missing == {"education", "projects"}
    assert all(
        f.finding_class is FindingClass.DEFINITE_DEFECT
        for f in by_rule(result, "SEC_REQUIRED_MISSING")
    )
    # Absent optional sections (achievements) are reported but never defects.
    assert status["achievements"] is CompletenessStatus.MISSING
    assert not any(f.section == "achievements" for f in result.findings)


def test_empty_and_placeholder_sections_are_distinct(results):
    result = results("resume.docx")
    status = {s.name: s.status for s in result.sections}
    assert status["certifications"] is CompletenessStatus.EMPTY
    assert status["achievements"] is CompletenessStatus.PLACEHOLDER_ONLY
    assert {"SEC_EMPTY", "SEC_PLACEHOLDER_ONLY"} <= rules(result)


# --------------------------------------------------------------------------- formatting


def test_different_font_families(results):
    findings = by_rule(results("mixed_fonts.pdf"), "FMT_MIXED_BODY_FONTS")
    assert findings
    for finding in findings:
        assert finding.evidence["font_family"] == "Times"
        assert finding.evidence["main_family"] == "Helvetica"
        assert finding.bounding_box is not None and finding.page_number == 1
        assert finding.section in {"projects", "experience"}


def test_consistent_fonts_not_flagged(results):
    # Bold headings in Helvetica-Bold + body in Helvetica is one family, not a defect.
    assert not {r for r in rules(results("resume.pdf")) if r.startswith("FMT_")}


def test_excessive_spacing(results):
    [gap] = by_rule(results("excessive_spacing.pdf"), "FMT_LARGE_GAP")
    assert gap.location_precision is LocationPrecision.REGION
    x0, y0, x1, y1 = gap.bounding_box
    assert y1 - y0 > 200
    assert gap.detected_text == "EXPERIENCE"
    assert gap.finding_class is FindingClass.POTENTIAL_ISSUE


def test_legitimate_two_column_resume_not_penalised(results):
    result = results("two_column.pdf")
    assert result.validation.status.value == "valid"
    assert not [f for f in result.findings if f.category.value in ("layout", "formatting")]
    assert not [f for f in result.findings if f.finding_class is FindingClass.DEFINITE_DEFECT]


# --------------------------------------------------------------------------- identity


def test_filename_identity_mismatch_triggers_review_only(results):
    result = results("mismatch")
    assert result.identity.status is IdentityStatus.POSSIBLE_MISMATCH
    assert result.identity.reference.source == "filename"
    assert not result.identity.reference.authoritative
    [finding] = by_rule(result, "IDENTITY_POSSIBLE_MISMATCH")
    assert finding.review_required and finding.rubric_criterion.value == "none"
    assert "verify manually" in finding.explanation
    assert result.review_required
    assert not result.grading.eligible_for_grading
    assert "review:IDENTITY_POSSIBLE_MISMATCH" in result.grading.blocked_by
    # The document itself stays a valid resume: identity does not change validation.
    assert result.validation.status.value == "valid"


def test_filename_identity_match_and_insufficient(results):
    assert results("match").identity.status is IdentityStatus.CONFIRMED_MATCH
    assert results("no_name").identity.status is IdentityStatus.INSUFFICIENT_INFORMATION
    assert not by_rule(results("no_name"), "IDENTITY_POSSIBLE_MISMATCH")


def test_common_words_in_filename_are_not_conflicts(settings, docs):
    # "two_column" is not a person's name; it must not cause a mismatch.
    assert analyze_file(docs["two_column.pdf"], settings).identity.status is (
        IdentityStatus.INSUFFICIENT_INFORMATION
    )


def test_manifest_is_authoritative(tmp_path, settings, docs):
    manifest = tmp_path / "students.csv"
    manifest.write_text(
        "file_name,student_name,student_id\n"
        "Priya_Desai_Resume.pdf,Asha Example,\n"
        "Final_Resume_ATS.pdf,Rahul Verma,\n",
        encoding="utf-8",
    )
    records = load_manifest(manifest)
    confirmed = analyze_file(docs["mismatch"], settings, records)  # filename wrong, manifest right
    assert confirmed.identity.status is IdentityStatus.CONFIRMED_MATCH
    assert confirmed.identity.reference.authoritative
    mismatch = analyze_file(docs["no_name"], settings, records)
    assert mismatch.identity.status is IdentityStatus.POSSIBLE_MISMATCH
    assert by_rule(mismatch, "IDENTITY_POSSIBLE_MISMATCH")[0].confidence >= 0.8


def test_filename_tokens(settings):
    from pps_ats.analysis.context import AnalysisContext

    class _Ctx:  # minimal stand-in exposing settings only
        pass

    ctx = _Ctx()
    ctx.settings = settings  # type: ignore[attr-defined]
    ref = filename_reference("RuchikaPatil_Resume_FINAL_2023012345.pdf", ctx)  # type: ignore[arg-type]
    assert ref.name_tokens == ["ruchika", "patil"]
    assert ref.student_ids == ["2023012345"]
    assert name_tokens("Ruchika  Patil") == ["ruchika", "patil"]
    assert AnalysisContext  # imported for typing only


# --------------------------------------------------------------------------- content


def test_repeated_bullets(results):
    [finding] = by_rule(results("repeated_bullets.pdf"), "CQ_REPEATED_LINE")
    assert "Built REST endpoints" in finding.detected_text
    assert finding.finding_class is FindingClass.DEFINITE_DEFECT
    assert finding.bounding_box is not None


def test_recommendations_are_not_defects(results):
    for name in ("resume.pdf", "two_column.pdf"):
        for finding in results(name).findings:
            if finding.rule_id in {
                "CQ_PROJECT_NO_CONTRIBUTION",
                "CQ_PROJECT_NO_TECH",
                "CQ_WEAK_VERB",
                "CQ_FILLER_PHRASE",
            }:
                assert finding.finding_class is FindingClass.RECOMMENDATION
                assert not finding.ats_impact


# --------------------------------------------------------------------------- language


def test_technology_names_not_flagged_as_spelling_errors(results):
    result = results("spelling_resume.pdf")
    flagged = {f.detected_text for f in by_rule(result, "LANG_SPELLING")}
    for term in (
        "Python",
        "JavaScript",
        "TypeScript",
        "PostgreSQL",
        "GitHub",
        "FastAPI",
        "Kubernetes",
        "TensorFlow",
        "PyTorch",
        "NumPy",
        "OpenCV",
        "scikit",
        "Node",
        "MongoDB",
        "Jupyter",
        "Firebase",
        "Tailwind",
        "Postman",
        "MEERA",
        "TESTCASE",
        "testcase",
        "github",
        "example",
    ):
        assert term not in flagged, term
    assert {"dashbord", "Devloped", "Pyhton"} <= flagged
    pyhton = next(f for f in by_rule(result, "LANG_SPELLING") if f.detected_text == "Pyhton")
    assert pyhton.evidence["suggestion"] == "Python"
    assert pyhton.bounding_box is not None


def test_clean_resume_has_no_spelling_findings(results):
    for name in ("resume.pdf", "resume.docx", "two_column.pdf"):
        assert not by_rule(results(name), "LANG_SPELLING"), name


# --------------------------------------------------------------------------- orchestration


def test_grade_card_named_as_resume_is_not_analysed(results):
    result = results("gradecard_named_resume")
    assert result.validation.status.value == "invalid"
    assert result.validation.classification.document_class.value == "non_resume"
    assert result.analysis_status == "skipped"
    assert result.findings == [] and result.sections == []
    assert not result.grading.eligible_for_grading and not result.grading.graded
    assert all(m.status == "skipped" for m in result.modules)


def test_image_only_resume_needs_review_and_is_not_analysed(results):
    result = results("image_only.pdf")
    assert result.validation.status.value == "needs_review"
    assert result.analysis_status == "skipped"
    assert result.review_required and not result.grading.eligible_for_grading


def test_validation_preserved_and_summary_consistent(results, docs, settings):
    result = results("template_resume.pdf")
    scan = scan_file(docs["template_resume.pdf"], settings)
    assert result.validation.model_dump(mode="json") == scan.validation.model_dump(mode="json")
    assert result.analysis_schema_version == ANALYSIS_SCHEMA_VERSION
    assert result.summary.total == len(result.findings)
    assert sum(result.summary.by_class.values()) == len(result.findings)
    assert sum(result.summary.by_severity.values()) == len(result.findings)
    assert len({f.issue_id for f in result.findings}) == len(result.findings)
    assert all(m.status == "completed" for m in result.modules)
    assert result.grading.graded is False


def test_issue_ids_are_deterministic(docs, settings):
    first = analyze_file(docs["template_resume.pdf"], settings)
    second = analyze_file(docs["template_resume.pdf"], settings)
    assert [f.issue_id for f in first.findings] == [f.issue_id for f in second.findings]


def test_module_failure_is_isolated(monkeypatch, docs, settings):
    from pps_ats.analysis import orchestrator

    def boom(ctx, state):
        raise RuntimeError("synthetic failure")

    patched = [(n, boom if n == "formatting" else f) for n, f in MODULES]
    monkeypatch.setattr(orchestrator, "MODULES", patched)
    scan = scan_file(docs["template_resume.pdf"], settings)
    result = analyze_scan(scan, settings)
    assert result.analysis_status == "partial"
    assert {m.name: m.status for m in result.modules}["formatting"] == "failed"
    assert "PH_CITY_STATE" in rules(result)  # other modules still ran
    assert result.review_required
    assert "module_failed:formatting" in result.grading.blocked_by


def test_findings_have_required_fields(results):
    required = {
        "issue_id",
        "rule_id",
        "category",
        "finding_class",
        "severity",
        "rubric_criterion",
        "detected_text",
        "explanation",
        "suggested_correction",
        "page_number",
        "bounding_box",
        "block_id",
        "span_id",
        "confidence",
        "detection_method",
        "review_required",
    }
    for name in ("template_resume.pdf", "template_resume.docx", "mixed_fonts.pdf"):
        for finding in results(name).findings:
            data = finding.model_dump(mode="json")
            assert required <= data.keys()
            assert 0.0 <= data["confidence"] <= 1.0
            if data["bounding_box"] is None:
                assert data["location_precision"] in ("none", "page")


# --------------------------------------------------------------------------- CLI / privacy


def test_cli_analyze_writes_json(docs, tmp_path, capsys):
    out = tmp_path / "out" / "analysis.json"
    assert main(["analyze", str(docs["template_resume.pdf"]), "--output", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["analysis_schema_version"] == ANALYSIS_SCHEMA_VERSION
    assert data["validation"]["status"] == "valid"
    assert any(f["rule_id"] == "PH_CITY_STATE" for f in data["findings"])
    assert "content" not in data  # full extracted content is not duplicated
    assert "findings=" in capsys.readouterr().err


def test_cli_analyze_with_manifest(docs, tmp_path):
    manifest = tmp_path / "m.csv"
    manifest.write_text(
        "file_name,student_name\nPriya_Desai_Resume.pdf,Asha Example\n", encoding="utf-8"
    )
    out = tmp_path / "a.json"
    assert (
        main(["analyze", str(docs["mismatch"]), "-o", str(out), "--manifest", str(manifest)]) == 0
    )
    assert json.loads(out.read_text(encoding="utf-8"))["identity"]["status"] == "confirmed_match"
    assert (
        main(["analyze", str(docs["mismatch"]), "--manifest", str(tmp_path / "missing.csv")]) == 2
    )


def test_analysis_logs_never_contain_document_text(docs, settings, caplog):
    with caplog.at_level(logging.DEBUG):
        for name in ("template_resume.pdf", "template_resume.docx", "mismatch"):
            analyze_file(docs[name], settings)
    logged = caplog.text
    assert "analysed file=" in logged
    for secret in (
        "meera.testcase@example.com",
        "City, State",
        "Smart Attendance",
        "Asha",
        "90000",
        "Lorem",
    ):
        assert secret not in logged


def test_manifest_record_lookup_is_case_insensitive(tmp_path):
    manifest = tmp_path / "m.csv"
    manifest.write_text(
        "Filename,Name,PRN,Email\nA_B.PDF,Asha Example,123456,a@example.com\n", encoding="utf-8"
    )
    records = load_manifest(manifest)
    assert records["a_b.pdf"] == ManifestRecord(
        "A_B.PDF", "Asha Example", "123456", "a@example.com"
    )
