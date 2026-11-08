# Milestone 5 — optional AI assistance

Disabled by default. When enabled, a model only *assists* with context the deterministic
scanner cannot judge:

* career-objective clarity and relevance (`career_objective`)
* quality of project descriptions (`project_description`)
* professional tone (`professional_tone`)
* contextual grammar (`contextual_grammar`)
* evidence-backed correction suggestions (in each observation)

## What the model can and cannot do

| | |
|---|---|
| sees | redacted section text (names, e-mails, phones, profile URLs, student IDs replaced), rubric descriptors, deterministic scores, scanner rule ids |
| never sees | invalid, non-resume or unreadable documents (they are not analysed) |
| may propose | per criterion an adjustment, clipped to ±`max_adjustment` (default 1). With the default `advisory_only = true` it is **recorded, never applied**; with `advisory_only = false` it is applied only if `confidence ≥ min_confidence` |
| must provide | JSON with a verbatim quote for every claim; quotes not found in the text are rejected and listed in `ai_rejected` |
| cannot | change validation, findings, review flags of the scanner, or final marks (faculty decide); a large proposal sends the document to review |
| on failure | timeouts, non-JSON output or schema errors keep the deterministic marks (`ai_failed` in the audit) |

Results: `CriterionScore.ai` (proposed/applied/confidence/evidence),
`ScoringResult.ai_observations` (advisory notes) and `ai_rejected`.

## Configuration (`[scoring.ai]`)

Local model (recommended): any OpenAI-compatible server on this computer, e.g. Ollama.

```toml
[scoring.ai]
enabled = true
provider = "local"
base_url = "http://127.0.0.1:11434/v1"   # loopback only; anything else is refused
model = "qwen2.5:7b-instruct"           # whatever the local server has pulled
request_timeout_s = 120
temperature = 0.0
seed = 7
```

External provider (only with institutional approval):

```toml
provider = "external"
base_url = "https://..."                # https required
allow_external = true
external_authorised_by = "Name, role, date of approval"
api_key_env = "PPS_ATS_AI_API_KEY"      # key read from the environment, never the config
```

Check the setup without any student data (sends a fictional resume):

```powershell
python -m pps_ats --config config\local.toml ai-check
```

With AI enabled the batch runs one worker and adds `request_timeout_s` to the
per-document timeout.

## Keep it disabled unless the benchmark says otherwise

Run `benchmark-ai` on the held-out faculty-corrected set (see `docs/EVALUATION.md`).
Enable AI for a faculty pilot only if the decision is `eligible_for_faculty_pilot`.

## Contextual review pathway (advisory, default)

Career-objective relevance, project quality and professional tone need context that
deterministic rules cannot judge. The pathway for them:

1. **Off by default.** `enabled = false`; when switched on, use a local model only (`provider = "local"`).
2. **Advisory only.** `advisory_only = true` (default): every proposal is stored in
   `CriterionScore.ai` with `accepted = false` and reason "advisory only"; CA1 marks are
   the deterministic marks. Only a faculty decision (override) changes a mark.
3. **Uncertainty.** Each proposal and observation carries the model's `confidence` (0-1)
   and `severity`; proposals below `min_confidence` are marked as such.
4. **Evidence.** Every claim must quote the resume verbatim; quotes not found in the
   redacted text are rejected (`ai_rejected`), so unsupported claims never reach faculty.
5. **Privacy.** Names, e-mails, phones, profile URLs and student IDs are redacted before
   the request; external providers are refused unless explicitly authorised.
6. **Adoption.** Applying adjustments (`advisory_only = false`) is a rubric decision that
   needs the `[evaluation.ai_adoption]` evidence (faculty-marked documents, MAE improvement)
   from `benchmark-ai`; until then observations are review aids only.

Deterministic companions (0 marks, reported with evidence): `CQ_OBJECTIVE_GENERIC`
(stock phrases), `CQ_OBJECTIVE_NO_ROLE` (fields named but no role), `CQ_OBJECTIVE_NO_TARGET`.
