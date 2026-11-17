# codex-reviewer

Antigravity (AGY) plugin integrating non-interactive, read-only Codex second-opinion reviews (`gpt-6.1-sol` + `high` reasoning effort, `timeout_sec: 0` = unlimited by default) with **pure background MCP execution**, zero-polling automatic wakeup, hot-reloaded model configuration, and AGY-primary adjudication.

## Architecture & Features

- **Unlimited-Duration Background MCP (`codex_review`, `codex_followup`, `codex_review_status`, `codex_review_config`)**:
  - Calling `codex_review` or `codex_followup` immediately spawns a detached background worker (outside the terminal sandbox, with no 3-minute MCP RPC deadline and `timeout_sec: 0` unlimited duration) and returns `job_id` in `< 100ms`.
  - AGY pairs this with `codex-review-wait <job_id>` (runs inside Standard Sandbox reading `/tmp/codex-reviewer-jobs/<job_id>`, zero network permission prompts), goes idle with zero polling/token burn, and wakes up automatically with the full `report.md` when Codex finishes—even 30–60+ minutes later.
- **Read-only codebase verification**: Runs `codex -a never -s read-only exec ...` in your workspace so Codex checks real files, equations, and code-to-method alignment without modifying files.
- **Unified 3-in-1 adaptive prompt**: A single `review.md` template audits plans, scientific/mathematical rigor, code logic, and code-to-science alignment without mode-switching overhead while suppressing tone-policing or hedging feedback.
- **AGY-primary adjudication**: Bundled `rules/AGENTS.md` enforces independent verification—mandatory adoption of verified fundamental defects, explicit reporting of unresolved substantive risks, and automatic filtering of stylistic/tone nitpicks.
- **Hot-reloaded model config**: Change models, reasoning effort, or timeout anytime via CLI, chat, or JSON without restarting AGY.

## Prerequisites (on any machine)

1. [Antigravity CLI (`agy`)](https://antigravity.google/docs)
2. [Codex CLI (`codex`)](https://github.com/openai/codex) installed and authenticated (`codex login`)
3. `python3` (3.9+, standard library only)

## Installation on Another Computer

```bash
git clone https://github.com/bingbing12138/codex-review.git ~/.local/share/codex-reviewer
~/.local/share/codex-reviewer/install.sh
```

Then start a new `agy` session (or restart the IDE/desktop app). Verify installation with:

```bash
agy plugin list
codex-review-config status
codex-review-wait --help
```

> If `codex-review-config` / `codex-review-wait` is not found in your shell, add `~/.local/bin` to your `PATH` (e.g., `export PATH="$HOME/.local/bin:$PATH"`).

## Updating to Latest Version

```bash
cd ~/.local/share/codex-reviewer && git pull && ./install.sh
```

Existing `~/.config/codex-reviewer/config.json` customizations are preserved during updates.

## Switching Models / Updating Codex Settings (Zero Restart)

Runtime config lives at `~/.config/codex-reviewer/config.json` and is re-read on every launch (`timeout_sec: 0` means no duration limit):

1. **Terminal CLI**:
   ```bash
   codex-review-config status
   codex-review-config set gpt-6-astra xhigh
   codex-review-config set gpt-6.1-sol high
   codex-review-config set --timeout 0
   codex-review-config reset
   ```
2. **In AGY Chat**: Tell AGY *"把 codex review 模型切到 gpt-6-astra xhigh"* (uses `codex_review_config`).
3. **Edit JSON directly**: `~/.config/codex-reviewer/config.json`.

## Plugin Layout

```text
codex-reviewer/
├── plugin.json
├── mcp_config.json
├── install.sh
├── bin/
│   ├── codex-review-config
│   ├── codex-review-wait
│   └── codex-review-run
├── rules/
│   └── AGENTS.md
└── skills/
    └── codex-review/
        ├── SKILL.md
        ├── references/prompts/
        │   └── review.md
        └── scripts/
            └── codex_mcp.py
```
