#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
# shellcheck source=durable_tables.sh
source "${SCRIPT_DIR}/durable_tables.sh"

backup_file=${1:-}
: "${RESTORE_DATABASE:=}"
if [[ -z "${backup_file}" || ! -r "${backup_file}" ]]; then
  printf 'Usage: RESTORE_DATABASE=<fresh-db> %s <durable-backup.dump>\n' "$0" >&2
  exit 2
fi
if [[ -z "${RESTORE_DATABASE}" ]]; then
  printf 'RESTORE_DATABASE is required and must identify a fresh database\n' >&2
  exit 2
fi

cd "${PROJECT_ROOT}"
configure_compose_command
durable_pg_restore_filter_args

# Production restore is intentionally unsupported in V3.0. Recovery must target a
# separately created database which can be verified before any controlled cutover.
if ! "${COMPOSE_COMMAND[@]}" exec -T postgres sh -eu -c \
  'test "$1" != "$POSTGRES_DB"' restore-safety "${RESTORE_DATABASE}"; then
  printf 'Refusing to restore directly into the configured production database\n' >&2
  exit 2
fi

missing_sql=$(durable_missing_sql)
missing_count=$("${COMPOSE_COMMAND[@]}" exec -T postgres sh -eu -c \
  'exec psql --username="$POSTGRES_USER" --dbname="$1" --tuples-only --no-align --command="$2"' \
  restore-schema-check "${RESTORE_DATABASE}" "${missing_sql}")
if [[ "${missing_count}" != "0" ]]; then
  printf 'Restore target is missing durable tables; initialize its schema first\n' >&2
  exit 2
fi

nonempty_sql=$(durable_nonempty_sql)
existing_count=$("${COMPOSE_COMMAND[@]}" exec -T postgres sh -eu -c \
  'exec psql --username="$POSTGRES_USER" --dbname="$1" --tuples-only --no-align --command="$2"' \
  restore-empty-check "${RESTORE_DATABASE}" "${nonempty_sql}")
if [[ "${existing_count}" != "0" ]]; then
  printf 'Refusing to restore into a database containing durable rows\n' >&2
  exit 3
fi

"${COMPOSE_COMMAND[@]}" exec -T postgres sh -eu -c \
  'exec pg_restore --list' restore-archive-check <"${backup_file}" >/dev/null

"${COMPOSE_COMMAND[@]}" exec -T postgres sh -eu -c \
  'database=$1; shift; exec pg_restore --username="$POSTGRES_USER" --dbname="$database" \
    --data-only --no-owner --no-privileges --single-transaction --exit-on-error \
    --disable-triggers "$@"' \
  restore-data "${RESTORE_DATABASE}" "${DURABLE_PG_RESTORE_FILTER_ARGS[@]}" <"${backup_file}"

sequence_sql="
  SELECT setval(
    pg_get_serial_sequence('public.messages', 'id'),
    COALESCE((SELECT max(id) FROM public.messages), 1),
    EXISTS (SELECT 1 FROM public.messages)
  );
"
"${COMPOSE_COMMAND[@]}" exec -T postgres sh -eu -c \
  'exec psql --username="$POSTGRES_USER" --dbname="$1" --quiet --command="$2"' \
  sequence-reset "${RESTORE_DATABASE}" "${sequence_sql}" >/dev/null

integrity_sql=$(durable_integrity_sql)
integrity_violation_count=$("${COMPOSE_COMMAND[@]}" exec -T postgres sh -eu -c \
  'exec psql --username="$POSTGRES_USER" --dbname="$1" --tuples-only --no-align --command="$2"' \
  restore-integrity-check "${RESTORE_DATABASE}" "${integrity_sql}")
if [[ "${integrity_violation_count}" != "0" ]]; then
  printf 'Restored durable state failed integrity check\n' >&2
  exit 1
fi

printf 'restore_database=%s\n' "${RESTORE_DATABASE}"
printf 'restore_completed=true\n'
