# Milestone 5 — accuracy evaluation against faculty-corrected resumes

## Reference dataset

```
DATASET/
  documents.csv      one row per resume (required)
  annotations.csv    faculty issue annotations (optional)
  split_lock.json    frozen calibration/evaluation split (written by eval-split)
  files/...          the resumes (paths as given in documents.csv)
```

**documents.csv**

| column | required | values |
|---|---|---|
| `document_id` | yes | unique id |
| `file` | yes | path relative to the dataset (or `--documents`) |
| `split` | yes* | `calibration` (may be used to tune rules) or `evaluation` (held out) |
| `expected_validation` | no | `valid`, `needs_review`, `invalid` (the faculty view) |
| `marks_source` | with marks | `faculty` — anything else (`ai`, `unknown`) is **not** ground truth |
| `faculty_content_relevance`, `faculty_structure_formatting`, `faculty_language_professional_tone` | no | 0–10 each; all three or none |
| `faculty_total` | no | checked against the sum |
| `annotations_complete` | no | `yes` if every issue in the resume was annotated (enables false-positive counts) |
| `grader_id`, `notes` | no | |

\* `eval-split` fills empty splits deterministically and freezes them.

**annotations.csv**: `document_id, label` (required), `severity` (`high|medium|low`),
`text` (exact quote from the resume, strongly recommended), `page`, `criterion`, `source`
(`faculty` — others are excluded), `annotator_id`, `comment`.

`label` must be one of the canonical labels in `[evaluation.labels]` of
`config/default.toml` (placeholder, missing_section, empty_section, contact, spelling,
grammar, formatting, layout, career_objective, project_description, weak_language,
education_details, certification_details, duplicate_content, identity,
uncertain_structure). Unknown labels are reported as warnings.

## Keeping evaluation data separate from tuning

* Only the `evaluation` split is a held-out estimate; `evaluate` uses it by default.
  Evaluating `calibration` is allowed but every report is marked **IN-SAMPLE**.
* `eval-split` writes `split_lock.json`. Afterwards any document whose split changes
  (e.g. an evaluation resume moved into calibration after seeing results) makes the
  dataset load fail.
* Previously AI-generated marks (`marks_source` ≠ `faculty`) and AI annotations
  (`source` ≠ `faculty`) are loaded only to be counted and excluded.

## Commands

```powershell
python -m pps_ats eval-split  D:\pps_private\reference
python -m pps_ats evaluate    D:\pps_private\reference -o D:\pps_private\eval_A
python -m pps_ats evaluate    D:\pps_private\reference -o D:\pps_private\eval_B --variant B
python -m pps_ats benchmark-ai D:\pps_private\reference -o D:\pps_private\bench
```

Report folders inside OneDrive/Dropbox/... are refused unless `--allow-synced-output`.

## Report (`evaluation.json` / `evaluation.xlsx` / `evaluation.md`)

| section | content |
|---|---|
| Summary | variant, split, held-out flag, counts, fingerprints (dataset, scoring), AI model, warnings |
| Validation Errors | confusion matrix expected × scanner status; each mismatch as *wrongly rejected*, *wrongly accepted* or *review status differs* |
| Issue Metrics | per label: matched (TP), missed (FN), false (FP), unverified, precision, recall; plus all mapped labels and **high severity** |
| Issue Detail | every faculty annotation and scanner detection with its outcome and similarity |
| Criterion Errors | per criterion and total: n, MAE, bias (scanner − faculty), RMSE, max, share within ±1 (±2 for the total), band agreement |
| Score Detail | faculty vs scanner marks per document |
| Uncertain Cases | needs_review / not_graded / near a band boundary / classification uncertain / errors |

Matching: same document and label; texts match if one contains the other or word overlap
≥ `text_match_threshold`; an annotation without a quote matches on page. Several scanner
rules reporting the same text count once. False positives count only on documents with
`annotations_complete = yes`; elsewhere they are *unverified*.

High severity: faculty severity `high` (for misses), scanner severity in
`high_severity_levels` or class in `high_severity_classes` (for false detections).

## A/B benchmark and adoption rule

`benchmark-ai` runs the same documents with the deterministic scanner (A) and with AI
assistance (B) and writes `A_deterministic/`, `B_with_ai/`, `benchmark.md/json`.
AI is reported as **eligible for a faculty pilot** only if all `[evaluation.ai_adoption]`
conditions hold on the held-out split: at least 30 faculty-marked documents, total MAE
improves by ≥ 0.25, no criterion gets worse, no more high-severity misses or false
detections, and the 95% paired-bootstrap interval of the error change excludes zero.
Otherwise the decision is `keep_ai_disabled`. The benchmark never changes the config.

## Fine-tuning

Not implemented. The report's *fine-tuning readiness* section shows how many
fully-annotated, faculty-marked calibration documents and held-out documents exist
(defaults: 500 and 100 required). Even then it needs an explicit decision.
