# Milestone 3 — CA1 scoring architecture

Status: **provisional**. The weights in `[scoring]` of `config/default.toml` are a
starting point for faculty calibration. They were not fitted to previous Excel marks,
which are not treated as ground truth.

## Flow

```
file ─► scan (M1) ─► analyse (M2 findings) ─► score (M3) ─► faculty review ─► final marks
                                               │                │
                                               ├─ CA1 /30       ├─ approve
                                               ├─ ATS /100      ├─ override (per criterion)
                                               └─ optional AI   └─ manual grade (ungraded docs)
```

| module | responsibility |
|---|---|
| `scoring/deductions.py` | findings → per-criterion deductions, de-duplication, caps |
| `scoring/engine.py` | criterion scores, bands, status, optional AI step, audit entries |
| `scoring/ats.py` | ATS compatibility score (separate from CA1) |
| `scoring/ai.py` | `ContextualEvaluator` protocol, request/response models, redaction |
| `scoring/review.py` | override / approve / manual grade, hash-chained audit log |
| `schemas/scoring.py` | `ScoringResult` (versioned, `scoring_schema_version` 1.0.0) |

## Criterion score

```
deterministic = clamp( min(max_points − Σ capped group deductions, ceiling), floor, max_points )
```

* Each finding is routed to its `rubric_criterion` (Content & Relevance, Structure &
  Formatting, Language & Professional Tone). ATS-only and identity findings (`none`)
  never reduce CA1 marks.
* Nominal points come from `[scoring.rules.<RULE>].points`, else the
  `finding_class × severity` table, multiplied by the finding's confidence.
* `ceiling`: Content & Relevance is capped at 9 by default because "tailored to the role
  / clear goals" cannot be verified deterministically. An accepted AI adjustment or a
  faculty override can award 10.
* `floor` = 1 for any gradeable resume (rubric bands start at 1).
* Bands: excellent ≥ 9, good ≥ 6, needs improvement below. `near_band_boundary` flags
  raw scores within `boundary_margin` of a band edge.

## Avoiding duplicate penalties

Applied in order; every finding gets one `FindingContribution` with its outcome
(`applied`, `absorbed`, `rule_cap`, `zero`) and reason:

1. **Supersede** — a section-level finding covers the line-level findings it summarises
   (`[scoring.supersedes]`, e.g. `SEC_PLACEHOLDER_ONLY` covers `PH_*` in that section).
2. **Same text** — findings of one group on the same line whose detected texts overlap
   count once; different problems on the same line still count.
3. **Rule cap** — `max_count` occurrences per rule (e.g. one font inconsistency reported
   in many places).
4. **Group cap** — the total of a group per criterion is capped
   (`[scoring.groups.<name>].max_points`), so one problem type cannot dominate.

## Status

| status | when | marks |
|---|---|---|
| `not_graded` | invalid, non-resume, unreadable / image-only (analysis skipped) | none |
| `needs_review` | any review flag (identity mismatch, garbled text, AI disagreement, failed module, validation warning) | provisional, `releasable=false` |
| `provisional` | no review flags | provisional, `releasable=true` |
| `faculty_reviewed` | after approve / override / manual grade | final |

## Optional AI evaluation

Disabled by default (`[scoring.ai] enabled = false`); nothing is sent anywhere.
An evaluator implements `ContextualEvaluator.evaluate(AIEvaluationRequest)` and returns
per-criterion proposals with a confidence and rationale. The engine:

* redacts e-mails, phones, profile URLs, the candidate name and student IDs first;
* clips each proposal to `±max_adjustment` (default 1 mark);
* applies it only if `confidence ≥ min_confidence`, otherwise records it as rejected;
* sets `needs_review` when `|proposal| ≥ review_disagreement`;
* keeps the deterministic marks if the evaluator fails.

No model is trained or bundled in this milestone.

## Faculty review and audit

* `apply_override`, `approve` and `manual_grade` return a new result; the system's
  `deterministic_score` / `provisional_score` are kept next to `final_score`.
* Every action needs a reviewer id and a reason, and appends an `AuditEntry`.
* `AuditLog` writes JSONL with a SHA-256 hash chain (`prev_hash` → `entry_hash`);
  `verify()` returns the index of the first altered entry. Store it under `audit/`
  (git-ignored).
* `config_hash` and `rubric_version` are stored with every result, so marks can be
  reproduced with the same configuration.

## Calibration (next step)

1. Score a stratified sample of real submissions.
2. Have two faculty members grade the same sample independently.
3. Compare per criterion; adjust `[scoring]` values; bump `rubric_version`.
4. Re-run and record agreement before releasing marks.
