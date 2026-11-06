# PPS ATS Resume Scanner

Standalone Python-based resume validation, extraction,
ATS analysis, faculty rubric scoring and reporting engine.

## Objectives
- Validate PDF and DOCX resume submissions
- Detect structural, formatting and content issues
- Generate evidence-backed corrections
- Calculate CA1 scores out of 30
- Generate annotated reports and batch Excel results

## Status

* **Milestone 1** — validation, PDF/DOCX extraction, section detection, structured JSON.
* **Milestone 2** — evidence-based findings: placeholders, section completeness, formatting
  and ATS layout risks, identity cross-check, content quality, spelling/grammar.
* **Milestone 3** — provisional CA1 rubric scoring (/30), separate ATS compatibility score
  (/100), optional AI interface (disabled), faculty review and audit log. Weights are
  provisional until calibrated with faculty; see [docs/SCORING.md](docs/SCORING.md).

Migrated from `pps-platform/backend` (feature/ats-engine); this repository is now the
standalone home of the scanner.

All processing is local. No document content is sent to any external service, and
document text is never written to logs.

## Setup

Requires Python 3.11+ (developed on 3.13).

```powershell
py -3.13 -m venv .venv-ats        # or py -3.11 / py -3.12
.venv-ats\Scripts\activate
python -m pip install -e ".[dev]"
```

## Usage

```powershell
# Full structured JSON (metadata, validation, sections, blocks/lines/spans, tables, images)
python -m pps_ats scan "path\to\resume.pdf" --output "outputs\resume.json"

# Same, without the extracted content (much smaller)
python -m pps_ats scan "path\to\resume.docx" --output "outputs\resume.json" --no-content

# Metadata / validation / section summary only, printed to stdout (no document text)
python -m pps_ats inspect "path\to\resume.pdf"

# Milestone 2: validation + findings (placeholders, sections, formatting, identity, ...)
python -m pps_ats analyze "path\to\resume.pdf" --output "outputs\resume.analysis.json"

# ... with an authoritative identity manifest (CSV: file_name, student_name, student_id, email)
python -m pps_ats analyze "path\to\resume.pdf" -o "outputs\a.json" --manifest "manifests\ca1.csv"

# Milestone 3: provisional CA1 marks + ATS score (append events to a local audit log)
python -m pps_ats score "path\to\resume.pdf" -o "outputs\resume.score.json" --audit-log "audit\ca1.jsonl"

# Faculty review of a scoring JSON: override, approve, or manually grade an ungraded file
python -m pps_ats review "outputs\resume.score.json" --reviewer F042 --reason "Objective is generic" --override content_relevance=7 --audit-log "audit\ca1.jsonl"
python -m pps_ats review "outputs\resume.score.json" --reviewer F042 --reason "Checked" --approve
python -m pps_ats review "outputs\scan.score.json" --reviewer F042 --reason "Scanned resume read manually" --manual content_relevance=6,structure_formatting=5,language_professional_tone=6

# Folder smoke-run: one JSON per file + summary.csv (no resume text in the CSV)
python scripts\scan_folder.py "path\to\folder" --output outputs\run1

# Generate synthetic sample documents (fictional data) for demos
python scripts\generate_samples.py samples
```

`pps-ats` is installed as a console script too (`pps-ats scan ...`).

Use `--config my.toml` (or the `PPS_ATS_CONFIG` env var) to override values from
`config/default.toml`. Keep private overrides in `config/local.toml` (git-ignored).

## Validation outcome

| status         | meaning                                                         |
|----------------|-----------------------------------------------------------------|
| `valid`        | readable, classified as a resume, no warnings                   |
| `needs_review` | processed, but a warning was raised or classification uncertain; also image-only / outline-text documents (possibly genuine but unreadable — never auto-graded) |
| `invalid`      | unsupported/corrupted/encrypted/empty/not-a-resume              |

`validation.eligible_for_grading` is `true` **only** for `valid` + `resume`. Later grading
milestones must refuse to score anything else.

Every reason is a stable issue code (`IMAGE_ONLY`, `NOT_A_RESUME`, `EXTENSION_MISMATCH`, ...)
with a human-readable message in `validation.review_reasons`.

Image-heavy resumes that still contain readable text are **not** invalidated (`IMAGE_HEAVY`
is informational). Filenames are never used to classify a document.

## Section statuses

