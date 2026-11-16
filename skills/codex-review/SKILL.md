---
name: codex-review
description: >-
  Run an independent, read-only Codex second-opinion review on scientific methods,
  numerical conclusions, implementation plans, or code, then verify and filter
  findings with AGY as primary authority. Use when the user asks Codex to review
  work, cross-check conclusions/plans/code, or inspect/change the Codex review model.
---

# Codex Second-Opinion Review Workflow

Use the plugin's native MCP tools (`codex_review`, `codex_followup`, `codex_review_config`) so reviews run silently with zero terminal prompts and return results directly into the active conversation.

## 1. Prepare a De-Biased Review Packet

Before calling `codex_review`, summarize the target objectively so Codex neither agrees automatically nor guesses hidden context:

1. **Problem & Goal**: What scientific hypothesis, computational task, or feature is being implemented/evaluated.
2. **Method, Equations, or Plan**: Concrete steps, mathematical definitions, normalization/split rules, or architectural invariants.
3. **Observed Evidence / Claims**: Quantitative outputs, metrics, or final conclusions under review.
4. **Workspace & Key Files**: Always pass `workspace_dir` (current repo/project root) and `focus_files` so Codex reads the real files in `--sandbox read-only` mode rather than relying solely on prose.

Do not use leading or self-defending wording (e.g., avoid *"confirm that this method is optimal"*).

## 2. Select the Review Mode & Call `codex_review`

Choose the `mode` matching the built-in prompt templates in [references/prompts](./references/prompts):

- **`science`**: Scientific methodology, equations vs. code alignment, data/statistical calculation, train/val/test leakage, baseline fairness, and conclusion validity ([science_review.md](./references/prompts/science_review.md)).
- **`plan`**: Technical feasibility, hidden assumptions, logical gaps, edge cases, and verification design ([plan_review.md](./references/prompts/plan_review.md)).
- **`code`**: Implementation bugs, numerical/indexing/type errors, plan-to-code mismatch, and optionally `git_scope` (`"uncommitted"`, `"base:<branch>"`, `"commit:<sha>"`) ([code_review.md](./references/prompts/code_review.md)).

Default model configuration (`gpt-6.1-sol` + `high` reasoning effort) is automatically loaded from `~/.config/codex-reviewer/config.json`. Only pass `model` or `reasoning_effort` overrides when the user explicitly requests a one-off override.

## 3. Independent Verification & Optional Follow-up

After `codex_review` returns:

1. **Independently inspect** every `[FUNDAMENTAL / MUST-FIX]` and `[VERIFY / EDGE-CASE RISKS]` item against actual code, formulas, and outputs in the workspace.
2. **Optional 1-turn follow-up**: Only if a critical claim appears to rest on a misread file or missing premise, call `codex_followup` with the returned `session_id` to resolve it cleanly.

## 4. Two-Tier Adjudication (AGY Remains Primary)

Apply strict triage before changing code, plans, or conclusions:

- **Adopt & Fix Immediately (Substantive / Fundamental Defects)**:
  - Verified data, formula, unit, or statistical calculation errors.
  - Data leakage, flawed controls, or invalid experimental comparisons.
  - Confirmed code bugs, shape/index errors, or silent data corruption.
  - Mismatch between stated logical plan/method and actual implementation.
  - Conclusions factually invalidated by the data or equations.
- **Ignore & Preserve Original Stance (Non-Fundamental / Tone Policing)**:
  - Claims that wording is *"too strong"*, *"too absolute"*, or needs more hedging/caveats when the underlying math, data, and code are correct.
  - Purely stylistic prose edits, subjective naming preferences, or speculative over-engineering.

## 5. Report Concisely to the User

Summarize outcome clearly in two short parts:
1. **Verified & Adopted Fixes**: Any fundamental defects confirmed and fixed (or explicitly state none were found).
2. **Filtered / Rejected Feedback**: Briefly note in one line any non-substantive items or false positives dismissed after verification.

## 6. Model & Config Management

When the user asks which Codex model is active or wants to switch models/reasoning effort (e.g., when Codex updates or new models release), call `codex_review_config` (`action="get"` / `"set"` / `"reset"`), or remind them they can run `codex-review-config` in any terminal.
