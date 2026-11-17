#!/usr/bin/env python3
"""Detached background-job MCP server & worker wrapping the local Codex CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

SERVER_NAME = "codex-reviewer"
SERVER_VERSION = "2.0.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
_EOF = object()

CONFIG_PATH = Path(
    os.environ.get(
        "CODEX_REVIEWER_CONFIG",
        str(Path.home() / ".config" / "codex-reviewer" / "config.json"),
    )
)
CACHE_DIR = Path(
    os.environ.get(
        "CODEX_REVIEWER_CACHE",
        str(Path.home() / ".cache" / "codex-reviewer"),
    )
)
SESSIONS_PATH = CACHE_DIR / "sessions.json"
JOBS_DIR = Path(
    os.environ.get(
        "CODEX_REVIEWER_JOBS_DIR",
        "/tmp/codex-reviewer-jobs",
    )
)
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "references" / "prompts"
PROMPT_FILE = PROMPTS_DIR / "review.md"

VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}

DEFAULT_CONFIG: dict[str, Any] = {
    "model": "gpt-6.1-sol",
    "reasoning_effort": "high",
    "codex_bin": "codex",
    "timeout_sec": 0,
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
    "Codex provides strictly advisory read-only second opinions. codex_review and codex_followup "
    "launch detached background jobs with NO duration cap (returning job_id immediately). "
    "Run `codex-review-wait <job_id>` via run_command (BypassSandbox: false, WaitMsBeforeAsync: 5000) "
    "and end your turn to sleep with zero polling until automatic wakeup. Always verify Codex findings "
    "against actual workspace code, math, and data before deciding: adopt verified fundamental defects "
    "and ignore tone policing or stylistic nitpicks."
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


def _lock_file(lock_path: Path):
    """Acquire an exclusive advisory lock when fcntl is available."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(lock_path, "a+", encoding="utf-8")
    if fcntl is not None:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
    return lock_fh