`detected` (heading + content) · `empty` (heading, no content or placeholder-only) ·
`uncertain` (weak heading or very little content) · `missing` (no evidence).

## Output schema

`schema_version` (currently `1.0.0`) is included in every result. Top-level keys:
`document`, `validation`, `sections`, `content`, `errors`. PDF and DOCX share the same
structure. For DOCX, `content.diagnostics.coordinates_available` is `false` and every
`bbox`/`page` is `null`; DOCX pagination is not known without rendering.

## Findings (Milestone 2)

`analyze` returns an `AnalysisResult` (`analysis_schema_version` `1.0.0`); the Milestone 1
`ScanResult` schema is unchanged and its `validation` block is embedded verbatim.

Each finding has `issue_id` (deterministic), `rule_id`, `category`, `finding_class`,
`severity`, `rubric_criterion`, `ats_impact`, `detected_text`, `explanation`,
`suggested_correction`, `page_number`, `bounding_box`, `location_precision`, `block_id`,
`span_id`, `confidence`, `detection_method`, `review_required` and `evidence`.

| finding_class      | meaning                                                          |
|--------------------|------------------------------------------------------------------|
| `definite_defect`  | evidence-backed error (unfilled placeholder, empty section, ...) |
| `potential_issue`  | suspicious layout, incomplete entry, uncertain formatting        |
| `recommendation`   | optional improvement; not an ATS failure                         |

`bounding_box` is only filled from real parser coordinates (`location_precision`: `span`,
`line`, `block`, `region`, `page`). DOCX findings have `bounding_box = null` and
`location_precision = none`; they carry `block_id`/`span_id` instead.
`span_id` = `<block_id>:l<line>:s<span>` indexes into the `scan` JSON content.

Invalid documents, non-resumes and unreadable (image-only) documents are not analysed
(`analysis_status = skipped`). `grading.eligible_for_grading` is true only for a valid
resume with no finding that requires review (identity mismatch, garbled text, text in an
image, uncertain structure) and no failed module. `grading.graded` is always `false`.

All rules, word lists and thresholds are in `[analysis.*]` of `config/default.toml`.
The spelling allow-list is `src/pps_ats/resources/tech_terms.txt`
(extend it with `[analysis.language] extra_allowed_words` in a local config).

## Layout

```
config/default.toml        thresholds, section synonyms, non-resume phrases, placeholders
src/pps_ats/
  cli.py, __main__.py      CLI (scan / inspect / analyze / score / review)
  pipeline.py              detect -> validate -> parse -> sections -> classify -> status
  config.py                settings model + TOML loading
  schemas/                 Pydantic models (common, content, result, findings, scoring)
  validation/              filetype sniffing, checks, classifier, final decision
  parsers/                 pdf_parser (PyMuPDF), docx_parser (python-docx), layout, text utils
  analysis/sections.py     heading-based section segmentation (shared line->section map)
  analysis/context.py      read-only analysis view, location lookup, finding factory
  analysis/placeholders.py template leftovers + empty "Label:" fields
  analysis/completeness.py present / missing / empty / placeholder-only / uncertain
  analysis/formatting.py   fonts, sizes, spacing, alignment, margins, garbled text
  analysis/layout_checks.py columns, tables, text boxes, headers/footers, text in images
  analysis/identity.py     name/email/phone/ID extraction, filename/manifest cross-check
  analysis/content_quality.py projects, education, certifications, duplicates, verbs, filler
  analysis/language.py     local spelling (pyspellchecker + allow-list), basic grammar
  analysis/orchestrator.py runs modules, isolates failures, dedupes, grading gate
  resources/tech_terms.txt spelling allow-list (technology / academic terms)
  scoring/                 M3: deductions, engine, ats, ai (interface), review (audit)
  devtools/synthetic*.py   fictional fixture generators
scripts/                   generate_samples.py, scan_folder.py
tests/                     pytest suite (fixtures generated at test time)
```

## Tests

```powershell
python -m pytest
python -m ruff check src tests scripts
```

## Privacy rules

- Student documents, generated outputs, `.env`, local config and model weights are
  git-ignored (see `.gitignore`). Never commit real submissions.
- Logs contain file name, hash prefix, status, counts and timing only, never document text.
- Identity results (names, e-mails, IDs) appear only in the JSON output; manifests are
  git-ignored (`manifests/`, `*.csv`). An identity mismatch only requests manual review.
