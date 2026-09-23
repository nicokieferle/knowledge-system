#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
cd "${PROJECT_ROOT}"

run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
project_name="knowledge-v30-smoke-${run_id,,}"
temporary_root=$(mktemp -d "/tmp/knowledge-v30-smoke.${run_id}.XXXXXX")
export SMOKE_POSTGRES_PASSWORD
SMOKE_POSTGRES_PASSWORD=$(openssl rand -hex 32)
export SMOKE_IMAGE_NAME="knowledge-system-v30-smoke:${run_id,,}"
compose=(docker compose --project-name "${project_name}" -f compose.v30-smoke.yml)

cleanup() {
  local status=$?
  printf 'cleanup_project=%s\n' "${project_name}"
  if [[ "${project_name}" != knowledge-v30-smoke-* ]]; then
    printf 'Refusing cleanup for unexpected Compose project\n' >&2
    exit 1
  fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  if [[ "${SMOKE_IMAGE_NAME}" == knowledge-system-v30-smoke:* ]]; then
    docker image rm "${SMOKE_IMAGE_NAME}" >/dev/null 2>&1 || true
  else
    printf 'Refusing cleanup for unexpected smoke image: %s\n' "${SMOKE_IMAGE_NAME}" >&2
  fi
  if [[ "${temporary_root}" == /tmp/knowledge-v30-smoke.* ]]; then
    rm -rf -- "${temporary_root}"
  else
    printf 'Refusing cleanup for unexpected temporary path: %s\n' "${temporary_root}" >&2
  fi
  trap - EXIT
  exit "${status}"
}
trap cleanup EXIT

printf 'smoke_project=%s\n' "${project_name}"
printf 'smoke_data_scope=project_named_volume_only\n'
"${compose[@]}" build smoke
"${compose[@]}" up -d postgres

smoke_output=$("${compose[@]}" run --rm smoke)
printf '%s\n' "${smoke_output}"

source_url="postgresql://knowledge_smoke:${SMOKE_POSTGRES_PASSWORD}@postgres:5432/knowledge_v30_smoke"
restore_url="postgresql://knowledge_smoke:${SMOKE_POSTGRES_PASSWORD}@postgres:5432/knowledge_v30_restore"
source_fingerprint=$("${compose[@]}" run --rm \
  -e DATABASE_URL="${source_url}" \
  --entrypoint python smoke /app/scripts/durable_state_fingerprint.py)

backup_output=$(BACKUP_DIR="${temporary_root}/backups" \
  COMPOSE_FILE=compose.v30-smoke.yml \
  COMPOSE_ENV_FILE= \
  COMPOSE_PROJECT_NAME="${project_name}" \
  bash "${SCRIPT_DIR}/backup_durable_state.sh")
printf '%s\n' "${backup_output}"
backup_file=$(printf '%s\n' "${backup_output}" | sed -n 's/^backup_file=//p')
[[ -n "${backup_file}" && -s "${backup_file}" ]]

archive_entries=$("${compose[@]}" exec -T postgres pg_restore --list <"${backup_file}" | wc -l)
printf 'backup_archive_entries=%s\n' "${archive_entries}"

"${compose[@]}" exec -T postgres sh -eu -c \
  'exec createdb --username="$POSTGRES_USER" "$1"' create-restore-db knowledge_v30_restore
"${compose[@]}" run --rm -e DATABASE_URL="${restore_url}" smoke knowledge init-db

RESTORE_DATABASE=knowledge_v30_restore \
COMPOSE_FILE=compose.v30-smoke.yml \
COMPOSE_ENV_FILE= \
COMPOSE_PROJECT_NAME="${project_name}" \
  bash "${SCRIPT_DIR}/restore_durable_state.sh" "${backup_file}"

restore_fingerprint=$("${compose[@]}" run --rm \
  -e DATABASE_URL="${restore_url}" \
  --entrypoint python smoke /app/scripts/durable_state_fingerprint.py)
[[ "${source_fingerprint}" == "${restore_fingerprint}" ]]
printf 'restore_verified=true\n'

set +e
RESTORE_DATABASE=knowledge_v30_restore \
COMPOSE_FILE=compose.v30-smoke.yml \
COMPOSE_ENV_FILE= \
COMPOSE_PROJECT_NAME="${project_name}" \
  bash "${SCRIPT_DIR}/restore_durable_state.sh" "${backup_file}" \
  >"${temporary_root}/defensive-restore.log" 2>&1
defensive_status=$?
set -e
[[ "${defensive_status}" -ne 0 ]]
after_refusal_fingerprint=$("${compose[@]}" run --rm \
  -e DATABASE_URL="${restore_url}" \
  --entrypoint python smoke /app/scripts/durable_state_fingerprint.py)
[[ "${restore_fingerprint}" == "${after_refusal_fingerprint}" ]]
printf 'defensive_restore_into_nonempty_db=true\n'
printf 'production_compose_touched=false\n'
printf 'production_data_path_touched=false\n'
