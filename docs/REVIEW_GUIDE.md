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

## Partial calibration pilot (before the whole sheet is reviewed)

```powershell
python -m pps_ats partial-pilot "D:\pps_private\corrected_only\reference_v1" -o "D:\pps_private\corrected_only\pilot_partial_<date>"
```

* Uses only **calibration** documents whose every row is decided (accept/reject, accepted
  rows complete with `final_label`, `faculty_intent`, `reviewer_id`) and whose scanner input
  is clean. Partly reviewed, unreviewed, ineligible and all held-out documents are excluded
  and counted. Nothing is auto-accepted or filled in.
* Reads the master workbook as saved on disk (save in Excel first); never writes to it, the
  split or the reference; refuses to overwrite an existing pilot folder.
* Reports recall for faculty corrections the scanner has rules for (`correction_required`);
  recommendations, labels outside scanner scope and unclear intent are counted separately.
  Scanner findings without a faculty annotation are *unverified*, not false positives, so
  precision and F1 are not computed; there are no marks, so no score agreement.
* Output: `pilot_summary.md/json`, `included_documents.csv`, `dataset\` and `report\`.

## Auditing a pilot's misses

```powershell
python -m pps_ats pilot-audit "D:\pps_private\corrected_only\reference_v1" --pilot "D:\pps_private\corrected_only\pilot_partial_<date>" -o "D:\pps_private\corrected_only\pilot_partial_<date>_audit"
```

Re-derives the scanner findings for the pilot's calibration documents (from the exact
workbook the pilot read: the master if unchanged, else the matching backup) and explains
every applicable faculty correction: misses by root cause (`no_finding`,
`section_not_detected`, `page_association`, `one_to_one_matching`,
`label_mapping_confirmed/unconfirmed`) and matches by validity (`valid`,
`different_section`, `topic_mismatch`, `topic_unverifiable`), with original vs strict
recall. Writes aggregate counts and pseudonymous ids only; never modifies the pilot,
the workbook, mappings or the split.

The audit replays the evaluation matcher (strict by default: a formatting/layout finding
must agree with the comment's topic, and a finding in an unrelated section is not credited);
add `--location-level` to audit a pilot produced before strict matching. To repeat a pilot on
the exact review input of an earlier run, pass that run's workbook backup:

```powershell
python -m pps_ats partial-pilot "D:\pps_private\corrected_only\reference_v1" -o "D:\pps_private\corrected_only\pilot_v2" --workbook "D:\pps_private\corrected_only\reference_v1\review\backups\<backup>.xlsx"
```
