# Milestone 4 — reports and batch processing

## Command

```powershell
python -m pps_ats batch INPUT_DIR -o PRIVATE_OUTPUT_DIR [--manifest students.csv]
       [--workers N] [--retry-failed] [--force] [--limit N] [--report-only]
       [--allow-synced-output] [--break-lock]
```

* `INPUT_DIR` is searched recursively. Every file becomes one submission row; Office lock
  files (`~$…`), `desktop.ini`, `Thumbs.db` and symbolic links are listed as ignored in the
  summary, never dropped silently. Unsupported files (e.g. `.doc`, images) get a row with
  validation status `invalid`.
* The manifest (`file_name, student_name, student_id, email`) may use the relative path
  (`div_A/resume.pdf`) or the bare file name. It supplies the student ID and the identity
  reference.
* Input files are only read. The output folder may not be inside the input folder, and a
  folder inside OneDrive/Dropbox/Google Drive/iCloud is refused unless
  `--allow-synced-output` is given.

## Output folder

| path | content |
|---|---|
| `ca1_results.xlsx` | consolidated workbook (rebuilt every run) |
| `results/<key>.json` | analysis + scoring + annotation outcome per submission |
| `annotated/<key>.annotated.pdf` | annotated copy (PDF submissions) |
| `findings_reports/<key>.findings.pdf` | generated findings report (DOCX submissions) |
| `manifest.jsonl` | processing journal: hash, status, attempts, outputs, errors |
| `batch_summary.json`, `runs/<run_id>.json` | counts and timings |

`<key>` = readable file stem + hash of the relative path, so equal file names in different
folders never collide. If `ca1_results.xlsx` is open in Excel, the workbook is written as
`ca1_results_<run_id>.xlsx` instead.

## Resume and restart

* The journal line for a document is written (and fsynced) when it starts and when it
  finishes. After Ctrl+C or a crash, run the same command again: finished documents are
  reused, the interrupted one is processed again. A torn last line is ignored.
* A result is reused only when the file hash **and** the settings fingerprint (engine
  version + all analysis/scoring/report settings + manifest hash) are unchanged; changing
  the config or the manifest re-processes everything. `--force` re-processes all files.
* Failed documents stay failed on the next run (listed in Manual Review) until
  `--retry-failed`. A failure never stops the batch.
* A lock file prevents two runs on one output folder; `--break-lock` removes a stale lock
  after a killed run.

## Workbook

| sheet | rows |
|---|---|
| CA1 Grades | one per submission; scores empty (not 0) when not graded |
| Detailed Findings | one per finding: annotation status, whether/how much it affected the score |
| Manual Review | one per (submission, reason): invalid, unreadable, not_a_resume, uncertain_classification, validation_warning, identity_mismatch, duplicate_submission, multiple_submissions, processing_failed, not_processed, needs_review |
| Processing Summary | counts, averages (all marked documents / releasable only), run id, timings |
| Scoring Configuration | rubric/scanner versions, policy fingerprint, all deduction settings |

Batch-level checks: identical files submitted under different paths are flagged
`duplicate_submission`; several files with one manifest student ID are flagged
`multiple_submissions`. Either makes the row non-releasable (`needs_review`).
Text from submissions is always written as literal text (never an Excel formula).

## Annotation

| status | meaning |
|---|---|
| `word` | the detected text was found inside the finding's own span/line box and the text under the mark was read back and matched (repeated words elsewhere are never marked) |
| `area` | box drawn from the parser's span/line/block/region coordinates (text not matched exactly) |
| `page_note` | page-level finding: a note on that page |
| `not_located` | no reliable coordinates: listed in the page-1 legend note and the workbook |
| `report_only` | DOCX: findings report only, no page coordinates claimed |

Markup by class: definite defect = red highlight, potential issue = orange squiggly,
recommendation = blue underline. Each comment has the explanation, suggestion and CA1
effect. The original's SHA-256 is re-checked after annotating.

## Workers

`--workers N` (bounded by `[batch] max_workers`) runs documents in separate processes; the
main process alone writes the journal. Default is 1: on the development machine extra
workers did not increase throughput for small synthetic resumes (see the Milestone 4
report). No AI model is loaded in batch mode.
