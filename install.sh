#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
CONFIG_DIR="${HOME}/.config/codex-reviewer"
CONFIG_FILE="${CONFIG_DIR}/config.json"

echo "[1/5] Checking prerequisites..."
if ! command -v agy >/dev/null 2>&1; then
  echo "Error: 'agy' (Antigravity CLI) not found in PATH." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: 'python3' not found in PATH." >&2
  exit 1
fi
if ! command -v codex >/dev/null 2>&1; then
  echo "Warning: 'codex' not found in PATH. Install Codex CLI and run 'codex login' before reviewing." >&2
fi

echo "[2/5] Installing codex-review-config and codex-review-run CLIs to ${BIN_DIR}/..."
mkdir -p "${BIN_DIR}"
cp "${SCRIPT_DIR}/bin/codex-review-config" "${BIN_DIR}/codex-review-config"
cp "${SCRIPT_DIR}/bin/codex-review-run" "${BIN_DIR}/codex-review-run"
chmod +x "${BIN_DIR}/codex-review-config" "${BIN_DIR}/codex-review-run"

echo "[3/5] Initializing default runtime config (preserving existing if present)..."
mkdir -p "${CONFIG_DIR}"
if [[ ! -f "${CONFIG_FILE}" ]]; then
  cat > "${CONFIG_FILE}" <<'EOF'
{
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
    "terra": "gpt-6.1-terra"
  }
}
EOF
  echo "Created ${CONFIG_FILE}"
else
  echo "Preserved existing ${CONFIG_FILE}"
fi

echo "[4/5] Validating plugin structure..."
agy plugin validate "${SCRIPT_DIR}" --strict

echo "[5/5] Installing plugin into Antigravity (AGY)..."
agy plugin install "${SCRIPT_DIR}"

echo ""
echo "Done! Verify with: agy plugin list"
echo "Ensure ${BIN_DIR} is in your PATH, then start a new 'agy' session."
