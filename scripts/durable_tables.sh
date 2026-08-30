#!/usr/bin/env bash

# Shared by backup and restore so the durable table allowlist has one operational definition.
readonly DURABLE_TABLES=(
  conversations
  messages
  conversation_summaries
  proposal_suggestions
  proposals
)

configure_compose_command() {
  if [[ ! -v COMPOSE_FILE ]]; then
    COMPOSE_FILE=compose.server.yml
  fi
  if [[ ! -v COMPOSE_ENV_FILE ]]; then
    COMPOSE_ENV_FILE=.env.server
  fi

  COMPOSE_COMMAND=(docker compose)
  if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
    COMPOSE_COMMAND+=(--project-name "${COMPOSE_PROJECT_NAME}")
  fi
  if [[ -n "${COMPOSE_ENV_FILE}" ]]; then
    [[ -r "${COMPOSE_ENV_FILE}" ]] || {
      printf 'Compose environment file is not readable: %s\n' "${COMPOSE_ENV_FILE}" >&2
      return 1
    }
    COMPOSE_COMMAND+=(--env-file "${COMPOSE_ENV_FILE}")
  fi
  COMPOSE_COMMAND+=(-f "${COMPOSE_FILE}")
}

durable_table_args() {
  DURABLE_TABLE_ARGS=()
  local table
  for table in "${DURABLE_TABLES[@]}"; do
    DURABLE_TABLE_ARGS+=("--table=public.${table}")
  done
}

durable_nonempty_sql() {
  local parts=()
  local table
  for table in "${DURABLE_TABLES[@]}"; do
    parts+=("(SELECT count(*) FROM public.${table})")
  done
  local joined
  joined=$(IFS=+; printf '%s' "${parts[*]}")
  printf 'SELECT %s;' "${joined}"
}

durable_missing_sql() {
  local values=()
  local table
  for table in "${DURABLE_TABLES[@]}"; do
    values+=("('public.${table}')")
  done
  local joined
  joined=$(IFS=,; printf '%s' "${values[*]}")
  printf "SELECT count(*) FROM (VALUES %s) AS required(name) WHERE to_regclass(name) IS NULL;" \
    "${joined}"
}
