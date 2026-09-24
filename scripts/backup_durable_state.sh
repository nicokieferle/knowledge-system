#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
# shellcheck source=durable_tables.sh
source "${SCRIPT_DIR}/durable_tables.sh"

: "${BACKUP_DIR:=/data/knowledgesystem/backups}"
if [[ "${BACKUP_DIR}" == "/data/knowledgesystem/postgres"* ]]; then
  printf 'Backup directory must not be inside the PostgreSQL data directory\n' >&2
  exit 2
fi

cd "${PROJECT_ROOT}"
configure_compose_command
durable_pg_dump_table_args
command -v docker >/dev/null || {
  printf 'docker is required\n' >&2
  exit 2
}

install -d -m 0700 "${BACKUP_DIR}"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
temporary_file=$(mktemp "${BACKUP_DIR}/.durable-state-${timestamp}.XXXXXX.dump")
archive_suffix=$(basename -- "${temporary_file}")
archive_suffix=${archive_suffix#*.durable-state-${timestamp}.}
final_file="${BACKUP_DIR}/durable-state-${timestamp}-${archive_suffix}"
trap 'rm -f -- "${temporary_file}"' EXIT
chmod 0600 "${temporary_file}"

"${COMPOSE_COMMAND[@]}" exec -T postgres \
  sh -eu -c 'exec pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" "$@"' \
  durable-backup \
  --format=custom \
  --data-only \
  --no-owner \
  --no-privileges \
  "${DURABLE_PG_DUMP_TABLE_ARGS[@]}" >"${temporary_file}"

[[ -s "${temporary_file}" ]] || {
  printf 'pg_dump produced an empty archive\n' >&2
  exit 1
}
"${COMPOSE_COMMAND[@]}" exec -T postgres pg_restore --list \
  <"${temporary_file}" >/dev/null
mv -- "${temporary_file}" "${final_file}"
trap - EXIT

printf 'backup_file=%s\n' "${final_file}"
printf 'backup_bytes=%s\n' "$(wc -c <"${final_file}")"
printf 'backup_format=custom\n'
printf 'backup_archive_check=pass\n'
