---
name: codex-review
description: >-
  Run an independent, read-only Codex second-opinion review on scientific methods,
  numerical conclusions, implementation plans, or code, then verify and filter
  findings with AGY as primary authority. Use when the user asks Codex to review
  work, cross-check conclusions/plans/code, or inspect/change the Codex review model.
---

# Codex Second-Opinion Review Workflow

This plugin provides **two execution channels** sharing one unified adaptive prompt ([review.md](./references/prompts/review.md)) and hot-reloaded `~/.config/codex-reviewer/config.json` (`gpt-6.1-sol` + `high` reasoning effort, `timeout_sec: 0` = unlimited by default):

1. **Native MCP Channel (`codex_review`, `codex_followup`, `codex_review_config`)**: Best for fast/routine reviews (< 3 minutes) with zero terminal prompts.
2. **Async Background CLI Channel (`codex-review-run`)**: Best for deep, large-codebase, or long-running reviews (5–60+ minutes, `xhigh`/`max`/`ultra`, or explicit async requests) with **zero timeout cap** and **zero polling**.

## 1. Prepare a De-Biased Review Packet

Before launching Codex, summarize the target objectively so Codex neither agrees automatically nor guesses hidden context:

1. **Problem & Goal**: What scientific hypothesis, computational task, plan, or feature is being designed/implemented/evaluated.
2. **Method, Equations, or Plan**: Concrete steps, mathematical definitions, normalization/split rules, or architectural invariants.
3. **Observed Evidence / Claims**: Quantitative outputs, metrics, or final conclusions under review (omit if pre-implementation plan).
4. **Workspace & Key Files**: Always provide `workspace_dir` (`--workspace`) and `focus_files` (`--focus`) — and optionally `git_scope` (`"uncommitted"`, `"base:<branch>"`, `"commit:<sha>"`) — so Codex reads the real files in `--sandbox read-only` mode rather than relying solely on prose.

Do not use leading or self-defending wording (e.g., avoid *"confirm that this method is optimal"*).

## 2. Launch Review via Fast MCP or Async Background CLI

The single unified prompt ([review.md](./references/prompts/review.md)) automatically adapts to **plans, standalone math/science, pure software engineering code, and joint scientific + code frameworks** without needing a `mode` parameter.

### Channel A: Fast / Routine Reviews (Native MCP)
Call `codex_review` directly. Do not override `model` or `reasoning_effort` unless the user explicitly asks.

### Channel B: Deep / Complex / Long-Running Reviews (`codex-review-run` Background Wakeup)
Use when reviewing large multi-file codebases, complex scientific pipelines, using `xhigh`/`max`/`ultra`, or whenever the review could run 5–60+ minutes:

1. Write the de-biased review packet to `<artifactDir>/scratch/codex_packet.md`.
2. Launch `~/.local/bin/codex-review-run` (or `codex-review-run`) via `run_command` with `WaitMsBeforeAsync: 500` using a clean literal-argument command (no `&&`, pipes, or `$VAR` so prefix auto-approval works):
   ```text
   codex-review-run --workspace <dir> --prompt-file <artifactDir>/scratch/codex_packet.md --out <artifactDir>/scratch/codex_report.md [--focus <file> ...] [--git-scope <scope>]
   ```
   Follow standard sandbox execution rules (try sandboxed first; retry with `BypassSandbox: true` keeping identical `toolSummary` if sandbox blocks network/`~/.codex`).
3. **Stop & Sleep (Zero Polling)**: Once `run_command` moves to background, **end the turn immediately** (do NOT call `manage_task` or `schedule` to poll). AGY stays completely idle with zero token burn until the OS process finishes, at which point AGY is automatically woken up with the completion result.
4. Read `<artifactDir>/scratch/codex_report.md` if terminal output was truncated.

## 3. Independent Verification & Optional Follow-up

After results return:
1. **Independently inspect** every `[FUNDAMENTAL / MUST-FIX]` and `[VERIFY / EDGE-CASE RISKS]` item against actual code, formulas, and outputs in the workspace.
2. **Optional 1-turn follow-up**: Only if a critical claim appears to rest on a misread file or missing premise, call `codex_followup` (MCP) or `codex-review-run --followup <session_id> ...` (CLI) to resolve it cleanly.

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

Summarize outcome clearly in two short parts:
1. **Verified & Adopted Fixes**: Any fundamental defects confirmed and fixed (or explicitly state none were found).
2. **Filtered / Rejected Feedback**: Briefly note in one line any non-substantive items or false positives dismissed after verification.

## 6. Model & Config Management

When the user asks which Codex model is active or wants to switch models/reasoning effort/timeout, call `codex_review_config` (`action="get"` / `"set"` / `"reset"`), or remind them they can run `codex-review-config` in any terminal.
