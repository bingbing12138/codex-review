#!/usr/bin/env python3
"""Stdio MCP server wrapping the local Codex CLI for read-only second-opinion reviews."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SERVER_NAME = "codex-reviewer"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

CONFIG_PATH = Path(
    os.environ.get(
        "CODEX_REVIEWER_CONFIG",
        str(Path.home() / ".config" / "codex-reviewer" / "config.json"),
    )
)
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "references" / "prompts"

VALID_MODES = ("science", "plan", "code")
VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}

DEFAULT_CONFIG: dict[str, Any] = {
    "model": "gpt-6.1-sol",
    "reasoning_effort": "high",
    "codex_bin": "codex",
    "timeout_sec": 300,
    "extra_codex_args": [],
    "model_aliases": {
        "gpt-sol 6.1": "gpt-6.1-sol",
        "gpt-sol-6.1": "gpt-6.1-sol",
        "sol-6.1": "gpt-6.1-sol",
        "sol": "gpt-6.1-sol",
        "gpt-astra": "gpt-6-astra",
        "gpt-6-astra": "gpt-6-astra",
        "astra": "gpt-6-astra",
        "gpt-luna": "gpt-6-luna",
        "luna": "gpt-6-luna",
        "gpt-terra": "gpt-6.1-terra",
        "terra": "gpt-6.1-terra",
    },
}

SERVER_INSTRUCTIONS = (
    "AGY is the lead engineer and scientific investigator with final decision authority; "
    "Codex provides strictly advisory read-only second opinions. Always verify Codex findings "
    "against actual workspace code, math, and data before deciding. Must-adopt verified "
    "fundamental bugs, data/formula calculation errors, data leakage, plan-implementation "
    "mismatches, and false conclusions; filter and ignore tone policing, hedging complaints, "
    "and purely stylistic nitpicks. Runtime config hot-reloads from "
    f"{CONFIG_PATH}."
)


def _merge_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Merge user config over defaults without losing default aliases."""
    merged = dict(DEFAULT_CONFIG)
    aliases = dict(DEFAULT_CONFIG["model_aliases"])
    user_aliases = raw.get("model_aliases")
    if isinstance(user_aliases, dict):
        for k, v in user_aliases.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                aliases[k.strip()] = v.strip()
    for key in ("model", "reasoning_effort", "codex_bin", "timeout_sec", "extra_codex_args"):
        if key in raw and raw[key] is not None:
            merged[key] = raw[key]
    merged["model_aliases"] = aliases
    return merged


def load_config() -> dict[str, Any]:
    """Load runtime config from disk on every call for zero-restart hot reload."""
    if not CONFIG_PATH.exists():
        return _merge_config({})
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return _merge_config(data)
    except Exception as exc:  # noqa: BLE001
        print(f"[{SERVER_NAME}] warning: failed reading {CONFIG_PATH}: {exc}", file=sys.stderr)
    return _merge_config({})


