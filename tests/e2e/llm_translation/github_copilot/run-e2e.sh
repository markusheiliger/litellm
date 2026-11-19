#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
env_file="$SCRIPT_DIR/.env"
auth_args=()
command=(
  env PYTHON_DOTENV_DISABLED=1 E2E_GITHUB_COPILOT_LIVE=1 E2E_PROXY_WIDE_CONFIG_STACK=1
  python "$SCRIPT_DIR/runner.py"
)

while [[ $# -gt 0 ]]; do
case "$1" in
  --help|-h)
    printf '%s\n' \
      'Usage: run-e2e.sh [--env-file FILE] [--auth | pytest arguments]' \
      '' \
      'Run all Copilot API tests against an already-running, exclusive test stack.' \
      'Set LITELLM_PROXY_URL and LITELLM_MASTER_KEY explicitly.' \
      'Without --env-file, use .env beside this script when present, otherwise exported settings.' \
      'The selected dotenv file overrides exported settings. Other dotenv files are not loaded.' \
      'Forward pytest options such as -k, --collect-only and --junitxml normally.' \
      'Use --auth for configured *_LOGIN accounts and APP_ORGANIZATION registration and installation.' \
      'Passwords, MFA and final approval remain interactive. Setup does not run tests.' \
      'This runner does not start services or run browser tests.'
    exit 0
    ;;
  --env-file)
    if [[ -z "${2:-}" || ! -f "$2" || ! -r "$2" ]]; then
      printf '%s\n' 'Provide a readable dotenv file after --env-file.' >&2
      exit 2
    fi
    env_file="$2"
    if [[ "$env_file" != /* ]]; then
      env_file="$PWD/$env_file"
    fi
    shift 2
    ;;
  --auth)
    auth_args=(--auth)
    shift
    ;;
  *)
    break
    ;;
esac
done
if [[ -e "$env_file" || -L "$env_file" ]]; then
  if [[ ! -f "$env_file" || ! -r "$env_file" ]]; then
    printf '%s\n' 'The selected dotenv file must be a readable regular file.' >&2
    exit 2
  fi
  command=(python -m dotenv -f "$env_file" run --override env E2E_GITHUB_COPILOT_ENV_FILE="$env_file" "${command[@]}")
else
  command=(env E2E_GITHUB_COPILOT_ENV_FILE= "${command[@]}")
fi
if [[ "${1:-}" == -- ]]; then
  shift
fi

cd "$REPO_ROOT"
export PYTHON_DOTENV_DISABLED=1
export PYTHONPATH="$REPO_ROOT/tests/e2e${PYTHONPATH:+:$PYTHONPATH}"
exec uv run --no-sync --no-env-file "${command[@]}" "${auth_args[@]}" "$@"
