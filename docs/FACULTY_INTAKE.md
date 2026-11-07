# Faculty intake: corrected resumes → reference dataset

Status: the importer is tested on **synthetic** corrected PDFs only. It has not yet been
validated against real faculty annotations; the first real batch is that validation.

## 1. Where to put the files (outside OneDrive, outside this repository)

```
D:\pps_private\intake_pilot\                  <- INTAKE (any local, non-synced folder)
  originals\
    PPS2026-0001.pdf                          the student's submission exactly as graded
    PPS2026-0002.docx                         (PDF or DOCX)
    ...
  corrected\
    PPS2026-0001_corrected.pdf                the teacher's corrected copy (PDF only)
    PPS2026-0002_corrected.pdf                accepted suffixes: _corrected -corrected
    ...                                       _reviewed -reviewed _annotated _checked
  marks.csv                                   optional: faculty-assigned marks
  intake.csv                                  optional: only if names cannot follow the rule
```

* **File name = document id.** Use one id per resume (a roll number or an anonymised id
  such as `PPS2026-0001`). The original and the corrected copy must share it.
* Avoid student names in file names where possible; the id is enough.
* `corrected\` must contain the teacher's actual annotated PDF (with comments/highlights),
  not a re-typed summary. A corrected copy can be missing; the resume is still imported.
* If names cannot follow the rule, add `intake.csv`:

  ```
  document_id,original_file,corrected_file
  PPS2026-0001,Asha resume final.pdf,Asha resume final - checked by AB.pdf
  ```

### marks.csv (only marks a faculty member actually assigned)

```
document_id,content_relevance,structure_formatting,language_professional_tone,total,grader_id,source
PPS2026-0001,7,8,6,21,F01,faculty
PPS2026-0002,5,7,7,19,F02,faculty
```

A row is imported only if: the id matches a file in `originals\`; all three criterion
marks are present and 0–10; `grader_id` is filled; `total` (if given) equals the sum;
`source` is `faculty` or empty; the id appears once. Every other row is listed in the
import report and **not** imported. Do **not** copy the old AI-generated Excel grades into
this file; if any are included, mark them `source = ai` so they are excluded.

## 2. What the importer reads

| teacher used | read as | review |
|---|---|---|
| highlight / underline / strike-out / squiggly with a comment | exact quoted words (from the annotation's coordinates), page, comment, replies, author, date | auto-suggested label if the comment maps to exactly one label |
| sticky note / text box | comment, page, nearby text | auto-suggested label if one label |
| red text typed onto the page | counted only if it does **not** occur in the original (evidence that it was added) | always manual |
| red text also present in the original | the student's own styling | ignored (listed) |
| pen/ink drawings, circles, boxes, red lines | region + nearby words | always manual |
| scanned / photographed pages (handwritten) | flagged per page | always manual (transcribe) |
| marks written in comments (`24/30`) | listed in `marks_mentioned` | never imported; use marks.csv |

## 3. Commands for a 30–50 document pilot

```powershell
# 0. environment (once)
.venv-ats\Scripts\activate

# 1. import (reads INTAKE, writes DATASET; refuses OneDrive/Dropbox output)
python -m pps_ats import-corrections "D:\pps_private\intake_pilot" -o "D:\pps_private\ref_pilot"

# 2. review in Excel:  D:\pps_private\ref_pilot\annotation_candidates.csv
#    - every row with review_status = needs_review: reviewer_decision = accept | reject
#    - fix final_label (canonical labels, see docs/EVALUATION.md), final_severity, final_text
#    - auto_mapped rows are accepted unless you write reject
#    and in documents.csv: annotations_complete = yes only where the teacher marked every
#    issue; expected_validation = valid | needs_review | invalid if the teacher judged it

# 3. write annotations.csv (refuses while decisions are missing or labels are invalid)
python -m pps_ats finalize-annotations "D:\pps_private\ref_pilot"

# 4. freeze the calibration/evaluation split (pilot: hold out most documents)
python -m pps_ats eval-split "D:\pps_private\ref_pilot" --eval-fraction 0.6

# 5. evaluate the deterministic scanner on the held-out part
python -m pps_ats evaluate "D:\pps_private\ref_pilot" -o "D:\pps_private\eval_pilot_A"
```

Outputs to read: `ref_pilot\import_report.md` (what was found, rejected marks, problems)
and `eval_pilot_A\evaluation.md` / `.xlsx`.

Pilot size: 30–50 resumes from more than one class/teacher, including a few known
problem cases (scanned, template leftovers, wrong file). At least 30 with faculty marks
in the evaluation split are needed before any AI comparison (`benchmark-ai`).

## 4. Privacy

* INTAKE and DATASET stay on the local disk, outside OneDrive and outside the repository
  (`.gitignore` also excludes `*.pdf`, `*.docx`, `*.csv`, `*.xlsx`).
* The importer copies files; it never modifies or deletes anything in INTAKE.
* Nothing is sent to any external service.

## 5. Corrected-only intake (no separate originals)

Use when faculty returned annotated PDFs and the students' originals are not available.

```
D:\pps_private\corrected_only\
  intake\                      the faculty-corrected PDFs (flat, or in intake\corrected\)
    <document_id>.pdf          e.g. PPS2026-0001.pdf (suffixes like _corrected are stripped)
  marks.csv                    optional, same format as above; leave out if none exist
```

```powershell
python -m pps_ats import-corrections "D:\pps_private\corrected_only\intake" -o "D:\pps_private\corrected_only\dataset" --corrected-only
```

What it does (the corrected PDF is never written to):

| found in the PDF | treated as | scanner input |
|---|---|---|
| annotation objects (FreeText, sticky notes, markup, shapes) | faculty corrections (one annotation = one correction) | annotation objects deleted from the copy |
| red text in the page content that reads as a section heading in the resume font | the student's own styling | kept |
| red text in the page content with instruction wording, not overlapping other text | likely faculty (flattened) | glyphs removed by text redaction; document set to `needs_review` until confirmed |
| other red text, or red text drawn over student text | ambiguous | document `needs_review`, excluded from evaluation |
| page without a text layer | handwritten/scanned | `manual_transcription`, excluded |

Every copy is verified: no annotation objects left, no faculty red text extractable, and
the student's non-red characters identical page by page. `documents.csv` gets a
`scanner_input_status` column; only `clean` documents are evaluated.

Review CSV columns added in this mode: `source_type`, `faculty_likelihood`,
`associated_section`, `section_basis`. Labels come from `[evaluation.import.patterns]`
(instruction phrasing) then `[evaluation.import.keywords]`; `section_title` and
`section_order` are faculty-only labels with no scanner rule yet (always reported as
missed). Without marks.csv all score fields stay empty and the evaluation reports issue
agreement only (no grading accuracy).

### 5-document pilot

```powershell
python -m pps_ats import-corrections "D:\pps_private\corrected_only\intake_pilot5" -o "D:\pps_private\corrected_only\pilot5" --corrected-only
# review pilot5\annotation_candidates.csv (reviewer_decision, final_label, reviewer_note)
python -m pps_ats finalize-annotations "D:\pps_private\corrected_only\pilot5"
python -m pps_ats eval-split "D:\pps_private\corrected_only\pilot5" --eval-fraction 1.0
python -m pps_ats evaluate "D:\pps_private\corrected_only\pilot5" -o "D:\pps_private\corrected_only\pilot5_eval"
```

With 5 documents this checks the workflow, not accuracy. For the full set, freeze a real
split (e.g. `--eval-fraction 0.4`) and tune keyword/pattern lists only on calibration rows.
