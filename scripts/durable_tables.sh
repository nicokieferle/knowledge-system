#!/usr/bin/env bash

# Shared by backup and restore so the durable table allowlist has one operational definition.
readonly DURABLE_TABLES=(
  conversations
  client_states
  client_conversations
  client_message_bindings
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

durable_pg_dump_table_args() {
  DURABLE_PG_DUMP_TABLE_ARGS=()
  local table
  for table in "${DURABLE_TABLES[@]}"; do
    DURABLE_PG_DUMP_TABLE_ARGS+=("--table=public.${table}")
  done
}

durable_pg_restore_filter_args() {
  # pg_restore table patterns are not schema-qualified. Pair unqualified table
  # names with an explicit schema filter to preserve the durable allowlist.
  DURABLE_PG_RESTORE_FILTER_ARGS=("--schema=public")
  local table
  for table in "${DURABLE_TABLES[@]}"; do
    DURABLE_PG_RESTORE_FILTER_ARGS+=("--table=${table}")
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

durable_integrity_sql() {
  cat <<'SQL'
SELECT
  (SELECT count(*)
   FROM public.client_states cs
   LEFT JOIN public.conversations c ON c.id = cs.active_conversation_id
   WHERE cs.active_conversation_id IS NOT NULL AND c.id IS NULL)
  +
  (SELECT count(*)
   FROM public.client_conversations cc
   LEFT JOIN public.client_states cs
     ON cs.client_type = cc.client_type
    AND cs.external_chat_id = cc.external_chat_id
    AND cs.external_user_id = cc.external_user_id
   WHERE cs.client_type IS NULL)
  +
  (SELECT count(*)
   FROM public.client_conversations cc
   LEFT JOIN public.conversations c ON c.id = cc.conversation_id
   WHERE c.id IS NULL)
  +
  (SELECT count(*)
   FROM public.client_message_bindings cmb
   LEFT JOIN public.conversations c ON c.id = cmb.conversation_id
   WHERE c.id IS NULL)
  +
  (SELECT count(*)
   FROM public.messages m
   LEFT JOIN public.conversations c ON c.id = m.conversation_id
   WHERE c.id IS NULL)
  +
  (SELECT count(*)
   FROM public.conversation_summaries cs
   LEFT JOIN public.conversations c ON c.id = cs.conversation_id
   WHERE c.id IS NULL)
  +
  (SELECT count(*)
   FROM public.conversation_summaries cs
   LEFT JOIN public.messages m
     ON m.conversation_id = cs.conversation_id
    AND m.id = cs.through_message_id
   WHERE m.id IS NULL)
  +
  (SELECT count(*)
   FROM public.proposal_suggestions ps
   LEFT JOIN public.conversations c ON c.id = ps.conversation_id
   WHERE c.id IS NULL)
  +
  (SELECT count(*)
   FROM public.proposal_suggestions ps
   LEFT JOIN public.messages m
     ON m.conversation_id = ps.conversation_id
    AND m.id = ps.trigger_message_id
   WHERE m.id IS NULL)
  +
  (SELECT count(*)
   FROM public.proposal_suggestions ps
   LEFT JOIN public.messages m
     ON m.conversation_id = ps.conversation_id
    AND m.id = ps.resolution_message_id
   WHERE ps.resolution_message_id IS NOT NULL AND m.id IS NULL)
  +
  (SELECT count(*)
   FROM public.proposals p
   LEFT JOIN public.conversations c ON c.id = p.conversation_id
   WHERE c.id IS NULL)
  +
  (SELECT count(*)
   FROM public.proposals p
   LEFT JOIN public.messages m
     ON m.conversation_id = p.conversation_id
    AND m.id = p.trigger_message_id
   WHERE m.id IS NULL)
  +
  (SELECT count(*)
   FROM public.proposals p
   LEFT JOIN public.proposal_suggestions ps ON ps.id = p.source_suggestion_id
   WHERE p.source_suggestion_id IS NOT NULL AND ps.id IS NULL);
SQL
}
