#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${INSPECTION_ENV_FILE:-/etc/inspection-bot/inspection-bot.env}"
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

: "${TELEGRAM_BOT_TOKEN:?Missing TELEGRAM_BOT_TOKEN}"
: "${THINKTRACE_BASE_URL:?Missing THINKTRACE_BASE_URL}"
: "${TELEGRAM_BRIDGE_TOKEN:?Missing TELEGRAM_BRIDGE_TOKEN}"

export INSPECTION_DATA_ROOT="${INSPECTION_DATA_ROOT:-/var/lib/inspection-bot}"

mkdir -p "${INSPECTION_DATA_ROOT}"

if [[ ! -f "${INSPECTION_DATA_ROOT}/projects.json" ]]; then
  cp "${REPO_ROOT}/projects.json" "${INSPECTION_DATA_ROOT}/projects.json"
fi

if [[ ! -d "${INSPECTION_DATA_ROOT}/templates" ]]; then
  mkdir -p "${INSPECTION_DATA_ROOT}/templates"
  cp -R "${REPO_ROOT}/templates/." "${INSPECTION_DATA_ROOT}/templates/"
fi

exec "${VENV_PYTHON}" "${REPO_ROOT}/main.py"