def _unlock_file(lock_fh: Any) -> None:
    """Release an advisory lock cleanly."""
    try:
        if fcntl is not None:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    finally:
        lock_fh.close()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically using a same-directory temp file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically using a same-directory temp file and os.replace."""
    _atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


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
    """Persist runtime config atomically under an advisory lock."""
    lock_fh = _lock_file(CONFIG_PATH.with_suffix(".lock"))
    try:
        _atomic_write_json(CONFIG_PATH, cfg)
    finally:
        _unlock_file(lock_fh)


def update_config(mutator: Any) -> dict[str, Any]:
    """Load, mutate, and save config under a single exclusive lock."""
    lock_fh = _lock_file(CONFIG_PATH.with_suffix(".lock"))
    try:
        cfg = load_config()
        updated = mutator(cfg)
        _atomic_write_json(CONFIG_PATH, updated)
        return updated
    finally:
        _unlock_file(lock_fh)


def _record_session_workspace(session_id: str | None, workspace_dir: str | None) -> None:
    """Best-effort cache of session_id -> workspace_dir for workspace-safe follow-ups."""
    if not session_id or not workspace_dir:
        return
    try:
        lock_fh = _lock_file(SESSIONS_PATH.with_suffix(".lock"))
        try:
            sessions: dict[str, str] = {}
            if SESSIONS_PATH.exists():
                raw = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    sessions = {str(k): str(v) for k, v in raw.items()}
            sessions.pop(session_id, None)
            sessions[session_id] = workspace_dir
            while len(sessions) > 100:
                oldest = next(iter(sessions))
                sessions.pop(oldest)
            _atomic_write_json(SESSIONS_PATH, sessions)
        finally:
            _unlock_file(lock_fh)
    except Exception as exc:  # noqa: BLE001
        print(f"[{SERVER_NAME}] note: could not cache session workspace: {exc}", file=sys.stderr)


def _lookup_session_workspace(session_id: str) -> str | None:
    """Retrieve previously recorded workspace_dir for session_id, if valid."""
    try:
        if not SESSIONS_PATH.exists():
            return None
        raw = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            val = raw.get(session_id)
            if isinstance(val, str) and val and os.path.isdir(val):
                return val
    except Exception:  # noqa: BLE001
        return None
    return None


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


def load_prompt_template() -> str:
    """Load the unified adaptive review prompt template."""
    if not PROMPT_FILE.exists():
        raise FileNotFoundError(f"Missing prompt template: {PROMPT_FILE}")
    return PROMPT_FILE.read_text(encoding="utf-8").strip()


def build_review_prompt(
    content: str,
    focus_files: list[str] | None,
    extra_instructions: str | None,
    git_scope: str | None,
) -> str:
    """Assemble a de-biased, structured review prompt for Codex."""
    sections = [load_prompt_template()]

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


def _extract_event_fields(
    event: dict[str, Any],
) -> tuple[str | None, str | None, str | None, bool, bool]:
    """Extract (session_id, agent_message, error_text, is_completed, is_failed) from one event."""
    sid: str | None = None
    for key in ("thread_id", "session_id"):
        val = event.get(key)
        if isinstance(val, str) and val:
            sid = val
            break
    if not sid:
        for container in ("thread", "session", "msg", "payload"):
            sub = event.get(container)
            if isinstance(sub, dict):
                val = sub.get("id") or sub.get("session_id") or sub.get("thread_id")
                if isinstance(val, str) and val:
                    sid = val
                    break

    agent_msg: str | None = None
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") == "agent_message":
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            agent_msg = text.strip()
    if agent_msg is None:
        msg = event.get("msg")
        if isinstance(msg, dict) and msg.get("type") == "agent_message":
            text = msg.get("message") or msg.get("text")
            if isinstance(text, str) and text.strip():
                agent_msg = text.strip()

    ev_type = str(event.get("type") or "").lower()
    is_completed = ev_type == "turn.completed"
    is_failed = ev_type == "turn.failed"
    err_text: str | None = None
    if ev_type in ("error", "turn.failed", "stream_error"):
        err_val = event.get("error") or event.get("message")
        if isinstance(err_val, dict):
            err_val = err_val.get("message") or json.dumps(err_val, ensure_ascii=False)
        if err_val:
            err_text = str(err_val)

    return sid, agent_msg, err_text, is_completed, is_failed


def parse_codex_jsonl(stdout_text: str) -> tuple[str | None, str, list[str], bool]:
    """Extract session ID, final agent message, errors, and turn failure flag from JSONL."""
    session_id: str | None = None
    messages: list[str] = []
    errors: list[str] = []
    turn_failed = False
    turn_completed = False

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

        sid, agent_msg, err_text, is_comp, is_fail = _extract_event_fields(event)
        if not session_id and sid:
            session_id = sid
        if agent_msg:
            messages.append(agent_msg)
        if is_comp:
            turn_completed = True
        if is_fail:
            turn_failed = True
        if err_text:
            errors.append(err_text)

    if errors and not turn_completed and not messages:
        turn_failed = True

    last_message = messages[-1] if messages else ""
    return session_id, last_message, errors, turn_failed


def _resolve_timeout_sec(args: dict[str, Any], config: dict[str, Any]) -> int:
    """Resolve subprocess timeout in seconds; 0 means unlimited (no timeout)."""
    raw = args.get("timeout_sec")
    if raw is None:
        raw = config.get("timeout_sec")
    timeout_sec = 0 if raw is None else int(raw)
    if timeout_sec < 0:
        raise ValueError("'timeout_sec' must be >= 0 (0 means unlimited).")
    return timeout_sec


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


def _ensure_jobs_dir() -> Path:
    """Ensure /tmp/codex-reviewer-jobs exists with owner-only permissions."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(JOBS_DIR, 0o700)
    except OSError:
        pass
    return JOBS_DIR


def _prune_old_jobs(max_age_sec: int = 7 * 86400) -> None:
    """Best-effort cleanup of completed job directories older than max_age_sec."""
    if not JOBS_DIR.exists():
        return
    cutoff = time.time() - max_age_sec
    try:
        for entry in JOBS_DIR.iterdir():
            if not entry.is_dir():
                continue
            done_file = entry / "done.json"
            if not done_file.exists():
                continue
            try:
                if done_file.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        pass


