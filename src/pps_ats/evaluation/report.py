"""Write evaluation reports: ``evaluation.json`` (complete), ``evaluation.xlsx`` (sheets
for faculty) and ``evaluation.md`` (one-page summary)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from pps_ats.evaluation.compare import ErrorStats, EvaluationReport, IssueCounts
from pps_ats.evaluation.dataset import CRITERIA

_HEAD = PatternFill("solid", fgColor="1F3864")


def _put(ws: Any, row: int, col: int, value: Any) -> None:
    cell = ws.cell(row=row, column=col)
    if isinstance(value, str):
        cell.value = ILLEGAL_CHARACTERS_RE.sub("", value)[:1000]
        cell.data_type = "s"  # never a formula
    elif isinstance(value, (list, dict)):
        cell.value = json.dumps(value)
        cell.data_type = "s"
    else:
        cell.value = value


def _table(wb: Workbook, title: str, rows: list[dict], columns: list[str] | None = None) -> None:
    ws = wb.create_sheet(title)
    columns = columns or (list(rows[0]) if rows else ["(none)"])
    for c, name in enumerate(columns, 1):
        _put(ws, 1, c, name)
        ws.cell(row=1, column=c).font = Font(bold=True, color="FFFFFF")
        ws.cell(row=1, column=c).fill = _HEAD
        ws.column_dimensions[get_column_letter(c)].width = max(10, min(45, len(name) + 4))
    for r, item in enumerate(rows, 2):
        for c, name in enumerate(columns, 1):
            _put(ws, r, c, item.get(name))
    ws.freeze_panes = "A2"


def _err_rows(report: EvaluationReport) -> list[dict]:
    out = []
    for name, st in [*report.criterion_errors.items(), ("total /30", report.total_error)]:
        out.append({"criterion": name, **st.model_dump()})
    return out


def _issue_rows(report: EvaluationReport) -> list[dict]:
    rows = [{"label": k, **v.model_dump()} for k, v in report.issue_metrics.items()]
    rows.append({"label": "ALL (mapped labels)", **report.issue_overall.model_dump()})
    rows.append({"label": "HIGH SEVERITY", **report.high_severity.model_dump()})
    return rows


def _fmt(value: Any) -> str:
    return "–" if value is None else (f"{value:g}" if isinstance(value, float) else str(value))


def _stats_line(name: str, st: ErrorStats) -> str:
    return (
        f"| {name} | {st.n} | {_fmt(st.mae)} | {_fmt(st.bias)} | {_fmt(st.rmse)} | "
        f"{_fmt(st.max_abs)} | {_fmt(st.within)} (±{st.tolerance:g}) | {_fmt(st.band_agreement)} |"
    )


def _counts_line(name: str, c: IssueCounts) -> str:
    return (
        f"| {name} | {c.tp} | {c.fn} | {c.fp} | {c.fp_unverified} | "
        f"{_fmt(c.precision)} | {_fmt(c.recall)} |"
    )


def markdown(report: EvaluationReport) -> str:
    lines = [
        f"# Evaluation: {report.variant}",
        "",
        f"- Split: **{report.split}** ({'held-out' if report.held_out else 'IN-SAMPLE'})",
        f"- Documents: {report.documents} (missing {len(report.documents_missing)}, "
        f"failed {len(report.documents_failed)})",
        f"- Dataset fingerprint `{report.dataset_fingerprint}`, scoring fingerprint "
        f"`{report.scoring_fingerprint}`, rubric {report.rubric_version}, "
        f"engine {report.engine_version}"
        + (f", AI `{report.ai_model}`" if report.ai_model else ""),
        "",
        "## Document validation",
        "",
        "| expected \\ scanner | "
        + " | ".join(sorted({p for r in report.validation_confusion.values() for p in r}))
        + " |",
    ]
    predicted = sorted({p for r in report.validation_confusion.values() for p in r})
    lines.append("|---|" + "---|" * len(predicted))
    for exp, row in sorted(report.validation_confusion.items()):
        lines.append(f"| {exp} | " + " | ".join(str(row.get(p, 0)) for p in predicted) + " |")
    lines += [
        f"\nValidation errors: {len(report.validation_errors)}",
        "",
        "## Marks (scanner − faculty; faculty-entered marks only)",
        "",
        "| criterion | n | MAE | bias | RMSE | max | within | band agreement |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in CRITERIA:
        lines.append(_stats_line(c, report.criterion_errors[c]))
    lines.append(_stats_line("total /30", report.total_error))
    lines += [
        "",
        "Excluded from marks: "
        + ", ".join(f"{k} = {v}" for k, v in report.score_exclusions.items()),
        "",
        "## Issues (faculty annotations vs scanner detections)",
        "",
        "| label | matched | missed | false | unverified | precision | recall |",
        "|---|---|---|---|---|---|---|",
    ]
    for label, c in report.issue_metrics.items():
        lines.append(_counts_line(label, c))
    lines.append(_counts_line("**all mapped labels**", report.issue_overall))
    lines.append(_counts_line("**high severity**", report.high_severity))
    lines += [
        "",
        "`unverified` = scanner detections on documents whose faculty annotations are marked "
        "incomplete; they are not counted as errors.",
        "",
        f"## Uncertain or ungradable cases: {len(report.uncertain_cases)}",
        "",
        "## Fine-tuning readiness",
        "",
        "```",
        json.dumps(report.fine_tuning_readiness, indent=2),
        "```",
    ]
    if report.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in report.warnings]
    return "\n".join(lines) + "\n"


def write_report(report: EvaluationReport, output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    (output / "evaluation.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (output / "evaluation.md").write_text(markdown(report), encoding="utf-8")
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    summary = [
        ("Variant", report.variant),
        ("Split", report.split),
        ("Held-out", report.held_out),
        ("Documents", report.documents),
        ("Documents missing", len(report.documents_missing)),
        ("Documents failed", len(report.documents_failed)),
        ("Validation errors", len(report.validation_errors)),
        ("Total MAE /30", report.total_error.mae),
        ("Total bias /30", report.total_error.bias),
        ("Issue recall (mapped)", report.issue_overall.recall),
        ("Issue precision (verified)", report.issue_overall.precision),
        ("High-severity missed (FN)", report.high_severity.fn),
        ("High-severity false (FP)", report.high_severity.fp),
        ("Uncertain / ungradable cases", len(report.uncertain_cases)),
        ("Dataset fingerprint", report.dataset_fingerprint),
        ("Scoring fingerprint", report.scoring_fingerprint),
        ("AI model", report.ai_model),
        ("Created (UTC)", report.created_at),
    ]
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 60
    for r, (k, v) in enumerate(summary + [("Warning", w) for w in report.warnings], 1):
        _put(ws, r, 1, k)
        _put(ws, r, 2, v)
    _table(wb, "Validation Errors", report.validation_errors)
    _table(wb, "Issue Metrics", _issue_rows(report))
    _table(wb, "Issue Detail", report.issue_rows)
    _table(wb, "Criterion Errors", _err_rows(report))
    _table(wb, "Score Detail", report.score_rows)
    _table(wb, "Uncertain Cases", report.uncertain_cases)
    path = output / "evaluation.xlsx"
    wb.save(path)
    return path


def _pair_row(name: str, values: list) -> str:
    return f"| {name} | {_fmt(values[0])} | {_fmt(values[1])} |"


def write_benchmark(decision: dict, a: EvaluationReport, b: EvaluationReport, output: Path) -> Path:
    lines = [
        "# AI assistance benchmark (A = deterministic, B = deterministic + AI)",
        "",
        f"Split: **{a.split}** · documents compared: {decision['documents_compared']} · "
        f"AI model: `{b.ai_model}`",
        "",
        "| metric | A | B |",
        "|---|---|---|",
        f"| total MAE /30 | {_fmt(decision['total_mae_a'])} | {_fmt(decision['total_mae_b'])} |",
    ]
    for c, (ma, mb) in decision["criterion_mae"].items():
        lines.append(f"| {c} MAE | {_fmt(ma)} | {_fmt(mb)} |")
    lines += (
        [
            _pair_row("issue recall", decision["issue_recall"]),
            f"| issue precision | {_fmt(decision['issue_precision'][0])} | "
            f"{_fmt(decision['issue_precision'][1])} |",
            _pair_row("high-severity missed", decision["high_severity_fn"]),
            _pair_row("high-severity false", decision["high_severity_fp"]),
            "",
            "Mean change in absolute total error (B − A): "
            f"{_fmt(decision['abs_error_change_mean'])} "
            f"(95% CI {decision['abs_error_change_ci95']})",
            "",
            f"## Decision: **{decision['decision']}**",
            "",
        ]
        + [f"- {r}" for r in decision["reasons"]]
        + ["", decision["note"]]
    )
    path = output / "benchmark.md"
    output.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
