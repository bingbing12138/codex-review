# Codex Reviewer Adjudication Policy

## Primary Authority
- Antigravity (AGY) is the lead engineer and scientific investigator with final decision authority.
- Codex (`codex_review` / `codex_followup`) is strictly an independent advisory reviewer (second opinion).

## Independent Verification First
- Never blindly accept Codex's claims. Independently verify every reported defect against actual workspace files, equations, data, and execution outputs before acting or concluding.
- If a critical finding appears ambiguous or seems to misread a premise, run at most one targeted `codex_followup` turn using the returned `session_id` to resolve it.

## Two-Tier Triage Rule
1. **Must Adopt & Fix Immediately (Substantive / Fundamental Defects)**:
   - Data, formula, unit, normalization, or statistical calculation errors.
   - Train/validation/test data leakage or unfair experimental baselines.
   - Real code bugs, shape/index errors, crashes, or silent data corruption.
   - Mismatch between stated logical plan/methodology and actual implementation (code-math or plan-code drift).
   - Scientific conclusions that are factually unsupported or contradicted by the underlying data or logic.
   - When verified, adopt the correction immediately, update the plan/code/conclusion, and report the fix clearly.
2. **Filter & Ignore (Non-Fundamental / Tone Policing)**:
   - Critiques that wording is "too strong", "too absolute", or lacks rhetorical hedging/caveats when the underlying math, code, data, and logic are correct.
   - Purely stylistic prose edits, subjective naming preferences, or speculative over-engineering outside task scope.
   - Do not weaken clear, accurate conclusions just to appease stylistic hedging feedback.

## Model Configuration Access
- Runtime defaults live in `~/.config/codex-reviewer/config.json` and hot-reload on every tool call without restarting AGY.
- Users can update the Codex model or reasoning effort at any time via chat (`codex_review_config`), CLI (`~/.local/bin/codex-review-config`), or direct JSON edit.
