---
name: codex-review
description: >-
  Run an independent, read-only Codex second-opinion review on scientific methods,
  numerical conclusions, implementation plans, or code, then verify and filter
  findings with AGY as primary authority. Use when the user asks Codex to review
  work, cross-check conclusions/plans/code, or inspect/change the Codex review model.
---

# Codex Second-Opinion Review Workflow

This plugin runs **all reviews as detached, unlimited-duration background jobs** via MCP (`codex_review`, `codex_followup`, `codex_review_status`, `codex_review_config`) paired with a sandbox-safe local waiter (`codex-review-wait`)—giving you **no runtime cap (30+ min supported)**, **zero network permission prompts**, and **zero polling**.

## 1. Prepare a De-Biased Review Packet

Before launching Codex, summarize the target objectively so Codex neither agrees automatically nor guesses hidden context:

1. **Problem & Goal**: What scientific hypothesis, computational task, plan, or feature is being designed/implemented/evaluated.
2. **Method, Equations, or Plan**: Concrete steps, mathematical definitions, normalization/split rules, or architectural invariants.
3. **Observed Evidence & Experimental Context**: Quantitative metrics AND exact references/paths to raw outputs, evaluation logs, dataset split/filtering definitions, sample sizes (`N`), random seeds/statistical tests, and baseline configurations (omit if pre-implementation plan).
4. **Workspace & Key Files**: Always provide `workspace_dir` and `focus_files` — and optionally `git_scope` (`"uncommitted"`, `"base:<branch>"`, `"commit:<sha>"`) — so Codex reads the real files in `--sandbox read-only` mode rather than relying solely on prose.

Do not use leading or self-defending wording (e.g., avoid *"confirm that this method is optimal"*).

## 2. Launch Background Review & Sleep Until Automatic Wakeup

The unified adaptive prompt ([review.md](./references/prompts/review.md)) handles plans, standalone math/science, pure engineering code, and joint scientific + code frameworks without needing a `mode` parameter.

1. **Launch via MCP (`codex_review` or `codex_followup`)**:
   - Call `codex_review` (do not override `model`, `reasoning_effort`, or `timeout_sec` unless asked; defaults are hot-reloaded from `~/.config/codex-reviewer/config.json` with `timeout_sec: 0` = unlimited).
   - The MCP server spawns a detached worker outside the terminal sandbox and returns `job_id`, `report_path`, and `wait_command` in `< 100ms`.
2. **Attach Sandboxed Waiter (`codex-review-wait <job_id>`)**:
   - Immediately run `codex-review-wait <job_id>` (or `~/.local/bin/codex-review-wait <job_id>`) via `run_command` with **`WaitMsBeforeAsync: 5000`** and **`BypassSandbox: false`** (Standard Sandbox works natively because `codex-review-wait` only inspects `/tmp/codex-reviewer-jobs/<job_id>`).
   - If the review finishes within 5s, you get the report right away; otherwise `run_command` moves to background.
3. **Stop & Sleep (Zero Polling)**:
   - Once `codex-review-wait` is in the background, **end the turn immediately** (do NOT poll `manage_task`, `codex_review_status`, or `schedule`).
   - AGY sleeps at zero token cost until the background task finishes and automatically wakes AGY up with the complete `report.md`.
4. **Status / Cancel / Fallback**:
   - Only call `codex_review_status` (`action="get"` / `"cancel"` / `"list"`) if the user explicitly asks for progress or cancellation, or to recover a finished report if the session restarted.
   - Standalone CLI `codex-review-run` remains available for manual terminal invocations.

## 3. Independent Verification & Optional Follow-up

After results return:
1. **Independently inspect** every `[FUNDAMENTAL / MUST-FIX]` and `[VERIFY / EDGE-CASE RISKS]` item against actual code, formulas, and outputs in the workspace.
2. **Optional 1-turn follow-up**: Only if a critical claim appears to rest on a misread file or missing premise, call `codex_followup` with the returned `session_id` (then `codex-review-wait <new_job_id>`) to resolve it cleanly.

## 4. Two-Tier Adjudication (AGY Remains Primary)

Apply strict triage before changing code, plans, or conclusions:

- **Adopt & Fix Immediately (Substantive / Fundamental Defects)**:
  - Verified data, formula, unit, or statistical calculation errors.
  - Data leakage, flawed controls, or invalid experimental comparisons.
  - Confirmed code bugs, shape/index errors, or silent data corruption.
  - Infeasible/contradictory plan steps or mismatch between stated plan/method and actual implementation.
  - Conclusions factually invalidated by the data or equations.
- **Ignore & Preserve Original Stance (Non-Fundamental / Tone Policing)**:
  - Claims that wording is *"too strong"*, *"too absolute"*, or needs more hedging/caveats when the underlying math, data, and code are correct.
  - Purely stylistic prose edits, subjective naming preferences, or speculative over-engineering.

## 5. Report Concisely to the User

Summarize outcome clearly in three short parts:
1. **Verified & Adopted Fixes**: Any fundamental defects confirmed and fixed (or explicitly state none were found).
2. **Unresolved Substantive Risks**: Any substantive findings or boundary risks that could not yet be verified locally and require follow-up or empirical checks (omit or write `None.` if all substantive items were resolved).
3. **Filtered / Rejected Feedback**: Briefly note in one line any non-substantive items or false positives dismissed after verification.

## 6. Model & Config Management

When the user asks which Codex model is active or wants to switch models/reasoning effort/timeout, call `codex_review_config` (`action="get"` / `"set"` / `"reset"`), or remind them they can run `codex-review-config` in any terminal.