def save_config(cfg: dict[str, Any]) -> None:
    """Persist runtime config atomically."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=str(CONFIG_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_name, CONFIG_PATH)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def resolve_model_and_effort(
    model_arg: str | None,
    effort_arg: str | None,
    config: dict[str, Any],
) -> tuple[str, str]:
    """Normalize informal model names/aliases and resolve reasoning effort."""
    raw_model = (model_arg if model_arg is not None else str(config.get("model", ""))).strip()
    if not raw_model:
        raw_model = str(DEFAULT_CONFIG["model"])

    explicit_effort = (effort_arg if effort_arg is not None else "").strip().lower()
    inferred_effort = ""

    parts = raw_model.split()
    if len(parts) >= 2 and parts[-1].lower() in VALID_EFFORTS:
        inferred_effort = parts[-1].lower()
        raw_model = " ".join(parts[:-1])

    normalized_key = re.sub(r"\s+", " ", raw_model).strip().lower()
    alias_map = {
        re.sub(r"\s+", " ", k).strip().lower(): v
        for k, v in config.get("model_aliases", {}).items()
    }
    resolved_model = alias_map.get(normalized_key, raw_model)

    if explicit_effort:
        resolved_effort = explicit_effort
    elif inferred_effort:
        resolved_effort = inferred_effort
    else:
        resolved_effort = str(config.get("reasoning_effort", "")).strip().lower()

    if resolved_effort in ("none", "off", "default", "unset"):
        resolved_effort = ""

    return resolved_model, resolved_effort


def resolve_codex_bin(config: dict[str, Any]) -> str:
    """Resolve the codex executable path across PATH and common install locations."""
    configured = str(config.get("codex_bin") or "").strip()
    if configured:
        if os.sep in configured and Path(configured).exists():
            return configured
        found = shutil.which(configured)
        if found:
            return found

    found_default = shutil.which("codex")
    if found_default:
        return found_default

    home = Path.home()
    candidates = [
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
        home / ".local" / "bin" / "codex",
        home / ".npm-global" / "bin" / "codex",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    raise FileNotFoundError(
        f"Codex binary not found (checked '{configured}', PATH, and common bin paths). "
        "Install Codex CLI or update 'codex_bin' via codex_review_config / codex-review-config."
    )


def load_prompt_template(mode: str) -> str:
    """Load the built-in review prompt template for the requested mode."""
    prompt_file = PROMPTS_DIR / f"{mode}_review.md"
    if not prompt_file.exists():
        raise FileNotFoundError(f"Missing prompt template: {prompt_file}")
    return prompt_file.read_text(encoding="utf-8").strip()


def build_review_prompt(
    mode: str,
    content: str,
    focus_files: list[str] | None,
    extra_instructions: str | None,
    git_scope: str | None,
) -> str:
    """Assemble a de-biased, structured review prompt for Codex."""
    sections = [load_prompt_template(mode)]

    if focus_files:
        file_lines = "\n".join(f"- {p}" for p in focus_files if isinstance(p, str) and p.strip())
        if file_lines:
            sections.append(f"## Focus Files to Inspect (Read-Only)\n{file_lines}")

    if git_scope:
        sections.append(
            "## Git Scope Context\n"
            f"Inspect changes matching scope `{git_scope}` in the workspace alongside the target below."
        )

    if extra_instructions and extra_instructions.strip():
        sections.append(f"## Additional Review Instructions\n{extra_instructions.strip()}")

    sections.append(f"<review_target>\n{content.strip()}\n</review_target>")
    return "\n\n".join(sections)


def build_base_cmd(
    codex_bin: str,
    model: str,
    effort: str,
    workspace_dir: str | None,
    extra_args: list[str],
) -> list[str]:
    """Build common non-interactive read-only Codex CLI prefix flags."""
    cmd = [codex_bin, "-a", "never", "-s", "read-only"]
    if workspace_dir:
        cmd.extend(["-C", workspace_dir])
    if model:
        cmd.extend(["-m", model])
    if effort:
        cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
    cmd.extend(str(a) for a in extra_args if str(a).strip())
    return cmd


def parse_codex_jsonl(stdout_text: str) -> tuple[str | None, str, list[str]]:
    """Extract session/thread ID, fallback agent message, and errors from Codex JSONL."""
    session_id: str | None = None
    messages: list[str] = []
    errors: list[str] = []

    for line in stdout_text.splitlines():
        raw = line.strip()
        if not raw or not raw.startswith("{"):
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        if not session_id:
            for key in ("thread_id", "session_id"):
                val = event.get(key)
                if isinstance(val, str) and val:
                    session_id = val
                    break
            if not session_id:
                for container in ("thread", "session", "msg", "payload"):
                    sub = event.get(container)
                    if isinstance(sub, dict):
                        val = sub.get("id") or sub.get("session_id") or sub.get("thread_id")
                        if isinstance(val, str) and val:
                            session_id = val
                            break

        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                messages.append(text.strip())

        msg = event.get("msg")
        if isinstance(msg, dict) and msg.get("type") == "agent_message":
            text = msg.get("message") or msg.get("text")
            if isinstance(text, str) and text.strip():
                messages.append(text.strip())

        ev_type = str(event.get("type") or "").lower()
        if ev_type in ("error", "turn.failed", "stream_error"):
            err_val = event.get("error") or event.get("message")
            if isinstance(err_val, dict):
                err_val = err_val.get("message") or json.dumps(err_val, ensure_ascii=False)
            if err_val:
                errors.append(str(err_val))

    last_message = messages[-1] if messages else ""
    return session_id, last_message, errors


def run_codex_subprocess(
    cmd: list[str],
    prompt_text: str,
    cwd: str | None,
    timeout_sec: int,
    out_path: Path,
) -> tuple[int, str, str, str | None, str]:
    """Execute Codex CLI with stdin prompt and capture output file + JSONL telemetry."""
    proc = subprocess.run(
        cmd,
        input=prompt_text,
        text=True,
        capture_output=True,
        cwd=cwd or None,
        timeout=timeout_sec,
        check=False,
    )
    session_id, jsonl_msg, jsonl_errors = parse_codex_jsonl(proc.stdout or "")
    file_text = ""
    if out_path.exists():
        file_text = out_path.read_text(encoding="utf-8").strip()
    final_text = file_text or jsonl_msg
    combined_err = (proc.stderr or "").strip()
    if jsonl_errors:
        joined = "\n".join(jsonl_errors)
        combined_err = f"{combined_err}\n{joined}".strip() if combined_err else joined
    return proc.returncode, final_text, combined_err, session_id, (proc.stdout or "")


def _validate_workspace(workspace_dir: str | None) -> str | None:
    """Resolve and verify optional workspace directory."""
    if not workspace_dir or not workspace_dir.strip():
        cwd = os.getcwd()
        return cwd if os.path.isdir(cwd) else None
    resolved = os.path.abspath(os.path.expanduser(workspace_dir.strip()))
    if not os.path.isdir(resolved):
        raise ValueError(f"workspace_dir does not exist or is not a directory: {resolved}")
    return resolved


def _parse_git_scope(git_scope: str) -> list[str]:
    """Translate git_scope shorthand into `codex exec review` flags."""
    scope = git_scope.strip()
    if scope == "uncommitted":
        return ["--uncommitted"]
    if scope.startswith("base:") and len(scope) > 5:
        return ["--base", scope[5:].strip()]
    if scope.startswith("commit:") and len(scope) > 7:
        return ["--commit", scope[7:].strip()]
    raise ValueError(
        "Invalid git_scope. Expected 'uncommitted', 'base:<branch>', or 'commit:<sha>'."
    )


def handle_codex_review(args: dict[str, Any]) -> dict[str, Any]:
    """Run a non-interactive read-only Codex review and return structured findings."""
    mode = str(args.get("mode") or "science").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode '{mode}'. Must be one of: {', '.join(VALID_MODES)}.")

    content = str(args.get("content") or "").strip()
    git_scope_raw = args.get("git_scope")
    git_scope = str(git_scope_raw).strip() if isinstance(git_scope_raw, str) and git_scope_raw.strip() else None

    if not content and not git_scope:
        raise ValueError("Provide non-empty 'content' (or 'git_scope' in code mode).")
    if git_scope and mode != "code":
        raise ValueError("'git_scope' is only supported when mode='code'.")

    workspace_dir = _validate_workspace(args.get("workspace_dir"))
    focus_files = args.get("focus_files") if isinstance(args.get("focus_files"), list) else None
    extra_instructions = args.get("extra_instructions") if isinstance(args.get("extra_instructions"), str) else None

    config = load_config()
    codex_bin = resolve_codex_bin(config)
    model, effort = resolve_model_and_effort(args.get("model"), args.get("reasoning_effort"), config)
    timeout_sec = int(config.get("timeout_sec") or 300)
    extra_args = list(config.get("extra_codex_args") or [])

    full_prompt = build_review_prompt(
        mode=mode,
        content=content or f"Review the git changes specified by scope: {git_scope}",
        focus_files=focus_files,
        extra_instructions=extra_instructions,
        git_scope=git_scope,
    )
    base_cmd = build_base_cmd(codex_bin, model, effort, workspace_dir, extra_args)

    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="codex-reviewer-") as tmpdir:
        out_path = Path(tmpdir) / "review_output.md"
        used_subcommand = "exec"

        if git_scope:
            scope_flags = _parse_git_scope(git_scope)
            review_cmd = base_cmd + [
                "exec",
                "review",
                "--skip-git-repo-check",
                "--json",
                "-o",
                str(out_path),
                *scope_flags,
                "-",
            ]
            used_subcommand = "exec review"
            rc, final_text, stderr_text, session_id, _ = run_codex_subprocess(
                review_cmd, full_prompt, workspace_dir, timeout_sec, out_path
            )
            # Fall back to standard read-only `exec` if `exec review` rejects scope+prompt flags.
            if rc != 0 and not final_text and (
                "cannot be used with" in stderr_text or "Usage:" in stderr_text
            ):
                git_hint = (
                    f"\n\nRun read-only git inspection commands (`git status`, `git diff`, etc.) "
                    f"for scope `{git_scope}` before writing your review."
                )
                exec_cmd = base_cmd + [
                    "exec",
                    "--skip-git-repo-check",
                    "--color",
                    "never",
                    "--json",
                    "-o",
                    str(out_path),
                    "-",
                ]
                used_subcommand = "exec (git fallback)"
                rc, final_text, stderr_text, session_id, _ = run_codex_subprocess(
                    exec_cmd, full_prompt + git_hint, workspace_dir, timeout_sec, out_path
                )
        else:
            exec_cmd = base_cmd + [
                "exec",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--json",
                "-o",
                str(out_path),
                "-",
            ]
            rc, final_text, stderr_text, session_id, _ = run_codex_subprocess(
                exec_cmd, full_prompt, workspace_dir, timeout_sec, out_path
            )

    elapsed = round(time.monotonic() - start, 2)
    if rc != 0 and not final_text:
        raise RuntimeError(
            f"Codex `{used_subcommand}` failed (exit {rc}, model={model}, effort={effort or 'default'}):\n"
            f"{stderr_text or '(no stderr output)'}"
        )

    header = "\n".join(
        [
            "### Codex Second-Opinion Review",
            f"- **mode**: `{mode}`",
            f"- **model**: `{model}`",
            f"- **reasoning_effort**: `{effort or 'model-default'}`",
            f"- **session_id**: `{session_id or 'unavailable'}`",
            f"- **elapsed_sec**: `{elapsed}`",
            f"- **command**: `{used_subcommand}`",
        ]
    )
    if rc != 0 and stderr_text:
        header += f"\n- **warning**: Codex exited with code {rc}: {stderr_text}"

    body = final_text or "(Codex produced an empty response.)"
    return {
        "content": [{"type": "text", "text": f"{header}\n\n---\n\n{body}"}],
        "isError": False,
    }


def handle_codex_followup(args: dict[str, Any]) -> dict[str, Any]:
    """Resume an existing Codex review session to ask a targeted clarification question."""
    session_id = str(args.get("session_id") or "").strip()
    question = str(args.get("question") or "").strip()
    if not session_id:
        raise ValueError("'session_id' is required.")
    if not question:
        raise ValueError("'question' is required.")

    workspace_dir = _validate_workspace(args.get("workspace_dir"))
    config = load_config()
    codex_bin = resolve_codex_bin(config)
    model, effort = resolve_model_and_effort(args.get("model"), args.get("reasoning_effort"), config)
    timeout_sec = int(config.get("timeout_sec") or 300)
    extra_args = list(config.get("extra_codex_args") or [])

    base_cmd = build_base_cmd(codex_bin, model, effort, workspace_dir, extra_args)
    resume_cmd = base_cmd + [
        "exec",
        "resume",
        session_id,
        "--skip-git-repo-check",
        "--json",
        "-o",
    ]

    followup_prompt = (
        "Follow-up verification from the primary investigator. Answer concisely with concrete "
        "file:line, mathematical, or empirical evidence, and still ignore tone/style nitpicks:\n\n"
        f"{question}"
    )

    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="codex-followup-") as tmpdir:
        out_path = Path(tmpdir) / "followup_output.md"
        rc, final_text, stderr_text, resumed_id, _ = run_codex_subprocess(
            resume_cmd + [str(out_path), "-"],
            followup_prompt,
            workspace_dir,
            timeout_sec,
            out_path,
        )

    elapsed = round(time.monotonic() - start, 2)
    if rc != 0 and not final_text:
        raise RuntimeError(
            f"Codex `exec resume` failed (exit {rc}, session_id={session_id}):\n"
            f"{stderr_text or '(no stderr output)'}"
        )

    header = "\n".join(
        [
            "### Codex Follow-up Verification",
            f"- **session_id**: `{resumed_id or session_id}`",
            f"- **model**: `{model}`",
            f"- **reasoning_effort**: `{effort or 'model-default'}`",
            f"- **elapsed_sec**: `{elapsed}`",
        ]
    )
    body = final_text or "(Codex produced an empty follow-up response.)"
    return {
        "content": [{"type": "text", "text": f"{header}\n\n---\n\n{body}"}],
        "isError": False,
    }


def _detect_codex_version(codex_bin: str) -> str:
    """Return `codex --version` output or an explanatory fallback string."""
    try:
        proc = subprocess.run(
            [codex_bin, "--version"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        return (proc.stdout or proc.stderr or "").strip() or f"exit {proc.returncode}"
    except Exception as exc:  # noqa: BLE001
        return f"unavailable ({exc})"


def handle_codex_review_config(args: dict[str, Any]) -> dict[str, Any]:
    """Get, update, or reset the hot-reloaded Codex reviewer configuration."""
    action = str(args.get("action") or "get").strip().lower()
    if action not in ("get", "set", "reset"):
        raise ValueError("Invalid action. Must be 'get', 'set', or 'reset'.")

    if action == "reset":
        cfg = _merge_config({})
        save_config(cfg)
    else:
        cfg = load_config()
        if action == "set":
            if args.get("model") is not None or args.get("reasoning_effort") is not None:
                resolved_model, resolved_effort = resolve_model_and_effort(
                    args.get("model"),
                    args.get("reasoning_effort"),
                    cfg,
                )
                if args.get("model") is not None:
                    cfg["model"] = resolved_model
                if args.get("reasoning_effort") is not None or (
                    args.get("model") is not None and resolved_effort != cfg.get("reasoning_effort")
                ):
                    cfg["reasoning_effort"] = resolved_effort
            if args.get("timeout_sec") is not None:
                timeout_val = int(args["timeout_sec"])
                if timeout_val <= 0:
                    raise ValueError("'timeout_sec' must be a positive integer.")
                cfg["timeout_sec"] = timeout_val
            if args.get("codex_bin") is not None:
                cfg["codex_bin"] = str(args["codex_bin"]).strip()
            if args.get("extra_codex_args") is not None:
                if not isinstance(args["extra_codex_args"], list):
                    raise ValueError("'extra_codex_args' must be an array of strings.")
                cfg["extra_codex_args"] = [str(x) for x in args["extra_codex_args"]]
            save_config(cfg)

    resolved_model, resolved_effort = resolve_model_and_effort(None, None, cfg)
    codex_version = _detect_codex_version(str(cfg.get("codex_bin") or "codex"))
    payload = {
        "config_path": str(CONFIG_PATH),
        "codex_version": codex_version,
        "resolved_model": resolved_model,
        "resolved_reasoning_effort": resolved_effort or "model-default",
        "config": cfg,
    }
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, ensure_ascii=False)}],
        "isError": False,
    }


TOOLS_SPEC = [
    {
        "name": "codex_review",
        "description": (
            "Run a strictly read-only Codex second-opinion review against scientific methods/conclusions, "
            "implementation plans, or code. Uses built-in anti-nitpick review prompts and hot-reloaded "
            "model configuration (default: gpt-6.1-sol with high reasoning effort)."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["mode", "content"],
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["science", "plan", "code"],
                    "description": "Review lens: 'science' (math/data/leakage/conclusions), 'plan' (architecture/feasibility), or 'code' (bugs/spec alignment).",
                },
                "content": {
                    "type": "string",
                    "description": "Objective, de-biased summary of the plan, methodology, equations, or conclusion to audit.",
                },
                "workspace_dir": {
                    "type": "string",
                    "description": "Absolute path to the workspace directory so Codex can inspect real files read-only.",
                },
                "focus_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of key file paths relative to workspace_dir (or absolute) to inspect.",
                },
                "git_scope": {
                    "type": "string",
                    "description": "Optional git diff scope (only when mode='code'): 'uncommitted', 'base:<branch>', or 'commit:<sha>'.",
                },
                "extra_instructions": {
                    "type": "string",
                    "description": "Optional extra focus points or constraints for this review.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional one-off Codex model slug or alias override (e.g. 'gpt-6.1-sol', 'gpt-6-astra').",
                },
                "reasoning_effort": {
                    "type": "string",
                    "description": "Optional one-off reasoning effort override: low, medium, high, xhigh, max, ultra, or none.",
                },
            },
        },
    },
    {
        "name": "codex_followup",
        "description": (
            "Resume a prior Codex review session by session_id to challenge or clarify a specific "
            "finding before deciding whether to adopt or discard it."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id", "question"],
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID returned by a previous codex_review call.",
                },
                "question": {
                    "type": "string",
                    "description": "Specific technical follow-up or counter-evidence to verify.",
                },
                "workspace_dir": {
                    "type": "string",
                    "description": "Optional workspace directory path.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override for this follow-up turn.",
                },
                "reasoning_effort": {
                    "type": "string",
                    "description": "Optional reasoning effort override for this follow-up turn.",
                },
            },
        },
    },
    {
        "name": "codex_review_config",
        "description": (
            "View, update, or reset the hot-reloaded Codex reviewer configuration "
            "(model, reasoning_effort, timeout_sec, codex_bin, extra_codex_args) without restarting AGY."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get", "set", "reset"],
                    "description": "'get' current config, 'set' fields, or 'reset' to defaults.",
                },
                "model": {
                    "type": "string",
                    "description": "New default Codex model slug or alias (e.g. 'gpt-6.1-sol', 'gpt-6-astra').",
                },
                "reasoning_effort": {
                    "type": "string",
                    "description": "New default reasoning effort (low, medium, high, xhigh, max, ultra, none).",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "Subprocess timeout in seconds.",
                },
                "codex_bin": {
                    "type": "string",
                    "description": "Path to the codex CLI binary.",
                },
                "extra_codex_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional CLI flags forwarded to codex (e.g. ['--profile', 'research']).",
                },
            },
        },
    },
]

TOOL_HANDLERS = {
    "codex_review": handle_codex_review,
    "codex_followup": handle_codex_followup,
    "codex_review_config": handle_codex_review_config,
}


def read_rpc_message() -> tuple[dict[str, Any] | None, str]:
    """Read one JSON-RPC message supporting both JSONL and Content-Length framing."""
    stream = sys.stdin.buffer
    while True:
        line = stream.readline()
        if not line:
            return None, "jsonl"
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            headers: dict[str, str] = {}
            header_line = line
            while header_line and header_line.strip():
                decoded = header_line.decode("ascii", errors="replace")
                if ":" in decoded:
                    k, v = decoded.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
                header_line = stream.readline()
            length = int(headers.get("content-length", "0"))
            body = stream.read(length)
            return json.loads(body.decode("utf-8")), "content-length"
        return json.loads(stripped.decode("utf-8")), "jsonl"


def write_rpc_message(payload: dict[str, Any], framing: str) -> None:
    """Write one JSON-RPC message using the active stdio framing."""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    out = sys.stdout.buffer
    if framing == "content-length":
        header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
        out.write(header + raw)
    else:
        out.write(raw + b"\n")
    out.flush()


def dispatch_request(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Route a single MCP JSON-RPC request and return a response object (None for notifications)."""
    req_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    # Notifications have no `id` and must not emit a response.
    if req_id is None:
        return None

    try:
        if method == "initialize":
            proto = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
            result = {
                "protocolVersion": proto,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": SERVER_INSTRUCTIONS,
            }
            return {"jsonrpc": "2.0", "id": req_id, "result": result}

        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS_SPEC}}

        if method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments") or {}
            handler = TOOL_HANDLERS.get(tool_name)
            if handler is None:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                }
            try:
                tool_result = handler(tool_args)
            except Exception as exc:  # noqa: BLE001
                tool_result = {
                    "content": [{"type": "text", "text": f"Error in {tool_name}: {exc}"}],
                    "isError": True,
                }
            return {"jsonrpc": "2.0", "id": req_id, "result": tool_result}

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32603, "message": f"Internal error: {exc}"},
        }


def main() -> int:
    """Run the stdio JSON-RPC loop."""
    active_framing = "jsonl"
    while True:
        try:
            msg, framing = read_rpc_message()
        except Exception as exc:  # noqa: BLE001
            print(f"[{SERVER_NAME}] parse error: {exc}", file=sys.stderr)
            write_rpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                },
                active_framing,
            )
            continue

        if msg is None:
            return 0
        active_framing = framing
        response = dispatch_request(msg)
        if response is not None:
            write_rpc_message(response, active_framing)


if __name__ == "__main__":
    raise SystemExit(main())
