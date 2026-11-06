from __future__ import annotations

import json
import logging

from pps_ats.cli import main
from pps_ats.pipeline import scan_file
from pps_ats.schemas import SCHEMA_VERSION


def test_scan_writes_json(samples, tmp_path, capsys):
    out = tmp_path / "out" / "result.json"
    assert main(["scan", str(samples["resume.pdf"]), "--output", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["validation"]["status"] == "valid"
    assert data["validation"]["eligible_for_grading"] is True
    assert data["content"]["blocks"][0]["bbox"] is not None
    assert "status=valid" in capsys.readouterr().err


def test_scan_no_content(samples, tmp_path):
    out = tmp_path / "r.json"
    main(["scan", str(samples["resume.docx"]), "-o", str(out), "--no-content"])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "content" not in data and data["sections"]


def test_inspect_prints_metadata_without_text(samples, capsys):
    assert main(["inspect", str(samples["resume.pdf"])]) == 0
    printed = capsys.readouterr().out
    summary = json.loads(printed)
    assert summary["validation"]["document_class"] == "resume"
    assert summary["extraction"]["pages"][0]["size_pt"] == [595.0, 842.0]
    assert "Weather Data Pipeline" not in printed  # no body text in inspect output


def test_missing_file_exit_code(tmp_path, capsys):
    assert main(["inspect", str(tmp_path / "missing.pdf")]) == 2


def test_pdf_and_docx_share_schema(samples, settings):
    pdf = json.loads(scan_file(samples["resume.pdf"], settings).model_dump_json())
    docx = json.loads(scan_file(samples["resume.docx"], settings).model_dump_json())
    assert pdf.keys() == docx.keys()
    assert pdf["content"].keys() == docx["content"].keys()
    assert pdf["content"]["blocks"][0].keys() == docx["content"]["blocks"][0].keys()
    assert [s["name"] for s in pdf["sections"]] == [s["name"] for s in docx["sections"]]


def test_logs_never_contain_document_text(samples, settings, caplog):
    with caplog.at_level(logging.DEBUG):
        for name in ("resume.pdf", "resume.docx", "grade_card.pdf", "corrupted.pdf"):
            scan_file(samples[name], settings)
    logged = caplog.text
    assert "scanned file=" in logged
    for secret in ("asha.example@example.com", "98765", "Weather Data Pipeline", "SGPA"):
        assert secret not in logged
