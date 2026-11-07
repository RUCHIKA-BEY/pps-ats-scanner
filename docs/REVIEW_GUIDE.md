# Reviewing the calibration sheet

The workbook is private (it contains faculty comments and resume snippets). Keep it in
`D:\pps_private\corrected_only\reference_v1\review\` — not in OneDrive, e-mail or Git.

## Before you start

1. Close any other copy of the file. Make a backup copy of
   `annotation_review.xlsx` in the same private folder if you like.
2. Open it in Excel. If Excel shows "Protected View", click **Enable Editing**.
3. Go to the **calibration** sheet. (Leave the **evaluation** sheet alone: it is reviewed
   later, independently, ideally by a different person.)

## The columns

| colour of header | columns | what to do |
|---|---|---|
| blue | `candidate_id`, `document_id`, `page`, `faculty_correction`, `associated_section`, `source_type`, `faculty_likelihood`, `resume_context` | read only — never edit |
| grey (italic cells) | `suggested_label`, `alternative_labels`, `suggestion_reason` | the tool's guess — a hint, not an answer |
| gold (yellow cells) | `decision`, `final_label`, `faculty_intent`, `final_severity`, `reviewer_id`, `reviewer_notes` | **your input** |

`document_id` (P001…) is a pseudonym; the PDF is `reference_v1\corrected\P001.pdf` if you
need to see the faculty comment on the page.

## For every row

1. Read `faculty_correction` (and `resume_context` / the PDF if needed).
2. **decision** (drop-down):
   * `accept` — it is a genuine faculty correction or piece of advice;
   * `reject` — not a correction (stray mark, duplicate, the student's own text, empty box);
   * `unclear` — you cannot decide; it must be discussed before finalising.
3. If you accepted it:
   * **final_label** (drop-down): the label that fits. Use the suggestion only if you agree.
     Feedback that fits no label → `other_feedback` (it is kept). See the **labels** sheet;
     labels marked "outside scanner scope" are kept as faculty feedback and never counted
     against the scanner.
   * **faculty_intent** (drop-down): `correction_required` if the faculty says something is
     wrong or missing; `recommendation` if it is optional advice ("could", "consider",
     style suggestions); `unclear` if you cannot tell.
   * **final_severity** (optional): `high`, `medium` or `low` (blank = medium).
   * **reviewer_id**: your initials or staff ID.
4. **reviewer_notes** (optional): anything useful, e.g. "two corrections in one comment".

Tips:
* Use the filter arrows (row 1) to show only empty `decision` cells, or one document.
* Sorting is fine. Do **not** delete rows, insert rows, or change the blue columns —
  finalisation checks every row against the original list.
* The drop-downs refuse typing errors; capital letters and extra spaces are fine.
* Save often (**Ctrl+S**), keeping the `.xlsx` format and the same file name. You can close
  and continue later; decisions are kept.

## Check progress

Close Excel (or at least save), then in a terminal in the project folder:

```powershell
.venv-ats\Scripts\activate
python -m pps_ats review-status "D:\pps_private\corrected_only\reference_v1"
```

It prints counts only, for example:

```
calibration  378 rows | accepted  120 | rejected   40 | unclear    3 | not reviewed  215 | invalid   0 | missing   0 -> 218 remaining
```

and a `fix:` line (candidate ID + reason) for each row that needs attention — e.g. an
accepted row without `final_label`, `faculty_intent` or `reviewer_id`, or a changed /
missing row.

## Finalise the calibration sheet

When `review-status` shows `ready to finalise` for **calibration** (0 not reviewed,
0 unclear, 0 invalid, 0 missing):

```powershell
python -m pps_ats finalize-review "D:\pps_private\corrected_only\reference_v1" --split calibration
```

This writes `reference_v1\annotations.csv` from the **accepted** rows only and records the
review in `review_state.json`. It refuses (and writes nothing) if any row is still open,
so an incomplete review can never become ground truth. Afterwards:

```powershell
python -m pps_ats review-status "D:\pps_private\corrected_only\reference_v1"   # shows FINALISED
python -m pps_ats evaluate "D:\pps_private\corrected_only\reference_v1" --split calibration -o "D:\pps_private\corrected_only\eval_calibration"
```

The calibration comparison is refused until this step is done; the held-out evaluation
stays refused until the calibration choices are frozen and the evaluation sheet has been
reviewed independently (see `docs/FACULTY_INTAKE.md`, section 6).

If you change decisions after finalising, run `finalize-review --split calibration` again;
editing `annotations.csv` by hand is detected and blocks evaluation.
