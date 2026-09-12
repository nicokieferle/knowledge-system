#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

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
if [[ ! "${RESTORE_DATABASE}" =~ ^[A-Za-z_][A-Za-z0-9_]{0,62}$ ]]; then
  printf 'RESTORE_DATABASE must be a simple local database name\n' >&2
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

sequence_sql=$(cat <<'SQL'
DO $restore_sequence$
DECLARE
  next_message_id bigint;
BEGIN
  SELECT COALESCE(max(id) + 1, 1) INTO next_message_id FROM public.messages;
  EXECUTE format(
    'ALTER SEQUENCE public.messages_id_seq RESTART WITH %s',
    next_message_id
  );
END
$restore_sequence$;
SQL
)
integrity_guard_sql=$(durable_integrity_guard_sql)
expected_tables=$(IFS=,; printf '%s' "${DURABLE_TABLES[*]}")

# Copy the archive and extract only the allowlisted table data to mode-0600 files
# inside the database container. Archive SEQUENCE SET entries use non-transactional
# setval(), so they are excluded and replaced by transactional ALTER SEQUENCE.
# psql then executes the archive SQL, sequence repair and integrity
# guard in one transaction. The accepted-review FK is initially deferred; all
# other FKs and every immutability trigger remain active throughout the restore.
"${COMPOSE_COMMAND[@]}" exec -T postgres sh -eu -c '
  database=$1
  sequence_sql=$2
  integrity_guard_sql=$3
  expected_tables=$4
  shift 4
  umask 077
  archive=$(mktemp /tmp/knowledge-durable-archive.XXXXXX.dump)
  toc=$(mktemp /tmp/knowledge-durable-archive.XXXXXX.toc)
  filtered_toc=$(mktemp /tmp/knowledge-durable-archive.XXXXXX.filtered.toc)
  restore_sql=$(mktemp /tmp/knowledge-durable-restore.XXXXXX.sql)
  cleanup() { rm -f -- "$archive" "$toc" "$filtered_toc" "$restore_sql"; }
  trap cleanup EXIT HUP INT TERM
  cat >"$archive"
  pg_restore --list "$archive" >"$toc"
  old_ifs=$IFS
  IFS=,
  for table in $expected_tables; do
    count=$(grep -F -c " TABLE DATA public $table " "$toc" || true)
    if [ "$count" != 1 ]; then
      printf "Archive must contain exactly one data entry for every durable table\n" >&2
      exit 1
    fi
  done
  IFS=$old_ifs
  grep -F -v " SEQUENCE SET " "$toc" >"$filtered_toc"
  pg_restore --username="$POSTGRES_USER" --file="$restore_sql" \
    --use-list="$filtered_toc" --data-only --no-owner --no-privileges \
    --exit-on-error "$@" "$archive"
  restrict_count=$(head -n 10 "$restore_sql" | grep -F -c "\\restrict " || true)
  unrestrict_count=$(tail -n 10 "$restore_sql" | grep -F -c "\\unrestrict " || true)
  if [ "$restrict_count" != 1 ] || [ "$unrestrict_count" != 1 ]; then
    printf "Restore SQL is missing PostgreSQL restriction guards\n" >&2
    exit 1
  fi
  psql --no-psqlrc --username="$POSTGRES_USER" --dbname="$database" --quiet \
    --set=ON_ERROR_STOP=1 --single-transaction \
    --file="$restore_sql" --command="$sequence_sql" --command="$integrity_guard_sql"
' restore-data "${RESTORE_DATABASE}" "${sequence_sql}" "${integrity_guard_sql}" \
  "${expected_tables}" \
  "${DURABLE_PG_RESTORE_FILTER_ARGS[@]}" <"${backup_file}" >/dev/null

printf 'restore_database=%s\n' "${RESTORE_DATABASE}"
printf 'restore_atomic=true\n'
printf 'restore_triggers_disabled=false\n'
printf 'restore_psql_restricted=true\n'
printf 'restore_integrity=pass\n'
printf 'restore_completed=true\n'