def _pid_definitely_dead(pid: Any) -> bool:
    """Return True only when OS confirms pid does not exist (ProcessLookupError)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False


def _terminate_process_group(pid: int | None) -> None:
    """Send SIGTERM then SIGKILL to a detached process group cleanly."""
    if not pid or pid <= 0:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if _pid_definitely_dead(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def _validate_job_id(job_id: str) -> Path:
    """Validate job_id format and resolve its directory under JOBS_DIR."""
    cleaned = job_id.strip()
    if not cleaned or not re.fullmatch(r"[A-Za-z0-9._-]+", cleaned):
        raise ValueError(f"Invalid job_id: {job_id}")
    job_dir = JOBS_DIR / cleaned
    if not job_dir.is_dir():
        raise FileNotFoundError(f"Job not found: {cleaned}")
    return job_dir


def start_background_job(request_data: dict[str, Any]) -> dict[str, Any]:
    """Persist request metadata and spawn a detached worker process."""
    _ensure_jobs_dir()
    _prune_old_jobs()

    job_id = f"rev-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}"
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(job_dir, 0o700)
    except OSError:
        pass

    request_data["job_id"] = job_id
    _atomic_write_json(job_dir / "request.json", request_data)

    now = time.time()
    report_path = str(job_dir / "report.md")
    status: dict[str, Any] = {
        "job_id": job_id,
        "kind": request_data["kind"],
        "state": "running",
        "model": request_data["model"],
        "reasoning_effort": request_data["effort"] or "model-default",
        "timeout_sec": request_data["timeout_sec"],
        "workspace_dir": request_data.get("workspace_dir"),
        "session_id": request_data.get("session_id"),
        "started_at": now,
        "heartbeat_ts": now,
        "elapsed_sec": 0.0,
        "worker_pid": None,
        "codex_pid": None,
        "report_path": report_path,
        "wait_command": f"codex-review-wait {job_id}",
    }
    _atomic_write_json(job_dir / "status.json", status)

    worker_cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", str(job_dir)]
    with open(job_dir / "worker.log", "a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            worker_cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            close_fds=True,
        )

    status["worker_pid"] = proc.pid
    _atomic_write_json(job_dir / "status.json", status)
    return status


def _run_streaming_codex(
    cmd: list[str],
    prompt_text: str,
    cwd: str | None,
    timeout_sec: int,
    out_path: Path,
    job_dir: Path,
    status: dict[str, Any],
    start_mono: float,
) -> tuple[int, str, str, str | None, bool, bool, bool]:
    """Run Codex CLI with streaming JSONL heartbeat, session capture, and cancel support."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd or None,
        start_new_session=True,
    )
    status["codex_pid"] = proc.pid
    _atomic_write_json(job_dir / "status.json", status)

    lock = threading.Lock()
    messages: list[str] = []
    errors: list[str] = []
    stderr_lines: list[str] = []
    state = {"session_id": status.get("session_id"), "completed": False, "failed": False}

    def _feed_stdin() -> None:
        try:
            if proc.stdin is not None:
                proc.stdin.write(prompt_text)
                proc.stdin.close()
        except OSError:
            pass

    def _read_stderr() -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            with lock:
                stderr_lines.append(line.rstrip("\n"))

    def _read_stdout() -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            raw = line.strip()
            if not raw or not raw.startswith("{"):
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            sid, agent_msg, err_text, is_comp, is_fail = _extract_event_fields(event)
            new_sid = None
            with lock:
                if sid and not state["session_id"]:
                    state["session_id"] = sid
                    status["session_id"] = sid
                    new_sid = sid
                if agent_msg:
                    messages.append(agent_msg)
                if is_comp:
                    state["completed"] = True
                if is_fail:
                    state["failed"] = True
                if err_text:
                    errors.append(err_text)
            if new_sid:
                _record_session_workspace(new_sid, cwd)
                _atomic_write_json(job_dir / "status.json", status)

    t_in = threading.Thread(target=_feed_stdin, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_in.start()
    t_err.start()
    t_out.start()

    canceled = False
    timed_out = False
    last_hb = time.monotonic()

    while proc.poll() is None:
        if (job_dir / "cancel.flag").exists():
            canceled = True
            _terminate_process_group(proc.pid)
            break
        elapsed_now = time.monotonic() - start_mono
        if timeout_sec > 0 and elapsed_now > timeout_sec:
            timed_out = True
            _terminate_process_group(proc.pid)
            break
        if time.monotonic() - last_hb >= 2.0:
            last_hb = time.monotonic()
            with lock:
                status["heartbeat_ts"] = time.time()
                status["elapsed_sec"] = round(elapsed_now, 2)
                _atomic_write_json(job_dir / "status.json", status)
        time.sleep(0.5)

    rc = proc.wait()
    if (job_dir / "cancel.flag").exists():
        canceled = True
    t_in.join(timeout=2.0)
    t_out.join(timeout=2.0)
    t_err.join(timeout=2.0)

    with lock:
        session_id = state["session_id"]
        turn_failed = bool(state["failed"])
        if errors and not state["completed"] and not messages:
            turn_failed = True
        jsonl_msg = messages[-1] if messages else ""
        stderr_text = "\n".join(stderr_lines).strip()
        if errors:
            joined = "\n".join(errors)
            stderr_text = f"{stderr_text}\n{joined}".strip() if stderr_text else joined

    file_text = out_path.read_text(encoding="utf-8").strip() if out_path.exists() else ""
    final_text = file_text or jsonl_msg
    return rc, final_text, stderr_text, session_id, turn_failed, canceled, timed_out


def run_worker_main(job_dir_str: str) -> int:
    """Detached worker entry point: executes Codex with no duration limit and writes done.json."""
    job_dir = Path(job_dir_str).resolve()
    req = json.loads((job_dir / "request.json").read_text(encoding="utf-8"))
    status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))

    kind = req["kind"]
    job_id = req["job_id"]
    model = req["model"]
    effort = req["effort"]
    codex_bin = req["codex_bin"]
    extra_args = req.get("extra_args") or []
    workspace_dir = req.get("workspace_dir")
    timeout_sec = int(req.get("timeout_sec") or 0)
    git_scope = req.get("git_scope")
    prompt_text = req["prompt"]
    initial_sid = req.get("session_id")

    start_mono = time.monotonic()
    rc = 1
    final_text = ""
    stderr_text = ""
    session_id = initial_sid
    turn_failed = False
    canceled = False
    timed_out = False
    used_subcommand = "exec resume" if kind == "followup" else "exec"
    fatal_exc: str | None = None

    try:
        if (job_dir / "cancel.flag").exists():
            canceled = True
        else:
            base_cmd = build_base_cmd(codex_bin, model, effort, workspace_dir, extra_args)
            with tempfile.TemporaryDirectory(prefix="codex-worker-") as tmpdir:
                out_path = Path(tmpdir) / "codex_output.md"

                if kind == "followup":
                    cmd = base_cmd + [
                        "exec",
                        "resume",
                        initial_sid,
                        "--skip-git-repo-check",
                        "--json",
                        "-o",
                        str(out_path),
                        "-",
                    ]
                    rc, final_text, stderr_text, session_id, turn_failed, canceled, timed_out = (
                        _run_streaming_codex(
                            cmd, prompt_text, workspace_dir, timeout_sec, out_path, job_dir, status, start_mono
                        )
                    )
                elif git_scope:
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
                    rc, final_text, stderr_text, session_id, turn_failed, canceled, timed_out = (
                        _run_streaming_codex(
                            review_cmd,
                            prompt_text,
                            workspace_dir,
                            timeout_sec,
                            out_path,
                            job_dir,
                            status,
                            start_mono,
                        )
                    )
                    if (
                        not canceled
                        and not timed_out
                        and rc != 0
                        and not final_text
                        and ("cannot be used with" in stderr_text or "Usage:" in stderr_text)
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
                        rc, final_text, stderr_text, session_id, turn_failed, canceled, timed_out = (
                            _run_streaming_codex(
                                exec_cmd,
                                prompt_text + git_hint,
                                workspace_dir,
                                timeout_sec,
                                out_path,
                                job_dir,
                                status,
                                start_mono,
                            )
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
                    rc, final_text, stderr_text, session_id, turn_failed, canceled, timed_out = (
                        _run_streaming_codex(
                            exec_cmd,
                            prompt_text,
                            workspace_dir,
                            timeout_sec,
                            out_path,
                            job_dir,
                            status,
                            start_mono,
                        )
                    )
    except Exception as exc:  # noqa: BLE001
        fatal_exc = str(exc)
    finally:
        elapsed = round(time.monotonic() - start_mono, 2)
        if session_id:
            _record_session_workspace(session_id, workspace_dir)

        if canceled or (job_dir / "cancel.flag").exists():
            final_state = "canceled"
        elif fatal_exc or timed_out or rc != 0 or turn_failed:
            final_state = "failed"
        else:
            final_state = "completed"

        is_error = final_state != "completed"
        title = "### Codex Follow-up Verification" if kind == "followup" else "### Codex Second-Opinion Review"
        header_lines = [
            title,
            f"- **job_id**: `{job_id}`",
            f"- **state**: `{final_state}`",
            f"- **model**: `{model}`",
            f"- **reasoning_effort**: `{effort or 'model-default'}`",
            f"- **session_id**: `{session_id or 'unavailable'}`",
            f"- **elapsed_sec**: `{elapsed}`",
            f"- **command**: `{used_subcommand}`",
        ]
        err_msg = (
            fatal_exc
            or ("Timed out." if timed_out else ("Canceled." if final_state == "canceled" else stderr_text))
        )
        if is_error and not err_msg:
            err_msg = f"Codex exited with code/signal {rc}."
        if is_error and err_msg:
            header_lines.append(f"- **error**: {err_msg}")

        body = final_text or (
            err_msg if is_error and err_msg else "(Codex produced an empty response.)"
        )
        report_md = f"{chr(10).join(header_lines)}\n\n---\n\n{body}\n"
        report_path = job_dir / "report.md"
        _atomic_write_text(report_path, report_md)

        done_payload = {
            "job_id": job_id,
            "kind": kind,
            "state": final_state,
            "is_error": is_error,
            "exit_code": rc,
            "model": model,
            "reasoning_effort": effort or "model-default",
            "session_id": session_id,
            "workspace_dir": workspace_dir,
            "elapsed_sec": elapsed,
            "command": used_subcommand,
            "report_path": str(report_path),
            "error": err_msg if is_error else None,
        }
        status.update(
            {
                "state": final_state,
                "is_error": is_error,
                "session_id": session_id,
                "elapsed_sec": elapsed,
                "heartbeat_ts": time.time(),
                "finished_at": time.time(),
            }
        )
        _atomic_write_json(job_dir / "status.json", status)
        _atomic_write_json(job_dir / "done.json", done_payload)

    return 1 if is_error else 0


def _format_launch_response(status: dict[str, Any]) -> dict[str, Any]:
    """Format immediate background-job launch response for MCP callers."""
    timeout_val = int(status.get("timeout_sec") or 0)
    summary = {
        "job_id": status["job_id"],
        "kind": status["kind"],
        "state": status["state"],
        "model": status["model"],
        "reasoning_effort": status["reasoning_effort"],
        "timeout": "unlimited" if timeout_val == 0 else f"{timeout_val}s",
        "workspace_dir": status.get("workspace_dir"),
        "report_path": status["report_path"],
        "wait_command": status["wait_command"],
        "next_action": (
            f"Run `{status['wait_command']}` via run_command (BypassSandbox: false, WaitMsBeforeAsync: 5000) "
            "and end your turn to sleep with zero polling until Codex finishes and wakes you up."
        ),
    }
    return {
        "content": [{"type": "text", "text": json.dumps(summary, indent=2, ensure_ascii=False)}],
        "isError": False,
    }


def handle_codex_review(args: dict[str, Any]) -> dict[str, Any]:
    """Validate inputs and launch a detached unlimited-duration background Codex review."""
    content = str(args.get("content") or "").strip()
    git_scope_raw = args.get("git_scope")
    git_scope = str(git_scope_raw).strip() if isinstance(git_scope_raw, str) and git_scope_raw.strip() else None

    if not content and not git_scope:
        raise ValueError("Provide non-empty 'content' or 'git_scope'.")
    if git_scope:
        _parse_git_scope(git_scope)

    workspace_dir = _validate_workspace(args.get("workspace_dir"))
    focus_files = args.get("focus_files") if isinstance(args.get("focus_files"), list) else None
    extra_instructions = args.get("extra_instructions") if isinstance(args.get("extra_instructions"), str) else None

    config = load_config()
    codex_bin = resolve_codex_bin(config)
    model, effort = resolve_model_and_effort(args.get("model"), args.get("reasoning_effort"), config)
    timeout_sec = _resolve_timeout_sec(args, config)
    extra_args = list(config.get("extra_codex_args") or [])

    full_prompt = build_review_prompt(
        content=content or f"Review the git changes specified by scope: {git_scope}",
        focus_files=focus_files,
        extra_instructions=extra_instructions,
        git_scope=git_scope,
    )

    status = start_background_job(
        {
            "kind": "review",
            "model": model,
            "effort": effort,
            "codex_bin": codex_bin,
            "extra_args": extra_args,
            "timeout_sec": timeout_sec,
            "workspace_dir": workspace_dir,
            "git_scope": git_scope,
            "prompt": full_prompt,
        }
    )
    return _format_launch_response(status)


def handle_codex_followup(args: dict[str, Any]) -> dict[str, Any]:
    """Validate inputs and launch a detached background Codex session follow-up."""
    session_id = str(args.get("session_id") or "").strip()
    question = str(args.get("question") or "").strip()
    if not session_id:
        raise ValueError("'session_id' is required.")
    if not question:
        raise ValueError("'question' is required.")

    raw_ws = args.get("workspace_dir")
    if isinstance(raw_ws, str) and raw_ws.strip():
        workspace_dir = _validate_workspace(raw_ws)
    else:
        saved_ws = _lookup_session_workspace(session_id)
        workspace_dir = _validate_workspace(saved_ws) if saved_ws else None

    config = load_config()
    codex_bin = resolve_codex_bin(config)
    model, effort = resolve_model_and_effort(args.get("model"), args.get("reasoning_effort"), config)
    timeout_sec = _resolve_timeout_sec(args, config)
    extra_args = list(config.get("extra_codex_args") or [])

    followup_prompt = (
        "Follow-up verification from the primary investigator. Answer concisely with concrete "
        "file:line, mathematical, or empirical evidence, and still ignore tone/style nitpicks:\n\n"
        f"{question}"
    )

    status = start_background_job(
        {
            "kind": "followup",
            "session_id": session_id,
            "model": model,
            "effort": effort,
            "codex_bin": codex_bin,
            "extra_args": extra_args,
            "timeout_sec": timeout_sec,
            "workspace_dir": workspace_dir,
            "prompt": followup_prompt,
        }
    )
    return _format_launch_response(status)


def handle_codex_review_status(args: dict[str, Any]) -> dict[str, Any]:
    """Query, retrieve, cancel, or list background Codex review jobs."""
    action = str(args.get("action") or "get").strip().lower()
    if action not in ("get", "cancel", "list"):
        raise ValueError("Invalid action. Must be 'get', 'cancel', or 'list'.")

    if action == "list":
        jobs: list[dict[str, Any]] = []
        if JOBS_DIR.exists():
            for entry in JOBS_DIR.iterdir():
                if not entry.is_dir():
                    continue
                done_path = entry / "done.json"
                status_path = entry / "status.json"
                try:
                    if done_path.exists():
                        data = json.loads(done_path.read_text(encoding="utf-8"))
                    elif status_path.exists():
                        data = json.loads(status_path.read_text(encoding="utf-8"))
                    else:
                        continue
                    if isinstance(data, dict):
                        jobs.append(data)
                except Exception:  # noqa: BLE001
                    continue
        jobs.sort(key=lambda x: float(x.get("started_at") or 0.0), reverse=True)
        return {
            "content": [{"type": "text", "text": json.dumps({"jobs": jobs[:20]}, indent=2, ensure_ascii=False)}],
            "isError": False,
        }

    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        raise ValueError(f"'job_id' is required for action='{action}'.")
    job_dir = _validate_job_id(job_id)

    if action == "cancel":
        done_path = job_dir / "done.json"
        if done_path.exists():
            done = json.loads(done_path.read_text(encoding="utf-8"))
            return {
                "content": [{"type": "text", "text": json.dumps({"note": "Job already finished.", **done}, indent=2, ensure_ascii=False)}],
                "isError": False,
            }
        (job_dir / "cancel.flag").write_text("cancel\n", encoding="utf-8")
        status_path = job_dir / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
        # Terminate only Codex so the worker cleanly finishes its finally block and writes done.json.
        _terminate_process_group(status.get("codex_pid"))
        status["state"] = "canceling"
        return {
            "content": [{"type": "text", "text": json.dumps(status, indent=2, ensure_ascii=False)}],
            "isError": False,
        }

    # action == "get"
    done_path = job_dir / "done.json"
    if done_path.exists():
        done = json.loads(done_path.read_text(encoding="utf-8"))
        report_path = job_dir / "report.md"
        report_text = report_path.read_text(encoding="utf-8").strip() if report_path.exists() else ""
        return {
            "content": [{"type": "text", "text": report_text or json.dumps(done, indent=2, ensure_ascii=False)}],
            "isError": bool(done.get("is_error")),
        }

    status_path = job_dir / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {"job_id": job_id}
    if _pid_definitely_dead(status.get("worker_pid")) and not done_path.exists():
        log_path = job_dir / "worker.log"
        log_tail = ""
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            log_tail = "\n".join(lines[-25:])
        status["state"] = "dead"
        status["error"] = "Worker process exited unexpectedly before writing done.json."
        if log_tail:
            status["worker_log_tail"] = log_tail
        return {
            "content": [{"type": "text", "text": json.dumps(status, indent=2, ensure_ascii=False)}],
            "isError": True,
        }

    started_at = float(status.get("started_at") or time.time())
    status["elapsed_sec"] = round(time.time() - started_at, 2)
    return {
        "content": [{"type": "text", "text": json.dumps(status, indent=2, ensure_ascii=False)}],
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

    def _mutate(cfg: dict[str, Any]) -> dict[str, Any]:
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
            if timeout_val < 0:
                raise ValueError("'timeout_sec' must be >= 0 (0 means unlimited).")
            cfg["timeout_sec"] = timeout_val
        if args.get("codex_bin") is not None:
            cfg["codex_bin"] = str(args["codex_bin"]).strip()
        if args.get("extra_codex_args") is not None:
            if not isinstance(args["extra_codex_args"], list):
                raise ValueError("'extra_codex_args' must be an array of strings.")
            cfg["extra_codex_args"] = [str(x) for x in args["extra_codex_args"]]
        return cfg

    if action == "reset":
        cfg = _merge_config({})
        save_config(cfg)
    elif action == "set":
        cfg = update_config(_mutate)
    else:
        cfg = load_config()

    resolved_model, resolved_effort = resolve_model_and_effort(None, None, cfg)
    codex_version = _detect_codex_version(str(cfg.get("codex_bin") or "codex"))
    raw_timeout = cfg.get("timeout_sec")
    resolved_timeout: str | int = "unlimited" if (raw_timeout is None or int(raw_timeout) == 0) else int(raw_timeout)
    payload = {
        "config_path": str(CONFIG_PATH),
        "jobs_dir": str(JOBS_DIR),
        "codex_version": codex_version,
        "resolved_model": resolved_model,
        "resolved_reasoning_effort": resolved_effort or "model-default",
        "resolved_timeout": resolved_timeout,
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
            "Launch a strictly read-only, unlimited-duration background Codex second-opinion review on "
            "plans, scientific methods/conclusions, code, or any combination thereof. Returns job_id "
            "immediately; run `codex-review-wait <job_id>` via run_command (WaitMsBeforeAsync=5000) "
            "and end your turn to sleep until automatic wakeup."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": [],
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Objective, de-biased summary of the plan, methodology, equations, code scope, or conclusion to audit.",
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
                    "description": "Optional git diff scope: 'uncommitted', 'base:<branch>', or 'commit:<sha>'.",
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
                "timeout_sec": {
                    "type": "integer",
                    "description": "Optional one-off timeout in seconds (0 = unlimited, default).",
                },
            },
        },
    },
    {
        "name": "codex_followup",
        "description": (
            "Launch a detached background follow-up question against a prior Codex review session_id. "
            "Returns job_id immediately; wait via `codex-review-wait <job_id>`."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_id", "question"],
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID returned by a previous codex_review job.",
                },
                "question": {
                    "type": "string",
                    "description": "Specific technical follow-up or counter-evidence to verify.",
                },
                "workspace_dir": {
                    "type": "string",
                    "description": "Optional workspace directory path (auto-restores original session workspace if omitted).",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override for this follow-up turn.",
                },
                "reasoning_effort": {
                    "type": "string",
                    "description": "Optional reasoning effort override for this follow-up turn.",
                },
                "timeout_sec": {
                    "type": "integer",
                    "description": "Optional one-off timeout in seconds (0 = unlimited, default).",
                },
            },
        },
    },
    {
        "name": "codex_review_status",
        "description": (
            "Get final report/live status of a background Codex job, cancel a running job, or list recent jobs. "
            "Do NOT poll this in a loop—use `codex-review-wait <job_id>` for automatic wakeup."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get", "cancel", "list"],
                    "description": "'get' job status/report, 'cancel' a running job, or 'list' recent jobs.",
                },
                "job_id": {
                    "type": "string",
                    "description": "Job ID returned by codex_review or codex_followup (required for 'get' and 'cancel').",
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
                    "description": "Background worker timeout in seconds (0 = unlimited, no time limit).",
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
    "codex_review_status": handle_codex_review_status,
    "codex_review_config": handle_codex_review_config,
}


def read_rpc_message() -> tuple[Any, str]:
    """Read one JSON-RPC message supporting both JSONL and Content-Length framing."""
    stream = sys.stdin.buffer
    while True:
        line = stream.readline()
        if not line:
            return _EOF, "jsonl"
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith((b"{", b"[")) and b":" in stripped:
            headers: dict[str, str] = {}
            header_line = line
            while header_line and header_line.strip():
                decoded = header_line.decode("ascii", errors="replace")
                if ":" in decoded:
                    k, v = decoded.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
                header_line = stream.readline()
            if "content-length" not in headers:
                raise ValueError("Missing Content-Length header in framed RPC message.")
            length = int(headers["content-length"])
            body = stream.read(length)
            if len(body) < length:
                return _EOF, "content-length"
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


def dispatch_request(msg: Any) -> dict[str, Any] | None:
    """Route a single MCP JSON-RPC request and return a response object (None for notifications)."""
    if not isinstance(msg, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request: expected JSON object."},
        }

    req_id = msg.get("id")
    method = msg.get("method", "")
    raw_params = msg.get("params")
    if raw_params is None:
        params: dict[str, Any] = {}
    elif isinstance(raw_params, dict):
        params = raw_params
    else:
        return None if req_id is None else {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": "Invalid params: expected JSON object."},
        }

    if req_id is None:
        return None

    try:
        if method == "initialize":
            req_proto = str(params.get("protocolVersion") or "").strip()
            proto = req_proto if req_proto in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
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
            tool_args = params.get("arguments")
            if tool_args is None:
                tool_args = {}
            elif not isinstance(tool_args, dict):
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": "Invalid tool arguments: expected JSON object."},
                }
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
    """Run either detached background worker mode or stdio MCP JSON-RPC loop."""
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        return run_worker_main(sys.argv[2])

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

        if msg is _EOF:
            return 0
        active_framing = framing
        response = dispatch_request(msg)
        if response is not None:
            write_rpc_message(response, active_framing)


if __name__ == "__main__":
    raise SystemExit(main())
